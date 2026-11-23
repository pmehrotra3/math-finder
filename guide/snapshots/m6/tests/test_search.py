"""Tests for mathfinder/search.py and mathfinder/cli.py."""

import json

import pytest

from mathfinder import cli, llm, planner, prover
from mathfinder.planner import Lemma, Plan
from mathfinder.search import SearchResult, assemble, score, search, splice

ODD_SUM = "(n : ℕ) : ∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2"
INDUCTION = "induction n with\n| zero => simp\n| succ k ih =>\n  rw [Finset.sum_range_succ, ih]\n  ring"


@pytest.fixture(autouse=True)
def temp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(prover, "CACHE_FILE", tmp_path / "lemmas.json")


# ---------- fast tests ----------


def test_assemble_builds_one_namespaced_file():
    plan = Plan("idea", [Lemma("key", "(a : ℝ) : 0 ≤ a ^ 2", "", status="proved", proof="positivity")], "nlinarith [key a]")
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
    assert [l.name for l in plan.lemmas] == ["first", "hard_step", "hard"]
    assert stuck.status == "proved" and stuck.proof == "exact hard_step n"


def test_cli_passes_arguments_to_search(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)  # don't write into the real runs/ folder
    seen = {}

    def fake_search(theorem, hints, rounds, on_event):
        seen.update(theorem=theorem, hints=hints, rounds=rounds)
        on_event({"kind": "start", "theorem": theorem})
        return SearchResult(True, message="verified")

    monkeypatch.setattr(cli, "search", fake_search)
    code = cli.main(["prove", "--lean", "(n : ℕ) : n = n", "--hint", "use rfl", "--rounds", "1", "--no-explain"])
    assert code == 0
    assert seen == {"theorem": "(n : ℕ) : n = n", "hints": ["use rfl"], "rounds": 1}
    assert "Result: PROVED" in capsys.readouterr().out


def test_cli_indents_sub_search_events(capsys):
    cli.print_event({"kind": "subsearch_start", "depth": 0, "name": "hard", "statement": ": P"})
    cli.print_event({"kind": "lemma_proved", "depth": 1, "name": "step", "statement": ": Q", "feedback": "hammer: simp"})
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "      ↳ hard is stuck: starting a sub-search on it"
    assert out[1] == "        ✓ step : : Q   [hammer: simp]"


# ---------- Lean tests ----------


@pytest.mark.lean
def test_false_theorem_is_refuted_before_any_planning(monkeypatch):
    def no_model(system, prompt):
        raise AssertionError("the model should not be called for a false statement")

    monkeypatch.setitem(llm.BACKENDS, "fake", no_model)
    monkeypatch.setenv("MATHFINDER_LLM", "fake")
    events = []
    result = search("(n : ℕ) : n ^ 2 ≤ 2 * n + 3", on_event=events.append)
    assert not result.proved
    assert "false" in result.message
    assert [e["kind"] for e in events] == ["start", "refuted"]


@pytest.mark.lean
def test_full_search_with_scripted_model(monkeypatch):
    """Plan → skeleton check → hammer proves one lemma, 'the model' proves the other → final check."""
    theorem = "(n : ℕ) : (∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2) ∧ n ≤ n ^ 2"
    plan_json = {"plans": [{
        "sketch": "Prove both halves separately.",
        "lemmas": [
            {"name": "odd_sum", "statement": ODD_SUM, "informal": "sum of odd numbers"},
            {"name": "le_sq", "statement": "(n : ℕ) : n ≤ n ^ 2", "informal": "n ≤ n²"},
        ],
        "main_proof": "exact ⟨odd_sum n, le_sq n⟩",
    }]}

    def scripted(system, prompt):
        if system == planner.SYSTEM:
            return "```json\n" + json.dumps(plan_json) + "\n```"
        return "```json\n" + json.dumps({"proofs": [INDUCTION]}) + "\n```"

    monkeypatch.setitem(llm.BACKENDS, "fake", scripted)
    monkeypatch.setenv("MATHFINDER_LLM", "fake")
    events = []
    result = search(theorem, rounds=1, on_event=events.append)

    assert result.proved, result.message
    assert "theorem main" in result.lean_code and "sorry" not in result.lean_code
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "done"
    assert kinds.count("lemma_proved") == 2


@pytest.mark.lean
def test_stuck_lemma_is_proved_by_a_sub_search(monkeypatch):
    """The 'model' can't prove odd_sum directly, so a sub-search splits it into a smaller step."""
    theorem = "(n : ℕ) : (∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2) ∧ n ≤ n ^ 2"
    top_plan = {"plans": [{
        "sketch": "Prove both halves separately.",
        "lemmas": [
            {"name": "odd_sum", "statement": ODD_SUM, "informal": ""},
            {"name": "le_sq", "statement": "(n : ℕ) : n ≤ n ^ 2", "informal": ""},
        ],
        "main_proof": "exact ⟨odd_sum n, le_sq n⟩",
    }]}
    sub_plan = {"plans": [{
        "sketch": "Induction, using the step (k+1)^2 = k^2 + (2k+1).",
        "lemmas": [{"name": "step", "statement": "(k : ℕ) : (k + 1) ^ 2 = k ^ 2 + (2 * k + 1)", "informal": ""}],
        "main_proof": "induction n with\n| zero => simp\n| succ k ih =>\n  rw [Finset.sum_range_succ, ih, step k]",
    }]}

    def scripted(system, prompt):
        if system == planner.SYSTEM:
            chosen = top_plan if "∧" in prompt else sub_plan
            return "```json\n" + json.dumps(chosen) + "\n```"
        return "```json\n" + json.dumps({"proofs": ["simp"]}) + "\n```"  # the prover always fails

    monkeypatch.setitem(llm.BACKENDS, "fake", scripted)
    monkeypatch.setenv("MATHFINDER_LLM", "fake")
    monkeypatch.setattr(prover, "REPAIR_ROUNDS", 0)  # fail fast: one LLM round per lemma
    events = []
    result = search(theorem, rounds=1, on_event=events.append)

    assert result.proved, result.message
    assert "theorem odd_sum_step" in result.lean_code  # the sub-lemma, renamed and spliced in
    assert any(e["kind"] == "subsearch_start" for e in events)
    assert any(e["kind"] == "done" and e["depth"] == 1 for e in events)
