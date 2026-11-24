"""START HERE. One complete run, from an approved Lean statement to a saved, explained proof.

The command line and the web server both call `prove`. (Turning LaTeX into the statement
happens before this, in translation/formalize.py, because a person must approve it.)
"""

import json
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from mathfinder import PROJECT_ROOT
from mathfinder.lean.repl import HEADER
from mathfinder.llm import LLMError
from mathfinder.proving.optimize import optimize
from mathfinder.proving.search import OnEvent, search
from mathfinder.proving.session import Session
from mathfinder.translation.explain import explain

RUNS_DIR = PROJECT_ROOT / "runs"


def new_run_dir() -> Path:
    """A fresh folder runs/<date-time>/ (with -2, -3, ... if that name is taken)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:
        folder = RUNS_DIR / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            folder.mkdir(parents=True)
            return folder
        except FileExistsError:
            n += 1


def tracer(folder: Path) -> OnEvent:
    """An on_event callback that appends each event to <folder>/trace.jsonl."""

    def log(event: dict) -> None:
        with (folder / "trace.jsonl").open("a") as f:
            f.write(json.dumps({"time": round(time.time(), 3), **event}, ensure_ascii=False) + "\n")

    return log


def prove(
    statement: str,
    latex: str = "",
    hints: Sequence[str] = (),
    rounds: int = 3,
    metric: str | None = "short",
    explained: bool = True,
    on_event: OnEvent = lambda event: None,
    session: Session | None = None,
) -> dict:
    """Run everything for one theorem. `metric=None` keeps the first proof found."""
    theorem, folder = latex or statement, new_run_dir()
    log = tracer(folder)

    def emit(event: dict) -> None:
        log(event)
        on_event(event)

    # 1. Search for a proof that Lean accepts.
    log({"kind": "meta", "theorem": theorem})
    result = search(statement, hints=hints, rounds=rounds, on_event=emit, session=session)
    used_hints = [*hints, *(session.hints() if session else [])]
    log({"kind": "result", "proved": result.proved, "hints": used_hints})
    outcome = {
        "proved": result.proved,
        "message": result.message,
        "lean_code": result.lean_code,
        "explanation": "",
        "plan": result.plan.sketch if result.plan else "",
        "optimization": None,
        "rounds": result.rounds,
        "folder": f"runs/{folder.name}",
    }
    if not result.proved:
        return outcome

    # 2. Look for a better proof.
    plan = result.plan
    if metric:
        better = optimize(statement, result.lean_code, plan, metric, emit)
        plan = better.plan
        outcome.update(
            lean_code=better.lean_code,
            plan=plan.sketch,
            optimization={"metric": metric, "before": better.original, "after": better.best},
        )
    # 3. Explain it in words.
    if explained:
        emit({"kind": "explaining"})
        try:
            outcome["explanation"] = explain(theorem, plan, outcome["lean_code"])
        except LLMError as e:  # the proof is still valid; only the write-up failed
            outcome["explanation"] = f"(could not write an explanation: {e})"

    # 4. Save everything.
    (folder / "theorem.txt").write_text(theorem + "\n")
    (folder / "proof.lean").write_text(HEADER + outcome["lean_code"])  # Lean can check this file on its own
    (folder / "explanation.md").write_text(outcome["explanation"] + "\n")
    return outcome
