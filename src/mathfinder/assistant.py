"""The project chat: you talk with the assistant while the proof develops next to it.

You can discuss the math ("why is this lemma true?"), tell it what to do ("stop, try
induction on n instead"), or ask it to change the plan. It answers in Markdown with LaTeX
and, when you asked for it, also ACTS on the proof. The actions it may take are the same ones
the buttons in the proof panel offer, nothing else:

  stop                        stop the running job
  note {text}                 remember a hint; every later plan and proof attempt gets it
  clear_notes                 forget all hints
  prove {goal}                prove a goal (its lemmas first)
  search {goal}               let the full automatic search prove it
  plan {goal, sketch, lemmas, main_proof}
                              give a goal a plan; Lean checks that it fits together
  clear_plan {goal}           forget a goal's plan, to prove it directly
  statement {goal, lean}      change a lemma's Lean statement
  assume {goal, on}           assume a lemma for now (or stop assuming it)
  cite {goal, project}        prove a goal with an established project's theorem
  finish                      run the final Lean check of the whole proof

The server carries the actions out (interface/server.py, Live.act) and the chat shows what
each one did. Lean still checks everything, so the assistant can't make a wrong proof count.
"""

from dataclasses import dataclass, field

from mathfinder import llm

SYSTEM = """You are the assistant in Math Finder, where a mathematician and you prove a theorem
in Lean 4 together. The proof is a tree of goals: the theorem "main" and its lemmas. Each goal
has a Lean statement and either a direct proof or a plan (an idea, lemmas, and a main proof
that combines them). Lean checks everything; only goals marked [proved] are proved.

You are in a chat next to the proof. The mathematician may discuss the math with you, or tell
you to do something. Reply in JSON:

{"reply": "...", "actions": [ ... ]}

"reply" is Markdown with LaTeX in $...$ or $$...$$, a few sentences or a short list.

"actions" changes the proof. Take actions ONLY when the mathematician asks for something to
be done (or clearly agrees to your suggestion); for a question, answer and leave it empty.
The actions, carried out in order:
  {"do": "stop"}                                     stop the running job
  {"do": "note", "text": "..."}                      a hint every later plan and proof attempt sees
  {"do": "clear_notes"}                              forget all hints
  {"do": "prove", "goal": "main"}                    prove a goal (its lemmas first, then it)
  {"do": "search", "goal": "main"}                   let the automatic search prove a goal on its own
  {"do": "plan", "goal": "main", "sketch": "idea in one sentence",
   "lemmas": [{"name": "step_one", "lean": "(n : ℤ) : 2 ∣ n * (n + 1)"}],
   "main_proof": "Lean tactics that prove the goal from the lemmas"}
                                                     replace a goal's plan (Lean checks it fits)
  {"do": "clear_plan", "goal": "..."}                forget a goal's plan
  {"do": "statement", "goal": "...", "lean": "..."}  change a lemma's statement (not main's)
  {"do": "assume", "goal": "...", "on": true}        assume a lemma for now (on: false to undo)
  {"do": "cite", "goal": "...", "project": "id"}     prove a goal with an established result
  {"do": "finish"}                                   final check of the whole proof

Rules:
- A Lean statement is what follows `theorem name`, e.g. "(a b : ℝ) : a ^ 2 + b ^ 2 ≥ 2 * a * b".
  Lemma names are Lean identifiers (letters, digits, _), different from every other goal.
- In a main proof, use a lemma by name with its arguments: `nlinarith [step_one a b]`.
- While a job is running, every action except stop and note waits for it; to change course,
  put "stop" first. Only one of prove, search and finish can start at a time: put it last.
- When asked to "try X" (a different approach), usually: stop if running, a note or a new
  plan, then prove.
- Use current Lean 4 and Mathlib names. If you are not sure a name exists, say so.
- A job you start reports its progress in the chat by itself ("step_one: proved"); you only
  speak when the mathematician writes, so don't promise to report back.
- Be honest: never call something proved that isn't marked [proved]."""

HISTORY = 14  # recent chat lines to include, so follow-ups make sense
ACTIONS = {
    "stop",
    "note",
    "clear_notes",
    "prove",
    "search",
    "plan",
    "clear_plan",
    "statement",
    "assume",
    "cite",
    "finish",
}


@dataclass
class Reply:
    text: str
    actions: list[dict] = field(default_factory=list)


def build_prompt(message: str, situation: str, history: list[dict]) -> str:
    parts = [situation]
    lines = []
    for entry in history[-HISTORY:]:
        who = {"you": "Mathematician", "assistant": "You", "event": "Proof log"}.get(entry["role"], entry["role"])
        lines.append(f"{who}: {entry['text']}")
    if lines:
        parts.append("The chat so far:\n" + "\n".join(lines))
    parts.append(f"Mathematician: {message}")
    return "\n\n".join(parts)


def parse_reply(data: object) -> Reply:
    """Keep the answer text and only well-formed actions of the known kinds."""
    if not isinstance(data, dict):
        return Reply(str(data))
    actions = [a for a in data.get("actions") or [] if isinstance(a, dict) and a.get("do") in ACTIONS]
    return Reply(str(data.get("reply", "")).strip(), actions)


def reply(message: str, situation: str, history: list[dict] = ()) -> Reply:
    """Answer `message`. `situation` describes the proof, what is running, notes and the library."""
    return parse_reply(llm.complete_json(SYSTEM, build_prompt(message, situation, list(history))))
