"""The critic: let Lean judge a plan before anyone tries to prove its lemmas.

Check 1 (this file, so far): does the plan even fit together? We put each lemma in
as a theorem with `sorry` as its proof, then check the main proof. If that
fails, the plan is broken no matter how the lemmas get proved: a lemma
statement doesn't type-check, a name is wrong, or the lemmas aren't enough.
All plans are checked in ONE Lean run.
"""

from mathfinder.lean import Decl, check_decls
from mathfinder.planner import Plan

FORBIDDEN = ("sorry", "admit", "axiom")


def lemma_context(plan: Plan) -> str:
    """The plan's lemmas as Lean theorems with `sorry` proofs: "pretend these are proved"."""
    return "\n\n".join(f"theorem {l.name} {l.statement} := by\n  sorry" for l in plan.lemmas)


def check_skeletons(theorem: str, plans: list[Plan]) -> list[list[str]]:
    """For each plan, the list of problems with its skeleton (empty list = the plan fits together)."""
    problems = [[] for _ in plans]
    decls = []
    for i, plan in enumerate(plans):
        cheat = [w for w in FORBIDDEN if w in plan.main_proof]
        if cheat:
            problems[i].append(f"main proof uses forbidden {cheat}")
            continue
        decls.append(Decl(f"plan_{i}", theorem, plan.main_proof, context=lemma_context(plan)))

    if decls:
        results = check_decls(decls)
        for i in range(len(plans)):
            result = results.get(f"plan_{i}")
            if result is not None:
                # The lemmas use sorry on purpose, so only real errors count here.
                problems[i].extend(result.errors)

    for plan, found in zip(plans, problems):
        if found:
            plan.feedback.append("skeleton does not compile: " + " | ".join(e[:300] for e in found))
    return problems
