"""Command line.

  mathfinder prove "For every integer $n$, $6 \\mid n^3 - n$."
  mathfinder prove --lean "(n : ℤ) : 6 ∣ n ^ 3 - n"
  mathfinder bench

With LaTeX, you first see the Lean statement, its back-translation and the judge's
opinion, and nothing is searched until you approve.
"""

import argparse
from collections.abc import Callable

from mathfinder.bench import load_problems, run_bench
from mathfinder.lean import statement_errors
from mathfinder.optimize import METRICS
from mathfinder.pipeline import prove
from mathfinder.translate import formalize

LEMMA = "{name} : {statement}   [{feedback:.80}]"
MESSAGES = {
    "start": "\nTheorem: {theorem}",
    "refuted": "✗ The statement is FALSE. Counterexample: {counterexample}",
    "round": "\n— round {number}: asking the planner —",
    "plan_rejected": "  ✗ plan doesn't fit together: {sketch}\n      {reason:.160}",
    "plan_start": "  ▶ plan: {sketch}  (lemmas: {lemmas})",
    "plan_failed": "    plan failed (score {score:.2f})",
    "lemma_proved": "      ✓ " + LEMMA,
    "lemma_refuted": "      ✗ " + LEMMA,
    "lemma_stuck": "      … " + LEMMA,
    "subsearch_start": "      ↳ {name} is stuck: starting a sub-search on it",
    "done": "\n✓ VERIFIED by Lean. Plan: {plan}",
    "gave_up": "\nNo verified proof found.",
    "optimize_start": "Looking for a better proof ({metric}, current cost {cost})...",
    "optimized": "  cost {before[cost]} → {after[cost]}",
    "explaining": "Writing the explanation...",
}
SUBSEARCH_MESSAGES = {
    "plan_start": "▶ sub-plan: {sketch}",
    "done": "✓ sub-search proved it",
    "lemma_proved": "✓ " + LEMMA,
    "lemma_refuted": "✗ " + LEMMA,
    "lemma_stuck": "… " + LEMMA,
}


def print_event(event: dict) -> None:
    depth = event.get("depth", 0)  # events from a sub-search are indented under their lemma
    template = (SUBSEARCH_MESSAGES if depth else MESSAGES).get(event["kind"])
    if template:
        lemmas = ", ".join(event.get("lemmas", [])) or "none"
        print("        " * depth + template.format(**{**event, "lemmas": lemmas}), flush=True)


def approve(latex: str, auto_yes: bool = False, ask: Callable[[str], str] = input) -> str | None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mathfinder")
    commands = parser.add_subparsers(dest="command", required=True)
    prove_cmd = commands.add_parser("prove", help="search for a proof")
    prove_cmd.add_argument("latex", nargs="?", default="", help="the theorem in LaTeX (in quotes)")
    prove_cmd.add_argument("--lean", help='skip formalization: a Lean statement, e.g. "(n : ℕ) : n + 0 = n"')
    prove_cmd.add_argument("--yes", action="store_true", help="don't ask for approval if the judge is happy")
    prove_cmd.add_argument("--hint", action="append", default=[], help="a hint for the planner (repeatable)")
    prove_cmd.add_argument("--rounds", type=int, default=3)
    prove_cmd.add_argument("--no-explain", action="store_true", help="skip the plain-math explanation")
    prove_cmd.add_argument("--metric", choices=sorted(METRICS), default="short", help="what 'better proof' means")
    prove_cmd.add_argument("--no-optimize", action="store_true", help="keep the first proof found")
    bench_cmd = commands.add_parser("bench", help="run the benchmark in bench/problems.jsonl")
    bench_cmd.add_argument("--rounds", type=int, default=2, help="planning rounds per problem")
    bench_cmd.add_argument("--only", nargs="*", default=[], help="names of problems to run (default: all)")
    args = parser.parse_args(argv)

    if args.command == "bench":
        problems = [p for p in load_problems() if not args.only or p["name"] in args.only]
        path = run_bench(problems, args.rounds)
        print(f"\n{path.read_text()}\nSaved {path}")
        return 0

    if not (args.lean or args.latex):
        parser.error("give a theorem in LaTeX, or --lean STATEMENT")
    statement = args.lean or approve(args.latex, args.yes)
    if statement is None:
        print("Not searching.")
        return 1
    outcome = prove(
        statement,
        args.latex,
        args.hint,
        args.rounds,
        metric=None if args.no_optimize else args.metric,
        explained=not args.no_explain,
        on_event=print_event,
    )
    print(f"\nResult: {'PROVED' if outcome['proved'] else 'not proved'} ({outcome['message']})")
    if outcome["proved"]:
        print(f"\n{outcome['lean_code']}\n{outcome['explanation']}")
    print(f"Saved in {outcome['folder']}")
    return 0 if outcome["proved"] else 1
