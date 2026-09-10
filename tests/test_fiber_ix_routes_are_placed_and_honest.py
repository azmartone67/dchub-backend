"""The IX fiber lane must place its routes, and must not invent a strand count.

TWO FAILURES, one visible and one not.

VISIBLE — the lane returned [] on every run from 2026-06-22. It filtered on
ix["latitude"], and PeeringDB's ix objects carry no coordinate field at all
(measured 2026-09-09: /api/ix returns 212 US exchanges, none with lat/lng).
Coordinates live on the FACILITY objects, joined through /api/ixfac.

INVISIBLE, and the reason this was escalated to the owner before it was fixed —
the old code set fiber_count to max(net_count), an exchange's PEER count. The
upsert renders that field to humans as "<n> fibers", so a derived straight line
between two exchange centroids published "57 fibers" about a route whose strand
count nobody has measured. The surveyed carrier lanes leave the field NULL on
all 20,381 rows. A wrong number is worse than an honest blank.

★ A FAILED FETCH AND AN EMPTY JOIN MUST NOT LOOK ALIKE. That is what hid the
  first failure for 73 days: the lane reported "ok" while returning nothing.
"""
import pytest

import fiber_network_discovery as F


class _Resp:
    """Mirrors the attributes of requests.Response that the lane touches:
    status_code, .json() AND .text. The first version omitted .text, which the
    real object always has — so the error path blew up on the fixture instead of
    exercising the code. A fake missing something the real thing always carries
    fails just as blindly as one that carries something extra."""

    def __init__(self, payload, status=200, text=""):
        self._p, self.status_code, self.text = payload, status, text

    def json(self):
        return {"data": self._p}


IX = [{"id": 1, "name": "AlphaIX", "city": "Ashburn", "net_count": 300},
      {"id": 2, "name": "BetaIX", "city": "New York", "net_count": 12},
      {"id": 3, "name": "GhostIX", "city": "Nowhere", "net_count": 99}]
FAC = [{"id": 10, "latitude": 39.0164, "longitude": -77.4590},
       {"id": 20, "latitude": 40.7128, "longitude": -74.0060},
       {"id": 30, "latitude": None, "longitude": None}]      # unusable
IXFAC = [{"ix_id": 1, "fac_id": 10}, {"ix_id": 2, "fac_id": 20},
         {"ix_id": 3, "fac_id": 30}]                          # GhostIX unplaceable


def _wire(monkeypatch, ix=IX, fac=FAC, ixfac=IXFAC, status=200):
    def fake_get(url, **kw):
        body = "Request was throttled." if status == 429 else ""
        if "/ixfac" in url:
            return _Resp(ixfac, status, body)
        if "/fac" in url:
            return _Resp(fac, status, body)
        return _Resp(ix, status, body)
    monkeypatch.setattr(F.requests, "get", fake_get)


def test_routes_are_placed_from_facility_coordinates(monkeypatch):
    _wire(monkeypatch)
    routes, diag = F._discover_peeringdb_fiber()
    assert diag["status"] == "ok"
    assert routes, "no routes built — the ix->ixfac->fac join produced nothing"
    names = {r["name"] for r in routes}
    assert any("AlphaIX" in n and "BetaIX" in n for n in names)


def test_an_exchange_with_no_placeable_facility_is_dropped_not_defaulted(monkeypatch):
    """GhostIX's only facility has no coordinates. It must vanish, not land at
    (0, 0) — a route to the Gulf of Guinea is worse than a missing route."""
    _wire(monkeypatch)
    routes, _ = F._discover_peeringdb_fiber()
    assert not any("GhostIX" in r["name"] for r in routes)
    for r in routes:
        for k in ("start_lat", "start_lng", "end_lat", "end_lng"):
            assert abs(r[k]) > 0.1, f"{k} defaulted to zero"


# ── the invisible failure ───────────────────────────────────────────────────
def test_fiber_count_is_never_the_peer_count(monkeypatch):
    _wire(monkeypatch)
    routes, _ = F._discover_peeringdb_fiber()
    assert routes
    for r in routes:
        assert r["fiber_count"] is None, (
            f"published fiber_count={r['fiber_count']!r} on a DERIVED route; the "
            f"upsert renders that as '<n> fibers' and nobody counted strands here")
    # control: the peer counts ARE in the fixture, so this could have failed
    assert max(i["net_count"] for i in IX) == 300


def test_the_capacity_label_stays_blank(monkeypatch):
    """The upsert derives capacity from fiber_count. Blank in, blank out."""
    _wire(monkeypatch)
    routes, _ = F._discover_peeringdb_fiber()
    for r in routes:
        label = f"{r.get('fiber_count', 0)} fibers" if r.get("fiber_count") else None
        assert label is None


# ── a dead fetch must not read as an empty join ─────────────────────────────
@pytest.mark.parametrize("code", [429, 503])
def test_a_non_200_is_reported_as_a_fetch_failure_with_its_code(monkeypatch, code):
    """A dead endpoint must not read as 'no usable records' — that is what hid
    this lane for 73 days. And the CODE must survive: 429 means we are being
    throttled (this lane now makes three calls per run against a source that
    throttles anonymous callers after ~4) and 5xx means their server broke.
    Those two demand different responses, so they must not collapse."""
    _wire(monkeypatch, status=code)
    routes, diag = F._discover_peeringdb_fiber()
    assert routes == []
    assert diag["status"] == "http_%d" % code, (
        f"status={diag['status']!r} — the HTTP code was lost, so throttling and "
        f"an outage now look identical")
    assert diag["status"] != "no_usable_records"


def test_an_empty_join_says_the_join_is_empty(monkeypatch):
    """Fetches succeed, nothing links. The detail must name the JOIN."""
    _wire(monkeypatch, ixfac=[])
    routes, diag = F._discover_peeringdb_fiber()
    assert routes == []
    assert diag["status"] == "no_usable_records"
    assert "join" in (diag["detail"] or "").lower()
