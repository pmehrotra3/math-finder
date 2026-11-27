"""An open project: its workspace, its chat, the assistant working in it, and the pages watching.

The server (server.py) keeps one Live object per project someone opened. Everything that
happens in the project goes through it:

  - every change is SAVED to disk and BROADCAST to the open pages (over their WebSockets);
  - slow jobs (proving, the search, the final check) run in a background thread;
  - the CHAT: your messages, the assistant's replies, and what the proof reported
    ("case_even: proved") are lines in one conversation, saved with the project;
  - the ASSISTANT works as an agent. When you send a message it runs in steps: it replies and
    acts, waits for the job it started, reads the result, and takes the next step, until it
    says it is done (or MAX_STEPS). A message you send while it works is read at its next
    step, and also ends a wait, so you can redirect it. Stop ends both the job and the agent.
"""

import asyncio
import threading
import time
import uuid

from mathfinder import assistant, pipeline
from mathfinder.llm import LLMError
from mathfinder.projects import Project, ProjectStore
from mathfinder.proving.search import NAMESPACE
from mathfinder.proving.workspace import Busy, Workspace
from mathfinder.translation.explain import explain

MAX_STEPS = 12  # steps the assistant may take for one request before it pauses and asks
STOP_WAIT = 30  # seconds to wait for a stopped job to end before going on


class Live:
    def __init__(self, project: Project, store: ProjectStore):
        self.project = project
        self.store = store
        self.workspace = Workspace(
            project.statement,
            project.latex,
            on_event=self.on_event,
            library_code=store.lean_code_for,
            goals=project.goals or None,
            notes=project.notes,
        )
        self.listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self.lock = threading.Lock()  # guards the listeners
        self.saving = threading.RLock()  # the chat grows from several threads; save one at a time

        # the assistant (see run_agent)
        self.agent_lock = threading.Lock()  # guards the three flags below
        self.agent_running = False
        self.unread = False  # you wrote something the assistant hasn't read yet
        self.interrupted = False  # you pressed Stop
        self.agent_state = {"state": "idle", "step": 0}  # idle | thinking | acting | waiting (for a job)
        self.looking_at = "main"  # the goal open in your proof panel
        self.job_to_start = None  # a job the assistant asked for; it starts once its reply is shown
        self.wait_asked = False  # the assistant asked to wait for the running job

    # ---------- saving and showing ----------

    def save(self) -> None:
        with self.saving:
            self.project.goals = self.workspace.saved_goals()
            self.project.uses = self.workspace.projects_used()
            self.project.notes = list(self.workspace.notes)
            self.store.save(self.project)

    def add_line(self, role: str, text: str, actions: list[dict] = ()) -> dict:
        """Add a line to the chat, save it, and show it on every open page."""
        entry = {"id": uuid.uuid4().hex[:10], "role": role, "text": text, "time": time.time()}
        if actions:
            entry["actions"] = list(actions)
        with self.saving:
            self.project.conversation.append(entry)
            self.save()
        self.broadcast({"kind": "chat", "entry": entry})
        return entry

    def update_line(self, entry: dict, actions: list[dict]) -> None:
        """Show the actions of a reply as they run: each one "running", then its result."""
        with self.saving:
            entry["actions"] = list(actions)
            self.save()
        self.broadcast({"kind": "chat", "entry": dict(entry)})

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
            "notes": self.workspace.notes,
            "result": p.result,
            "agent": self.agent_state,
        }

    def on_event(self, event: dict) -> None:
        """Called by the workspace (from any thread) for every change: save it, then tell the pages.
        Its log lines ("step_one: proved") also go into the chat."""
        if "workspace" in event:
            self.save()
        self.broadcast(event)
        if event["kind"] == "log":
            self.add_line("event", event["text"])

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

    # ---------- slow jobs ----------

    def in_background(self, job) -> None:
        """Run a slow job in its own thread. Raises Busy if one is already running."""
        if self.workspace.busy:
            raise Busy("something is already running; stop it or wait")

        def work() -> None:
            try:
                self.workspace.run(job)
            except Exception as e:  # never leave the page waiting without saying why
                self.broadcast({"kind": "error", "text": str(e)})
                self.add_line("event", f"Stopped with an error: {e}")

        threading.Thread(target=work, daemon=True).start()

    def finish(self) -> None:
        """Final Lean check of the whole tree. If it passes, the project becomes established:
        it is explained, saved, and other projects may cite its theorem from now on."""
        result = self.workspace.finish()
        outcome = {"proved": result["ok"], "lean_code": result["lean_code"], "explanation": "", "folder": ""}
        if not result["ok"]:
            outcome["message"] = "The final Lean check failed: " + " | ".join(result["errors"])[:600]
            self.project.result = outcome
            self.save()
            self.broadcast({"kind": "finished", "result": outcome})
            self.add_line("event", outcome["message"])
            return
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
        self.add_line("event", "The whole proof passed Lean's final check. This project is now established.")

    def stop(self) -> None:
        """The Stop button: end the running job and the assistant's work."""
        with self.agent_lock:
            if self.agent_running:
                self.interrupted = True
        self.workspace.session.stop()

    def check_citations(self, cited: list[str]) -> None:
        """A project may cite only established projects, never itself, never one that cites it."""
        allowed = {result["id"] for result in self.store.library(excluding=self.project.id)}
        for pid in cited:
            if pid and pid not in allowed:
                raise ValueError(f"project {pid} is not an established result this project can use")

    # ---------- the chat and the assistant ----------

    def chat(self, message: str, looking_at: str = "main") -> dict:
        """Add your message to the chat. The assistant reads it: right away if it is idle, or at
        its next step if it is working."""
        entry = self.add_line("you", message)
        self.looking_at = looking_at
        with self.agent_lock:
            self.unread = True
            if self.agent_running:
                return entry
            self.agent_running, self.interrupted = True, False
        threading.Thread(target=self.run_agent, daemon=True).start()
        return entry

    def set_agent_state(self, state: str, step: int = 0) -> None:
        self.agent_state = {"state": state, "step": step}
        self.broadcast({"kind": "agent", **self.agent_state})

    def run_agent(self) -> None:
        """The assistant's steps, until it is done, needs you, or you stop it."""
        step, more = 0, False
        try:
            while True:
                with self.agent_lock:
                    if self.interrupted or not (self.unread or more):
                        self.agent_running = False
                        return
                    fresh, self.unread = self.unread, False
                step += 1
                if step > MAX_STEPS:
                    self.add_line("event", f"Paused after {MAX_STEPS} steps. Say “continue” to keep going.")
                    with self.agent_lock:
                        self.agent_running = False
                    return
                more = self.take_step(step, fresh)
        except Exception as e:  # never leave the assistant "working" forever
            self.add_line("assistant", f"(I had to stop: {e})")
            with self.agent_lock:
                self.agent_running = False
        finally:
            self.set_agent_state("idle")

    def take_step(self, step: int, fresh: bool) -> bool:
        """One step: think, act, and wait for the job it started. Returns whether to go on."""
        self.set_agent_state("thinking", step)
        reply = assistant.reply(self.situation(), list(self.project.conversation), step, fresh)
        if self.interrupted:
            self.add_line("event", "Stopped.")
            return False
        self.job_to_start, self.wait_asked = None, False
        self.set_agent_state("acting", step)
        line, done = self.add_line("assistant", reply.text), []
        for action in reply.actions:  # the reply first, then what it does, as it happens
            self.update_line(line, [*done, {"do": action["do"], "ok": None, "text": "running…"}])
            done.append(self.act(action))
            self.update_line(line, done)
        job, self.job_to_start = self.job_to_start, None
        if job is not None:
            try:
                self.in_background(job)
            except Busy:
                self.add_line("event", "Couldn't start: something else started running first.")
                job = None
        if job is None and not (self.wait_asked and self.workspace.busy):
            return not reply.done
        # A job is running for the assistant: wait for it, then look at the result (and report it).
        self.set_agent_state("waiting", step)
        wait_until(lambda: not self.workspace.busy or self.interrupted or self.unread, None)
        return not self.interrupted

    def situation(self) -> str:
        """What the assistant needs to know right now."""
        ws = self.workspace
        parts = [f"The proof (goals, status, plans, what Lean said):\n{ws.summary()}"]
        parts.append("A job is RUNNING now." if ws.busy else "Nothing is running.")
        if self.looking_at in ws.goals and self.looking_at != "main":
            parts.append(f"The mathematician has the goal {self.looking_at} open in the proof panel.")
        library = self.store.library(excluding=self.project.id)
        if library:
            parts.append(
                "Established results from other projects (cite with their id):\n"
                + "\n".join(f"- id {r['id']}: {r['statement']}" for r in library)
            )
        return "\n\n".join(parts)

    def act(self, action: dict) -> dict:
        """Carry out one action from the assistant. Returns what happened, for the chat."""
        try:
            ok, text = self._act(action)
        except (ValueError, KeyError, TypeError, Busy) as e:
            ok, text = False, f"couldn't: {e}"
        done = {"do": action["do"], "ok": ok, "text": text}
        if action["do"] == "try_proof":  # show the Lean it wrote under its reply
            done["lean"] = str(action.get("proof", ""))
        elif action["do"] == "plan":
            lemmas = [f"lemma {lem.get('name')} {lem.get('lean')}" for lem in action.get("lemmas", [])]
            done["lean"] = "\n".join([*lemmas, f"-- main proof:\n{action.get('main_proof', '')}"])
        return done

    def start_after_reply(self, job, text: str) -> tuple[bool, str]:
        if self.job_to_start is not None:
            return False, "skipped: only one job can run at a time"
        self.job_to_start = job
        return True, text

    def _act(self, a: dict) -> tuple[bool, str]:
        ws, do = self.workspace, a["do"]
        if do == "stop":
            if not ws.busy:
                return True, "nothing was running"
            ws.session.stop()
            stopped = wait_until(lambda: not ws.busy, STOP_WAIT)
            return True, "stopped" if stopped else "asked to stop; it stops after the step it is on"
        if do == "wait":
            self.wait_asked = True
            return True, "waiting for the running job" if ws.busy else "nothing was running"
        if do == "note":
            ws.add_note(str(a["text"]))
            self.save()
            return True, f"noted: {a['text']}"
        if do == "clear_notes":
            ws.clear_notes()
            self.save()
            return True, "forgot all hints"
        if ws.busy or self.job_to_start is not None:
            return False, "skipped: a job is still running (stop it or wait first)"
        if do == "finish":
            root = ws.goals["main"]
            if root.status != "proved" or ws.assumed_below("main"):
                return False, "the theorem isn't proved without assumptions yet"
            return self.start_after_reply(self.finish, "started the final check")
        goal = a.get("goal", "main")
        if goal not in ws.goals:
            return False, f"there is no goal called {goal}"
        if do == "try_proof":
            result = {}
            ws.run(lambda: result.update(zip(("ok", "error"), ws.try_proof(goal, str(a["proof"])), strict=True)))
            self.save()
            if result["ok"]:
                return True, f"Lean accepted the proof of {goal}"
            return False, f"Lean rejected it: {result['error'][:400]}"
        if do == "prove":
            return self.start_after_reply(lambda: ws.prove(goal), f"started proving {goal}")
        if do == "search":
            return self.start_after_reply(lambda: ws.auto(goal), f"started the search for {goal}")
        if do == "plan":
            lemmas = [{"name": str(lem["name"]), "lean": str(lem["lean"])} for lem in a.get("lemmas", [])]
            problems = ws.set_plan(goal, str(a.get("sketch", "")), str(a.get("main_proof", "")), lemmas)
            self.save()
            if problems:
                return False, f"new plan for {goal} saved, but it doesn't fit together yet: {problems[0][:300]}"
            return True, f"new plan for {goal} with {len(lemmas)} lemmas; Lean says it fits together"
        if do == "clear_plan":
            ws.clear_plan(goal)
            self.save()
            return True, f"removed the plan for {goal}"
        if do == "statement":
            errors = ws.edit_statement(goal, str(a["lean"]))
            self.save()
            return (False, f"Lean rejected it: {errors[0][:300]}") if errors else (True, f"changed {goal}")
        if do == "assume":
            on = bool(a.get("on", True))
            ws.assume(goal, on)
            self.save()
            return True, f"{'assuming' if on else 'no longer assuming'} {goal}"
        if do == "cite":
            self.check_citations([str(a["project"])])
            ws.use_established(goal, str(a["project"]))
            self.save()
            g = ws.goals[goal]
            return g.status == "proved", f"{goal} cited" if g.status == "proved" else g.feedback[:300]
        return False, f"unknown action {do}"


def wait_until(condition, seconds: float | None) -> bool:
    """Wait until `condition()` is true (True), or `seconds` pass (False); None = no limit."""
    deadline = None if seconds is None else time.monotonic() + seconds
    while not condition():
        if deadline is not None and time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def in_namespace(code: str, namespace: str) -> str:
    """Move a verified proof from the working namespace into the project's own namespace."""
    head, end = f"namespace {NAMESPACE}\n", f"end {NAMESPACE}\n"
    assert code.startswith(head) and code.endswith(end), "unexpected shape of an assembled proof"
    return f"namespace {namespace}\n" + code[len(head) : -len(end)] + f"end {namespace}\n"
