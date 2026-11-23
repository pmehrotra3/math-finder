"""Command line.

  uv run python -m mathfinder prove "For every integer $n$, $6 \\mid n^3 - n$."
  uv run python -m mathfinder prove --lean "(n : ℤ) : 6 ∣ n ^ 3 - n"

With LaTeX, you first see the Lean statement, its back-translation and the
judge's opinion, and nothing is searched until you approve.
Every run gets a folder runs/<date-time>/ with trace.jsonl (everything that happened);
a proved theorem also gets proof.lean + explanation.md there. After the first proof,
the optimizer looks for a better one (--metric short | readable | few_lemmas).
With --interactive you can type hints and commands while the search runs.

  uv run python -m mathfinder export      builds training data in data/ from all traces
  uv run python -m mathfinder bench       runs the benchmark in bench/problems.jsonl
"""

import argparse
import select
import sys
import threading
from datetime import datetime
from pathlib import Path

from mathfinder import PROJECT_ROOT
from mathfinder.bench import load_problems, results_table, run_bench
from mathfinder.collab import Session
from mathfinder.explain import explain
from mathfinder.formalize import formalize
from mathfinder.lean import HEADER, statement_errors
from mathfinder.llm import LLMError
from mathfinder.optimize import METRICS, optimize
from mathfinder.search import search
from mathfinder.traces import TraceLogger, chain, export

RUNS_DIR = PROJECT_ROOT / "runs"
DATA_DIR = PROJECT_ROOT / "data"

SYMBOL = {"lemma_proved": "✓", "lemma_refuted": "✗", "lemma_stuck": "…"}


def print_event(event: dict) -> None:
    kind = event["kind"]
    if event.get("depth", 0) > 0:  # events from a sub-search are indented under their lemma
        indent = "        " * event["depth"]
        if kind in SYMBOL:
            print(f"{indent}{SYMBOL[kind]} {event['name']} : {event['statement']}   [{event['feedback'][:80]}]")
        elif kind == "plan_start":
            print(f"{indent}▶ sub-plan: {event['sketch']}")
        elif kind == "done":
            print(f"{indent}✓ sub-search proved it")
        sys.stdout.flush()
        return
    if kind == "start":
        print(f"\nTheorem: {event['theorem']}")
    elif kind == "subsearch_start":
        print(f"      ↳ {event['name']} is stuck: starting a sub-search on it")
    elif kind == "refuted":
        print(f"✗ The statement is FALSE. Counterexample: {event['counterexample']}")
    elif kind == "round":
        print(f"\n— round {event['number']}: asking the planner —")
    elif kind == "plan_rejected":
        print(f"  ✗ plan doesn't fit together: {event['sketch']}\n      {event['reason'][:160]}")
    elif kind == "plan_start":
        print(f"  ▶ plan: {event['sketch']}  (lemmas: {', '.join(event['lemmas']) or 'none'})")
    elif kind in SYMBOL:
        print(f"      {SYMBOL[kind]} {event['name']} : {event['statement']}   [{event['feedback'][:80]}]")
    elif kind == "plan_failed":
        print(f"    plan failed (score {event['score']:.2f})")
    elif kind == "done":
        print(f"\n✓ VERIFIED by Lean. Plan: {event['plan']}\n")
        print(event["lean_code"])
    elif kind == "gave_up":
        print("\nNo verified proof found.")
    elif kind == "ask_user":
        print(f"\n❓ {event['question']}\n   (type your answer and press Enter)")
    elif kind == "user_answer":
        print(f"  ← thanks, trying again with your idea: {event['text']}")
    elif kind == "no_answer":
        print("  (no answer)")
    elif kind == "stopped":
        print(f"\n■ Stopped: {event['reason']}")
    sys.stdout.flush()  # show progress immediately, even when output is piped to a file


def approve(latex: str, auto_yes: bool = False, ask=input) -> str | None:
    """Formalize `latex`, show it, and return the statement the user approved (None = no)."""
    print("Formalizing (LaTeX → Lean)...")
    f = formalize(latex)
    if not f.ok:
        print("Could not produce a statement Lean accepts:\n  " + f.errors[0][:400])
        return None
    print(f"\nFormal statement:  theorem main {f.statement}")
    print(f"Back-translation:  {f.back_translation}")
    print(f"Judge:             {'same meaning' if f.faithful else 'MEANING MAY DIFFER'}: {f.judgment}")

    statement = f.statement
    if auto_yes and f.faithful:
        return statement
    if auto_yes:
        print("(--yes ignored: the judge thinks the meaning may differ, so please look.)")
    while True:
        try:
            answer = ask("\nProve this statement? [y]es / [n]o / [e]dit: ").strip().lower()
        except EOFError:  # no keyboard attached (e.g. running in a script)
            return None
        if answer == "y":
            return statement
        if answer == "n":
            return None
        if answer == "e":
            new = ask("Lean statement (what comes after `theorem main`): ").strip()
            errors = statement_errors(new)
            if errors:
                print("Lean rejected it: " + errors[0][:300])
            else:
                statement = new
                print(f"Now:  theorem main {statement}")


COMMANDS = """While it runs you can type:
  hint <text>                     e.g.  hint try induction on n
  lemma <name> <lean statement>   e.g.  lemma key (a b : ℝ) : 0 ≤ (a - b) ^ 2
  reject <lemma name>             plans using that lemma are dropped
  pin <approach>                  "keep going in this direction"
  pause | resume | stop
When the search asks you a question, just type your answer."""


def handle_command(session: Session, line: str) -> str:
    """Apply one typed line to the session. Returns a short confirmation to print."""
    line = line.strip()
    word, _, rest = line.partition(" ")  # "hint try x" -> ("hint", " ", "try x")
    if session.question is not None and word not in ("pause", "resume", "stop"):
        session.answer(line)  # a question is open: the whole line is the answer
        return "answer sent"
    if word == "hint" and rest:
        session.add_hint(rest)
        return f"hint added: {rest}"
    if word == "lemma" and " " in rest:
        name, statement = rest.split(" ", 1)
        session.add_lemma(name, statement)
        return f"lemma suggested: {name}"
    if word == "reject" and rest:
        session.reject_lemma(rest)
        return f"rejected: {rest}"
    if word == "pin" and rest:
        session.pin(rest)
        return f"pinned: {rest}"
    if word == "pause":
        session.pause()
        return "paused (type resume to continue)"
    if word == "resume":
        session.resume()
        return "resumed"
    if word == "stop":
        session.stop()
        return "stopping..."
    return "unknown command. " + COMMANDS


def interactive_search(statement: str, hints: list[str], rounds: int, on_event=print_event):
    """Run the search in a background thread while this thread reads your commands.
    Returns (result, every hint used, including the ones you typed)."""
    session = Session(statement)
    for h in hints:
        session.add_hint(h)
    box = {}
    worker = threading.Thread(
        target=lambda: box.setdefault("result", search(statement, rounds=rounds, on_event=on_event, session=session)),
        daemon=True,  # don't keep Python alive just for this thread
    )
    worker.start()
    print(COMMANDS)
    keyboard_open = True
    while worker.is_alive():
        if not keyboard_open:
            worker.join(0.5)
            continue
        ready, _, _ = select.select([sys.stdin], [], [], 0.5)  # wait up to 0.5 s for a typed line
        if ready:
            line = sys.stdin.readline()
            if line == "":  # end of input (Ctrl-D, or a pipe that closed)
                keyboard_open = False
            elif line.strip():
                print(f"  [{handle_command(session, line)}]")
    return box["result"], session.hints()


def new_run_dir() -> Path:
    """A fresh folder for this run: runs/<date-time>/ (with -2, -3, ... if that name is taken)."""
    base = RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder, n = base, 1
    while folder.exists():
        n += 1
        folder = base.with_name(f"{base.name}-{n}")
    folder.mkdir(parents=True)
    return folder


def save_run(folder: Path, theorem: str, lean_code: str, explanation: str) -> Path:
    """Save a proved theorem into its run folder: theorem.txt, proof.lean, explanation.md."""
    (folder / "theorem.txt").write_text(theorem + "\n")
    (folder / "proof.lean").write_text(HEADER + lean_code)  # a complete file Lean can check on its own
    (folder / "explanation.md").write_text(explanation + "\n")
    return folder


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mathfinder")
    commands = parser.add_subparsers(dest="command", required=True)
    prove = commands.add_parser("prove", help="search for a proof")
    prove.add_argument("latex", nargs="?", help="the theorem in LaTeX (in quotes)")
    prove.add_argument("--lean", help='skip formalization: a Lean statement, e.g. "(n : ℕ) : n + 0 = n"')
    prove.add_argument("--yes", action="store_true", help="don't ask for approval if the judge is happy")
    prove.add_argument("--hint", action="append", default=[], help="a hint for the planner (repeatable)")
    prove.add_argument("--rounds", type=int, default=3)
    prove.add_argument("--no-explain", action="store_true", help="skip the plain-math explanation")
    prove.add_argument("--metric", choices=sorted(METRICS), default="short", help="what 'better proof' means")
    prove.add_argument("--no-optimize", action="store_true", help="keep the first proof found")
    prove.add_argument("--interactive", action="store_true", help="type hints and commands while it searches")
    export_cmd = commands.add_parser("export", help="build training data from all runs/*/trace.jsonl")
    export_cmd.add_argument("--out", default=str(DATA_DIR), help="output folder (default: data/)")
    bench_cmd = commands.add_parser("bench", help="run the benchmark in bench/problems.jsonl")
    bench_cmd.add_argument("--rounds", type=int, default=2, help="planning rounds per problem")
    bench_cmd.add_argument("--only", nargs="*", default=[], help="names of problems to run (default: all)")
    args = parser.parse_args(argv)

    if args.command == "bench":
        problems = load_problems()
        if args.only:
            problems = [p for p in problems if p["name"] in args.only]
        rows, md_path = run_bench(problems, args.rounds)
        print("\n" + results_table(rows))
        print(f"\nSaved {md_path}")
        return 0

    if args.command == "export":
        counts = export(RUNS_DIR, Path(args.out))
        for name, count in counts.items():
            print(f"{name:12} {count:5} rows  -> {Path(args.out) / (name + '.jsonl')}")
        return 0

    if args.command == "prove":
        if args.lean:
            statement = args.lean
        elif args.latex:
            statement = approve(args.latex, args.yes)
            if statement is None:
                print("Not searching.")
                return 1
        else:
            parser.error("give a theorem in LaTeX, or --lean STATEMENT")
        folder = new_run_dir()
        logger = TraceLogger(folder, args.latex or statement)
        on_event = chain(print_event, logger)  # print AND log every event
        if args.interactive:
            result, hints_used = interactive_search(statement, args.hint, args.rounds, on_event)
        else:
            result = search(statement, hints=args.hint, rounds=args.rounds, on_event=on_event)
            hints_used = args.hint
        logger.finish(result.proved, hints_used)
        print(f"\nResult: {'PROVED' if result.proved else 'not proved'} ({result.message})")
        print(f"Trace: {logger.path}")
        if not result.proved:
            return 1
        lean_code, plan = result.lean_code, result.plan
        if not args.no_optimize:
            print(f"\nLooking for a better proof (metric: {args.metric})...")
            better = optimize(statement, lean_code, plan, args.metric, on_event)
            print(f"  cost {better.original['cost']} → {better.best['cost']}"
                  + ("  (better proof found)" if better.improved else "  (kept the first proof)"))
            if better.improved:
                lean_code, plan = better.lean_code, better.plan
                print("\n" + lean_code)
        explanation = ""
        if not args.no_explain:
            print("\nWriting the explanation...")
            try:
                explanation = explain(args.latex or statement, plan, lean_code)
                print("\n" + explanation)
            except LLMError as e:  # the proof is still valid; only the write-up failed
                print(f"(could not write an explanation: {e})")
        print(f"\nSaved to {save_run(folder, args.latex or statement, lean_code, explanation)}")
        return 0
    return 2
