"""2026-08-08 — /api/v1/fiber/summary published six confident zeros.

Measured live on a KEYLESS endpoint (allow-listed in free_tier_gate.py:187):

    fiber_routes: 0             while /api/v1/stats said 55,079 and the server
                               card, llms.txt and the MCP handshake all
                               advertise 55,064
    legacy_metro_dark_fiber: 0 while /api/v1/stats said 59, from a table of
                               the same name
    legacy_fiber_routes: 0
    major_hubs: 0
    subsea_planned: 0
    last_sync: null

Two causes, and the second is the one that generalises:

1. WRONG TABLE — `fiber_routes` was counted from `fiber_route_geometry`, not
   the `fiber_routes` table that /api/v1/stats reads.

2. `except Exception: stats[...] = 0` on every count. A missing table, a
   permission error and a genuinely empty table produced identical output. A
   zero is a MEASUREMENT; absence must be null.

These tests read the shipped source with `ast`/regex rather than importing the
app, per the house rule that tests never import main.py.
"""
import pathlib
import re

import pytest

SRC = (pathlib.Path(__file__).resolve().parent.parent / "fiber_integration.py")


@pytest.fixture(scope="module")
def src():
    return SRC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def summary_fn(src):
    """Just the body of fiber_intelligence_summary."""
    start = src.index("def fiber_intelligence_summary")
    nxt = src.find("\n    @app.route", start)
    end = nxt if nxt != -1 else src.find("\n    logger.info", start)
    assert end > start, "could not bound fiber_intelligence_summary"
    return src[start:end]


def test_fiber_routes_counts_the_canonical_table(summary_fn):
    """It must count the SAME table /api/v1/stats counts, or the two surfaces
    publish different numbers under one name."""
    assert "FROM fiber_route_geometry" not in summary_fn, (
        "fiber_route_geometry is the wrong table — it published 0 against an "
        "advertised 55,064")
    assert re.search(r"SELECT COUNT\(\*\) FROM fiber_routes\b", summary_fn), (
        "fiber_routes must be counted from the fiber_routes table, matching "
        "main.py's total_fiber_routes")


def test_no_count_falls_back_to_a_fabricated_zero(summary_fn):
    """The whole bug class: on failure, produce null — never 0.

    ★The first version of this test only matched ASSIGNMENTS (`... = 0`) inside
    an except block. Mutation-testing caught that it sailed straight past
    `return 0, None` in the helper — which is the MOST likely regression shape,
    since that is where the failure value is now produced. Cover both forms.
    """
    offenders = []
    for m in re.finditer(r"except[^\n]*:\s*\n(?:[ \t]+[^\n]*\n){0,6}", summary_fn):
        block = m.group(0)
        if re.search(r"=\s*0\s*(?:#.*)?$", block, re.M):
            offenders.append("assigns 0: " + block.strip()[:110])
        if re.search(r"\breturn\s+0\b", block):
            offenders.append("returns 0: " + block.strip()[:110])
    assert not offenders, (
        "an except handler produces 0 — absence must be null, never a measured "
        "zero:\n  " + "\n  ".join(offenders))


def test_failed_reads_are_null_and_declared(summary_fn):
    """A null must be accompanied by a reason, so a reader can tell 'we could
    not read this' from 'we measured zero'."""
    assert "unavailable" in summary_fn, (
        "the response must declare which fields could not be read")
    assert re.search(r"stats\[key\]\s*=\s*value", summary_fn), (
        "counts must be assigned from the helper's return value, which is None "
        "on failure")
    assert "basis" in summary_fn, (
        "each count must name the table it came from")


def test_rollback_on_failure_so_one_bad_read_cannot_cascade(summary_fn):
    """In PostgreSQL a failed statement aborts the transaction and every later
    statement on that connection fails too — which turns ONE missing table into
    a page of zeros. That is the most likely mechanism behind six of them."""
    assert "rollback()" in summary_fn, (
        "a failed count must roll back, or it poisons every subsequent read")


def test_last_sync_is_not_a_phantom_key(summary_fn):
    """`stats.get('last_sync')` read a key nothing in the function ever set, so
    it was unconditionally null while implying a sync clock existed."""
    # Match the CODE usage (dict-value position), not any mention: the comment
    # documenting this fix necessarily quotes the old expression, and a guard
    # that scans for the bare string flags the explanation of its own fix.
    assert not re.search(r"'last_sync'\s*:\s*stats\.get\(", summary_fn), (
        "last_sync was read from a key never assigned — say so explicitly "
        "instead of implying a tracked sync time")
    assert "last_sync_note" in summary_fn, (
        "if last_sync is null, the response must say why")
