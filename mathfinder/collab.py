"""Working together: a Session is the shared notebook between you and the search.

The search runs in one thread; you (through the web page) act
from another. You can change the session at ANY time and the search picks the
change up at its next checkpoint (before each round, plan and lemma):

  add_hint("try induction on n")        - free text, given to the planner with top priority
  add_lemma("key", "(a : ℝ) : 0 ≤ a^2") - a lemma YOU think helps (it still gets proved)
  reject_lemma("bad_step")              - "this lemma is false / useless": plans using it are dropped
  pin("induction on n")                 - "keep going in this direction"
  pause() / resume()                    - the search waits at its next checkpoint
  stop()                                - end this search

And the search can ASK you: ask(question) waits until you call answer(...).
"""

import queue
import threading

from mathfinder.planner import Lemma, Plan


def normalize(statement: str) -> str:
    return " ".join(statement.split())


class Session:
    def __init__(self, answer_timeout: float = 600):
        self.answer_timeout = answer_timeout  # seconds the search waits for your answer
        self.question = None  # the question currently waiting for an answer, if any
        self._hints = []
        self._rejected = set()  # lemma names and normalized statements
        self._lock = threading.Lock()  # only one thread at a time may change the lists above
        self._running = threading.Event()  # "set" = not paused
        self._running.set()
        self._stop_reason = None
        self._answers = queue.Queue()

    # ---------- things the user does ----------

    def add_hint(self, text: str) -> None:
        with self._lock:
            self._hints.append(text.strip())

    def add_lemma(self, name: str, statement: str, informal: str = "") -> None:
        self.add_hint(
            f"The user suggests this lemma; use it if it helps (it must still be proved): "
            f"theorem {name} {statement}" + (f"  -- {informal}" if informal else "")
        )

    def reject_lemma(self, name_or_statement: str) -> None:
        with self._lock:
            self._rejected.add(normalize(name_or_statement))
            self._hints.append(f"Do NOT use this lemma, the user says it is false or useless: {name_or_statement}")

    def pin(self, approach: str) -> None:
        self.add_hint(f"The user likes this approach; keep developing it: {approach}")

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self) -> None:
        self._stop_reason = "stopped by the user"
        self._running.set()  # wake the search if it is paused, so it can notice the stop
        self._answers.put(None)  # and if it is waiting for an answer

    def answer(self, text: str) -> None:
        self._answers.put(text)

    # ---------- things the search does ----------

    def hints(self) -> list[str]:
        with self._lock:
            return list(self._hints)  # a copy, so the caller can't change ours by accident

    def is_rejected(self, lemma: Lemma) -> bool:
        with self._lock:
            return lemma.name in self._rejected or normalize(lemma.statement) in self._rejected

    def uses_rejected(self, plan: Plan) -> str | None:
        """The name of the first rejected lemma this plan uses, or None."""
        for lemma in plan.lemmas:
            if self.is_rejected(lemma):
                return lemma.name
        return None

    def checkpoint(self) -> str | None:
        """Wait while paused. Returns a reason if the search should stop now, else None."""
        self._running.wait()
        return self._stop_reason

    @property
    def stopped(self) -> bool:
        return self._stop_reason is not None

    def ask(self, question: str) -> str | None:
        """Wait for the user's answer (None after answer_timeout seconds, or if stopped)."""
        self.question = question
        try:
            answer = self._answers.get(timeout=self.answer_timeout)
        except queue.Empty:
            answer = None
        self.question = None
        return answer
