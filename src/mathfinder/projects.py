"""Projects: every theorem you work on is a project, saved on disk so you can come back to it.

A project holds the workspace (the tree of goals), the chat with the assistant and,
once the whole proof passed Lean's final check, the verified Lean code. From then on the
project is ESTABLISHED: other projects can use its theorem as a lemma.

How using an established result works in Lean: the project's verified code is kept in a
namespace of its own (Project_<id>), so its lemma names can't clash with anyone else's.
A project that uses it puts that code in front of its own, and proves the lemma by applying
the established theorem. Lean therefore still checks every step, including the borrowed ones.

Projects are JSON files in runs/projects/<id>.json.
"""

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mathfinder import PROJECT_ROOT

PROJECTS_DIR = PROJECT_ROOT / "runs" / "projects"


@dataclass
class Project:
    id: str
    title: str  # the theorem as you wrote it (LaTeX), or its Lean statement
    statement: str  # the approved Lean statement
    latex: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    goals: dict = field(default_factory=dict)  # the workspace's goals (see proving/workspace.py)
    # the chat: {"role": "you" | "assistant" | "event", "text": ..., "actions": [...], "time": ...}
    # ("event" lines come from the proof itself: "step_one: proved", ...)
    conversation: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # hints for every plan and proof attempt
    result: dict | None = None  # the last final check: {proved, lean_code, explanation, folder, message}
    established_code: str = ""  # verified Lean, in namespace Project_<id>; "" = not established yet
    uses: list[str] = field(default_factory=list)  # ids of established projects this one builds on

    @property
    def namespace(self) -> str:
        return f"Project_{self.id}"

    @property
    def established(self) -> bool:
        return bool(self.established_code)

    def summary(self) -> dict:
        """What the project list shows."""
        root = self.goals.get("main", {})
        return {
            "id": self.id,
            "title": self.title,
            "statement": self.statement,
            "latex": self.latex,
            "updated": self.updated,
            "status": "established" if self.established else root.get("status", "open"),
            "lemmas": max(0, len(self.goals) - 1),
            "uses": self.uses,
        }


class ProjectStore:
    """All projects, kept in memory and written to disk after every change."""

    def __init__(self, folder: Path = PROJECTS_DIR):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.projects: dict[str, Project] = {}
        for path in sorted(self.folder.glob("*.json")):
            project = Project(**json.loads(path.read_text()))
            project.conversation = [*upgraded(project.conversation)]
            for goal in project.goals.values():  # a job that was running when the server stopped is gone
                if goal.get("status") == "working":
                    goal["status"] = "open"
            self.projects[project.id] = project

    def create(self, statement: str, latex: str = "") -> Project:
        project = Project(id=uuid.uuid4().hex[:8], title=latex or statement, statement=statement, latex=latex)
        self.save(project)
        return project

    def save(self, project: Project) -> None:
        project.updated = time.time()
        with self.lock:
            self.projects[project.id] = project
            path = self.folder / f"{project.id}.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(project), ensure_ascii=False, indent=1))
            temporary.replace(path)  # replace in one step, so a crash never leaves half a file

    def delete(self, project_id: str) -> None:
        users = [p.title for p in self.projects.values() if project_id in p.uses]
        if users:
            raise ValueError(f"other projects use this one as a lemma: {', '.join(users)}")
        with self.lock:
            self.projects.pop(project_id, None)
            (self.folder / f"{project_id}.json").unlink(missing_ok=True)

    def summaries(self) -> list[dict]:
        return [p.summary() for p in sorted(self.projects.values(), key=lambda p: -p.updated)]

    def library(self, excluding: str = "") -> list[dict]:
        """Established results that another project may use (never itself, never one that uses it)."""
        return [
            {**p.summary(), "theorem": f"{p.namespace}.main"}
            for p in self.projects.values()
            if p.established and p.id != excluding and excluding not in self.everything_used_by(p.id)
        ]

    def everything_used_by(self, project_id: str) -> list[str]:
        """The projects `project_id` builds on, directly or through others; the deepest ones first."""
        order: list[str] = []

        def visit(pid: str) -> None:
            for used in self.projects[pid].uses:
                if used not in order and used in self.projects:
                    visit(used)
                    order.append(used)

        visit(project_id)
        return order

    def lean_code_for(self, used_ids: list[str]) -> str:
        """The verified Lean of these established projects and of everything they build on,
        each project exactly once, in an order where every project comes after what it uses."""
        order: list[str] = []
        for pid in used_ids:
            for needed in [*self.everything_used_by(pid), pid]:
                if needed not in order:
                    order.append(needed)
        return "\n\n".join(self.projects[pid].established_code for pid in order)


def upgraded(conversation: list):
    """Older projects saved the chat as [question, answer] pairs; turn those into chat lines."""
    for entry in conversation:
        if isinstance(entry, list):
            yield {"role": "you", "text": entry[0]}
            yield {"role": "assistant", "text": entry[1]}
        else:
            yield entry
