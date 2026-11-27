"""LaTeX → Lean: turn a theorem into a Lean statement you can trust.

This is the most dangerous step in the whole system: Lean will happily prove a statement
that doesn't say what you meant. (On ℕ, 2 - 5 = 0. Forget "n ≥ 1" and the claim changes.)
So we use three separate checks before any search starts:

  1. Lean must accept the statement (it type-checks, with `sorry` as the proof)
  2. a SEPARATE model call translates the Lean back into LaTeX without seeing your
     original, so it can't paper over a mistake
  3. a judge compares your LaTeX with that back-translation

And then YOU approve it. Nothing is proved until a person says "yes, that's my theorem".
"""

from dataclasses import dataclass, field

from mathfinder import llm
from mathfinder.lean.checker import statement_errors

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
- Also give the theorem a short name a mathematician would use for it, in plain words, at most
  five words: "Sum of odd numbers", "AM-GM for two numbers", "6 divides n³ − n".
Reply with ONLY a JSON object inside a ```json block: {"statement": "...", "name": "...", "notes": "..."}"""

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
    name: str = ""  # a short name for the theorem, suggested as the project's name

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
        result.name = str(data.get("name", "")).strip()[:80]
        result.errors = statement_errors(result.statement) if result.statement else ["empty statement"]
        if not result.errors:
            break
        prompt += (
            f"\n\nYour statement:\n  {result.statement}\nfailed in Lean with:\n"
            f"{result.errors[0][:600]}\nFix it and reply again."
        )
    if result.errors:
        return result

    result.back_translation = to_latex("main", result.statement)
    result.faithful, result.judgment = judge(latex, result.back_translation)
    return result


SYSTEM_NAME = """You name theorems. Give the short name a mathematician would use for this one, in plain
words, at most five words: "Sum of odd numbers", "AM-GM for two numbers", "6 divides n³ − n".
Reply with ONLY a JSON object inside a ```json block: {"name": "..."}"""


def suggest_name(theorem: str) -> str:
    """A short name for a theorem (LaTeX or Lean), for a project that doesn't have one."""
    answer = llm.complete_json(SYSTEM_NAME, theorem)
    return str(answer.get("name", "") if isinstance(answer, dict) else "").strip()[:80]


def to_latex(name: str, statement: str) -> str:
    """Lean → formal LaTeX, literally (the model never sees what the author meant)."""
    answer = llm.complete_json(SYSTEM_BACK, f"theorem {name} {statement}")
    return str(answer.get("latex", "")) if isinstance(answer, dict) else ""


def judge(original: str, back_translation: str) -> tuple[bool, str]:
    """Do these two say the same thing? Returns (same_meaning, reason)."""
    verdict = llm.complete_json(
        SYSTEM_JUDGE,
        f"Original:\n{original}\n\nLiteral translation of the formal statement:\n{back_translation}",
    )
    return verdict.get("same_meaning") is True, str(verdict.get("reason", ""))
