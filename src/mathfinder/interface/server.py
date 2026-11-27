"""The web server. Start it with:  uv run mathfinder serve

The page works on a WORKSPACE: the theorem and the tree of lemmas you build with the assistant
(see proving/workspace.py).

  POST /api/formalize                          {latex}       → Lean statement, back-translation, judge
  POST /api/check                              {statement}   → Lean's errors for a statement ([] = fine)
  POST /api/workspaces                         {statement, latex} → a new workspace
  POST /api/workspaces/{id}/goals/{name}/{action}             → see GOAL_ACTIONS below
  POST /api/workspaces/{id}/stop                              → stop the running job
  POST /api/workspaces/{id}/finish                            → final Lean check, explanation, save
  POST /api/workspaces/{id}/ask                {question}    → the side assistant's answer
  WS   /ws/{id}                                               → the workspace now, then every change
  GET  /                                                      → the web page (the files in web/)

Slow jobs (proving, the search, the final check) run in a background thread and report
through the WebSocket. Everything else answers directly.
"""

import asyncio
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mathfinder import assistant, pipeline
from mathfinder.lean.checker import statement_errors
from mathfinder.llm import LLMError
from mathfinder.proving.workspace import Workspace
from mathfinder.translation import formalize as formalizer
from mathfinder.translation.explain import explain

app = FastAPI(title="Math Finder")


# ---------- request bodies (pydantic checks their shape) ----------


class FormalizeRequest(BaseModel):
    latex: str


class CheckRequest(BaseModel):
    statement: str


class NewWorkspace(BaseModel):
    statement: str  # the approved Lean statement
    latex: str = ""  # the theorem as you wrote it


class LemmaInPlan(BaseModel):
    name: str
    lean: str
    latex: str = ""


class GoalRequest(BaseModel):
    hints: list[str] = []  # propose
    sketch: str = ""  # plan
    main_proof: str = ""  # plan
    lemmas: list[LemmaInPlan] = []  # plan
    lean: str = ""  # statement
    on: bool = True  # assume


class Question(BaseModel):
    question: str


# ---------- one workspace and the browsers watching it ----------


class Live:
    def __init__(self, req: NewWorkspace):
        self.id = uuid.uuid4().hex[:8]
        self.latex = req.latex
        self.workspace = Workspace(req.statement, req.latex, on_event=self.broadcast)
        self.conversation: list[tuple[str, str]] = []  # the side assistant's questions and answers
        self.listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self.lock = threading.Lock()

    def broadcast(self, event: dict) -> None:
        """Send an event to every open page. Called from any thread."""
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
        """Final Lean check of the whole tree; if it passes, explain it and save it."""
        self.broadcast({"kind": "log", "text": "Final check of the whole proof…"})
        result = self.workspace.finish()
        outcome = {"proved": result["ok"], "lean_code": result["lean_code"], "explanation": "", "folder": ""}
        if not result["ok"]:
            outcome["message"] = "The final Lean check failed: " + " | ".join(result["errors"])[:600]
            self.broadcast({"kind": "finished", "result": outcome})
            return
        self.broadcast({"kind": "log", "text": "Writing the explanation…"})
        theorem = self.latex or self.workspace.goals["main"].lean
        try:
            outcome["explanation"] = explain(theorem, result["plan"], result["lean_code"])
        except LLMError as e:
            outcome["explanation"] = f"(could not write an explanation: {e})"
        folder = pipeline.new_run_dir()
        pipeline.save_proof(folder, theorem, result["lean_code"], outcome["explanation"])
        outcome["folder"] = f"runs/{folder.name}"
        self.broadcast({"kind": "finished", "result": outcome})


WORKSPACES: dict[str, Live] = {}


def get(workspace_id: str) -> Live:
    if workspace_id not in WORKSPACES:
        raise HTTPException(status_code=404, detail="no such workspace")
    return WORKSPACES[workspace_id]


# ---------- endpoints (plain `def`: FastAPI runs slow calls in a worker thread) ----------


@app.post("/api/formalize")
def formalize(req: FormalizeRequest) -> dict:
    f = formalizer.formalize(req.latex)
    return {**asdict(f), "ok": f.ok}


@app.post("/api/check")
def check(req: CheckRequest) -> dict:
    return {"errors": statement_errors(req.statement)}


@app.post("/api/workspaces")
def new_workspace(req: NewWorkspace) -> dict:
    live = Live(req)
    WORKSPACES[live.id] = live
    return {"id": live.id, "workspace": live.workspace.to_dict()}


GOAL_ACTIONS = {"propose", "plan", "clear-plan", "statement", "assume", "prove", "auto"}


@app.post("/api/workspaces/{workspace_id}/goals/{name}/{action}")
def goal_action(workspace_id: str, name: str, action: str, req: GoalRequest) -> dict:
    live = get(workspace_id)
    ws = live.workspace
    if name not in ws.goals:
        raise HTTPException(status_code=404, detail=f"no goal called {name!r}")
    if action not in GOAL_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    if action == "prove":
        live.in_background(lambda: ws.prove(name))
        return {"started": True}
    if action == "auto":
        live.in_background(lambda: ws.auto(name))
        return {"started": True}
    if ws.busy:
        raise HTTPException(status_code=409, detail="wait until the running job finishes, or stop it")
    try:
        if action == "propose":
            return {"options": ws.propose(name, req.hints)}
        if action == "plan":
            lemmas = [lemma.model_dump() for lemma in req.lemmas]
            return {"problems": ws.set_plan(name, req.sketch, req.main_proof, lemmas), "workspace": ws.to_dict()}
        if action == "clear-plan":
            ws.clear_plan(name)
        elif action == "statement":
            return {"errors": ws.edit_statement(name, req.lean), "workspace": ws.to_dict()}
        elif action == "assume":
            ws.assume(name, req.on)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"workspace": ws.to_dict()}


@app.post("/api/workspaces/{workspace_id}/stop")
def stop(workspace_id: str) -> dict:
    get(workspace_id).workspace.session.stop()
    return {"ok": True}


@app.post("/api/workspaces/{workspace_id}/finish")
def finish(workspace_id: str) -> dict:
    live = get(workspace_id)
    live.in_background(live.finish)
    return {"started": True}


@app.post("/api/workspaces/{workspace_id}/ask")
def ask(workspace_id: str, req: Question) -> dict:
    live = get(workspace_id)
    try:
        answer = assistant.ask(req.question, live.workspace.summary(), live.conversation)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"the assistant could not answer: {e}") from e
    live.conversation.append((req.question, answer))
    return {"answer": answer}


@app.websocket("/ws/{workspace_id}")
async def watch(websocket: WebSocket, workspace_id: str) -> None:
    await websocket.accept()
    if workspace_id not in WORKSPACES:
        await websocket.close(code=4404)
        return
    live = WORKSPACES[workspace_id]
    q = live.subscribe(asyncio.get_running_loop())
    try:
        await websocket.send_json({"kind": "snapshot", "workspace": live.workspace.to_dict()})
        while True:
            await websocket.send_json(await q.get())
    except WebSocketDisconnect:
        pass  # the browser tab closed; that's fine
    finally:
        live.unsubscribe(q)


# Everything that isn't an /api or /ws address is a file from web/ ("/" gives index.html).
# This must come LAST: a mount at "/" would otherwise catch the API addresses too.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
