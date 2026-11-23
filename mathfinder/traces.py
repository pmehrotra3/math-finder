"""Traces: a record of everything a search did.

A TraceLogger writes every event as one line of JSON to runs/<id>/trace.jsonl,
failed proof attempts included, so any run can be replayed or debugged later
(tools/make_demo.py turns one into the web page's offline demo).
"""

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

OnEvent = Callable[[dict], None]  # receives every search event (see search.py)


class TraceLogger:
    """An on_event callback that appends every event to <run_dir>/trace.jsonl."""

    def __init__(self, run_dir: Path, theorem: str):
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "trace.jsonl"
        self._lock = threading.Lock()  # events can arrive from the search thread and the main thread
        self({"kind": "meta", "theorem": theorem})

    def __call__(self, event: dict) -> None:
        record = {"time": round(time.time(), 3), **event}
        with self._lock, self.path.open("a") as f:  # "a" = append to the end of the file
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def finish(self, proved: bool, hints: list[str]) -> None:
        self({"kind": "result", "proved": proved, "hints": list(hints)})


def chain(*callbacks: OnEvent) -> OnEvent:
    """One on_event callback that passes each event to several watchers (e.g. printer + logger)."""

    def on_event(event: dict) -> None:
        for callback in callbacks:
            callback(event)

    return on_event
