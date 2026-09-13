"""kmz_auto_discovery: an ArcGIS error body is a failure, not an empty layer.

Measured 2026-09-13: 53 of the 58 PUBLIC_KMZ_SOURCES were dead. 38 ArcGIS
services answered HTTP 200 with {"error": {"code": 400, "message": "Invalid
URL"}}, 13 named a host with no DNS address record, one answered "Service not
found" and one refused /query with HTTP 403. _fetch_arcgis_routes checked only
`status_code != 200` and then read `data.get('features', [])`, so every one of
them looked like a layer with nothing in it: zero routes, nothing logged above
DEBUG, and a kmz_discovery_log row that said 'success'.

These tests EXECUTE the module's methods against a scripted HTTP session and a
recording database stub. Nothing here touches the network or a database.
"""
import json
import os
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import kmz_auto_discovery as kad  # noqa: E402

LAYER = ("https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/"
         "Electric_Substations/FeatureServer/0")
INVALID_URL = {"error": {"code": 400, "message": "Invalid URL", "details": ["Invalid URL"]}}
BAD_ORDER_FIELD = {"error": {"code": 400,
                             "message": "Cannot perform query. Invalid query parameters.",
                             "details": ["'OBJECTID' parameter is invalid"]}}

# Every source removed on 2026-09-13, each measured dead that day (see the note
# above PUBLIC_KMZ_SOURCES for what each class answered).
MEASURED_DEAD_2026_09_13 = (
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Submarine_Cable_Landing_Points/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NTIA_BIP_Round_1_Middle_Mile/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NTIA_BIP_Round_1_Last_Mile/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/BEAD_Eligible_Locations/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/BEAD_Challenge_Results/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NTIA_BIP_Round_2_Middle_Mile/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Tribal_Broadband_Connectivity/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Fixed_Broadband_Deployment/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Broadband_Funding/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USDA_ReConnect_Funded_Areas/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Submarine_Cables/FeatureServer/0',
    'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Zayo_Network/FeatureServer/0',
    'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Crown_Castle_Fiber/FeatureServer/0',
    'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Lumen_Fiber/FeatureServer/0',
    'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/Windstream_Fiber/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Liquefied_Natural_Gas_Import_Export_Terminals/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Storage/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Underground_Storage_1/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Water_Treatment_Plants/FeatureServer/0',
    'https://services.arcgis.com/bMDHnT5gHwXJ62Xo/arcgis/rest/services/NBN_Fixed_Line_Footprint/FeatureServer/0',
    'https://services.arcgis.com/G1JXlRDy3Sp3D6SO/arcgis/rest/services/Canada_Broadband_Internet/FeatureServer/0',
    'https://services.arcgis.com/dLMuXcEHPBYXdzOo/arcgis/rest/services/Openreach_Fibre/FeatureServer/0',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/communications/FeatureServer/5',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/communications/FeatureServer/1',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/communications/FeatureServer/10',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/4',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/10',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/16',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/17',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/32',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/33',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/36',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/38',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/5',
    'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/11',
    'https://gis.harnett.org/arcgis/rest/services/Public_Utilities/Fiber/FeatureServer/1',
    'https://hub.arcgis.com/api/v3/datasets/aef932d6b3fd4f0994ef672368b09217_0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Petroleum_Pipelines/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Pipelines/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Planning_Areas/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Control_Areas/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/NERC_Regions/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Railroads/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Major_Dams/FeatureServer/0',
    'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Wastewater_Treatment_Plants/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Electric_Retail_Service_Territories_2/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Electric_Planning_Areas_1/FeatureServer/0',
    'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Coal_Mines_1/FeatureServer/0',
    'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Flood_Hazard_Reduced_Set/FeatureServer/0',
    'https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/Superfund_National_Priorities_List/FeatureServer/0',
    'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Opportunity_Zone_Tract/FeatureServer/0',
)


class _Response:
    """The two parts of requests.Response the fetch reads."""

    def __init__(self, status_code=200, data=None, raw=None):
        self.status_code = status_code
        self._data = data
        self._raw = raw

    def json(self):
        if self._raw is not None:
            # requests raises its JSONDecodeError, a json.JSONDecodeError subclass.
            raise json.JSONDecodeError("Expecting value", self._raw, 0)
        return self._data


class _Session:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        assert self._answers, f"unscripted request: {url}"
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


# The ?f=json answer every fetch reads first, for a layer whose field is OBJECTID.
META = _Response(200, {"objectIdField": "OBJECTID",
                       "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}]})


def _engine(session=None):
    inst = object.__new__(kad.KMZAutoDiscovery)
    inst.session = session
    inst._cache = {"last_cycle": None, "total_routes_discovered": 0, "total_kmz_processed": 0}
    inst._scheduler_running = False
    inst._cycle_in_progress = False
    return inst


def _feature(i):
    lng = -77.40 - i * 1e-3
    return {"attributes": {"NAME": f"line {i}", "OBJECTID": i},
            "geometry": {"paths": [[[lng, 39.00], [lng + 0.1, 39.05]]]}}


def _fetch(session):
    return _engine(session)._fetch_arcgis_routes(LAYER, "HIFLD", "HIFLD Electric Substations")


@pytest.fixture
def no_db(monkeypatch):
    """A fetch with nothing to write must never open a connection."""
    def _refuse():
        raise AssertionError("opened a database connection with nothing to write")
    monkeypatch.setattr(kad, "_conn", _refuse)
    monkeypatch.setattr(kad.time, "sleep", lambda s: None)


@pytest.fixture
def recording_db(monkeypatch):
    """execute_values replaced by a recorder that inserts every row it is given."""
    written = []

    class _Cur:
        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

    def _execute_values(cur, sql, rows, page_size=None, fetch=False):
        assert "INSERT INTO fiber_kmz_routes" in sql, sql
        written.extend(rows)
        return [(row[5],) for row in rows]  # RETURNING distance_km

    monkeypatch.setattr(kad, "_conn", lambda: _Conn())
    monkeypatch.setattr(kad, "_release", lambda conn: None)
    monkeypatch.setattr(kad, "execute_values", _execute_values)
    monkeypatch.setattr(kad.time, "sleep", lambda s: None)
    return written


# ── _fetch_arcgis_routes ─────────────────────────────────────────────────────

def test_an_error_body_on_http_200_is_reported_not_read_as_an_empty_layer(no_db):
    session = _Session(META, _Response(200, INVALID_URL))
    r = _fetch(session)
    assert len(session.urls) == 2 and "/query?" in session.urls[1], session.urls
    assert r["routes_found"] == 0, r
    assert "error" in r, f"an ArcGIS error body was read as a layer with no features: {r}"
    assert "Invalid URL" in r["error"], r


def test_the_error_names_the_parameter_arcgis_rejected(no_db):
    r = _fetch(_Session(META, _Response(200, BAD_ORDER_FIELD)))
    assert "'OBJECTID' parameter is invalid" in r.get("error", ""), r


def test_a_layer_that_really_is_empty_is_not_an_error(no_db):
    r = _fetch(_Session(META, _Response(200, {"features": []})))
    assert r == {"routes_found": 0, "total_km": 0}, r


@pytest.mark.parametrize("answer, expected", [
    (_Response(500, {"error": "boom"}), "HTTP 500"),
    (_Response(200, raw="<html>"), "not JSON"),
    (_Response(200, {"count": 52244}), "without a features list"),
    (requests.exceptions.ConnectionError("Failed to resolve maps.nccs.nasa.gov"), "ConnectionError"),
], ids=["http-500", "non-json-body", "no-features-list", "transport-failure"])
def test_every_other_unreadable_answer_is_an_error(no_db, answer, expected):
    r = _fetch(_Session(META, answer))
    assert r["routes_found"] == 0, r
    assert expected in r.get("error", ""), r


def test_a_readable_page_is_written_and_is_not_an_error(recording_db):
    r = _fetch(_Session(META, _Response(200, {"features": [_feature(1), _feature(2)]})))
    assert "error" not in r, r
    assert r["routes_found"] == 2 and len(recording_db) == 2, (r, len(recording_db))


def test_an_error_on_a_later_page_keeps_the_rows_already_read(recording_db):
    first = _Response(200, {"features": [_feature(i) for i in range(1000)]})
    session = _Session(META, first, _Response(200, INVALID_URL))
    r = _fetch(session)
    assert len(session.urls) == 3, session.urls
    assert r["routes_found"] == 1000 and len(recording_db) == 1000, r
    assert "Invalid URL" in r.get("error", ""), r


def test_a_failure_outside_the_page_loop_is_an_error(monkeypatch):
    """The page was read; opening the connection to write it failed. That
    escapes the page loop and used to be logged at DEBUG and returned as zero
    routes with no error."""
    def _pool_exhausted():
        raise RuntimeError("connection pool exhausted")

    monkeypatch.setattr(kad, "_conn", _pool_exhausted)
    monkeypatch.setattr(kad.time, "sleep", lambda s: None)
    r = _fetch(_Session(META, _Response(200, {"features": [_feature(1)]})))
    assert r["routes_found"] == 0, r
    assert "RuntimeError" in r.get("error", ""), r


# ── the layer's object-id field ──────────────────────────────────────────────

@pytest.mark.parametrize("answer, expected", [
    (_Response(200, INVALID_URL), "Invalid URL"),
    (_Response(500, None), "HTTP 500"),
    (_Response(200, raw="<html>"), "not JSON"),
    (_Response(200, {"currentVersion": 11.3, "layers": [{"id": 0}]}), "names no object-id field"),
    (_Response(200, {"fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                                {"name": "FID", "type": "esriFieldTypeOID"}]}), "2 esriFieldTypeOID"),
    (requests.exceptions.ReadTimeout("read timed out"), "ReadTimeout"),
], ids=["deleted-service", "http-500", "non-json", "service-root-not-a-layer",
        "two-oid-fields", "transport-failure"])
def test_metadata_that_names_no_object_id_field_fails_before_any_page(no_db, answer, expected):
    """Paging without an order overlaps, so a layer whose object-id field cannot
    be read is a failure: no page is requested and the reason is reported."""
    session = _Session(answer)
    r = _fetch(session)
    assert session.urls == [LAYER + "?f=json"], session.urls
    assert r["routes_found"] == 0, r
    assert "metadata" in r.get("error", "") and expected in r["error"], r


def test_the_object_id_field_is_sent_encoded_not_spliced_into_the_query(recording_db):
    """The field name comes from a third party's metadata."""
    session = _Session(_Response(200, {"objectIdField": "OID&where=0=1"}),
                       _Response(200, {"features": [_feature(1)]}))
    r = _fetch(session)
    q = parse_qs(urlsplit(session.urls[1]).query)
    assert q["orderByFields"] == ["OID&where=0=1"] and q["where"] == ["1=1"], q
    assert r["routes_found"] == 1, r


# ── _process_known_sources ───────────────────────────────────────────────────

def test_known_sources_count_and_name_the_layers_they_could_not_read(monkeypatch, recording_db):
    monkeypatch.setattr(kad, "PUBLIC_KMZ_SOURCES", [
        {"name": "live layer", "url": "https://example.test/live/FeatureServer/0",
         "type": "arcgis_kml", "provider": "P", "category": "gas"},
        {"name": "dead layer", "url": "https://example.test/dead/FeatureServer/0",
         "type": "arcgis_kml", "provider": "P", "category": "power"},
        {"name": "not a layer", "url": "https://example.test/api",
         "type": "api_discover", "provider": "P", "category": "federal"},
    ])
    # The dead layer fails at its metadata read, as a deleted service does.
    session = _Session(META, _Response(200, {"features": [_feature(1)]}),
                       _Response(200, INVALID_URL))
    inst = _engine(session)
    inst._add_discovered_source = lambda source: False
    r = inst._process_known_sources()
    assert len(session.urls) == 3, session.urls
    assert (r["checked"], r["queried"], r["failed"]) == (3, 2, 1), r
    assert [s["name"] for s in r["failed_sources"]] == ["dead layer"], r
    assert "Invalid URL" in r["failed_sources"][0]["error"], r
    assert r["routes_found"] == 1, r


# ── _export_arcgis_as_kml ────────────────────────────────────────────────────

def test_export_marks_an_unreadable_discovered_source_error_not_empty(monkeypatch):
    rows = [(7, "dead search hit", "https://example.test/dead/FeatureServer", "someone"),
            (8, "empty layer", "https://example.test/empty/FeatureServer/0", "someone"),
            (9, "live layer", "https://example.test/live/FeatureServer/0", "someone")]
    fetched = {
        rows[0][2]: {"routes_found": 0, "total_km": 0,
                     "error": "HTTP 200 with an error body: 400 Invalid URL"},
        rows[1][2]: {"routes_found": 0, "total_km": 0},
        rows[2][2]: {"routes_found": 4, "total_km": 9.0},
    }

    class _Cur:
        def execute(self, sql, params=None):
            assert "FROM kmz_discovered_sources" in sql, sql

        def fetchall(self):
            return rows

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(kad, "_conn", lambda: _Conn())
    monkeypatch.setattr(kad, "_release", lambda conn: None)
    monkeypatch.setattr(kad.time, "sleep", lambda s: None)
    inst = _engine()
    inst._fetch_arcgis_routes = lambda url, provider, name, route_type="fiber": fetched[url]
    statuses = []
    inst._update_source_status = lambda sid, status, n: statuses.append((sid, status, n))
    r = inst._export_arcgis_as_kml()
    assert statuses == [(7, "error", 0), (8, "empty", 0), (9, "active", 4)], statuses
    assert (r["errors"], r["exported"], r["routes_parsed"]) == (1, 1, 4), r


# ── the cycle row ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("known, other, expected", [
    # 'ok', never 'success': every row written before 2026-09-13 says 'success'.
    ({"queried": 4, "failed": 0}, {}, "ok"),
    ({"queried": 4, "failed": 2}, {}, "partial"),
    ({"queried": 4, "failed": 4}, {}, "failed"),
    ({"checked": 0, "routes_found": 0, "total_km": 0, "error": "boom"}, {}, "failed"),
    ({"queried": 4, "failed": 0}, {"arcgis_kml_export": {"error": "boom"}}, "partial"),
    ({"queried": 0, "failed": 0}, {}, "ok"),
], ids=["none-failed", "some-failed", "all-failed", "stage-raised",
        "other-stage-raised", "nothing-queried"])
def test_cycle_status(known, other, expected):
    # errors=3 in the export stage: unreadable DISCOVERED sources never set it.
    results = {"known_sources": known, "arcgis_search": {}, "state_broadband": {},
               "arcgis_kml_export": {"errors": 3}}
    results.update(other)
    assert kad._cycle_status(results) == expected


def test_log_cycle_writes_the_computed_status_not_a_literal_success(monkeypatch):
    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    monkeypatch.setattr(kad, "_conn", lambda: _Conn())
    monkeypatch.setattr(kad, "_release", lambda conn: None)
    _engine()._log_cycle({
        "known_sources": {"queried": 4, "failed": 4,
                          "failed_sources": [{"name": "x", "error": "400 Invalid URL"}]},
        "total_new_routes": 0, "total_new_km": 0})
    inserts = [params for sql, params in executed
               if sql.startswith("INSERT INTO kmz_discovery_log")]
    assert len(inserts) == 1, executed
    assert inserts[0][0] == "auto_discovery_cycle", inserts
    assert inserts[0][5] == "failed", f"the cycle row's status is not the computed one: {inserts}"


def test_status_serves_the_cycle_rows_status_from_the_watermark_read(monkeypatch):
    log = []

    class _Cur:
        def __init__(self):
            self._row = None

        def execute(self, sql, params=None):
            q = " ".join(sql.split()).lower()
            log.append(q)
            if "from kmz_discovery_log" in q:
                # A real cursor returns exactly the columns the SELECT names.
                cols = q.split(" from ")[0]
                self._row = (("2026-09-13 00:29:14.179393+00", "partial")
                             if "status" in cols else ("2026-09-13 00:29:14.179393+00",))
            else:
                self._row = (0,)

        def fetchone(self):
            return self._row

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(kad, "_conn", lambda: _Conn())
    monkeypatch.setattr(kad, "_release", lambda conn: None)
    st = _engine().get_status()
    assert st["last_cycle_at"] == "2026-09-13T00:29:14.179393+00:00", st
    assert st.get("last_cycle_status") == "partial", st
    assert sum("kmz_discovery_log" in q for q in log) == 1, (
        "the cycle status must come from the same single-row watermark read", log)


# ── the list itself ──────────────────────────────────────────────────────────

def test_sources_measured_dead_on_2026_09_13_stay_out():
    assert len(MEASURED_DEAD_2026_09_13) == 53
    queried = [s for s in kad.PUBLIC_KMZ_SOURCES if s["type"] == "arcgis_kml"]
    assert queried, "no ArcGIS sources left: this check would pass over nothing"
    back = sorted({s["url"] for s in kad.PUBLIC_KMZ_SOURCES} & set(MEASURED_DEAD_2026_09_13))
    assert not back, f"sources measured dead on 2026-09-13 are back in PUBLIC_KMZ_SOURCES: {back}"
