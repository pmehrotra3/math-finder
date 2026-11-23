"""Command line.

  uv run python -m mathfinder prove "For every integer $n$, $6 \\mid n^3 - n$."
  uv run python -m mathfinder prove --lean "(n : ℤ) : 6 ∣ n ^ 3 - n"

With LaTeX, you first see the Lean statement, its back-translation and the
judge's opinion, and nothing is searched until you approve.
"""

import argparse
import sys

from mathfinder.formalize import formalize
from mathfinder.lean import statement_errors
from mathfinder.search import search

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mathfinder")
    commands = parser.add_subparsers(dest="command", required=True)
    prove = commands.add_parser("prove", help="search for a proof")
    prove.add_argument("latex", nargs="?", help="the theorem in LaTeX (in quotes)")
    prove.add_argument("--lean", help='skip formalization: a Lean statement, e.g. "(n : ℕ) : n + 0 = n"')
    prove.add_argument("--yes", action="store_true", help="don't ask for approval if the judge is happy")
    prove.add_argument("--hint", action="append", default=[], help="a hint for the planner (repeatable)")
    prove.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args(argv)

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
        result = search(statement, hints=args.hint, rounds=args.rounds, on_event=print_event)
        print(f"\nResult: {'PROVED' if result.proved else 'not proved'} ({result.message})")
        return 0 if result.proved else 1
    return 2
