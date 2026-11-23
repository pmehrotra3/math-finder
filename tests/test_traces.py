"""Tests for mathfinder/traces.py."""

import json

from mathfinder.traces import TraceLogger, chain


def test_logger_writes_one_json_line_per_event(tmp_path):
    log = TraceLogger(tmp_path / "run", "THM")
    log({"kind": "round", "number": 1})
    log.finish(True, ["a hint"])
    events = [json.loads(line) for line in (tmp_path / "run" / "trace.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in events] == ["meta", "round", "result"]
    assert events[0]["theorem"] == "THM" and "time" in events[1]
    assert events[2] == {**events[2], "proved": True, "hints": ["a hint"]}


def test_chain_calls_every_watcher():
    seen_a, seen_b = [], []
    chain(seen_a.append, seen_b.append)({"kind": "x"})
    assert seen_a == seen_b == [{"kind": "x"}]
