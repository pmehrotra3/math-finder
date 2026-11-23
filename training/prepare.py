"""Turn the exported datasets (data/*.jsonl, from `mathfinder export`) into training files.

  uv run python training/prepare.py            reads data/, writes data/train/ and data/test/

Output, in the formats the Hugging Face TRL library expects:
  sft_chat.jsonl  {"messages": [system, user, assistant]}       teach the prover to write proofs
  dpo.jsonl       {"prompt": [...], "chosen": [...], "rejected": [...]}   prefer proofs that work
  value.jsonl     {"text": statement, "label": 1 or 0, "status": ...}      predict "will this get proved?"

The train/test split is by GOAL: every row about the same statement lands on the same side.
Otherwise a model could be tested on a goal it has already seen the answer to (a "leak").
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_PERCENT = 10

SYSTEM = (
    "You are an expert in Lean 4 and Mathlib. Write ONLY the tactic proof that goes after `:= by`. "
    "Never use sorry, admit or axiom."
)


def split_of(statement: str) -> str:
    """'test' for about TEST_PERCENT% of statements, else 'train'. Always the same for the same statement."""
    digest = hashlib.sha256(" ".join(statement.split()).encode()).hexdigest()
    return "test" if int(digest, 16) % 100 < TEST_PERCENT else "train"


def user_prompt(statement: str, context: list[str]) -> str:
    text = f"Prove this lemma:\n  theorem goal {statement}"
    if context:
        text += "\n\nEarlier lemmas you may use:\n" + "\n".join(f"  {c}" for c in context)
    return text


def sft_rows(sft: list[dict]) -> list[dict]:
    rows = []
    for r in sft:
        if r["type"] != "lemma":  # theorem rows hold whole files; the prover writes tactic proofs
            continue
        rows.append({
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_prompt(r["statement"], r["context"])},
                {"role": "assistant", "content": r["proof"]},
            ],
            "statement": r["statement"],
        })
    return rows


def dpo_rows(preferences: list[dict]) -> list[dict]:
    return [
        {
            "prompt": [{"role": "system", "content": SYSTEM},
                       {"role": "user", "content": user_prompt(p["statement"], p["context"])}],
            "chosen": [{"role": "assistant", "content": p["chosen"]}],
            "rejected": [{"role": "assistant", "content": p["rejected"]}],
            "statement": p["statement"],
        }
        for p in preferences
    ]


def value_rows(value: list[dict]) -> list[dict]:
    return [{"text": v["statement"], "label": int(v["label"] == "proved"), "status": v["label"],
             "statement": v["statement"]} for v in value]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def prepare(data_dir: Path) -> dict[str, dict[str, int]]:
    """Write data_dir/train/*.jsonl and data_dir/test/*.jsonl. Returns row counts."""
    datasets = {
        "sft_chat": sft_rows(read_jsonl(data_dir / "sft.jsonl")),
        "dpo": dpo_rows(read_jsonl(data_dir / "preferences.jsonl")),
        "value": value_rows(read_jsonl(data_dir / "value.jsonl")),
    }
    counts = {"train": {}, "test": {}}
    for split in ("train", "test"):
        (data_dir / split).mkdir(parents=True, exist_ok=True)
        for name, rows in datasets.items():
            chosen = [r for r in rows if split_of(r["statement"]) == split]
            with (data_dir / split / f"{name}.jsonl").open("w") as f:
                for r in chosen:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            counts[split][name] = len(chosen)
    return counts


if __name__ == "__main__":
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    for split, numbers in prepare(data_dir).items():
        print(f"{split:5}  " + "  ".join(f"{name} {n}" for name, n in numbers.items()))
