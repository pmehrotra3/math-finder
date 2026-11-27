"""The web server. Start it with:  uv run mathfinder serve

Every theorem is a PROJECT (see projects.py), saved on disk. Inside a project you work in its
WORKSPACE: the theorem and the tree of lemmas you build with the assistant (proving/workspace.py).

  POST   /api/formalize                {latex}       → Lean statement, back-translation, judge
  POST   /api/check                    {statement}   → Lean's errors for a statement ([] = fine)

  GET    /api/projects                               → all projects, newest first
  POST   /api/projects                 {statement, latex} → a new project
  GET    /api/projects/{id}                          → one project: workspace, conversation, result
  DELETE /api/projects/{id}                          → delete it (not while others cite it)
  GET    /api/projects/{id}/library                  → established results this project may cite

  POST   /api/projects/{id}/goals/{name}/{action}    → see GOAL_ACTIONS below
  POST   /api/projects/{id}/stop                     → stop the running job
  POST   /api/projects/{id}/finish                   → final Lean check, explanation, save
  POST   /api/projects/{id}/ask        {question}    → the side assistant's answer
  WS     /ws/{id}                                    → the workspace now, then every change

Slow jobs (proving, the search, the final check) run in a background thread and report
through the WebSocket. Everything else answers directly.
"""

import asyncio
import threading
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mathfinder import assistant, pipeline
from mathfinder.lean.checker import statement_errors
from mathfinder.llm import LLMError
from mathfinder.projects import Project, ProjectStore
from mathfinder.proving.search import NAMESPACE
from mathfinder.proving.workspace import Workspace
from mathfinder.translation import formalize as formalizer
from mathfinder.translation.explain import explain

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


class Question(BaseModel):
    question: str


# ---------- an open project and the browsers watching it ----------


class Live:
    def __init__(self, project: Project):
        self.project = project
        self.workspace = Workspace(
            project.statement,
            project.latex,
            on_event=self.on_event,
            library_code=STORE.lean_code_for,
            goals=project.goals or None,
        )
        self.listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self.lock = threading.Lock()

    def save(self) -> None:
        self.project.goals = self.workspace.saved_goals()
        self.project.uses = self.workspace.projects_used()
        STORE.save(self.project)

    def view(self) -> dict:
        """Everything the page needs to show this project."""
        p = self.project
        return {
            "id": p.id,
            "title": p.title,
            "latex": p.latex,
            "established": p.established,
            "workspace": self.workspace.to_dict(),
            "conversation": p.conversation,
            "result": p.result,
        }

    def on_event(self, event: dict) -> None:
        """Called by the workspace (from any thread) for every change: save it, then tell the pages."""
        if "workspace" in event:
            self.save()
        self.broadcast(event)

    def broadcast(self, event: dict) -> None:
        with self.lock:
            listeners = list(self.listeners)
        for loop, q in listeners:
            # asyncio queues aren't thread-safe: ask each page's event loop to do the put itself
            loop.call_soon_threadsafe(q.put_nowait, event)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        q = asyncio.Queue()
        with self.lock:
            self.listeners.append((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self.lock:
            self.listeners = [(loop, x) for loop, x in self.listeners if x is not q]

    def in_background(self, job) -> None:
        """Run a slow job in its own thread; refuse if one is already running."""
        if self.workspace.busy:
            raise HTTPException(status_code=409, detail="something is already running; stop it or wait")

        def work() -> None:
            try:
                self.workspace.run(job)
            except Exception as e:  # never leave the page waiting without saying why
                self.broadcast({"kind": "error", "text": str(e)})

        threading.Thread(target=work, daemon=True).start()

    def finish(self) -> None:
        """Final Lean check of the whole tree. If it passes, the project becomes established:
        it is explained, saved, and other projects may cite its theorem from now on."""
        self.broadcast({"kind": "log", "text": "Final check of the whole proof…"})
        result = self.workspace.finish()
        outcome = {"proved": result["ok"], "lean_code": result["lean_code"], "explanation": "", "folder": ""}
        if not result["ok"]:
            outcome["message"] = "The final Lean check failed: " + " | ".join(result["errors"])[:600]
            self.project.result = outcome
            self.save()
            self.broadcast({"kind": "finished", "result": outcome})
            return
        self.broadcast({"kind": "log", "text": "Writing the explanation…"})
        theorem = self.project.latex or self.project.statement
        try:
            outcome["explanation"] = explain(theorem, result["plan"], result["lean_code"])
        except LLMError as e:
            outcome["explanation"] = f"(could not write an explanation: {e})"
        folder = pipeline.new_run_dir()
        pipeline.save_proof(folder, theorem, result["lean_code"], outcome["explanation"])
        outcome["folder"] = f"runs/{folder.name}"
        self.project.result = outcome
        self.project.established_code = in_namespace(result["own_code"], self.project.namespace)
        self.save()
        self.broadcast({"kind": "finished", "result": outcome, "established": True})


def in_namespace(code: str, namespace: str) -> str:
    """Move a verified proof from the working namespace into the project's own namespace."""
    head, end = f"namespace {NAMESPACE}\n", f"end {NAMESPACE}\n"
    assert code.startswith(head) and code.endswith(end), "unexpected shape of an assembled proof"
    return f"namespace {namespace}\n" + code[len(head) : -len(end)] + f"end {namespace}\n"


OPEN: dict[str, Live] = {}  # projects someone opened since the server started


def live(project_id: str) -> Live:
    if project_id not in STORE.projects:
        raise HTTPException(status_code=404, detail="no such project")
    if project_id not in OPEN:
        OPEN[project_id] = Live(STORE.projects[project_id])
    return OPEN[project_id]


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
    project = STORE.create(req.statement, req.latex)
    opened = live(project.id)
    opened.save()
    return opened.view()


@app.get("/api/projects/{project_id}")
def open_project(project_id: str) -> dict:
    return live(project_id).view()


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


GOAL_ACTIONS = {"propose", "plan", "clear-plan", "statement", "assume", "use-established", "prove", "auto"}


@app.post("/api/projects/{project_id}/goals/{name}/{action}")
def goal_action(project_id: str, name: str, action: str, req: GoalRequest) -> dict:
    opened = live(project_id)
    ws = opened.workspace
    if name not in ws.goals:
        raise HTTPException(status_code=404, detail=f"no goal called {name!r}")
    if action not in GOAL_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    if action == "prove":
        opened.in_background(lambda: ws.prove(name))
        return {"started": True}
    if action == "auto":
        opened.in_background(lambda: ws.auto(name))
        return {"started": True}
    if ws.busy:
        raise HTTPException(status_code=409, detail="wait until the running job finishes, or stop it")
    try:
        if action == "propose":
            return {"options": ws.propose(name, req.hints)}
        if action == "plan":
            lemmas = [lemma.model_dump() for lemma in req.lemmas]
            check_citations(project_id, [lemma["established_by"] for lemma in lemmas])
            problems = ws.set_plan(name, req.sketch, req.main_proof, lemmas)
            opened.save()
            return {"problems": problems, "workspace": ws.to_dict()}
        if action == "clear-plan":
            ws.clear_plan(name)
        elif action == "statement":
            return {"errors": ws.edit_statement(name, req.lean), "workspace": ws.to_dict()}
        elif action == "assume":
            ws.assume(name, req.on)
        elif action == "use-established":
            check_citations(project_id, [req.project])
            ws.use_established(name, req.project)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    opened.save()
    return {"workspace": ws.to_dict()}


def check_citations(project_id: str, cited: list[str]) -> None:
    """A project may cite only established projects, never itself, never one that cites it."""
    allowed = {result["id"] for result in STORE.library(excluding=project_id)}
    for pid in cited:
        if pid and pid not in allowed:
            raise ValueError(f"project {pid} is not an established result this project can use")


@app.post("/api/projects/{project_id}/stop")
def stop(project_id: str) -> dict:
    live(project_id).workspace.session.stop()
    return {"ok": True}


@app.post("/api/projects/{project_id}/finish")
def finish(project_id: str) -> dict:
    opened = live(project_id)
    opened.in_background(opened.finish)
    return {"started": True}


@app.post("/api/projects/{project_id}/ask")
def ask(project_id: str, req: Question) -> dict:
    opened = live(project_id)
    history = [tuple(turn) for turn in opened.project.conversation]
    try:
        answer = assistant.ask(req.question, opened.workspace.summary(), history)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"the assistant could not answer: {e}") from e
    opened.project.conversation.append([req.question, answer])
    opened.save()
    return {"answer": answer}


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
