"""Tests for the approval step in mathfinder/cli.py. No Lean or model needed: both are faked."""

import pytest

from mathfinder import cli
from mathfinder.formalize import Formalization
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


def test_main_with_latex_and_yes_runs_the_search(fake_formalize, monkeypatch):
    fake_formalize()
    seen = {}

    def fake_search(statement, hints, rounds, on_event):
        seen["statement"] = statement
        return SearchResult(True, message="verified")

    monkeypatch.setattr(cli, "search", fake_search)
    assert cli.main(["prove", r"For every integer $n$, $6 \mid n^3 - n$.", "--yes"]) == 0
    assert seen["statement"] == "(n : ℤ) : 6 ∣ n ^ 3 - n"
