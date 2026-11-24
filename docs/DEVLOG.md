# Math Finder — dev log

The plan and checklist live in `prompt.md`. This file records what each run did and how it was verified.
The original 18-iteration prototype log (M0–M13) is in git: `git show v0-prototype:PROGRESS.md`.

## Prototype facts worth remembering

- **Lean cost**: every check spawns `lake env lean` and reloads Mathlib: ~77 s cold, ~6–20 s warm. A hammer call
  (15 tactics) is ~30–60 s. Hence everything is batched into one Lean run (`check_decls`). Fix = step 2 (persistent REPL).
- **`lake env lean` ignores lakefile `leanOptions`**: `HEADER` sets `autoImplicit false` itself (a test guards it).
- **Test markers**: `-m "not lean and not llm"` is the fast set (<1 s). A command-line `-m` replaces the default
  `-m 'not llm'`, so `-m "not lean"` would call the real model.
- **API backend** was only tested with a fake client (no API key on this machine); the `cli` backend is the one used.
- **Prover cache** (`cache/lemmas.json`) trusts entries; the final `verify_final` re-checks the assembled file anyway.
- **Sub-searches** prove a stuck lemma standalone (they can't use sibling lemmas); never triggered on the 20-problem set.
- **Benchmark (2026-11-14)**: 20/20 on the self-written miniF2F-style set (`bench/results/20261114-165343.md`);
  8 by automation alone, 12 in the first planning round, 69–399 s each. Easy set: a regression test, not a skill measure.

## Log

### 2026-11-22 — Step 0: baseline under git
- `git init -b main`; extended .gitignore (*.log, lean_env/.lake/, .claude/settings.local.json, scheduler lock).
- lean_env/ had an empty nested repo from `lake new` (no commits); moved it aside to the session scratchpad so the root repo can track lean_env/.
- Committed the prototype as-is (125 files, .git 1.3 MB), tagged `v0-prototype`.
- Verified: `uv run pytest -q -m "not lean and not llm"` → `1 failed, 86 passed, 31 deselected in 0.84s`
  (failure: tests/test_guide.py — guide.html stale; the guide is removed in step 1).
- Next: step 1 (make it lean).

### 2026-11-23 — Step 1: make it lean
- Archived the tutorial guide + `training/` on branch `archive/guide-and-training`, then removed from main:
  `guide/`, `guide.html`, `tools/build_guide.py`, `tests/test_guide.py`, `training/`, `tests/test_training.py`,
  and `tools/watch.py` (needed an undeclared `websockets` dep). Kept `tools/make_demo.py` (offline web demo).
- `traces.py` is now just the event logger (training-data `export` + the `export` CLI command removed).
- `critic.py` folded into `search.py` (its only user). PROGRESS.md → this file, trimmed (full log: `git show v0-prototype:PROGRESS.md`).
- Added ruff (line 120; E,F,W,I,B,UP), fixed all findings (`l` → `lem`, `raise ... from`, `zip(strict=True)`,
  lambda default arg in prover; lint only, no behavior change), formatted everything. Type hints on all public functions (`OnEvent` alias in traces.py).
- `.env.example` documents MATHFINDER_LLM, MATHFINDER_CLI_MODEL, ANTHROPIC_API_KEY.
- Verified: `uv run ruff check .` → `All checks passed!`; `uv run pytest -q -m "not lean and not llm"` → `77 passed, 31 deselected in 0.86s`;
  `uv run pytest -q -m "lean and not llm" tests/test_planner.py tests/test_search.py tests/test_traces.py tests/test_cli.py`
  → `4 passed, 23 deselected in 135.66s`.
- Size: 94 files changed, −21.3k lines (mostly the generated guide); mathfinder/ + server/ = 2,122 lines.
- Next: step 2 (persistent Lean REPL).

### 2026-11-23 — Step 2: fast Lean (persistent REPL)
- `lean_env/lakefile.toml` requires leanprover-community/repl @ v4.36.0-rc2 (matches toolchain; `lake update REPL`
  left the Mathlib rev untouched). Build once: `cd lean_env && lake build repl`.
- `mathfinder/lean.py`: `run_lean` now sends the code (minus HEADER) to a pool of REPLs that loaded Mathlib + HEADER
  once; every check runs on that same clean env, so checks stay isolated. Line numbers are shifted back so
  `check_decls`/`verify_final` are unchanged. Timeout or crash → that REPL is killed and replaced. Recycled after
  `MATHFINDER_LEAN_RECYCLE` (300) checks; pool size `MATHFINDER_LEAN_WORKERS` (2).
- REPL format quirks: severity "info" (→ "information"); sorry has no `kind`, detected from "declaration uses `sorry`".
- Measured on the same 5-candidate file, nothing else running: `lake env lean` median 2.78 s (6 runs); warm REPL median
  74 ms (20 runs, 70–114 ms), about 38x faster. REPL startup 2.3–2.7 s warm (32 s right after the Mathlib cache refresh).
  RSS 7.46 GB → 7.71 GB after 300 small checks (~0.8 MB/check); sharing between two workers was not measured.
  (Corrected 2026-11-24: the step 2 commit message's "~20 ms instead of ~3 s" compared different inputs.)
- Tests: new parse/header/isolation/timeout-recovery tests. `uv run pytest -q -m "not lean and not llm"` → `79 passed, 32 deselected in 0.91s`;
  `uv run pytest -q -m "not llm"` → `105 passed, 6 deselected in 35.45s` (prototype: 112 passed in 1040.56s).
- Next: step 3 (safe to host).
