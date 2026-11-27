"""The workspace: a theorem and its lemmas as a tree you build together with the assistant.

Every goal (the theorem, a lemma, a lemma of a lemma, ...) has a Lean statement, a formal
LaTeX version of it, and either a direct proof or a PLAN: an idea, a list of smaller lemmas
(its children) and a main proof that combines them. You can

  propose(goal)         ask the model for plans, already checked by Lean
  set_plan(goal, ...)   use a plan as it is or after editing it (Lean is the source of truth;
                        the LaTeX is translated from the Lean)
  try_proof(goal, proof)
                        check a proof that you (or the assistant) wrote; Lean answers at once
  prove(goal)           prove it: children first, then its main proof (or directly, if no plan)
  auto(goal)            let the full search find a plan and proofs on its own
  assume(goal)          skip it for now; everything above it then counts as "proved, assuming"
  use_established(goal, project)
                        prove it by citing a theorem another project already established
  add_note(text)        a hint from you that every later plan and proof attempt sees
  finish()              put the whole tree into one Lean file and run the strict final check

The workspace itself doesn't know where projects are stored: the server gives it a
`library_code` function that returns the verified Lean of the projects it cites.
"""

import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from mathfinder.lean.checker import Decl, check_decls, statement_errors, verify_final
from mathfinder.proving.planner import NAME_RE, Lemma, Plan, propose_plans
from mathfinder.proving.prover import MAX_ERROR, preamble_for, prove_lemma
from mathfinder.proving.search import NAMESPACE, assemble, check_skeletons, search
from mathfinder.proving.session import Session
from mathfinder.translation.formalize import to_latex

ROOT = "main"


@dataclass
class Goal:
    name: str  # Lean name; the theorem itself is "main"
    lean: str  # Lean statement, without the name: "(n : ℤ) : 6 ∣ n ^ 3 - n"
    latex: str = ""  # the same statement in formal LaTeX
    informal: str = ""
    status: str = "open"  # open | working | proved | assumed | refuted | stuck
    proof: str = ""  # direct proof, or the plan's main proof
    sketch: str = ""  # the plan's idea ("" = no plan)
    children: list[str] = field(default_factory=list)  # names of the plan's lemmas, in order
    feedback: str = ""  # the latest Lean error or counterexample
    problems: list[str] = field(default_factory=list)  # why the plan doesn't fit together yet
    attempts: list[dict] = field(default_factory=list)  # every proof Lean checked for this goal
    established_by: str = ""  # id of the project whose established theorem proves this goal


class Busy(Exception):
    """Another job is still running in this workspace."""


class Workspace:
    def __init__(
        self,
        statement: str,
        latex: str = "",
        on_event: Callable[[dict], None] = lambda e: None,
        library_code: Callable[[list[str]], str] = lambda project_ids: "",
        goals: dict | None = None,
        notes: list[str] | None = None,
    ):
        """A new workspace for `statement`, or a saved one when `goals` (from saved_goals) is given."""
        if goals:
            self.goals = {name: Goal(**fields) for name, fields in goals.items()}
        else:
            self.goals = {ROOT: Goal(ROOT, statement, latex)}
        self.on_event = on_event
        self.library_code = library_code
        self.notes = list(notes or [])  # your hints, for every plan and proof attempt from now on
        self.session = Session()  # lets the user stop a running job
        self._job = threading.Lock()

    # ---------- reading ----------

    def parent(self, name: str) -> Goal | None:
        return next((g for g in self.goals.values() if name in g.children), None)

    def descendants(self, name: str) -> list[str]:
        """All goals below `name`, children before parents (the order Lean needs them in)."""
        out = []
        for child in self.goals[name].children:
            out += [*self.descendants(child), child]
        return out

    def assumed_below(self, name: str) -> list[str]:
        return [n for n in [*self.descendants(name), name] if self.goals[n].status == "assumed"]

    def lemmas_for(self, names: list[str]) -> list[Lemma]:
        """Goals as Lemma objects for a Lean preamble: real proofs, `sorry` where there is none yet."""
        out = []
        for n in names:
            g = self.goals[n]
            proved = g.status == "proved" and g.proof
            out.append(
                Lemma(
                    name=n,
                    statement=g.lean,
                    informal=g.informal,
                    status="proved" if proved else "open",
                    proof=g.proof if proved else "",
                )
            )
        return out

    def context_for(self, name: str) -> list[str]:
        """What `name` may use: its earlier siblings, with everything below them."""
        parent = self.parent(name)
        if parent is None:
            return []
        earlier = parent.children[: parent.children.index(name)]
        return [n for sibling in earlier for n in [*self.descendants(sibling), sibling]]

    def projects_used(self) -> list[str]:
        """Ids of the established projects this workspace cites, in the order they were first used."""
        used = []
        for g in self.goals.values():
            if g.established_by and g.established_by not in used:
                used.append(g.established_by)
        return used

    def background(self) -> str:
        """Lean code that every check needs first: the established results this workspace cites."""
        return self.library_code(self.projects_used())

    def saved_goals(self) -> dict:
        """The goals as plain data, for saving to disk (Workspace(..., goals=...) reads it back)."""
        return {name: asdict(g) for name, g in self.goals.items()}

    def to_dict(self) -> dict:
        goals = {}
        for name, g in self.goals.items():
            goals[name] = {**asdict(g), "attempts": g.attempts[-12:], "assumes": self.assumed_below(name)}
        return {"root": ROOT, "goals": goals, "busy": self.busy}

    def summary(self) -> str:
        """The whole tree in plain text, for the side assistant."""
        lines = []

        def walk(name: str, depth: int) -> None:
            g = self.goals[name]
            pad = "  " * depth
            lines.append(f"{pad}- {name} [{g.status}]: {g.lean}")
            if g.sketch:
                lines.append(f"{pad}  plan: {g.sketch}  (main proof: {g.proof!r})")
            if g.feedback and g.status != "proved":
                lines.append(f"{pad}  Lean said: {g.feedback[:300]}")
            for child in g.children:
                walk(child, depth + 1)

        walk(ROOT, 0)
        if self.notes:
            lines.append("Hints from the mathematician: " + " | ".join(self.notes))
        return "\n".join(lines)

    # ---------- changing the tree ----------

    def _emit_goal(self, name: str) -> None:
        self.on_event({"kind": "goal", "name": name, "workspace": self.to_dict()})

    def _log(self, text: str) -> None:
        self.on_event({"kind": "log", "text": text})

    def add_note(self, text: str) -> None:
        """Remember a hint. A job that is running now gets it at its next step."""
        self.notes.append(text.strip())
        self.session.add_hint(text)

    def clear_notes(self) -> None:
        self.notes = []

    def propose(self, name: str, hints: list[str] = (), n: int = 2) -> list[dict]:
        """Plans for `name` from the model, with Lean's verdict on whether each one fits together."""
        g = self.goals[name]
        plans = propose_plans(g.lean, n, [*self.notes, *hints], g.feedback)
        taken = set(self.goals) - set(self.descendants(name))
        for plan in plans:
            rename_clashes(plan, name, taken)
        problems = check_skeletons(g.lean, plans)
        return [
            {
                "sketch": p.sketch,
                "main_proof": p.main_proof,
                "lemmas": [{"name": lem.name, "lean": lem.statement, "latex": lem.latex} for lem in p.lemmas],
                "problems": found,
            }
            for p, found in zip(plans, problems, strict=True)
        ]

    def set_plan(self, name: str, sketch: str, main_proof: str, lemmas: list[dict]) -> list[str]:
        """Make this the plan for `name`, as proposed or after you edited it.

        `lemmas` is a list of {"name", "lean"} with an optional "latex". Lean is the source of
        truth: when a lemma has no LaTeX yet (or its Lean changed), the LaTeX is translated from
        the Lean. Returns the plan's problems; an empty list means Lean says the plan fits together.
        """
        goal = self.goals[name]
        new_names = [lemma["name"].strip() for lemma in lemmas]
        self._check_new_names(name, new_names)

        # Take the old plan's goals out of the tree. A lemma that comes back unchanged (same name,
        # same Lean) keeps its proof, its own plan and its attempts.
        old_goals = {n: self.goals.pop(n) for n in self.descendants(name)}
        for lemma, lemma_name in zip(lemmas, new_names, strict=True):
            lean = lemma["lean"].strip()
            old = old_goals.get(lemma_name)
            if old is not None and old.lean == lean:
                for n in subtree(old_goals, lemma_name):
                    self.goals[n] = old_goals[n]
            else:
                self.goals[lemma_name] = Goal(lemma_name, lean, lemma.get("latex", ""), lemma.get("informal", ""))
                if lemma.get("established_by"):
                    self.goals[lemma_name].established_by = lemma["established_by"]

        goal.children = new_names
        goal.sketch = sketch.strip()
        goal.proof = main_proof.strip()
        if goal.status != "assumed":
            goal.status = "open"
        self._fill_latex(new_names)
        for n in new_names:  # lemmas taken from established projects: cite them now
            lemma_goal = self.goals[n]
            if lemma_goal.established_by and lemma_goal.status != "proved":
                self._cite(lemma_goal)
        goal.problems = self._plan_problems(goal)
        self._emit_goal(name)
        return goal.problems

    def _check_new_names(self, name: str, new_names: list[str]) -> None:
        """Lemma names must be valid Lean names, different from each other and from every other goal."""
        used_elsewhere = set(self.goals) - set(self.descendants(name))
        invalid = [n for n in new_names if not NAME_RE.match(n)]
        repeated = sorted({n for n in new_names if new_names.count(n) > 1})
        clashing = sorted(used_elsewhere & set(new_names))
        if invalid:
            raise ValueError(f"not valid Lean names: {', '.join(invalid)}")
        if repeated:
            raise ValueError(f"used twice in this plan: {', '.join(repeated)}")
        if clashing:
            raise ValueError(f"already used by another goal: {', '.join(clashing)}")

    def _plan_problems(self, goal: Goal) -> list[str]:
        """Ask Lean whether the plan fits together: every lemma statement makes sense, and the
        main proof compiles when the lemmas are assumed (`sorry`)."""
        if not goal.proof:
            return ["the plan needs a main proof"]
        problems = []
        for n in goal.children:
            errors = statement_errors(self.goals[n].lean)
            if errors:
                problems.append(f"{n}: {errors[0][:300]}")
        if problems:
            return problems
        plan = Plan(goal.sketch, self.lemmas_for(goal.children), goal.proof)
        return check_skeletons(goal.lean, [plan])[0]

    def clear_plan(self, name: str) -> None:
        """Forget the plan: `name` will be proved directly."""
        g = self.goals[name]
        for n in self.descendants(name):
            del self.goals[n]
        g.children, g.sketch, g.proof, g.problems = [], "", "", []
        if g.status != "assumed":
            g.status = "open"
        self._emit_goal(name)

    def edit_statement(self, name: str, lean: str) -> list[str]:
        """Change a lemma's Lean statement (not the theorem's). Returns Lean's errors ([] = fine)."""
        if name == ROOT:
            raise ValueError("the theorem itself was approved at the start")
        if errors := statement_errors(lean):
            return errors
        g = self.goals[name]
        g.lean, g.latex, g.status, g.proof, g.feedback = lean.strip(), "", "open", "", ""
        self._fill_latex([name])
        parent = self.parent(name)
        parent.problems = self._plan_problems(parent)  # the plan above may no longer fit
        self._emit_goal(name)
        return []

    def assume(self, name: str, on: bool = True) -> None:
        g = self.goals[name]
        if on and g.status != "proved":
            g.status = "assumed"
        elif not on and g.status == "assumed":
            g.status = "open"
        self._emit_goal(name)

    def use_established(self, name: str, project_id: str) -> None:
        """Prove `name` by citing the main theorem of an established project."""
        g = self.goals[name]
        for n in self.descendants(name):  # a cited result needs no plan of its own
            del self.goals[n]
        g.children, g.sketch, g.problems = [], "", []
        g.established_by = project_id
        self._cite(g)
        self._log(f"{name}: " + ("proved by citing an established project" if g.status == "proved" else g.feedback))
        self._emit_goal(name)

    def _cite(self, g: Goal) -> None:
        """Ask Lean whether the established theorem proves `g`. Its statement must match, up to
        hypotheses that `assumption` can fill in."""
        g.proof = f"apply Project_{g.established_by}.main <;> assumption"
        self._check_main_proof(g)
        if g.status != "proved":
            g.feedback = "the established theorem doesn't prove this statement: " + g.feedback

    def _fill_latex(self, names: list[str]) -> None:
        """Write the missing LaTeX from the Lean, several at a time."""
        missing = [n for n in names if not self.goals[n].latex]
        with ThreadPoolExecutor(max_workers=4) as pool:
            for n, latex in zip(missing, pool.map(lambda n: to_latex(n, self.goals[n].lean), missing), strict=True):
                self.goals[n].latex = latex

    # ---------- proving (one job at a time; the server runs these in a thread) ----------

    @property
    def busy(self) -> bool:
        return self._job.locked()

    def run(self, job: Callable[[], None]) -> None:
        if not self._job.acquire(blocking=False):
            raise Busy("something is already running here")
        self.session = Session()
        try:
            job()
        finally:
            self._job.release()
            self.on_event({"kind": "idle", "workspace": self.to_dict()})

    def prove(self, name: str) -> None:
        """Prove `name`: its open lemmas first (in order), then its own proof."""
        g = self.goals[name]
        if g.status in ("proved", "assumed") or self.session.stopped:
            return
        for child in g.children:
            self.prove(child)
        g.status = "working"
        self._emit_goal(name)
        if g.children:
            # A plan with lemmas: the lemmas are done, so check the proof that combines them.
            self._check_main_proof(g)
        elif g.proof:
            # A plan without lemmas: try its proof; if Lean rejects it, prove the goal directly.
            self._check_main_proof(g)
            if g.status != "proved":
                self._prove_directly(g)
        else:
            self._prove_directly(g)
        self._log(f"{name}: {g.status}" + (f" ({g.feedback[:120]})" if g.status != "proved" else ""))
        self._emit_goal(name)

    def try_proof(self, name: str, proof: str) -> tuple[bool, str]:
        """Check a proof written by hand. For a goal with a plan it becomes the plan's main proof
        (so it may use the plan's lemmas); otherwise it is a direct proof. Returns (accepted, error)."""
        g = self.goals[name]
        before = (g.proof, g.status, g.sketch)  # kept if Lean rejects the new proof
        g.proof = proof.strip()
        g.status = "working"
        self._emit_goal(name)
        if g.children:
            checked = len(g.attempts)
            self._check_main_proof(g)  # needs the lemmas proved (or assumed) first
            if len(g.attempts) > checked:
                g.attempts[-1]["source"] = "written"
        else:
            preamble = (self.background() + "\n\n" + preamble_for(self.lemmas_for(self.context_for(name)))).strip()
            result = check_decls([Decl(name, g.lean, g.proof)], preamble)[name]
            error = "\n".join(result.errors)[:MAX_ERROR]
            g.attempts.append({"proof": g.proof, "ok": result.ok, "error": error, "source": "written"})
            g.status, g.feedback = ("proved", "") if result.ok else ("stuck", error)
            if result.ok:
                g.established_by, g.sketch = "", ""
        accepted, error = g.status == "proved", g.feedback
        if not accepted and before[1] in ("proved", "assumed"):
            g.proof, g.status, g.sketch = before  # a failed try doesn't undo earlier work
        self._log(f"{name}: " + ("Lean accepted the proof" if accepted else "Lean rejected the proof"))
        self._emit_goal(name)
        return accepted, error

    def _prove_directly(self, g: Goal) -> None:
        def record(attempt: dict) -> None:
            g.attempts.append({k: attempt[k] for k in ("proof", "ok", "error", "source")})
            self.on_event({"kind": "attempt", "name": g.name, "ok": attempt["ok"], "source": attempt["source"]})

        usable = self.lemmas_for(self.context_for(g.name))
        informal = " ".join([g.informal, *(f"Hint: {note}" for note in self.notes)]).strip()
        lemma = Lemma(name=g.name, statement=g.lean, informal=informal)
        lemma = prove_lemma(lemma, usable, record, background=self.background())
        g.status = lemma.status if lemma.status != "open" else "stuck"
        g.proof, g.feedback = lemma.proof, lemma.feedback
        if g.status == "proved":
            g.established_by = ""  # proved on its own, so it no longer relies on another project

    def _check_main_proof(self, g: Goal) -> None:
        if not g.proof:
            g.status, g.feedback = "stuck", "the plan has no main proof yet"
            return
        below = self.descendants(g.name)
        if any(self.goals[n].status not in ("proved", "assumed") for n in below):
            g.status, g.feedback = "stuck", "some of its lemmas are not proved yet"
            return
        lemmas_preamble = preamble_for(self.lemmas_for([*self.context_for(g.name), *below]))
        preamble = (self.background() + "\n\n" + lemmas_preamble).strip()
        result = check_decls([Decl(g.name, g.lean, g.proof)], preamble)[g.name]
        g.attempts.append(
            {"proof": g.proof, "ok": result.ok, "error": "\n".join(result.errors)[:MAX_ERROR], "source": "plan"}
        )
        # Lemmas that are only assumed appear as `sorry` in the preamble; that doesn't count against
        # this proof. The "assumes" list in to_dict() keeps track of them.
        g.status = "proved" if result.ok else "stuck"
        g.feedback = "" if g.status == "proved" else (result.errors[0][:MAX_ERROR] if result.errors else "")

    def auto(self, name: str, rounds: int = 2) -> None:
        """Let the full search prove `name` on its own; its plan becomes this goal's plan."""
        g = self.goals[name]
        g.status = "working"
        self._emit_goal(name)
        self._log(f"{name}: searching on its own…")
        result = search(g.lean, list(self.notes), rounds=rounds, on_event=self._relay, session=self.session)
        if not result.proved:
            g.status, g.feedback = "stuck", result.message
            self._log(f"{name}: no proof found ({result.message})")
            self._emit_goal(name)
            return
        plan = result.plan
        rename_clashes(plan, name, set(self.goals) - set(self.descendants(name)))
        for n in self.descendants(name):
            del self.goals[n]
        for lem in plan.lemmas:
            self.goals[lem.name] = Goal(lem.name, lem.statement, lem.latex, lem.informal, "proved", lem.proof)
        g.children, g.sketch, g.proof, g.problems = [lem.name for lem in plan.lemmas], plan.sketch, plan.main_proof, []
        if not g.children:  # proved directly by automation: no plan to show
            g.sketch = ""
        g.status, g.feedback = "proved", ""
        self._fill_latex(g.children)
        self._log(f"{name}: proved by the search")
        self._emit_goal(name)

    def _relay(self, event: dict) -> None:
        """Pass the search's progress on as short log lines."""
        kind = event["kind"]
        if kind == "plan_start" and event.get("depth", 0) == 0:
            self._log(f"trying: {event['sketch']}")
        elif kind in ("lemma_proved", "lemma_refuted", "lemma_stuck") and event.get("depth", 0) == 0:
            self._log(f"  {event['name']}: {kind.removeprefix('lemma_')}")
        elif kind == "attempt":
            self.on_event({"kind": "attempt", "name": event.get("statement", "")[:40], "ok": event["ok"]})

    def finish(self) -> dict:
        """The whole tree as one Lean file, through the strict final check."""
        root = self.goals[ROOT]
        if root.status != "proved":
            raise ValueError("the theorem is not proved yet")
        if assumed := self.assumed_below(ROOT):
            raise ValueError(f"still assumed, not proved: {', '.join(assumed)}")
        plan = Plan(root.sketch, self.lemmas_for(self.descendants(ROOT)), root.proof)
        own_code = assemble(root.lean, plan)  # this project's lemmas and theorem
        full_code = (self.background() + "\n\n" + own_code).strip()  # plus the established results it cites
        final = verify_final(full_code, f"{NAMESPACE}.main")
        return {"ok": final.ok, "errors": final.errors, "lean_code": full_code, "own_code": own_code, "plan": plan}


def subtree(goals: dict[str, Goal], name: str) -> list[str]:
    """`name` and every goal below it, looked up in `goals`."""
    out = [name]
    for child in goals[name].children:
        out += subtree(goals, child)
    return out


def rename_clashes(plan: Plan, owner: str, taken: set[str]) -> None:
    """Rename the plan's lemmas whose names are already used elsewhere to `<owner>_<name>`."""
    renames = {}
    for lem in plan.lemmas:
        new, i = lem.name, 1
        while new in taken or new in renames.values():
            new = f"{owner}_{lem.name}" if i == 1 else f"{owner}_{lem.name}_{i}"
            i += 1
        if new != lem.name:
            renames[lem.name] = new

    def rename(text: str) -> str:
        for old, new in renames.items():
            text = re.sub(rf"\b{re.escape(old)}\b", new, text)
        return text

    for lem in plan.lemmas:
        lem.name, lem.statement, lem.proof = renames.get(lem.name, lem.name), rename(lem.statement), rename(lem.proof)
    plan.main_proof = rename(plan.main_proof)
