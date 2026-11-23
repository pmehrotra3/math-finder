# Math Finder — progress log

Each loop iteration: read this file, do the next unchecked item, verify it, update the guide
(`guide/chapters/*.html`, then `uv run python tools/build_guide.py`), and log here.

## Milestones

- [x] M0 venv, pyproject, PROGRESS.md, guide.html skeleton
- [x] M1 lean.py + tests
- [x] M2 hammer + refutation
- [x] M3 LLM backends
- [x] M4 planner/critic/prover/search, end-to-end CLI on 5 easy problems (5/5 proved)
  - [x] M4a planner + critic skeleton check
  - [x] M4b prover (hammer → LLM attempts → repair) + lemma cache
  - [x] M4c search + cli, end-to-end on 5 problems
  - [x] M4d recursive decomposition of stuck lemmas
- [x] M5 LaTeX formalization + approval
- [x] M6 explanation
- [x] M7 collab hints + ask-the-user
- [x] M8 traces + export
- [x] M9 FastAPI server + WebSocket
- [x] M10 web UI
- [x] M11 proof optimization
- [x] M12 benchmark (~20 problems) + results table (20/20)
- [x] M13 training scaffolding

## How the guide works

- Guide source: `guide/template.html` (layout, CSS, JS) + `guide/chapters/NN-name.html` (one per chapter, sorted by filename).
- In a chapter, `{{file:relative/path}}` is replaced by the real file contents at build time, so the guide's code always matches.
  To show the literal tag in prose, write `&#123;&#123;file:...&#125;&#125;`.
- Each `<li class="step">` gets a remembered checkbox; each `<section id=".." data-title="..">` gets a sidebar entry.
- `tests/test_guide.py` fails if `guide.html` is stale. Rebuild: `uv run python tools/build_guide.py`.
- **Snapshots:** if a later milestone changes a file that an earlier chapter shows, first copy the OLD version to
  `guide/snapshots/<milestone>/<same path>` and change the earlier chapter's tag to `{{file:path@<milestone>}}`.
  The newest chapter that touches the file shows the live version (whole file + "what changed" note).
  Tooling (`tools/build_guide.py`, `tests/test_guide.py`) is not snapshotted; it's always shown live.
- When a milestone is done: add its chapter file (e.g. `03-m1.html`), remove its row from `99-next.html`, mark its tag `ok` in the folder tree in `00-map.html`.

## Log

### Iteration 1 — M0 (2026-11-14)
Done:
- Installed Lean via `brew install elan-init`. (Running the curl installer script was blocked by the permission classifier; Homebrew worked.)
- `lake +leanprover-community/mathlib4:lean-toolchain new lean_env math` + `lake exe cache get` → Lean v4.36.0-rc2, Mathlib cache ready.
- `pyproject.toml` (anthropic dep, pytest dev group, `llm` marker skipped by default), `mathfinder/__init__.py` (PROJECT_ROOT, LEAN_DIR), `.gitignore`.
- Tests: `tests/test_smoke.py`, `tests/test_guide.py`.
- Guide: template + chapters 00-map, 01-prereqs, 02-m0, 99-next; builder `tools/build_guide.py`.

Verified:
- `uv run pytest -q` → `4 passed in 0.01s`
- `cd lean_env && lake env lean --json .mf_tmp/hello.lean` (a²+b² ≥ 2ab via `nlinarith [sq_nonneg (a - b)]`, plus `#eval 2 + 2`) → only message: `"data":"4","severity":"information"`, exit 0.
- Guide JS passes `node --check`; guide.html has 4 sections, 16 steps.

Known issues / notes:
- **Lean is slow per process**: cold first run 77 s, warm run ~21 s, almost all of it loading Mathlib. So M1 must batch many declarations per file. Later optimization idea: keep Mathlib loaded in a persistent process (leanprover-community/repl, or Pantograph) — consider after M4 works.
- LLM backend check: `claude -p --system-prompt ... --tools "" --no-session-persistence --strict-mcp-config --setting-sources "" --output-format text` with prompt on stdin works (~3.5 s for a trivial reply).
- Project is not a git repo.

Next: M1.

### Iteration 2 — M1 (2026-11-14)
Done:
- `mathfinder/lean.py`: `run_lean` (temp file in `lean_env/.mf_tmp`, `lake env lean --json`, process-group kill on timeout,
  semaphore MAX_PARALLEL=2), `parse_messages`, `Decl.render` (maxHeartbeats per decl), `check_decls` (many decls per file,
  line-range → owner mapping, preamble errors fail everything, `hasSorry` = not ok), `statement_errors`, `verify_final`
  (`#print axioms`, only propext / Classical.choice / Quot.sound allowed).
- `tests/test_lean.py`: 3 fast + 10 Lean tests (batch fixture shared via scope="module").
- pyproject: added `lean` marker. Guide: builder supports `@snapshot` tags; M0 shows `guide/snapshots/m0/pyproject.toml`; new chapter `03-m1.html`.

Verified:
- Probed real Lean output formats first: sorry → `kind:"hasSorry"`, `"declaration uses \`sorry\`"`; axioms →
  `'good' depends on axioms: [propext, Classical.choice, Quot.sound]`, sorry shows as `sorryAx`; positions are 1-based lines.
- `uv run pytest -q` → `17 passed in 29.56s`. `uv run pytest -q -m "not lean" tests/test_lean.py` → `3 passed, 10 deselected`.

Bug found & fixed: `lake env lean` does NOT apply lakefile `leanOptions`, so autoImplicit was ON. A statement mentioning
`undefined_thing` silently got it as an implicit variable. HEADER now has `set_option autoImplicit false`; a test guards it.

Known issues / notes:
- Warm Lean runs are now ~6–8 s each (the whole test file with 5 Lean runs took ~30 s). Cold start is much slower.
- Lakefile options (`maxSynthPendingDepth = 3`, etc.) also aren't applied to our checks; only matters if a Mathlib
  tactic needs them. Add to HEADER if we see synthesis failures.

Next: M2.

### Iteration 3 — M2 (2026-11-14, ~12:15–12:45)
Note: no iterations ran between 04:06 and 12:11. The 20-min cron fires piled up unprocessed while the session sat
(probably not idle-eligible) and were delivered all at once with the user's next message. Cron job 61ff8dc4 is still listed.

Done:
- `mathfinder/hammer.py`: `hammer(statement, preamble, tactics)` runs 14 tactics + a `plausible` refutation as
  candidates in ONE Lean run (HAMMER_HEARTBEATS=100k); first success in list order wins; `exact?` result replaced by
  its "Try this" suggestion; `counterexample_text`, `suggestion` parsers. `testable=False` when plausible can't sample (ℝ).
- `tests/test_hammer.py`: 3 fast parser tests (real Lean strings) + 4 Lean tests.
- Guide: new chapter `04-m2.html`; M1 chapter now shows snapshots `guide/snapshots/m1/{mathfinder/lean.py,tests/test_lean.py}`.

**Soundness bug found & fixed in lean.py:** candidates were rendered as `theorem mf_hammer_i ...`. A failed theorem
is still added to the environment (elaborated with sorryAx), so `exact?` "proved" a FALSE statement by citing
`mf_hammer_0`. Candidates are now `example` (checked but never added to the environment) with a `-- name` comment.
Regression test: `test_candidates_cannot_cite_each_other`. Preamble lemmas are still real theorems (intended).
Also added `DeclResult.infos` (information messages).

Verified:
- Probed real output: plausible counterexample = error "Found a counter-example!\nn := 4\nissue: 16 ≤ 11 does not hold\n(0 shrinks)";
  no counterexample = info "Unable to find a counter-example" + sorry warning; ℝ = error "Failed to create a `testable` instance";
  exact? = info "Try this:\n  [apply] exact Nat.add_comm a b".
- By hand: `hammer('(n : ℕ) (h : n > 3) : 2 * n < n * n')` → proved=True, tactic='nlinarith';
  `hammer('(n : ℕ) : n ^ 2 ≤ 2 * n + 3')` → counterexample='n := 4; issue: 16 ≤ 11 does not hold'.
- `uv run pytest -q` → `25 passed in 177.00s`; `uv run pytest -q -m "not lean"` → `10 passed, 15 deselected`.

Known issues / notes:
- A hammer call takes ~30–60 s (15 candidates; `exact?` and `aesop` are the slow ones). Fine for now; a persistent
  Lean REPL would cut this a lot (still the main perf TODO).
- plausible can't test ℝ statements; a future idea is to also try the statement specialized to ℚ / small ℤ values.
- The exact? suggestion text isn't re-verified separately; final `verify_final` covers it.

Next: M3.

### Iteration 4 — M3 (2026-11-14)
Done:
- `mathfinder/llm.py`: `complete(system, prompt)` dispatching via `BACKENDS` dict on env `MATHFINDER_LLM` (default "cli");
  `complete_cli` (claude -p, prompt on stdin, cwd=tempdir, timeout 600 s, optional `MATHFINDER_CLI_MODEL`);
  `complete_api` (beta stream, claude-opus-5-5, max_tokens 64k, effort high, fallback beta + fallbacks="default",
  refusal → LLMError); `extract_json` (fenced or bare); `complete_json` (one retry quoting the parse error); `LLMError`.
- `tests/test_llm.py`: 8 fast tests (fake backend via monkeypatch.setitem(BACKENDS), fake API client recording
  the kwargs, refusal path) + 1 `llm` test of the real CLI.
- Guide chapter `05-m3.html`.

Verified:
- Installed SDK is anthropic 2.8.0 (uv resolved the latest); `beta.messages.stream` signature includes fallbacks,
  output_config, betas.
- `uv run pytest -q -m llm tests/test_llm.py` → `1 passed, 8 deselected in 4.03s` (real claude CLI call).
- Real `complete_json(...)` for a²+b² ≥ 2ab → `{'tactic': 'nlinarith [sq_nonneg (a - b)]'}` in ~4 s.
- `uv run pytest -q` → `33 passed, 1 deselected in 168.87s`; `-m "not lean and not llm"` → `18 passed, 16 deselected`.

Known issues / notes:
- API backend NOT tested live: no ANTHROPIC_API_KEY and no `ant` CLI on this machine. Request shape is tested with a
  fake client only.
- Gotcha (documented in the M3 chapter): a command-line `-m` REPLACES the default `-m 'not llm'` from addopts, so
  `-m "not lean"` runs the llm test too. Use `-m "not lean and not llm"` for fast-only.

Next: M4, the big one; split over several iterations:
  M4a `planner.py` (prompt → JSON plans: sketch, lemmas [{name, statement, informal}], main_proof) +
      `critic.py` skeleton check (lemmas as sorried preamble theorems, main proof must compile) with fake-LLM tests.
  M4b `prover.py` (hammer → k LLM attempts in one batch → repair rounds with Lean errors; JSON cache file).
  M4c `search.py` (best-first, events callback, recursive decomposition) + `cli.py prove --lean "<statement>"`
      run end-to-end on the 5 problems.

### Iteration 5 — M4a (2026-11-14)
Done:
- lean.py: `Decl.context` (optional Lean code private to one candidate, wrapped with the example in
  `namespace <name> ... end <name>`). Snapshots for the M2 chapter: `guide/snapshots/m2/{mathfinder/lean.py,tests/test_lean.py}`.
- `mathfinder/planner.py`: `Lemma`, `Plan` dataclasses; SYSTEM prompt (statement format = what follows `theorem name`,
  small true lemmas, different ideas, follow user hints, JSON in ```json); `build_prompt` (hints + feedback sections);
  `parse_plans` (drops malformed plans, invalid/duplicate names); `propose_plans(theorem, n, hints, feedback)`.
- `mathfinder/critic.py`: `lemma_context` (lemmas as sorried theorems) + `check_skeletons(theorem, plans)`:
  one Lean run for all plans; only errors count (sorry expected); text scan rejects sorry/admit/axiom in main proof;
  appends feedback to `plan.feedback`.
- `tests/test_planner.py`: 5 fast + 1 Lean (4 plans in one run: ok / unknown lemma / ill-typed lemma / too-weak lemma)
  + 1 llm. Guide chapter `06-m4a.html`.

Verified:
- Real run for a²+b² ≥ 2ab: Claude gave 2 different plans (direct `nlinarith [sq_nonneg (a - b)]`; and
  diff_eq_sq + diff_nonneg then `linarith`). Both skeletons OK. 1m13s total.
- `uv run pytest -q -m llm tests/test_planner.py` → `1 passed, 6 deselected in 14.15s`.
- `uv run pytest -q` → `40 passed, 2 deselected in 157.44s`; fast-only → `24 passed, 18 deselected`.

Known issues / notes:
- Skeleton errors aren't attributed to a specific lemma (just the Lean error text). Good enough for feedback now.
- Lemmas are sorried theorems inside the plan namespace, so lemma i may use lemma j<i (intended) but the main theorem
  statement can't refer to plan-local defs (plans don't define anything; fine).

Next: M4b — `mathfinder/prover.py`: `prove_lemma(lemma, earlier_lemmas)`:
  1. cache lookup (JSON file `cache/lemmas.json`, key = normalized statement) → 2. hammer (preamble = earlier lemmas
  as sorried theorems... NOTE: for the final proof earlier lemmas get real proofs) → 3. if counterexample: status
  refuted → 4. k LLM attempts in ONE check_decls batch → 5. up to r repair rounds feeding back Lean errors →
  status proved/stuck + feedback. Fake-LLM fast tests + one Lean test where the fake LLM returns a known-good proof.

### Iteration 6 — M4b (2026-11-14)
Done:
- `mathfinder/prover.py`: `prove_lemma(lemma, earlier)` ladder: cache (`cache/lemmas.json`, key = earlier lemma
  names+statements ⊢ statement, whitespace-normalized) → hammer (preamble = earlier lemmas, real proof or sorry) →
  counterexample ⇒ "refuted" → ATTEMPTS=4 LLM proofs checked in one batch → REPAIR_ROUNDS=2 with up to 2
  (failed proof, Lean error ≤600 chars) pairs in the prompt → "stuck" with the first error as feedback.
  `parse_proofs` drops sorry/admit/axiom and non-strings, strips leading `by`. LLMError ⇒ empty attempt list.
- `tests/test_prover.py`: 4 fast, 2 Lean with a scripted fake model (refute without model call; wrong→repair→right,
  then cache hit with no model call), 1 llm. autouse temp cache.
- `.gitignore` += `cache/` (M0 chapter now shows `guide/snapshots/m0/.gitignore`). Guide chapter `07-m4b.html`.

Verified:
- `uv run pytest -q tests/test_prover.py -m "lean or llm or not lean"` → `7 passed in 72.27s` (incl. real Claude).
- By hand: odd-sum lemma → `proved | LLM`, proof = induction / Finset.sum_range_succ / ring (38 s); rerun →
  `from cache` in 0.09 s.
- `uv run pytest -q` → `46 passed, 3 deselected in 131.06s`; fast-only → `28 passed, 21 deselected`.

Known issues / notes:
- The cache trusts its entries (no re-check). Final verification in M4c must re-check the assembled file anyway.
- Hammer always runs before the LLM, ~30–60 s even when the lemma obviously needs induction. Could skip later via
  the planner/critic's judgment.

Next: M4c — `mathfinder/search.py`: `search(theorem, hints, budget, on_event)`:
  propose plans → check_skeletons → for each plan that fits, prove lemmas in order with prove_lemma (skip a plan as
  soon as one lemma is refuted) → best-first by score = proved fraction → if all proved: assemble the file
  (lemmas as real theorems + `theorem main ... := by <main_proof>`), `verify_final`, done. Else replan with all
  feedback (round limit). Recursive decomposition of stuck lemmas (depth 1) can be M4d if time runs out.
  + `mathfinder/cli.py` / `__main__.py`: `python -m mathfinder prove --lean "<statement>"` printing events.
  Run on the 5 problems; record results in PROGRESS.md.

### Iteration 7 — M4c (2026-11-14)
Done:
- prover.py: `preamble_for` now wraps earlier lemmas in an (unclosed) `namespace MathFinder`, so plan lemma names
  can't clash with Mathlib (e.g. a plan lemma called `sq_nonneg`). Verified Lean accepts an unclosed namespace at EOF.
  Snapshots for the M4b chapter: `guide/snapshots/m4b/{mathfinder/prover.py,tests/test_prover.py}`.
- `mathfinder/search.py`: `search(theorem, hints, rounds=3, plans_per_round=2, on_event)`: try_direct (hammer on the
  whole theorem: proof or counterexample) → per round: propose_plans (with last 6 feedback summaries) →
  check_skeletons → fitting plans cheapest-first → prove_plan (stop at refuted) → assemble (namespace MathFinder,
  real lemma proofs, `theorem main`) → verify_final("MathFinder.main"). Events: start, refuted, round, plans,
  plan_rejected, plan_start, lemma_start, lemma_proved/refuted/stuck, plan_failed, done, gave_up.
- `mathfinder/cli.py` (`prove --lean ... --hint ... --rounds N`, exit code 0 iff proved) + `__main__.py`.
- `tests/test_search.py`: 3 fast + 2 Lean (false theorem refuted with no model call; full scripted search with a
  2-lemma plan: hammer proves one, fake model the other, final check passes). Guide chapter `08-m4c.html`.

Verified — real end-to-end run, `runs/m4c/run.sh` (claude CLI backend, logs in runs/m4c/p*.log):

| # | Theorem | Result | Time | How |
|---|---------|--------|------|-----|
| 1 | a²+b² ≥ 2ab (ℝ) | PROVED | 18 s | direct automation: `exact?` → `exact two_mul_le_add_sq a b` |
| 2 | Σ_{i<n} (2i+1) = n² | PROVED | 37 s | round 1: critic rejected a broken plan (type mismatch); 0-lemma induction plan verified |
| 3 | 6 ∣ n³ − n (ℤ) | PROVED | 54 s | lemma mod_six_cases (hammer: omega) + rcases into 6 cases `<;> simp [Int.sub_emod, Int.mul_emod, ...]` |
| 4 | √2 irrational | PROVED | 16 s | direct automation: `norm_num` |
| 5 | AM-GM 2 vars: √(ab) ≤ (a+b)/2 | PROVED | 62 s | lemma amgm_sq (LLM: `nlinarith [sq_nonneg (a - b)]`) + `Real.sqrt_le_iff` |

- CLI on a false statement → "✗ The statement is FALSE. Counterexample: n := 4; ..." exit 1 (no planner call).
- `uv run pytest -q` → `51 passed, 3 deselected in 198.20s`.

Known issues / notes:
- Problem 2's cache: the odd-sum lemma was in cache/lemmas.json from M4b, but the winning plan had 0 lemmas, so the
  cache wasn't used. Problems 1 and 4 never reached the planner (direct automation).
- These 5 are easy: 2 fell to the hammer alone. M12's benchmark should include harder ones.
- Recursive decomposition of stuck lemmas is NOT implemented yet (→ M4d). A stuck lemma currently just becomes
  feedback for the next planning round.

Next: M4d — in search.py, when a plan's lemma is stuck (not refuted), call `search(lemma.statement, ...)` with
depth+1 (max depth 1, 1 round, its own events tagged with depth) and, if it proves it, splice the sub-proof's lemmas
in (rename with a prefix to avoid clashes) and continue. Fake-model Lean test. Then M5 (LaTeX formalization).

### Iteration 8 — M4d (2026-11-14)
Done:
- search.py: `depth` param (events carry "depth"; sub-searches skip try_direct). `prove_plan(plan, emit, on_event,
  hints, depth)` is a while-loop; a stuck lemma with depth < MAX_DEPTH (=1) gets `search(lemma.statement, hints,
  rounds=1, depth=depth+1)`; on success `splice` inserts the sub-plan's lemmas (renamed `<lemma>_<name>`,
  whole-word regex) before it and sets its proof to the renamed sub main proof.
- cli.py: indented printing for depth>0 events + "↳ X is stuck: starting a sub-search on it".
- tests/test_search.py: +splice test, +CLI indentation test, +Lean test where the scripted prover always fails
  (REPAIR_ROUNDS=0) and a sub-plan with helper `step` proves odd_sum → final code contains `theorem odd_sum_step`.
- Snapshots for the M4c chapter: `guide/snapshots/m4c/{mathfinder/search.py,mathfinder/cli.py,tests/test_search.py}`.
  Guide chapter `09-m4d.html`.

Verified:
- `uv run pytest -q tests/test_search.py -rA` → 8 passed in 141.79s (incl. the sub-search Lean test).
- `uv run pytest -q` → `54 passed, 3 deselected in 274.56s`.

Known issues / notes:
- A sub-search proves the stuck lemma STANDALONE: it can't use the parent plan's earlier lemmas (the planner/critic
  don't take a preamble yet). Fine for now; revisit if real runs show stuck lemmas that need their siblings.
- Not yet seen a sub-search triggered by the REAL model (the 5 easy problems never got stuck). M12 benchmark will tell.

Next: M5 — `mathfinder/formalize.py`: `formalize(latex) -> Formalization(statement, back_translation, judgment,
errors)`: LLM LaTeX→Lean statement (format: after `theorem name`), `statement_errors` check with up to 2 repair
rounds, LLM back-translation to LaTeX, LLM judge (same meaning? yes/no + reason). CLI: `prove "<latex>"` shows the
formal statement + back-translation and asks y/n (approval) before searching; `--yes` to skip; `--lean` still works.

### Iteration 9 — M5 (2026-11-14)
Done:
- `mathfinder/formalize.py`: `formalize(latex) -> Formalization(latex, statement, back_translation, faithful,
  judgment, errors, .ok)`: SYSTEM_FORMALIZE (traps: ℕ truncation, explicit hypotheses, format) → `statement_errors`
  check with REPAIR_ROUNDS=2 (error appended to prompt) → SYSTEM_BACK literal back-translation (never sees the
  original) → `judge(original, back)` (SYSTEM_JUDGE checklist; only JSON `true` counts). `clean_statement` strips
  `theorem name` / `:= ...`.
- cli.py: positional LaTeX arg; `--lean` optional (skips formalization); `--yes` (honoured only if the judge agrees);
  `approve(latex, auto_yes, ask=input)` with y / n / e(dit, Lean-checked) loop; EOFError ⇒ no.
  Snapshot for the M4d chapter: `guide/snapshots/m4d/mathfinder/cli.py`.
- Tests: `tests/test_formalize.py` (5 parametrized fast, 1 Lean with scripted model incl. repair + "back-translator
  never sees original" assert, 2 llm incl. judge catching ℕ truncation), `tests/test_cli.py` (6 fast approval tests).
- Guide chapter `10-m5.html`.

Verified:
- Real: "6 ∣ n³ − n" → `(n : ℤ) : 6 ∣ n ^ 3 - n`, faithful. "a/b + b/a ≥ 2 for positive reals" →
  `(a b : ℝ) (ha : 0 < a) (hb : 0 < b) : a / b + b / a ≥ 2`, faithful.
- `uv run pytest -q -m llm tests/test_formalize.py` → `2 passed, 6 deselected in 26.75s` (judge rejected the ℕ trap).
- End to end from LaTeX: `python -m mathfinder prove 'For all positive reals $a, b$: ...' --yes` → PROVED in 2m11s
  (plan: diff_identity [LLM: field_simp; ring] + frac_sq_nonneg [hammer: positivity], main: linarith).
  Log: runs/m5_latex_demo.log.
- `uv run pytest -q` → `66 passed, 5 deselected in 289.03s`.

Known issues / notes:
- The interactive y/n/e prompt was exercised only through tests (scripted `ask`), not by a human at a terminal.
- Full suite now takes ~5 min because of Lean tests; use `-m "not lean and not llm"` (0.06 s) while iterating.

Next: M6 — `mathfinder/explain.py`: `explain(latex_or_statement, result) -> str` Markdown with LaTeX: one LLM call
given the verified Lean code + plan sketch + lemma informal texts; require a section per lemma mapping to its Lean
name ("Step 1 (`diff_identity`): ..."). Validate that every lemma name appears. CLI prints it after "VERIFIED"
and saves `runs/<timestamp>/proof.lean` + `explanation.md`.

### Iteration 10 — M6 (2026-11-14)
Done:
- `mathfinder/explain.py`: `explain(theorem, plan, lean_code) -> markdown`: SYSTEM fixes structure
  (## Theorem / ## Idea / ## Proof, **Step k** (`lemma`), **Conclusion** (`main`)); `build_prompt` (original wording,
  sketch, lemma meanings, full verified Lean code); `missing_steps` (each lemma name + main must appear in backticks)
  → one retry naming what was skipped.
- cli.py: after a proof, explain (unless `--no-explain`; LLMError ⇒ note, proof still saved) and `save_run` →
  `runs/<YYYYmmdd-HHMMSS>/{theorem.txt, proof.lean (HEADER + code), explanation.md}`; `RUNS_DIR` for tests.
- Tests: `tests/test_explain.py` (3 fast + 1 llm); test_cli.py main test now checks the saved files, + failed
  explanation still saves; test_search.py CLI test uses tmp RUNS_DIR + --no-explain.
- Snapshots: `guide/snapshots/m5/{mathfinder/cli.py,tests/test_cli.py}`, `guide/snapshots/m4d/tests/test_search.py`.
  Guide chapter `11-m6.html`.

Verified:
- `uv run pytest -q -m llm tests/test_explain.py` → `1 passed, 3 deselected in 11.07s`.
- Real: `python -m mathfinder prove 'For every integer $n$, $6 \mid n^3 - n$.' --yes` → PROVED + explanation with
  Step 1 (`mod_six_cases`) + Conclusion (`main`) and a correct residue table; saved to runs/20261114-144642 (1m25s).
  Log: runs/m6_demo.log.
- The saved proof.lean compiled standalone: `lake env lean .mf_tmp/saved_check.lean && echo OK` → OK (no messages).
- `uv run pytest -q` → `70 passed, 6 deselected in 289.48s`.

Known issues / notes:
- Explanation quality is only checked mechanically (names present). Content is the model's; it's grounded in the
  verified code but not itself verified.

Next: M7 — `mathfinder/collab.py`: `Session` (thread-safe): theorem, hints list, user lemmas, rejected lemma names,
pinned plan, paused flag (threading.Event), question/answer channel. search() takes an optional `session`: checks
pause between steps, reads session hints each round (+ user lemmas offered to the planner, rejected names filtered
out by parse/after planning), and when a plan's lemma is stuck AND the round budget is spent, emits "ask_user"
with the goal + Lean error and waits (timeout) for an answer, which becomes a hint for one more round.
CLI: `--interactive` lets you answer questions at the terminal. Tests with a scripted session in a thread.

### Iteration 11 — M7 (2026-11-14)
Done:
- `mathfinder/collab.py`: `Session(theorem, answer_timeout=600)` — thread-safe (Lock), add_hint / add_lemma (becomes a
  "must still be proved" hint) / reject_lemma (hint + filter by name or normalized statement) / pin / pause / resume
  (Event) / stop / edit_statement (stop with reason) / answer; search side: hints() copy, uses_rejected(plan),
  checkpoint() (waits while paused, returns stop reason), ask(question) (Queue with timeout).
- search.py: `session=None` param; per-round hints = hints + session.hints(); plans using rejected lemmas dropped
  ("uses X, which you rejected"); stop checks before rounds/plans/lemmas; rounds loop is now `while True` and, when
  out of rounds at depth 0, emits `ask_user` (build_question: first stuck lemma of best plan + Lean error), waits,
  answer → hint + one more round (MAX_QUESTIONS=2). New events: ask_user, user_answer, no_answer, stopped.
- cli.py: `--interactive` (search in a daemon thread; main thread select()s stdin every 0.5 s; EOF-safe);
  `handle_command` (hint/lemma/reject/pin/pause/resume/stop; open question ⇒ the line is the answer); prints new events.
- Tests: `tests/test_collab.py` (6 Session tests with real threads + 4 search-with-session tests on fakes: hints +
  rejection, ask → answer → extra round proves, timeout → gave_up, edit stops); test_cli.py +2 command tests.
- Snapshots: m4d/mathfinder/search.py, m6/{mathfinder/cli.py,tests/test_cli.py}. Guide chapter `12-m7.html`.

Verified:
- `uv run pytest -q tests/test_collab.py` → `10 passed in 0.65s`.
- Real interactive run with piped commands (hint at 1 s, pause at 2 s, resume at 5 s) on
  a²+b²+c² ≥ ab+bc+ca: all three acknowledged; planner's plan "Direct nlinarith with the three squared-difference
  hints, as suggested by the user." → VERIFIED `nlinarith [sq_nonneg (a - b), sq_nonneg (b - c), sq_nonneg (c - a)]`.
  Log: runs/m7_interactive_demo.log.
- `uv run pytest -q` → `82 passed, 6 deselected in 292.48s`.

Known issues / notes:
- The real ask_user path (real model fails → question → human answer) is only tested with fakes; a real failing
  search is slow/expensive to provoke. Will show up naturally in M12.
- "Pin a branch" is implemented as a planner hint, not as a structural pin of a plan object (plans are regenerated
  each round). "Edit statement" stops the search; restarting is the caller's job (server in M9).
- Commands take effect at checkpoints only; a long Lean run (30–60 s) delays them.

Next: M8 — `mathfinder/traces.py`: `TraceLogger(run_dir)` = an on_event callback that appends every event as a
JSON line to runs/<id>/trace.jsonl (plus timestamps); make the prover emit attempt-level events (each LLM proof
attempt with its Lean result/error, hammer result) via an optional `on_attempt` callback so failures are logged too;
`export(runs_dir, out_dir)` → sft.jsonl (statement+context → proof, proved only), preferences.jsonl (same goal:
working vs failed attempt), value.jsonl (lemma statement → proved?/refuted/stuck), hints.jsonl (hint → outcome).
CLI: `export` subcommand; `prove` always logs.

### Iteration 12 — M8 (2026-11-14)
Done:
- prover.py: `prove_lemma(..., on_attempt=None)` reports one record per attempt {statement, context (earlier lemma
  theorems), proof, ok, error ≤600, source: hammer|llm|repair}. `try_proofs` now reports ALL attempts in a batch
  (found the hard way: preferences were 0 because it returned at the first success) and returns the first success.
- search.py: prove_plan forwards attempts as "attempt" events.
- `mathfinder/traces.py`: `TraceLogger(run_dir, theorem)` (callable on_event, thread-safe append to trace.jsonl,
  meta first, `finish(proved, hints)` last), `chain(*callbacks)`, `read_trace`, `records_from_trace` (sft: ok
  attempts + depth-0 done; preferences: success×failure per (statement, context); value: lemma_* statuses;
  hints: result with hints), `export(runs_dir, out_dir)` (dedupe by sorted-key JSON).
- cli.py: `new_run_dir()` before searching (collision suffix -2...), every prove run traced, `save_run(folder, ...)`,
  interactive_search returns (result, hints used), `export [--out data/]` sub-command. `.gitignore` += data/.
- Tests: tests/test_traces.py (4 fast), test_prover.py (+attempt assertions, +"every attempt after a success"),
  test_cli.py (+trace checked, +export command), test_collab.py fake accepts on_attempt.
- Snapshots: m4c/{prover.py, test_prover.py}, m7/{search.py, cli.py, test_cli.py, test_collab.py}, m4b/.gitignore.
  Guide chapter `13-m8.html`; map tree shows runs/, cache/, data/.

Verified:
- Real: `prove --lean "(x : ℝ) (hx : 0 < x) : x + 1 / x ≥ 2" --no-explain` → PROVED (combine_frac via hammer
  field_simp, quad_ineq via LLM), trace at runs/20261114-152642/trace.jsonl; `export` → sft 3, preferences 0,
  value 2, hints 0. Second run (a+b=2 ⇒ ab ≤ 1) proved with a 0-lemma plan → no attempts (expected).
- `uv run pytest -q tests/test_prover.py -m lean -k every_attempt` → 1 passed (success-then-failure both reported);
  records_from_trace on that shape → one preference row with rejected_error.
- `uv run pytest -q` → `88 passed, 6 deselected in 338.65s`.

Known issues / notes:
- Real preference rows are still 0: easy theorems rarely produce failures. M12's harder problems should.
- Plan-level data isn't exported yet (skeleton failures, plan → success), only lemma-level. Could add a
  "plans.jsonl" in M13.
- Runs from before this iteration have no trace.jsonl.

Next: M9 — server/: FastAPI app (`server/app.py`): POST /api/formalize {latex} → Formalization; POST /api/sessions
{statement, hints} → starts search in a background thread with a Session + TraceLogger, returns id;
GET /api/sessions/{id} → status/result; POST /api/sessions/{id}/hint|lemma|reject|pin|pause|resume|stop|answer;
WebSocket /ws/{id} streams events (history first, then live) via an asyncio queue fed thread-safely
(loop.call_soon_threadsafe). Add fastapi + uvicorn deps. Tests with FastAPI TestClient and a fake search.
Verify with curl + a tiny websocket client against a real uvicorn run.

### Iteration 13 — M9 (2026-11-14)
Done:
- Deps: fastapi 0.163, uvicorn[standard] 0.54 (websockets 17.4), dev httpx 0.28. pytest `pythonpath = ["."]`.
  Snapshot for the M1 chapter: guide/snapshots/m1/pyproject.toml.
- `server/app.py`: REST — POST /api/formalize, POST /api/check, POST /api/sessions, GET /api/sessions/{id},
  POST /api/sessions/{id}/{hint|lemma|reject|pin|pause|resume|stop|answer|edit} (edit: Lean-checks, stops old, starts
  new run with same hints, returns new_id); WS /ws/{id} (history replay then live, closes after "finished").
  `Run`: background thread per search + Session + TraceLogger + run folder; thread→asyncio bridge via
  loop.call_soon_threadsafe; history+subscribe under one lock; always emits "finished" (even on crash); explains and
  saves on success. Modules imported as engine/explainer/formalizer so tests can fake them.
- `tools/watch.py`: one-line-per-event terminal WebSocket client.
- `tests/test_server.py`: 5 TestClient tests (formalize, full streamed run + saved proof, hint+answer while the
  search waits on a question, edit flow incl. 400 on bad statement, 404/400). Guide chapter `14-m9.html`.

Verified (real uvicorn on port 8765; port 8000 is occupied by another python process on this Mac, left alone):
- `curl POST /api/formalize` "n²+n is even" → `(n : ℕ) : Even (n ^ 2 + n)`, faithful, ok (40 s).
- `POST /api/sessions` + `POST .../hint` + WebSocket: 10 events start → … → finished; status proved;
  proof: lemma n^2+n = n*(n+1) (hammer: linarith) + `exact Nat.even_mul_succ_self n`; explanation written
  (and it honestly noted linarith vs the planner's "ring"). Hint visible in GET status.
- `tools/watch.py` on a second session ((a+b)² expansion) printed start/done/explaining/finished; status proved.
- `uv run pytest -q -m "not lean and not llm"` → `70 passed, 29 deselected in 0.97s`.
- `uv run pytest -q` → `93 passed, 6 deselected in 718.58s` (slower than usual: the machine was busy with real runs).

Known issues / notes:
- RUNS lives in memory: restarting the server forgets sessions (their traces/proofs stay on disk).
- No auth: the server is meant for localhost only (uvicorn binds 127.0.0.1 by default).
- Port 8000 is used by something else on this Mac; the guide uses 8765.

Next: M10 — web/ single page (index.html, style.css, app.js) served by FastAPI StaticFiles at "/".

### Iteration 14 — M10 (2026-11-14)
Done:
- `web/index.html` (4 steps: LaTeX + live KaTeX preview + start hints → approve (editable statement, Lean check,
  back-translation, verdict) → live search (tree + chat/question panel, suggest lemma, pause/resume, stop) → result
  (metrics, explanation, Lean proof, saved folder)); `web/style.css` (dark, status colours via classes);
  `web/app.js` (fetch/post helpers; renderMarkdown hides $…$ from marked then KaTeX; textContent/escapeHtml for
  safety; WebSocket → handleEvent → state → renderTree; lemma click menu: hint / reject / pin plan; chat box sends
  answer when a question is open else hint; direct-automation proofs get their own node; #demo replays
  web/demo_events.json). CDN: katex 0.16.11, marked 12.0.2.
- server/app.py: `app.mount("/", StaticFiles(web/, html=True))` LAST. Snapshot for M9 chapter: guide/snapshots/m9/server/app.py.
- `tools/make_demo.py` (trace → web/demo_events.json). tests/test_server.py +1 (page served, API still wins).
- Guide chapter `15-m10.html`.

Verified:
- Headless Chrome screenshots: home (KaTeX preview typeset), /#demo (tree with ✓ lemma, chat log, metrics,
  explanation with typeset math, Lean code), direct-proof replay shows "Automation: linarith" node.
- REAL click-through via Chrome DevTools protocol (scratchpad script, not part of the project): typed LaTeX
  "x ≥ 0 ⇒ x^n ≥ 0" → Formalize (53 s) → `(x : ℝ) (n : ℕ) (hx : 0 ≤ x) : 0 ≤ x ^ n`, ✓ same meaning → Approve →
  hint "positivity should do it" sent via chat and echoed → (73 s) "Proved, and verified by Lean", `positivity`.
- `node --check web/app.js` OK; `uv run pytest -q -m "not lean and not llm"` → `71 passed, 29 deselected`.

Known issues / notes:
- Page needs internet for KaTeX/marked (CDN). Without it, it still works but shows raw $…$ and plain text.
- marked output isn't sanitized beyond escaping < > & before parsing (inline-code with < shows as &lt;). Local use only.
- The ask_user question UI is only exercised via replay logic, not a real stuck search.
- Metrics are computed in the browser from events ("time" is wall time since the page started watching).
- `uv run pytest -q` (M10 code) → `94 passed, 6 deselected in 868.24s`.

Next: M11 — proof optimization.

### Iteration 15 — M11 (2026-11-14)
Done:
- `mathfinder/optimize.py`: `proof_stats(code, metric)` (steps = indented lines, lemmas = theorems − 1, heavy =
  nlinarith/polyrith/aesop/simp_all/decide/exact? occurrences) + METRICS short / readable (+5·heavy) / few_lemmas
  (10·lemmas); `optimize(theorem, code, plan, metric, on_event)`: one LLM call for CANDIDATES=4 single-block rewrites
  → one check_decls batch → cheapest that beats the original AND passes verify_final wins; otherwise the original is
  kept. Events: optimize_start, optimize_candidate, optimized.
- cli.py: `--metric {few_lemmas,readable,short}` (default short), `--no-optimize`; explanation + saved proof use the
  chosen proof. server/app.py: StartRequest.metric (400 if unknown), Run optimizes before explaining, result has
  `optimization` {metric, before, after, improved, tried}. web: metric <select>, optimizer messages, cost tile.
- Tests: tests/test_optimize.py (4 fast + 1 Lean); test_cli.py (+1, older tests use --no-optimize);
  test_search.py CLI test --no-optimize (it was silently calling the real optimizer: fast tests took 15 s);
  test_server.py (optimizer faked in fixture, +1 metric test).
- Snapshots: m8/{cli.py,test_cli.py}, m10/{server/app.py,tests/test_server.py,web/index.html,web/app.js,web/style.css},
  m6/tests/test_search.py, and a missed one from M10: m9/tests/test_server.py. Guide chapter `16-m11.html`.

Verified:
- `uv run pytest -q tests/test_optimize.py -m "not llm"` → `5 passed in 47.47s` (Lean: wrong candidate rejected,
  one-liner replaces 2-lemma proof, cost 7 → 1).
- Real optimizer on the saved M8 proof of x + 1/x ≥ 2: short 6 → 2 (`have h ... ; nlinarith [...]`), readable
  11 → 4 (`have key : x + 1/x − 2 = (x−1)²/x`, `rw [ge_iff_le, ← sub_nonneg, key]`, `exact div_nonneg (sq_nonneg _) hx.le`).
- Real web click-through (headless Chrome, metric=readable, x + 1/x ≥ 2 from LaTeX): proved in 209 s; optimizer ran
  ("current cost 6" → kept, no cheaper verified rewrite); page shows "6 → 6 proof cost (readable)" tile.
- `uv run pytest -q -m "not lean and not llm"` → `77 passed, 30 deselected`.

Known issues / notes:
- Optimizer budget is fixed (1 call, 4 candidates). No per-lemma optimization; rewrites are single-block only.
- `heavy` is a word count, so a lemma *name* containing e.g. "decide" would count. Fine for now.

### Iteration 16 — M12 part 1 (2026-11-14)
Done:
- `bench/problems.jsonl`: 20 miniF2F-style problems as Lean statements (algebra 5, number theory 9, induction 3,
  inequality 3). All 20 type-check (one Lean run with sorry, 56 s).
- `mathfinder/bench.py`: load_problems, run_one (search with rounds=2, traced to runs/bench-<ts>/<name>/,
  crash-safe), results_table (summary + rows + per-topic), run_bench (→ bench/results/<ts>.json + .md).
- cli.py: `bench [--rounds N] [--only names...]`. Snapshot for M11 chapter: guide/snapshots/m11/mathfinder/cli.py.
- tests/test_bench.py: 4 fast + 1 Lean (all statements type-check). Guide chapter `17-m12.html`; map tree updated.

Verified so far:
- `uv run pytest -q tests/test_bench.py -m "not lean and not llm"` → `4 passed, 1 deselected in 0.06s`.
- Statement type-check: all 20 "ok" (script output in iteration log above).

In progress: the full benchmark run is chained to start after the M11 full suite finishes; log in
bench/last_run.log, results in bench/results/<ts>.md. NEXT ITERATION: read the results, paste the table here,
fix the checkpoint numbers in 17-m12.html if needed, mark M11 (if its suite passed) and M12 done.
- M11 full suite (`uv run pytest -q`, collected before test_bench.py existed) → `101 passed, 6 deselected in 1139.73s`. M11 marked done.
- Benchmark started 16:53:43 (bench/last_run.log).

### Iteration 17 — M13 (2026-11-14), while the M12 benchmark runs
Done:
- `training/prepare.py`: data/{sft,preferences,value}.jsonl → data/{train,test}/{sft_chat,dpo,value}.jsonl in TRL
  formats (chat messages; prompt/chosen/rejected; text/label). Split by GOAL via sha256(normalized statement) % 100
  < 10 → test (no leaks). Theorem-level SFT rows skipped (whole files, not tactic proofs).
- `training/finetune.py`: LoRA SFT with TRL (SFTConfig + SFTTrainer + peft LoraConfig r=16, all-linear, grad accum 8,
  lr 2e-4); heavy imports only inside train(); `--dry-run` (data report + plan, no ML libs); `--max-steps` for smoke tests.
- `training/__init__.py`, `.gitignore` += models/ (snapshot m8/.gitignore), tests/test_training.py (5 fast).
- Guide chapter `18-m13.html`; map tree updated.

Verified:
- Real data: `mathfinder export` → sft 12, preferences 3 (first real preference pairs!), value 4, hints 2;
  `training/prepare.py` → train sft_chat 6 / dpo 3 / value 4, test all 0 (few goals); `finetune.py --dry-run` OK.
- REAL smoke training (2 steps, CPU, ephemeral env): `uv run --with torch --with transformers --with datasets --with
  peft --with trl python training/finetune.py --base-model HuggingFaceTB/SmolLM2-135M-Instruct --max-steps 2 --out
  models/smoke-test` → loss 3.875 → 3.829, "saved the LoRA adapter to models/smoke-test" (19.6 MB adapter), 71 s total.
- `uv run pytest -q tests/test_training.py` → 5 passed; fast suite → `86 passed, 31 deselected`.

Known issues / notes:
- No real training was run (by design). A trained adapter isn't wired back into Math Finder as an LLM backend yet;
  that would be a new backend (e.g. serve the model with an OpenAI-compatible server and add it to llm.BACKENDS).
- Full Lean suite postponed until the benchmark finishes (they slow each other down).

Remaining: M12 — finish the benchmark run (started 16:53), paste the results table, then run the full suite once.

### Iteration 18 — M12 results (2026-11-14)
Benchmark run: `uv run python -m mathfinder bench` (rounds=2, claude CLI backend), 16:53 → 17:58, log bench/last_run.log,
traces in runs/bench-20261114-165343/, results bench/results/20261114-165343.{md,json}:

**20/20 proved** (100%), total time 64 min

| # | Problem | Topic | Result | Time | How |
|---|---------|-------|--------|------|-----|
| 1 | `linear_eq` | algebra | ✓ proved | 127 s | Automation: linarith |
| 2 | `sum_diff_product` | algebra | ✓ proved | 217 s | Automation: nlinarith |
| 3 | `rational_eq` | algebra | ✓ proved | 324 s | plan with 0 lemma(s), round 1 |
| 4 | `function_compose` | algebra | ✓ proved | 285 s | plan with 0 lemma(s), round 1 |
| 5 | `log_square` | algebra | ✓ proved | 155 s | Automation: norm_num |
| 6 | `mod_double` | number theory | ✓ proved | 171 s | Automation: omega |
| 7 | `gcd_180_168` | number theory | ✓ proved | 176 s | Automation: norm_num |
| 8 | `crt_small` | number theory | ✓ proved | 178 s | Automation: omega |
| 9 | `squares_mod_4` | number theory | ✓ proved | 362 s | plan with 1 lemma(s), round 1 |
| 10 | `three_divides` | number theory | ✓ proved | 399 s | plan with 1 lemma(s), round 1 |
| 11 | `six_divides_consecutive` | number theory | ✓ proved | 212 s | plan with 1 lemma(s), round 1 |
| 12 | `seven_divides_power` | number theory | ✓ proved | 379 s | plan with 1 lemma(s), round 1 |
| 13 | `primes_sum_nine` | number theory | ✓ proved | 253 s | plan with 1 lemma(s), round 1 |
| 14 | `sqrt3_irrational` | number theory | ✓ proved | 83 s | Automation: norm_num |
| 15 | `gauss_sum_101` | induction | ✓ proved | 76 s | Automation: norm_num |
| 16 | `gauss_sum_formula` | induction | ✓ proved | 123 s | plan with 0 lemma(s), round 1 |
| 17 | `two_pow_ge` | induction | ✓ proved | 111 s | plan with 1 lemma(s), round 1 |
| 18 | `amgm_three_pairs` | inequality | ✓ proved | 88 s | plan with 0 lemma(s), round 1 |
| 19 | `product_max` | inequality | ✓ proved | 69 s | plan with 0 lemma(s), round 1 |
| 20 | `quartic_nonneg` | inequality | ✓ proved | 76 s | plan with 0 lemma(s), round 1 |

By topic: algebra 5/5, induction 3/3, inequality 3/3, number theory 9/9

Honest reading: 8/20 by direct automation, the other 12 in the FIRST planning round (0–1 lemmas); no sub-search,
no ask_user, no second round was needed. The set is easy for this system (I wrote it, miniF2F-style; not real
miniF2F) — a good regression test, not a measure of hard-problem skill. Times include ~30–60 s of Lean per call.

Bug found & fixed: `export` used runs_dir.glob("*/trace.jsonl") and missed the nested benchmark traces
(runs/bench-<ts>/<name>/trace.jsonl). Now `rglob("trace.jsonl")`; test added (test_traces.py, nested trace).
After the fix: export → sft 41, preferences 10, value 10, hints 2 (was 12/3/4/2); prepare → train sft_chat 15 /
dpo 10 / value 10. Snapshots: m8/{mathfinder/traces.py, tests/test_traces.py}. M12 chapter got the fix as steps 5–6,
real checkpoint numbers and the full results table; M13 checkpoint numbers updated; 99-next is now "What's next".

Verified: `uv run pytest -q tests/test_bench.py` → `5 passed in 32.25s` (incl. all 20 statements type-check);
`uv run pytest -q tests/test_traces.py` → 5 passed; guide JS `node --check` OK; guide has 20 sections.
- Final full suite (all milestones' code): `uv run pytest -q` → `112 passed, 6 deselected in 1040.56s`.
  (The 6 deselected are the `llm` tests that call the real model; each was run on its own when its milestone was built.)

## ALL MILESTONES (M0–M13) DONE AND VERIFIED — the loop can be stopped (cron job 61ff8dc4).

---
# Productionization loop (see prompt.md)

### 2026-11-22 — Step 0: baseline under git
- `git init -b main`; extended .gitignore (*.log, lean_env/.lake/, .claude/settings.local.json, scheduler lock).
- lean_env/ had an empty nested repo from `lake new` (no commits); moved it aside to the session scratchpad so the root repo can track lean_env/.
- Committed the prototype as-is (125 files, .git 1.3 MB), tagged `v0-prototype`.
- Verified: `uv run pytest -q -m "not lean and not llm"` → `1 failed, 86 passed, 31 deselected in 0.84s`
  (failure: tests/test_guide.py — guide.html stale; the guide is removed in step 1).
- Next: step 1 (make it lean).
