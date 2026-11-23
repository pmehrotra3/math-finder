"""Tests for mathfinder/lean.py.

The ones marked `lean` really run Lean (about 25 seconds each).
Skip them with:  uv run pytest -m "not lean"
"""

import pytest

from mathfinder.lean import (
    Decl,
    check_decls,
    indent,
    parse_messages,
    run_lean,
    statement_errors,
    verify_final,
)

# ---------- fast tests: no Lean needed ----------


def test_indent_adds_two_spaces_per_line():
    assert indent("simp\nring") == "  simp\n  ring"


def test_render_wraps_heartbeats_and_indents_proof():
    text = Decl("t", "(n : ℕ) : n + 0 = n", "simp").render(heartbeats=1000)
    assert text == (
        "-- t\n"
        "set_option maxHeartbeats 1000 in\n"
        "example (n : ℕ) : n + 0 = n := by\n"
        "  simp\n"
    )


def test_render_with_context_uses_its_own_namespace():
    text = Decl("plan_0", ": True", "trivial", context="theorem key : True := trivial").render(1000)
    assert text == (
        "-- plan_0\n"
        "namespace plan_0\n\n"
        "theorem key : True := trivial\n\n"
        "set_option maxHeartbeats 1000 in\n"
        "example : True := by\n"
        "  trivial\n\n"
        "end plan_0\n"
    )


def test_parse_messages_reads_lean_json():
    line = (
        '{"caption":"","data":"declaration uses `sorry`","fileName":"x.lean",'
        '"kind":"hasSorry","pos":{"column":8,"line":14},"severity":"warning"}'
    )
    [msg] = parse_messages("some non-json noise\n" + line)
    assert (msg.line, msg.severity, msg.kind) == (14, "warning", "hasSorry")


# ---------- slow tests: these run Lean ----------


@pytest.fixture(scope="module")
def batch():
    """One Lean run that checks five different kinds of candidate at once."""
    preamble = "theorem helper (n : ℕ) : n * 1 = n := by simp"
    decls = [
        Decl("good", "(a b : ℝ) : a ^ 2 + b ^ 2 ≥ 2 * a * b", "nlinarith [sq_nonneg (a - b)]"),
        Decl("wrong", "(n : ℕ) : n + 1 > n + 2", "omega"),
        Decl("holey", "(n : ℕ) : n + 0 = n", "sorry"),
        Decl("nonsense", "(n : ℕ) : n = undefined_thing", "simp"),
        Decl("uses_helper", "(m : ℕ) : m * 1 = m", "exact helper m"),
    ]
    return check_decls(decls, preamble=preamble)


@pytest.mark.lean
def test_batch_correct_proof_passes(batch):
    assert batch["good"].ok
    assert batch["good"].errors == []


@pytest.mark.lean
def test_batch_wrong_proof_fails_with_lean_error(batch):
    assert not batch["wrong"].ok
    assert "omega could not prove the goal" in batch["wrong"].errors[0]


@pytest.mark.lean
def test_batch_sorry_is_not_a_proof(batch):
    assert not batch["holey"].ok
    assert batch["holey"].used_sorry


@pytest.mark.lean
def test_batch_bad_statement_fails(batch):
    assert not batch["nonsense"].ok
    assert any("undefined_thing" in e for e in batch["nonsense"].errors)


@pytest.mark.lean
def test_batch_can_use_preamble_lemmas(batch):
    assert batch["uses_helper"].ok


@pytest.mark.lean
def test_candidates_cannot_cite_each_other():
    # "first" fails. If candidates were theorems, "second" could cite it as proved.
    decls = [
        Decl("first", "(n : ℕ) : n = n + 1", "simp"),
        Decl("second", "(n : ℕ) : n = n + 1", "exact?"),
    ]
    results = check_decls(decls)
    assert not results["first"].ok
    assert not results["second"].ok


@pytest.mark.lean
def test_statement_errors_accepts_good_statement():
    assert statement_errors("(n : ℕ) : n ^ 3 - n ≥ 0") == []


@pytest.mark.lean
def test_verify_final_accepts_real_proof():
    code = "theorem amgm2 (a b : ℝ) : a ^ 2 + b ^ 2 ≥ 2 * a * b := by\n  nlinarith [sq_nonneg (a - b)]"
    result = verify_final(code, "amgm2")
    assert result.ok, result.errors
    assert set(result.axioms) <= {"propext", "Classical.choice", "Quot.sound"}


@pytest.mark.lean
def test_verify_final_rejects_sorry():
    result = verify_final("theorem t : 1 = 2 := by\n  sorry", "t")
    assert not result.ok
    assert "proof uses sorry" in result.errors


@pytest.mark.lean
def test_verify_final_rejects_custom_axiom():
    code = "axiom cheat : False\n\ntheorem t : 1 = 2 := cheat.elim"
    result = verify_final(code, "t")
    assert not result.ok
    assert "cheat" in result.axioms


@pytest.mark.lean
def test_run_lean_timeout_is_reported():
    out = run_lean("import Mathlib\n", timeout=1)
    assert out.timed_out
