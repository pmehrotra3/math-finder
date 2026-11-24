"""Tests for mathfinder/collab.py and how search() works with a Session.

Lean and the model are replaced by fakes, so these run instantly. Threads play
the user acting while the search runs.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from mathfinder import search as search_module
from mathfinder.collab import Session
from mathfinder.planner import Lemma, Plan

# ---------- the Session on its own ----------


def test_hints_lemmas_and_pins_become_planner_hints():
    s = Session()
    s.add_hint("try induction on n")
    s.add_lemma("key", "(n : ℕ) : Q n", "Q always holds")
    s.pin("strong induction")
    hints = s.hints()
    assert hints[0] == "try induction on n"
    assert "theorem key (n : ℕ) : Q n  -- Q always holds" in hints[1]
    assert "strong induction" in hints[2]


def test_rejected_lemma_is_found_by_name_or_statement():
    s = Session()
    s.reject_lemma("bad")
    s.reject_lemma("(x : ℝ)   :  x = 0")  # spacing doesn't matter
    good = Plan("", [Lemma("fine", ": True", "")], "")
    by_name = Plan("", [Lemma("bad", ": True", "")], "")
    by_statement = Plan("", [Lemma("other", "(x : ℝ) : x = 0", "")], "")
    assert s.uses_rejected(good) is None
    assert s.uses_rejected(by_name) == "bad"
    assert s.uses_rejected(by_statement) == "other"


def test_pause_blocks_the_checkpoint_until_resume():
    s = Session()
    s.pause()
    passed = threading.Event()
    threading.Thread(target=lambda: (s.checkpoint(), passed.set()), daemon=True).start()
    time.sleep(0.2)
    assert not passed.is_set()  # still waiting
    s.resume()
    assert passed.wait(2)


def test_ask_waits_for_an_answer_from_another_thread():
    s = Session()
    threading.Timer(0.1, lambda: s.answer("use AM-GM")).start()
    assert s.ask("Any idea?") == "use AM-GM"
    assert s.question is None


def test_ask_gives_up_after_the_timeout():
    assert Session(answer_timeout=0.1).ask("Any idea?") is None


def test_stop_wakes_a_paused_search():
    s = Session()
    s.pause()
    s.stop()
    assert s.checkpoint() == "stopped by the user"


# ---------- search() with a session (fake Lean, fake model) ----------


@pytest.fixture
def fake_world(monkeypatch):
    """Lemmas named 'good*' get proved, 'hard*' get stuck. The planner is scripted per round."""
    seen_hints = []

    def install(plans_per_round):
        rounds = list(plans_per_round)

        def fake_propose(theorem, n, hints, feedback):
            seen_hints.append(list(hints))
            return rounds.pop(0) if rounds else []

        def fake_prove(lemma, earlier, on_attempt=None):
            lemma.status = "proved" if lemma.name.startswith("good") else "stuck"
            lemma.proof = "simp" if lemma.status == "proved" else ""
            lemma.feedback = "" if lemma.status == "proved" else "simp made no progress"
            return lemma

        monkeypatch.setattr(search_module, "try_direct", lambda theorem, emit: None)
        monkeypatch.setattr(search_module, "propose_plans", fake_propose)
        monkeypatch.setattr(search_module, "check_skeletons", lambda t, plans: [[] for _ in plans])
        monkeypatch.setattr(search_module, "prove_lemma", fake_prove)
        monkeypatch.setattr(search_module, "verify_final", lambda code, name: SimpleNamespace(ok=True, errors=[]))
        monkeypatch.setattr(search_module, "MAX_DEPTH", 0)  # no sub-searches in these tests
        return seen_hints

    return install


def plan(*names):
    return Plan(f"plan with {', '.join(names)}", [Lemma(n, f"(n : ℕ) : {n} n", "") for n in names], "simp")


def test_session_hints_reach_the_planner_and_rejected_plans_are_dropped(fake_world):
    seen_hints = fake_world([[plan("bad_idea"), plan("good_a")]])
    session = Session()
    session.add_hint("use induction")
    session.reject_lemma("bad_idea")
    events = []
    result = search_module.search("t", rounds=1, on_event=events.append, session=session)
    assert result.proved
    assert "use induction" in seen_hints[0]
    rejected = [e for e in events if e["kind"] == "plan_rejected"]
    assert rejected[0]["reason"] == "uses bad_idea, which you rejected"


def test_search_asks_the_user_and_the_answer_buys_another_round(fake_world):
    seen_hints = fake_world([[plan("hard_step")], [plan("good_b")]])
    session = Session()
    events = []

    def user_watches(event):
        events.append(event)
        if event["kind"] == "ask_user":  # answer from "another person", a moment later
            threading.Timer(0.1, lambda: session.answer("split it into two cases")).start()

    result = search_module.search("t", rounds=1, on_event=user_watches, session=session)
    assert result.proved and result.rounds == 2
    question = next(e["question"] for e in events if e["kind"] == "ask_user")
    assert "hard_step" in question and "simp made no progress" in question
    assert "split it into two cases" in seen_hints[1]  # the planner saw the answer in round 2


def test_no_answer_means_giving_up(fake_world):
    fake_world([[plan("hard_step")]])
    session = Session(answer_timeout=0.1)
    events = []
    result = search_module.search("t", rounds=1, on_event=events.append, session=session)
    assert not result.proved
    kinds = [e["kind"] for e in events]
    assert kinds[-3:] == ["ask_user", "no_answer", "gave_up"]


def test_stopping_ends_the_search(fake_world):
    fake_world([[plan("good_a")]])
    session = Session()
    session.stop()
    result = search_module.search("t", rounds=1, session=session)
    assert not result.proved and session.stopped
    assert result.message == "stopped: stopped by the user"
