"""Tests for mathfinder/cli.py: approval, explanation, saving, tracing, typed commands. Lean and the model are faked."""

import json

import pytest

from mathfinder import cli
from mathfinder.collab import Session
from mathfinder.formalize import Formalization
from mathfinder.llm import LLMError
from mathfinder.planner import Plan
from mathfinder.search import SearchResult


@pytest.fixture
def fake_formalize(monkeypatch):
    def install(faithful=True):
        result = Formalization("latex", "(n : ℤ) : 6 ∣ n ^ 3 - n", "For all integers n, 6 | n^3 - n.", faithful, "ok")
        monkeypatch.setattr(cli, "formalize", lambda latex: result)

    return install


def scripted_user(*answers):
    """Stands in for input(): gives the answers in order."""
    queue = list(answers)
    return lambda question: queue.pop(0)


def test_yes_returns_the_statement(fake_formalize):
    fake_formalize()
    assert cli.approve("latex", ask=scripted_user("y")) == "(n : ℤ) : 6 ∣ n ^ 3 - n"


def test_no_returns_none(fake_formalize):
    fake_formalize()
    assert cli.approve("latex", ask=scripted_user("n")) is None


def test_edit_is_checked_by_lean_before_it_is_used(fake_formalize, monkeypatch):
    fake_formalize()
    monkeypatch.setattr(cli, "statement_errors", lambda s: ["unknown identifier 'oops'"] if "oops" in s else [])
    user = scripted_user("e", "(n : ℤ) : oops", "e", "(n : ℕ) : 6 ∣ n ^ 3 - n", "y")
    assert cli.approve("latex", ask=user) == "(n : ℕ) : 6 ∣ n ^ 3 - n"


def test_auto_yes_only_when_the_judge_agrees(fake_formalize):
    fake_formalize(faithful=True)
    assert cli.approve("latex", auto_yes=True, ask=scripted_user()) is not None  # never asks
    fake_formalize(faithful=False)
    assert cli.approve("latex", auto_yes=True, ask=scripted_user("n")) is None  # asks anyway


def test_no_keyboard_means_no(fake_formalize):
    fake_formalize()

    def no_keyboard(question):
        raise EOFError

    assert cli.approve("latex", ask=no_keyboard) is None


def test_main_with_latex_and_yes_runs_the_search_explains_and_saves(fake_formalize, monkeypatch, tmp_path):
    fake_formalize()
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)
    seen = {}

    def fake_search(statement, hints, rounds, on_event):
        seen["statement"] = statement
        return SearchResult(True, "theorem main ... := by\n  decide\n", Plan("check all cases", [], "decide"))

    monkeypatch.setattr(cli, "search", fake_search)
    monkeypatch.setattr(cli, "explain", lambda theorem, plan, code: "## Theorem\nexplained")
    latex = r"For every integer $n$, $6 \mid n^3 - n$."
    assert cli.main(["prove", latex, "--yes"]) == 0
    assert seen["statement"] == "(n : ℤ) : 6 ∣ n ^ 3 - n"

    [run] = tmp_path.iterdir()  # exactly one run folder was created
    assert (run / "theorem.txt").read_text() == latex + "\n"
    assert (run / "proof.lean").read_text().startswith("import Mathlib")
    assert (run / "explanation.md").read_text() == "## Theorem\nexplained\n"
    trace = [json.loads(line) for line in (run / "trace.jsonl").read_text().splitlines()]
    assert trace[0]["kind"] == "meta" and trace[-1] == {**trace[-1], "kind": "result", "proved": True}


def test_a_failed_explanation_still_saves_the_proof(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(cli, "search", lambda *a, **k: SearchResult(True, "code\n", Plan("p", [], "simp")))

    def broken_explain(theorem, plan, code):
        raise LLMError("model unavailable")

    monkeypatch.setattr(cli, "explain", broken_explain)
    assert cli.main(["prove", "--lean", "(n : ℕ) : n = n"]) == 0
    assert "could not write an explanation" in capsys.readouterr().out
    assert len(list(tmp_path.iterdir())) == 1


def test_typed_commands_change_the_session():
    s = Session("t")
    assert cli.handle_command(s, "hint try induction on n\n") == "hint added: try induction on n"
    assert cli.handle_command(s, "lemma key (a : ℝ) : 0 ≤ a ^ 2").startswith("lemma suggested: key")
    assert cli.handle_command(s, "reject bad_step") == "rejected: bad_step"
    assert cli.handle_command(s, "pause").startswith("paused")
    assert cli.handle_command(s, "resume") == "resumed"
    assert "theorem key (a : ℝ) : 0 ≤ a ^ 2" in s.hints()[1]
    assert cli.handle_command(s, "dance").startswith("unknown command")


def test_while_a_question_is_open_any_line_is_the_answer():
    s = Session("t")
    s.question = "Any idea?"
    assert cli.handle_command(s, "hint this whole line is an answer") == "answer sent"
    assert s.ask("Any idea?") == "hint this whole line is an answer"


def test_export_command_prints_row_counts(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "runs").mkdir()
    assert cli.main(["export", "--out", str(tmp_path / "data")]) == 0
    out = capsys.readouterr().out
    assert "sft" in out and "preferences" in out and (tmp_path / "data" / "value.jsonl").exists()
