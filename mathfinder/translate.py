"""Translate between mathematics for people and Lean, in both directions.

`formalize` turns a LaTeX theorem into a Lean statement. This is the most dangerous step
in the whole system: Lean will happily prove a statement that doesn't say what you meant.
(On ℕ, 2 - 5 = 0. Forget "n ≥ 1" and the claim changes.) So we use three separate checks
before any search starts:

  1. Lean must accept the statement (it type-checks, with `sorry` as the proof)
  2. a SEPARATE model call translates the Lean back into LaTeX without seeing your
     original, so it can't paper over a mistake
  3. a judge compares your LaTeX with that back-translation

And then YOU approve it. Nothing is proved until a person says "yes, that's my theorem".

`explain` turns a verified Lean proof back into readable math: one step per lemma, each
tagged with its Lean name.
"""

from dataclasses import dataclass, field

from mathfinder import llm
from mathfinder.lean import statement_errors
from mathfinder.planner import Plan

REPAIR_ROUNDS = 2

SYSTEM_FORMALIZE = """You translate theorems written in LaTeX into Lean 4 + Mathlib statements.
Rules:
- Write only what comes after `theorem name`: binders and hypotheses, a colon, then the claim.
  Example: "(a b : ℝ) (ha : 0 < a) : a * b ≤ a * b + a". If there are no variables, start with ": ".
- Choose the number system the LaTeX means (ℕ, ℤ, ℚ, ℝ, ℂ). Be careful: subtraction and division on ℕ
  truncate, so prefer ℤ or ℝ when the original involves negatives or fractions.
- Make every assumption explicit as a hypothesis. Do not add assumptions the original doesn't have,
  and do not drop any.
- Lean 4 syntax and current Mathlib names only. Sums: ∑ i ∈ Finset.range n, f i.
Reply with ONLY a JSON object inside a ```json block: {"statement": "...", "notes": "..."}"""

SYSTEM_BACK = """You translate Lean 4 + Mathlib statements into LaTeX, LITERALLY.
Say exactly what the Lean says, including the number types (ℕ, ℤ, ℝ) and every hypothesis.
Do not guess what the author intended and do not fix anything.
Reply with ONLY a JSON object inside a ```json block: {"latex": "..."}"""

SYSTEM_JUDGE = """You check whether a formal statement says the same thing as the original theorem.
You get: the original LaTeX, and a literal LaTeX translation of the formal Lean statement.
Watch for: wrong number type (ℕ vs ℤ vs ℝ), truncated subtraction or division on ℕ, missing or
extra hypotheses, wrong quantifiers, off-by-one ranges in sums, strict vs non-strict inequalities.
Reply with ONLY a JSON object inside a ```json block: {"same_meaning": true or false, "reason": "..."}"""


@dataclass
class Formalization:
    latex: str
    statement: str = ""  # the Lean statement (what comes after `theorem name`)
    back_translation: str = ""  # literal LaTeX of the Lean statement
    faithful: bool = False  # the judge's opinion
    judgment: str = ""  # the judge's reason
    errors: list[str] = field(default_factory=list)  # Lean errors, if it never type-checked

    @property
    def ok(self) -> bool:
        return bool(self.statement) and not self.errors


def clean_statement(text: str) -> str:
    """Accept answers like 'theorem foo (n : ℕ) : P n := by sorry' and keep just '(n : ℕ) : P n'."""
    text = text.strip()
    for keyword in ("theorem ", "lemma ", "example "):
        if text.startswith(keyword):
            rest = text[len(keyword) :].lstrip()
            if keyword != "example " and not rest.startswith(("(", "{", "[", ":")):
                rest = rest.split(None, 1)[1] if " " in rest else ""  # drop the name
            text = rest
            break
    return text.split(":=", 1)[0].strip()


def formalize(latex: str) -> Formalization:
    """LaTeX in; a checked Lean statement with a back-translation and a judgment out."""
    result = Formalization(latex)
    prompt = f"Theorem in LaTeX:\n{latex}"
    for _ in range(1 + REPAIR_ROUNDS):
        data = llm.complete_json(SYSTEM_FORMALIZE, prompt)
        result.statement = clean_statement(str(data.get("statement", "")))
        result.errors = statement_errors(result.statement) if result.statement else ["empty statement"]
        if not result.errors:
            break
        prompt += (
            f"\n\nYour statement:\n  {result.statement}\nfailed in Lean with:\n"
            f"{result.errors[0][:600]}\nFix it and reply again."
        )
    if result.errors:
        return result

    back = llm.complete_json(SYSTEM_BACK, f"theorem main {result.statement}")
    result.back_translation = str(back.get("latex", ""))
    result.faithful, result.judgment = judge(latex, result.back_translation)
    return result


def judge(original: str, back_translation: str) -> tuple[bool, str]:
    """Do these two say the same thing? Returns (same_meaning, reason)."""
    verdict = llm.complete_json(
        SYSTEM_JUDGE,
        f"Original:\n{original}\n\nLiteral translation of the formal statement:\n{back_translation}",
    )
    return verdict.get("same_meaning") is True, str(verdict.get("reason", ""))


SYSTEM_EXPLAIN = """You explain formal Lean 4 proofs to people, in plain mathematics.
Write Markdown with LaTeX math ($...$ inline, $$...$$ for displays). Use exactly this structure:

## Theorem
The statement, in LaTeX.

## Idea
One to three sentences: the key insight.

## Proof
One numbered step per lemma, in order, each starting with **Step k** (`lemma_name`),
then a final step starting with **Conclusion** (`main`) that combines them.
In each step say WHAT is shown and WHY it is true, the way a careful textbook would.
Mention a Lean tactic only if it helps the reader (e.g. "by induction on n").

Rules: explain only what the Lean proof actually does. Do not invent steps it doesn't take.
Reply with the Markdown only."""


def explain_prompt(theorem: str, plan: Plan, lean_code: str) -> str:
    lemma_lines = "\n".join(f"- `{lem.name}`: {lem.informal or lem.statement}" for lem in plan.lemmas) or "- (none)"
    return (
        f"Theorem (as the user wrote it):\n{theorem}\n\n"
        f"Proof idea from the planner: {plan.sketch}\n\n"
        f"Lemmas, in order:\n{lemma_lines}\n\n"
        f"The complete Lean proof, verified by Lean:\n```lean\n{lean_code}\n```"
    )


def missing_steps(markdown: str, plan: Plan) -> list[str]:
    """Lean names that the explanation never mentions as `name`."""
    names = [lem.name for lem in plan.lemmas] + ["main"]
    return [n for n in names if f"`{n}`" not in markdown]


def explain(theorem: str, plan: Plan, lean_code: str) -> str:
    """A Markdown explanation of a verified proof. Asks once more if it skipped a lemma."""
    prompt = explain_prompt(theorem, plan, lean_code)
    markdown = llm.complete(SYSTEM_EXPLAIN, prompt)
    missing = missing_steps(markdown, plan)
    if missing:
        markdown = llm.complete(
            SYSTEM_EXPLAIN,
            f"{prompt}\n\nYour previous explanation had no step for: {', '.join(missing)}. "
            "Write it again with one step for every lemma, tagged with its name in backticks.",
        )
    return markdown
