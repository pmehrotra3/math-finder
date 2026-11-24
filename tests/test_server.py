"""Tests for mathfinder/pipeline.py and mathfinder/server.py. Lean and the model are faked."""

import json

import pytest
from fastapi.testclient import TestClient

from mathfinder import pipeline, server
from mathfinder.llm import LLMError
from mathfinder.optimize import Optimization
from mathfinder.planner import Plan
from mathfinder.search import SearchResult
from mathfinder.translate import Formalization


def fake_search(statement, hints, rounds, on_event, session):
    on_event({"kind": "start", "depth": 0, "theorem": statement})
    on_event({"kind": "done", "depth": 0, "lean_code": "LONG", "plan": "long"})
    return SearchResult(True, "LONG", Plan("long", [], "simp"), 1, "verified")


def fake_optimize(statement, code, plan, metric, on_event):
    before, after = {"cost": 7}, {"cost": 1}
    return Optimization("SHORT", Plan(f"optimized for {metric}", [], "simp"), metric, before, after, 4, True)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pipeline, "search", fake_search)
    monkeypatch.setattr(pipeline, "optimize", fake_optimize)
    monkeypatch.setattr(pipeline, "explain", lambda theorem, plan, code: f"explained {plan.sketch}: {code}")
    server.RUNS.clear()
    return TestClient(server.app)


def start(client, **body):
    return client.post("/api/sessions", json={"statement": "t", **body})


def watch(client, run_id):
    with client.websocket_connect(f"/ws/{run_id}") as ws:
        events = []
        while not events or events[-1]["kind"] != "finished":
            events.append(ws.receive_json())
    return events


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


def test_formalize_endpoint(client, monkeypatch):
    f = Formalization("x", "(n : ℕ) : n = n", "For all n, n = n.", True, "same")
    monkeypatch.setattr(server.translate, "formalize", lambda latex: f)
    data = client.post("/api/formalize", json={"latex": "$n = n$"}).json()
    assert data["statement"] == "(n : ℕ) : n = n" and data["ok"] and data["faithful"]


def test_a_search_streams_its_events_and_ends_with_the_result(client):
    run_id = start(client).json()["id"]
    events = watch(client, run_id)
    assert [e["kind"] for e in events] == ["start", "done", "explaining", "finished"]
    assert events[-1]["status"] == "proved" and events[-1]["result"]["lean_code"] == "SHORT"
    assert watch(client, run_id) == events  # a late visitor gets the full history


def test_actions_reach_the_session_while_it_waits_for_an_answer(client, monkeypatch):
    def search_that_asks(statement, hints, rounds, on_event, session):
        on_event({"kind": "ask_user", "question": "Any idea?"})
        on_event({"kind": "user_answer", "text": session.ask("Any idea?")})
        return SearchResult(False, message=f"hints were {session.hints()}")

    monkeypatch.setattr(pipeline, "search", search_that_asks)
    run_id = start(client, hints=["start hint"]).json()["id"]
    assert client.post(f"/api/sessions/{run_id}/hint", json={"text": "try cases"}).json() == {"ok": True}
    with client.websocket_connect(f"/ws/{run_id}") as ws:
        assert ws.receive_json()["kind"] == "ask_user"
        client.post(f"/api/sessions/{run_id}/answer", json={"text": "use induction"})
        assert ws.receive_json()["text"] == "use induction"
        final = ws.receive_json()
    assert final["status"] == "not_proved"
    assert final["result"]["message"] == "hints were ['start hint', 'try cases']"


def test_stopped_and_crashed_searches_still_finish(client, monkeypatch):
    def stopped(statement, hints, rounds, on_event, session):
        session.stop()
        return SearchResult(False, message="stopped")

    monkeypatch.setattr(pipeline, "search", stopped)
    assert watch(client, start(client).json()["id"])[-1]["status"] == "stopped"
    monkeypatch.setattr(pipeline, "search", lambda *a, **k: 1 / 0)
    final = watch(client, start(client).json()["id"])[-1]
    assert final["status"] == "not_proved" and "internal error" in final["result"]["message"]


def test_bad_requests_are_rejected(client):
    assert start(client, metric="fastest").status_code == 400
    assert client.post("/api/sessions/nope/hint", json={}).status_code == 404
    run_id = start(client).json()["id"]
    assert client.post(f"/api/sessions/{run_id}/dance", json={}).status_code == 400


def test_the_web_page_is_served(client):
    assert "<title>Math Finder</title>" in client.get("/").text
    assert client.get("/app.js").status_code == 200
