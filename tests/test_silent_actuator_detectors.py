"""The three silent-actuator detectors must be able to FIRE, and must fail closed.

A detector that returns [] is indistinguishable from a detector that cannot
work. On live production data at commit time, detectors 1 and 2 correctly
returned 0 (their lanes were healthy that hour) and only detector 3 fired -- so
"it returned 0" proves nothing on its own. These tests drive each one with a
stub connection into the state it exists to catch.

Live proof captured 2026-08-24 before committing:
  check_actuator_lane_silent  aimed at mcp_pair_codes (silent 19d) -> 1 finding
  check_approved_backlog_unpublished at 0h threshold               -> 1 finding
  check_failsoft_metric_null_streak on real data                   -> 1 finding
                (social_audience/linkedin: 29 rows written, 0 non-null, http_400)
  all three with DATABASE_URL unset                                -> 0 findings
"""
import importlib


class _Cur:
    """Minimal cursor stub: replays a queued list of fetch results."""
    def __init__(self, script):
        self._script = script
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self._last = self._script.pop(0) if self._script else None

    def fetchone(self):
        v = self._last
        return v[0] if isinstance(v, list) and v else v

    def fetchall(self):
        return self._last if isinstance(self._last, list) else []


class _Conn:
    def __init__(self, script):
        self._script = script

    def cursor(self):
        return _Cur(self._script)

    def close(self):
        pass


def _mod():
    import routes.brain_actuator_detectors as m
    return importlib.reload(m)


# ── detector 1: a lane that used to write and has gone quiet ─────────────

def test_actuator_lane_silent_fires_on_a_silent_lane(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_ACTUATOR_LANES",
                        [("some_lane", "ts", 14, "test lane", "because")])
    # (ever, last_write, quiet_days) -- 4,000 rows of history, silent 19 days
    monkeypatch.setattr(m, "_db", lambda: _Conn([(4000, "2026-08-05", 19.2)]))
    out = m.check_actuator_lane_silent()
    assert len(out) == 1, "a lane with history, silent past its cadence, must fire"
    assert out[0]["issue"] == "actuator_lane_silent"
    assert out[0]["count"] == 19


def test_actuator_lane_silent_ignores_a_lane_that_never_wrote(monkeypatch):
    """Zero rows EVER is an unbuilt feature, not a regression."""
    m = _mod()
    monkeypatch.setattr(m, "_ACTUATOR_LANES",
                        [("never_written", "ts", 1, "test lane", "because")])
    monkeypatch.setattr(m, "_db", lambda: _Conn([(0, None, None)]))
    assert m.check_actuator_lane_silent() == []


def test_actuator_lane_silent_quiet_inside_cadence_is_not_a_finding(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_ACTUATOR_LANES",
                        [("fine", "ts", 14, "test lane", "because")])
    monkeypatch.setattr(m, "_db", lambda: _Conn([(4000, "2026-08-23", 1.1)]))
    assert m.check_actuator_lane_silent() == []


# ── detector 2: approvals piling up while the drain reads zero ────────────

def test_approved_backlog_fires_when_approvals_pile_up(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_PUBLISH_QUEUES",
                        [("q", "status", "approved", "posted_at", "created_at",
                          3, 12, "test queue")])
    # (waiting, last_published, quiet_hours)
    monkeypatch.setattr(m, "_db", lambda: _Conn([(5, "2026-08-21 06:43", 71.5)]))
    out = m.check_approved_backlog_unpublished()
    assert len(out) == 1, "5 approved + 71h with no publish must fire"
    assert out[0]["count"] == 5


def test_approved_backlog_is_not_just_a_non_empty_queue(monkeypatch):
    """A queue with items AND a recent publish is a working queue."""
    m = _mod()
    monkeypatch.setattr(m, "_PUBLISH_QUEUES",
                        [("q", "status", "approved", "posted_at", "created_at",
                          3, 12, "test queue")])
    monkeypatch.setattr(m, "_db", lambda: _Conn([(9, "2026-08-24 06:18", 0.3)]))
    assert m.check_approved_backlog_unpublished() == [], (
        "fired on a queue that published 18 minutes ago -- this detector would "
        "cry wolf on every healthy backlog"
    )


# ── detector 3: the fail-soft metric that is always null ──────────────────

def test_failsoft_null_streak_fires_and_flags_never_worked(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_FAILSOFT_METRICS",
                        [("t", "d", "followers", "platform", "reason", 7, "test metric")])
    # first fetchall -> the GROUP BY rows; then the "ever non-null" fetchone
    monkeypatch.setattr(m, "_db", lambda: _Conn([
        [("linkedin", 29, 0, None, "http_400")],
        (0,),
    ]))
    out = m.check_failsoft_metric_null_streak()
    assert len(out) == 1
    assert out[0]["issue"] == "failsoft_metric_null_streak"
    assert out[0]["count"] == 29
    assert "never once been non-null" in out[0]["detail"], (
        "a metric that NEVER worked must be called out differently from a "
        "recent regression -- it is the one most likely to be waved off as "
        "'not wired up yet'"
    )
    assert "http_400" in out[0]["detail"], "the recorded reason must reach the finding"


def test_failsoft_null_streak_quiet_when_the_metric_sometimes_works(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_FAILSOFT_METRICS",
                        [("t", "d", "followers", "platform", "reason", 7, "test metric")])
    monkeypatch.setattr(m, "_db", lambda: _Conn([
        [("x", 29, 19, "2026-08-23", "no_creds")],
    ]))
    assert m.check_failsoft_metric_null_streak() == []


# ── the fail-closed contract, all three ──────────────────────────────────

def test_all_three_fail_closed_without_a_database(monkeypatch):
    """No DB must yield NO findings -- never a false 'healthy' either, but
    resolve-on-absence means emitting [] is the safe direction here."""
    m = _mod()
    monkeypatch.setattr(m, "_db", lambda: None)
    for fn in m.ACTUATOR_DETECTORS:
        assert fn() == [], f"{fn.__name__} did not fail closed"


def test_all_three_swallow_a_raising_connection(monkeypatch):
    m = _mod()

    class _Boom:
        def cursor(self):
            raise RuntimeError("connection reset")

        def close(self):
            pass

    monkeypatch.setattr(m, "_db", lambda: _Boom())
    for fn in m.ACTUATOR_DETECTORS:
        assert fn() == [], f"{fn.__name__} raised instead of failing closed"


def test_registered_in_the_radar_sweep():
    """A detector absent from the sweep never runs -- the documented trap."""
    import os
    radar = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "routes", "brain_consistency_radar.py")
    src = open(radar, encoding="utf-8").read()
    assert "ACTUATOR_DETECTORS" in src, (
        "brain_actuator_detectors is not wired into brain_consistency_radar."
        "scan_all, so these three detectors would never run."
    )
