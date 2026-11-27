"""Tests for pipeline.py and interface/server.py. Lean and the model are faked."""

import json

import pytest
from fastapi.testclient import TestClient

from mathfinder import assistant, pipeline
from mathfinder.interface import server
from mathfinder.llm import LLMError
from mathfinder.projects import ProjectStore
from mathfinder.proving.optimize import Optimization
from mathfinder.proving.planner import Plan
from mathfinder.proving.search import SearchResult
from mathfinder.translation.formalize import Formalization


def fake_search(statement, hints, rounds, on_event, session):
    on_event({"kind": "start", "depth": 0, "theorem": statement})
    on_event({"kind": "done", "depth": 0, "lean_code": "LONG", "plan": "long"})
    return SearchResult(True, "LONG", Plan("long", [], "simp"), 1, "verified")


def fake_optimize(statement, code, plan, metric, on_event):
    before, after = {"cost": 7}, {"cost": 1}
    return Optimization("SHORT", Plan(f"optimized for {metric}", [], "simp"), metric, before, after, 4, True)


@pytest.fixture
def client(monkeypatch, fake, tmp_path):
    monkeypatch.setattr(pipeline, "search", fake_search)
    monkeypatch.setattr(pipeline, "optimize", fake_optimize)
    monkeypatch.setattr(pipeline, "explain", lambda theorem, plan, code: f"explained {plan.sketch}: {code}")
    monkeypatch.setattr(server, "explain", lambda theorem, plan, code: "## Proof\nexplained")
    monkeypatch.setattr(server, "STORE", ProjectStore(tmp_path / "projects"))
    server.OPEN.clear()
    return TestClient(server.app)


# ---------- pipeline ----------


def test_the_better_proof_is_saved_explained_and_traced(client):
    outcome = pipeline.prove(": True", r"$1 = 1$", metric="readable")
    assert outcome["lean_code"] == "SHORT" and outcome["plan"] == "optimized for readable"
    assert outcome["optimization"] == {"metric": "readable", "before": {"cost": 7}, "after": {"cost": 1}}
    [run] = pipeline.RUNS_DIR.iterdir()
    assert outcome["folder"] == f"runs/{run.name}"
    assert (run / "theorem.txt").read_text() == "$1 = 1$\n"
    proof = (run / "proof.lean").read_text()
    assert proof.startswith("import Mathlib") and proof.endswith("SHORT")
    assert (run / "explanation.md").read_text() == "explained optimized for readable: SHORT\n"
    trace = [json.loads(line) for line in (run / "trace.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in trace] == ["meta", "start", "done", "result", "explaining"]


def test_first_proof_can_be_kept_and_a_failed_explanation_still_saves_it(client, monkeypatch):
    def broken(theorem, plan, code):
        raise LLMError("model unavailable")

    monkeypatch.setattr(pipeline, "explain", broken)
    outcome = pipeline.prove(": True", metric=None)
    assert outcome["lean_code"] == "LONG" and outcome["optimization"] is None
    assert "could not write an explanation" in outcome["explanation"]
    assert len(list(pipeline.RUNS_DIR.glob("*/proof.lean"))) == 1


def test_run_folders_never_collide():
    assert len({pipeline.new_run_dir() for _ in range(3)}) == 3


# ---------- projects (no server) ----------


def test_projects_are_saved_and_read_back(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create("(n : ℕ) : n = n", "$n = n$")
    project.goals = {"main": {"status": "working"}}
    store.save(project)
    again = ProjectStore(tmp_path).projects[project.id]
    assert again.title == "$n = n$"
    assert again.goals["main"]["status"] == "open"  # a job can't still be running after a restart


def test_established_code_comes_with_everything_it_builds_on(tmp_path):
    store = ProjectStore(tmp_path)
    a, b, c = (store.create(f": {x}") for x in "ABC")
    a.established_code, b.established_code = "A-code", "B-code"
    b.uses = [a.id]
    for p in (a, b, c):
        store.save(p)
    assert store.lean_code_for([b.id, a.id]) == "A-code\n\nB-code"  # each once, dependencies first
    assert {r["id"] for r in store.library(excluding=c.id)} == {a.id, b.id}
    assert {r["id"] for r in store.library(excluding=a.id)} == set()  # b uses a, so a can't use b
    with pytest.raises(ValueError, match="other projects use this one"):
        store.delete(a.id)
    store.delete(c.id)
    assert c.id not in ProjectStore(tmp_path).projects


# ---------- server ----------


def new_project(client, statement=": T"):
    return client.post("/api/projects", json={"statement": statement, "latex": "$T$"}).json()["id"]


def act(client, project_id, goal, action, **body):
    return client.post(f"/api/projects/{project_id}/goals/{goal}/{action}", json=body)


def prove_and_finish(client, project_id):
    with client.websocket_connect(f"/ws/{project_id}") as ws:
        assert ws.receive_json()["kind"] == "snapshot"
        assert act(client, project_id, "main", "prove").json() == {"started": True}
        events = [ws.receive_json()]
        while events[-1]["kind"] != "idle":
            events.append(ws.receive_json())
        client.post(f"/api/projects/{project_id}/finish")
        while events[-1]["kind"] not in ("finished", "error"):
            events.append(ws.receive_json())
    assert events[-1]["kind"] == "finished", events[-1]
    return events


def with_one_lemma_plan(client, project_id, lemma):
    """Give the theorem a plan that the fake prover can prove."""
    plan = {"sketch": "", "main_proof": f"exact {lemma}", "lemmas": [{"name": lemma, "lean": ": A"}]}
    act(client, project_id, "main", "plan", **plan)


def test_formalize_endpoint(client, monkeypatch):
    f = Formalization("x", "(n : ℕ) : n = n", "For all n, n = n.", True, "same")
    monkeypatch.setattr(server.formalizer, "formalize", lambda latex: f)
    data = client.post("/api/formalize", json={"latex": "$n = n$"}).json()
    assert data["statement"] == "(n : ℕ) : n = n" and data["ok"] and data["faithful"]


def test_plan_prove_and_finish_makes_the_project_established(client):
    project_id = new_project(client)
    lemmas = [{"name": "good_a", "lean": ": A"}, {"name": "good_b", "lean": ": B"}]
    answer = act(
        client, project_id, "main", "plan", sketch="two steps", main_proof="exact ⟨good_a, good_b⟩", lemmas=lemmas
    )
    assert answer.json()["problems"] == []
    assert answer.json()["workspace"]["goals"]["good_a"]["latex"] == "$: A$"

    events = prove_and_finish(client, project_id)
    assert any(e["kind"] == "attempt" and e["name"] == "good_b" for e in events)
    result = events[-1]["result"]
    assert result["proved"] and result["explanation"] == "## Proof\nexplained" and events[-1]["established"]
    saved = pipeline.RUNS_DIR / result["folder"].removeprefix("runs/")
    assert (saved / "proof.lean").read_text().endswith(result["lean_code"])

    project = server.STORE.projects[project_id]
    assert project.established_code.startswith(f"namespace Project_{project_id}\n")
    assert project.established_code.endswith(f"end Project_{project_id}\n")
    listed = client.get("/api/projects").json()["projects"]
    assert listed[0]["id"] == project_id and listed[0]["status"] == "established"


def test_a_project_can_cite_an_established_one(client):
    established = new_project(client, ": A")
    with_one_lemma_plan(client, established, "good_x")
    prove_and_finish(client, established)
    citing = new_project(client, ": T")
    assert [r["id"] for r in client.get(f"/api/projects/{citing}/library").json()["results"]] == [established]

    lemma = {"name": "from_a", "lean": ": A", "established_by": established}
    goals = act(client, citing, "main", "plan", sketch="use A", main_proof="exact from_a", lemmas=[lemma]).json()
    cited = goals["workspace"]["goals"]["from_a"]
    assert cited["status"] == "proved" and cited["proof"] == f"apply Project_{established}.main <;> assumption"
    assert server.STORE.projects[citing].uses == [established]

    result = prove_and_finish(client, citing)[-1]["result"]
    assert result["proved"]
    assert result["lean_code"].index(f"namespace Project_{established}") < result["lean_code"].index("theorem from_a")
    # the established project can't now cite the one that cites it
    assert client.get(f"/api/projects/{established}/library").json()["results"] == []
    bad = act(client, established, "main", "use-established", project=citing)
    assert bad.status_code == 400
    assert client.delete(f"/api/projects/{established}").status_code == 400  # it is in use


def test_only_established_projects_can_be_cited(client):
    unfinished = new_project(client, ": A")
    project_id = new_project(client)
    act(client, project_id, "main", "plan", sketch="", main_proof="simp", lemmas=[{"name": "good_a", "lean": ": A"}])
    answer = act(client, project_id, "good_a", "use-established", project=unfinished)
    assert answer.status_code == 400 and "not an established result" in answer.json()["detail"]


def test_a_project_survives_a_server_restart(client, monkeypatch):
    project_id = new_project(client)
    act(
        client, project_id, "main", "plan", sketch="idea", main_proof="simp", lemmas=[{"name": "good_a", "lean": ": A"}]
    )
    server.OPEN.clear()
    monkeypatch.setattr(server, "STORE", ProjectStore(server.STORE.folder))
    reopened = client.get(f"/api/projects/{project_id}").json()
    assert reopened["workspace"]["goals"]["main"]["children"] == ["good_a"]
    assert reopened["workspace"]["goals"]["main"]["sketch"] == "idea"


def test_assume_then_edit_a_statement(client):
    project_id = new_project(client)
    act(client, project_id, "main", "plan", sketch="", main_proof="simp", lemmas=[{"name": "good_a", "lean": ": A"}])
    goals = act(client, project_id, "good_a", "assume", on=True).json()["workspace"]["goals"]
    assert goals["good_a"]["status"] == "assumed"
    assert act(client, project_id, "good_a", "statement", lean=": nope").json()["errors"] == ["unknown identifier"]
    edited = act(client, project_id, "good_a", "statement", lean=": A2").json()["workspace"]["goals"]["good_a"]
    assert (edited["lean"], edited["latex"], edited["status"]) == (": A2", "$: A2$", "open")


def test_the_side_assistant_sees_the_proof_and_keeps_the_conversation(client, monkeypatch):
    seen = []

    def fake_ask(question, proof_state, history):
        seen.append((question, proof_state, list(history)))
        return f"answer to {question}"

    monkeypatch.setattr(assistant, "ask", fake_ask)
    project_id = new_project(client)
    assert client.post(f"/api/projects/{project_id}/ask", json={"question": "why?"}).json() == {
        "answer": "answer to why?"
    }
    client.post(f"/api/projects/{project_id}/ask", json={"question": "and then?"})
    assert "- main [open]: : T" in seen[0][1]
    assert seen[1][2] == [("why?", "answer to why?")]
    assert client.get(f"/api/projects/{project_id}").json()["conversation"][0] == ["why?", "answer to why?"]


def test_the_assistant_failing_is_a_clear_error(client, monkeypatch):
    def broken(*args):
        raise LLMError("model unavailable")

    monkeypatch.setattr(assistant, "ask", broken)
    answer = client.post(f"/api/projects/{new_project(client)}/ask", json={"question": "why?"})
    assert answer.status_code == 502 and "model unavailable" in answer.json()["detail"]


def test_changes_are_refused_while_a_job_runs(client):
    project_id = new_project(client)
    workspace = server.live(project_id).workspace
    workspace._job.acquire()
    try:
        assert act(client, project_id, "main", "assume", on=True).status_code == 409
        assert act(client, project_id, "main", "prove").status_code == 409
        assert client.delete(f"/api/projects/{project_id}").status_code == 409
    finally:
        workspace._job.release()


def test_bad_requests_are_rejected(client):
    project_id = new_project(client)
    assert act(client, project_id, "nope", "prove").status_code == 404
    assert act(client, project_id, "main", "dance").status_code == 400
    bad = act(client, project_id, "main", "plan", sketch="", main_proof="simp", lemmas=[{"name": "a b", "lean": ": A"}])
    assert bad.status_code == 400 and "not valid" in bad.json()["detail"]
    assert client.get("/api/projects/nope").status_code == 404


def test_the_web_page_is_served(client):
    page = client.get("/").text
    assert "<title>Math Finder</title>" in page and 'id="projects-panel"' in page
    assert client.get("/app.js").status_code == 200
