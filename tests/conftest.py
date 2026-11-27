import json
from types import SimpleNamespace

import pytest

from mathfinder import llm, pipeline
from mathfinder.lean.checker import DeclResult
from mathfinder.proving import prover
from mathfinder.proving import workspace as wsmod


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


@pytest.fixture
def fake(monkeypatch):
    """Lemmas called good_* get proved, bad_* get stuck. Every plan fits; LaTeX is "$<lean>$"."""
    seen = {"proved": [], "events": []}

    def prove_lemma(lemma, earlier, on_attempt, background=""):
        ok = lemma.name.startswith("good")
        seen["proved"].append((lemma.name, [e.name for e in earlier]))
        on_attempt({"proof": "simp", "ok": ok, "error": "" if ok else "simp failed", "source": "llm"})
        lemma.status, lemma.proof, lemma.feedback = ("proved", "simp", "") if ok else ("stuck", "", "simp failed")
        return lemma

    def check_decls(decls, preamble=""):
        seen["preamble"] = preamble
        return {d.name: DeclResult(d.name, ok=True) for d in decls}

    monkeypatch.setattr(wsmod, "prove_lemma", prove_lemma)
    monkeypatch.setattr(wsmod, "check_decls", check_decls)
    monkeypatch.setattr(wsmod, "check_skeletons", lambda theorem, plans: [[] for _ in plans])
    monkeypatch.setattr(wsmod, "statement_errors", lambda s: ["unknown identifier"] if "nope" in s else [])
    monkeypatch.setattr(wsmod, "to_latex", lambda name, lean: f"${lean}$")
    monkeypatch.setattr(wsmod, "verify_final", lambda code, name: SimpleNamespace(ok=True, errors=[], axioms=[]))
    return seen
