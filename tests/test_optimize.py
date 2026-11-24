"""Tests for proving/optimize.py."""

import pytest

from mathfinder.proving import optimize as opt
from mathfinder.proving.planner import Lemma, Plan
from mathfinder.proving.search import assemble

AMGM = "(a b : ℝ) : a ^ 2 + b ^ 2 ≥ 2 * a * b"
LONG_PLAN = Plan(
    "Two lemmas, then linarith.",
    [
        Lemma("sq_diff", "(a b : ℝ) : (a - b) ^ 2 = a ^ 2 - 2 * a * b + b ^ 2", "", status="proved", proof="ring"),
        Lemma("sq_diff_nonneg", "(a b : ℝ) : 0 ≤ (a - b) ^ 2", "", status="proved", proof="positivity"),
    ],
    "have h1 := sq_diff a b\nhave h2 := sq_diff_nonneg a b\nlinarith",
)
LONG_CODE = assemble(AMGM, LONG_PLAN)


def test_proof_stats_counts_steps_lemmas_and_heavy_tactics():
    assert opt.proof_stats(LONG_CODE) == {"steps": 5, "lemmas": 2, "heavy": 0, "cost": 7}
    one_liner = assemble(AMGM, Plan("", [], "nlinarith [sq_nonneg (a - b)]"))
    assert opt.proof_stats(one_liner, "short") == {"steps": 1, "lemmas": 0, "heavy": 1, "cost": 1}
    assert opt.proof_stats(one_liner, "readable")["cost"] == 6  # 1 step + 5 for the heavy nlinarith


def test_metric_changes_which_proof_wins():
    one_liner = assemble(AMGM, Plan("", [], "nlinarith [sq_nonneg (a - b)]"))
    assert opt.proof_stats(one_liner, "short")["cost"] < opt.proof_stats(LONG_CODE, "short")["cost"]
    assert opt.proof_stats(one_liner, "readable")["cost"] < opt.proof_stats(LONG_CODE, "readable")["cost"]
    assert opt.proof_stats(one_liner, "few_lemmas")["cost"] < opt.proof_stats(LONG_CODE, "few_lemmas")["cost"]


def test_prompt_names_the_goal_of_the_metric():
    assert "READABLE" in opt.build_prompt(AMGM, LONG_CODE, "readable")


def test_no_candidates_keeps_the_original(fake_llm):
    fake_llm({"proofs": []})
    result = opt.optimize(AMGM, LONG_CODE, LONG_PLAN)
    assert not result.improved and result.lean_code == LONG_CODE


@pytest.mark.lean
def test_a_shorter_verified_proof_replaces_the_original(fake_llm):
    fake_llm({"proofs": ["linarith", "nlinarith [sq_nonneg (a - b)]"]})  # the first one is wrong
    events = []
    result = opt.optimize(AMGM, LONG_CODE, LONG_PLAN, on_event=events.append)

    assert result.improved and result.tried == 2
    assert "nlinarith [sq_nonneg (a - b)]" in result.lean_code
    assert (result.original["cost"], result.best["cost"]) == (7, 1)
    assert [e["ok"] for e in events if e["kind"] == "optimize_candidate"] == [False, True]
