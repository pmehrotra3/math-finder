"""Tests for proving/workspace.py. Lean and the model are faked (the `fake` fixture in conftest.py)."""

from types import SimpleNamespace

import pytest

from mathfinder.proving import workspace as wsmod
from mathfinder.proving.planner import Lemma, Plan
from mathfinder.proving.workspace import Busy, Workspace


def lemmas(*names):
    return [{"name": n, "lean": f": {n}_holds"} for n in names]


def test_a_plan_makes_children_and_writes_their_latex_from_the_lean(fake):
    ws = Workspace(": T")
    assert ws.set_plan("main", "split it", "exact ⟨good_a, good_b⟩", lemmas("good_a", "good_b")) == []
    assert ws.goals["main"].children == ["good_a", "good_b"]
    assert ws.goals["good_b"].latex == "$: good_b_holds$"


def test_lemma_names_must_be_valid_unique_and_new(fake):
    ws = Workspace(": T")
    with pytest.raises(ValueError, match="not valid"):
        ws.set_plan("main", "", "simp", lemmas("not a name"))
    with pytest.raises(ValueError, match="used twice"):
        ws.set_plan("main", "", "simp", lemmas("good_a", "good_a"))
    ws.set_plan("main", "", "simp", lemmas("good_a"))
    with pytest.raises(ValueError, match="already used"):
        ws.set_plan("good_a", "", "simp", lemmas("good_a_sub", "good_a"))


def test_a_bad_lemma_statement_is_reported_as_a_plan_problem(fake):
    ws = Workspace(": T")
    problems = ws.set_plan("main", "", "simp", [{"name": "good_a", "lean": ": nope"}])
    assert problems == ["good_a: unknown identifier"]


def test_editing_a_plan_keeps_unchanged_lemmas_and_their_proofs(fake):
    ws = Workspace(": T")
    ws.set_plan("main", "", "simp", lemmas("good_a", "good_b"))
    ws.set_plan("good_a", "", "simp", lemmas("good_a1"))
    ws.run(lambda: ws.prove("good_a"))
    ws.set_plan("main", "", "simp", [*lemmas("good_a"), {"name": "good_c", "lean": ": new"}])
    assert ws.goals["good_a"].status == "proved" and ws.goals["good_a"].children == ["good_a1"]
    assert "good_b" not in ws.goals and ws.goals["good_c"].status == "open"


def test_proving_goes_bottom_up_and_each_lemma_sees_the_earlier_ones(fake):
    ws = Workspace(": T")
    ws.set_plan("main", "", "exact ⟨good_a, good_b⟩", lemmas("good_a", "good_b"))
    ws.set_plan("good_a", "", "exact good_a1", lemmas("good_a1"))
    ws.run(lambda: ws.prove("main"))
    assert [name for name, _ in fake["proved"]] == ["good_a1", "good_b"]
    assert dict(fake["proved"])["good_b"] == ["good_a1", "good_a"]  # earlier sibling, with what's inside it
    assert all(g.status == "proved" for g in ws.goals.values())
    assert "theorem good_a1" in fake["preamble"]


def test_a_stuck_lemma_leaves_its_parent_stuck(fake):
    ws = Workspace(": T")
    ws.set_plan("main", "", "simp", lemmas("good_a", "bad_b"))
    ws.run(lambda: ws.prove("main"))
    assert ws.goals["bad_b"].status == "stuck" and ws.goals["bad_b"].feedback == "simp failed"
    assert ws.goals["main"].status == "stuck"
    assert ws.goals["bad_b"].attempts[0] == {"proof": "simp", "ok": False, "error": "simp failed", "source": "llm"}


def test_an_assumed_lemma_lets_the_parent_be_proved_but_not_finished(fake):
    ws = Workspace(": T")
    ws.set_plan("main", "", "simp", lemmas("good_a", "bad_b"))
    ws.assume("bad_b")
    ws.run(lambda: ws.prove("main"))
    assert ws.goals["main"].status == "proved"
    assert ws.to_dict()["goals"]["main"]["assumes"] == ["bad_b"]
    with pytest.raises(ValueError, match="still assumed"):
        ws.finish()


def test_finish_assembles_the_whole_tree_children_first(fake):
    ws = Workspace(": T")
    ws.set_plan("main", "", "exact good_a", lemmas("good_a"))
    ws.set_plan("good_a", "", "exact good_a1", lemmas("good_a1"))
    ws.run(lambda: ws.prove("main"))
    code = ws.finish()["lean_code"]
    assert code.index("theorem good_a1") < code.index("theorem good_a ") < code.index("theorem main")


def test_only_one_job_at_a_time(fake):
    ws = Workspace(": T")

    def nested():
        with pytest.raises(Busy):
            ws.run(lambda: None)

    ws.run(nested)
    assert not ws.busy


def test_auto_turns_the_searchs_plan_into_lemmas(fake, monkeypatch):
    plan = Plan(
        "found it", [Lemma(name="key", statement=": K", informal="", status="proved", proof="simp")], "exact key"
    )
    monkeypatch.setattr(wsmod, "search", lambda *a, **k: SimpleNamespace(proved=True, plan=plan, message=""))
    ws = Workspace(": T")
    ws.run(lambda: ws.auto("main"))
    assert ws.goals["main"].children == ["key"] and ws.goals["key"].status == "proved"
    assert ws.goals["main"].proof == "exact key" and ws.goals["key"].latex == "$: K$"


def test_proposed_plans_are_renamed_when_their_names_are_taken(fake, monkeypatch):
    ws = Workspace(": T")
    ws.set_plan("main", "", "exact key", lemmas("key"))
    proposal = Plan("p", [Lemma(name="key", statement=": K", informal="")], "exact key")
    monkeypatch.setattr(wsmod, "propose_plans", lambda *a: [proposal])
    [option] = ws.propose("key")
    assert option["lemmas"][0]["name"] == "key_key" and option["main_proof"] == "exact key_key"


def test_summary_shows_the_tree_for_the_assistant(fake):
    ws = Workspace(": T")
    ws.set_plan("main", "two steps", "simp", lemmas("good_a", "bad_b"))
    ws.run(lambda: ws.prove("bad_b"))
    text = ws.summary()
    assert "- main [open]: : T" in text and "plan: two steps" in text
    assert "  - bad_b [stuck]" in text and "Lean said: simp failed" in text


def test_a_plan_without_lemmas_tries_its_own_proof_first(fake):
    ws = Workspace(": T")
    ws.set_plan("main", "one line", "omega", [])
    ws.run(lambda: ws.prove("main"))
    assert ws.goals["main"].status == "proved" and ws.goals["main"].proof == "omega"
    assert ws.goals["main"].attempts[0]["source"] == "plan" and fake["proved"] == []  # no model needed


@pytest.mark.lean
def test_citing_an_established_project_passes_the_real_final_check(monkeypatch):
    """Real Lean: project A proves a² + b² ≥ 2ab; project B cites it to prove x² + 1 ≥ 2x."""
    from mathfinder.interface.server import in_namespace

    monkeypatch.setattr(wsmod, "to_latex", lambda name, lean: "")  # no model needed
    amgm = "(a b : ℝ) : a ^ 2 + b ^ 2 ≥ 2 * a * b"
    a = Workspace(amgm)
    a.set_plan("main", "", "nlinarith [sq_nonneg (a - b)]", [])
    a.run(lambda: a.prove("main"))
    project_a = in_namespace(a.finish()["own_code"], "Project_aaaa1111")

    b = Workspace("(x : ℝ) : x ^ 2 + 1 ≥ 2 * x", library_code=lambda ids: project_a if ids else "")
    lemma = {"name": "amgm", "lean": amgm, "established_by": "aaaa1111"}
    assert b.set_plan("main", "AM-GM with b = 1", "have h := amgm x 1\nnlinarith [h]", [lemma]) == []
    assert b.goals["amgm"].status == "proved", b.goals["amgm"].feedback
    b.run(lambda: b.prove("main"))
    final = b.finish()
    assert final["ok"], final["errors"]
    assert final["lean_code"].startswith("namespace Project_aaaa1111")
