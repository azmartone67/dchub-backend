"""The facility page and its JSON twin are the ANONYMOUS view of a location.

r-location-gate (2026-09-21, owner-approved). Exact facility location is
paid-only. /facilities/<slug> is edge-cached for 24 h and byte-identical for
every visitor, crawlers included, so nothing in it may state the exact point or
the house number — not the visible text, an attribute, an iframe, a link, the
JSON-LD or an inline script. It may state the street NAME, the city, region and
country, and a point rounded to 2 dp (~1.1 km). A signed-in visitor gets the
exact point client-side from /api/v1/facility/<slug>/location.

Measured on the renderer before this change, for a row stored at 6 dp with a
numbered street address: the point at 6 dp in the Place JSON-LD `geo`, at 4 dp
in a Coordinates tile, raw in a pinned map (`marker=`) and in three OSM links
(`mlat=`/`mlon=`), a bbox computed from the raw floats, and the house number
six times (facts line, Address tile, JSON-LD, and the three meta descriptions).

HOW, so that nothing here is a mirror of the code under test:
  * the real renderer on a hand-built row (main.py is never imported);
  * the real routes, through a Flask test client, with only their DB reads
    stubbed — so the order the route runs them in is what is tested;
  * the real withheld lookup against a fake cursor that answers by the SQL it
    is handed, and records it;
  * the real reveal script, run in node against a DOM built from the ids and
    attributes the rendered page defines.

Run:  python3 -m pytest tests/test_facility_page_location_gate.py -v
"""
import ast
import html as _html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import types

import pytest
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from util.facility_entity import LOCATION_WITHHELD, facility_entity  # noqa: E402
from util.facility_facts import street_address, street_name_only  # noqa: E402

PAGE = ROOT / "routes" / "facility_profile_page.py"
PAGE_SRC = PAGE.read_text(encoding="utf-8")
PAGE_TREE = ast.parse(PAGE_SRC)
NODE = shutil.which("node")

SLUG = "example-operator-quarry-ridge-campus-5e1f0c2b"
HOUSE = "4417"
STREET = "Quarry Ridge Road"
LAT, LNG = 39.781234, -89.650987            # 6 dp, the precision stored
ROW = {"id": 424242, "_src_table": "discovered_facilities",
       "name": "Quarry Ridge Campus", "provider": "Example Operator",
       "city": "Springfield", "state": "IL", "country": "US",
       "status": "Operational", "address": f"{HOUSE} {STREET}",
       "latitude": LAT, "longitude": LNG, "power_mw": 36.0,
       "canonical_slug": SLUG}
APPROX_SRC = ("https://www.openstreetmap.org/export/embed.html"
              "?bbox=-89.67,39.76,-89.63,39.80&amp;layer=mapnik")
APPROX_HREF = "https://www.openstreetmap.org/#map=14/39.78/-89.65"
WITHHELD = "Exact location withheld at the operator's request."
NOT_ON_RECORD = "Exact location not on record."
SIGN_IN = "Sign in free to see the exact location — 10 sites a month"

# More than two decimals on any number anywhere in the document. Measured: the
# page's own CSS and markup carry none, so every hit is a coordinate.
FINE_NUMBER = re.compile(r"\d+\.\d{3,}")


# ── util.facility_facts.street_name_only ────────────────────────────────────

# Invented addresses, one per shape the owner's brief lists, plus the shapes
# the stored address column is known to hold (tests/test_facility_facts.py).
STREET_NAME_CASES = [
    ("2500 MAPLE RD", "MAPLE RD"),                        # number first
    ("1725 Harbor St", "Harbor St"),
    ("Birch Hollow, 16A", "Birch Hollow"),                # number after a comma
    ("12-14 High Street", "High Street"),                 # a range
    ("Unit 5, 3 Mill Lane", "Mill Lane"),                 # a unit, then a number
    ("Suite 100, 21110 Meadow Circle", "Meadow Circle"),
    ("No. 8 Jalan Kenanga", "Jalan Kenanga"),             # "No." designator
    ("Rue des Lilas 12", "Rue des Lilas"),                # number last
    ("Plot 7", ""),                                       # nothing left
    ("Springfield", "Springfield"),                       # a bare city
    (None, ""),
    ("", ""),
    ("   ", ""),
    # postcodes, separately and inline
    ("705 Orchard Court, Springfield, IL, 62701", "Orchard Court"),
    ("Brunel Close, Newark, AB12 3CD, GB", "Brunel Close"),
    ("Brunel Close AB12 3CD", "Brunel Close"),
    ("62701-1234", ""),
    ("62701 Springfield", "Springfield"),
    # ranges spelled out, suffixes, glued marks
    ("4762 AND 4764 BAKERS FERRY RD, SW", "BAKERS FERRY RD"),
    ("12 & 14 Mill Lane", "Mill Lane"),
    ("12 bis Rue des Lilas", "Rue des Lilas"),
    ("12 A Main St", "Main St"),
    ("No.8 Jalan Kenanga", "Jalan Kenanga"),
    ("#200 Main St", "Main St"),
    ("Main St #200", "Main St"),
    ("Main St Ste 200", "Main St"),
    ("Main St, Suite B", "Main St"),
    ("3rd Floor, 12 Main St", "Main St"),
    ("PO Box 123, Springfield", "Springfield"),
    ("Jl. Kenanga Blok Bi No.1, Cikarang", "Jl. Kenanga"),
    ("No. 111 Lane 3111 West Orchard Rd., Springfield", "West Orchard Rd."),
    ("Hauptstrasse 5a", "Hauptstrasse"),
    ("Hauptstrasse 5 10115 Springfield", "Hauptstrasse"),
    ("Main Street 5", "Main Street"),
    ("Rua Augusta, 1500 - Centro", "Rua Augusta"),
    ("Km 12,5 Carretera Central", "Carretera Central"),
    ("1-2-3 Chuo", "Chuo"),
    ("中央1丁目2番3号", "中央"),
    ("東風路88号", "東風路"),
    # what must SURVIVE: directionals, ordinals, a route's own number
    ("350 E Main St", "E Main St"),
    ("12 E Main St", "E Main St"),
    ("111 8th Avenue", "8th Avenue"),
    ("1st St", "1st St"),
    ("20544 HIGHWAY 370", "HIGHWAY 370"),
    ("Calle 31", "Calle 31"),
    ("Calle 31 # 4-15", "Calle 31"),
    ("Carrera 7 No. 71-21", "Carrera 7"),
    ("Carretera 7 km 12", "Carretera 7"),
    ("State Route 28", "State Route 28"),
    ("Hall Road", "Hall Road"),
    ("Box Hill Road", "Box Hill Road"),
    ("1301 Avenue of the Americas", "Avenue of the Americas"),
    # a route number is never kept past a number in the middle
    ("Calle 31 12", ""),
    ("12 Street", ""),
]


@pytest.mark.parametrize("value,expected", STREET_NAME_CASES)
def test_street_name_only(value, expected):
    assert street_name_only(value) == expected


# A digit may survive only inside an ordinal or as a route's own number.
_SURVIVING_DIGITS = re.compile(r"\b\d{1,4}(?:st|nd|rd|th)\b|(?<=Calle )\d+"
                               r"|(?<=HIGHWAY )\d+|(?<=Carrera )\d+"
                               r"|(?<=Carretera )\d+|(?<=Route )\d+")


@pytest.mark.parametrize("value,expected", STREET_NAME_CASES)
def test_no_house_number_or_postcode_digit_survives(value, expected):
    """Every digit sequence of the input that is not an ordinal or a route's
    own number is gone — the property the table above exists to pin."""
    out = street_name_only(value)
    assert not re.search(r"\d", _SURVIVING_DIGITS.sub("", out)), out


def test_the_page_passes_only_validated_streets():
    """street_name_only never validates ("Springfield" comes back); the page
    asks street_address first, so a bare city is never printed as a street."""
    assert street_address("Springfield") == ""
    assert street_name_only(street_address("Springfield")) == ""
    assert street_name_only(street_address(f"{HOUSE} {STREET}")) == STREET


# ── rendering ───────────────────────────────────────────────────────────────

class _Cursor:
    """Answers by the SQL it is handed ((substring, rows) pairs, first match
    wins) and records every statement, so a test can see what ran."""

    def __init__(self, conn):
        self.conn = conn
        self._rows = None

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.conn.log.append((flat, params))
        self._rows = None
        for needle, rows in self.conn.answers:
            if needle in flat:
                if isinstance(rows, Exception):
                    raise rows
                self._rows = list(rows)
                return

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows or [])

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.log = []
        self.closed = False

    def cursor(self, *a, **k):
        return _Cursor(self)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def fpp(monkeypatch):
    """The page module with a DB-less `main` and no market lookup."""
    fake = types.ModuleType("main")
    fake.get_read_db = lambda: None
    fake.get_db = lambda: None
    monkeypatch.setitem(sys.modules, "main", fake)
    import routes.facility_profile_page as mod
    monkeypatch.setattr(mod, "_market_dcpi", lambda *a, **k: None)
    return mod


def _render(fpp, **over):
    fac = dict(ROW, **over)
    return fpp._render_profile(fac, fac["canonical_slug"])


def _ld_nodes(page):
    """Every JSON-LD block, parsed. A block that does not parse fails."""
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                        page, re.S)
    assert len(blocks) == 3, "Place, Dataset, BreadcrumbList"
    return [json.loads(b) for b in blocks]


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def _location(page):
    """The Location section alone: from its opening tag to the </div> that
    balances it."""
    start = page.index('<div class="section loc-section" id="location">')
    depth = 0
    for m in re.finditer(r"<(/?)div\b[^>]*>", page[start:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return page[start:start + m.end()]
    raise AssertionError("the Location section never closes")


def _assert_no_exact_location(page):
    """The anonymous-view contract, over the WHOLE document."""
    assert FINE_NUMBER.findall(page) == []
    for leak in ("39.781", "89.650", "89.651", "marker=", "mlat=", "mlon=",
                 "GeoCoordinates", '"geo"', "Coordinates"):
        assert leak not in page, leak
    assert not re.search(rf"\b{HOUSE}\b", page), "the house number"
    for node in _ld_nodes(page):
        keys = set(_walk_keys(node))
        assert not keys & {"geo", "latitude", "longitude"}, keys


def test_the_anonymous_page_states_no_exact_location(fpp):
    page = _render(fpp)
    _assert_no_exact_location(page)


def test_it_states_the_street_name_and_the_area(fpp):
    page = _render(fpp)
    loc = _location(page)
    assert ('<span class="loc-k">Street</span> '
            f'<span class="loc-v">{STREET}</span>') in loc
    assert ('<span class="loc-k">Area</span> '
            '<span class="loc-v">Springfield, IL, US</span>') in loc
    place = _ld_nodes(page)[0]
    assert place["@type"] == "Place"
    assert place["address"]["streetAddress"] == STREET
    assert place["address"]["addressLocality"] == "Springfield"
    desc = re.search(r'<meta name="description" content="([^"]*)"', page)
    assert f"at {STREET}, Springfield" in _html.unescape(desc.group(1))
    hero = page.split('<div class="hero">', 1)[1].split("</div>", 1)[0]
    assert f'<span class="fact-label">Street</span> {STREET}</li>' in hero


def test_the_map_is_an_approximate_area_with_no_marker(fpp):
    loc = _location(_render(fpp))
    frames = re.findall(r'<iframe\b[^>]*\bsrc="([^"]*)"', loc)
    assert frames == [APPROX_SRC]
    assert f'href="{APPROX_HREF}"' in loc
    assert "Approximate area (about 1 km)" in loc
    # the rounded point is the only point on the page
    assert re.findall(r"-?\d+\.\d+", _html.unescape(frames[0])) == [
        "-89.67", "39.76", "-89.63", "39.80"]


def test_the_reveal_widget_is_rendered_for_everyone_and_opens_for_no_one(fpp):
    loc = _location(_render(fpp))
    assert f'data-slug="{SLUG}"' in loc
    signin = re.search(r'<p class="loc-signin" id="loc-signin"><a class="link" '
                       r'rel="nofollow"\s+href="([^"]*)">([^<]*)</a></p>', loc)
    assert signin, "the signed-out offer is rendered, so it works without JS"
    assert signin.group(1) == f"/signup?next=/facilities/{SLUG}"
    assert _html.unescape(signin.group(2)) == SIGN_IN
    assert ('<button type="button" class="loc-reveal" id="loc-reveal" hidden>'
            '</button>') in loc
    assert ('<div class="loc-out" id="loc-out" aria-live="polite" hidden>'
            '</div>') in loc


def test_a_withheld_facility_shows_no_area_map_or_button(fpp):
    page = _render(fpp, _location_withheld=True)
    loc = _location(page)
    assert WITHHELD in loc
    for absent in ("openstreetmap", "<iframe", 'id="loc-exact"',
                   'id="loc-reveal"', "<button", "Sign in free", STREET,
                   "Approximate area"):
        assert absent not in page, absent
    for point in ("39.78", "89.65", "39.8", "89.6"):
        assert point not in page, point
    assert "Springfield, IL, US" in loc, "the area is still stated"
    assert "streetAddress" not in page
    _assert_no_exact_location(page)


def test_a_withheld_row_renders_nothing_derived_from_its_location(fpp):
    """The renderer serves a withheld row the way production stores it — no
    point, no address, and so no section computed from them — whatever the
    dict it is handed still carries. The caller's dict is left alone."""
    fac = dict(ROW, _location_withheld=True, substation_band="within 5 km",
               _nearby_gen=[("gas", 2, 480.0)], _fiber_carriers=["Carrier One"])
    before = dict(fac)
    page = fpp._render_profile(fac, SLUG)
    for absent in ("Power generation nearby", "Fiber connectivity",
                   "Carrier One", "within 5 km"):
        assert absent not in page, absent
    assert fac == before
    # the anchor: the same inputs, not withheld, do render those sections
    page = fpp._render_profile(dict(fac, _location_withheld=False), SLUG)
    assert "Power generation nearby" in page and "Carrier One" in page


def test_no_stored_coordinates_means_no_map(fpp):
    for lat, lng in ((None, None), (0.0, 0.0), (float("nan"), LNG),
                     ("x", LNG), (LAT, 200.0)):
        page = _render(fpp, latitude=lat, longitude=lng)
        loc = _location(page)
        assert NOT_ON_RECORD in loc, (lat, lng)
        assert "openstreetmap" not in page and 'id="loc-exact"' not in page
        assert STREET in loc, "the street name does not need coordinates"


def test_the_other_sections_still_render(fpp, monkeypatch):
    """The sections computed from the exact point server-side — nearby
    generation, fiber — and the market ones keep rendering, and none of them
    prints the point."""
    conn = _Conn([
        ("information_schema.columns", [(1,)]),
        ("WHERE id <> %s", [(7, "Neighbour Hall", "Other Operator", 12.0,
                             "other-operator-neighbour-hall-0a0b0c0d")]),
    ])
    monkeypatch.setattr(sys.modules["main"], "get_read_db", lambda: conn)
    monkeypatch.setattr(fpp, "_market_dcpi", lambda *a, **k: {
        "market_slug": "springfield-il", "market_name": "Springfield",
        "verdict": "BUILD", "iso": "MISO", "time_to_power_months": 18})
    import routes.market_deep_dive as mdd
    narrative = ("Springfield is a mid-sized market with steady absorption and "
                 "grid headroom on the MISO side. " * 6)
    monkeypatch.setattr(mdd, "read_deep_dive", lambda slug: {
        "key_stats": {"facility_count": 9}, "narrative_md": narrative})
    page = _render(fpp,
                   _nearby_gen=[("gas", 2, 480.0), ("solar", 3, 120.0)],
                   _fiber_carriers=["Carrier One", "Carrier Two"])
    for section in ("<h2>Fiber connectivity</h2>", "Carrier One",
                    "<h2>Power generation nearby</h2>", "480 MW",
                    "<h2>Market intelligence</h2>", "<h2>Market context</h2>",
                    "<h2>Other data centers nearby</h2>", "Neighbour Hall",
                    "<h2>Location</h2>"):
        assert section in page, section
    _assert_no_exact_location(page)


def test_the_renderer_opens_no_connection_for_the_location(fpp, monkeypatch):
    """The withheld lookup belongs to the route (the _nearby_gen rule): a
    render must not reach the redaction functions."""
    conn = _Conn()
    monkeypatch.setattr(sys.modules["main"], "get_read_db", lambda: conn)
    _render(fpp)
    assert not [s for s, _p in conn.log if "facility_location" in s]


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(fpp, monkeypatch):
    """The real blueprint. Its DB reads are stubbed and RECORD what they were
    handed, so the order the route runs them in is observable."""
    seen = {}

    def nearby(lat, lng):
        seen["nearby"] = (lat, lng)
        return [("gas", 2, 480.0)]

    def fiber(fac):
        seen["fiber"] = (fac.get("latitude"), fac.get("longitude"))
        return ["Carrier One"]

    monkeypatch.setattr(fpp, "_fetch_facility_by_slug", lambda s: dict(ROW))
    monkeypatch.setattr(fpp, "_twin_redirect_target", lambda fac, slug: None)
    monkeypatch.setattr(fpp, "_nearby_generation_rows", nearby)
    monkeypatch.setattr(fpp, "_fiber_carrier_names", fiber)
    monkeypatch.setattr(fpp, "_facility_change_rows", lambda fac: [])
    app = Flask(__name__)
    app.register_blueprint(fpp.facility_profile_bp)
    c = app.test_client()
    c.seen = seen
    return c


def test_the_route_serves_the_anonymous_page(client, fpp, monkeypatch):
    monkeypatch.setattr(fpp, "_location_withheld", lambda fac: False)
    r = client.get(f"/facilities/{SLUG}")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    _assert_no_exact_location(page)
    assert APPROX_SRC in page
    assert client.seen["nearby"] == (LAT, LNG), \
        "the server-side sections still get the exact point"


def test_the_route_asks_about_withholding_before_anything_reads_the_point(
        client, fpp, monkeypatch):
    monkeypatch.setattr(fpp, "_location_withheld", lambda fac: True)
    r = client.get(f"/facilities/{SLUG}")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert WITHHELD in page
    assert client.seen["nearby"] == (None, None)
    assert client.seen["fiber"] == (None, None)
    assert STREET not in page and "openstreetmap" not in page
    _assert_no_exact_location(page)


def test_every_visitor_gets_the_same_bytes(client, fpp, monkeypatch):
    """The page is edge-cached as ONE copy: a signed-in visitor's cookie or
    header must not change a byte of it."""
    monkeypatch.setattr(fpp, "_location_withheld", lambda fac: False)
    anon = client.get(f"/facilities/{SLUG}").get_data()
    client.set_cookie("dchub_token", "tok-123")
    signed = client.get(f"/facilities/{SLUG}", headers={
        "Authorization": "Bearer tok-123", "X-API-Key": "dch_live_k"})
    assert signed.get_data() == anon
    assert "Cookie" not in signed.headers.get("Vary", "")
    assert signed.headers["Cache-Control"].startswith("public")


def test_a_failing_withheld_lookup_costs_nothing(client, fpp, monkeypatch):
    def boom(fac):
        raise RuntimeError("lookup exploded")
    monkeypatch.setattr(fpp, "_location_withheld", boom)
    r = client.get(f"/facilities/{SLUG}")
    assert r.status_code == 200
    assert APPROX_SRC in r.get_data(as_text=True)


def _twin(client, fpp, monkeypatch, withheld):
    monkeypatch.setattr(fpp, "_location_withheld", lambda fac: withheld)
    r = client.get(f"/facilities/{SLUG}.json")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/ld+json"
    return r.get_data(as_text=True), json.loads(r.get_data(as_text=True))


def test_the_twin_publishes_no_point_and_no_house_number(client, fpp,
                                                         monkeypatch):
    raw, body = _twin(client, fpp, monkeypatch, withheld=False)
    assert FINE_NUMBER.findall(raw) == []
    for leak in ("39.78", "89.65", "geo", "latitude", "longitude",
                 "GeoCoordinates"):
        assert leak not in raw, leak
    assert not re.search(rf"\b{HOUSE}\b", raw)
    place = body["spatialCoverage"]
    assert place["address"]["streetAddress"] == STREET
    assert place["address"]["addressLocality"] == "Springfield"
    assert "description" not in place


def test_the_twin_of_a_withheld_facility_says_so(client, fpp, monkeypatch):
    raw, body = _twin(client, fpp, monkeypatch, withheld=True)
    place = body["spatialCoverage"]
    assert "streetAddress" not in place["address"]
    assert STREET not in raw
    assert place["description"] == LOCATION_WITHHELD == WITHHELD


def test_the_twin_drops_the_street_of_a_fleet_row():
    ent = facility_entity(dict(ROW, power_mw=63000.0),
                          canonical_url="u", display_name="d")
    assert "streetAddress" not in ent["spatialCoverage"]["address"]


# ── the withheld lookup ─────────────────────────────────────────────────────

def _withheld_fn(monkeypatch, conn):
    """The SHIPPED _location_withheld, compiled out of the page source and run
    against `conn` (None for no pool)."""
    fake = types.ModuleType("main")
    fake.get_read_db = lambda: conn
    monkeypatch.setitem(sys.modules, "main", fake)
    fn = next(n for n in PAGE_TREE.body
              if isinstance(n, ast.FunctionDef) and n.name == "_location_withheld")
    consts = {}
    for n in PAGE_TREE.body:
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) in ("_WITHHOLD_ID_PREFIX",
                                           "_WITHHOLD_FN_PROBE")
                for t in n.targets):
            consts[n.targets[0].id] = ast.literal_eval(n.value)
    ns = dict(consts, logger=types.SimpleNamespace(warning=lambda *a, **k: None))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(PAGE), "exec"), ns)  # noqa: S102
    return ns["_location_withheld"]


FN_PRESENT = ("to_regprocedure", [(True,)])


@pytest.mark.parametrize("src,prefix,rid", [
    ("discovered_facilities", "df", 424242),
    ("facilities", "f", "osm_0123abcd"),
])
def test_a_registered_row_reads_as_withheld(monkeypatch, src, prefix, rid):
    conn = _Conn([FN_PRESENT, ("facility_location_is_redacted", [(True,)])])
    check = _withheld_fn(monkeypatch, conn)
    assert check({"_src_table": src, "id": rid}) is True
    (probe, _), (sql, params) = conn.log
    assert "facility_location_redaction_keys(text,text,text,text,text,text)" in probe
    assert "facility_location_is_redacted(text[])" in probe
    assert f"FROM {src} WHERE id = %s" in sql
    assert ("facility_location_redaction_keys(%s, id::text, canonical_slug, "
            "source, source_id, source_url)") in sql
    assert params == (prefix, rid)
    assert conn.closed


def test_an_unregistered_row_is_not_withheld(monkeypatch):
    conn = _Conn([FN_PRESENT, ("facility_location_is_redacted", [(False,)])])
    assert _withheld_fn(monkeypatch, conn)(dict(ROW)) is False


def test_missing_functions_are_never_called(monkeypatch):
    """The migration may not have run: the probe says so and nothing that
    names the functions is executed."""
    conn = _Conn([("to_regprocedure", [(False,)])])
    assert _withheld_fn(monkeypatch, conn)(dict(ROW)) is False
    assert len(conn.log) == 1 and conn.closed


@pytest.mark.parametrize("answers", [
    [("to_regprocedure", RuntimeError("permission denied"))],
    [FN_PRESENT, ("facility_location_is_redacted",
                  RuntimeError('column "source_id" does not exist'))],
    [FN_PRESENT],                         # the row is gone: no answer
    [("to_regprocedure", [(None,)])],
])
def test_every_failure_reads_as_not_withheld(monkeypatch, answers):
    conn = _Conn(answers)
    assert _withheld_fn(monkeypatch, conn)(dict(ROW)) is False
    assert conn.closed


def test_no_pool_and_unknown_rows_read_as_not_withheld(monkeypatch):
    assert _withheld_fn(monkeypatch, None)(dict(ROW)) is False
    conn = _Conn([FN_PRESENT, ("facility_location_is_redacted", [(True,)])])
    check = _withheld_fn(monkeypatch, conn)
    for fac in ({"_src_table": "carrier_facility_presence", "id": 1},
                {"_src_table": "discovered_facilities", "id": None},
                {"_src_table": "discovered_facilities", "id": "  "},
                {"id": 1}):
        assert check(fac) is False, fac
    assert conn.log == [], "no query for a row the registry cannot name"


# ── the reveal script, run ──────────────────────────────────────────────────

_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const violations = [];

function textNode(t) { return { nodeType: 3, textContent: String(t), parentNode: null }; }
function makeEl(tag, id, attrs, hidden) {
  const el = {
    nodeType: 1, tagName: String(tag).toUpperCase(), id: id || "",
    hidden: !!hidden, disabled: false, className: "", children: [],
    attrs: Object.assign({}, attrs || {}), listeners: {}, parentNode: null,
    get firstChild() { return this.children[0] || null; },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      c.parentNode = null; return c;
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    get textContent() { return this.children.map((c) => c.textContent).join(""); },
    set textContent(v) { this.children = []; if (v !== "" && v != null) this.appendChild(textNode(v)); },
    set innerHTML(v) { violations.push("innerHTML=" + v); },
    set outerHTML(v) { violations.push("outerHTML=" + v); },
    insertAdjacentHTML(p, v) { violations.push("insertAdjacentHTML=" + v); },
  };
  return el;
}
const els = {};
for (const [id, spec] of Object.entries(cfg.ids)) {
  els[id] = makeEl(spec.tag, id, spec.attrs, spec.hidden);
}
const storage = cfg.storage || {};
const queue = { GET: (cfg.get || []).slice(), POST: (cfg.post || []).slice() };
const requests = [];
const sandbox = {
  console: { log() {}, warn() {}, error() {} },
  URLSearchParams,
  document: {
    cookie: cfg.cookie || "",
    getElementById: (id) => (Object.prototype.hasOwnProperty.call(els, id) ? els[id] : null),
    createElement: (tag) => makeEl(tag),
    createTextNode: (t) => textNode(t),
    write: (v) => violations.push("document.write=" + v),
  },
  localStorage: {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null),
  },
  fetch: (url, opts) => {
    const o = opts || {};
    const method = o.method || "GET";
    requests.push({ url, method, headers: o.headers || {} });
    const next = queue[method].shift();
    if (next === undefined) return Promise.reject(new Error("unscripted " + method));
    if (next === "network") return Promise.reject(new TypeError("Failed to fetch"));
    return Promise.resolve({
      ok: next.status >= 200 && next.status < 300, status: next.status,
      json: () => Promise.resolve(next.body),
    });
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);

function serial(n) {
  if (n.nodeType === 3) return { text: n.textContent };
  return { tag: n.tagName, cls: n.className, attrs: n.attrs, text: n.textContent,
           children: n.children.map(serial) };
}
function snapshot() {
  const out = {};
  for (const [id, el] of Object.entries(els)) {
    out[id] = { hidden: el.hidden, disabled: el.disabled, text: el.textContent,
                tree: el.children.map(serial) };
  }
  return out;
}
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };

(async () => {
  const result = { snapshots: [], requests, violations, error: null };
  try {
    vm.runInContext(cfg.script, sandbox, { filename: "location-reveal.js" });
    await settle();
    result.snapshots.push(snapshot());
    for (let i = 0; i < (cfg.clicks || 0); i++) {
      for (const fn of (els["loc-reveal"].listeners.click || [])) fn({});
      await settle();
      result.snapshots.push(snapshot());
    }
  } catch (e) {
    result.error = String((e && e.stack) || e);
  }
  process.stdout.write(JSON.stringify(result));
})();
"""

_TAG = re.compile(r"<([a-z]+)\b([^>]*?)\sid=\"([^\"]+)\"([^>]*)>", re.S)
_ATTR = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:=\"([^\"]*)\")?")


def _widget(page):
    """(script, {id: {tag, attrs, hidden}}) for the Location section."""
    loc = _location(page)
    start = loc.index("<script>") + len("<script>")
    script = loc[start:loc.index("</script>", start)]
    ids = {}
    for m in _TAG.finditer(loc[:loc.index("<script>")]):
        attrs = {}
        for a in _ATTR.finditer(m.group(2) + " " + m.group(4)):
            attrs[a.group(1)] = _html.unescape(a.group(2) or "")
        ids[m.group(3)] = {"tag": m.group(1), "attrs": attrs,
                           "hidden": "hidden" in attrs}
    return script, ids


@pytest.fixture
def widget(fpp, tmp_path):
    script, ids = _widget(_render(fpp))

    def run(storage=None, cookie="", get=(), post=(), clicks=0):
        if NODE is None:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                pytest.fail("node is not on PATH in CI, so the reveal script "
                            "cannot run; a skip would pass it unrun")
            pytest.skip("node is not on PATH")
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({
            "script": script, "ids": ids, "storage": storage or {},
            "cookie": cookie, "get": list(get), "post": list(post),
            "clicks": clicks}))
        harness = tmp_path / "harness.cjs"
        harness.write_text(_HARNESS)
        proc = subprocess.run([NODE, str(harness), str(cfg)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = json.loads(proc.stdout)
        assert out["error"] is None, out["error"]
        assert out["violations"] == [], "API text inserted as markup"
        return out
    run.ids = ids
    run.script = script
    return run


def _ok(body):
    return {"status": 200, "body": body}


EXACT = {"status": "exact", "latitude": LAT, "longitude": LNG,
         "address": f"{HOUSE} {STREET}, Springfield, IL 62701"}
TOKEN = {"dchub_token": "tok-123"}
ALLOWANCE = {"limit": 7, "used": 4, "remaining": 3, "period": "month",
             "resets_at": "2026-10-01T00:00:00Z"}
API = f"/api/v1/facility/{SLUG}/location"


def _frames(snap):
    found = []

    def walk(nodes):
        for n in nodes:
            if n.get("tag") == "IFRAME":
                found.append(n["attrs"]["src"])
            walk(n.get("children", []))
    walk(snap["loc-out"]["tree"])
    return found


def test_the_widget_ids_the_script_reads_are_on_the_page(widget):
    for i in ("loc-exact", "loc-signin", "loc-reveal", "loc-note", "loc-out",
              "loc-map"):
        assert i in widget.ids, i
        assert f"'{i}'" in widget.script, i
    assert widget.ids["loc-exact"]["attrs"]["data-slug"] == SLUG


def test_the_script_hardcodes_no_limit_and_no_point():
    """The limit comes from the server's `allowance`; the anonymous offer's
    number lives in Python. Neither the limit nor any fine number is typed in
    the script."""
    (js,) = [ast.literal_eval(n.value) for n in PAGE_TREE.body
             if isinstance(n, ast.Assign)
             and getattr(n.targets[0], "id", "") == "_LOCATION_REVEAL_JS"]
    assert not re.search(r"(?<![\w.])10(?![\w.])", js)
    assert FINE_NUMBER.findall(js) == []
    assert "innerHTML" not in js


def test_signed_out_it_asks_nothing_and_offers_sign_in(widget):
    out = widget()
    snap = out["snapshots"][0]
    assert out["requests"] == []
    assert snap["loc-signin"]["hidden"] is False
    assert snap["loc-reveal"]["hidden"] and snap["loc-out"]["hidden"]
    assert snap["loc-map"]["hidden"] is False


def test_exact_renders_the_point_the_address_and_a_pinned_map(widget):
    out = widget(storage=TOKEN, get=[_ok(EXACT)])
    (req,) = out["requests"]
    assert (req["url"], req["method"]) == (API, "GET")
    assert req["headers"] == {"Authorization": "Bearer tok-123"}
    snap = out["snapshots"][0]
    assert snap["loc-out"]["hidden"] is False
    assert snap["loc-map"]["hidden"] is True, "the approximate view gives way"
    assert snap["loc-signin"]["hidden"] and snap["loc-reveal"]["hidden"]
    assert "39.7812, -89.6510" in snap["loc-out"]["text"]
    assert EXACT["address"] in snap["loc-out"]["text"]
    (src,) = _frames(snap)
    assert "marker=39.781234%2C-89.650987" in src
    assert src.startswith("https://www.openstreetmap.org/export/embed.html?")


def test_the_token_cookie_is_used_when_storage_is_empty(widget):
    out = widget(cookie="other=1; dchub_token=tok%2Dcookie", get=[_ok(EXACT)])
    assert out["requests"][0]["headers"] == {"Authorization": "Bearer tok-cookie"}


def test_an_api_key_alone_is_sent_as_x_api_key(widget):
    out = widget(storage={"dchub_api_key": "dch_live_k"}, get=[_ok(EXACT)])
    assert out["requests"][0]["headers"] == {"X-API-Key": "dch_live_k"}


def test_reveal_available_counts_from_the_allowance_then_reveals(widget):
    out = widget(storage=TOKEN,
                 get=[_ok({"status": "reveal_available", "allowance": ALLOWANCE})],
                 post=[_ok(dict(EXACT, allowance=dict(ALLOWANCE, remaining=2)))],
                 clicks=1)
    before, after = out["snapshots"]
    assert before["loc-signin"]["hidden"] is True, "a signed-in visitor"
    assert before["loc-reveal"]["hidden"] is False
    assert before["loc-reveal"]["text"] == \
        "Show exact location (3 of 7 left this month)"
    assert before["loc-out"]["hidden"] is True
    assert [r["method"] for r in out["requests"]] == ["GET", "POST"]
    assert out["requests"][1]["url"] == API
    assert out["requests"][1]["headers"] == {"Authorization": "Bearer tok-123"}
    assert after["loc-out"]["hidden"] is False
    assert after["loc-reveal"]["hidden"] is True
    assert "39.7812, -89.6510" in after["loc-out"]["text"]


def test_a_failed_reveal_leaves_the_button_to_try_again(widget):
    out = widget(storage=TOKEN,
                 get=[_ok({"status": "reveal_available", "allowance": ALLOWANCE})],
                 post=["network"], clicks=1)
    after = out["snapshots"][1]
    assert after["loc-reveal"]["hidden"] is False
    assert after["loc-reveal"]["disabled"] is False
    assert after["loc-note"]["hidden"] is True and after["loc-out"]["hidden"]


def test_limit_reached_names_the_limit_the_reset_and_the_plan(widget):
    out = widget(storage=TOKEN, get=[_ok({
        "status": "limit_reached", "allowance": dict(ALLOWANCE, remaining=0),
        "upgrade_url": "/pricing?plan=developer"})])
    note = out["snapshots"][0]["loc-note"]
    assert note["hidden"] is False
    assert note["text"] == (
        "You've used your 7 exact locations this month — resets Oct 1, "
        "2026. Developer plan ($49/mo) shows every exact location.")
    (link,) = [n for n in note["tree"] if n.get("tag") == "A"]
    assert link["attrs"]["href"] == "/pricing?plan=developer"
    assert out["snapshots"][0]["loc-reveal"]["hidden"] is True


def test_limit_reached_without_numbers_still_reads(widget):
    """No allowance in the answer: the message drops the numbers rather than
    printing a number the server did not send."""
    out = widget(storage=TOKEN, get=[_ok({"status": "limit_reached"})])
    assert out["snapshots"][0]["loc-note"]["text"] == (
        "You've used your exact locations this month. Developer plan "
        "($49/mo) shows every exact location.")


def test_an_off_site_upgrade_link_is_not_followed(widget):
    for bad in ("javascript:alert(1)", "//evil.example/x",
                "https://dchub.cloud.evil.example/"):
        out = widget(storage=TOKEN, get=[_ok({
            "status": "limit_reached", "allowance": ALLOWANCE,
            "upgrade_url": bad})])
        (link,) = [n for n in out["snapshots"][0]["loc-note"]["tree"]
                   if n.get("tag") == "A"]
        assert link["attrs"]["href"] == "/pricing", bad


def test_sign_in_falls_back_to_the_api_key_then_to_the_link(widget):
    out = widget(storage=dict(TOKEN, dchub_api_key="dch_live_k"),
                 get=[_ok({"status": "sign_in"}),
                      _ok({"status": "reveal_available", "allowance": ALLOWANCE})])
    assert [r["headers"] for r in out["requests"]] == [
        {"Authorization": "Bearer tok-123"}, {"X-API-Key": "dch_live_k"}]
    assert out["snapshots"][0]["loc-reveal"]["hidden"] is False
    out = widget(storage=TOKEN, get=[_ok({"status": "sign_in"})])
    snap = out["snapshots"][0]
    assert snap["loc-signin"]["hidden"] is False
    assert snap["loc-reveal"]["hidden"] is True


@pytest.mark.parametrize("status,text,map_hidden", [
    ("withheld", WITHHELD, True),
    ("unknown", NOT_ON_RECORD, False),
])
def test_withheld_and_unknown_say_so(widget, status, text, map_hidden):
    snap = widget(storage=TOKEN, get=[_ok({"status": status})])["snapshots"][0]
    assert snap["loc-note"]["text"] == text
    assert snap["loc-map"]["hidden"] is map_hidden
    assert snap["loc-reveal"]["hidden"] and snap["loc-out"]["hidden"]


@pytest.mark.parametrize("answer", ["network", {"status": 500, "body": {}},
                                    _ok({"status": "exact", "latitude": "x"})])
def test_an_error_leaves_the_approximate_view_quietly(widget, answer):
    snap = widget(storage=TOKEN, get=[answer])["snapshots"][0]
    assert snap["loc-map"]["hidden"] is False
    assert snap["loc-signin"]["hidden"] is True, "signed in, just unanswered"
    assert snap["loc-out"]["hidden"] is True
    assert snap["loc-note"]["hidden"] is True
    assert snap["loc-reveal"]["hidden"] is True


def test_a_refused_credential_reads_as_signed_out(widget):
    snap = widget(storage=TOKEN, get=[{"status": 401, "body": {}}])["snapshots"][0]
    assert snap["loc-signin"]["hidden"] is False


def test_api_text_is_inserted_as_text(widget):
    hostile = '<img src=x onerror="alert(1)">'
    out = widget(storage=TOKEN, get=[_ok(dict(EXACT, address=hostile))])
    tree = out["snapshots"][0]["loc-out"]["tree"]
    addr = [n for n in tree if n.get("cls") == "loc-exact-addr"]
    assert addr and addr[0]["children"] == [{"text": hostile}]
