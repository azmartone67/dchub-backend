"""Tests for brain_draft_pr_writer.parse_gate — the post-replacement syntax gate.

★ This gate exists because of a real incident: on 2026-08-19 three brain-authored
PRs (#2915/#2916/#2917) passed every existing gate, merged, and left `main`
un-parseable. Each of the three failure MODES below is taken from one of those
commits, so this file is a regression test for an outage-shaped event, not a
hypothetical.

Nothing at module scope (CLAUDE.md — a module-scope failure aborts collection).
"""
import pytest

from routes.brain_draft_pr_writer import parse_gate


# ── the three real failure modes ─────────────────────────────────────
def test_refuses_unterminated_string_literal():
    """ai_wars_automation.py (#2916): the replacement spliced a string literal
    in half. Every shape gate passed — it is one hunk, few lines, no new
    control flow — and the file stopped parsing."""
    before = "X = {\n    'templates': [\n        \"a b c\",\n    ],\n}\n"
    after = "X = {\n    'templates': [\n        \"a b c\", d, e\",\n    ],\n}\n"
    r = parse_gate("ai_wars_automation.py", before, after)
    assert r and "breaks_syntax" in r
    assert "unterminated" in r.lower()


def test_refuses_unindent_mismatch():
    """routes/mcp_funnel.py (#2915): replacement landed at the wrong indent."""
    before = "def f():\n    if x:\n        a = 1\n    return a\n"
    after = "def f():\n    if x:\n                a = 1\n      return a\n"
    r = parse_gate("routes/mcp_funnel.py", before, after)
    assert r and "breaks_syntax" in r


def test_refuses_unexpected_indent():
    """routes/monthly_outreach.py (#2917): a `try:` re-indented into nothing."""
    before = "def f():\n    try:\n        a = 1\n    except Exception:\n        pass\n"
    after = "def f():\n        try:\n        a = 1\n    except Exception:\n        pass\n"
    r = parse_gate("routes/monthly_outreach.py", before, after)
    assert r and "breaks_syntax" in r


def test_reason_names_the_line():
    before = "a = 1\nb = 2\nc = 3\n"
    after = "a = 1\nb = (2\nc = 3\n"
    r = parse_gate("x.py", before, after)
    assert r and "line" in r


# ── it must not become a blanket refusal ─────────────────────────────
def test_allows_a_clean_edit():
    assert parse_gate("x.py", "def f():\n    return 1\n",
                      "def f():\n    return 2\n") is None


def test_allows_non_python_files():
    """We have no parser for YAML/JS/etc — pass them through rather than
    guess. The caller's other gates still apply."""
    assert parse_gate("x.yml", "a: [", "b: [") is None
    assert parse_gate("_worker.js", "function(", "function((") is None
    assert parse_gate("", "x", "y") is None


def test_allows_a_file_that_was_ALREADY_broken():
    """★ Fails closed on OUR regression, not on someone else's. If a
    pre-existing syntax error blocked the gate, every fix to that file would be
    permanently refused — including the one that repairs it."""
    broken = "def f(:\n    pass\n"
    assert parse_gate("x.py", broken, broken + "# touched\n") is None


def test_allows_a_fix_that_REPAIRS_a_broken_file():
    assert parse_gate("x.py", "def f(:\n    pass\n",
                      "def f():\n    pass\n") is None


def test_never_raises_on_weird_input():
    """A tick must not die because a file is strange."""
    for bad in (None, 123, b"bytes"):
        try:
            parse_gate("x.py", "a = 1\n", bad)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"parse_gate raised on {bad!r}: {e}")


# ── both lanes must actually call it ─────────────────────────────────
def test_both_lanes_invoke_the_gate():
    """★ A gate nobody calls is the shape that caused this incident — the
    checks all existed, none of them looked at the output. Pin the call sites."""
    import inspect

    from routes import brain_draft_pr_writer as w
    from routes import brain_review_lane as rl

    for mod, name in ((w, "brain_draft_pr_writer"), (rl, "brain_review_lane")):
        src = inspect.getsource(mod)
        body = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "parse_gate(file_path, content, new_content)" in body, (
            f"{name} computes a replacement without calling parse_gate")
