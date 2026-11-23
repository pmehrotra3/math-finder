"""The benchmark: how good is Math Finder, really?

bench/problems.jsonl holds competition-style problems (in the spirit of miniF2F),
already written as Lean statements, one JSON object per line:
  {"name": "...", "topic": "...", "statement": "..."}

`run_bench` runs the full search on each one and records what happened. Every run
is also traced (runs/bench-<time>/<name>/trace.jsonl), so the benchmark doubles as
a source of training data. Results go to bench/results/<time>.json and .md.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from mathfinder import PROJECT_ROOT
from mathfinder.optimize import proof_stats
from mathfinder.search import search
from mathfinder.traces import TraceLogger, chain

PROBLEMS_FILE = PROJECT_ROOT / "bench" / "problems.jsonl"
RESULTS_DIR = PROJECT_ROOT / "bench" / "results"
RUNS_DIR = PROJECT_ROOT / "runs"


def load_problems(path: Path = PROBLEMS_FILE) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def how_it_was_proved(result) -> str:
    if not result.proved:
        return result.message
    if result.rounds == 0:
        return result.plan.sketch  # e.g. "Automation: nlinarith"
    return f"plan with {len(result.plan.lemmas)} lemma(s), round {result.rounds}"


def run_one(problem: dict, rounds: int, run_dir: Path) -> dict:
    """Search for a proof of one problem; return a results row."""
    events = []
    logger = TraceLogger(run_dir / problem["name"], problem["statement"])
    start = time.time()
    try:
        result = search(problem["statement"], rounds=rounds, on_event=chain(events.append, logger))
    except Exception as e:  # one broken problem must not stop the whole benchmark
        logger.finish(False, [])
        return {**problem, "proved": False, "seconds": round(time.time() - start), "how": f"crash: {e}",
                "attempts": 0, "steps": None}
    logger.finish(result.proved, [])
    attempts = [e for e in events if e["kind"] == "attempt"]
    return {
        **problem,
        "proved": result.proved,
        "seconds": round(time.time() - start),
        "how": how_it_was_proved(result),
        "attempts": len(attempts),
        "steps": proof_stats(result.lean_code)["steps"] if result.proved else None,
    }


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
    by_topic = ", ".join(f"{t} {sum(r['proved'] for r in rows if r['topic'] == t)}/"
                         f"{sum(r['topic'] == t for r in rows)}" for t in topics)
    lines += ["", f"By topic: {by_topic}"]
    return "\n".join(lines)


def run_bench(problems: list[dict], rounds: int = 2, on_progress=print) -> tuple[list[dict], Path]:
    """Run every problem, save the results, return (rows, path of the .md file)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"bench-{stamp}"
    rows = []
    for i, problem in enumerate(problems, 1):
        on_progress(f"[{i}/{len(problems)}] {problem['name']}: {problem['statement']}")
        row = run_one(problem, rounds, run_dir)
        rows.append(row)
        on_progress(f"    {'✓ proved' if row['proved'] else '✗ not proved'} in {row['seconds']} s  ({row['how'][:80]})")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{stamp}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    md_path = RESULTS_DIR / f"{stamp}.md"
    md_path.write_text(results_table(rows) + "\n")
    return rows, md_path
