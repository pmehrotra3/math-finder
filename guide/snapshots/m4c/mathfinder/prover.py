"""The prover: get ONE lemma proved, as cheaply as possible.

  1. cache   - proved this exact lemma before? reuse it, free
  2. hammer  - a dozen automatic tactics in one Lean run, no AI
               (also finds counterexamples: then the lemma is false, stop)
  3. LLM     - ask Claude for several proofs, check them all in ONE Lean run
  4. repair  - show Claude its failed proofs WITH Lean's error messages, ask for fixes

The lemma ends up "proved" (with a proof), "refuted" (with a counterexample) or
"stuck" (with the most useful Lean error, so the planner can rethink).
"""

import json

from mathfinder import PROJECT_ROOT, llm
from mathfinder.hammer import hammer
from mathfinder.lean import Decl, check_decls
from mathfinder.planner import Lemma

CACHE_FILE = PROJECT_ROOT / "cache" / "lemmas.json"
ATTEMPTS = 4  # proofs asked for per LLM call
REPAIR_ROUNDS = 2  # extra rounds that see Lean's errors
MAX_ERROR = 600  # characters of each Lean error shown to the model

SYSTEM = """You are an expert in Lean 4 and Mathlib. You write tactic proofs that compile.
Rules:
- Lean 4 syntax, current Mathlib lemma names (e.g. Finset.sum_range_succ, sq_nonneg, Nat.succ_le_iff).
- Write ONLY the tactic script that goes after `:= by` (do not write `by` or the statement).
- Never use sorry, admit, or axiom.
- You may use the earlier lemmas listed, by name.
- Each proof should be a genuinely different approach.
Reply with ONLY a JSON object inside a ```json block: {"proofs": ["<tactic script>", ...]}"""


# ---------- cache ----------


def cache_key(lemma: Lemma, earlier: list[Lemma]) -> str:
    """Same statement AND same available lemmas = same problem. Whitespace doesn't matter."""
    context = " ; ".join(f"{l.name} {l.statement}" for l in earlier)
    return " ".join(f"{context} ⊢ {lemma.statement}".split())


def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_to_cache(key: str, proof: str) -> None:
    cache = load_cache()
    cache[key] = proof
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


# ---------- talking to the model ----------


def preamble_for(earlier: list[Lemma]) -> str:
    """Earlier lemmas the proof may use: real proofs where we have them, sorry otherwise.

    They go inside `namespace MathFinder` (left open, which Lean allows at the end of
    a file), so a plan lemma called e.g. `sq_nonneg` can't clash with Mathlib's.
    """
    if not earlier:
        return ""
    parts = []
    for l in earlier:
        proof = l.proof if l.status == "proved" else "sorry"
        body = "\n".join("  " + line for line in proof.split("\n"))
        parts.append(f"theorem {l.name} {l.statement} := by\n{body}")
    return "namespace MathFinder\n\n" + "\n\n".join(parts)


def build_prompt(lemma: Lemma, earlier: list[Lemma], failures: list[tuple[str, str]] = ()) -> str:
    parts = [f"Prove this lemma:\n  theorem {lemma.name} {lemma.statement}"]
    if lemma.informal:
        parts.append(f"In words: {lemma.informal}")
    if earlier:
        parts.append("Earlier lemmas you may use:\n" + "\n".join(f"  theorem {l.name} {l.statement}" for l in earlier))
    for proof, error in failures:
        parts.append(f"This attempt FAILED:\n{proof}\nLean said:\n{error[:MAX_ERROR]}")
    if failures:
        parts.append("Fix these mistakes. Use the error messages.")
    parts.append(f"Give {ATTEMPTS} different proofs.")
    return "\n\n".join(parts)


def parse_proofs(data) -> list[str]:
    """Keep only usable proof strings; strip a leading `by` the model may add."""
    proofs = []
    raw = data.get("proofs", []) if isinstance(data, dict) else []
    for p in raw:
        if not isinstance(p, str) or not p.strip():
            continue
        p = p.strip()
        if p.startswith("by"):
            p = p[2:].strip()
        if any(word in p for word in ("sorry", "admit", "axiom")):
            continue
        proofs.append(p)
    return proofs


def ask_for_proofs(lemma: Lemma, earlier: list[Lemma], failures=()) -> list[str]:
    try:
        return parse_proofs(llm.complete_json(SYSTEM, build_prompt(lemma, earlier, failures)))
    except llm.LLMError:
        return []  # one bad model answer shouldn't end the search


def try_proofs(lemma: Lemma, proofs: list[str], preamble: str):
    """Check all proofs in one Lean run. Returns (working proof or None, [(proof, error), ...])."""
    decls = [Decl(f"attempt_{i}", lemma.statement, p) for i, p in enumerate(proofs)]
    results = check_decls(decls, preamble)
    failures = []
    for i, p in enumerate(proofs):
        result = results[f"attempt_{i}"]
        if result.ok:
            return p, []
        failures.append((p, "\n".join(result.errors) or "proof uses sorry"))
    return None, failures


# ---------- the whole pipeline ----------


def prove_lemma(lemma: Lemma, earlier: list[Lemma] = (), use_cache: bool = True) -> Lemma:
    """Try to prove `lemma` (which may use the `earlier` lemmas). Updates and returns it."""
    earlier = list(earlier)
    key = cache_key(lemma, earlier)
    if use_cache and key in load_cache():
        lemma.status, lemma.proof, lemma.feedback = "proved", load_cache()[key], "from cache"
        return lemma

    preamble = preamble_for(earlier)
    hit = hammer(lemma.statement, preamble)
    if hit.proved:
        lemma.status, lemma.proof, lemma.feedback = "proved", hit.proof, f"hammer: {hit.tactic}"
        save_to_cache(key, hit.proof)
        return lemma
    if hit.counterexample:
        lemma.status, lemma.feedback = "refuted", f"counterexample: {hit.counterexample}"
        return lemma

    failures = []
    for round_number in range(1 + REPAIR_ROUNDS):
        proofs = ask_for_proofs(lemma, earlier, failures[:2])  # show the model at most 2 failures
        if not proofs:
            continue
        found, failures = try_proofs(lemma, proofs, preamble)
        if found:
            how = "LLM" if round_number == 0 else f"LLM after {round_number} repair round(s)"
            lemma.status, lemma.proof, lemma.feedback = "proved", found, how
            save_to_cache(key, found)
            return lemma

    lemma.status = "stuck"
    lemma.feedback = failures[0][1][:MAX_ERROR] if failures else "no usable proof attempts"
    return lemma
