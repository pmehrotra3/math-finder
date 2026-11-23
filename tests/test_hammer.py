"""Tests for mathfinder/hammer.py. The `lean` ones take ~30-60 s each."""

import pytest

from mathfinder.hammer import counterexample_text, hammer, suggestion

# ---------- fast tests: parse real Lean output, no Lean needed ----------

PLAUSIBLE_ERROR = (
    "\n===================\nFound a counter-example!\nn := 4\n"
    "issue: 16 ≤ 11 does not hold\n(0 shrinks)\n-------------------"
)


def test_counterexample_text_is_one_line():
    assert counterexample_text(PLAUSIBLE_ERROR) == "n := 4; issue: 16 ≤ 11 does not hold"


def test_suggestion_strips_try_this_and_apply():
    infos = ["something else", "Try this:\n  [apply] exact Nat.add_comm a b"]
    assert suggestion(infos) == "exact Nat.add_comm a b"


def test_suggestion_empty_when_missing():
    assert suggestion(["no suggestion here"]) == ""


# ---------- slow tests: these run Lean ----------


@pytest.mark.lean
def test_hammer_proves_nonlinear_inequality():
    result = hammer("(n : ℕ) (h : n > 3) : 2 * n < n * n")
    assert result.proved
    assert result.counterexample == ""


@pytest.mark.lean
def test_hammer_finds_counterexample_for_false_statement():
    result = hammer("(n : ℕ) : n ^ 2 ≤ 2 * n + 3")
    assert not result.proved
    assert "n :=" in result.counterexample


@pytest.mark.lean
def test_hammer_reports_untestable_types():
    # x^2 ≥ x is false for x = 1/2, but plausible can't sample real numbers.
    result = hammer("(x : ℝ) : x ^ 2 ≥ x")
    assert not result.proved
    assert not result.testable


@pytest.mark.lean
def test_exact_search_keeps_the_lemma_it_found():
    result = hammer("(a b : ℕ) (h : a ∣ b) (hb : 0 < b) : a ≤ b", tactics=["exact?"])
    assert result.proved
    assert result.proof.startswith("exact ")
    assert "?" not in result.proof
