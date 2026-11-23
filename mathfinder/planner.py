"""The planner (the "policy"): ask Claude for proof PLANS, not whole proofs.

A plan is a short informal sketch, a list of small lemmas, and a main proof that
puts the lemmas together. Small lemmas are much easier to prove one at a time
(often the hammer does it with no AI at all), and when one fails we know exactly
which step of the idea is broken.
"""

import re
from dataclasses import dataclass, field

from mathfinder import llm

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")  # a valid simple Lean name


@dataclass
class Lemma:
    name: str  # Lean name, e.g. "sq_sub_nonneg"
    statement: str  # Lean, without the name: "(a b : ℝ) : 0 ≤ (a - b) ^ 2"
    informal: str  # one sentence of math, for people
    status: str = "open"  # open | proved | refuted | stuck
    proof: str = ""  # tactic proof, once found
    feedback: str = ""  # why it failed (Lean error, counterexample, ...)


@dataclass
class Plan:
    sketch: str  # the idea in 1-3 sentences
    lemmas: list[Lemma]
    main_proof: str  # tactic proof of the theorem; may use the lemmas by name
    feedback: list[str] = field(default_factory=list)  # what the critic said


SYSTEM = """You are an expert in Lean 4 and Mathlib, planning formal proofs.
You never write a whole long proof at once. You split the theorem into a few small lemmas
that are each easy to prove on their own, then prove the theorem from those lemmas.

Rules:
- Lean 4 syntax and current Mathlib names only (no Lean 3).
- A lemma "statement" is everything that would come after `theorem name`:
  binders and hypotheses, then a colon, then the claim. Example: "(a b : ℝ) : 0 ≤ (a - b) ^ 2".
- Every lemma must be TRUE as stated, and as small as possible (ideally one tactic).
- A lemma may use earlier lemmas in the list.
- "main_proof" is a tactic proof of the theorem (no `by`), and uses the lemmas by name.
  Example: "nlinarith [sq_sub_nonneg a b]".
- Lemma names: snake_case, letters/digits/underscores only.
- If the theorem is easy, a plan may have zero lemmas.
- Never use sorry, admit or axiom.
- Make the plans genuinely different from each other (different ideas, not reworded copies).
- If the user gave hints, follow them: they come from a person working with you.

Reply with ONLY a JSON object inside a ```json block:
{"plans": [{"sketch": "...", "lemmas": [{"name": "...", "statement": "...", "informal": "..."}],
            "main_proof": "..."}]}"""


def build_prompt(theorem: str, n: int, hints: list[str], feedback: str) -> str:
    parts = [f"Theorem to prove (Lean statement):\n  theorem main {theorem}", f"Give {n} different plans."]
    if hints:
        parts.append("Hints from the user (highest priority, follow them):\n" + "\n".join(f"- {h}" for h in hints))
    if feedback:
        parts.append(f"What happened to earlier plans (avoid these mistakes):\n{feedback}")
    return "\n\n".join(parts)


def parse_plans(data) -> list[Plan]:
    """Turn the model's JSON into Plan objects, dropping any plan that is malformed."""
    plans = []
    for raw in data.get("plans", []) if isinstance(data, dict) else []:
        try:
            lemmas = [
                Lemma(name=l["name"].strip(), statement=l["statement"].strip(), informal=l.get("informal", ""))
                for l in raw.get("lemmas", [])
            ]
            plan = Plan(sketch=raw.get("sketch", ""), lemmas=lemmas, main_proof=raw["main_proof"].strip())
        except (KeyError, AttributeError, TypeError):
            continue  # missing field or wrong type: skip this plan
        names = [l.name for l in lemmas]
        if all(NAME_RE.match(n) for n in names) and len(set(names)) == len(names) and plan.main_proof:
            plans.append(plan)
    return plans


def propose_plans(theorem: str, n: int = 2, hints: list[str] = (), feedback: str = "") -> list[Plan]:
    """Ask Claude for `n` plans to prove `theorem` (a Lean statement without a name)."""
    data = llm.complete_json(SYSTEM, build_prompt(theorem, n, list(hints), feedback))
    return parse_plans(data)
