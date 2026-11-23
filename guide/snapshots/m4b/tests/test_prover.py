"""Tests for mathfinder/prover.py.

The Lean tests use a FAKE model with scripted answers, so they're free and
repeatable while still checking every proof with real Lean.
"""

import json

import pytest

from mathfinder import llm, prover
from mathfinder.planner import Lemma

ODD_SUM = "(n : ℕ) : ∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2"  # needs induction: the hammer can't
GOOD = "induction n with\n| zero => simp\n| succ k ih =>\n  rw [Finset.sum_range_succ, ih]\n  ring"
WRONG = "simp"


@pytest.fixture(autouse=True)
def temp_cache(tmp_path, monkeypatch):
    """Every test gets its own empty cache file, so tests can't affect each other."""
    monkeypatch.setattr(prover, "CACHE_FILE", tmp_path / "lemmas.json")


@pytest.fixture
def scripted_model(monkeypatch):
    """A fake model that gives the scripted proof lists in order and records each prompt."""

    def install(*answers):
        prompts = []

        def fake(system, prompt):
            prompts.append(prompt)
            assert len(prompts) <= len(answers), "the model was called more times than expected"
            proofs = answers[len(prompts) - 1]
            return "```json\n" + json.dumps({"proofs": proofs}) + "\n```"

        monkeypatch.setitem(llm.BACKENDS, "fake", fake)
        monkeypatch.setenv("MATHFINDER_LLM", "fake")
        return prompts

    return install


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
        "theorem a : 1 = 1 := by\n  rfl\n\ntheorem b : 2 = 2 := by\n  sorry"
    )


# ---------- Lean tests (fake model, real Lean) ----------


@pytest.mark.lean
def test_false_lemma_is_refuted_without_asking_the_model(scripted_model):
    prompts = scripted_model()  # no answers: any model call fails the test
    lemma = prover.prove_lemma(Lemma("bad", "(n : ℕ) : n ^ 2 ≤ 2 * n + 3", ""))
    assert lemma.status == "refuted"
    assert "counterexample" in lemma.feedback
    assert prompts == []


@pytest.mark.lean
def test_repair_round_fixes_a_wrong_proof_then_cache_reuses_it(scripted_model):
    prompts = scripted_model([WRONG], [GOOD])
    lemma = prover.prove_lemma(Lemma("odd_sum", ODD_SUM, "sum of the first n odd numbers is n^2"))
    assert lemma.status == "proved", lemma.feedback
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
