"""Tests for proving/search.py."""

import pytest

from mathfinder.proving import planner, prover
from mathfinder.proving.planner import Lemma, Plan
from mathfinder.proving.search import assemble, score, search, splice

ODD_SUM = "(n : ℕ) : ∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2"
INDUCTION = "induction n with\n| zero => simp\n| succ k ih =>\n  rw [Finset.sum_range_succ, ih]\n  ring"


# ---------- fast tests ----------


def test_assemble_builds_one_namespaced_file():
    plan = Plan(
        "idea", [Lemma("key", "(a : ℝ) : 0 ≤ a ^ 2", "", status="proved", proof="positivity")], "nlinarith [key a]"
    )
    assert assemble("(a : ℝ) : a ^ 2 ≥ 0", plan) == (
        "namespace MathFinder\n\n"
        "theorem key (a : ℝ) : 0 ≤ a ^ 2 := by\n  positivity\n\n"
        "theorem main (a : ℝ) : a ^ 2 ≥ 0 := by\n  nlinarith [key a]\n\n"
        "end MathFinder\n"
    )


def test_score_is_fraction_proved_and_refuted_is_worst():
    proved = Lemma("a", ": True", "", status="proved")
    stuck = Lemma("b", ": True", "", status="stuck")
    refuted = Lemma("c", ": True", "", status="refuted")
    assert score(Plan("", [proved, stuck], "")) == 0.5
    assert score(Plan("", [proved, refuted], "")) == -1.0


def test_splice_inserts_renamed_sub_lemmas_before_the_stuck_lemma():
    first = Lemma("first", ": True", "", status="proved", proof="trivial")
    stuck = Lemma("hard", "(n : ℕ) : P n", "", status="stuck")
    plan = Plan("", [first, stuck], "exact hard")
    sub = Plan("", [Lemma("step", "(k : ℕ) : Q k", "", status="proved", proof="simp")], "exact step n")

    assert splice(plan, stuck, sub) == 1
    assert [lem.name for lem in plan.lemmas] == ["first", "hard_step", "hard"]
    assert stuck.status == "proved" and stuck.proof == "exact hard_step n"


# ---------- Lean tests ----------


@pytest.mark.lean
def test_false_theorem_is_refuted_before_any_planning(fake_llm):
    fake_llm()  # no replies: any model call fails the test
    events = []
    result = search("(n : ℕ) : n ^ 2 ≤ 2 * n + 3", on_event=events.append)
    assert not result.proved
    assert "false" in result.message
    assert [e["kind"] for e in events] == ["start", "refuted"]


@pytest.mark.lean
def test_full_search_with_scripted_model(fake_llm):
    """Plan → skeleton check → hammer proves one lemma, 'the model' proves the other → final check."""
    theorem = "(n : ℕ) : (∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2) ∧ n ≤ n ^ 2"
    plan_json = {
        "plans": [
            {
                "sketch": "Prove both halves separately.",
                "lemmas": [
                    {"name": "odd_sum", "statement": ODD_SUM, "informal": "sum of odd numbers"},
                    {"name": "le_sq", "statement": "(n : ℕ) : n ≤ n ^ 2", "informal": "n ≤ n²"},
                ],
                "main_proof": "exact ⟨odd_sum n, le_sq n⟩",
            }
        ]
    }

    def scripted(system, prompt):
        if system == planner.SYSTEM:
            return plan_json
        return {"proofs": [INDUCTION]}

    fake_llm(scripted)
    events = []
    result = search(theorem, rounds=1, on_event=events.append)

    assert result.proved, result.message
    assert "theorem main" in result.lean_code and "sorry" not in result.lean_code
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "done"
    assert kinds.count("lemma_proved") == 2


@pytest.mark.lean
def test_stuck_lemma_is_proved_by_a_sub_search(fake_llm, monkeypatch):
    """The 'model' can't prove odd_sum directly, so a sub-search splits it into a smaller step."""
    theorem = "(n : ℕ) : (∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2) ∧ n ≤ n ^ 2"
    top_plan = {
        "plans": [
            {
                "sketch": "Prove both halves separately.",
                "lemmas": [
                    {"name": "odd_sum", "statement": ODD_SUM, "informal": ""},
                    {"name": "le_sq", "statement": "(n : ℕ) : n ≤ n ^ 2", "informal": ""},
                ],
                "main_proof": "exact ⟨odd_sum n, le_sq n⟩",
            }
        ]
    }
    sub_plan = {
        "plans": [
            {
                "sketch": "Induction, using the step (k+1)^2 = k^2 + (2k+1).",
                "lemmas": [
                    {"name": "step", "statement": "(k : ℕ) : (k + 1) ^ 2 = k ^ 2 + (2 * k + 1)", "informal": ""}
                ],
                "main_proof": (
                    "induction n with\n| zero => simp\n| succ k ih =>\n  rw [Finset.sum_range_succ, ih, step k]"
                ),
            }
        ]
    }

    def scripted(system, prompt):
        if system == planner.SYSTEM:
            return top_plan if "∧" in prompt else sub_plan
        return {"proofs": ["simp"]}  # the prover always fails

    fake_llm(scripted)
    monkeypatch.setattr(prover, "REPAIR_ROUNDS", 0)  # fail fast: one LLM round per lemma
    events = []
    result = search(theorem, rounds=1, on_event=events.append)

    assert result.proved, result.message
    assert "theorem odd_sum_step" in result.lean_code  # the sub-lemma, renamed and spliced in
    assert any(e["kind"] == "subsearch_start" for e in events)
    assert any(e["kind"] == "done" and e["depth"] == 1 for e in events)
