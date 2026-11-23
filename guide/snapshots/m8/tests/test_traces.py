"""Tests for mathfinder/traces.py."""

import json

from mathfinder.traces import TraceLogger, chain, export, read_trace, records_from_trace

GOAL = "(n : ℕ) : n + 0 = n"

EVENTS = [
    {"kind": "meta", "theorem": "THM"},
    {"kind": "attempt", "statement": GOAL, "context": [], "proof": "ring", "ok": False, "error": "ring failed", "source": "llm"},
    {"kind": "attempt", "statement": GOAL, "context": [], "proof": "simp", "ok": True, "error": "", "source": "repair"},
    {"kind": "lemma_proved", "depth": 0, "name": "a", "statement": GOAL, "feedback": "LLM"},
    {"kind": "lemma_stuck", "depth": 0, "name": "b", "statement": ": False", "feedback": "no"},
    {"kind": "done", "depth": 0, "lean_code": "theorem main ...", "plan": "p"},
    {"kind": "result", "proved": True, "hints": ["try simp"]},
]


def test_logger_writes_one_json_line_per_event(tmp_path):
    log = TraceLogger(tmp_path / "run", "THM")
    log({"kind": "round", "number": 1})
    log.finish(True, ["a hint"])
    events = read_trace(tmp_path / "run" / "trace.jsonl")
    assert [e["kind"] for e in events] == ["meta", "round", "result"]
    assert events[0]["theorem"] == "THM" and "time" in events[1]
    assert events[2] == {**events[2], "proved": True, "hints": ["a hint"]}


def test_chain_calls_every_watcher():
    seen_a, seen_b = [], []
    chain(seen_a.append, seen_b.append)({"kind": "x"})
    assert seen_a == seen_b == [{"kind": "x"}]


def test_records_from_trace_builds_all_four_datasets():
    rows = records_from_trace(EVENTS)
    assert [r["proof"] for r in rows["sft"]] == ["simp", "theorem main ..."]
    assert rows["sft"][1]["statement"] == "THM"
    assert rows["preferences"] == [{"statement": GOAL, "context": [], "chosen": "simp",
                                    "rejected": "ring", "rejected_error": "ring failed"}]
    assert rows["value"] == [{"statement": GOAL, "label": "proved"}, {"statement": ": False", "label": "stuck"}]
    assert rows["hints"] == [{"theorem": "THM", "hints": ["try simp"], "proved": True}]


def test_export_reads_every_run_and_removes_duplicates(tmp_path):
    for name in ("run1", "run2"):  # the same trace twice: rows must not be doubled
        (tmp_path / "runs" / name).mkdir(parents=True)
        (tmp_path / "runs" / name / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in EVENTS) + "\n")
    counts = export(tmp_path / "runs", tmp_path / "data")
    assert counts == {"sft": 2, "preferences": 1, "value": 2, "hints": 1}
    first = json.loads((tmp_path / "data" / "sft.jsonl").read_text().splitlines()[0])
    assert first["proof"] == "simp"
