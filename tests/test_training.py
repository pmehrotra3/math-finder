"""Tests for training/prepare.py and training/finetune.py (no machine-learning libraries needed)."""

import json

import pytest

from training import finetune, prepare


def test_split_is_stable_and_about_ten_percent():
    statements = [f"(n : ℕ) : n + {i} = {i} + n" for i in range(2000)]
    splits = [prepare.split_of(s) for s in statements]
    assert splits == [prepare.split_of(s) for s in statements]  # same answer every time
    assert 0.07 < splits.count("test") / len(splits) < 0.13
    assert prepare.split_of("(n : ℕ) :  n = n") == prepare.split_of("(n : ℕ) : n = n")  # spacing ignored


def test_rows_have_the_shapes_trl_expects():
    sft = prepare.sft_rows([
        {"type": "lemma", "statement": "S", "context": ["theorem a : True"], "proof": "simp", "source": "llm"},
        {"type": "theorem", "statement": "T", "context": [], "proof": "whole file", "source": "search"},
    ])
    assert len(sft) == 1  # theorem rows are skipped
    roles = [m["role"] for m in sft[0]["messages"]]
    assert roles == ["system", "user", "assistant"] and sft[0]["messages"][2]["content"] == "simp"
    assert "theorem a : True" in sft[0]["messages"][1]["content"]

    [dpo] = prepare.dpo_rows([{"statement": "S", "context": [], "chosen": "good", "rejected": "bad"}])
    assert dpo["chosen"][0]["content"] == "good" and dpo["rejected"][0]["content"] == "bad"

    labels = prepare.value_rows([{"statement": "S", "label": "proved"}, {"statement": "T", "label": "stuck"}])
    assert [v["label"] for v in labels] == [1, 0]


def test_prepare_keeps_each_goal_on_one_side(tmp_path):
    rows = [{"type": "lemma", "statement": f"(n : ℕ) : n * {i} = {i} * n", "context": [], "proof": p, "source": "llm"}
            for i in range(60) for p in ("ring", "simp")]  # two proofs of every goal
    (tmp_path / "sft.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    counts = prepare.prepare(tmp_path)
    assert counts["train"]["sft_chat"] + counts["test"]["sft_chat"] == 120
    train = {json.loads(l)["statement"] for l in (tmp_path / "train" / "sft_chat.jsonl").read_text().splitlines()}
    test = {json.loads(l)["statement"] for l in (tmp_path / "test" / "sft_chat.jsonl").read_text().splitlines()}
    assert train and test and not (train & test)  # no goal in both


def test_finetune_dry_run_reports_without_training(tmp_path, capsys):
    data = tmp_path / "sft_chat.jsonl"
    data.write_text(json.dumps({"messages": [{"role": "user", "content": "x" * 40},
                                             {"role": "assistant", "content": "simp"}]}) + "\n")
    finetune.main(["--data", str(data), "--dry-run"])
    out = capsys.readouterr().out
    assert "1 examples, about 11 tokens" in out and "dry run: not training." in out


def test_finetune_refuses_an_empty_dataset(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(SystemExit):
        finetune.main(["--data", str(empty), "--dry-run"])
