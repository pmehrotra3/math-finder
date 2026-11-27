"""The benchmark: run the full search on every problem in problems.jsonl (next to this file).

Traces and a results table go to runs/bench-<date-time>/.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from mathfinder.pipeline import RUNS_DIR, tracer
from mathfinder.proving.search import SearchResult, search

PROBLEMS_FILE = Path(__file__).with_name("problems.jsonl")


def load_problems() -> list[dict]:
    return [json.loads(line) for line in PROBLEMS_FILE.read_text().splitlines() if line.strip()]


def how_it_was_proved(result: SearchResult) -> str:
    if not result.proved:
        return result.message
    if result.rounds == 0:
        return result.plan.sketch  # e.g. "Automation: nlinarith"
    return f"plan with {len(result.plan.lemmas)} lemma(s), round {result.rounds}"


def run_one(problem: dict, rounds: int, folder: Path) -> dict:
    """Search for a proof of one problem; return a results row."""
    folder.mkdir(parents=True)
    start = time.time()
    try:
        result = search(problem["statement"], rounds=rounds, on_event=tracer(folder))
        proved, how = result.proved, how_it_was_proved(result)
    except Exception as e:  # one broken problem must not stop the whole benchmark
        proved, how = False, f"crash: {e}"
    return {**problem, "proved": proved, "seconds": round(time.time() - start), "how": how}


def results_table(rows: list[dict]) -> str:
    """A Markdown table, plus a one-line summary and a per-topic count."""
    proved = sum(r["proved"] for r in rows)
    lines = [
        f"**{proved}/{len(rows)} proved** ({proved / len(rows):.0%}), "
        f"total time {sum(r['seconds'] for r in rows) // 60} min",
        "",
        "| # | Problem | Topic | Result | Time | How |",
        "|---|---------|-------|--------|------|-----|",
    ]
    for i, r in enumerate(rows, 1):
        mark = "✓ proved" if r["proved"] else "✗ not proved"
        lines.append(f"| {i} | `{r['name']}` | {r['topic']} | {mark} | {r['seconds']} s | {r['how'][:90]} |")
    topics = sorted({r["topic"] for r in rows})
    by_topic = ", ".join(
        f"{t} {sum(r['proved'] for r in rows if r['topic'] == t)}/{sum(r['topic'] == t for r in rows)}" for t in topics
    )
    lines += ["", f"By topic: {by_topic}"]
    return "\n".join(lines)


def run_bench(problems: list[dict], rounds: int = 2) -> Path:
    """Run every problem; return the path of the results table."""
    folder = RUNS_DIR / f"bench-{datetime.now():%Y%m%d-%H%M%S}"
    rows = []
    for i, problem in enumerate(problems, 1):
        print(f"[{i}/{len(problems)}] {problem['name']}: {problem['statement']}", flush=True)
        rows.append(run_one(problem, rounds, folder / problem["name"]))
        print(f"    {'✓ proved' if rows[-1]['proved'] else '✗ not proved'} in {rows[-1]['seconds']} s", flush=True)
    path = folder / "results.md"
    path.write_text(results_table(rows) + "\n")
    return path
