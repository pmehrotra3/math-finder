"""Math Finder: an automated reasoning system that turns theorems into machine-checked Lean 4 proofs.

Start with pipeline.py, then follow a theorem through the folders:
translation/ → proving/ → lean/. The interface/ folder is what people use.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEAN_DIR = PROJECT_ROOT / "lean_env"  # the Lean project (Lean 4 + Mathlib) every proof is checked in
