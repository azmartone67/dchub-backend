"""r-streak (2026-07-18): return-streak daily-cap boost — unit tests.

Covers the streak→cap ladder (the mission-spec mapping at base 10:
1 day=10, 2-3=15, 4-6=20, 7+=30), the fail-open contract (any streak
error → base cap, never block), the ~1h cache, and the live wiring in
mcp_gatekeeper._RateLimiter (boosted day cap + fail-open).

Pure/in-process — no DB, no Flask app. Run: python3 -m pytest tests/test_return_streak.py -v
"""
import time

import pytest

import return_streak
import mcp_gatekeeper


@pytest.fixture(autouse=True)
def _clean_cache():
    with return_streak._cache_lock:
        return_streak._cache.clear()
    yield
    with return_streak._cache_lock:
        return_streak._cache.clear()


# ── ladder mapping ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("days,expected", [
    (0, 10), (1, 10),           # 0-1 active day → base
    (2, 15), (3, 15),           # 2-3 → 1.5x
    (4, 20), (5, 20), (6, 20),  # 4-6 → 2x
    (7, 30), (10, 30), (14, 30),  # 7+ → 3x
])
def test_mission_spec_mapping_at_base_10(days, expected):
    assert return_streak.boosted_cap(10, days) == expected


@pytest.mark.parametrize("base,days,expected", [
    # trial unbound base 15
    (15, 0, 15), (15, 2, 23), (15, 4, 30), (15, 7, 45),
    # flask /mcp anon base 25
    (25, 2, 38), (25, 4, 50), (25, 7, 75),
    # trial bound base 50
    (50, 2, 75), (50, 4, 100), (50, 7, 150),
    # identified base 100
    (100, 2, 150), (100, 4, 200), (100, 7, 300),
])
def test_multiplier_ladder_other_bases(base, days, expected):
    assert return_streak.boosted_cap(base, days) == expected


def test_boost_never_below_base():
    for days in (-3, 0, 1, 2, 7, 500):
        for base in (1, 3, 10, 25, 50):
            assert return_streak.boosted_cap(base, days) >= base


def test_boost_garbage_inputs_fail_open():
    assert return_streak.boosted_cap(10, None) == 10
    assert return_streak.boosted_cap(10, "not-a-number") == 10
    assert return_streak.boosted_cap(None, 7) is None       # non-numeric base untouched
    assert return_streak.boosted_cap("x", 7) == "x"
    assert return_streak.boosted_cap(0, 7) == 0             # non-positive base untouched
    assert return_streak.boosted_cap(-5, 7) == -5


def test_streak_multiplier_boundaries():
    assert return_streak.streak_multiplier(1) == 1.0
    assert return_streak.streak_multiplier(2) == 1.5
    assert return_streak.streak_multiplier(4) == 2.0
    assert return_streak.streak_multiplier(7) == 3.0
    assert return_streak.streak_multiplier(None) == 1.0
    assert return_streak.streak_multiplier("zzz") == 1.0


def test_next_unlock_rungs():
    assert return_streak.next_unlock(0, 10) == {"active_days": 2, "multiplier": 1.5, "cap": 15}
    assert return_streak.next_unlock(2, 10) == {"active_days": 4, "multiplier": 2.0, "cap": 20}
    assert return_streak.next_unlock(5, 10) == {"active_days": 7, "multiplier": 3.0, "cap": 30}
    assert return_streak.next_unlock(7, 10) is None          # top rung
    assert return_streak.next_unlock(3) == {"active_days": 4, "multiplier": 2.0}  # no base → no cap


# ── fail-open + cache ───────────────────────────────────────────────────────

def test_get_streak_days_fail_open_on_db_error(monkeypatch):
    def _boom(_key):
        raise RuntimeError("db down")
    monkeypatch.setattr(return_streak, "_query_streak_days", _boom)
    assert return_streak.get_streak_days("dch_trial_x") == 0
    # errors are NOT cached — a later healthy query must be able to recover
    monkeypatch.setattr(return_streak, "_query_streak_days", lambda k: 4)
    assert return_streak.get_streak_days("dch_trial_x") == 4


def test_get_streak_days_bad_key_shapes():
    assert return_streak.get_streak_days(None) == 0
    assert return_streak.get_streak_days("") == 0
    assert return_streak.get_streak_days(12345) == 0


def test_get_streak_days_cached_for_ttl(monkeypatch):
    calls = {"n": 0}

    def _q(_key):
        calls["n"] += 1
        return 3
    monkeypatch.setattr(return_streak, "_query_streak_days", _q)
    assert return_streak.get_streak_days("k1") == 3
    assert return_streak.get_streak_days("k1") == 3
    assert calls["n"] == 1  # second hit served from cache

    # expire the entry → next call re-queries
    with return_streak._cache_lock:
        return_streak._cache["k1"] = (3, time.time() - return_streak.STREAK_CACHE_TTL_S - 1)
    assert return_streak.get_streak_days("k1") == 3
    assert calls["n"] == 2


# ── surfacing blocks ────────────────────────────────────────────────────────

def test_streak_snapshot_with_base(monkeypatch):
    monkeypatch.setattr(return_streak, "_query_streak_days", lambda k: 2)
    snap = return_streak.streak_snapshot("k2", 10)
    assert snap["streak_days"] == 2
    assert snap["base_cap"] == 10
    assert snap["cap_today"] == 15
    assert snap["next_unlock"] == {"active_days": 4, "multiplier": 2.0, "cap": 20}
    assert "15/day" in snap["message"]
    assert "20/day" in snap["message"]          # the return-tomorrow carrot


def test_streak_snapshot_without_base_and_max(monkeypatch):
    monkeypatch.setattr(return_streak, "_query_streak_days", lambda k: 9)
    snap = return_streak.streak_snapshot("k3")
    assert snap["streak_days"] == 9
    assert snap["multiplier"] == 3.0
    assert snap.get("max_boost_active") is True
    assert "cap_today" not in snap              # no base → no absolute numbers


def test_streak_snapshot_fail_open(monkeypatch):
    def _boom(_key):
        raise RuntimeError("db down")
    monkeypatch.setattr(return_streak, "_query_streak_days", _boom)
    snap = return_streak.streak_snapshot("k4", 50)
    assert snap["streak_days"] == 0
    assert snap["cap_today"] == 50              # base cap, never blocked


# ── live wiring: mcp_gatekeeper._RateLimiter ────────────────────────────────

def _seed_day_count(rl, key, count):
    rl._day[key] = {rl._today(): count}


def test_gatekeeper_day_cap_boosted_by_streak(monkeypatch):
    monkeypatch.setattr(return_streak, "get_streak_days", lambda k: 7)
    rl = mcp_gatekeeper._RateLimiter()
    key = "dchub_free_streaker"
    _seed_day_count(rl, key, 25)                # at FREE base cap (25/day)
    rl._last[key] = 0                           # no cooldown in play
    # 7-day streak → 3x → 75/day → call 26 allowed
    assert rl.check(key, mcp_gatekeeper.Tier.FREE) is None
    _seed_day_count(rl, key, 75)                # at the boosted cap
    rl._last[key] = 0                           # clear the re-armed cooldown
    rl._minute[key] = []
    err = rl.check(key, mcp_gatekeeper.Tier.FREE)
    assert err is not None and "75" in err      # wall reports the boosted cap


def test_gatekeeper_fail_open_to_base_cap(monkeypatch):
    def _boom(_key):
        raise RuntimeError("db down")
    monkeypatch.setattr(return_streak, "get_streak_days", _boom)
    rl = mcp_gatekeeper._RateLimiter()
    key = "dchub_free_nodb"
    _seed_day_count(rl, key, 24)
    rl._last[key] = 0
    assert rl.check(key, mcp_gatekeeper.Tier.FREE) is None   # 25th call passes
    _seed_day_count(rl, key, 25)
    rl._last[key] = 0                           # clear the re-armed cooldown
    rl._minute[key] = []
    err = rl.check(key, mcp_gatekeeper.Tier.FREE)
    assert err is not None and "25" in err      # base cap enforced, not blocked early


def test_gatekeeper_paid_tiers_untouched(monkeypatch):
    called = {"n": 0}

    def _spy(_key):
        called["n"] += 1
        return 7
    monkeypatch.setattr(return_streak, "get_streak_days", _spy)
    rl = mcp_gatekeeper._RateLimiter()
    key = "dchub_pro_whale"
    rl._last[key] = 0
    assert rl.check(key, mcp_gatekeeper.Tier.PRO) is None
    assert called["n"] == 0                     # streak lookup never runs for paid
