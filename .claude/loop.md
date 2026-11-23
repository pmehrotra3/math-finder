# Math Finder: from prototype to a hostable, lean repository

You run every 40 minutes. Each run: do **one** unchecked step below (or the next slice of it),
verify it, tick it, log it, commit. Then stop. Keep each run under ~35 minutes of work.

## The target (what "done" looks like)

A small, readable, public-quality repo that someone can clone, `docker compose up`, and use:
type a theorem (LaTeX or Lean), watch the agent plan → prove → repair live in the browser, and get a
proof that **only counts once the Lean kernel accepts it**. It runs on this machine (Intel Arc 140T iGPU,
16 cores, 61 GB shared RAM, Linux, Docker) and on a small VPS, with Claude or a local model behind it.

Matches the resume line: *"AI agent that formalizes math in Lean 4 and searches for machine-checked proofs,
splitting goals into lemmas and using compiler errors to repair failed attempts."* Every number in the
README must come from a real run that is in the repo.

## Rules (every run)

1. Start by reading this file, then the last ~40 lines of `docs/DEVLOG.md` (or `PROGRESS.md` before step 1 moves it).
2. Pick the first unchecked `- [ ]` step. If it is too big for one run, do a clean slice and add a sub-bullet saying what's left.
3. **Lean means lean**: fewer files, fewer abstractions, no speculative config or plugin systems, no dead code.
   Prefer deleting to adding. Plain functions over classes. A newcomer should understand any module in one read.
   Comments explain *why*, not *what*.
4. **Verify before ticking**: run the relevant tests (`uv run pytest -q -m "not lean and not llm"` at minimum;
   lean/llm tests when the step touches those paths) and paste the exact result line into the log.
   Never claim a number you did not measure.
5. Log the run at the bottom of `docs/DEVLOG.md`: date, step, what changed, verified-by output, next. Keep it short (≤15 lines).
6. Commit locally with a clear message (git is initialized in step 0). **Never push**, never create remotes,
   never publish or deploy anything public: those need the user's explicit OK.
7. Do not touch `lean_env/.lake/` (7 GB Mathlib build) except via `lake` commands. Don't download anything
   larger than ~6 GB without noting it in the log first.
8. If blocked (needs a decision, a key, a sudo install, a public deploy), write the question under
   `## Open questions` at the bottom of this file, skip to the next step you can do, and say so in the log.
9. When every step is ticked: run the full verification (step 9), write a final summary in the log, and
   tell the user the loop can be stopped.

## Steps

- [x] **0. Baseline under git.** `git init`, a sensible `.gitignore` (runs/, cache/, models/, .venv, .lake, *.log,
      generated html), commit the current prototype as-is, tag `v0-prototype`. Record current test status.
- [ ] **1. Make it lean.** Target layout:
      ```
      mathfinder/      core: lean.py, llm.py, prover.py, search.py, planner.py, formalize.py, ...
      server/          FastAPI app + WebSocket
      web/             static UI (no build step)
      lean_env/        Lake project pinned to a Mathlib version
      bench/           problem sets + runner output (results committed, raw runs not)
      tests/
      docs/            DEVLOG.md (was PROGRESS.md, trimmed), ARCHITECTURE.md
      ```
      - Merge or delete modules that don't earn their place (e.g. tiny `critic.py`, `explain.py`, `optimize.py`:
        keep only if used in the main proof path or the UI; otherwise fold in or remove).
      - The tutorial guide (`guide/`, `guide.html`, `tools/build_guide.py`, `tests/test_guide.py`) and `training/`
        are out of scope for the product: move them to a `archive/` branch (git branch, not folder) and delete from main.
      - Add `ruff` (lint + format) to dev deps, fix all findings. Add type hints on public functions.
      - All config via env vars, documented in one `.env.example`.
- [ ] **2. Fast Lean.** Replace per-check `lake env lean` (20–60 s Mathlib load each time) with a persistent
      Lean REPL process (leanprover-community/repl, pinned to the same toolchain) that imports Mathlib once and
      checks many snippets. Keep a pool of 1–N workers (N from env, default 2 to fit RAM). Restart a worker on
      crash/timeout. Measure and log before/after time per check.
- [ ] **3. Safe to host.** Lean code from users/LLMs is untrusted: reject `#eval`, `#exit`, `run_cmd`, `run_tac`,
      `IO`, `unsafe`, `implemented_by`, `extern`, `open ... System`, `import` of anything but Mathlib, `set_option` of
      resource limits, etc. (allowlist approach; test each). Per-check time + memory limits (maxHeartbeats + process
      rlimits). Server: one job queue, max concurrent jobs, per-IP rate limit, request size limit, `/healthz`.
- [ ] **4. Local model on the Intel Arc GPU.** Add one OpenAI-compatible backend to `llm.py`
      (`MATHFINDER_LLM=openai`, `OPENAI_BASE_URL`, `OPENAI_MODEL`), so Claude (cli/api) and local models are swappable.
      Serve a local model with llama.cpp `llama-server` using the **Vulkan** backend (works on Arc iGPU; SYCL is the
      fallback if it's clearly faster). Pick a 7–8B Lean prover model available as GGUF (check Hugging Face for current
      options, e.g. DeepSeek-Prover-V2-7B or Goedel-Prover-V2-8B, Q4_K_M/Q5_K_M). Allow role split: Claude for
      planning/formalization, local model for proof attempts (env-configurable). Log tokens/s and RAM use.
- [ ] **5. Honest benchmark.** Keep the 20 self-built problems as a fast regression set. Add a fixed, seeded sample
      (e.g. 40) of real **miniF2F-test (Lean 4)** problems. `python -m mathfinder bench --set minif2f40 --k 4` reports
      pass@k, median time, and LLM cost/tokens per backend. Commit the results table to `bench/results/`. Run it for
      Claude and for the local model if time allows (it can span several runs: make the runner resumable).
- [ ] **6. Containerize.** Multi-stage `Dockerfile` (elan + pinned toolchain, `lake exe cache get` for Mathlib
      oleans, uv-installed Python deps, non-root user). `docker-compose.yml`: `app` (+ optional `llm` service running
      llama-server with the Vulkan image and `/dev/dri` passed through, behind a compose profile). Image size and cold-start
      time logged. `docker compose up` → working UI on localhost verified end-to-end on one problem.
- [ ] **7. CI.** GitHub Actions workflow file: ruff + unit tests (no lean/llm) on every push; a separate manual
      workflow that builds the image and runs 3 Lean-only tests. (Write the file; don't create a GitHub repo.)
- [ ] **8. Docs that sell it.** `README.md`: one-sentence pitch, a screenshot/GIF of the live UI,
      an architecture diagram (Mermaid: formalize → plan → hammer → LLM prove → Lean check → repair/decompose),
      quickstart (Docker + local), config table, benchmark table from step 5, limitations stated plainly.
      `docs/ARCHITECTURE.md` (≤1 page), `docs/DEPLOY.md` (self-host behind Cloudflare Tunnel or a VPS: RAM/disk needs).
      `LICENSE` (MIT unless the user says otherwise).
- [ ] **9. Final pass.** Fresh clone into a temp dir → `docker compose up` → prove one problem from the UI. Full test
      suite including lean tests. `ruff check`. Line count per module in the log. Re-read every file once as a newcomer
      would and cut anything confusing. Tag `v1.0`.

## Open questions

(none yet)
