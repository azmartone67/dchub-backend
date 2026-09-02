"""GUARD — a dead upstream must cost LESS per request, not more.

WHAT WAS MEASURED (2026-09-02 05:45-06:05Z, ENTSO-E scheduled maintenance)

  web-api.tp.entsoe.eu answered HTTP 503 with an HTML "Transparency Platform —
  Service Temporarily Unavailable. Scheduled maintenance is currently underway"
  body. Tokenless request: ALSO 503, not 401 — so the platform was failing
  before auth, which is what rules out our credential. /api/v1/iso/eu/health
  reported token_configured:true, live_feed_ok:false.

  GET /api/v1/iso/eu/snapshot -> 503 after 37.7s, on EVERY request.

  Root cause: _SNAP_CACHE is populated ONLY on success (`_SNAP_CACHE["data"] =
  result`, reached after `if not zones: return None`). So a total failure
  stored nothing, and every caller re-entered the full fan-out: 33 zones,
  max_workers=24, `as_completed(timeout=25)`. A dead upstream cost MORE per
  request than a healthy one and did so indefinitely — 33 outbound sockets per
  request per gunicorn worker per replica, aimed at an origin already in
  maintenance.

THE CONTRACT

  1. A total failure is remembered for _SNAP_DOWN_TTL and short-circuits the
     fan-out — the second caller opens ZERO sockets.
  2. force=True NEVER reads either cache. run_extraction() uses it, because
     its summary is the verdict the data-pulse must-have gate and the
     iso-eu-entsoe deadman feed read (#3568). A verdict inherited from a
     breaker some dashboard set 30s earlier would be a measured-looking claim
     about a measurement that never happened.
  3. Any zone answering clears the breaker IMMEDIATELY — a stale negative
     cache must not serve 503 through a recovery.
  4. The breaker expires on its own.
  5. /snapshot says WHICH of the two facts it is reporting: "no_zone_answered"
     (we asked 33 times) vs "upstream_down_recently" (we asked nothing), and
     carries Retry-After.

MUST-FAIL — executed 2026-09-02, each mutation confirmed applied on disk
before its run (a no-opped mutation reads as a passing guard):
    baseline                                          exit=0   9 passed
    M1  remove the negative-cache short-circuit       exit=1   1 failed
    M2  run_extraction stops forcing                  exit=1   1 failed
    M3  success no longer clears the breaker          exit=1   1 failed
    M4  route reads the breaker AFTER the call        exit=1   1 failed
    M5  every failure restamps the outage start       exit=1   1 failed
    M6  force stops bypassing the POSITIVE cache      exit=1   2 failed

★ M4 is not hypothetical — it is the bug this test caught on its first run.
_live_snapshot() OPENS the breaker on its way out of a failed fan-out, so the
route's original `if _now < _SNAP_DOWN["until"]` was true for the very request
that had just measured all 33 zones, and it reported "fan-out skipped" about
calls it had actually made. The state must be sampled BEFORE the call.

NO NETWORK, NO DB: _zone_snapshot is replaced by a counting stub.
"""
import time

import pytest

import routes.iso_eu_entsoe as eu


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts with both caches empty and a token present."""
    monkeypatch.setattr(eu, "_token", lambda: "test-token")
    eu._SNAP_CACHE["data"] = None
    eu._SNAP_CACHE["ts"] = 0.0
    eu._SNAP_DOWN["until"] = 0.0
    eu._SNAP_DOWN["since"] = 0.0
    eu._SNAP_DOWN["reason"] = ""
    eu._ZONE_CACHE.clear()
    eu._ZONE_ERRORS.clear()
    yield
    eu._SNAP_DOWN["until"] = 0.0
    eu._SNAP_DOWN["since"] = 0.0
    eu._SNAP_CACHE["data"] = None


def _counting_zone(monkeypatch, result):
    """Replace the per-zone fetch with a stub that counts every call.

    `result` is a callable(code) -> snap|None, so a test can flip the upstream
    from dead to alive between fan-outs.
    """
    calls = []

    def _stub(code, max_age=None):
        calls.append(code)
        return result(code)

    monkeypatch.setattr(eu, "_zone_snapshot", _stub)
    return calls


def _good_zone(code):
    return {
        "code": code, "name": code, "hub": code,
        "data_period_end": "2026-09-02T05:00:00Z",
        "data_period_end_newest": "2026-09-02T05:00:00Z",
        "generation_total_mw": 1000.0, "fuel_gas_mw": 100.0,
        "fuel_nuclear_mw": 100.0, "fuel_coal_mw": 100.0,
        "fuel_wind_mw": 300.0, "fuel_solar_mw": 200.0,
        "fuel_hydro_mw": 100.0, "fuel_biomass_mw": 100.0,
        "renewable_pct": 60.0, "gas_pct": 10.0,
    }


# ── 1. the stampede is stopped ───────────────────────────────────────────────

def test_second_call_during_an_outage_opens_zero_sockets(monkeypatch):
    calls = _counting_zone(monkeypatch, lambda c: None)

    assert eu._live_snapshot() is None
    first = len(calls)
    assert first == len(eu._ZONES), (
        "the FIRST call must still measure every zone — %d of %d"
        % (first, len(eu._ZONES)))

    assert eu._live_snapshot() is None
    assert len(calls) == first, (
        "the second call re-ran the fan-out: %d extra zone fetches against an "
        "upstream already known to be down" % (len(calls) - first))


def test_the_breaker_records_when_the_outage_started(monkeypatch):
    _counting_zone(monkeypatch, lambda c: None)
    eu._live_snapshot()
    since = eu._SNAP_DOWN["since"]
    assert since > 0 and eu._SNAP_DOWN["reason"] == "all_zones_failed"
    # A second failure must NOT restamp the start — the outage began once.
    eu._SNAP_DOWN["until"] = 0.0          # let it re-measure
    eu._live_snapshot()
    assert eu._SNAP_DOWN["since"] == since, "consecutive failures restamped the start"


# ── 2. the PROBE never inherits the verdict ──────────────────────────────────

def test_force_always_measures_even_with_the_breaker_open(monkeypatch):
    calls = _counting_zone(monkeypatch, lambda c: None)
    eu._live_snapshot()
    first = len(calls)
    assert eu._SNAP_DOWN["until"] > time.time(), "breaker should be open"

    assert eu._live_snapshot(force=True) is None
    assert len(calls) == first + len(eu._ZONES), (
        "run_extraction()'s probe read the negative cache — it would report "
        "'all zones unreachable' WITHOUT HAVING ASKED, and that summary is "
        "what the data-pulse gate and the deadman feed treat as measured")


def test_run_extraction_passes_force(monkeypatch):
    """The wiring, not just the capability: the caller whose output is a
    verdict must be the one using force."""
    seen = {}

    def _fake(force=False):
        seen["force"] = force
        return None

    monkeypatch.setattr(eu, "_live_snapshot", _fake)
    out = eu.run_extraction()
    assert seen.get("force") is True, "run_extraction() must force a live measurement"
    assert out["status"] == "error"


def test_force_also_bypasses_the_POSITIVE_cache(monkeypatch):
    calls = _counting_zone(monkeypatch, _good_zone)
    eu._live_snapshot()
    first = len(calls)
    eu._live_snapshot()                     # served from _SNAP_CACHE
    assert len(calls) == first
    eu._live_snapshot(force=True)
    assert len(calls) == first + len(eu._ZONES), (
        "a forced probe served a cached success — the extractor would write "
        "rows it did not fetch")


# ── 3 & 4. recovery ──────────────────────────────────────────────────────────

def test_a_single_answering_zone_clears_the_breaker(monkeypatch):
    state = {"alive": False}
    _counting_zone(monkeypatch, lambda c: _good_zone(c) if state["alive"] else None)

    eu._live_snapshot()
    assert eu._SNAP_DOWN["until"] > time.time()

    state["alive"] = True
    eu._SNAP_DOWN["until"] = 0.0            # simulate the TTL lapsing
    assert eu._live_snapshot() is not None
    assert eu._SNAP_DOWN["until"] == 0.0 and eu._SNAP_DOWN["since"] == 0.0, (
        "a stale negative cache survived a recovery and would keep serving 503")


def test_the_breaker_expires(monkeypatch):
    calls = _counting_zone(monkeypatch, lambda c: None)
    eu._live_snapshot()
    first = len(calls)
    # Wind the clock past the cooldown rather than sleeping.
    eu._SNAP_DOWN["until"] = time.time() - 0.01
    eu._live_snapshot()
    assert len(calls) == first + len(eu._ZONES), (
        "the breaker never expired — a recovered upstream would stay dark")


def test_cooldown_is_short_enough_to_show_a_recovery():
    assert 0 < eu._SNAP_DOWN_TTL <= 300, (
        "the negative cache bounds how stale the 'down' verdict can be; "
        "beyond a few minutes it hides a recovery instead of sparing an outage")


# ── 5. the two facts are not the same sentence ───────────────────────────────

def test_route_distinguishes_asked_and_failed_from_did_not_ask(monkeypatch):
    """'no_zone_answered' is a claim about 33 calls. When the breaker
    short-circuits we made none of them, and must not say the same thing."""
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(eu.iso_eu_entsoe_bp)
    client = app.test_client()

    _counting_zone(monkeypatch, lambda c: None)

    r1 = client.get("/api/v1/iso/eu/snapshot")
    assert r1.status_code == 503
    assert r1.get_json()["reason"] == "no_zone_answered"
    assert r1.get_json()["zones_attempted"] == len(eu._ZONES)

    r2 = client.get("/api/v1/iso/eu/snapshot")
    assert r2.status_code == 503
    body = r2.get_json()
    assert body["reason"].startswith("upstream_down_recently"), body["reason"]
    assert "asked NOTHING" in body["basis"]
    assert body["retry_after_s"] >= 1
    assert r2.headers.get("Retry-After"), "a 503 that says 'retry' must say when"
