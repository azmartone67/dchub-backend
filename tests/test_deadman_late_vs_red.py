"""D2 (QA sweep 2026-09-02) — the dead-man board separates LATE from RED.

MEASURED 2026-09-02 00:29Z on /api/v1/ops/deadman: overdue_count=11, and every
one of the eleven was a master shell that had run MINUTES earlier and beat
`status=lanes_failing` (agent-pay 0.1h into a 24h cadence, webmcp 3.4h/24h …).
Since #3365 (08-30) a shell with any red lane beats `lanes_failing` — which is
right — but the board counted a non-OK status as "overdue", so an on-time shell
sat on the overdue list beside the one feed that was genuinely late
(needs-decision-digest). Then three OTHER shells failed because of it:
growthfix lane 3 (`ingest_board=FAIL: 11 overdue …`), loop-flywheel lane 9
(`cron=FAIL`, identical text) and audit-closure lane A (`p0_incidents=FAIL:
loop-control shell beats on schedule — overdue 0.0h: status=lanes_failing`).
A self-referential cascade: shells red because other shells were red.

The split:
  overdue  = LATE  (never ran | last beat > 2x cadence)        -> any_overdue, overdue_count, overdue[]
  red      = ran, but the last beat carried a fault           -> any_red, red_count, red[] (names)
  unhealthy = either                                           -> unhealthy_count
Nothing that alarmed before stops alarming: the off-worker watcher folds
`unhealthy`, and every per-feed record carries all three booleans.

What must STILL hold (the #3365 guard, tests/test_shell_beat_reports_red.py):
a shell that beat `lanes_failing` is NOT green on the wire. It is `red`, and
counted in `unhealthy_count` — it is just not "late".
"""
from __future__ import annotations

import datetime as dt

import pytest

from routes.ingest_runs import _OK_STATUS

_H = dt.timedelta(hours=1)


# ── the evaluator: GET /api/v1/ops/deadman over a fake ledger ──────────

def _client(monkeypatch, rows):
    flask = pytest.importorskip("flask")
    import routes.ingest_runs as ir

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://ledger.example/db")
    monkeypatch.setattr(ir.psycopg2, "connect", lambda *a, **k: _Conn())
    app = flask.Flask(__name__)
    app.register_blueprint(ir.ingest_runs_bp)
    return app.test_client()


def _board(monkeypatch, rows):
    return _client(monkeypatch, rows).get("/api/v1/ops/deadman").get_json()


def test_a_shell_that_ran_on_time_and_beat_lanes_failing_is_red_not_overdue(monkeypatch):
    """Kills: counting `status not in _OK_STATUS` as overdue. The exact live
    shape from 00:29Z — a 24h-cadence shell that beat lanes_failing 0.1h ago."""
    now = dt.datetime.now(dt.timezone.utc)
    rows = [("agent-pay-shell-daily", now - 0.1 * _H, "lanes_failing", None, None, 24.0, 0,
             "lanes: demand=FAIL reachability=FAIL")]
    body = _board(monkeypatch, rows)
    assert body["ok"] is True
    rec = body["feeds"][0]
    assert "lanes_failing" not in _OK_STATUS, "fixture: lanes_failing must be a non-OK status"
    # not late …
    assert rec["overdue"] is False, rec
    assert body["any_overdue"] is False and body["overdue_count"] == 0, body
    assert body["overdue"] == []
    # … but NOT green either (the #3365 guard, on the wire)
    assert rec["red"] is True and rec["unhealthy"] is True, rec
    assert body["any_red"] is True and body["red_count"] == 1
    assert body["red"] == ["agent-pay-shell-daily"]
    assert body["unhealthy_count"] == 1
    assert rec["kinds"] == ["run_failed"] and rec["reasons"] == ["status=lanes_failing"]


def test_a_feed_past_2x_cadence_is_overdue(monkeypatch):
    """The cadence rule is unchanged: 36h into an 18h cadence with a green
    last beat is LATE — nothing broke, it just has not run. `red` stays False."""
    now = dt.datetime.now(dt.timezone.utc)
    rows = [("needs-decision-digest", now - 37 * _H, "success", 1, None, 18.0, 0, None)]
    body = _board(monkeypatch, rows)
    rec = body["feeds"][0]
    assert rec["overdue"] is True and rec["red"] is False and rec["unhealthy"] is True
    assert body["any_overdue"] is True and body["overdue_count"] == 1
    assert body["red_count"] == 0 and body["red"] == []
    assert body["unhealthy_count"] == 1
    assert rec["kinds"] == ["stale_age"]


def test_never_ran_is_overdue(monkeypatch):
    rows = [("ghost-feed", None, None, None, None, 6.0, 0, None)]
    body = _board(monkeypatch, rows)
    rec = body["feeds"][0]
    assert rec["overdue"] is True and rec["kinds"] == ["never_ran"]
    assert body["overdue_count"] == 1 and body["red_count"] == 0


def test_zero_rows_and_future_content_date_are_red_not_late(monkeypatch):
    """The other two ran-but-faulted rules land on the same side as a bad
    status: the loop FIRED, so it is not late; what it produced is wrong."""
    now = dt.datetime.now(dt.timezone.utc)
    rows = [("osm-crawl", now - 1 * _H, "success", 0, None, 24.0, 5, None),
            ("news-crawl", now - 1 * _H, "success", 40, now + 30 * _H, 6.0, 0, None)]
    body = _board(monkeypatch, rows)
    by = {f["feed"]: f for f in body["feeds"]}
    assert by["osm-crawl"]["kinds"] == ["zero_rows"]
    assert by["news-crawl"]["kinds"] == ["future_content_date"]
    for f in by.values():
        assert f["overdue"] is False and f["red"] is True and f["unhealthy"] is True
    assert body["overdue_count"] == 0 and body["red_count"] == 2
    assert sorted(body["red"]) == ["news-crawl", "osm-crawl"]
    assert body["unhealthy_count"] == 2


def test_late_and_red_is_counted_once_in_unhealthy(monkeypatch):
    """A feed that is BOTH stale and failed sits in both lists (each list is
    honest on its own) and once in the union."""
    now = dt.datetime.now(dt.timezone.utc)
    rows = [("worker:deals", now - 50 * _H, "timeout", None, None, 18.0, 0, None),
            ("iso-lmp-pjm", now - 1 * _H, "success", 5, None, 3.0, 0, None)]
    body = _board(monkeypatch, rows)
    by = {f["feed"]: f for f in body["feeds"]}
    bad = by["worker:deals"]
    assert bad["overdue"] is True and bad["red"] is True and bad["unhealthy"] is True
    assert bad["kinds"] == ["stale_age", "run_failed"]
    assert body["overdue_count"] == 1 and body["red_count"] == 1
    assert body["unhealthy_count"] == 1, "one feed, two faults, ONE unhealthy loop"
    assert body["tracked"] == 2
    assert by["iso-lmp-pjm"]["unhealthy"] is False
    # overdue sorts first, then red, then the healthy tail
    assert [f["feed"] for f in body["feeds"]] == ["worker:deals", "iso-lmp-pjm"]


def test_a_healthy_board_publishes_all_zero(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    rows = [("iso-lmp-pjm", now - 1 * _H, "success", 5, None, 3.0, 0, None),
            ("eia-pricing", now - 2 * _H, "no_new_data", 0, None, 24.0, 7, None)]
    body = _board(monkeypatch, rows)
    assert body["any_overdue"] is False and body["any_red"] is False
    assert body["overdue_count"] == body["red_count"] == body["unhealthy_count"] == 0
    assert body["overdue"] == [] and body["red"] == []
    assert "basis" in body


# ── consumer 1: growthfix lane 3 (in-DB mirror of the board) ────────────

def _gf_rows(now):
    return [
        # ran 6 minutes ago, beat lanes_failing -> red, NOT late
        ("agentic-loop-shell-daily", now - 0.1 * _H, "lanes_failing", 24.0, 0, None),
        # green and on time
        ("iso-lmp-pjm", now - 1 * _H, "success", 3.0, 0, None),
    ]


def test_growthfix_ingest_board_does_not_fail_on_red_but_on_time_shells(monkeypatch):
    import routes.growthfix_master_shell as gf
    now = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(gf, "_rows", lambda c, sql: _gf_rows(now))
    (chk,) = gf._lane_ingest_board(object())
    assert chk["id"] == "board"
    assert chk["pass"] is True, chk["detail"]
    # …and the red shell is still NAMED, as a note
    assert "0 overdue" in chk["detail"]
    assert "agentic-loop-shell-daily(status=lanes_failing)" in chk["detail"]
    assert "ON TIME" in chk["detail"]


def test_growthfix_ingest_board_still_fails_on_a_late_feed(monkeypatch):
    """The other direction — narrowing the lane must not blind it."""
    import routes.growthfix_master_shell as gf
    now = dt.datetime.now(dt.timezone.utc)
    rows = _gf_rows(now) + [("needs-decision-digest", now - 37 * _H, "success", 18.0, 0, None),
                            ("ghost", None, None, 6.0, 0, None)]
    monkeypatch.setattr(gf, "_rows", lambda c, sql: rows)
    (chk,) = gf._lane_ingest_board(object())
    assert chk["pass"] is False
    assert "2 overdue" in chk["detail"], chk["detail"]
    assert "needs-decision-digest(stale)" in chk["detail"]
    assert "ghost(never ran)" in chk["detail"]
    assert chk["critical"] is True


# ── consumer 2: loop-flywheel lane 9 ─────────────────────────────────────

class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)


def _lf_deadman(rows):
    import routes.loop_flywheel_master_shell as lf
    return {c["id"]: c for c in lf._lane_cron(_Conn(rows))}["deadman"]


def test_loop_flywheel_cron_lane_does_not_fail_on_red_but_on_time_shells():
    now = dt.datetime.now(dt.timezone.utc)
    chk = _lf_deadman(_gf_rows(now))
    assert chk["pass"] is True, chk["detail"]
    assert "0 overdue" in chk["detail"]
    assert "agentic-loop-shell-daily(status=lanes_failing)" in chk["detail"]


def test_loop_flywheel_cron_lane_still_fails_on_a_late_feed():
    now = dt.datetime.now(dt.timezone.utc)
    rows = _gf_rows(now) + [("needs-decision-digest", now - 37 * _H, "success", 18.0, 0, None)]
    chk = _lf_deadman(rows)
    assert chk["pass"] is False
    assert "1 overdue: needs-decision-digest(stale)" in chk["detail"], chk["detail"]


# ── consumer 3: audit-closure lane A "loop-control shell beats on schedule" ──

@pytest.fixture
def ac(monkeypatch):
    pytest.importorskip("flask")
    monkeypatch.setenv("AUDIT_CLOSURE_SHELL_PROBE", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    from routes import audit_closure_master_shell as m
    monkeypatch.setattr(m, "_http",
                        lambda url, timeout=8, headers=None, fresh=False, _memo=None:
                        (None, {}, "", "stubbed dead in CI"))
    return m


def _feed_stub(rows):
    return lambda name: (rows[name], None) if name in rows else (None, "n/a")


def test_audit_closure_a_loopctl_passes_when_on_time_but_red(ac, monkeypatch):
    """The live failure text: 'overdue 0.0h: status=lanes_failing'. On schedule
    is a cadence question; a red lane on that shell is its own board to read."""
    monkeypatch.setattr(ac, "_deadman_feed", _feed_stub({
        "loop-control-shell-daily": {"feed": "loop-control-shell-daily", "overdue": False,
                                     "red": True, "unhealthy": True, "age_hours": 0.1,
                                     "kinds": ["run_failed"],
                                     "reasons": ["status=lanes_failing"]}}))
    checks = {c["id"]: c for c in ac._lane_p0_incidents()}
    chk = checks["a_loopctl"]
    assert chk["pass"] is True, chk["detail"]
    assert "RED" in chk["detail"] and "lanes_failing" in chk["detail"], chk["detail"]


def test_audit_closure_a_loopctl_fails_when_late(ac, monkeypatch):
    monkeypatch.setattr(ac, "_deadman_feed", _feed_stub({
        "loop-control-shell-daily": {"feed": "loop-control-shell-daily", "overdue": True,
                                     "red": False, "unhealthy": True, "age_hours": 49.2,
                                     "kinds": ["stale_age"],
                                     "reasons": ["last success 49h ago (>2x cadence 24h)"]}}))
    checks = {c["id"]: c for c in ac._lane_p0_incidents()}
    assert checks["a_loopctl"]["pass"] is False
    assert "overdue 49.2h" in checks["a_loopctl"]["detail"]


def test_audit_closure_health_checks_read_the_union(ac, monkeypatch):
    """h_geninv asks 'green?', not 'on schedule?' — a red-but-on-time feed
    must still fail it (the split must not launder a failed ingest)."""
    monkeypatch.setattr(ac, "_deadman_feed", _feed_stub({
        "generator-inventory-ingest": {"feed": "generator-inventory-ingest", "overdue": False,
                                       "red": True, "unhealthy": True, "age_hours": 2.0,
                                       "status": "error", "reasons": ["status=error"]}}))
    checks = {c["id"]: c for c in ac._lane_inventory()}
    assert checks["h_geninv"]["pass"] is False, checks["h_geninv"]["detail"]
    assert checks["h_geninv"]["detail"].startswith("red 2.0h")
