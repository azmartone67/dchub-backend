#!/usr/bin/env python3
"""tests/test_fiber_lane_reports_no_source.py — a discovery lane whose only
source returns nothing must not report success.

NO NETWORK, NO DB. The real `run_fiber_discovery` and the real
`_discover_peeringdb_fiber` run against a stubbed `requests` and a stubbed
connection.

WHAT WENT WRONG (measured 2026-09-03). `fiber_routes` last received a
`peeringdb` row on 2026-06-22 — 73 days. Every `daily-infra-sync` run in
between reported `"fiber": {"status": "ok", "seeded": 20}`. Two independent
defects, and both were invisible:

  1. PeeringDB's `/api/ix` carries NO latitude/longitude field. Measured live:
     `GET /api/ix?country=US&status=ok` -> HTTP 200, 212 US exchanges, and the
     full key set is aka/city/country/created/fac_count/id/.../website — no
     coordinate of any kind. `if lat and lng` therefore dropped all 212 and the
     pair loop never ran. Coordinates live on `/api/fac` (1,376 facilities,
     1,353 with usable lat/lng). This is NOT throttling and NOT egress.

  2. Anonymous callers are throttled after ~4 requests ("Expected available in
     58 minutes. Authenticate for less restrictions."). A 429 took the
     `status_code != 200` branch, logged a warning, and returned [] — the same
     value as "healthy, nothing new".

Both collapsed into `discovered: 0`, and `run_fiber_discovery` set
`status = 'success' if errors == 0` where `errors` counts failed row WRITES.
The write path was never broken, so nothing could ever make that field nonzero.

★ THE SEED CANNOT RESCUE IT. `seeded: 20` counts a hardcoded list re-upserted
every run: exactly 20 rows carry `updated_at = today` and 0 carry
`created_at = today`, every day. Counting it as a write attempt is what made
the infrastructure-sync error blame the write path for 20 attempts that were
never discovery.

Run standalone:   python3 tests/test_fiber_lane_reports_no_source.py
Run under pytest: pytest tests/test_fiber_lane_reports_no_source.py
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("requests")
import fiber_network_discovery as F  # noqa: E402


class _Resp:
    def __init__(self, code, payload=None, text=""):
        self.status_code, self._p, self.text = code, payload or {}, text

    def json(self):
        return self._p


class _Cur:
    def __init__(self):
        self.rows = 0

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return [64836]

    def close(self):
        pass


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_db_no_net(monkeypatch):
    """Every test drives the REAL run_fiber_discovery; only the edges are stubs."""
    monkeypatch.setattr(F, "_ensure_fiber_routes_table", lambda: True)
    monkeypatch.setattr(F, "_get_pg_connection", lambda: _Conn())
    monkeypatch.setattr(F, "_upsert_fiber_route", lambda conn, route: True)


# The real shape PeeringDB returns: 200, records present, NO coordinates.
IX_NO_COORDS = {"data": [
    {"id": 1, "name": "Equinix Ashburn", "city": "Ashburn", "net_count": 517},
    {"id": 2, "name": "Equinix Chicago", "city": "Chicago", "net_count": 340},
]}
IX_WITH_COORDS = {"data": [
    {"id": 1, "name": "A", "city": "Ashburn", "latitude": 39.0,
     "longitude": -77.4, "net_count": 500},
    # ~145 miles apart. The generator only emits pairs 50-500 miles apart, so
    # a closer pair (Ashburn/Baltimore is 48) silently produces nothing and the
    # control would assert on an empty result for the wrong reason.
    {"id": 2, "name": "B", "city": "Philadelphia", "latitude": 39.95,
     "longitude": -75.16, "net_count": 200},
]}


def _get(payload=None, code=200, exc=None, text=""):
    def _f(*a, **k):
        if exc:
            raise exc
        return _Resp(code, payload, text)
    return _f


def test_the_real_production_shape_is_reported_as_no_usable_records(monkeypatch):
    """200 + records + zero coordinates is its OWN state, not 'ok'."""
    monkeypatch.setattr(F.requests, "get", _get(IX_NO_COORDS))
    res = F.run_fiber_discovery()
    assert res["peeringdb"]["status"] == "no_usable_records"
    assert res["peeringdb"]["fetched"] == 2
    assert res["peeringdb"]["usable"] == 0
    assert res["status"] == "no_source", (
        "a lane whose only source yielded nothing must not report success")
    assert res["seeded"] == len(F.MAJOR_ROUTES), (
        "precondition: the seed still ran — it is what used to mask this")


def test_a_429_is_not_the_same_as_healthy_and_empty(monkeypatch):
    monkeypatch.setattr(F.requests, "get",
                        _get(code=429, text="Request was throttled."))
    res = F.run_fiber_discovery()
    assert res["peeringdb"]["status"] == "http_429"
    assert "throttled" in (res["peeringdb"]["detail"] or "")
    assert res["status"] == "no_source"


def test_a_timeout_is_recorded_as_a_timeout(monkeypatch):
    monkeypatch.setattr(F.requests, "get",
                        _get(exc=F.requests.exceptions.Timeout()))
    res = F.run_fiber_discovery()
    assert res["peeringdb"]["status"] == "timeout"
    assert res["status"] == "no_source"


def test_a_working_source_still_reports_success(monkeypatch):
    """★ The control. This must not become a check that always fails."""
    monkeypatch.setattr(F.requests, "get", _get(IX_WITH_COORDS))
    res = F.run_fiber_discovery()
    assert res["peeringdb"]["status"] == "ok"
    assert res["peeringdb"]["usable"] == 2
    assert res["discovered"] > 0
    assert res["status"] == "success"


def test_the_seed_is_published_as_hardcoded_so_callers_need_not_know(monkeypatch):
    monkeypatch.setattr(F.requests, "get", _get(IX_NO_COORDS))
    res = F.run_fiber_discovery()
    assert res["seed_is_hardcoded"] is True
    assert res["seed_row_count"] == len(F.MAJOR_ROUTES)


def test_a_failed_write_still_reports_partial_not_no_source(monkeypatch):
    """errors (failed writes) and no_source are different diagnoses."""
    monkeypatch.setattr(F, "_upsert_fiber_route", lambda conn, route: False)
    monkeypatch.setattr(F.requests, "get", _get(IX_WITH_COORDS))
    res = F.run_fiber_discovery()
    assert res["errors"] > 0
    assert res["status"] == "partial"


# ──────────────────────────────────────────────────────────────────────────
# The infrastructure-sync side. These drive the two decisions the handler
# makes, which until 2026-09-03 lived inline and could only be grepped.
# ──────────────────────────────────────────────────────────────────────────

jobs_routes = pytest.importorskip("routes.jobs_routes")


def test_no_source_becomes_a_job_error_not_an_ok_leg():
    st, err = jobs_routes._fiber_status_from(
        {"status": "no_source", "message": "peeringdb=no_usable_records"})
    assert st == "no_source"
    assert "peeringdb" in err


def test_a_healthy_loader_is_still_ok():
    st, err = jobs_routes._fiber_status_from({"status": "success"})
    assert (st, err) == ("ok", None)


def test_the_hardcoded_seed_is_not_counted_as_a_write_attempt():
    """★ This is what made the red permanent and blamed the write path."""
    n = jobs_routes._fiber_write_attempts(
        {"seeded": 20, "discovered": 0, "seed_is_hardcoded": True})
    assert n == 0, "20 re-upserts of a fixed list are not 20 write attempts"


def test_real_discovery_is_still_counted():
    n = jobs_routes._fiber_write_attempts(
        {"seeded": 20, "discovered": 7, "seed_is_hardcoded": True})
    assert n == 7


def test_a_loader_that_does_not_declare_its_seed_is_still_counted():
    """★ Silence must not zero the check — that is unknown-as-healthy."""
    n = jobs_routes._fiber_write_attempts({"seeded": 20, "discovered": 3})
    assert n == 23


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
