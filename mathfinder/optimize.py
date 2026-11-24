"""The optimizer: once we HAVE a verified proof, look for a better one.

"Better" depends on what you care about, so there are several scoring rules
(METRICS). Lower is better. Each one combines three counts taken from the Lean code:

  steps   - tactic lines (how long the proof is)
  lemmas  - helper lemmas besides `main`
  heavy   - calls to brute-force automation (nlinarith, aesop, decide, ...) that
            prove things without showing WHY

The search is bounded: one model call proposes a few rewrites of the whole proof,
Lean checks them all in one run, and the best one is kept only if it scores lower
AND passes the strict final verification. The original proof is never lost.
"""

from dataclasses import dataclass

from mathfinder import llm
from mathfinder.lean import Decl, check_decls, verify_final
from mathfinder.planner import Plan
from mathfinder.prover import parse_proofs
from mathfinder.search import NAMESPACE, OnEvent, assemble

CANDIDATES = 4
HEAVY = ("nlinarith", "polyrith", "aesop", "simp_all", "decide", "exact?")

METRICS = {
    "short": lambda s: s["steps"] + s["lemmas"],  # fewest lines overall
    "readable": lambda s: s["steps"] + s["lemmas"] + 5 * s["heavy"],  # avoid black-box automation
    "few_lemmas": lambda s: 10 * s["lemmas"] + s["steps"],  # one self-contained proof
}

DESCRIPTIONS = {
    "short": "as SHORT as possible (fewest tactic lines)",
    "readable": "as READABLE as possible: prefer explicit steps (rw, calc, have, exact lemma) over brute-force "
    "automation like nlinarith, aesop, decide or simp_all",
    "few_lemmas": "self-contained: one proof of the main theorem, using `have` instead of separate lemmas",
}

SYSTEM = """You are an expert in Lean 4 and Mathlib who polishes proofs that already work.
You get a theorem and a VERIFIED proof of it (possibly split into helper lemmas).
Write alternative proofs of the main theorem alone, each one a single tactic script
(no separate lemmas: inline them with `have` if needed). Use current Mathlib names.
Never use sorry, admit or axiom. Write ONLY the tactic script that goes after `:= by`.
Reply with ONLY a JSON object inside a ```json block: {"proofs": ["<tactic script>", ...]}"""


@dataclass
class Optimization:
    lean_code: str  # the best verified proof (maybe the original)
    plan: Plan
    metric: str
    original: dict  # stats of the original proof, including "cost"
    best: dict  # stats of the chosen proof, including "cost"
    tried: int  # how many candidates Lean checked
    improved: bool


def proof_stats(lean_code: str, metric: str = "short") -> dict:
    """Count steps, lemmas and heavy automation in an assembled proof file, and score it."""
    lines = lean_code.split("\n")
    steps = sum(1 for line in lines if line.startswith(" ") and line.strip())  # indented = inside a proof
    lemmas = sum(1 for line in lines if line.startswith("theorem ")) - 1  # every theorem except main
    heavy = sum(line.count(word) for line in lines for word in HEAVY)
    stats = {"steps": steps, "lemmas": max(lemmas, 0), "heavy": heavy}
    stats["cost"] = METRICS[metric](stats)
    return stats


def build_prompt(theorem: str, lean_code: str, metric: str) -> str:
    return (
        f"Theorem:\n  theorem main {theorem}\n\n"
        f"A verified proof:\n```lean\n{lean_code}\n```\n\n"
        f"Give {CANDIDATES} different proofs of `main` that are {DESCRIPTIONS[metric]}."
    )


def optimize(
    theorem: str, lean_code: str, plan: Plan, metric: str = "short", on_event: OnEvent | None = None
) -> Optimization:
    """Try to find a better verified proof than `lean_code` under `metric`."""

    def emit(kind: str, **data) -> None:
        if on_event:
            on_event({"kind": kind, "depth": 0, **data})

    original = proof_stats(lean_code, metric)
    keep = Optimization(lean_code, plan, metric, original, original, 0, False)
    emit("optimize_start", metric=metric, cost=original["cost"])
    try:
        proofs = parse_proofs(llm.complete_json(SYSTEM, build_prompt(theorem, lean_code, metric)))
    except llm.LLMError:
        proofs = []
    if not proofs:
        emit("optimized", improved=False, before=original, after=original, tried=0)
        return keep

    results = check_decls([Decl(f"candidate_{i}", theorem, p) for i, p in enumerate(proofs)])
    scored = []
    for i, proof in enumerate(proofs):
        if results[f"candidate_{i}"].ok:
            candidate_plan = Plan(f"Optimized ({metric})", [], proof)
            code = assemble(theorem, candidate_plan)
            scored.append((proof_stats(code, metric)["cost"], i, code, candidate_plan))
        emit("optimize_candidate", ok=results[f"candidate_{i}"].ok, proof=proof)

    for cost, _, code, candidate_plan in sorted(scored):  # cheapest first
        if cost >= original["cost"]:
            break  # nothing cheaper is left
        if verify_final(code, f"{NAMESPACE}.main").ok:
            best = proof_stats(code, metric)
            emit("optimized", improved=True, before=original, after=best, tried=len(proofs))
            return Optimization(code, candidate_plan, metric, original, best, len(proofs), True)

    emit("optimized", improved=False, before=original, after=original, tried=len(proofs))
    keep.tried = len(proofs)
    return keep
