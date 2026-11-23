"""The search: plan, check, prove, verify, and replan with what we learned.

  0. maybe the hammer proves the whole theorem directly (or refutes it)
  1. the planner proposes a few plans
  2. the critic throws out plans that don't fit together (one Lean run)
  3. remaining plans are tried cheapest-first (fewest lemmas); each lemma goes to
     the prover, and a refuted lemma kills its plan immediately
  4. a plan with every lemma proved is assembled into one file and checked by
     the strict final verification (no sorry, only standard axioms)
  5. otherwise everything that went wrong becomes feedback for the next round

Everything that happens is reported through `on_event`, so the command line,
the web page and the trace logger can all watch the same search.
"""

from dataclasses import dataclass

from mathfinder.critic import check_skeletons
from mathfinder.hammer import hammer
from mathfinder.lean import verify_final
from mathfinder.planner import Plan, propose_plans
from mathfinder.prover import prove_lemma

NAMESPACE = "MathFinder"


@dataclass
class SearchResult:
    proved: bool
    lean_code: str = ""  # the complete verified proof (everything after the file header)
    plan: Plan | None = None
    rounds: int = 0
    message: str = ""


def assemble(theorem: str, plan: Plan) -> str:
    """One Lean file: every lemma with its real proof, then the main theorem."""
    blocks = [f"namespace {NAMESPACE}"]
    for l in plan.lemmas:
        body = "\n".join("  " + line for line in l.proof.split("\n"))
        blocks.append(f"theorem {l.name} {l.statement} := by\n{body}")
    main_body = "\n".join("  " + line for line in plan.main_proof.split("\n"))
    blocks.append(f"theorem main {theorem} := by\n{main_body}")
    blocks.append(f"end {NAMESPACE}")
    return "\n\n".join(blocks) + "\n"


def score(plan: Plan) -> float:
    """Fraction of lemmas proved; a refuted lemma makes the whole plan worthless."""
    if any(l.status == "refuted" for l in plan.lemmas):
        return -1.0
    if not plan.lemmas:
        return 0.0
    return sum(l.status == "proved" for l in plan.lemmas) / len(plan.lemmas)


def summarize(plan: Plan) -> str:
    """What happened to one plan, written for the planner's next round."""
    lines = [f"Plan: {plan.sketch}  (main proof: {plan.main_proof!r})"]
    lines += [f"  - {note}" for note in plan.feedback]
    for l in plan.lemmas:
        lines.append(f"  - lemma {l.name} {l.statement}: {l.status.upper()}  {l.feedback[:300]}")
    return "\n".join(lines)


def prove_plan(plan: Plan, emit) -> bool:
    """Prove the plan's lemmas in order. Stops at the first refuted lemma."""
    for i, lemma in enumerate(plan.lemmas):
        emit("lemma_start", name=lemma.name, statement=lemma.statement)
        prove_lemma(lemma, plan.lemmas[:i])
        emit("lemma_" + lemma.status, name=lemma.name, statement=lemma.statement, feedback=lemma.feedback)
        if lemma.status == "refuted":
            return False
    return all(l.status == "proved" for l in plan.lemmas)


def try_direct(theorem: str, emit) -> SearchResult | None:
    """Step 0: maybe automation proves (or refutes) the whole theorem."""
    hit = hammer(theorem)
    if hit.counterexample:
        emit("refuted", counterexample=hit.counterexample)
        return SearchResult(False, message=f"the statement is false: {hit.counterexample}")
    if hit.proved:
        plan = Plan(f"Automation: {hit.tactic}", [], hit.proof)
        code = assemble(theorem, plan)
        if verify_final(code, f"{NAMESPACE}.main").ok:
            emit("done", lean_code=code, plan=plan.sketch)
            return SearchResult(True, code, plan, 0, "proved directly by automation")
    return None


def search(theorem: str, hints=(), rounds: int = 3, plans_per_round: int = 2, on_event=None) -> SearchResult:
    """Search for a verified proof of `theorem` (a Lean statement without a name)."""

    def emit(kind, **data):
        if on_event:
            on_event({"kind": kind, **data})

    emit("start", theorem=theorem)
    direct = try_direct(theorem, emit)
    if direct:
        return direct

    feedback = []  # what went wrong so far, fed to the planner
    best = None
    for r in range(1, rounds + 1):
        emit("round", number=r)
        plans = propose_plans(theorem, plans_per_round, list(hints), "\n".join(feedback[-6:]))
        emit("plans", sketches=[p.sketch for p in plans])
        problems = check_skeletons(theorem, plans)

        fitting = []
        for plan, found in zip(plans, problems):
            if found:
                emit("plan_rejected", sketch=plan.sketch, reason=found[0][:300])
                feedback.append(summarize(plan))
            else:
                fitting.append(plan)

        for plan in sorted(fitting, key=lambda p: len(p.lemmas)):  # cheapest first
            emit("plan_start", sketch=plan.sketch, lemmas=[l.name for l in plan.lemmas])
            if prove_plan(plan, emit):
                code = assemble(theorem, plan)
                final = verify_final(code, f"{NAMESPACE}.main")
                if final.ok:
                    emit("done", lean_code=code, plan=plan.sketch)
                    return SearchResult(True, code, plan, r, "verified")
                plan.feedback.append("final check failed: " + " | ".join(final.errors)[:400])
            emit("plan_failed", sketch=plan.sketch, score=score(plan))
            feedback.append(summarize(plan))
            if best is None or score(plan) > score(best):
                best = plan

    emit("gave_up", best=best.sketch if best else None)
    message = f"no verified proof after {rounds} rounds"
    if best:
        message += f"; closest plan proved {score(best):.0%} of its lemmas"
    return SearchResult(False, plan=best, rounds=rounds, message=message)
