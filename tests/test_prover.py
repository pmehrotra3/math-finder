"""Tests for mathfinder/prover.py.

The Lean tests use a FAKE model with scripted answers, so they're free and
repeatable while still checking every proof with real Lean.
"""

import pytest

from mathfinder import prover
from mathfinder.planner import Lemma
from mathfinder.prover import counterexample_text, hammer, suggestion

ODD_SUM = "(n : ℕ) : ∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2"  # needs induction: the hammer can't
GOOD = "induction n with\n| zero => simp\n| succ k ih =>\n  rw [Finset.sum_range_succ, ih]\n  ring"
WRONG = "simp"


# ---------- fast tests ----------


def test_parse_proofs_cleans_model_output():
    data = {"proofs": ["by simp", "  ring  ", "sorry", 42, "", "exact foo"]}
    assert prover.parse_proofs(data) == ["simp", "ring", "exact foo"]


def test_cache_key_ignores_whitespace_but_not_context():
    a = Lemma("x", "(n : ℕ) :  n = n", "")
    b = Lemma("y", "(n : ℕ) : n = n", "")
    helper = Lemma("h", ": True", "")
    assert prover.cache_key(a, []) == prover.cache_key(b, [])
    assert prover.cache_key(a, []) != prover.cache_key(a, [helper])


def test_repair_prompt_shows_lean_errors():
    lemma = Lemma("odd_sum", ODD_SUM, "sum of first n odd numbers")
    prompt = prover.build_prompt(lemma, [], [("simp", "simp made no progress")])
    assert "FAILED" in prompt and "simp made no progress" in prompt


def test_preamble_uses_real_proofs_when_known():
    done = Lemma("a", ": 1 = 1", "", status="proved", proof="rfl")
    open_ = Lemma("b", ": 2 = 2", "")
    assert prover.preamble_for([done, open_]) == (
        "namespace MathFinder\n\ntheorem a : 1 = 1 := by\n  rfl\n\ntheorem b : 2 = 2 := by\n  sorry"
    )
    assert prover.preamble_for([]) == ""


# ---------- Lean tests (fake model, real Lean) ----------


@pytest.mark.lean
def test_false_lemma_is_refuted_without_asking_the_model(fake_llm):
    prompts = fake_llm()  # no answers: any model call fails the test
    lemma = prover.prove_lemma(Lemma("bad", "(n : ℕ) : n ^ 2 ≤ 2 * n + 3", ""))
    assert lemma.status == "refuted"
    assert "counterexample" in lemma.feedback
    assert prompts == []


@pytest.mark.lean
def test_repair_round_fixes_a_wrong_proof_then_cache_reuses_it(fake_llm):
    prompts = fake_llm({"proofs": [WRONG]}, {"proofs": [GOOD]})
    attempts = []
    lemma = prover.prove_lemma(
        Lemma("odd_sum", ODD_SUM, "sum of the first n odd numbers is n^2"), on_attempt=attempts.append
    )
    assert lemma.status == "proved", lemma.feedback
    assert [(a["proof"], a["ok"], a["source"]) for a in attempts] == [(WRONG, False, "llm"), (GOOD, True, "repair")]
    assert attempts[0]["error"]  # the failure is logged WITH Lean's error
    assert lemma.proof == GOOD
    assert lemma.feedback == "LLM after 1 repair round(s)"
    assert "Lean said" in prompts[1]  # the second call saw the real Lean error

    again = prover.prove_lemma(Lemma("odd_sum_again", ODD_SUM, ""))
    assert again.status == "proved" and again.feedback == "from cache"
    assert len(prompts) == 2  # no new model call


# ---------- real Claude + Lean ----------


@pytest.mark.llm
def test_real_model_proves_a_lemma_the_hammer_cannot():
    lemma = prover.prove_lemma(Lemma("odd_sum", ODD_SUM, "the sum of the first n odd numbers is n^2"))
    assert lemma.status == "proved", lemma.feedback
    assert lemma.feedback.startswith("LLM")


@pytest.mark.lean
def test_every_attempt_in_a_batch_is_reported_even_after_a_success(fake_llm):
    fake_llm({"proofs": [GOOD, WRONG]})  # one batch: the working proof FIRST, then a failing one
    attempts = []
    prover.prove_lemma(Lemma("odd_sum", ODD_SUM, ""), on_attempt=attempts.append)
    assert [(a["proof"], a["ok"]) for a in attempts] == [(GOOD, True), (WRONG, False)]


# ---------- hammer ----------

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
def test_exact_search_keeps_the_lemma_it_found():
    result = hammer("(a b : ℕ) (h : a ∣ b) (hb : 0 < b) : a ≤ b", tactics=["exact?"])
    assert result.proved
    assert result.proof.startswith("exact ")
    assert "?" not in result.proof
