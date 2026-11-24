"""Tests for proving/planner.py and the skeleton check in proving/search.py."""

import pytest

from mathfinder import llm
from mathfinder.proving.planner import Lemma, Plan, build_prompt, parse_plans, propose_plans
from mathfinder.proving.search import check_skeletons

AMGM = "(a b : ℝ) : a ^ 2 + b ^ 2 ≥ 2 * a * b"

GOOD_JSON = """Here you go:
```json
{"plans": [
  {"sketch": "Square of a difference is nonnegative.",
   "lemmas": [{"name": "key", "statement": "(a b : ℝ) : 0 ≤ (a - b) ^ 2", "informal": "squares are nonnegative"}],
   "main_proof": "nlinarith [key a b]"},
  {"sketch": "Bad names.",
   "lemmas": [{"name": "not a name!", "statement": ": True", "informal": ""}],
   "main_proof": "trivial"},
  {"sketch": "Missing main proof.", "lemmas": []}
]}
```"""


# ---------- fast tests ----------


def test_parse_plans_keeps_only_wellformed_plans():
    plans = parse_plans(llm.extract_json(GOOD_JSON))
    assert len(plans) == 1
    assert plans[0].lemmas[0].name == "key"
    assert plans[0].main_proof == "nlinarith [key a b]"


def test_parse_plans_rejects_duplicate_lemma_names():
    data = {
        "plans": [
            {
                "sketch": "",
                "main_proof": "simp",
                "lemmas": [{"name": "a", "statement": ": True"}, {"name": "a", "statement": ": True"}],
            }
        ]
    }
    assert parse_plans(data) == []


def test_prompt_puts_user_hints_first_class():
    prompt = build_prompt(AMGM, 2, ["use (a-b)^2 ≥ 0"], "plan 1 failed: lemma false")
    assert "theorem main (a b : ℝ)" in prompt
    assert "- use (a-b)^2 ≥ 0" in prompt
    assert "plan 1 failed" in prompt


def test_propose_plans_with_fake_llm(fake_llm):
    fake_llm(GOOD_JSON)
    plans = propose_plans(AMGM)
    assert [p.sketch for p in plans] == ["Square of a difference is nonnegative."]


def test_sorry_in_main_proof_is_rejected_without_lean():
    plan = Plan("cheat", [], "sorry")
    assert "forbidden" in check_skeletons(AMGM, [plan])[0][0]


# ---------- Lean tests ----------


@pytest.mark.lean
def test_skeleton_check_in_one_lean_run():
    key = Lemma("key", "(a b : ℝ) : 0 ≤ (a - b) ^ 2", "")
    good = Plan("works", [key], "nlinarith [key a b]")
    wrong_name = Plan("cites a lemma that isn't there", [key], "nlinarith [nope a b]")
    bad_statement = Plan("lemma doesn't type-check", [Lemma("key", "(a : ℝ) : a = undefined_x", "")], "simp")
    not_enough = Plan("lemma too weak", [Lemma("key", "(a : ℝ) : 0 ≤ a ^ 2", "")], "linarith [key a]")

    problems = check_skeletons(AMGM, [good, wrong_name, bad_statement, not_enough])

    assert problems[0] == []  # both plans name their lemma "key": namespaces keep them apart
    assert any("nope" in p for p in problems[1])
    assert any("undefined_x" in p for p in problems[2])
    assert problems[3] != []
    assert good.feedback == [] and wrong_name.feedback != []


# ---------- real Claude + Lean ----------


@pytest.mark.llm
def test_real_planner_produces_a_plan_that_fits_together():
    plans = propose_plans(AMGM, n=2)
    assert plans, "planner returned no well-formed plans"
    problems = check_skeletons(AMGM, plans)
    assert any(p == [] for p in problems), problems
