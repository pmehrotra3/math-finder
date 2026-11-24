"""Talk to Lean: send it code, get its messages back.

Loading Mathlib is slow, so we never start Lean per check. Instead a few long-running
Lean REPL processes (leanprover-community/repl) each load Mathlib and HEADER once; every
check then runs on top of that loaded state in well under a second. Each check starts
from the same clean state, so checks can't see each other.
"""

import atexit
import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from mathfinder import LEAN_DIR

# autoImplicit false: an unknown name in a statement is an ERROR, not a silent new variable.
HEADER = "import Mathlib\n\nset_option autoImplicit false\n\nopen BigOperators Real Nat Topology\n\n"
DEFAULT_TIMEOUT = 300  # seconds for one check

REPL_BINARY = ".lake/packages/REPL/.lake/build/bin/repl"  # built by `lake build repl` in lean_env/
STARTUP_TIMEOUT = 600  # seconds to load Mathlib in a fresh REPL (cold disk cache can be slow)
# How many REPLs may run at once. Each maps Mathlib into memory (~8 GB resident, much of it shared).
WORKERS = int(os.environ.get("MATHFINDER_LEAN_WORKERS", "2"))
# Every check leaves a new environment behind inside the REPL, so recycle it now and then.
MAX_CHECKS_PER_REPL = int(os.environ.get("MATHFINDER_LEAN_RECYCLE", "300"))


@dataclass
class Message:
    line: int  # 1-based line in the generated file (0 = not tied to a line)
    severity: str  # "error", "warning" or "information"
    text: str
    kind: str = ""


@dataclass
class RunOutput:
    messages: list[Message]
    timed_out: bool


class Repl:
    """One Lean REPL process with Mathlib and HEADER already loaded."""

    def __init__(self) -> None:
        if not (LEAN_DIR / REPL_BINARY).exists():
            raise RuntimeError(f"Lean REPL not built: run `lake build repl` in {LEAN_DIR}")
        self.proc = subprocess.Popen(
            ["lake", "env", REPL_BINARY],
            cwd=LEAN_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,  # so we can kill Lean and all its children
        )
        self.checks = 0
        # A reader thread turns stdout into a queue, so waiting for an answer can time out.
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        reply = self.send({"cmd": HEADER.strip()}, STARTUP_TIMEOUT)
        if reply is None or "env" not in reply or any(m["severity"] == "error" for m in reply.get("messages", [])):
            self.close()
            raise RuntimeError(f"Lean REPL failed to load the header: {reply}")
        self.env = reply["env"]

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # the process ended

    def send(self, command: dict, timeout: float) -> dict | None:
        """Send one command; return the reply, or None if Lean timed out or died."""
        deadline = time.monotonic() + timeout
        try:
            self.proc.stdin.write(json.dumps(command) + "\n\n")  # a blank line ends a command
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return None
        reply = []
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                return None
            if line.strip():
                reply.append(line)
            elif reply:  # a blank line ends the reply
                return json.loads("".join(reply))

    def close(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()


_slots = threading.Semaphore(WORKERS)
_idle: queue.Queue[Repl] = queue.Queue()  # started REPLs waiting for work


@atexit.register
def shutdown() -> None:
    """Stop every idle REPL (each holds gigabytes of memory)."""
    while not _idle.empty():
        _idle.get_nowait().close()


def run_lean(code: str, timeout: int = DEFAULT_TIMEOUT) -> RunOutput:
    """Check `code` (which must start with HEADER) in a REPL and return Lean's messages.

    Line numbers in the messages are lines of `code`, as if it were one .lean file."""
    if not code.startswith(HEADER):
        raise ValueError("code must start with lean.HEADER (the REPLs have it preloaded)")
    offset = HEADER.count("\n")
    with _slots:
        try:
            repl = _idle.get_nowait()
        except queue.Empty:
            repl = Repl()  # ~30 s: loads Mathlib
        reply = repl.send({"cmd": code[len(HEADER) :], "env": repl.env}, timeout)
        if reply is None:  # timed out or crashed: this REPL's state is unknown, so replace it
            crashed = repl.proc.poll() is not None
            repl.close()
            if crashed:
                return RunOutput([Message(0, "error", "Lean crashed")], False)
            return RunOutput([], True)
        repl.checks += 1
        if repl.checks >= MAX_CHECKS_PER_REPL:
            repl.close()
        else:
            _idle.put(repl)
    return RunOutput(parse_messages(reply, offset), False)


def parse_messages(reply: dict, offset: int = 0) -> list[Message]:
    """Turn a REPL reply into Messages; `offset` shifts line numbers back to the full file."""
    messages = []
    if "message" in reply:  # the REPL itself rejected the command
        messages.append(Message(0, "error", reply["message"]))
    for m in reply.get("messages", []):
        severity = "information" if m["severity"] == "info" else m["severity"]
        text = m["data"]
        kind = "hasSorry" if severity == "warning" and text.startswith("declaration uses") and "sorry" in text else ""
        messages.append(Message(m["pos"]["line"] + offset, severity, text, kind))
    return messages
