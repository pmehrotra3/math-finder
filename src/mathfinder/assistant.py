"""The assistant in the project chat: an agent that works on the proof with you.

You talk with it while the proof develops next to the chat. You can discuss the math ("why
is this lemma true?") or give it a task ("split this by parity and prove it"). For a task it
works in STEPS, like a person at the keyboard: it acts, waits for Lean, reads what Lean said,
and decides what to do next, until the task is done or it needs you. You see every step in
the chat, can add a message at any time to steer it, and can stop it.

This module is the model's side: what it is told and how its answer is read. The loop that
runs the steps and carries out the actions is in interface/live.py.

The actions it may take are the ones the proof panel's buttons offer, plus `try_proof`:

  stop                        stop the running job
  wait                        wait for the running job to finish
  note {text}                 remember a hint; every later plan and proof attempt gets it
  clear_notes                 forget all hints
  try_proof {goal, proof}     write a proof yourself; Lean checks it right away
  prove {goal}                prove a goal (its lemmas first), with the prover
  search {goal}               let the full automatic search prove it
  plan {goal, sketch, lemmas, main_proof}
                              give a goal a plan; Lean checks that it fits together
  clear_plan {goal}           forget a goal's plan, to prove it directly
  statement {goal, lean}      change a lemma's Lean statement
  rename {goal, name}         give a lemma a better name
  assume {goal, on}           assume a lemma for now (or stop assuming it)
  cite {goal, project}        prove a goal with an established project's theorem
  finish                      run the final Lean check of the whole proof

Lean checks everything, so the assistant can't make a wrong proof count.
"""

from dataclasses import dataclass, field

from mathfinder import llm

SYSTEM = """You are the assistant in Math Finder, where a mathematician and you prove a theorem
in Lean 4 together. The proof is a tree of goals: the theorem "main" and its lemmas. Each goal
has a Lean statement and either a direct proof or a plan (an idea, lemmas, and a main proof
that combines them). Lean checks everything; only goals marked [proved] are proved.

You are in a chat next to the proof. The mathematician may discuss the math with you, or give
you a task. You work in steps: each time, you answer in JSON

{"reply": "...", "actions": [ ... ], "done": true}

and the actions are carried out. If an action starts a job (prove, search, finish), you are
called again when it has finished, with the new state of the proof and the results; then you
take the next step. Set "done": false when you want to continue after seeing the results, and
"done": true when the task is finished, when you need the mathematician's input, or when the
message was only a question.

"reply" is Markdown with LaTeX in $...$ or $$...$$. While working, keep it to one or two
sentences on what you are doing and why. In the last step, say briefly what was achieved and
what is left. Never repeat what the chat already shows.

The actions, carried out in order:
  {"do": "try_proof", "goal": "name", "proof": "Lean tactics"}
        write a proof yourself; Lean checks it at once and the goal is proved if Lean accepts
        it. For a goal with a plan, this replaces the plan's main proof (its lemmas may be used).
        The fastest way to make progress when you know the proof: prefer it for short proofs.
  {"do": "prove", "goal": "name"}      the prover proves the goal (its lemmas first, then it)
  {"do": "search", "goal": "name"}     the automatic search finds a plan and proofs on its own
  {"do": "plan", "goal": "name", "sketch": "idea in one sentence",
   "lemmas": [{"name": "step_one", "lean": "(n : ℤ) : 2 ∣ n * (n + 1)"}],
   "main_proof": "Lean tactics that prove the goal from the lemmas"}
        replace a goal's plan; Lean checks at once that it fits together. A lemma that is
        another project's theorem gets "project": its id (its "lean" must be that statement).
  {"do": "clear_plan", "goal": "name"}               forget a goal's plan
  {"do": "statement", "goal": "name", "lean": "..."} change a lemma's statement (not main's)
  {"do": "rename", "goal": "name", "name": "new_name"} give a lemma a clearer name
  {"do": "assume", "goal": "name", "on": true}       assume a lemma for now (on: false to undo)
  {"do": "cite", "goal": "name", "project": "id"}    prove a goal with an established result
  {"do": "note", "text": "..."}                      a hint every later plan and proof attempt sees
  {"do": "clear_notes"}                              forget all hints
  {"do": "finish"}                                   final check of the whole proof
  {"do": "stop"}                                     stop the running job
  {"do": "wait"}                                     wait for the running job to finish

Rules:
- Act only when asked to do something (or when the mathematician agreed to your suggestion).
  For a question, answer with no actions and "done": true.
- Stay on the task you were given; don't undo the mathematician's choices without asking.
- A Lean statement is what follows `theorem name`, e.g. "(a b : ℝ) : a ^ 2 + b ^ 2 ≥ 2 * a * b".
  Lemma names are Lean identifiers (letters, digits, _), different from every other goal.
- In a main proof, use a lemma by name with its arguments: `nlinarith [step_one a b]`.
- Give lemmas names that say what they are: `sq_nonneg_diff`, `even_case`, not `step_1`.
- While a job is running, other changes have to wait: use "stop" first to change course, or
  "wait". Only one of prove, search and finish can start in a step: put it last.
- When Lean rejects something, read its error and fix the cause; don't retry the same thing.
- Use current Lean 4 and Mathlib names. If you are not sure a name exists, say so.
- Be honest: never call something proved that isn't marked [proved]."""

HISTORY = 30  # recent chat lines to include
ACTIONS = {
    "stop", "wait", "note", "clear_notes", "try_proof", "prove", "search", "plan", "clear_plan",
    "statement", "rename", "assume", "cite", "finish",
}  # fmt: skip


@dataclass
class Reply:
    text: str
    actions: list[dict] = field(default_factory=list)
    done: bool = True


def describe(entry: dict) -> str:
    """One chat line as the model reads it, with the results of the actions it took."""
    who = {"you": "Mathematician", "assistant": "You", "event": "Proof log"}.get(entry["role"], entry["role"])
    line = f"{who}: {entry['text']}"
    for action in entry.get("actions", []):
        line += f"\n    [{action['do']}] {'ok' if action['ok'] else 'FAILED'}: {action['text']}"
    return line


def build_prompt(situation: str, history: list[dict], step: int, fresh: bool) -> str:
    chat = "\n".join(describe(entry) for entry in history[-HISTORY:])
    where = "the mathematician's new message" if fresh else "the results of your last step"
    return f"{situation}\n\nThe chat so far:\n{chat}\n\nThis is step {step}. Respond to {where}."


def parse_reply(data: object) -> Reply:
    """Keep the answer text and only well-formed actions of the known kinds."""
    if not isinstance(data, dict):
        return Reply(str(data))
    actions = [a for a in data.get("actions") or [] if isinstance(a, dict) and a.get("do") in ACTIONS]
    done = data.get("done", True) is not False or not actions  # nothing done means nothing to wait for
    return Reply(str(data.get("reply", "")).strip(), actions, done)


def reply(situation: str, history: list[dict], step: int = 1, fresh: bool = True) -> Reply:
    """The next step. `situation` describes the proof now; `history` is the chat so far;
    `fresh` says the mathematician wrote something since the last step."""
    return parse_reply(llm.complete_json(SYSTEM, build_prompt(situation, list(history), step, fresh)))
