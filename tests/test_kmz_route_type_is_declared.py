"""kmz_auto_discovery: a row's route_type is declared by a curated layer.

Measured in production on 2026-09-13, read-only: fiber_kmz_routes held 41,040
rows labelled 'fiber', and none of them came from a fiber layer.
- 7,748 were the curated HIFLD transmission lines. _process_known_sources
  labelled a layer 'gas' when its category was gas and 'fiber' otherwise.
- 33,292 came from the export stage. It fetched DISCOVERED layers (ArcGIS
  search results, whose category is the bucket of the search that found them)
  and relied on _fetch_arcgis_routes' default of 'fiber'. Six of those rows
  came from layers whose title names fiber, broadband, telecom, conduit or
  cable. The rest were wells, flood zones, water mains, pipelines, power
  stations, service territories and railroads.

These tests EXECUTE the module against a scripted ArcGIS server and a recording
database stub. Nothing here touches the network or a database.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import kmz_auto_discovery as kad  # noqa: E402

GAS_LAYER = "https://example.test/arcgis/rest/services/Gas/FeatureServer/0"
POWER_LAYER = "https://example.test/arcgis/rest/services/Power/FeatureServer/0"

# kmz_discovered_sources rows (id, name, url, provider) that wrote 'fiber' rows
# in production, as the export stage selected them.
DISCOVERED = [
    (686238, "Outer Continental Shelf Oil and Natural Gas Wells (Alaska)",
     "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/OCS_Oil_and_Natural_Gas_Wells_v1/FeatureServer/0",
     "someone"),
    (739601, "Flood Zones",
     "https://gcgis.guilfordcountync.gov/arcgis/rest/services/GISDV/Watershed_Management/FeatureServer/12",
     "someone"),
    (1015723, "FloodHazard_BestAvai_DNR_Water (BAFL Feature from Portal)",
     "https://gisdata.in.gov/server/rest/services/Hosted/FloodHazard_BestAvai_DNR_Water_PROD/FeatureServer/0",
     "someone"),
    (686302, "Broadband - Fiber Grant Requests based on the ePM map posted on UPlan",
     "https://services.arcgis.com/pA2nEVnB6tquxgOW/arcgis/rest/services/Fiber_Grants/FeatureServer/0",
     "someone"),
]


class _Response:
    def __init__(self, data):
        self.status_code = 200
        self._data = data

    def json(self):
        return self._data


class _Server:
    """Every layer answers a metadata read and one page holding one feature.
    Every URL requested is recorded."""

    def __init__(self):
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if url.endswith("?f=json"):
            return _Response({"objectIdField": "OBJECTID",
                              "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}]})
        assert "/query?" in url, f"unscripted request: {url}"
        return _Response({"features": [
            {"attributes": {"NAME": "segment", "OBJECTID": 1},
             "geometry": {"paths": [[[-77.40, 39.00], [-77.30, 39.05]]]}}]})


class _Cursor:
    def __init__(self, statements):
        self._statements = statements
        self._rows = []

    def execute(self, sql, params=None):
        q = " ".join(sql.split())
        self._statements.append(q)
        self._rows = list(DISCOVERED) if "FROM kmz_discovered_sources" in q else []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def __init__(self, statements):
        self._statements = statements

    def cursor(self):
        return _Cursor(self._statements)

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def written(monkeypatch):
    """Rows handed to the fiber_kmz_routes INSERT. A connection answers any
    SELECT on kmz_discovered_sources with the DISCOVERED rows above."""
    rows = []
    statements = []

    def _execute_values(cur, sql, values, page_size=None, fetch=False):
        assert "INSERT INTO fiber_kmz_routes" in sql, sql
        rows.extend(values)
        return [(v[5],) for v in values]  # RETURNING distance_km

    monkeypatch.setattr(kad, "_conn", lambda: _Conn(statements))
    monkeypatch.setattr(kad, "_release", lambda conn: None)
    monkeypatch.setattr(kad, "execute_values", _execute_values)
    monkeypatch.setattr(kad.time, "sleep", lambda s: None)
    return rows


def _engine(server):
    inst = object.__new__(kad.KMZAutoDiscovery)
    inst.session = server
    inst._cache = {"last_cycle": None, "total_routes_discovered": 0}
    inst._scheduler_running = False
    inst._cycle_in_progress = False
    inst._add_discovered_source = lambda source: False
    return inst


def _labels_by_layer(rows):
    # (name, provider, route_type, start, end, km, coordinates, kmz_file, source_url)
    out = {}
    for row in rows:
        out.setdefault(row[8], set()).add(row[2])
    return out


def test_every_curated_layer_is_written_under_the_route_type_it_declares(written):
    curated = [s for s in kad.PUBLIC_KMZ_SOURCES if s["type"] == "arcgis_kml"]
    assert curated, "no curated ArcGIS layers: this check would pass over nothing"
    r = _engine(_Server())._process_known_sources()
    assert (r["queried"], r["failed"]) == (len(curated), 0), r
    assert _labels_by_layer(written) == {s["url"]: {s["route_type"]} for s in curated}


def test_no_curated_layer_is_labelled_fiber_unless_it_is_curated_as_fiber():
    curated = [s for s in kad.PUBLIC_KMZ_SOURCES if s["type"] == "arcgis_kml"]
    assert curated, "no curated ArcGIS layers: this check would pass over nothing"
    undeclared = [s["name"] for s in curated if not s.get("route_type")]
    assert not undeclared, f"curated layers the cycle would refuse: {undeclared}"
    wrong = [(s["name"], s["category"]) for s in curated
             if s["route_type"] == "fiber" and s["category"] != "fiber"]
    assert not wrong, f"layers labelled fiber that are not curated as fiber: {wrong}"


def test_a_layer_that_declares_no_route_type_is_refused_not_labelled_fiber(monkeypatch, written):
    monkeypatch.setattr(kad, "PUBLIC_KMZ_SOURCES", [
        {"name": "declared", "url": GAS_LAYER, "type": "arcgis_kml",
         "provider": "EIA", "category": "gas", "route_type": "gas"},
        {"name": "undeclared", "url": POWER_LAYER, "type": "arcgis_kml",
         "provider": "HIFLD", "category": "power"},
    ])
    server = _Server()
    r = _engine(server)._process_known_sources()
    assert not [u for u in server.urls if u.startswith(POWER_LAYER)], server.urls
    assert _labels_by_layer(written) == {GAS_LAYER: {"gas"}}
    assert (r["queried"], r["failed"]) == (2, 1), r
    assert [s["name"] for s in r["failed_sources"]] == ["undeclared"], r
    assert "route_type" in r["failed_sources"][0]["error"], r
    # One refused layer beside a readable one is a partial cycle, not a failed one.
    assert kad._cycle_status({"known_sources": r}) == "partial"


def test_the_fetch_cannot_label_rows_by_default(written):
    server = _Server()
    with pytest.raises(TypeError):
        _engine(server)._fetch_arcgis_routes(POWER_LAYER, "HIFLD", "undeclared")
    assert server.urls == [] and written == []


def test_a_cycle_fetches_and_writes_no_discovered_layer(monkeypatch, written):
    monkeypatch.setattr(kad, "PUBLIC_KMZ_SOURCES", [
        {"name": "declared", "url": GAS_LAYER, "type": "arcgis_kml",
         "provider": "EIA", "category": "gas", "route_type": "gas"},
    ])
    server = _Server()
    inst = _engine(server)
    inst._discover_arcgis_sources = lambda: {"checked": 0, "new_sources": 0}
    inst._discover_state_broadband = lambda: {"checked": 0, "services_found": 0}
    results = inst.run_discovery_cycle()
    fetched_discovered = [u for u in server.urls
                          if any(u.startswith(row[2]) for row in DISCOVERED)]
    assert not fetched_discovered, fetched_discovered
    assert _labels_by_layer(written) == {GAS_LAYER: {"gas"}}
    assert results["total_new_routes"] == 1, results
