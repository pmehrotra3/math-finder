"""Tests for mathfinder/translate.py."""

import pytest

from mathfinder import translate as tr
from mathfinder.planner import Lemma, Plan

# ---------- fast tests ----------


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("(n : ℕ) : n + 0 = n", "(n : ℕ) : n + 0 = n"),
        ("theorem foo (n : ℕ) : n + 0 = n := by sorry", "(n : ℕ) : n + 0 = n"),
        ("lemma bar : 1 + 1 = 2", ": 1 + 1 = 2"),
        ("example (x : ℝ) : x = x := rfl", "(x : ℝ) : x = x"),
        ("theorem (a : ℤ) : a = a", "(a : ℤ) : a = a"),
    ],
)
def test_clean_statement(answer, expected):
    assert tr.clean_statement(answer) == expected


# ---------- Lean test, scripted model ----------


@pytest.mark.lean
def test_formalize_repairs_a_broken_statement_then_judges_it(fake_llm):
    statements = ["(n : ℕ) : n = undefined_q", "(n : ℕ) : ∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2"]
    seen = []

    def scripted(system, prompt):
        seen.append(system)
        if system == tr.SYSTEM_FORMALIZE:
            return {"statement": statements.pop(0)}
        if system == tr.SYSTEM_BACK:
            assert "sum of the first" not in prompt  # the back-translator never sees the original
            return {"latex": r"For all $n$, $\sum_{i<n} (2i+1) = n^2$."}
        return {"same_meaning": True, "reason": "identical"}

    fake_llm(scripted)
    result = tr.formalize(r"The sum of the first $n$ odd numbers is $n^2$.")
    assert result.ok and result.faithful
    assert result.statement.endswith("= n ^ 2")
    assert seen == [tr.SYSTEM_FORMALIZE, tr.SYSTEM_FORMALIZE, tr.SYSTEM_BACK, tr.SYSTEM_JUDGE]


# ---------- real model ----------


@pytest.mark.llm
def test_real_formalization_of_a_simple_theorem():
    result = tr.formalize(r"For every integer $n$, $6 \mid n^3 - n$.")
    assert result.ok, result.errors
    assert "ℤ" in result.statement or "Int" in result.statement
    assert result.faithful, result.judgment


@pytest.mark.llm
def test_real_judge_catches_natural_number_truncation():
    same, reason = tr.judge(
        r"For every integer $n$, $(n - 3)^2 \ge 0$.",
        r"For every natural number $n \in \mathbb{N}$, $(n - 3)^2 \ge 0$, where subtraction is truncated at 0.",
    )
    assert not same, reason


# ---------- explain ----------

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
    prompt = tr.explain_prompt(THEOREM, PLAN, CODE)
    assert "```lean\nnamespace MathFinder" in prompt
    assert "- `frac_sq_nonneg`: (a-b)^2/(ab) is nonnegative" in prompt


def test_missing_steps_finds_unmentioned_lemmas():
    text = "**Step 1** (`diff_identity`) ... **Conclusion** (`main`) ..."
    assert tr.missing_steps(text, PLAN) == ["frac_sq_nonneg"]


def test_explain_asks_again_when_a_step_is_missing(fake_llm):
    full = "(`diff_identity`) (`frac_sq_nonneg`) (`main`)"
    prompts = fake_llm("Step 1 (`diff_identity`) only.", full)
    assert tr.explain(THEOREM, PLAN, CODE) == full
    assert "no step for: frac_sq_nonneg, main" in prompts[1]


@pytest.mark.llm
def test_real_explanation_has_a_step_per_lemma():
    markdown = tr.explain(THEOREM, PLAN, CODE)
    assert tr.missing_steps(markdown, PLAN) == []
    assert "## Proof" in markdown
