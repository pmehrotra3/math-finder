"""Tests for mathfinder/llm.py.

Most tests use a fake backend, so they're instant and free.
The one `llm` test really calls Claude:  uv run pytest -m llm tests/test_llm.py
"""

from types import SimpleNamespace

import pytest

from mathfinder import llm


@pytest.fixture
def fake_replies(monkeypatch):
    """Install a fake backend that returns the given replies in order."""

    def install(*replies):
        queue = list(replies)
        calls = []

        def fake(system, prompt):
            calls.append(prompt)
            return queue.pop(0)

        monkeypatch.setitem(llm.BACKENDS, "fake", fake)
        monkeypatch.setenv("MATHFINDER_LLM", "fake")
        return calls

    return install


def test_complete_uses_the_backend_from_the_env_var(fake_replies):
    fake_replies("hello")
    assert llm.complete("sys", "hi") == "hello"


def test_unknown_backend_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("MATHFINDER_LLM", "nope")
    with pytest.raises(llm.LLMError, match="unknown MATHFINDER_LLM"):
        llm.complete("sys", "hi")


def test_extract_json_from_fenced_block():
    text = 'Here is the plan:\n```json\n{"lemmas": [1, 2]}\n```\nGood luck!'
    assert llm.extract_json(text) == {"lemmas": [1, 2]}


def test_extract_json_without_fence():
    assert llm.extract_json('Sure. {"ok": true} That is all.') == {"ok": True}
    assert llm.extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_failure_raises():
    with pytest.raises(llm.LLMError):
        llm.extract_json("no json at all")


def test_complete_json_retries_after_bad_json(fake_replies):
    calls = fake_replies("oops, not json", '```json\n{"fixed": 1}\n```')
    assert llm.complete_json("sys", "give json") == {"fixed": 1}
    assert len(calls) == 2
    assert "could not be parsed" in calls[1]


# ---------- the API backend, with a fake client (no key, no network) ----------


class FakeStream:
    def __init__(self, message, seen, kwargs):
        self.message = message
        seen.update(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def fake_client(message, seen):
    stream = lambda **kwargs: FakeStream(message, seen, kwargs)  # noqa: E731
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))


def text_message(text, stop_reason="end_turn"):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block], stop_reason=stop_reason)


def test_api_backend_sends_the_right_request(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "_client", fake_client(text_message("42"), seen))
    assert llm.complete_api("be brief", "6*7?") == "42"
    assert seen["model"] == "claude-opus-5-5"
    assert seen["output_config"] == {"effort": "high"}
    assert seen["betas"] == ["server-side-fallback-2026-07-01"]
    assert seen["fallbacks"] == "default"
    assert seen["messages"] == [{"role": "user", "content": "6*7?"}]


def test_api_backend_raises_on_refusal(monkeypatch):
    message = SimpleNamespace(content=[], stop_reason="refusal")
    monkeypatch.setattr(llm, "_client", fake_client(message, {}))
    with pytest.raises(llm.LLMError, match="declined"):
        llm.complete_api("sys", "hi")


# ---------- real call ----------


@pytest.mark.llm
def test_cli_backend_really_answers(monkeypatch):
    monkeypatch.setenv("MATHFINDER_LLM", "cli")
    answer = llm.complete("You are terse.", "Reply with exactly the word: OK")
    assert "OK" in answer
