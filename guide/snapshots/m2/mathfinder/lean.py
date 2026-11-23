"""Run Lean on generated code and read back what it says.

Starting Lean + loading Mathlib takes ~20 seconds, so we never check one proof
at a time. Instead we put many candidate theorems in ONE file, run Lean once,
and use line numbers to work out which messages belong to which theorem.
"""

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

from mathfinder import LEAN_DIR

# autoImplicit false: an unknown name in a statement is an ERROR, not a silent new variable.
HEADER = "import Mathlib\n\nset_option autoImplicit false\n\nopen BigOperators Real Nat Topology\n\n"
DEFAULT_HEARTBEATS = 200_000  # Lean's "fuel" per theorem; stops runaway tactics
DEFAULT_TIMEOUT = 300  # seconds for one whole Lean run
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}

# At most this many Lean processes at once (each one uses a lot of memory).
MAX_PARALLEL = 2
_slots = threading.Semaphore(MAX_PARALLEL)


@dataclass
class Message:
    line: int  # 1-based line in the generated file
    severity: str  # "error", "warning" or "information"
    text: str
    kind: str = ""


@dataclass
class RunOutput:
    messages: list[Message]
    timed_out: bool
    seconds: float


@dataclass
class Decl:
    """One candidate proof of a statement.

    It is written as an `example`, not a `theorem`: Lean checks an example but
    never adds it to the environment, so one candidate can't cite another
    (failed!) candidate from the same file as if it were proved. `name` is only
    our label for finding its messages.
    """

    name: str
    statement: str  # e.g. "(n : ℕ) : n + 0 = n"
    proof: str  # tactic script, e.g. "simp"

    def render(self, heartbeats: int = DEFAULT_HEARTBEATS) -> str:
        body = indent(self.proof)
        return (
            f"-- {self.name}\n"
            f"set_option maxHeartbeats {heartbeats} in\n"
            f"example {self.statement} := by\n{body}\n"
        )


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


def run_lean(code: str, timeout: int = DEFAULT_TIMEOUT) -> RunOutput:
    """Write `code` to a temporary .lean file, run Lean on it, parse its JSON messages."""
    tmp_dir = LEAN_DIR / ".mf_tmp"
    tmp_dir.mkdir(exist_ok=True)
    path = tmp_dir / f"check_{uuid.uuid4().hex}.lean"
    path.write_text(code)
    start = time.time()
    timed_out = False
    try:
        with _slots:
            proc = subprocess.Popen(
                ["lake", "env", "lean", "--json", str(path.relative_to(LEAN_DIR))],
                cwd=LEAN_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # so we can kill Lean and all its children
            )
            try:
                stdout, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, _ = proc.communicate()
                timed_out = True
    finally:
        path.unlink(missing_ok=True)
    return RunOutput(parse_messages(stdout), timed_out, time.time() - start)


def parse_messages(stdout: str) -> list[Message]:
    messages = []
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        data = json.loads(line)
        messages.append(
            Message(
                line=data["pos"]["line"],
                severity=data["severity"],
                text=data["data"],
                kind=data.get("kind", ""),
            )
        )
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
