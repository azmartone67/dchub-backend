"""`idle` must not be able to mean "everything upstream was rejected".

Measured 2026-08-31: brain_automerge_log held 666 consecutive `idle` runs over
30 days — every one reading `eligible=0 merged=0 skipped=0`, last `clean` on
2026-06-25. On a board that is indistinguishable from a healthy pipeline with an
empty queue, and it was read that way for two months.

It was not empty. brain-autonomy evaluated 22 proposals in a single day and
opened 0, rejecting 21 as `not_mechanical` against a 6-class SQL/datetime
allowlist (interval_literal, tz_naive_utcnow, bool_is_active,
sqlite_datetime_on_pg, now_text_cast, immutable_index) that has essentially no
overlap with the live backlog — consistency_radar 55, mcp_per_tool_conversion
27, ai_surface_sentinel 17, none of them SQL-idiom fixes.

`eligible` counts open brain/autofix-* PRs, so 0 is genuinely true for the merge
stage: its inbox IS empty. The failure is that the stage could not say WHY, so a
structural block rendered as health.

The functions are pulled out with ast and run against stubs — no DB, no network.
"""

import ast
import pathlib

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "routes" / "brain_automerge.py")
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)


def _fn(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _load(open_count):
    """Execute _status_for and _starvation_note with a stubbed upstream count."""
    ns = {"_open_proposal_count": lambda: open_count}
    for name in ("_status_for", "_starvation_note"):
        mod = ast.Module(body=[_fn(name)], type_ignores=[])
        exec(compile(mod, str(SRC), "exec"), ns)      # noqa: S102 — the point
    return ns


# ── the distinction the board was missing ────────────────────────────

def test_empty_inbox_with_a_backlog_reports_starved_not_idle():
    """The 666-run bug, directly."""
    ns = _load(open_count=22)
    assert ns["_status_for"](eligible=0, merged=[]) == "starved"


def test_empty_inbox_with_nothing_queued_is_genuinely_idle():
    """`idle` must stay available for the real thing, or the new status is just
    a rename and the board is still uninformative."""
    ns = _load(open_count=0)
    assert ns["_status_for"](eligible=0, merged=[]) == "idle"


def test_unknown_upstream_count_does_not_claim_idle():
    """A failed COUNT returns -1. Treating that as 0 would manufacture the exact
    false 'nothing to do' this change removes."""
    ns = _load(open_count=-1)
    assert ns["_status_for"](eligible=0, merged=[]) == "idle"
    note = ns["_starvation_note"](eligible=0)
    assert "unavailable" in note, \
        "an unknown count must be declared, not silently rendered as empty"


def test_starvation_note_names_the_backlog_size():
    ns = _load(open_count=22)
    note = ns["_starvation_note"](eligible=0)
    assert "STARVED" in note
    assert "22" in note
    assert "allowlist" in note, "the note must name the CAUSE, not just the count"


def test_no_note_when_the_stage_actually_had_work():
    ns = _load(open_count=22)
    assert ns["_starvation_note"](eligible=3) == ""


# ── the pre-existing statuses are unchanged ──────────────────────────

def test_merged_and_blocked_are_untouched():
    ns = _load(open_count=22)
    assert ns["_status_for"](eligible=3, merged=[{"pr": 1}]) == "merged"
    assert ns["_status_for"](eligible=3, merged=[]) == "blocked"


def test_upstream_count_is_not_queried_when_the_stage_had_work():
    """The heartbeat runs ~22x/day. Do not add a COUNT to the path that already
    has an answer."""
    calls = {"n": 0}

    def _counter():
        calls["n"] += 1
        return 22

    ns = {"_open_proposal_count": _counter}
    for name in ("_status_for", "_starvation_note"):
        mod = ast.Module(body=[_fn(name)], type_ignores=[])
        exec(compile(mod, str(SRC), "exec"), ns)      # noqa: S102
    ns["_status_for"](eligible=5, merged=[])
    ns["_starvation_note"](eligible=5)
    assert calls["n"] == 0


# ── the count itself fails safe ──────────────────────────────────────

def test_open_proposal_count_returns_minus_one_on_failure():
    """Never guess 0 — unknown and zero must stay distinguishable."""
    src = ast.get_source_segment(TEXT, _fn("_open_proposal_count"))
    assert "return -1" in src
    assert src.count("return -1") >= 2, \
        "both the query-error and exception paths must return -1, not 0"


def test_heartbeat_uses_the_helpers():
    """A helper nothing calls is the failure mode this whole audit kept finding."""
    hb = ast.get_source_segment(TEXT, _fn("_log_run_heartbeat"))
    assert "_status_for(" in hb
    assert "_starvation_note(" in hb
    # and the old inline ternary must be gone, or both could coexist
    assert '"idle" if eligible == 0 else' not in hb
