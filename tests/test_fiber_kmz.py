"""tests/test_fiber_kmz.py — KMZ/KML → fiber route parsing (2026-09-03).

The white-glove ingest path. Shapes here were taken from a real 1.7MB carrier
export (1,664 placemarks / 619 LineStrings / 4 MultiGeometry, Esri-style
ExtendedData) and from Google Earth exports, both of which the ingester must
read without special-casing.

House rules: no DB, no network, never import main.py. Everything under test is
the real production parser from routes/fiber_kmz.

Run:  python3 -m pytest tests/test_fiber_kmz.py -v
"""
from __future__ import annotations

import ast
import io
import pathlib
import xml.etree.ElementTree as ET
import zipfile

import pytest

from routes.fiber_kmz import (NAME_MAX, SOURCE_ID_MAX, haversine_miles,
                              parse_bytes, parse_kml_bytes, parse_kmz_bytes,
                              route_uid, storage_keys)

ROOT = pathlib.Path(__file__).resolve().parent.parent

NS = 'xmlns="http://www.opengis.net/kml/2.2"'


def kml(body, ns=True):
    return (f'<kml {NS if ns else ""}><Document>{body}</Document></kml>').encode()


LINE = """
<Placemark><name>Metro Ring A</name>
  <ExtendedData><SchemaData schemaUrl="#S">
    <SimpleData name="Owner">US Signal</SimpleData>
    <SimpleData name="Type">Metro</SimpleData>
  </SchemaData></ExtendedData>
  <LineString><coordinates>
     -86.25,43.21,0 -86.20,43.23,0 -86.15,43.26,0
  </coordinates></LineString>
</Placemark>"""


# ─── the shapes real files come in ───────────────────────────────────────

def test_namespaced_kml_with_esri_extended_data():
    r = parse_kml_bytes(kml(LINE))
    assert len(r) == 1
    assert r[0]["name"] == "Metro Ring A"
    assert r[0]["provider"] == "US Signal"
    assert r[0]["route_type"] == "Metro"
    assert r[0]["vertices"] == 3


def test_un_namespaced_kml_parses_identically():
    """Plenty of hand-edited exports drop the default namespace."""
    a = parse_kml_bytes(kml(LINE))
    b = parse_kml_bytes(kml(LINE, ns=False))
    assert [x["upstream_uid"] for x in a] == [x["upstream_uid"] for x in b]


def test_google_earth_data_value_extended_data():
    body = """
    <Placemark><name>Lateral</name>
      <ExtendedData>
        <Data name="Provider"><value>Uniti</value></Data>
        <Data name="Type"><value>dark</value></Data>
      </ExtendedData>
      <LineString><coordinates>-90.1,29.9 -90.0,30.0</coordinates></LineString>
    </Placemark>"""
    r = parse_kml_bytes(kml(body))
    assert r[0]["provider"] == "Uniti"
    assert r[0]["route_type"] == "dark"


def test_multigeometry_becomes_one_route_per_linestring():
    """★ A MultiGeometry holds SEPARATE physical spans. Merging them into one
    route would produce a bounding box spanning ground the fibre never touches,
    which then matches viewports it has no business matching."""
    body = """
    <Placemark><name>Split Span</name><MultiGeometry>
      <LineString><coordinates>-86.3,43.2 -86.2,43.2</coordinates></LineString>
      <LineString><coordinates>-83.1,42.3 -83.0,42.3</coordinates></LineString>
    </MultiGeometry></Placemark>"""
    r = parse_kml_bytes(kml(body))
    assert len(r) == 2
    assert r[0]["max_lng"] < -86.0 and r[1]["min_lng"] > -84.0
    assert r[0]["upstream_uid"] != r[1]["upstream_uid"]


def test_kmz_zip_is_unwrapped():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.kml", kml(LINE))
    assert len(parse_kmz_bytes(buf.getvalue())) == 1


def test_parse_bytes_dispatches_on_content_not_filename():
    """Files round-tripped through Earth/Esri are routinely a .kml holding a
    zip, or a .kmz holding bare XML."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.kml", kml(LINE))
    assert len(parse_bytes(buf.getvalue())) == 1     # zip content
    assert len(parse_bytes(kml(LINE))) == 1          # xml content


# ─── what must be ignored or survived ────────────────────────────────────

@pytest.mark.parametrize("geom", [
    "<Point><coordinates>-86.2,43.2</coordinates></Point>",
    "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
    "-86.3,43.1 -86.2,43.1 -86.2,43.2</coordinates></LinearRing>"
    "</outerBoundaryIs></Polygon>",
])
def test_points_are_not_routes(geom):
    """A Point is a splice or a handhole, not a route. (A Polygon's LinearRing
    IS read — a ring is a closed path and carriers do export rings that way.)"""
    r = parse_kml_bytes(kml(f"<Placemark><name>x</name>{geom}</Placemark>"))
    assert all(x["vertices"] >= 2 for x in r)
    if "Point" in geom:
        assert r == []


def test_a_single_bad_placemark_does_not_sink_the_file():
    body = ("<Placemark><name>bad</name><LineString><coordinates>"
            "garbage,,, not-a-number</coordinates></LineString></Placemark>" + LINE)
    r = parse_kml_bytes(kml(body))
    assert len(r) == 1 and r[0]["name"] == "Metro Ring A"


def test_out_of_range_vertices_are_dropped_not_kept():
    """One bad vertex must not stretch a route's box across the planet — a
    route boxed to the whole world matches EVERY viewport."""
    body = ("<Placemark><name>x</name><LineString><coordinates>"
            "-86.25,43.21 999,999 -86.15,43.26"
            "</coordinates></LineString></Placemark>")
    r = parse_kml_bytes(kml(body))
    assert r[0]["vertices"] == 2
    assert r[0]["max_lng"] <= -86.0 and r[0]["max_lat"] <= 44.0


def test_a_one_vertex_line_is_not_a_route():
    body = ("<Placemark><name>x</name><LineString>"
            "<coordinates>-86.25,43.21</coordinates></LineString></Placemark>")
    assert parse_kml_bytes(kml(body)) == []


def test_unreadable_xml_raises_rather_than_returning_empty():
    """★ Returning [] for a file we could not read is the 'healthy, nothing
    new' lie that hid the fiber discovery lane for 73 days. An unreadable file
    must be loud."""
    with pytest.raises(ET.ParseError):
        parse_kml_bytes(b"<kml><Document><unclosed>")


# ─── attribution ─────────────────────────────────────────────────────────

def test_default_provider_fills_only_where_the_file_is_silent():
    body = LINE + """
    <Placemark><name>No owner</name>
      <LineString><coordinates>-84.5,42.7 -83.0,42.3</coordinates></LineString>
    </Placemark>"""
    r = parse_kml_bytes(kml(body), default_provider="Bluebird")
    assert r[0]["provider"] == "US Signal"   # file wins
    assert r[1]["provider"] == "Bluebird"    # default fills the gap


def test_unattributed_routes_are_marked_unknown_not_guessed():
    body = ("<Placemark><name>x</name><LineString><coordinates>"
            "-84.5,42.7 -83.0,42.3</coordinates></LineString></Placemark>")
    assert parse_kml_bytes(kml(body))[0]["provider"] == "Unknown"


def test_folder_name_is_used_when_a_placemark_has_no_name():
    body = ("<Folder><name>Muskegon Ring</name>"
            "<Placemark><LineString><coordinates>"
            "-86.25,43.21 -86.15,43.26</coordinates></LineString></Placemark>"
            "</Folder>")
    assert parse_kml_bytes(kml(body))[0]["name"] == "Muskegon Ring"


# ─── identity / idempotency ──────────────────────────────────────────────

def test_same_route_reparsed_yields_the_same_uid():
    a = parse_kml_bytes(kml(LINE))[0]["upstream_uid"]
    b = parse_kml_bytes(kml(LINE))[0]["upstream_uid"]
    assert a == b


def test_uid_survives_a_sub_metre_reexport_wobble():
    """A re-export that shifts the 6th decimal (~10cm) must not mint a second
    row for one physical line."""
    shifted = LINE.replace("-86.20,43.23", "-86.200001,43.230001")
    assert (parse_kml_bytes(kml(LINE))[0]["upstream_uid"]
            == parse_kml_bytes(kml(shifted))[0]["upstream_uid"])


def test_uid_changes_when_the_route_actually_moves():
    moved = LINE.replace("-86.20,43.23", "-86.40,43.55")
    assert (parse_kml_bytes(kml(LINE))[0]["upstream_uid"]
            != parse_kml_bytes(kml(moved))[0]["upstream_uid"])


def test_same_geometry_under_a_different_carrier_is_a_different_row():
    other = LINE.replace("US Signal", "Uniti")
    assert (parse_kml_bytes(kml(LINE))[0]["upstream_uid"]
            != parse_kml_bytes(kml(other))[0]["upstream_uid"])


def test_uid_does_not_depend_on_the_file_that_carried_it():
    """Identity is the asset's, not the crawl's — the rule set by
    migrations/2026-08-12_fiber_route_upstream_uid.sql."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("some-other-name.kml", kml(LINE))
    assert (parse_kmz_bytes(buf.getvalue())[0]["upstream_uid"]
            == parse_kml_bytes(kml(LINE))[0]["upstream_uid"])


# ─── geometry maths ──────────────────────────────────────────────────────

def test_bbox_covers_every_vertex():
    r = parse_kml_bytes(kml(LINE))[0]
    for lng, lat in r["coordinates"]:
        assert r["min_lng"] <= lng <= r["max_lng"]
        assert r["min_lat"] <= lat <= r["max_lat"]


def test_endpoints_are_the_first_and_last_vertex():
    r = parse_kml_bytes(kml(LINE))[0]
    assert [r["start_lng"], r["start_lat"]] == r["coordinates"][0]
    assert [r["end_lng"], r["end_lat"]] == r["coordinates"][-1]


def test_distance_is_a_real_great_circle_length():
    # Chicago -> Detroit is ~382 km / ~237 mi.
    d = haversine_miles([[-87.63, 41.88], [-83.05, 42.33]])
    assert 225 < d < 250, d
    assert haversine_miles([[0, 0]]) == 0.0
    assert haversine_miles([]) == 0.0


def test_altitude_is_dropped_not_read_as_a_coordinate():
    body = ("<Placemark><name>x</name><LineString><coordinates>"
            "-86.25,43.21,1200 -86.15,43.26,1400"
            "</coordinates></LineString></Placemark>")
    r = parse_kml_bytes(kml(body))[0]
    assert r["coordinates"] == [[-86.25, 43.21], [-86.15, 43.26]]


@pytest.mark.parametrize("sep", [" ", "\n", "\t", "\n  \t "])
def test_coordinate_whitespace_variants_all_parse(sep):
    body = ("<Placemark><name>x</name><LineString><coordinates>"
            f"-86.25,43.21{sep}-86.15,43.26"
            "</coordinates></LineString></Placemark>")
    assert parse_kml_bytes(kml(body))[0]["vertices"] == 2


# ─── the writer must name its conflict target ────────────────────────────

def _ingester_strings():
    tree = ast.parse((ROOT / "tools" / "ingest_fiber_kmz.py").read_text())
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_ingester_names_its_conflict_target():
    """★ A BARE `ON CONFLICT DO NOTHING` is legal SQL that never fires when no
    matching unique index exists — it inserts instead of erroring. That is how
    fiber_kmz_routes reached 12,296,960 rows / 10 GB over ~70k distinct
    identities (2026-08-22, tests/test_kmz_routes_identity_conflict.py).

    Naming the target — with the partial index's WHERE predicate, so Postgres
    can infer fiber_routes_upstream_uid_uniq — makes a missing index RAISE on
    the first row. Verified against a real Postgres both ways: with the index,
    a re-run inserts 0; without it, psycopg2 raises InvalidColumnReference.
    """
    inserts = [s for s in _ingester_strings() if "INSERT INTO fiber_routes" in s]
    assert inserts, "ingester INSERT not found"
    for s in inserts:
        assert "ON CONFLICT (source, upstream_uid)" in s, \
            "conflict target must be named, not bare"
        assert "WHERE upstream_uid IS NOT NULL" in s, \
            "partial-index predicate is required for Postgres to infer the index"


def test_ingester_defaults_to_dry_run():
    """Writing must be an explicit act. `--write` is opt-in so pointing the
    tool at a directory can never mutate the table by accident."""
    src = (ROOT / "tools" / "ingest_fiber_kmz.py").read_text()
    assert '"--write", action="store_true"' in src
    assert "DRY RUN" in src


# ─── storage keys: every unique index the table arbitrates on ────────────

UID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
UID2 = "ffffffffffffffffffffffffffffffff"


def test_two_placemarks_sharing_a_name_get_distinct_storage_names():
    """★ THE PRODUCTION FAILURE. fiber_routes carries UNIQUE(name, provider)
    (twice). Google Earth labels every unnamed placemark with its enclosing
    folder, so a single export yields hundreds of routes called
    'Temporary Places'. The first --write against production died on:

        duplicate key value violates unique constraint
        "fiber_routes_name_provider_key"
        DETAIL: Key (name, provider)=(Temporary Places, C3NTRO) already exists.
    """
    a, _ = storage_keys("Temporary Places", UID, "f.kmz")
    b, _ = storage_keys("Temporary Places", UID2, "f.kmz")
    assert a != b


def test_two_routes_in_one_file_get_distinct_source_ids():
    """★ UNIQUE(source_id) is BARE — not (source, source_id). Stamping the
    file's basename meant only ONE row per file could ever insert."""
    _, a = storage_keys("A", UID, "same-file.kmz")
    _, b = storage_keys("B", UID2, "same-file.kmz")
    assert a != b


def test_the_same_route_composes_the_same_keys_every_time():
    """Idempotency depends on this: re-running a file must reproduce the keys
    already stored, so ON CONFLICT can recognise them."""
    assert storage_keys("Ring", UID, "f.kmz") == storage_keys("Ring", UID, "f.kmz")


def test_the_origin_file_is_not_part_of_identity():
    """Identity is the asset's, not the crawl's. The same route re-exported
    under a new filename must still dedup — so the NAME (which pairs with
    provider in the unique index) must not vary with the filename."""
    a, _ = storage_keys("Ring", UID, "export-january.kmz")
    b, _ = storage_keys("Ring", UID, "export-june.kmz")
    assert a == b


@pytest.mark.parametrize("name,origin", [
    ("X" * 400, "Y" * 400),
    ("", ""),
    ("Ring", "Z" * 5000),
    ("N" * 199, "f.kmz"),
])
def test_long_values_never_truncate_away_the_fingerprint(name, origin):
    """★ Clipping the COMPOSED string would silently drop the suffix on long
    names and reintroduce the collision. The human part is what gets trimmed."""
    n, sid = storage_keys(name, UID, origin)
    assert len(n) <= NAME_MAX
    assert len(sid) <= SOURCE_ID_MAX
    assert n.endswith("#" + UID[:8]), n
    assert UID in sid, sid
    # and two different routes still differ at these lengths
    n2, sid2 = storage_keys(name, UID2, origin)
    assert n != n2 and sid != sid2
