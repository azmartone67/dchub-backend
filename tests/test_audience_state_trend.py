"""Guards for GET /api/v1/admin/audience/state — the trend must span DAYS.

The defect, for the record: the query was `ORDER BY id DESC LIMIT 14`, and
master-tick is fired by cron_heartbeat, brain_lane_driver, gap_master_shell
and distribution_master_shell on top of the daily scheduled task. A single
day wrote 14+ snapshots, so the "14-row trend" covered 0.69 days (all 14
rows stamped 2026-07-31) and could never show a ramp. The daily growth-watch
gated its weekly GEO probe on that window and so skipped forever.

These tests hold the day-granularity and the `latest` semantics. The dedup
itself lives in SQL (DISTINCT ON), so the query text is asserted structurally
— the span/latest behaviour is exercised against a stub cursor.
"""
import datetime as dt

import pytest

ams = pytest.importorskip("routes.audience_master_shell")


def _row(day, hour, agents, rid):
    return {
        "id": rid,
        "computed_at": dt.datetime(2026, 7, day, hour, tzinfo=dt.timezone.utc),
        "real_agents_7d": agents,
    }


class _StubCursor:
    """Replays a scripted result per execute() in call order."""

    def __init__(self, script):
        self.script = list(script)
        self.sql = []
        self._last = None

    def execute(self, sql, *a):
        self.sql.append(" ".join(sql.split()))
        self._last = self.script.pop(0)

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StubConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, **kw):
        return self._cur

    def close(self):
        pass


def _call_state(monkeypatch, cur):
    monkeypatch.setattr(ams, "_admin_ok", lambda: True)
    monkeypatch.setattr(ams, "_conn", lambda: _StubConn(cur))
    app = __import__("flask").Flask(__name__)
    app.register_blueprint(ams.audience_master_shell_bp)
    with app.test_client() as c:
        r = c.get("/api/v1/admin/audience/state")
    return r


def test_trend_query_collapses_to_one_row_per_utc_day(monkeypatch):
    """The regression guard: a bare `ORDER BY id DESC LIMIT 14` must not
    come back. The window has to be day-granular or the flooding returns."""
    days = [_row(20 + i, 9, 100 + i, i) for i in range(3)]
    cur = _StubCursor([days, [days[-1]], [{"n": 412}]])
    _call_state(monkeypatch, cur)

    trend_sql = cur.sql[0]
    assert "DISTINCT ON" in trend_sql, "trend must dedup per day"
    assert "AT TIME ZONE 'UTC')::date" in trend_sql, \
        "day bucket must be UTC-explicit — a bare ::date shifts with session TimeZone"
    assert "ORDER BY (computed_at AT TIME ZONE 'UTC')::date DESC" in trend_sql, \
        "DISTINCT ON requires the ORDER BY to lead with the same expression"


def test_days_span_measures_oldest_to_newest(monkeypatch):
    """days_span is what the growth-watch gates its weekly probe on, so it
    must be real elapsed days across the window — not a row count."""
    # newest-first, as `ORDER BY ...date DESC` actually returns them
    days = [_row(27, 21, 145, 3), _row(25, 9, 120, 2), _row(20, 9, 100, 1)]
    cur = _StubCursor([days, [days[0]], [{"n": 412}]])
    r = _call_state(monkeypatch, cur)
    body = r.get_json()

    assert body["days_span"] == pytest.approx(7.5, abs=0.01), \
        "20 Jul 09:00 → 27 Jul 21:00 is 7.5 days"
    assert body["count"] == 3, "count is distinct DAYS present"
    assert body["snapshots_total"] == 412, "raw row total stays visible"


def test_trend_is_chronological_and_latest_is_the_true_newest(monkeypatch):
    """`latest` comes from its own id-DESC query, so it stays the real most
    recent snapshot rather than a day rollup."""
    days = [_row(27, 9, 145, 2), _row(20, 9, 100, 1)]   # newest-first from SQL
    true_latest = _row(27, 23, 147, 99)
    cur = _StubCursor([days, [true_latest], [{"n": 412}]])
    r = _call_state(monkeypatch, cur)
    body = r.get_json()

    assert body["trend"][0]["real_agents_7d"] == 100, "oldest first"
    assert body["trend"][-1]["real_agents_7d"] == 145, "newest last"
    assert body["latest"]["id"] == 99
    assert body["latest"]["real_agents_7d"] == 147


def test_single_day_window_reports_zero_span(monkeypatch):
    """The exact 2026-07-31 shape: everything same-day. span must read ~0 so
    the weekly gate can tell 'no history yet' from 'a week of history'."""
    days = [_row(31, 5, 145, 1)]
    cur = _StubCursor([days, [days[0]], [{"n": 14}]])
    r = _call_state(monkeypatch, cur)
    body = r.get_json()

    assert body["count"] == 1
    assert body["days_span"] is None, "a single row has no span to measure"
