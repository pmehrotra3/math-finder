"""Tests for mathfinder/formalize.py."""

import json

import pytest

from mathfinder import formalize as fm
from mathfinder import llm

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
    assert fm.clean_statement(answer) == expected


# ---------- Lean test, scripted model ----------


@pytest.mark.lean
def test_formalize_repairs_a_broken_statement_then_judges_it(monkeypatch):
    formal_answers = ["(n : ℕ) : n = undefined_q", "(n : ℕ) : ∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2"]
    seen = []

    def scripted(system, prompt):
        seen.append(system)
        if system == fm.SYSTEM_FORMALIZE:
            return "```json\n" + json.dumps({"statement": formal_answers.pop(0)}) + "\n```"
        if system == fm.SYSTEM_BACK:
            assert "sum of the first" not in prompt  # the back-translator never sees the original
            return '```json\n{"latex": "For all $n \\\\in \\\\mathbb{N}$, $\\\\sum_{i<n} (2i+1) = n^2$."}\n```'
        return '```json\n{"same_meaning": true, "reason": "identical"}\n```'

    monkeypatch.setitem(llm.BACKENDS, "fake", scripted)
    monkeypatch.setenv("MATHFINDER_LLM", "fake")
    result = fm.formalize(r"The sum of the first $n$ odd numbers is $n^2$.")

    assert result.ok and result.faithful
    assert result.statement.endswith("= n ^ 2")
    assert seen == [fm.SYSTEM_FORMALIZE, fm.SYSTEM_FORMALIZE, fm.SYSTEM_BACK, fm.SYSTEM_JUDGE]


# ---------- real model ----------


@pytest.mark.llm
def test_real_formalization_of_a_simple_theorem():
    result = fm.formalize(r"For every integer $n$, $6 \mid n^3 - n$.")
    assert result.ok, result.errors
    assert "ℤ" in result.statement or "Int" in result.statement
    assert result.faithful, result.judgment


@pytest.mark.llm
def test_real_judge_catches_natural_number_truncation():
    same, reason = fm.judge(
        r"For every integer $n$, $(n - 3)^2 \ge 0$.",
        r"For every natural number $n \in \mathbb{N}$, $(n - 3)^2 \ge 0$, where subtraction is truncated at 0.",
    )
    assert not same, reason
