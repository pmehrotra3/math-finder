# Math Finder

[![CI](https://github.com/pmehrotra3/math-finder/actions/workflows/ci.yml/badge.svg)](https://github.com/pmehrotra3/math-finder/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**You write a theorem. Math Finder finds a proof, and Lean checks that it is correct.**

![Math Finder proving that 6 divides n³ − n](.github/screenshot.png)

AI models can write convincing proofs that are wrong. Lean is a program that checks a proof step by step and rejects
any mistake. Math Finder combines them: the AI (Claude) has the ideas, and a proof counts only once Lean accepts it.

## Working with it

Math Finder is laid out like a chat app. Your projects are on the left, under the names you give them (a name is
suggested; click it to change it), split into the ones in progress and the established ones. Every theorem is a
**project**, saved on your computer, and each one is its own space: **a chat on one side, the proof on the other**
(drag the line between them to make either wider). Above the proof, a progress bar and the obvious next step.

1. **Write** the theorem in LaTeX (*New project*), for example *for every integer n, 6 divides n³ − n*.
2. **Confirm** the Lean version. It is translated back into LaTeX without seeing your text, so you can check
   that it says what you meant.
3. **Work on it together.** In the chat you can discuss the math ("why is this lemma true?") or give the
   assistant a task: "make a plan with one lemma per case and prove it", "write the proof by induction",
   "stop, and try AM-GM instead". Like a coding agent, it works in steps: it acts, waits for Lean, reads Lean's
   answer, fixes what was rejected, and goes on until the task is done, saying under each reply what it did.
   - **Steer it** by writing while it works; it reads your message at its next step.
   - **Stop it** with Esc or the Stop button.
   - **Act directly** with `/` commands (`/prove step_one`, `/search`, `/hint ...`, `/finish`), which run at once.

   What the proof reports ("case_even: proved") appears in the chat as it happens, and your hints are given to
   every later plan and proof attempt.

   The proof panel shows the same work, and every button there does what the chat can do:
   - **Plan.** Ask for plans, or write your own. A plan is an idea, a list of lemmas, and a short Lean proof
     that combines them. Lean checks that the lemmas really imply the goal before anything is proved.
   - **Lean first, then LaTeX.** Every lemma is written in Lean, and its formal LaTeX is generated from that Lean,
     so the two always say the same thing. Edit any lemma, add or remove lemmas, or change the idea.
   - **Zoom in and out.** Open a lemma to give it a plan of its own, see every proof attempt with Lean's errors,
     or prove it on its own. The outline and the breadcrumb take you back out.
   - **Prove, search, or assume.** Prove a goal (its lemmas first), let the automatic search handle it, or
     assume a lemma for now and come back to it later. **Stop** ends a running job.
4. **Check and explain.** When the theorem is proved with nothing assumed, the whole tree goes through Lean's
   final check (no gaps, only standard axioms) and you get an explanation in ordinary mathematics.

### Building on earlier projects

Any earlier project can be plugged into a new one. In the proof panel's **Library** tab, add a project's theorem as
a lemma of the goal you have open (it gets a name from the project, which you can change), or use it to prove that
goal. In the chat, refer to a project with **@** and the assistant can do the same.

Lean still checks everything. A project whose proof passed the final check is **established**: its verified code is
included in the new proof, in a namespace of its own, and the lemma is proved by applying that project's theorem.
A project that isn't established yet can be plugged in too; it counts as *assumed* until it is established, and
then proving the lemma cites it. Projects that use each other in a circle are refused, and a project can't be
deleted while another one uses it.

Lemmas can be renamed at any time (*Rename* next to a lemma's name), and every proof that uses one follows along.

The command line (`mathfinder prove`) runs the same steps automatically, without the workspace or projects.

## How the automatic search works

```mermaid
flowchart LR
    A[Goal] --> B[Automation]
    B -->|not enough| C[Plan small lemmas]
    C --> D[Prove each lemma]
    D -->|Lean error| D
    D -->|stuck| E[Split into smaller lemmas]
    E --> D
    D -->|plan fails| C
    D -->|all proved| F[Final Lean check]
    B -->|proved| F
```

Automatic tactics and a search for counterexamples come first. Claude is asked only when they fail, and it sees
Lean's errors when it tries again.

## Find your way around the code

Start with `pipeline.py`, then follow a theorem through `translation/`, `proving/` and `lean/`.

```
src/mathfinder/
├── pipeline.py          START HERE: one complete automatic run, step by step
├── translation/         between people and Lean
│   ├── formalize.py       LaTeX → Lean statement
│   └── explain.py         Lean proof → explanation
├── proving/             finding the proof
│   ├── workspace.py       the tree of goals you plan and prove together
│   ├── search.py          the automatic loop: plan, prove, check, try again
│   ├── planner.py         asks Claude for plans made of small lemmas
│   ├── prover.py          proves one lemma, repairing failed attempts
│   ├── hammer.py          automatic tactics and counterexamples, no AI
│   ├── optimize.py        looks for a better proof
│   └── session.py         lets you stop a running search
├── lean/                checking the proof
│   ├── repl.py            keeps Lean running and sends it code
│   └── checker.py         the questions we ask Lean, and the final check
├── interface/           what you use
│   ├── cli.py             command line
│   ├── server.py          web server
│   ├── live.py            an open project: saving, jobs, the chat and the assistant's steps
│   └── web/               web page
├── projects.py          saved projects, and the established results they share
├── benchmark/           the test problems, and how many it solves
├── assistant.py         the assistant in the chat: what it is told, and how its answer is read
└── llm.py               talks to Claude

lean/                    pinned versions of Lean, Mathlib and the REPL
tests/                   tests
runs/                    your projects and results (created when you run it; not in git)
```

## Setup

Requires [uv](https://docs.astral.sh/uv/), [elan](https://github.com/leanprover/elan), about 8 GB of disk for Mathlib,
and 16 GB of RAM or more (recommended).

```bash
git clone https://github.com/pmehrotra3/math-finder && cd math-finder
uv sync
cd lean && lake exe cache get && lake build repl && cd ..
```

The model is Claude, through the [`claude` CLI](https://claude.com/claude-code) by default.

| Variable | Default | Meaning |
|---|---|---|
| `MATHFINDER_LLM` | `cli` | `cli`, or `api` to use the Anthropic API |
| `ANTHROPIC_API_KEY` | | required for `api` |
| `MATHFINDER_CLI_MODEL` | CLI default | model for the `cli` backend |
| `MATHFINDER_LEAN_WORKERS` | `2` | Lean processes, each about 7.5 GB resident |
| `MATHFINDER_LEAN_RECYCLE` | `300` | checks before a Lean process restarts |

## Usage

```bash
uv run mathfinder serve                                               # the web page, at http://localhost:8000
uv run mathfinder prove 'For every integer $n$, $6 \mid n^3 - n$.'    # a theorem in LaTeX
uv run mathfinder prove --lean "(n : ℕ) : n + 0 = n"                  # a Lean statement
uv run mathfinder bench                                               # the benchmark
uv run pytest -m "not lean and not llm"                               # fast tests (no Lean, no model)
```

Each run is saved in `runs/` with a full trace. A proved theorem also gets its Lean proof and explanation.

## Results

20 of 20 proved on a [self-written set](src/mathfinder/benchmark/problems.jsonl) of competition-style problems, 8 of them by automation
alone ([per-problem results](src/mathfinder/benchmark/results.md), recorded before the REPL speedup). The set is small and fairly easy:
it is a regression test, not a miniF2F score.

Lean stays running with Mathlib loaded instead of restarting for each check. On one five-proof file that cut a check
from 2.78 s to 74 ms (medians).

## Limitations

- **Run it only for yourself.** Submitted Lean code is not sandboxed and there is no login or rate limiting, so do not
  expose the server to a network.
- Formalization can still miss the intended meaning, so read the Lean statement before approving it.
- The `api` backend has been tested only against a fake client.

## License

[MIT](LICENSE)
