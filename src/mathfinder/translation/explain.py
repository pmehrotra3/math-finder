"""Proof → words: turn a verified Lean proof back into mathematics a person can read.

One step per lemma, each tagged with its Lean name, so you can always see which part of
the formal proof a sentence is talking about.
"""

from mathfinder import llm
from mathfinder.proving.planner import Plan

SYSTEM = """You explain formal Lean 4 proofs to people, in plain mathematics.
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


def build_prompt(theorem: str, plan: Plan, lean_code: str) -> str:
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
    prompt = build_prompt(theorem, plan, lean_code)
    markdown = llm.complete(SYSTEM, prompt)
    missing = missing_steps(markdown, plan)
    if missing:
        markdown = llm.complete(
            SYSTEM,
            f"{prompt}\n\nYour previous explanation had no step for: {', '.join(missing)}. "
            "Write it again with one step for every lemma, tagged with its name in backticks.",
        )
    return markdown
