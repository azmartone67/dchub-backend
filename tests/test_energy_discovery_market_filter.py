"""SH52-051 follow-on — /api/energy-discovery/* must honour ?market=.

WHAT THIS GUARDS
================
The 2026-06-06 "live table" rewrite of routes/energy_discovery_routes.py moved
these endpoints off the curated seed arrays and onto power_plants_eia /
gas_pipelines / the transmission snapshot. `?market=` survived that rewrite
ONLY in the seed fallback (`_filter_market`). The live SQL had no market
predicate and stamped every row `'market': ''`.

Measured against production 2026-08-09, BEFORE the fix:

    market=phoenix            200  500 rows  47 states  sha1 1e9bd5f0ea
    market=dallas             200  500 rows  47 states  sha1 1e9bd5f0ea
    market=northern_virginia  200  500 rows  47 states  sha1 1e9bd5f0ea
    market=atlanta            200  500 rows  47 states  sha1 1e9bd5f0ea
    ... 7/7 markets byte-identical, top row "Grand Coulee" (WA) for Phoenix.

The data-sync "Energy discovery per market" step read that as
"23 markets OK, 11,500 plants" — 23 copies of one national list. A green
verification that verifies nothing (shell #51: FRESH != GROWTH).

These tests drive the real Flask view functions with `_rows_from_db` swapped
for a recorder, so they assert on the SQL and params the endpoint ACTUALLY
builds. Every assertion below fails against the pre-fix source.
"""

import importlib

import pytest

edr = importlib.import_module("routes.energy_discovery_routes")


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

def _record(monkeypatch, rows=None):
    """Swap _rows_from_db for a recorder. Returns the capture list."""
    seen = []

    def fake(sql, params, mapper):
        seen.append({"sql": " ".join(sql.split()), "params": list(params)})
        return list(rows or [])

    monkeypatch.setattr(edr, "_rows_from_db", fake)
    return seen


def _call(view_name, query):
    """Invoke a view function inside a request context, return (json, status)."""
    from flask import Flask

    app = Flask(__name__)
    with app.test_request_context("/?" + query):
        rv = getattr(edr, view_name)()
    if isinstance(rv, tuple):
        resp, status = rv[0], rv[1]
    else:
        resp, status = rv, 200
    return resp.get_json(), status


ENDPOINTS = [
    "energy_discovery_power_plants",
    "energy_discovery_transmission_lines",
    "energy_discovery_wind_projects",
    "energy_discovery_pipelines",
]


# ---------------------------------------------------------------------------
# the actual defect
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("view", ENDPOINTS)
def test_market_reaches_the_live_sql(monkeypatch, view):
    """?market= must constrain the LIVE query, not just the seed fallback."""
    seen = _record(monkeypatch)
    _call(view, "market=phoenix&limit=500")

    assert seen, f"{view} issued no query at all — test is vacuous, not passing"
    sql = seen[0]["sql"]
    assert "lat BETWEEN %s AND %s" in sql and "lng BETWEEN %s AND %s" in sql, (
        f"{view} built its live query with NO market predicate:\n  {sql}\n"
        "This is the SH52-051 follow-on defect: every market gets the same "
        "national list."
    )


@pytest.mark.parametrize("view", ENDPOINTS)
def test_different_markets_produce_different_queries(monkeypatch, view):
    """Phoenix and Atlanta must not resolve to the same bound parameters."""
    phx = _record(monkeypatch)
    _call(view, "market=phoenix&limit=500")
    atl = _record(monkeypatch)
    _call(view, "market=atlanta&limit=500")

    assert phx and atl, "no query captured — test is vacuous"
    assert phx[0]["params"] != atl[0]["params"], (
        f"{view}: phoenix and atlanta bound IDENTICAL params "
        f"{phx[0]['params']!r} — the market filter is not applied."
    )


def test_phoenix_bbox_contains_palo_verde_and_excludes_grand_coulee():
    """The radius has to be sized to the generation shed, not the city limits.

    Palo Verde (75 km out) is Phoenix's anchor unit and must be INSIDE.
    Grand Coulee, WA — the row production actually returned for
    ?market=phoenix — must be OUTSIDE.
    """
    lo_lat, hi_lat, lo_lng, hi_lng = edr._market_bbox("phoenix")

    def inside(lat, lng):
        return lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng

    assert inside(33.3881, -112.8614), "Palo Verde fell outside the Phoenix box"
    assert not inside(47.9575, -118.9773), (
        "Grand Coulee (WA) is inside the Phoenix box — this is the exact row "
        "production served for ?market=phoenix"
    )


# ---------------------------------------------------------------------------
# no-filter and unknown-key behaviour
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("view", ENDPOINTS)
def test_no_market_is_still_unfiltered(monkeypatch, view):
    """Omitting ?market= must keep the national behaviour the map relies on."""
    seen = _record(monkeypatch)
    _call(view, "limit=500")

    assert seen, "no query captured — test is vacuous"
    assert "lat BETWEEN" not in seen[0]["sql"], (
        f"{view} applied a market box with no market requested"
    )


@pytest.mark.parametrize("view", ENDPOINTS)
def test_unknown_market_is_a_400_not_a_silent_national_list(monkeypatch, view):
    """`?market=typo` must NOT quietly return everything.

    Silently widening an unrecognised key to "all markets" is how this stayed
    invisible: the caller asked for one market, got 47 states, and had no
    signal that the filter never applied.
    """
    _record(monkeypatch)
    body, status = _call(view, "market=not_a_real_market&limit=500")

    assert status == 400, f"{view} returned {status} for an unknown market key"
    assert body["error"] == "unknown_market"
    assert "phoenix" in body["valid_markets"]


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("salt_lake_city", "salt_lake"),
        ("new_york", "new_york_nj"),
        ("seattle", "seattle_quincy"),
        ("portland", "portland_hillsboro"),
    ],
)
def test_data_sync_workflow_market_names_resolve(alias, canonical):
    """.github/workflows/data-sync.yml ships these four non-canonical names.

    They are not keys in MONITORED_MARKETS, so without the alias table they
    would 400 the four markets the workflow asks for by those names.
    """
    key, err = edr._resolve_market(alias)
    assert err is None, f"{alias} did not resolve: {err}"
    assert key == canonical


def test_every_workflow_market_resolves():
    """Every market string the data-sync workflow sends must resolve."""
    workflow_markets = (
        "phoenix dallas northern_virginia atlanta las_vegas salt_lake_city "
        "columbus des_moines chicago silicon_valley new_york seattle portland "
        "denver san_antonio houston miami reno sacramento minneapolis "
        "kansas_city richmond nashville"
    ).split()
    assert len(workflow_markets) == 23
    unresolved = [m for m in workflow_markets if _resolve_or_none(m) is None]
    assert not unresolved, f"data-sync sends markets the API rejects: {unresolved}"


def _resolve_or_none(m):
    key, err = edr._resolve_market(m)
    return None if err else key


@pytest.mark.parametrize("view", ENDPOINTS)
def test_rows_are_stamped_with_the_resolved_market(monkeypatch, view):
    """A consumer must be able to tell from the payload that filtering ran."""
    _record(monkeypatch)
    body, status = _call(view, "market=denver&limit=10")

    assert status == 200
    assert body["market"] == "denver"
    assert body["market_filtered"] is True
