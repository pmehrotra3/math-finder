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
  late-binding lambda in prover), formatted everything. Type hints on all public functions (`OnEvent` alias in traces.py).
- `.env.example` documents MATHFINDER_LLM, MATHFINDER_CLI_MODEL, ANTHROPIC_API_KEY.
- Verified: `uv run ruff check .` → `All checks passed!`; `uv run pytest -q -m "not lean and not llm"` → `77 passed, 31 deselected in 0.86s`;
  `uv run pytest -q -m "lean and not llm" tests/test_planner.py tests/test_search.py tests/test_traces.py tests/test_cli.py`
  → `4 passed, 23 deselected in 135.66s`.
- Size: 94 files changed, −21.3k lines (mostly the generated guide); mathfinder/ + server/ = 2,122 lines.
- Next: step 2 (persistent Lean REPL).
