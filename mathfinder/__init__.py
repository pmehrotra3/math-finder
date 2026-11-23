"""Math Finder: a collaborative AI theorem prover.

You give it a theorem (in LaTeX). It formalizes the theorem in Lean 4,
searches for a proof with an LLM planner + a Lean-checked critic,
and explains the verified proof back to you in plain math.
"""

from pathlib import Path

__version__ = "0.1.0"

# The project root is the folder that contains this package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The Lean project (Lean 4 + Mathlib) that every proof is checked in.
LEAN_DIR = PROJECT_ROOT / "lean_env"
