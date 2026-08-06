"""The live "Latest AI Requests" feed — external-only, and self-explaining.

★THE BUG. The feed took the last N rows of ai_requests with NO platform
filter, while the surface renders them under "real events only". Self-traffic
lands in the SAME table with platform='internal', and it dominates: 3,321,235
of 3,899,804 all-time requests were internal when this was found (85%). So the
last 20 rows were, with near-certainty, 20 internal rows, and the page showed
"Quiet right now" while reporting 40 distinct agents and 6,221 MCP tool calls
over the same 7 days. Agent traffic was never the problem — the feed could not
see past our own.

It degrades SILENTLY as self-traffic grows: no error, no alert, just a feed
that gets quieter as the site gets busier.

★AND EMPTY MUST NOT MEAN THREE THINGS. The old body swallowed every exception
into `return []`, so a missing table, a broken query and a genuinely quiet feed
were indistinguishable on screen — which is why nobody investigated.

NO network, NO DB: _execute is monkeypatched.

★EVERY STATEMENT IS INSIDE A FUNCTION — a module-scope exit aborts collection
and takes the whole session with it (2026-07-28, twice).

Run:  python3 -m pytest tests/test_ai_recent_feed.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_tracking as t  # noqa: E402


def _rows(*platforms):
    return [{"platform": p, "endpoint": "/api/v1/x", "created_at": "2026-08-06"}
            for p in platforms]


class _Exec:
    """Stand-in for _execute that records the SQL it was handed."""

    def __init__(self, rows=None, counts=None, raise_on_select=False):
        self.rows = rows if rows is not None else []
        self.counts = counts
        self.raise_on_select = raise_on_select
        self.sql_seen = []

    def __call__(self, sql, params=None, fetch=False, fetchall=False):
        self.sql_seen.append(sql)
        if self.raise_on_select and fetchall:
            raise RuntimeError("relation \"ai_requests\" does not exist")
        if fetch:
            return self.counts
        return self.rows


# ── The filter itself ────────────────────────────────────────────────
def test_feed_excludes_our_own_traffic(monkeypatch):
    ex = _Exec(rows=_rows("claude", "chatgpt"))
    monkeypatch.setattr(t, "_execute", ex)
    feed = t.recent_activity_feed(20)
    select_sql = ex.sql_seen[0]
    assert "platform NOT IN (" in select_sql
    for bucket in ("internal", "mcp", "mcp_generic"):
        assert bucket in select_sql
    assert len(feed["activity"]) == 2


def test_exclusion_matches_the_public_roster():
    """The roster (get_cumulative_totals, r71) and this feed must hide the same
    buckets. If they drift, the page shows a platform in one place and not the
    other, which is how #1092-shaped bugs start."""
    import inspect
    roster = inspect.getsource(t.get_cumulative_totals)
    assert "'internal','mcp','mcp_generic'" in roster
    assert t._NON_PLATFORM_BUCKETS == ("internal", "mcp", "mcp_generic")


def test_include_internal_shows_the_unfiltered_stream(monkeypatch):
    """Debug escape hatch — must NOT filter, so an operator can see whether
    rows are arriving at all."""
    ex = _Exec(rows=_rows("internal", "internal"))
    monkeypatch.setattr(t, "_execute", ex)
    feed = t.recent_activity_feed(20, include_internal=True)
    assert "platform NOT IN (" not in ex.sql_seen[0]
    assert len(feed["activity"]) == 2
    assert "internal included" in feed["basis"]


# ── Quiet vs broken must be distinguishable ──────────────────────────
def test_broken_query_reports_an_error_instead_of_looking_quiet(monkeypatch):
    """★The reason this survived. A failing query used to return [] — the same
    thing a genuinely quiet feed returns. One is an outage, the other is
    nothing happening, and they need opposite responses."""
    monkeypatch.setattr(t, "_execute", _Exec(raise_on_select=True))
    feed = t.recent_activity_feed(20)
    assert feed["activity"] == []
    assert feed["error"] is not None
    assert "ai_requests" in feed["error"]


def test_genuinely_quiet_feed_has_no_error(monkeypatch):
    ex = _Exec(rows=[], counts={"c": 200, "internal_c": 200})
    monkeypatch.setattr(t, "_execute", ex)
    feed = t.recent_activity_feed(20)
    assert feed["activity"] == []
    assert feed["error"] is None
    # ...and it can SAY why: the whole recent window was ours.
    assert feed["scanned"] == 200
    assert feed["excluded_internal"] == 200


def test_diagnostics_failure_never_empties_a_working_feed(monkeypatch):
    """Best-effort means best-effort: if the count query dies, the rows we
    already fetched must still be served."""
    class _PartialFail(_Exec):
        def __call__(self, sql, params=None, fetch=False, fetchall=False):
            if fetch:
                raise RuntimeError("count blew up")
            return super().__call__(sql, params, fetch, fetchall)

    monkeypatch.setattr(t, "_execute", _PartialFail(rows=_rows("claude")))
    feed = t.recent_activity_feed(20)
    assert len(feed["activity"]) == 1
    assert feed["error"] is None
    assert feed["scanned"] is None


# ── Back-compat ──────────────────────────────────────────────────────
def test_get_recent_activity_still_returns_a_plain_list(monkeypatch):
    """The old callable keeps its shape — but now filtered."""
    ex = _Exec(rows=_rows("claude", "gemini"))
    monkeypatch.setattr(t, "_execute", ex)
    got = t.get_recent_activity(20)
    assert isinstance(got, list)
    assert [r["platform"] for r in got] == ["claude", "gemini"]
    assert "platform NOT IN (" in ex.sql_seen[0]


def test_get_recent_activity_returns_a_list_even_when_broken(monkeypatch):
    """Consumers index into this; it must never become None or a dict."""
    monkeypatch.setattr(t, "_execute", _Exec(raise_on_select=True))
    assert t.get_recent_activity(20) == []
