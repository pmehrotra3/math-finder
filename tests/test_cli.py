"""Tests for mathfinder/cli.py and mathfinder/bench.py. Lean and the model are faked."""

import pytest

from mathfinder import bench, cli, pipeline
from mathfinder.lean import Decl, check_decls
from mathfinder.planner import Lemma, Plan
from mathfinder.search import SearchResult
from mathfinder.translate import Formalization


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


def test_prove_command_passes_its_arguments_on(fake_formalize, monkeypatch, capsys):
    fake_formalize()
    calls = []

    def fake_prove(*args, **kwargs):
        calls.append((args, kwargs))
        return {"proved": True, "message": "ok", "lean_code": "CODE", "explanation": "WHY", "folder": "runs/x"}

    monkeypatch.setattr(cli, "prove", fake_prove)
    assert cli.main(["prove", "latex", "--yes", "--hint", "use rfl", "--rounds", "1", "--metric", "readable"]) == 0
    assert cli.main(["prove", "--lean", ": True", "--no-optimize", "--no-explain"]) == 0
    assert calls[0][0] == ("(n : ℤ) : 6 ∣ n ^ 3 - n", "latex", ["use rfl"], 1)
    assert (calls[0][1]["metric"], calls[0][1]["explained"]) == ("readable", True)
    assert calls[1][0][0] == ": True" and (calls[1][1]["metric"], calls[1][1]["explained"]) == (None, False)
    out = capsys.readouterr().out
    assert "Result: PROVED (ok)" in out and "CODE" in out and "WHY" in out


def test_sub_search_events_are_indented(capsys):
    cli.print_event({"kind": "subsearch_start", "depth": 0, "name": "hard", "statement": ": P"})
    cli.print_event(
        {"kind": "lemma_proved", "depth": 1, "name": "step", "statement": ": Q", "feedback": "hammer: simp"}
    )
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "      ↳ hard is stuck: starting a sub-search on it"
    assert out[1] == "        ✓ step : : Q   [hammer: simp]"


# ---------- benchmark ----------


def test_the_benchmark_has_20_problems_with_unique_names():
    problems = bench.load_problems()
    assert len(problems) == 20
    assert len({p["name"] for p in problems}) == 20
    assert all(p["topic"] and p["statement"] for p in problems)


def test_results_table_summarizes_by_topic():
    rows = [
        {"name": "a", "topic": "algebra", "proved": True, "seconds": 30, "how": "Automation: linarith"},
        {"name": "b", "topic": "algebra", "proved": False, "seconds": 90, "how": "no verified proof"},
        {"name": "c", "topic": "induction", "proved": True, "seconds": 60, "how": "plan with 1 lemma(s), round 1"},
    ]
    table = bench.results_table(rows)
    assert table.startswith("**2/3 proved** (67%), total time 3 min")
    assert "| 2 | `b` | algebra | ✗ not proved | 90 s | no verified proof |" in table
    assert table.endswith("By topic: algebra 1/2, induction 1/1")


def test_run_bench_survives_a_crash_and_saves_a_table(monkeypatch):
    def fake_search(statement, rounds, on_event):
        on_event({"kind": "attempt", "ok": True})
        if "crash" in statement:
            raise RuntimeError("boom")
        return SearchResult(True, "code", Plan("p", [Lemma("k", ": True", "")], "exact k"), 1, "verified")

    monkeypatch.setattr(bench, "RUNS_DIR", pipeline.RUNS_DIR)
    monkeypatch.setattr(bench, "search", fake_search)
    problems = [
        {"name": "good", "topic": "t", "statement": ": True"},
        {"name": "bad", "topic": "t", "statement": ": crash"},
    ]
    path = bench.run_bench(problems)
    table = path.read_text()
    assert "**1/2 proved**" in table and "plan with 1 lemma(s), round 1" in table and "crash: boom" in table
    assert len(list(path.parent.glob("*/trace.jsonl"))) == 2


def test_bench_command_can_run_only_some_problems(monkeypatch, tmp_path):
    seen = []
    (tmp_path / "results.md").write_text("TABLE")
    monkeypatch.setattr(cli, "run_bench", lambda problems, rounds: seen.extend(problems) or tmp_path / "results.md")
    assert cli.main(["bench", "--only", "gcd_180_168", "two_pow_ge"]) == 0
    assert [p["name"] for p in seen] == ["gcd_180_168", "two_pow_ge"]


@pytest.mark.lean
def test_every_benchmark_statement_typechecks():
    problems = bench.load_problems()
    results = check_decls([Decl(p["name"], p["statement"], "sorry") for p in problems])
    assert {p["name"]: results[p["name"]].errors for p in problems} == {p["name"]: [] for p in problems}
