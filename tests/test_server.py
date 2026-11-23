"""Tests for server/app.py. The search, explainer and formalizer are faked, so no Lean or model is needed."""

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from mathfinder import cli
from mathfinder.formalize import Formalization
from mathfinder.optimize import Optimization
from mathfinder.planner import Plan
from mathfinder.search import SearchResult


def fake_optimize_that_keeps(statement, code, plan, metric, on_event):
    stats = {"steps": 1, "lemmas": 0, "heavy": 0, "cost": 1}
    return Optimization(code, plan, metric, stats, stats, 0, False)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)  # run folders go to a temporary place
    monkeypatch.setattr(srv.explainer, "explain", lambda theorem, plan, code: "## Proof\nby simp")
    monkeypatch.setattr(srv.optimizer, "optimize", fake_optimize_that_keeps)
    srv.RUNS.clear()
    return TestClient(srv.app)


def fake_search_that_proves(statement, rounds, on_event, session):
    on_event({"kind": "start", "depth": 0, "theorem": statement})
    on_event({"kind": "lemma_proved", "depth": 0, "name": "key", "statement": ": True", "feedback": "hammer"})
    on_event({"kind": "done", "depth": 0, "lean_code": "theorem main ...", "plan": "simp it"})
    return SearchResult(True, "theorem main ...", Plan("simp it", [], "simp"), 1, "verified")


def watch_until_finished(client, run_id):
    with client.websocket_connect(f"/ws/{run_id}") as ws:
        events = []
        while not events or events[-1]["kind"] != "finished":
            events.append(ws.receive_json())
    return events


def test_formalize_endpoint(client, monkeypatch):
    f = Formalization("x", "(n : ℕ) : n = n", "For all n, n = n.", True, "same")
    monkeypatch.setattr(srv.formalizer, "formalize", lambda latex: f)
    data = client.post("/api/formalize", json={"latex": "$n = n$"}).json()
    assert data["statement"] == "(n : ℕ) : n = n" and data["ok"] and data["faithful"]


def test_a_search_streams_its_events_and_ends_with_the_result(client, monkeypatch):
    monkeypatch.setattr(srv.engine, "search", fake_search_that_proves)
    run_id = client.post("/api/sessions", json={"statement": "(n : ℕ) : n = n"}).json()["id"]

    events = watch_until_finished(client, run_id)
    kinds = [e["kind"] for e in events]
    assert kinds == ["start", "lemma_proved", "done", "explaining", "finished"]
    assert events[-1]["result"]["optimization"]["metric"] == "short"
    result = events[-1]["result"]
    assert result["proved"] and result["explanation"] == "## Proof\nby simp"

    status = client.get(f"/api/sessions/{run_id}").json()
    assert status["status"] == "proved"
    assert (cli.RUNS_DIR / status["result"]["folder"].split("/")[-1] / "proof.lean").exists()


def test_commands_reach_the_session_while_it_waits_for_an_answer(client, monkeypatch):
    def fake_search_that_asks(statement, rounds, on_event, session):
        on_event({"kind": "ask_user", "depth": 0, "question": "Any idea?"})
        answer = session.ask("Any idea?")
        on_event({"kind": "user_answer", "depth": 0, "text": answer})
        return SearchResult(False, message=f"hints were {session.hints()}")

    monkeypatch.setattr(srv.engine, "search", fake_search_that_asks)
    run_id = client.post("/api/sessions", json={"statement": "t", "hints": ["start hint"]}).json()["id"]
    assert client.post(f"/api/sessions/{run_id}/hint", json={"text": "try cases"}).json() == {"ok": True}
    with client.websocket_connect(f"/ws/{run_id}") as ws:
        assert ws.receive_json()["kind"] == "ask_user"
        assert client.get(f"/api/sessions/{run_id}").json()["question"] == "Any idea?"
        client.post(f"/api/sessions/{run_id}/answer", json={"text": "use induction"})
        assert ws.receive_json() == {"kind": "user_answer", "depth": 0, "text": "use induction"}
        final = ws.receive_json()
    assert final["kind"] == "finished" and final["status"] == "not_proved"
    assert final["result"]["message"] == "hints were ['start hint', 'try cases']"


def test_edit_checks_the_statement_then_starts_a_new_search(client, monkeypatch):
    monkeypatch.setattr(srv.engine, "search", fake_search_that_proves)
    monkeypatch.setattr(srv, "statement_errors", lambda s: ["bad"] if "oops" in s else [])
    run_id = client.post("/api/sessions", json={"statement": "t"}).json()["id"]
    assert client.post(f"/api/sessions/{run_id}/edit", json={"statement": "oops"}).status_code == 400
    new_id = client.post(f"/api/sessions/{run_id}/edit", json={"statement": "(n : ℕ) : n = n"}).json()["new_id"]
    assert new_id != run_id
    assert watch_until_finished(client, new_id)[-1]["status"] == "proved"


def test_unknown_session_and_unknown_action(client, monkeypatch):
    monkeypatch.setattr(srv.engine, "search", fake_search_that_proves)
    assert client.get("/api/sessions/nope").status_code == 404
    run_id = client.post("/api/sessions", json={"statement": "t"}).json()["id"]
    assert client.post(f"/api/sessions/{run_id}/dance", json={}).status_code == 400


def test_the_web_page_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "<title>Math Finder</title>" in page.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/api/sessions/nope").status_code == 404  # the API still wins over the static files


def test_the_chosen_metric_reaches_the_optimizer_and_a_better_proof_wins(client, monkeypatch):
    monkeypatch.setattr(srv.engine, "search", fake_search_that_proves)

    def fake_better(statement, code, plan, metric, on_event):
        on_event({"kind": "optimized", "depth": 0, "improved": True})
        before = {"steps": 5, "lemmas": 2, "heavy": 0, "cost": 7}
        after = {"steps": 1, "lemmas": 0, "heavy": 0, "cost": 1}
        return Optimization("SHORTER", Plan(f"optimized for {metric}", [], "simp"), metric, before, after, 4, True)

    monkeypatch.setattr(srv.optimizer, "optimize", fake_better)
    assert client.post("/api/sessions", json={"statement": "t", "metric": "fastest"}).status_code == 400
    run_id = client.post("/api/sessions", json={"statement": "t", "metric": "readable"}).json()["id"]
    result = watch_until_finished(client, run_id)[-1]["result"]
    assert result["lean_code"] == "SHORTER" and result["plan"] == "optimized for readable"
    assert result["optimization"]["before"]["cost"] == 7 and result["optimization"]["after"]["cost"] == 1
