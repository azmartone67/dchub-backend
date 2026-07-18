"""Tests for the 2026-07-18 chronic-episode wave.

1. brain_action_queue._score — the chronic-pressure ranking blend:
   long-open chronic episodes outrank fresh noise; severity still
   dominates; missing episode fields degrade safely.
2. episode_analytics endpoint — 403 unauth, 200 with X-Admin-Key
   (DB monkeypatched: no network, no Postgres).
"""
import datetime
import importlib

import pytest

pytest.importorskip("flask")

aq = importlib.import_module("routes.brain_action_queue")
ea = importlib.import_module("routes.episode_analytics")


# ── 1. scoring blend ─────────────────────────────────────────────────

def test_chronic_old_beats_fresh_low():
    """The mission case: a MED finding open 828h with heavy re-observation
    must outrank a MED finding that just appeared (fresh noise)."""
    chronic = aq._score(2, 1, 30, hours_open=828.0, re_observations=700)
    fresh = aq._score(2, 1, 0, hours_open=0.5, re_observations=1)
    assert chronic > fresh
    # and it beats fresh noise by a full chronic margin, not a tiebreak
    assert chronic - fresh >= 400


def test_severity_still_dominates():
    """Max chronic pressure on a lower band never crosses the next
    severity band (non-severity terms cap at 928 < 1000)."""
    worst_med = aq._score(2, 10**6, 10**6,
                          hours_open=10**6, re_observations=10**6)
    calm_high = aq._score(3, 1, 0, hours_open=0, re_observations=0)
    assert worst_med < calm_high
    assert worst_med == 2000 + 400 + 30 + 300 + 198


def test_missing_episode_fields_safe():
    """Legacy rows (no episode columns) score without the chronic terms
    and never raise — including junk inputs."""
    legacy = aq._score(2, 5, 10)  # defaults: hours_open=0, reobs=1
    assert legacy == 2000 + 50 + 10 + 0 + 2
    # junk episode inputs degrade to zero-contribution, no exception
    assert aq._score(2, 5, 10, hours_open=None,
                     re_observations="not-a-number") == 2000 + 50 + 10
    # negative reobs clamps to 0
    assert aq._score(1, 1, 0, hours_open=-5, re_observations=-3) == 1010


def test_hours_open_parser():
    now = datetime.datetime(2026, 7, 18, 12, 0, 0)
    st = datetime.datetime(2026, 7, 17, 12, 0, 0)
    assert aq._hours_open(st, now) == pytest.approx(24.0)
    assert aq._hours_open("2026-07-17T12:00:00", now) == pytest.approx(24.0)
    assert aq._hours_open(None, now) == 0.0
    assert aq._hours_open("garbage", now) == 0.0
    # future timestamp clamps to 0, not negative
    assert aq._hours_open(datetime.datetime(2027, 1, 1), now) == 0.0


# ── 2. endpoint auth + shape ─────────────────────────────────────────

def _mk_client(monkeypatch, fake_rows=None):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(ea.episode_analytics_bp)

    from contextlib import contextmanager

    @contextmanager
    def _fake_db():
        yield object()  # non-None sentinel conn

    monkeypatch.setattr(ea, "_read_db", _fake_db)
    monkeypatch.setattr(ea, "_rows", fake_rows or (lambda c, s, a=None: None))
    return app.test_client()


def _dispatch_rows(conn, sql, args=None):
    if "FILTER (WHERE status = 'open')" in sql:
        return [(72, 12, 392)]
    if "SELECT AVG" in sql:
        return [(27.3,)]
    if "GROUP BY 1" in sql:
        return [("consistency_radar", 300, 8.4), ("fast_qa", 60, 2.1)]
    if "status = 'open'" in sql and "LIMIT 15" in sql:
        # 8 cols, mirroring _SQL_CHRONIC_LEADERBOARD: issue, url, detector,
        # hours_open, re_observations, episode_count, reobs_per_day,
        # chronic_score (= hours_open × reobs_per_day; 700 reobs over the
        # 33.6h the counter ran → 500/day)
        return [("brand_surface_dormant", "https://dchub.cloud/translate",
                 "brand_surface", 828.4, 700, 1, 500.0, 828.4 * 500.0)]
    if "LIMIT 5" in sql:
        return [("radar_flap", "https://dchub.cloud/x", "consistency_radar",
                 9, "open", 3)]
    return None


def test_endpoint_403_without_key(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    client = _mk_client(monkeypatch)
    r = client.get("/api/v1/admin/brain/episodes/analytics")
    assert r.status_code == 403
    assert r.get_json()["ok"] is False


def test_endpoint_403_with_wrong_key(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    client = _mk_client(monkeypatch)
    r = client.get("/api/v1/admin/brain/episodes/analytics",
                   headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 403


def test_endpoint_gate_closed_when_env_unset(monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    client = _mk_client(monkeypatch)
    r = client.get("/api/v1/admin/brain/episodes/analytics",
                   headers={"X-Admin-Key": "anything"})
    assert r.status_code == 403


def test_endpoint_200_with_key(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    client = _mk_client(monkeypatch, fake_rows=_dispatch_rows)
    r = client.get("/api/v1/admin/brain/episodes/analytics",
                   headers={"X-Admin-Key": "sekrit"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["open_now"] == 72
    assert d["resolved_24h"] == 12
    assert d["resolved_7d"] == 392
    assert d["avg_mttr_h"] == 27.3
    assert [m["detector"] for m in d["mttr_by_detector"]] == \
        ["consistency_radar", "fast_qa"]
    top = d["chronic_leaderboard"][0]
    assert top["issue"] == "brand_surface_dormant"
    assert top["hours_open"] == 828.4
    assert top["re_observations"] == 700
    assert top["reobs_per_day"] == 500.0
    assert top["chronic_score"] == pytest.approx(828.4 * 500.0, abs=0.5)
    assert d["flappiest"][0]["episode_count"] == 9


def test_endpoint_200_fail_soft_on_short_rows(monkeypatch):
    """A row narrower than the SQL promises (column drift) skips its
    entry — it must never 500 the endpoint. Regression: the 07-18
    rate-based leaderboard rework widened the SQL to 8 columns and
    6-tuple rows made r[6] raise IndexError."""
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")

    def _short_rows(conn, sql, args=None):
        if "status = 'open'" in sql and "LIMIT 15" in sql:
            return [("stale_shape", "https://dchub.cloud/x",
                     "brand_surface", 828.4, 700, 1),          # 6 cols: skip
                    ("full_shape", "https://dchub.cloud/y",
                     "fast_qa", 10.0, 5, 2, 12.0, 120.0)]      # 8 cols: keep
        return None

    client = _mk_client(monkeypatch, fake_rows=_short_rows)
    r = client.get("/api/v1/admin/brain/episodes/analytics",
                   headers={"X-Admin-Key": "sekrit"})
    assert r.status_code == 200
    d = r.get_json()
    assert [e["issue"] for e in d["chronic_leaderboard"]] == ["full_shape"]


def test_endpoint_200_fail_soft_when_queries_break(monkeypatch):
    """Every section nulls (never 500s) when the SQL fails — schema drift
    must degrade the payload, not the endpoint."""
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    client = _mk_client(monkeypatch)  # _rows always returns None
    r = client.get("/api/v1/admin/brain/episodes/analytics?admin_key=sekrit")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["open_now"] is None
    assert d["chronic_leaderboard"] == []
