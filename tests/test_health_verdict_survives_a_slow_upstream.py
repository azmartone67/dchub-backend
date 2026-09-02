"""GUARD — the verdict must survive a SLOW origin, not just a failing one.

WHAT WAS MEASURED (2026-09-02, worker 4.9.50, ENTSO-E still in maintenance)

Six consecutive edge reads of /api/v1/iso/eu/health:

    503 railway  verdict=no-failover   x4   (9.5s, 11.6s, 14.4s, 22.9s)
    200 render   failover=true         x2   (20.0s, 25.3s)

The STEP 2.4 verdict exemption shipped in #3594 was CORRECT and still fired
only about two thirds of the time.

ROOT CAUSE — TWO 15-SECOND BUDGETS RACING EACH OTHER:
  * worker.js getTimeout() matched NO prefix for /api/v1/iso/eu/health, so it
    took ROUTE_TIMEOUTS.DEFAULT = 15_000.
  * this module's /health ran an UNCACHED DE_LU probe on every cache miss with
    the module default `timeout=15`.
Same number, so it was a coin flip by construction. When the origin overran,
proxyToRailway aborted, `resp` came back null, STEP 2.4's `resp &&` guard
skipped the exemption, and the stale Render 200 shipped.

★ `resp &&` is SELF-DEFEATING for this class of route and must NOT simply be
deleted — a null resp genuinely is "no verdict", and a dead Railway must still
fail over. The fix is to stop MANUFACTURING nulls: the endpoint whose job is to
report a dead upstream is the one most likely to be slow BECAUSE that upstream
is dead.

THE CONTRACT
  1. /health bounds its probe far below the edge budget.
  2. A live negative cache answers WITHOUT probing at all.
  3. That short-circuit is ONE-DIRECTIONAL — it can report DOWN, never up.
     A stale "down" self-corrects on the next probe; a stale "up" is the exact
     lie this endpoint exists to prevent.
  4. /health says HOW it decided (`basis`).
  5. worker.js gives verdict routes their own timeout, wider than DEFAULT.

MUST-FAIL — executed 2026-09-02, each mutation confirmed applied on disk:
    baseline                                          exit=0  7 passed
    M1 /health drops the bounded timeout               exit=1  1 failed
    M2 negative-cache short-circuit removed            exit=1  2 failed
    M3 short-circuit INVERTED (open breaker => 200)    exit=1  2 failed
    M4 worker reverts to getTimeout for verdict routes exit=1  1 failed
    M5 VERDICT_ROUTE_TIMEOUT_MS lowered to the default exit=1  1 failed
    M6 _HEALTH_PROBE_TIMEOUT_S raised back to 15       exit=1  1 failed

★ M2 and M3 are DIFFERENT bugs that this file catches with the same two tests:
"stop short-circuiting" and "short-circuit the wrong way" both break the
one-directional property. Kept as separate mutations because only M3 can serve
a stale UP, which is the failure mode that matters.

NO NETWORK, NO DB.
"""
import re
import time
from pathlib import Path

import pytest

import routes.iso_eu_entsoe as eu

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "worker.js").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(eu, "_token", lambda: "tok")
    eu._SNAP_DOWN["until"] = 0.0
    eu._SNAP_DOWN["since"] = 0.0
    eu._ZONE_CACHE.clear()
    yield
    eu._SNAP_DOWN["until"] = 0.0
    eu._SNAP_DOWN["since"] = 0.0


def _client():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(eu.iso_eu_entsoe_bp)
    return app.test_client()


def _spy(monkeypatch, result=None):
    """Record every _zone_snapshot call, including the kwargs it was given."""
    calls = []

    def _stub(code, max_age=None, timeout=None):
        calls.append({"code": code, "timeout": timeout})
        return result

    monkeypatch.setattr(eu, "_zone_snapshot", _stub)
    return calls


# ── 1 & 4. the probe is bounded, and the endpoint says how it decided ────────

def test_health_bounds_its_probe_far_below_the_edge_budget(monkeypatch):
    calls = _spy(monkeypatch, None)
    r = _client().get("/api/v1/iso/eu/health")
    assert r.status_code == 503
    assert len(calls) == 1, "expected exactly one probe, got %d" % len(calls)
    assert calls[0]["timeout"] == eu._HEALTH_PROBE_TIMEOUT_S, (
        "the probe ran with timeout=%r — an unbounded probe races the edge's "
        "route timeout and the verdict is lost to a stale mirror"
        % calls[0]["timeout"])


def test_the_probe_budget_is_small_enough_to_not_race_the_edge():
    assert 0 < eu._HEALTH_PROBE_TIMEOUT_S <= 10, (
        "_HEALTH_PROBE_TIMEOUT_S=%s — at or near the edge's own route timeout "
        "this is a coin flip again, which is how 2 of 6 reads got laundered"
        % eu._HEALTH_PROBE_TIMEOUT_S)


def test_health_reports_how_it_decided(monkeypatch):
    _spy(monkeypatch, None)
    body = _client().get("/api/v1/iso/eu/health").get_json()
    assert body.get("basis", "").startswith("live_probe"), body.get("basis")

    monkeypatch.setattr(eu, "_token", lambda: "")
    body = _client().get("/api/v1/iso/eu/health").get_json()
    assert body.get("basis", "").startswith("token_missing"), body.get("basis")


# ── 2 & 3. the negative cache answers without asking, and only says DOWN ─────

def test_an_open_breaker_answers_without_probing_at_all(monkeypatch):
    calls = _spy(monkeypatch, None)
    eu._SNAP_DOWN["until"] = time.time() + 30
    eu._SNAP_DOWN["since"] = time.time() - 5

    r = _client().get("/api/v1/iso/eu/health")
    assert r.status_code == 503
    assert calls == [], (
        "the endpoint probed a upstream it had just measured as dead — that "
        "probe is what blows the edge budget during the very outage this "
        "endpoint exists to report")
    assert r.get_json().get("basis", "").startswith("negative_cache")


def test_the_short_circuit_can_only_report_DOWN(monkeypatch):
    """One-directional by design. A stale 'down' self-corrects on the next
    probe; a stale 'up' is the lie this endpoint exists to prevent."""
    # Upstream is healthy, but the breaker is still open from a moment ago.
    _spy(monkeypatch, {"observed_age_s": 0, "data_age_s": 60, "data_period_end": "x"})
    eu._SNAP_DOWN["until"] = time.time() + 30

    r = _client().get("/api/v1/iso/eu/health")
    assert r.status_code == 503 and r.get_json()["live_feed_ok"] is False, (
        "an open breaker produced a 200 — the short-circuit has been inverted "
        "and can now serve a stale UP")


def test_an_expired_breaker_probes_again(monkeypatch):
    calls = _spy(monkeypatch, {"observed_age_s": 0, "data_age_s": 60, "data_period_end": "x"})
    eu._SNAP_DOWN["until"] = time.time() - 0.01   # lapsed
    r = _client().get("/api/v1/iso/eu/health")
    assert len(calls) == 1, "a lapsed breaker never re-probed — recovery stays dark"
    assert r.status_code == 200 and r.get_json()["live_feed_ok"] is True


# ── 5. the edge gives a verdict route room to answer ─────────────────────────

def test_worker_gives_verdict_routes_their_own_timeout():
    anchor = "const timeoutMs = isVerdictRoute(pathname)"
    assert WORKER.count(anchor) == 1, (
        "worker.js no longer widens the timeout for verdict routes; a slow "
        "origin will null the response and the exemption will be skipped")
    m = re.search(r"const VERDICT_ROUTE_TIMEOUT_MS = ([\d_]+);", WORKER)
    assert m, "VERDICT_ROUTE_TIMEOUT_MS is gone"
    verdict_ms = int(m.group(1).replace("_", ""))
    d = re.search(r"'DEFAULT':\s*([\d_]+),", WORKER)
    assert d, "ROUTE_TIMEOUTS.DEFAULT not parseable"
    default_ms = int(d.group(1).replace("_", ""))
    assert verdict_ms > default_ms, (
        "verdict timeout %d is not wider than DEFAULT %d — this is the exact "
        "race that laundered 2 of 6 reads" % (verdict_ms, default_ms))
    assert verdict_ms >= 3 * eu._HEALTH_PROBE_TIMEOUT_S * 1000, (
        "verdict timeout %dms leaves no headroom over the origin's own %ss "
        "probe budget" % (verdict_ms, eu._HEALTH_PROBE_TIMEOUT_S))
