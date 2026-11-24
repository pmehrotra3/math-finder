import json

import pytest

from mathfinder import llm, pipeline
from mathfinder.proving import prover


@pytest.fixture(autouse=True)
def temp_folders(tmp_path, monkeypatch):
    """No test touches the real lemma cache or runs/ folder."""
    monkeypatch.setattr(prover, "CACHE_FILE", tmp_path / "lemmas.json")
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")


@pytest.fixture
def fake_llm(monkeypatch):
    """fake_llm(reply, ...) scripts the model in order; fake_llm(function) answers (system, prompt).
    Non-string replies are sent as JSON. Returns the list of prompts the model received."""

    def install(*replies):
        queue, prompts = list(replies), []

        def backend(system, prompt):
            prompts.append(prompt)
            reply = queue[0](system, prompt) if queue and callable(queue[0]) else queue.pop(0)
            return reply if isinstance(reply, str) else f"```json\n{json.dumps(reply)}\n```"

        monkeypatch.setitem(llm.BACKENDS, "fake", backend)
        monkeypatch.setenv("MATHFINDER_LLM", "fake")
        return prompts

    return install
