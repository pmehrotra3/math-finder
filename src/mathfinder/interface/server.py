"""The web server. Run it with:  uv run mathfinder serve

  POST /api/formalize               {latex}           → Lean statement + back-translation + judge
  POST /api/check                   {statement}       → Lean errors for an edited statement ([] = fine)
  POST /api/sessions                {statement, ...}  → starts a search in the background, returns its id
  POST /api/sessions/{id}/{action}  {text|name|statement}, see ACTIONS
  WS   /ws/{id}                                       → every event of that search, live
  GET  /                                              → the web page (the files in web/)

Each search runs in its own thread (it's slow, blocking work: Lean and the model) and
the Run class passes its events to the asynchronous WebSockets.
"""

import asyncio
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mathfinder import pipeline
from mathfinder.lean.checker import statement_errors
from mathfinder.proving.optimize import METRICS
from mathfinder.proving.session import Session
from mathfinder.translation import formalize as formalizer

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
    metric: str = "short"  # what "better proof" means: short | readable | few_lemmas


class ActionRequest(BaseModel):
    text: str = ""  # hint / lemma / pin / answer text
    name: str = ""  # lemma name (lemma, reject)
    statement: str = ""  # lemma statement (lemma)


# ---------- one running search ----------


class Run:
    """A search running in a background thread, plus everyone watching it."""

    def __init__(self, req: StartRequest):
        self.id = uuid.uuid4().hex[:8]
        self.req = req
        self.session = Session()
        for hint in req.hints:
            self.session.add_hint(hint)
        self.events = []  # full history, so a page that connects late can catch up
        self.listeners = []  # (event loop, asyncio.Queue) for each open WebSocket
        self.lock = threading.Lock()
        threading.Thread(target=self._work, daemon=True).start()

    def on_event(self, event: dict) -> None:
        """Called from the SEARCH thread for every event."""
        with self.lock:
            self.events.append(event)
            listeners = list(self.listeners)
        for loop, q in listeners:
            # asyncio queues aren't thread-safe: ask each WebSocket's event loop to do the put itself
            loop.call_soon_threadsafe(q.put_nowait, event)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> tuple[list[dict], asyncio.Queue]:
        q = asyncio.Queue()
        with self.lock:  # history + registration together, so no event is missed or sent twice
            self.listeners.append((loop, q))
            return list(self.events), q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self.lock:
            self.listeners = [(cb, x) for cb, x in self.listeners if x is not q]

    def _work(self) -> None:
        req = self.req
        try:
            result = pipeline.prove(
                req.statement,
                req.latex,
                rounds=req.rounds,
                metric=req.metric,
                on_event=self.on_event,
                session=self.session,
            )
        except Exception as e:  # never let a crash leave the page waiting forever
            result = {"proved": False, "message": f"internal error: {e}"}
        status = "proved" if result["proved"] else "stopped" if self.session.stopped else "not_proved"
        self.on_event({"kind": "finished", "status": status, "result": result})


RUNS: dict[str, Run] = {}

ACTIONS = {
    "hint": lambda s, req: s.add_hint(req.text),
    "lemma": lambda s, req: s.add_lemma(req.name, req.statement, req.text),
    "reject": lambda s, req: s.reject_lemma(req.name or req.statement),
    "pin": lambda s, req: s.pin(req.text),
    "pause": lambda s, req: s.pause(),
    "resume": lambda s, req: s.resume(),
    "stop": lambda s, req: s.stop(),
    "answer": lambda s, req: s.answer(req.text),
}


# ---------- endpoints (plain `def`: FastAPI runs slow calls in a worker thread) ----------


@app.post("/api/formalize")
def formalize(req: FormalizeRequest) -> dict:
    f = formalizer.formalize(req.latex)
    return {**asdict(f), "ok": f.ok}


@app.post("/api/check")
def check(req: CheckRequest) -> dict:
    return {"errors": statement_errors(req.statement)}


@app.post("/api/sessions")
def start_session(req: StartRequest) -> dict:
    if req.metric not in METRICS:
        raise HTTPException(status_code=400, detail=f"metric must be one of {sorted(METRICS)}")
    run = Run(req)
    RUNS[run.id] = run
    return {"id": run.id}


@app.post("/api/sessions/{run_id}/{action}")
def session_action(run_id: str, action: str, req: ActionRequest) -> dict:
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="no such session")
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    ACTIONS[action](RUNS[run_id].session, req)
    return {"ok": True}


@app.websocket("/ws/{run_id}")
async def watch(websocket: WebSocket, run_id: str) -> None:
    await websocket.accept()
    if run_id not in RUNS:
        await websocket.close(code=4404)
        return
    run = RUNS[run_id]
    history, q = run.subscribe(asyncio.get_running_loop())
    try:
        while True:
            event = history.pop(0) if history else await q.get()
            await websocket.send_json(event)
            if event["kind"] == "finished":
                await websocket.close()
                return
    except WebSocketDisconnect:
        pass  # the browser tab closed; that's fine
    finally:
        run.unsubscribe(q)


# Everything that isn't an /api or /ws address is a file from web/ ("/" gives index.html).
# This must come LAST: a mount at "/" would otherwise catch the API addresses too.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
