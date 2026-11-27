"""The side assistant: answers questions while you work, without touching the proof.

Like asking a colleague "by the way, why does this lemma hold?" while the search keeps
running. It sees the whole proof tree (statements, plans, what Lean said) and the last few
questions and answers, and replies in Markdown with LaTeX. It cannot change anything; when
it helps, it suggests a lemma or a hint that you can then add yourself.
"""

from mathfinder import llm

SYSTEM = """You are the side assistant in Math Finder, where a mathematician and an AI prove a
theorem in Lean 4 together. The mathematician asks you a side question while they work.

- Answer the question directly and briefly: a few sentences, or a short list.
- Write Markdown, with LaTeX math in $...$ or $$...$$.
- You cannot change the proof. When it would help, suggest something they can add themselves:
  a lemma (its Lean statement after `theorem name`, and the same in LaTeX) or a hint for the planner.
- Use current Lean 4 and Mathlib names. If you are not sure a name exists, say so.
- Be honest about what is proved: only goals marked [proved] are checked by Lean."""

HISTORY = 6  # earlier questions and answers to include, so follow-up questions make sense


def ask(question: str, proof_state: str, history: list[tuple[str, str]] = ()) -> str:
    """Answer `question` about the proof described in `proof_state`."""
    parts = [f"The proof so far (goals, their status, plans and Lean errors):\n{proof_state}"]
    for earlier_question, earlier_answer in list(history)[-HISTORY:]:
        parts.append(f"Earlier question: {earlier_question}\nYour answer: {earlier_answer}")
    parts.append(f"Question: {question}")
    return llm.complete(SYSTEM, "\n\n".join(parts))
