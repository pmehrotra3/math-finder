"""Tests for pipeline.py and interface/server.py. Lean and the model are faked."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from mathfinder import assistant, pipeline
from mathfinder.interface import server
from mathfinder.llm import LLMError
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
def client(monkeypatch, fake):
    monkeypatch.setattr(pipeline, "search", fake_search)
    monkeypatch.setattr(pipeline, "optimize", fake_optimize)
    monkeypatch.setattr(pipeline, "explain", lambda theorem, plan, code: f"explained {plan.sketch}: {code}")
    monkeypatch.setattr(server, "explain", lambda theorem, plan, code: "## Proof\nexplained")
    server.WORKSPACES.clear()
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


# ---------- server ----------


def new_workspace(client):
    return client.post("/api/workspaces", json={"statement": ": T", "latex": "$T$"}).json()["id"]


def act(client, ws_id, goal, action, **body):
    return client.post(f"/api/workspaces/{ws_id}/goals/{goal}/{action}", json=body)


def wait_until_idle(client, ws_id):
    for _ in range(200):
        if not server.WORKSPACES[ws_id].workspace.busy:
            return
        time.sleep(0.01)
    raise AssertionError("the background job never finished")


def test_formalize_endpoint(client, monkeypatch):
    f = Formalization("x", "(n : ℕ) : n = n", "For all n, n = n.", True, "same")
    monkeypatch.setattr(server.formalizer, "formalize", lambda latex: f)
    data = client.post("/api/formalize", json={"latex": "$n = n$"}).json()
    assert data["statement"] == "(n : ℕ) : n = n" and data["ok"] and data["faithful"]


def test_plan_prove_and_finish_through_the_api(client):
    ws_id = new_workspace(client)
    lemmas = [{"name": "good_a", "lean": ": A"}, {"name": "good_b", "lean": ": B"}]
    answer = act(client, ws_id, "main", "plan", sketch="two steps", main_proof="exact ⟨good_a, good_b⟩", lemmas=lemmas)
    assert answer.json()["problems"] == []
    assert answer.json()["workspace"]["goals"]["good_a"]["latex"] == "$: A$"

    with client.websocket_connect(f"/ws/{ws_id}") as ws:
        assert ws.receive_json()["kind"] == "snapshot"
        assert act(client, ws_id, "main", "prove").json() == {"started": True}
        events = []
        while not events or events[-1]["kind"] != "idle":
            events.append(ws.receive_json())
        assert events[-1]["workspace"]["goals"]["main"]["status"] == "proved"
        assert any(e["kind"] == "attempt" and e["name"] == "good_b" for e in events)

        client.post(f"/api/workspaces/{ws_id}/finish")
        while events[-1]["kind"] != "finished":
            events.append(ws.receive_json())
    result = events[-1]["result"]
    assert result["proved"] and result["explanation"] == "## Proof\nexplained"
    saved = pipeline.RUNS_DIR / result["folder"].removeprefix("runs/")
    assert (saved / "proof.lean").read_text().endswith(result["lean_code"])
    assert "theorem good_b" in result["lean_code"]


def test_assume_then_edit_a_statement(client):
    ws_id = new_workspace(client)
    act(client, ws_id, "main", "plan", sketch="", main_proof="simp", lemmas=[{"name": "good_a", "lean": ": A"}])
    goals = act(client, ws_id, "good_a", "assume", on=True).json()["workspace"]["goals"]
    assert goals["good_a"]["status"] == "assumed"
    assert act(client, ws_id, "good_a", "statement", lean=": nope").json()["errors"] == ["unknown identifier"]
    edited = act(client, ws_id, "good_a", "statement", lean=": A2").json()["workspace"]["goals"]["good_a"]
    assert (edited["lean"], edited["latex"], edited["status"]) == (": A2", "$: A2$", "open")


def test_the_side_assistant_sees_the_proof_and_earlier_questions(client, monkeypatch):
    seen = []

    def fake_ask(question, proof_state, history):
        seen.append((question, proof_state, list(history)))
        return f"answer to {question}"

    monkeypatch.setattr(assistant, "ask", fake_ask)
    ws_id = new_workspace(client)
    assert client.post(f"/api/workspaces/{ws_id}/ask", json={"question": "why?"}).json() == {"answer": "answer to why?"}
    client.post(f"/api/workspaces/{ws_id}/ask", json={"question": "and then?"})
    assert "- main [open]: : T" in seen[0][1]
    assert seen[1][2] == [("why?", "answer to why?")]


def test_the_assistant_failing_is_a_clear_error(client, monkeypatch):
    def broken(*args):
        raise LLMError("model unavailable")

    monkeypatch.setattr(assistant, "ask", broken)
    answer = client.post(f"/api/workspaces/{new_workspace(client)}/ask", json={"question": "why?"})
    assert answer.status_code == 502 and "model unavailable" in answer.json()["detail"]


def test_changes_are_refused_while_a_job_runs(client, monkeypatch):
    ws_id = new_workspace(client)
    live = server.WORKSPACES[ws_id]
    live.workspace._job.acquire()
    try:
        assert act(client, ws_id, "main", "assume", on=True).status_code == 409
        assert act(client, ws_id, "main", "prove").status_code == 409
    finally:
        live.workspace._job.release()


def test_bad_requests_are_rejected(client):
    ws_id = new_workspace(client)
    assert act(client, ws_id, "nope", "prove").status_code == 404
    assert act(client, ws_id, "main", "dance").status_code == 400
    bad_plan = act(client, ws_id, "main", "plan", sketch="", main_proof="simp", lemmas=[{"name": "a b", "lean": ": A"}])
    assert bad_plan.status_code == 400 and "not valid" in bad_plan.json()["detail"]
    assert client.post("/api/workspaces/nope/stop").status_code == 404


def test_the_web_page_is_served(client):
    page = client.get("/").text
    assert "<title>Math Finder</title>" in page and 'id="step-workspace"' in page
    assert client.get("/app.js").status_code == 200
