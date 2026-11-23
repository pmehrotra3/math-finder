"""Fine-tune a small open model on Math Finder's proofs (supervised fine-tuning with LoRA).

This is SCAFFOLDING: it shows exactly how training would work, and runs end to end on a
tiny model, but real training needs a GPU and hours. Nothing here runs unless you ask.

  # 1. check the data and settings, without any machine-learning libraries installed:
  uv run python training/finetune.py --dry-run

  # 2. a 2-step smoke test on a tiny model, on a laptop CPU (downloads ~300 MB once):
  uv run --with torch --with transformers --with datasets --with peft --with trl \
      python training/finetune.py --base-model HuggingFaceTB/SmolLM2-135M-Instruct --max-steps 2

  # 3. real training (on a GPU machine): pick a prover model from huggingface.co, e.g. a
  #    DeepSeek-Prover or Kimina-Prover model, check its card for the licence and chat template.
  ... python training/finetune.py --base-model <model id> --epochs 2

LoRA ("low-rank adaptation") trains a few small extra matrices instead of the whole model,
so it fits on one GPU, and the result is a small "adapter" you load on top of the base model.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def describe(rows: list[dict]) -> str:
    """A short report about the dataset, so you know what you're about to train on."""
    chars = sum(len(m["content"]) for r in rows for m in r["messages"])
    example = rows[0]["messages"] if rows else []
    lines = [f"{len(rows)} examples, about {chars // 4} tokens in total (1 token ≈ 4 characters)"]
    if example:
        lines.append("first example:")
        lines += [f"  [{m['role']}] {m['content'][:120]!r}" for m in example]
    return "\n".join(lines)


def train(args) -> None:
    # Imported here, not at the top: these libraries are big and only needed for real training.
    from datasets import load_dataset
    from peft import LoraConfig
    from trl import SFTConfig, SFTTrainer

    dataset = load_dataset("json", data_files=str(args.data), split="train")
    dataset = dataset.remove_columns([c for c in dataset.column_names if c != "messages"])
    config = SFTConfig(
        output_dir=str(args.out),
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,  # -1 means "use the epochs"
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,  # behaves like a batch of 8, using the memory of 1
        learning_rate=2e-4,
        logging_steps=1,
        save_strategy="no" if args.max_steps > 0 else "epoch",
        report_to="none",
    )
    lora = LoraConfig(r=args.lora_rank, lora_alpha=2 * args.lora_rank, target_modules="all-linear", task_type="CAUSAL_LM")
    trainer = SFTTrainer(model=args.base_model, train_dataset=dataset, args=config, peft_config=lora)
    trainer.train()
    trainer.save_model(str(args.out))
    print(f"saved the LoRA adapter to {args.out}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "train" / "sft_chat.jsonl")
    parser.add_argument("--base-model", default="HuggingFaceTB/SmolLM2-135M-Instruct")
    parser.add_argument("--out", type=Path, default=ROOT / "models" / "prover-lora")
    parser.add_argument("--epochs", type=float, default=2)
    parser.add_argument("--max-steps", type=int, default=-1, help="stop after this many steps (for smoke tests)")
    parser.add_argument("--lora-rank", type=int, default=16)
    parser.add_argument("--dry-run", action="store_true", help="only check the data and print the plan")
    args = parser.parse_args(argv)

    rows = load_rows(args.data)
    print(describe(rows))
    print(f"base model: {args.base_model}\noutput:     {args.out}\nepochs: {args.epochs}, max steps: {args.max_steps}, "
          f"LoRA rank: {args.lora_rank}")
    if not rows:
        raise SystemExit("no training examples: run `mathfinder export` and `training/prepare.py` first")
    if args.dry_run:
        print("dry run: not training.")
        return
    train(args)


if __name__ == "__main__":
    main()
