"""Tests for pipeline.py and interface/server.py. Lean and the model are faked."""

import json

import pytest
from fastapi.testclient import TestClient

from mathfinder import assistant, pipeline
from mathfinder.assistant import Reply, parse_reply
from mathfinder.interface import live, server
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
    monkeypatch.setattr(live, "explain", lambda theorem, plan, code: "## Proof\nexplained")
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
    assert {r["id"] for r in store.library(excluding=a.id)} == {c.id}  # b uses a, so a can't use b
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


def test_an_unfinished_project_counts_as_assumed_until_it_is_established(client):
    unfinished = new_project(client, ": A")
    project_id = new_project(client)
    act(client, project_id, "main", "plan", sketch="", main_proof="simp", lemmas=[{"name": "good_a", "lean": ": A"}])
    goals = act(client, project_id, "good_a", "use-established", project=unfinished).json()["workspace"]["goals"]
    assert goals["good_a"]["status"] == "assumed" and goals["good_a"]["established_by"] == unfinished
    server.STORE.projects[unfinished].established_code = "namespace X\nend X\n"  # it gets established later
    with client.websocket_connect(f"/ws/{project_id}") as ws:
        ws.receive_json()
        act(client, project_id, "good_a", "prove")
        while ws.receive_json()["kind"] != "idle":
            pass
    assert client.get(f"/api/projects/{project_id}").json()["workspace"]["goals"]["good_a"]["status"] == "proved"


def test_a_project_cannot_use_one_that_uses_it(client):
    first = new_project(client, ": A")
    second = new_project(client)
    act(client, second, "main", "use-established", project=first)
    answer = act(client, first, "main", "plan", sketch="", main_proof="simp",
                 lemmas=[{"name": "loop", "lean": ": T", "established_by": second}])  # fmt: skip
    assert answer.status_code == 400 and "can use" in answer.json()["detail"]


def test_projects_have_names_you_can_change(client):
    project_id = client.post("/api/projects", json={"statement": ": T", "name": "Odd sums"}).json()["id"]
    assert client.get("/api/projects").json()["projects"][0]["name"] == "Odd sums"
    assert client.patch(f"/api/projects/{project_id}", json={"name": "Sum of odd numbers"}).status_code == 200
    assert client.get(f"/api/projects/{project_id}").json()["name"] == "Sum of odd numbers"


def test_a_lemma_can_be_renamed_and_its_uses_follow(client):
    project_id = new_project(client)
    act(client, project_id, "main", "plan", sketch="", main_proof="exact step_1",
        lemmas=[{"name": "step_1", "lean": ": A"}])  # fmt: skip
    goals = act(client, project_id, "step_1", "rename", new_name="key_bound").json()["workspace"]["goals"]
    assert goals["main"]["children"] == ["key_bound"] and goals["main"]["proof"] == "exact key_bound"
    assert act(client, project_id, "key_bound", "rename", new_name="main").status_code == 400


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


def chat(client, project_id, message, looking_at="main"):
    """Send a message and wait until the assistant has taken all its steps."""
    entry = client.post(f"/api/projects/{project_id}/chat", json={"message": message, "looking_at": looking_at})
    assert entry.status_code == 200
    opened = server.live(project_id)
    assert live.wait_until(lambda: not opened.agent_running and not opened.workspace.busy, 10)
    return client.get(f"/api/projects/{project_id}").json()


def scripted(monkeypatch, *replies):
    """The assistant takes these steps in order; returns what it was shown at each step."""
    seen, queue = [], list(replies)

    def fake_reply(situation, history, step, fresh):
        seen.append({"situation": situation, "history": list(history), "step": step, "fresh": fresh})
        return queue.pop(0)

    monkeypatch.setattr(assistant, "reply", fake_reply)
    return seen


def lines(project):
    return [(e["role"], e["text"]) for e in project["conversation"]]


def test_a_question_gets_one_answer_with_the_proof_in_view(client, monkeypatch):
    seen = scripted(monkeypatch, Reply("Because squares are $\\ge 0$."), Reply("Then add them."))
    project_id = new_project(client)
    project = chat(client, project_id, "why?")
    assert lines(project) == [("you", "why?"), ("assistant", "Because squares are $\\ge 0$.")]
    chat(client, project_id, "and then?", looking_at="main")
    assert "- main [open]: : T" in seen[0]["situation"] and "Nothing is running." in seen[0]["situation"]
    assert [e["text"] for e in seen[1]["history"]] == ["why?", "Because squares are $\\ge 0$.", "and then?"]
    assert seen[1]["step"] == 1 and seen[1]["fresh"]


def test_the_assistant_changes_the_plan_adds_a_hint_and_assumes(client, monkeypatch):
    plan = {"do": "plan", "goal": "main", "sketch": "split", "main_proof": "exact good_a",
            "lemmas": [{"name": "good_a", "lean": ": A"}]}  # fmt: skip
    actions = [plan, {"do": "note", "text": "use induction"}, {"do": "assume", "goal": "good_a", "on": True}]
    scripted(monkeypatch, Reply("Done.", actions))
    project = chat(client, new_project(client), "split it and assume the lemma")
    assert [a["ok"] for a in project["conversation"][-1]["actions"]] == [True, True, True]
    goals = project["workspace"]["goals"]
    assert goals["main"]["children"] == ["good_a"] and goals["good_a"]["status"] == "assumed"
    assert project["notes"] == ["use induction"]


def test_the_assistant_waits_for_its_job_then_sees_the_result_and_reports(client, monkeypatch):
    seen = scripted(
        monkeypatch,
        Reply("Proving it.", [{"do": "prove", "goal": "main"}], done=True),
        Reply("Both proved; you can run the final check."),
    )
    project_id = new_project(client)
    with_one_lemma_plan(client, project_id, "good_a")
    project = chat(client, project_id, "go")
    assert lines(project) == [
        ("you", "go"),
        ("assistant", "Proving it."),
        ("event", "good_a: proved"),
        ("event", "main: proved"),
        ("assistant", "Both proved; you can run the final check."),
    ]
    assert seen[1]["step"] == 2 and not seen[1]["fresh"] and "main [proved]" in seen[1]["situation"]
    assert "[prove] ok: started proving main" in assistant.build_prompt("", seen[1]["history"], 2, False)


def test_the_assistant_keeps_going_until_it_is_done(client, monkeypatch):
    scripted(
        monkeypatch,
        Reply("Trying a proof.", [{"do": "assume", "goal": "main", "on": False}], done=False),
        Reply("Done now."),
    )
    project = chat(client, new_project(client), "work on it")
    assert [text for role, text in lines(project) if role == "assistant"] == ["Trying a proof.", "Done now."]


def test_the_assistant_pauses_after_too_many_steps(client, monkeypatch):
    monkeypatch.setattr(live, "MAX_STEPS", 2)
    step = Reply("again", [{"do": "clear_notes"}], done=False)
    scripted(monkeypatch, step, step, step)
    project = chat(client, new_project(client), "loop")
    assert project["conversation"][-1]["text"].startswith("Paused after 2 steps")


def test_a_written_proof_is_checked_by_lean_at_once(client, monkeypatch, fake):
    scripted(monkeypatch, Reply("Let me try.", [{"do": "try_proof", "goal": "main", "proof": "simp"}]))
    project = chat(client, new_project(client), "prove it with simp")
    assert lines(project)[1:] == [("assistant", "Let me try."), ("event", "main: Lean accepted the proof")]
    assert project["conversation"][1]["actions"][0] == {
        "do": "try_proof", "ok": True, "text": "Lean accepted the proof of main", "lean": "simp"
    }  # fmt: skip
    main = project["workspace"]["goals"]["main"]
    assert (main["status"], main["proof"], main["attempts"][-1]["source"]) == ("proved", "simp", "written")


def test_a_message_while_it_works_is_read_at_the_next_step(client, monkeypatch):
    project_id = new_project(client)
    opened = server.live(project_id)
    seen = []

    def fake_reply(situation, history, step, fresh):
        seen.append((step, fresh, [e["text"] for e in history if e["role"] == "you"]))
        if step == 1:
            client.post(f"/api/projects/{project_id}/chat", json={"message": "also use induction"})
            return Reply("Working.", [], done=False)
        return Reply("Noted.")

    monkeypatch.setattr(assistant, "reply", fake_reply)
    chat(client, project_id, "start")
    assert seen == [(1, True, ["start"]), (2, True, ["start", "also use induction"])]
    assert not opened.agent_running


def test_stop_ends_the_assistant_and_the_job(client, monkeypatch):
    project_id = new_project(client)
    opened = server.live(project_id)

    def fake_reply(situation, history, step, fresh):
        client.post(f"/api/projects/{project_id}/stop")  # you press Stop while it is thinking
        return Reply("I would prove it now.", [{"do": "prove", "goal": "main"}])

    monkeypatch.setattr(assistant, "reply", fake_reply)
    project = chat(client, project_id, "prove it")
    assert lines(project)[-1] == ("event", "Stopped.")  # its reply's actions are not carried out
    assert project["workspace"]["goals"]["main"]["status"] == "open" and opened.workspace.session.stopped


def test_the_stop_action_waits_for_the_job_and_later_actions_wait_too(client, monkeypatch):
    monkeypatch.setattr(live, "STOP_WAIT", 0.3)
    scripted(monkeypatch, Reply("Stopping.", [{"do": "stop"}, {"do": "assume", "goal": "main"}]))
    project_id = new_project(client)
    workspace = server.live(project_id).workspace
    workspace._job.acquire()  # a job that doesn't end
    try:
        client.post(f"/api/projects/{project_id}/chat", json={"message": "stop and assume"})
        opened = server.live(project_id)
        assert live.wait_until(lambda: not opened.agent_running, 10)
    finally:
        workspace._job.release()
    done = client.get(f"/api/projects/{project_id}").json()["conversation"][-1]["actions"]
    assert done[0]["text"].startswith("asked to stop") and not done[1]["ok"]


def test_a_bad_action_is_reported_not_crashed(client, monkeypatch):
    scripted(monkeypatch, Reply("Hm.", [{"do": "prove", "goal": "nope"}, {"do": "statement", "goal": "main"}]))
    done = chat(client, new_project(client), "do it")["conversation"][-1]["actions"]
    assert [a["ok"] for a in done] == [False, False]
    assert "no goal called nope" in done[0]["text"]


def test_the_assistant_failing_is_said_in_the_chat(client, monkeypatch):
    def broken(*args):
        raise LLMError("model unavailable")

    monkeypatch.setattr(assistant, "reply", broken)
    last = chat(client, new_project(client), "why?")["conversation"][-1]
    assert last["role"] == "assistant" and "model unavailable" in last["text"]


def test_the_assistant_keeps_only_known_actions():
    data = {"reply": " ok ", "actions": [{"do": "prove", "goal": "main"}, {"do": "rm -rf"}, "junk"]}
    assert parse_reply(data) == Reply("ok", [{"do": "prove", "goal": "main"}])


def test_old_conversations_are_read_as_chat_lines(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(": T")
    project.conversation = [["why?", "because"]]
    store.save(project)
    lines = ProjectStore(tmp_path).projects[project.id].conversation
    assert lines == [{"role": "you", "text": "why?"}, {"role": "assistant", "text": "because"}]


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
    assert "<title>Math Finder</title>" in page and 'id="project-list"' in page
    assert client.get("/app.js").status_code == 200


def test_hints_can_be_cleared_from_the_page(client):
    project_id = new_project(client)
    server.live(project_id).workspace.add_note("use induction")
    assert client.post(f"/api/projects/{project_id}/notes", json={"notes": []}).json() == {"notes": []}


def test_a_project_without_a_name_gets_one_suggested(client, monkeypatch):
    monkeypatch.setattr(server.formalizer, "suggest_name", lambda theorem: f"name for {theorem}")
    project_id = new_project(client)
    assert client.post(f"/api/projects/{project_id}/suggest-name").json() == {"name": "name for $T$"}
    monkeypatch.setattr(server.formalizer, "suggest_name", lambda theorem: "should not be asked again")
    assert client.post(f"/api/projects/{project_id}/suggest-name").json() == {"name": "name for $T$"}
