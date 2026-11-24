"""Math Finder: an automated reasoning system that turns theorems into machine-checked Lean 4 proofs."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEAN_DIR = PROJECT_ROOT / "lean_env"  # the Lean project (Lean 4 + Mathlib) every proof is checked in
