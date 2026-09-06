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
        self.params_seen = []

    def __call__(self, sql, params=None, fetch=False, fetchall=False):
        self.sql_seen.append(sql)
        self.params_seen.append(params)
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


# ═══════════════════════════════════════════════════════════════════════
#  r-honest-feed-live (2026-08-06) — OUR OWN TRAFFIC WEARING A PLATFORM'S NAME
#
#  The platform filter above removes self-traffic that ADMITS it is ours
#  (platform='internal'). It does not remove ours that arrives labelled
#  'claude'. Measured live the day this was written: one residential IP
#  polling /api/v1/mcp/handoff-funnel twice a minute — plain Chrome UA,
#  attributed 'claude' by REFERER — was 6,807 of 7,769 'claude' rows over 7
#  days and held 14 of the 20 feed slots. Real rows, false picture: the same
#  end state as the fabricator, reached from the other side.
# ═══════════════════════════════════════════════════════════════════════

def test_feed_excludes_dashboard_self_refresh_endpoints(monkeypatch):
    ex = _Exec(rows=_rows("meta"))
    monkeypatch.setattr(t, "_execute", ex)
    t.recent_activity_feed(20)
    sql = ex.sql_seen[0]
    assert "NOT LIKE ALL(" in sql, "endpoint self-refresh filter missing from the query"
    assert "NOT ILIKE ALL(" in sql, "self UA filter missing from the query"


def test_the_measured_poller_path_is_actually_covered():
    """★The regression that matters. /api/v1/mcp/handoff-funnel is the exact
    path that held 14/20 slots. If a future edit trims this list, the panel
    silently goes back to rendering one poller as live Claude traffic."""
    covered = [m for m in t.FEED_SELF_REFRESH_ENDPOINTS
               if m in "/api/v1/mcp/handoff-funnel"]
    assert covered, "the measured poller endpoint is no longer filtered"
    assert any("dchub" in m for m in t.FEED_SELF_UA_MARKERS)


def test_self_refresh_patterns_are_passed_as_params_not_interpolated(monkeypatch):
    """A literal % in a psycopg2 SQL string is a 500. These are all wildcards,
    so they must ride in params."""
    ex = _Exec(rows=[])
    monkeypatch.setattr(t, "_execute", ex)
    t.recent_activity_feed(20)
    params = ex.params_seen[0]
    flat = [p for grp in params if isinstance(grp, list) for p in grp]
    # ★ 2026-09-05: the endpoint patterns are ANCHORED PREFIXES now, not
    # contains-matches. Read as `%pattern%`, `/health` matched real facility
    # pages — /facilities/healthpartners-data-center and
    # /facilities/health-dialog-bedford-datacenter — and excluded genuine
    # ChatGPT crawls as our own polling. This test is about PARAMETERISATION
    # (a literal % in a psycopg2 SQL string is a 500), and that property is
    # unchanged: the wildcards still ride in params, they just no longer lead.
    assert "/api/v1/mcp/%" in flat, flat
    assert not any(p.startswith("%/") for p in flat), (
        "a leading-wildcard endpoint pattern is back: %r"
        % [p for p in flat if p.startswith("%/")])
    # the PLATFORM pattern is a name prefix and keeps its own shape
    assert "dchub%" in flat, flat


def test_include_internal_still_bypasses_every_filter(monkeypatch):
    ex = _Exec(rows=_rows("internal"))
    monkeypatch.setattr(t, "_execute", ex)
    t.recent_activity_feed(20, include_internal=True)
    sql = ex.sql_seen[0]
    assert "NOT LIKE ALL(" not in sql and "platform NOT IN (" not in sql


# ── time_ago must come off the ROW, never a constant ──────────────────
def test_time_ago_is_derived_from_the_rows_own_timestamp(monkeypatch):
    """★THE FABRICATION THIS PANEL IS FAMOUS FOR. The removed client-side
    generator stamped a literal "Just now" on entries derived from a LIFETIME
    counter, so HuggingFace — one real request, ever — rendered as live
    traffic. A relative time is only checkable if it is computed from that
    row's real created_at."""
    from datetime import datetime, timedelta, timezone
    ninety_s_ago = datetime.now(timezone.utc) - timedelta(seconds=90)
    ex = _Exec(rows=[{"platform": "claude", "endpoint": "/api/v1/facilities",
                      "created_at": ninety_s_ago}])
    monkeypatch.setattr(t, "_execute", ex)
    row = t.feed_rows_for_surface(20)[0]
    assert row["time_ago"] == "1m ago", row["time_ago"]
    assert row["time_ago"] != "Just now"
    assert row["timestamp"].startswith(ninety_s_ago.isoformat()[:19])

    two_h_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    ex2 = _Exec(rows=[{"platform": "meta", "endpoint": "/x",
                       "created_at": two_h_ago}])
    monkeypatch.setattr(t, "_execute", ex2)
    assert t.feed_rows_for_surface(20)[0]["time_ago"] == "2h ago"


def test_surface_rows_carry_the_minimum_contract(monkeypatch):
    from datetime import datetime, timezone
    ex = _Exec(rows=[{"platform": "claude", "endpoint": "/api/v1/pockets/x",
                      "created_at": datetime.now(timezone.utc)}])
    monkeypatch.setattr(t, "_execute", ex)
    row = t.feed_rows_for_surface(20)[0]
    for field in ("platform_key", "endpoint", "timestamp"):
        assert row.get(field), f"{field} missing — renderFeed needs it"
    assert row["platform_key"] == "claude"


def test_quiet_surface_feed_is_an_empty_list_not_an_invention(monkeypatch):
    """Empty is a valid answer. It must never be padded."""
    monkeypatch.setattr(t, "_execute", _Exec(rows=[]))
    assert t.feed_rows_for_surface(20) == []
    monkeypatch.setattr(t, "_execute", _Exec(raise_on_select=True))
    assert t.feed_rows_for_surface(20) == []


def test_our_own_dchub_platform_rows_are_excluded_by_name(monkeypatch):
    """★Neither the endpoint nor the UA filter can see these. Measured:
    'dchub-internal' (166 rows/30d) hits '/mcp (initialize)' — not
    '/api/v1/mcp/', so the endpoint list misses it — under 'curl/8.7.1' and a
    plain Windows Chrome UA, so the UA markers miss it too. Only the bucket
    name identifies it."""
    ex = _Exec(rows=[])
    monkeypatch.setattr(t, "_execute", ex)
    t.recent_activity_feed(20)
    assert "COALESCE(platform,'') NOT LIKE ALL(" in ex.sql_seen[0]
    flat = [p for grp in ex.params_seen[0] if isinstance(grp, list) for p in grp]
    assert "dchub%" in flat
