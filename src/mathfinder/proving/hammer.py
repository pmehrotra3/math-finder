"""The hammer: try many automatic tactics at once, and look for a counterexample. No AI.

Many small lemmas fall to a single tactic like `omega` or `nlinarith`, and one Lean run
costs far less than a model call. In the same run `plausible` tests the statement on
random values, so nobody wastes time proving something false.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from mathfinder.lean.checker import Decl, check_decls

# Roughly from fastest/most specific to slowest/most general.
TACTICS = [
    "norm_num",
    "simp",
    "omega",
    "decide",
    "linarith",
    "nlinarith",
    "positivity",
    "ring_nf",
    "field_simp",
    "aesop",
    "tauto",
    "simp_all",
    "norm_num <;> nlinarith",  # simplify the numbers first, then nonlinear arithmetic
    "exact?",  # searches the whole library: slowest, so last
]
HAMMER_HEARTBEATS = 100_000  # less fuel than normal: if a tactic doesn't work fast, it won't work


@dataclass
class HammerResult:
    proved: bool
    tactic: str = ""  # the tactic that worked
    proof: str = ""  # proof text to keep (for exact?, the lemma it found)
    counterexample: str = ""  # e.g. "n := 4; issue: 16 ≤ 11 does not hold"


def hammer(
    statement: str,
    preamble: str = "",
    tactics: Sequence[str] = TACTICS,
) -> HammerResult:
    """Try every tactic on `statement`, plus a counterexample search, in ONE Lean run."""
    decls = [Decl(f"mf_hammer_{i}", statement, tac) for i, tac in enumerate(tactics)]
    decls.append(Decl("mf_refute", statement, "plausible"))
    results = check_decls(decls, preamble, heartbeats=HAMMER_HEARTBEATS)

    out = HammerResult(proved=False)
    for i, tac in enumerate(tactics):
        if results[f"mf_hammer_{i}"].ok:
            out.proved = True
            out.tactic = tac
            out.proof = tac
            if tac == "exact?":  # keep the lemma it found, not the slow search
                out.proof = suggestion(results[f"mf_hammer_{i}"].infos) or tac
            break

    for err in results["mf_refute"].errors:
        if "Found a counter-example!" in err:
            out.counterexample = counterexample_text(err)
    return out


def counterexample_text(error: str) -> str:
    """Turn plausible's report into one line, e.g. 'n := 4; issue: 16 ≤ 11 does not hold'."""
    after = error.split("Found a counter-example!", 1)[1]
    lines = []
    for line in after.strip().split("\n"):
        if line.startswith("---") or line.startswith("("):  # end marker / "(0 shrinks)"
            break
        lines.append(line.strip())
    return "; ".join(lines)


def suggestion(infos: list[str]) -> str:
    """Read 'Try this: [apply] exact Nat.add_comm a b' and return 'exact Nat.add_comm a b'."""
    for text in infos:
        if text.startswith("Try this:"):
            found = text.split(":", 1)[1].strip()
            return found.removeprefix("[apply]").strip()
    return ""
