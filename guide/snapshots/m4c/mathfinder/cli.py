"""Command line:  uv run python -m mathfinder prove --lean "(n : ℕ) : n + 0 = n"

(LaTeX input arrives in M5; for now you give the Lean statement directly.)
"""

import argparse
import sys

from mathfinder.search import search

SYMBOL = {"lemma_proved": "✓", "lemma_refuted": "✗", "lemma_stuck": "…"}


def print_event(event: dict) -> None:
    kind = event["kind"]
    if kind == "start":
        print(f"\nTheorem: {event['theorem']}")
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
    sys.stdout.flush()  # show progress immediately, even when output is piped to a file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mathfinder")
    commands = parser.add_subparsers(dest="command", required=True)
    prove = commands.add_parser("prove", help="search for a proof")
    prove.add_argument("--lean", required=True, help='Lean statement without a name, e.g. "(n : ℕ) : n + 0 = n"')
    prove.add_argument("--hint", action="append", default=[], help="a hint for the planner (repeatable)")
    prove.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args(argv)

    if args.command == "prove":
        result = search(args.lean, hints=args.hint, rounds=args.rounds, on_event=print_event)
        print(f"\nResult: {'PROVED' if result.proved else 'not proved'} ({result.message})")
        return 0 if result.proved else 1
    return 2
