"""Tests for mathfinder/bench.py and the `bench` command."""

import json

import pytest

from mathfinder import bench, cli
from mathfinder.lean import Decl, check_decls
from mathfinder.planner import Lemma, Plan
from mathfinder.search import SearchResult


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


def test_run_bench_records_and_saves_every_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(bench, "RUNS_DIR", tmp_path / "runs")

    def fake_search(statement, rounds, on_event):
        on_event({"kind": "attempt", "ok": True})
        if "fail" in statement:
            return SearchResult(False, message="no verified proof after 2 rounds")
        plan = Plan("p", [Lemma("k", ": True", "", status="proved", proof="trivial")], "exact k")
        return SearchResult(True, "theorem main : True := by\n  exact k\n", plan, 1, "verified")

    monkeypatch.setattr(bench, "search", fake_search)
    problems = [
        {"name": "good", "topic": "t", "statement": ": True"},
        {"name": "bad", "topic": "t", "statement": ": fail"},
    ]
    rows, md_path = bench.run_bench(problems, rounds=2, on_progress=lambda line: None)

    assert [(r["name"], r["proved"], r["attempts"]) for r in rows] == [("good", True, 1), ("bad", False, 1)]
    assert rows[0]["how"] == "plan with 1 lemma(s), round 1" and rows[0]["steps"] == 1
    assert json.loads(md_path.with_suffix(".json").read_text())[1]["name"] == "bad"
    assert len(list((tmp_path / "runs").glob("bench-*/*/trace.jsonl"))) == 2  # each run was traced


def test_bench_command_can_run_only_some_problems(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_run_bench(problems, rounds):
        seen["names"] = [p["name"] for p in problems]
        return [], tmp_path / "x.md"

    monkeypatch.setattr(cli, "run_bench", fake_run_bench)
    monkeypatch.setattr(cli, "results_table", lambda rows: "TABLE")
    assert cli.main(["bench", "--only", "gcd_180_168", "two_pow_ge"]) == 0
    assert seen["names"] == ["gcd_180_168", "two_pow_ge"]  # file order, filtered
    assert "TABLE" in capsys.readouterr().out


@pytest.mark.lean
def test_every_benchmark_statement_typechecks():
    problems = bench.load_problems()
    results = check_decls([Decl(p["name"], p["statement"], "sorry") for p in problems])
    assert {p["name"]: results[p["name"]].errors for p in problems} == {p["name"]: [] for p in problems}
