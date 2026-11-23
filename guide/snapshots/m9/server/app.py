"""The web server: the search engine, reachable from a browser.

  POST /api/formalize            {latex}             → Lean statement + back-translation + judge
  POST /api/check                {statement}         → Lean errors for a statement you edited ([] = fine)
  POST /api/sessions             {statement, ...}    → starts a search in the background, returns its id
  GET  /api/sessions/{id}                            → status, open question, result
  POST /api/sessions/{id}/{action}  {text|name|statement}
        action = hint | lemma | reject | pin | pause | resume | stop | answer | edit
  WS   /ws/{id}                                      → every event of that search, live

Each search runs in its own thread (it's slow, blocking work: Lean and the model).
The web server itself is asynchronous: one thread juggles many connections.
The Run class is the bridge between the two worlds.
"""

import asyncio
import threading
import uuid
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import mathfinder.explain as explainer
import mathfinder.formalize as formalizer
import mathfinder.search as engine
from mathfinder import cli
from mathfinder.collab import Session
from mathfinder.lean import statement_errors
from mathfinder.llm import LLMError
from mathfinder.traces import TraceLogger

app = FastAPI(title="Math Finder")


# ---------- request bodies (pydantic checks their shape for us) ----------


class FormalizeRequest(BaseModel):
    latex: str


class CheckRequest(BaseModel):
    statement: str


class StartRequest(BaseModel):
    statement: str  # the approved Lean statement
    latex: str = ""  # the original LaTeX, used for the explanation
    hints: list[str] = []
    rounds: int = 3


class ActionRequest(BaseModel):
    text: str = ""  # hint / pin / answer text
    name: str = ""  # lemma name (lemma, reject)
    statement: str = ""  # lemma statement (lemma) or new theorem statement (edit)


# ---------- one running search ----------


class Run:
    """A search running in a background thread, plus everyone watching it."""

    def __init__(self, statement: str, latex: str, hints: list[str], rounds: int):
        self.id = uuid.uuid4().hex[:8]
        self.statement, self.latex, self.hints, self.rounds = statement, latex, hints, rounds
        self.session = Session(statement)
        for h in hints:
            self.session.add_hint(h)
        self.status = "running"  # running | proved | not_proved | stopped
        self.result = None
        self.events = []  # full history, so a page that connects late can catch up
        self.listeners = []  # (event loop, asyncio.Queue) for each open WebSocket
        self.lock = threading.Lock()
        self.folder = cli.new_run_dir()
        self.logger = TraceLogger(self.folder, latex or statement)

    def on_event(self, event: dict) -> None:
        """Called from the SEARCH thread for every event."""
        self.logger(event)
        with self.lock:
            self.events.append(event)
            listeners = list(self.listeners)
        for loop, q in listeners:
            # asyncio queues aren't thread-safe: ask each WebSocket's event loop to do the put itself
            loop.call_soon_threadsafe(q.put_nowait, event)

    def subscribe(self, loop) -> tuple[list[dict], asyncio.Queue]:
        q = asyncio.Queue()
        with self.lock:  # history + registration together, so no event is missed or sent twice
            self.listeners.append((loop, q))
            return list(self.events), q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self.lock:
            self.listeners = [(l, x) for l, x in self.listeners if x is not q]

    def start(self) -> None:
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        try:
            result = engine.search(self.statement, rounds=self.rounds, on_event=self.on_event, session=self.session)
        except Exception as e:  # never let a crash leave the page waiting forever
            self.status, self.result = "not_proved", {"proved": False, "message": f"internal error: {e}"}
            self.on_event({"kind": "finished", "status": self.status, "result": self.result})
            return
        self.logger.finish(result.proved, self.session.hints())
        explanation = ""
        if result.proved:
            self.on_event({"kind": "explaining"})
            try:
                explanation = explainer.explain(self.latex or self.statement, result.plan, result.lean_code)
            except LLMError as e:
                explanation = f"(could not write an explanation: {e})"
            cli.save_run(self.folder, self.latex or self.statement, result.lean_code, explanation)
        stop = self.session.checkpoint()
        self.status = "proved" if result.proved else ("stopped" if stop else "not_proved")
        self.result = {
            "proved": result.proved,
            "message": result.message,
            "lean_code": result.lean_code,
            "explanation": explanation,
            "plan": result.plan.sketch if result.plan else "",
            "rounds": result.rounds,
            "folder": str(self.folder),
        }
        self.on_event({"kind": "finished", "status": self.status, "result": self.result})


RUNS: dict[str, Run] = {}


def get_run(run_id: str) -> Run:
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="no such session")
    return RUNS[run_id]


# ---------- endpoints ----------


@app.post("/api/formalize")
def formalize(req: FormalizeRequest):
    # A plain `def` (not `async def`): FastAPI runs it in a worker thread, so this slow call
    # (model + Lean) doesn't freeze the server for everyone else.
    f = formalizer.formalize(req.latex)
    return {**asdict(f), "ok": f.ok}


@app.post("/api/check")
def check(req: CheckRequest):
    return {"errors": statement_errors(req.statement)}


@app.post("/api/sessions")
def start_session(req: StartRequest):
    run = Run(req.statement, req.latex, req.hints, req.rounds)
    RUNS[run.id] = run
    run.start()
    return {"id": run.id}


@app.get("/api/sessions/{run_id}")
def session_status(run_id: str):
    run = get_run(run_id)
    return {
        "id": run.id,
        "statement": run.statement,
        "status": run.status,
        "question": run.session.question,
        "hints": run.session.hints(),
        "events": len(run.events),
        "result": run.result,
    }


@app.post("/api/sessions/{run_id}/{action}")
def session_action(run_id: str, action: str, req: ActionRequest):
    run = get_run(run_id)
    s = run.session
    if action == "hint":
        s.add_hint(req.text)
    elif action == "lemma":
        s.add_lemma(req.name, req.statement, req.text)
    elif action == "reject":
        s.reject_lemma(req.name or req.statement)
    elif action == "pin":
        s.pin(req.text)
    elif action == "pause":
        s.pause()
    elif action == "resume":
        s.resume()
    elif action == "stop":
        s.stop()
    elif action == "answer":
        s.answer(req.text)
    elif action == "edit":  # stop this search and start a fresh one on the new statement
        errors = statement_errors(req.statement)
        if errors:
            raise HTTPException(status_code=400, detail=f"Lean rejected the statement: {errors[0][:300]}")
        s.edit_statement(req.statement)
        new = Run(req.statement, "", s.hints(), run.rounds)
        RUNS[new.id] = new
        new.start()
        return {"ok": True, "new_id": new.id}
    else:
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    return {"ok": True}


@app.websocket("/ws/{run_id}")
async def watch(websocket: WebSocket, run_id: str):
    await websocket.accept()
    if run_id not in RUNS:
        await websocket.close(code=4404)
        return
    run = RUNS[run_id]
    history, q = run.subscribe(asyncio.get_running_loop())
    try:
        for event in history:
            await websocket.send_json(event)
            if event["kind"] == "finished":
                await websocket.close()  # nothing more will happen
                return
        while True:
            event = await q.get()  # `await` = pause here until an event arrives, without blocking the server
            await websocket.send_json(event)
            if event["kind"] == "finished":
                await websocket.close()
                return
    except WebSocketDisconnect:
        pass  # the browser tab closed; that's fine
    finally:
        run.unsubscribe(q)
