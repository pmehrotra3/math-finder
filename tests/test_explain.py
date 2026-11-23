"""Tests for mathfinder/explain.py."""

import pytest

from mathfinder import explain as ex
from mathfinder import llm
from mathfinder.planner import Lemma, Plan

PLAN = Plan(
    "Write a/b + b/a - 2 as (a-b)^2/(ab), which is nonnegative.",
    [
        Lemma(
            "diff_identity",
            "(a b : ℝ) (ha : 0 < a) (hb : 0 < b) : a / b + b / a - 2 = (a - b) ^ 2 / (a * b)",
            "a/b + b/a - 2 = (a-b)^2/(ab)",
            status="proved",
            proof="field_simp\nring",
        ),
        Lemma(
            "frac_sq_nonneg",
            "(a b : ℝ) (ha : 0 < a) (hb : 0 < b) : 0 ≤ (a - b) ^ 2 / (a * b)",
            "(a-b)^2/(ab) is nonnegative",
            status="proved",
            proof="positivity",
        ),
    ],
    "have h1 := diff_identity a b ha hb\nhave h2 := frac_sq_nonneg a b ha hb\nlinarith",
)
CODE = "namespace MathFinder\n\ntheorem diff_identity ...\n\nend MathFinder\n"
THEOREM = r"For all positive reals $a, b$: $\frac{a}{b} + \frac{b}{a} \ge 2$."


def test_prompt_contains_the_lean_proof_and_lemma_meanings():
    prompt = ex.build_prompt(THEOREM, PLAN, CODE)
    assert "```lean\nnamespace MathFinder" in prompt
    assert "- `frac_sq_nonneg`: (a-b)^2/(ab) is nonnegative" in prompt


def test_missing_steps_finds_unmentioned_lemmas():
    text = "**Step 1** (`diff_identity`) ... **Conclusion** (`main`) ..."
    assert ex.missing_steps(text, PLAN) == ["frac_sq_nonneg"]


def test_explain_asks_again_when_a_step_is_missing(monkeypatch):
    answers = ["Step 1 (`diff_identity`) only.", "(`diff_identity`) (`frac_sq_nonneg`) (`main`)"]
    prompts = []

    def fake(system, prompt):
        prompts.append(prompt)
        return answers.pop(0)

    monkeypatch.setitem(llm.BACKENDS, "fake", fake)
    monkeypatch.setenv("MATHFINDER_LLM", "fake")
    assert ex.explain(THEOREM, PLAN, CODE) == "(`diff_identity`) (`frac_sq_nonneg`) (`main`)"
    assert "no step for: frac_sq_nonneg, main" in prompts[1]


@pytest.mark.llm
def test_real_explanation_has_a_step_per_lemma():
    markdown = ex.explain(THEOREM, PLAN, CODE)
    assert ex.missing_steps(markdown, PLAN) == []
    assert "## Proof" in markdown
