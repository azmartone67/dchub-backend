"""fiber_kmz_routes: the writer must name its conflict target (2026-08-22).

THE BUG: kmz_auto_discovery._fetch_arcgis_routes inserted with a BARE
`ON CONFLICT DO NOTHING` while the live table had no unique index except the
PK. That is legal SQL that never fires, so every one of the same 15,082
features was re-inserted every cycle — 12,296,960 rows / 10 GB by 2026-08-22
(~70k distinct identities) and a 2.4e9 km distance total. With a named target,
a missing index RAISES instead of silently inserting; the boot check in
init_tables says so in the log.
"""
import ast
import pathlib
import re
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
IDENTITY = "ON CONFLICT (source_url, kmz_file, md5(coordinates)) DO NOTHING"


def _src(rel):
    return (ROOT / rel).read_text()


def _strings(rel):
    tree = ast.parse(_src(rel))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_live_writer_names_the_identity_target():
    inserts = [s for s in _strings("kmz_auto_discovery.py") if "INSERT INTO fiber_kmz_routes" in s]
    assert inserts, "writer INSERT not found"
    for s in inserts:
        assert IDENTITY in s, "bare ON CONFLICT DO NOTHING never fires without a matching unique index"
        assert "source_url" in s, "identity needs source_url written"


def test_second_writer_shares_the_identity():
    inserts = [s for s in _strings("global_intelligence_agent.py") if "INSERT INTO fiber_kmz_routes" in s]
    assert inserts, "second writer INSERT not found"
    for s in inserts:
        assert IDENTITY in s
        assert "source_url" in s


def test_no_bare_on_conflict_on_fiber_kmz_routes_anywhere():
    bare = re.compile(r"INSERT INTO fiber_kmz_routes[\s\S]{0,600}?ON CONFLICT DO NOTHING")
    offenders = [p.name for p in ROOT.glob("*.py") if bare.search(p.read_text())]
    offenders += [str(p.relative_to(ROOT)) for p in (ROOT / "routes").glob("*.py") if bare.search(p.read_text())]
    assert not offenders, f"bare ON CONFLICT on fiber_kmz_routes in {offenders}"


class _Answer:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Layer:
    """Answers ?f=json with `metadata` and serves `n` features by
    resultOffset/resultRecordCount, the way an ArcGIS layer does."""

    def __init__(self, metadata, n):
        self.metadata = metadata
        self.features = [{"attributes": {"NAME": f"route {i}"},
                          "geometry": {"paths": [[[-90.0 - i * 1e-4, 30.0],
                                                  [-90.1 - i * 1e-4, 30.1]]]}}
                         for i in range(n)]
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if url.endswith("?f=json"):
            return _Answer(self.metadata)
        q = parse_qs(urlsplit(url).query)
        start, count = int(q["resultOffset"][0]), int(q["resultRecordCount"][0])
        return _Answer({"features": self.features[start:start + count]})


@pytest.mark.parametrize("metadata, field", [
    # The EIA gas and crude pipeline layers, measured 2026-09-13.
    ({"objectIdField": "FID", "fields": [{"name": "FID", "type": "esriFieldTypeOID"}]}, "FID"),
    # HIFLD transmission: the OID is OBJECTID_1; OBJECTID is an ordinary integer.
    ({"objectIdField": "OBJECTID_1",
      "fields": [{"name": "OBJECTID", "type": "esriFieldTypeInteger"},
                 {"name": "OBJECTID_1", "type": "esriFieldTypeOID"}]}, "OBJECTID_1"),
    # A map service layer publishes no objectIdField, only the OID-typed field.
    ({"fields": [{"name": "OBJECTID", "type": "esriFieldTypeInteger"},
                 {"name": "objectid_1", "type": "esriFieldTypeOID"}]}, "objectid_1"),
], ids=["feature-layer-FID", "decoy-integer-OBJECTID", "map-service-layer"])
def test_arcgis_paging_is_ordered_by_the_layers_object_id_field(monkeypatch, metadata, field):
    """Unordered resultOffset paging overlaps (682 duplicate features per cycle),
    so every page is ordered, by the layer's own object-id field read once. The
    literal OBJECTID sent from 2026-08-22 is not a field on the EIA gas and crude
    layers: every page answered HTTP 200 "'OBJECTID' parameter is invalid"."""
    import kmz_auto_discovery as kad

    written = []

    class _Conn:
        def cursor(self):
            return self

        def close(self):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

    def _execute_values(cur, sql, rows, page_size=None, fetch=False):
        written.extend(rows)
        return [(row[5],) for row in rows]  # RETURNING distance_km

    monkeypatch.setattr(kad, "_conn", lambda: _Conn())
    monkeypatch.setattr(kad, "_release", lambda conn: None)
    monkeypatch.setattr(kad, "execute_values", _execute_values)
    monkeypatch.setattr(kad.time, "sleep", lambda s: None)

    layer = _Layer(metadata, 2500)
    engine = object.__new__(kad.KMZAutoDiscovery)
    engine.session = layer
    r = engine._fetch_arcgis_routes("https://example.test/arcgis/rest/services/L/FeatureServer/0",
                                    "EIA", "layer", route_type="gas")

    assert "error" not in r, r
    pages = [u for u in layer.urls if "/query?" in u]
    assert layer.urls[0].endswith("?f=json"), layer.urls
    assert sum(u.endswith("?f=json") for u in layer.urls) == 1, layer.urls
    assert len(pages) == 3, pages
    assert [parse_qs(urlsplit(u).query).get("orderByFields") for u in pages] == [[field]] * 3, pages
    assert len(written) == 2500 and len({row[6] for row in written}) == 2500, len(written)


def test_insert_failure_is_not_debug_level():
    src = _src("kmz_auto_discovery.py")
    assert 'logger.debug(f"Batched route insert error' not in src
    assert 'logger.warning(f"Batched route insert error' in src
    assert 'note_swallowed_write("fiber_kmz_routes", where="kmz_auto_discovery._fetch_arcgis_routes")' in src


def test_init_tables_asserts_the_identity_index_but_does_not_create_it():
    src = _src("kmz_auto_discovery.py")
    assert "md5(coordinates)" in src
    creators = [s for s in _strings("kmz_auto_discovery.py")
                if re.search(r"CREATE UNIQUE INDEX\s+(IF NOT EXISTS\s+)?\w", s)]
    assert not creators, \
        "never create the unique index inside init_tables (aborts boot on duplicates)"
    assert "fiber_kmz_routes has NO identity UNIQUE index" in src
