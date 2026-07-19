"""Lane 6 (verification velocity) of routes/coverage_master_shell.py (2026-07-18).

All pure — no DB, no network, never imports main. Contract under test:
  1. _floor_from_weekly — 60% of MEDIAN, min 1, outlier-robust (the 331-row
     bulk-import week must not set an impossible bar), None under 3 weeks
  2. _lane_verification pass logic — delta ≥ floor passes, a stall fails,
     missing history is UNKNOWN (None), never a spurious fail
  3. ratio math + the actuator-heartbeat check
  4. lane is registered and _verification_stats degrades to all-None w/o DB
"""
import datetime as _dt

import routes.coverage_master_shell as cms


# ── helpers ───────────────────────────────────────────────────────────

def _by_id(checks):
    return {c["id"]: c for c in checks}


def _facts(**over):
    """A healthy baseline mirroring live 2026-07-18 numbers."""
    f = {
        "dc_verified": 4964, "dc_tracked": 22000,
        "verified_7d_delta": 61, "verified_floor_7d": 20,
        "verified_median_weekly": 33,
        "verified_snapshot_latest": _dt.datetime.now(_dt.timezone.utc).date().isoformat(),
        "verified_discoveries_7d": 44,
        "verified_last_discovery": "2026-07-18T04:00:41+00:00",
        "verified_weekly_adds": [14, 18, 20, 23, 43, 48, 72, 331],
    }
    f.update(over)
    return f


# ── 1 · floor math ────────────────────────────────────────────────────

def test_floor_is_60pct_of_median_and_outlier_robust():
    # live series: median (23+43)/2 = 33 → floor round(19.8) = 20.
    # The 331 bulk week must NOT drag the floor up (mean would say ~34).
    assert cms._floor_from_weekly([14, 18, 20, 23, 43, 48, 72, 331]) == 20


def test_floor_min_is_one():
    assert cms._floor_from_weekly([0, 0, 1, 1, 0]) == 1


def test_floor_none_under_three_weeks():
    assert cms._floor_from_weekly([50, 60]) is None
    assert cms._floor_from_weekly([]) is None
    assert cms._floor_from_weekly(None) is None


def test_floor_ignores_garbage_values():
    # negatives / non-numbers are dropped, not counted toward the median
    assert cms._floor_from_weekly([-5, None, 10, 10, 10]) == 6


def test_median():
    assert cms._median([3]) == 3
    assert cms._median([1, 3]) == 2.0
    assert cms._median([1, 2, 100]) == 2
    assert cms._median([]) is None


# ── 2 · velocity-floor check pass logic ───────────────────────────────

def test_velocity_above_floor_passes():
    ch = _by_id(cms._lane_verification(_facts()))["verified_velocity_floor"]
    assert ch["pass"] is True
    assert "+61" in ch["detail"] and "20" in ch["detail"]


def test_velocity_stall_fails():
    ch = _by_id(cms._lane_verification(
        _facts(verified_7d_delta=3, verified_floor_7d=20)))["verified_velocity_floor"]
    assert ch["pass"] is False


def test_velocity_at_floor_passes():
    ch = _by_id(cms._lane_verification(
        _facts(verified_7d_delta=20, verified_floor_7d=20)))["verified_velocity_floor"]
    assert ch["pass"] is True


def test_missing_history_is_unknown_not_fail():
    # pre-2026-07-11 snapshots aren't comparable (issue #1539); short history
    # must read UNKNOWN, never a spurious red
    for over in ({"verified_7d_delta": None}, {"verified_floor_7d": None},
                 {"verified_7d_delta": None, "verified_floor_7d": None}):
        ch = _by_id(cms._lane_verification(_facts(**over)))["verified_velocity_floor"]
        assert ch["pass"] is None
        assert "1539" in ch["detail"]  # points at why


# ── 3 · totals / ratio / snapshot / actuator checks ───────────────────

def test_totals_and_ratio():
    ch = _by_id(cms._lane_verification(_facts()))["verified_totals"]
    assert ch["pass"] is True
    assert "22.6%" in ch["detail"]          # 4964/22000
    assert cms._verified_ratio(_facts()) == 0.2256


def test_totals_missing_feed_fails():
    ch = _by_id(cms._lane_verification(
        _facts(dc_verified=None)))["verified_totals"]
    assert not ch["pass"]
    assert cms._verified_ratio({"dc_verified": None, "dc_tracked": 22000}) is None
    assert cms._verified_ratio({"dc_verified": 5, "dc_tracked": 0}) is None


def test_snapshot_freshness():
    today = _dt.datetime.now(_dt.timezone.utc).date()
    fresh = _by_id(cms._lane_verification(_facts()))["verified_snapshot_fresh"]
    assert fresh["pass"] is True
    stale = _by_id(cms._lane_verification(_facts(
        verified_snapshot_latest=(today - _dt.timedelta(days=5)).isoformat()
    )))["verified_snapshot_fresh"]
    assert stale["pass"] is False
    unknown = _by_id(cms._lane_verification(_facts(
        verified_snapshot_latest=None)))["verified_snapshot_fresh"]
    assert unknown["pass"] is None


def test_actuator_heartbeat():
    ok = _by_id(cms._lane_verification(_facts()))["verified_actuator_alive"]
    assert ok["pass"] is True
    assert "dchub-jobs" in ok["name"]        # the actuator is NAMED
    dead = _by_id(cms._lane_verification(
        _facts(verified_discoveries_7d=0)))["verified_actuator_alive"]
    assert dead["pass"] is False
    unknown = _by_id(cms._lane_verification(
        _facts(verified_discoveries_7d=None)))["verified_actuator_alive"]
    assert unknown["pass"] is None


# ── 4 · wiring + no-DB degradation ────────────────────────────────────

def test_lane_registered():
    assert any(k == "verification" for k, _label, _fn in cms._LANES)
    fn = next(fn for k, _l, fn in cms._LANES if k == "verification")
    assert fn is cms._lane_verification


def test_stats_degrade_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = cms._verification_stats()
    assert s["verified_7d_delta"] is None
    assert s["verified_floor_7d"] is None
    assert s["verified_discoveries_7d"] is None
    # and the lane built from those Nones is all-unknown, not a crash / fail
    checks = cms._lane_verification({**s, "dc_verified": None, "dc_tracked": None})
    assert all(c["pass"] in (None, False) for c in checks)
