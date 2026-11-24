"""Run Lean on generated code and read back what it says.

Loading Mathlib takes ~30 seconds, so we never start Lean per check. Instead a few
long-running Lean REPL processes (leanprover-community/repl) each load Mathlib and
HEADER once; every check then runs on top of that loaded state in well under a second.
Each check still starts from the same clean state, so checks can't see each other.

We still batch many candidate theorems into one check and use line numbers to work
out which messages belong to which theorem.
"""

import atexit
import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

from mathfinder import LEAN_DIR

# autoImplicit false: an unknown name in a statement is an ERROR, not a silent new variable.
HEADER = "import Mathlib\n\nset_option autoImplicit false\n\nopen BigOperators Real Nat Topology\n\n"
DEFAULT_HEARTBEATS = 200_000  # Lean's "fuel" per theorem; stops runaway tactics
DEFAULT_TIMEOUT = 300  # seconds for one check
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
FORBIDDEN = ("sorry", "admit", "axiom")  # never allowed in a generated proof

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


@dataclass
class Decl:
    """One candidate proof of a statement.

    It is written as an `example`, not a `theorem`: Lean checks an example but
    never adds it to the environment, so one candidate can't cite another
    (failed!) candidate from the same file as if it were proved. `name` is only
    our label for finding its messages.

    `context` is optional Lean code that only this candidate can see, such as
    the lemmas of one proof plan. It goes inside a namespace of its own, so two
    plans can both have a lemma called `key` without clashing. Errors inside the
    context count as errors of this candidate.
    """

    name: str
    statement: str  # e.g. "(n : ℕ) : n + 0 = n"
    proof: str  # tactic script, e.g. "simp"
    context: str = ""

    def render(self, heartbeats: int = DEFAULT_HEARTBEATS) -> str:
        body = indent(self.proof)
        example = f"set_option maxHeartbeats {heartbeats} in\nexample {self.statement} := by\n{body}\n"
        if not self.context:
            return f"-- {self.name}\n{example}"
        return f"-- {self.name}\nnamespace {self.name}\n\n{self.context.strip()}\n\n{example}\nend {self.name}\n"


@dataclass
class DeclResult:
    name: str
    ok: bool  # True only if Lean accepted it with no errors and no sorry
    errors: list[str] = field(default_factory=list)
    used_sorry: bool = False
    infos: list[str] = field(default_factory=list)  # e.g. "Try this: exact foo" from exact?


@dataclass
class FinalResult:
    ok: bool
    errors: list[str]
    axioms: list[str]


def indent(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.strip("\n").split("\n"))


def render_theorem(name: str, statement: str, proof: str) -> str:
    return f"theorem {name} {statement} := by\n{indent(proof)}"


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


def check_decls(
    decls: list[Decl],
    preamble: str = "",
    heartbeats: int = DEFAULT_HEARTBEATS,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, DeclResult]:
    """Check many theorems in one Lean run. Returns one result per theorem name.

    `preamble` is extra Lean code placed before the theorems (for example,
    lemmas they are allowed to use). Problems inside the preamble are ignored
    unless they are errors, which make every theorem fail.
    """
    code = HEADER + preamble.strip() + "\n\n"
    ranges = {}  # name -> (first line, last line)
    for decl in decls:
        first = code.count("\n") + 1
        code += decl.render(heartbeats) + "\n"
        ranges[decl.name] = (first, code.count("\n"))

    out = run_lean(code, timeout)
    results = {d.name: DeclResult(d.name, ok=True) for d in decls}

    for msg in out.messages:
        owner = next((n for n, (a, b) in ranges.items() if a <= msg.line <= b), None)
        if owner is None:
            if msg.severity == "error":  # broken header/preamble: nothing can pass
                for r in results.values():
                    r.ok = False
                    r.errors.append(f"preamble: {msg.text}")
            continue
        result = results[owner]
        if msg.severity == "error":
            result.ok = False
            result.errors.append(msg.text)
        elif msg.kind == "hasSorry":
            result.ok = False
            result.used_sorry = True
        elif msg.severity == "information":
            result.infos.append(msg.text)

    if out.timed_out:
        for r in results.values():
            if r.ok:  # we can't trust a run that never finished
                r.ok = False
                r.errors.append(f"Lean timed out after {timeout}s")
    return results


def statement_errors(statement: str, timeout: int = DEFAULT_TIMEOUT) -> list[str]:
    """Does this statement even make sense to Lean? Checks it with `sorry` as the proof."""
    result = check_decls([Decl("mf_statement", statement, "sorry")], timeout=timeout)
    return result["mf_statement"].errors


def verify_final(code: str, theorem: str, timeout: int = DEFAULT_TIMEOUT) -> FinalResult:
    """The strict last check of a complete proof file.

    Passes only if Lean reports no errors, nothing uses sorry, and `theorem`
    depends on no axioms beyond Lean's three standard ones.
    """
    out = run_lean(HEADER + code.strip() + f"\n\n#print axioms {theorem}\n", timeout)
    errors = [m.text for m in out.messages if m.severity == "error"]
    if any(m.kind == "hasSorry" for m in out.messages):
        errors.append("proof uses sorry")
    if out.timed_out:
        errors.append(f"Lean timed out after {timeout}s")

    axioms = []
    report = [m.text for m in out.messages if m.text.startswith(f"'{theorem}' ")]
    if not report:
        errors.append(f"no axiom report for {theorem}")
    elif "depends on axioms:" in report[0]:
        inside = report[0].split("[", 1)[1].rsplit("]", 1)[0]
        axioms = [a.strip() for a in inside.split(",") if a.strip()]
    extra = [a for a in axioms if a not in ALLOWED_AXIOMS]
    if extra:
        errors.append(f"uses non-standard axioms: {extra}")
    return FinalResult(ok=not errors, errors=errors, axioms=axioms)
