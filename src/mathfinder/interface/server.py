"""The web server. Start it with:  uv run mathfinder serve

Every theorem is a PROJECT (see projects.py), saved on disk. Inside a project you work in its
WORKSPACE: the theorem and the tree of lemmas you build with the assistant (proving/workspace.py).

  POST   /api/formalize                {latex}       → Lean statement, back-translation, judge
  POST   /api/check                    {statement}   → Lean's errors for a statement ([] = fine)

  GET    /api/projects                               → all projects, newest first
  POST   /api/projects                 {statement, latex} → a new project
  GET    /api/projects/{id}                          → one project: workspace, conversation, result
  DELETE /api/projects/{id}                          → delete it (not while others cite it)
  PATCH  /api/projects/{id}             {name}        → rename it
  POST   /api/projects/{id}/suggest-name             → a suggested name, if it has none
  GET    /api/projects/{id}/library                  → the other projects it may use as lemmas

  POST   /api/projects/{id}/goals/{name}/{action}    → see GOAL_ACTIONS below
  POST   /api/projects/{id}/stop                     → stop the running job and the assistant
  POST   /api/projects/{id}/finish                   → final Lean check, explanation, save
  POST   /api/projects/{id}/chat       {message, looking_at} → your message; the assistant answers over the WebSocket
  POST   /api/projects/{id}/notes      {notes}       → replace your hints for the prover
  WS     /ws/{id}                                    → the workspace now, then every change

Each project is its own space: a chat on one side, the proof on the other. An open project is
a Live object (live.py): it saves every change, runs the slow jobs and the assistant in
background threads, and sends what happens to the open pages through the WebSocket.
"""

import asyncio
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mathfinder.interface.live import Live
from mathfinder.lean.checker import statement_errors
from mathfinder.projects import ProjectStore
from mathfinder.proving.workspace import Busy
from mathfinder.translation import formalize as formalizer

app = FastAPI(title="Math Finder")
STORE = ProjectStore()


# ---------- request bodies (pydantic checks their shape) ----------


class FormalizeRequest(BaseModel):
    latex: str


class CheckRequest(BaseModel):
    statement: str


class NewProject(BaseModel):
    statement: str  # the approved Lean statement
    latex: str = ""  # the theorem as you wrote it
    name: str = ""  # a short name for the project


class ProjectChange(BaseModel):
    name: str


class LemmaInPlan(BaseModel):
    name: str
    lean: str
    latex: str = ""
    established_by: str = ""  # id of an established project this lemma cites


class GoalRequest(BaseModel):
    hints: list[str] = []  # propose
    sketch: str = ""  # plan
    main_proof: str = ""  # plan
    lemmas: list[LemmaInPlan] = []  # plan
    lean: str = ""  # statement
    on: bool = True  # assume
    project: str = ""  # use-established
    new_name: str = ""  # rename


class ChatMessage(BaseModel):
    message: str
    looking_at: str = "main"  # the goal open in the proof panel, so "this lemma" makes sense


OPEN: dict[str, Live] = {}  # projects someone opened since the server started


def live(project_id: str) -> Live:
    if project_id not in STORE.projects:
        raise HTTPException(status_code=404, detail="no such project")
    if project_id not in OPEN:
        OPEN[project_id] = Live(STORE.projects[project_id], STORE)
    return OPEN[project_id]


def in_background(opened: Live, job) -> dict:
    try:
        opened.in_background(job)
    except Busy as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"started": True}


# ---------- endpoints (plain `def`: FastAPI runs slow calls in a worker thread) ----------


@app.post("/api/formalize")
def formalize(req: FormalizeRequest) -> dict:
    f = formalizer.formalize(req.latex)
    return {**asdict(f), "ok": f.ok}


@app.post("/api/check")
def check(req: CheckRequest) -> dict:
    return {"errors": statement_errors(req.statement)}


@app.get("/api/projects")
def list_projects() -> dict:
    return {"projects": STORE.summaries()}


@app.post("/api/projects")
def new_project(req: NewProject) -> dict:
    project = STORE.create(req.statement, req.latex, req.name)
    opened = live(project.id)
    opened.save()
    return opened.view()


@app.get("/api/projects/{project_id}")
def open_project(project_id: str) -> dict:
    return live(project_id).view()


@app.patch("/api/projects/{project_id}")
def change_project(project_id: str, req: ProjectChange) -> dict:
    """Rename a project."""
    opened = live(project_id)
    opened.project.name = req.name.strip()[:80]
    opened.save()
    return {"name": opened.project.name or opened.project.title}


@app.post("/api/projects/{project_id}/suggest-name")
def suggest_name(project_id: str) -> dict:
    """Give a project without a name a suggested one (projects made before names existed)."""
    opened = live(project_id)
    if not opened.project.name:
        opened.project.name = formalizer.suggest_name(opened.project.latex or opened.project.statement)
        opened.save()
    return {"name": opened.project.name or opened.project.title}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    opened = live(project_id)
    if opened.workspace.busy:
        raise HTTPException(status_code=409, detail="stop the running job first")
    try:
        STORE.delete(project_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    OPEN.pop(project_id, None)
    return {"ok": True}


@app.get("/api/projects/{project_id}/library")
def library(project_id: str) -> dict:
    live(project_id)
    return {"results": STORE.library(excluding=project_id)}


GOAL_ACTIONS = {"propose", "plan", "clear-plan", "statement", "rename", "assume", "use-established", "prove", "auto"}


@app.post("/api/projects/{project_id}/goals/{name}/{action}")
def goal_action(project_id: str, name: str, action: str, req: GoalRequest) -> dict:
    opened = live(project_id)
    ws = opened.workspace
    if name not in ws.goals:
        raise HTTPException(status_code=404, detail=f"no goal called {name!r}")
    if action not in GOAL_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    if action == "prove":
        return in_background(opened, lambda: ws.prove(name))
    if action == "auto":
        return in_background(opened, lambda: ws.auto(name))
    if ws.busy:
        raise HTTPException(status_code=409, detail="wait until the running job finishes, or stop it")
    try:
        if action == "propose":
            return {"options": ws.propose(name, req.hints)}
        if action == "plan":
            lemmas = [lemma.model_dump() for lemma in req.lemmas]
            opened.check_citations([lemma["established_by"] for lemma in lemmas])
            problems = ws.set_plan(name, req.sketch, req.main_proof, lemmas)
            opened.save()
            return {"problems": problems, "workspace": ws.to_dict()}
        if action == "clear-plan":
            ws.clear_plan(name)
        elif action == "statement":
            return {"errors": ws.edit_statement(name, req.lean), "workspace": ws.to_dict()}
        elif action == "rename":
            ws.rename(name, req.new_name)
        elif action == "assume":
            ws.assume(name, req.on)
        elif action == "use-established":
            opened.check_citations([req.project])
            ws.use_established(name, req.project)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    opened.save()
    return {"workspace": ws.to_dict()}


@app.post("/api/projects/{project_id}/stop")
def stop(project_id: str) -> dict:
    live(project_id).stop()
    return {"ok": True}


@app.post("/api/projects/{project_id}/finish")
def finish(project_id: str) -> dict:
    opened = live(project_id)
    return in_background(opened, opened.finish)


@app.post("/api/projects/{project_id}/chat")
def chat(project_id: str, req: ChatMessage) -> dict:
    return {"entry": live(project_id).chat(req.message, req.looking_at)}


class Notes(BaseModel):
    notes: list[str]


@app.post("/api/projects/{project_id}/notes")
def set_notes(project_id: str, req: Notes) -> dict:
    """Replace your hints (the chat adds them; the page can clear them)."""
    opened = live(project_id)
    opened.workspace.notes = [n.strip() for n in req.notes if n.strip()]
    opened.save()
    return {"notes": opened.workspace.notes}


@app.websocket("/ws/{project_id}")
async def watch(websocket: WebSocket, project_id: str) -> None:
    await websocket.accept()
    if project_id not in STORE.projects:
        await websocket.close(code=4404)
        return
    opened = live(project_id)
    q = opened.subscribe(asyncio.get_running_loop())
    try:
        await websocket.send_json({"kind": "snapshot", "workspace": opened.workspace.to_dict()})
        while True:
            await websocket.send_json(await q.get())
    except WebSocketDisconnect:
        pass  # the browser tab closed; that's fine
    finally:
        opened.unsubscribe(q)


# Everything that isn't an /api or /ws address is a file from web/ ("/" gives index.html).
# This must come LAST: a mount at "/" would otherwise catch the API addresses too.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
