"""Traces: remember everything the search did, then turn it into training data.

While searching, a TraceLogger writes every event as one line of JSON to
runs/<id>/trace.jsonl ("JSON Lines": one JSON object per line, easy to append to
and easy to read back). Failed attempts are logged too: knowing what DOESN'T work
is just as useful for training.

`export` reads every trace and builds four datasets in data/:
  sft.jsonl          goal (+ lemmas it may use) → a proof Lean accepted      ("imitate this")
  preferences.jsonl  same goal: a proof that worked vs. one that failed      ("prefer this one")
  value.jsonl        lemma statement → proved / refuted / stuck              ("how hard is this?")
  hints.jsonl        the hints a person gave → whether the theorem got proved
"""

import json
import threading
import time
from pathlib import Path

DATASETS = ("sft", "preferences", "value", "hints")


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


def chain(*callbacks):
    """One on_event callback that passes each event to several watchers (e.g. printer + logger)."""

    def on_event(event: dict) -> None:
        for callback in callbacks:
            callback(event)

    return on_event


def read_trace(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def records_from_trace(events: list[dict]) -> dict[str, list[dict]]:
    """Turn one run's events into rows for each dataset."""
    out = {name: [] for name in DATASETS}
    theorem = next((e["theorem"] for e in events if e["kind"] == "meta"), "")
    attempts = [e for e in events if e["kind"] == "attempt"]

    for a in attempts:
        if a["ok"]:
            out["sft"].append({"type": "lemma", "statement": a["statement"], "context": a["context"],
                               "proof": a["proof"], "source": a["source"]})
    for e in events:
        if e["kind"] == "done" and e.get("depth", 0) == 0:
            out["sft"].append({"type": "theorem", "statement": theorem, "context": [], "proof": e["lean_code"],
                               "source": "search"})

    by_goal = {}  # (statement, context) -> attempts on that exact goal
    for a in attempts:
        by_goal.setdefault((a["statement"], tuple(a["context"])), []).append(a)
    for (statement, context), group in by_goal.items():
        good = [a for a in group if a["ok"]]
        bad = [a for a in group if not a["ok"]]
        for g in good:
            for b in bad:
                out["preferences"].append({"statement": statement, "context": list(context), "chosen": g["proof"],
                                           "rejected": b["proof"], "rejected_error": b["error"]})

    for e in events:
        if e["kind"] in ("lemma_proved", "lemma_refuted", "lemma_stuck"):
            out["value"].append({"statement": e["statement"], "label": e["kind"].removeprefix("lemma_")})

    for e in events:
        if e["kind"] == "result" and e["hints"]:
            out["hints"].append({"theorem": theorem, "hints": e["hints"], "proved": e["proved"]})
    return out


def export(runs_dir: Path, out_dir: Path) -> dict[str, int]:
    """Build all datasets from every runs/*/trace.jsonl. Returns how many rows each got."""
    rows = {name: [] for name in DATASETS}
    for trace in sorted(runs_dir.glob("*/trace.jsonl")):
        for name, records in records_from_trace(read_trace(trace)).items():
            rows[name].extend(records)

    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, records in rows.items():
        unique = list({json.dumps(r, sort_keys=True, ensure_ascii=False): r for r in records}.values())
        with (out_dir / f"{name}.jsonl").open("w") as f:
            for r in unique:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        counts[name] = len(unique)
    return counts
