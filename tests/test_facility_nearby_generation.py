"""Global generation context on the facility profile — the thin-page fix.

MEASURED CAUSE, 2026-08-31. The GSC seed showed DC Hub ranking on page one for
~1,000 non-branded queries across 39,071 impressions, earning 29 clicks. 982 of
995 queries earned ZERO. The queries are machine-shaped single-fact asks —
"coresite sv2 milpitas backup power mw", "digital realty ewr20 pue",
"intergate.west tukwila number of buildings" — and the pages contain none of
those facts.

It is NOT a template problem. A facility WITH data renders 463 words / 239
unique and a title carrying the grid ("Meta Hyperion — LA, US Data Center |
MISO grid"). One WITHOUT renders 240 / 136. The joins come back empty:

    power_mw > 0           33% of live facilities. 7,002 rows carry a
                           PLACEHOLDER 0 — counting those as data is the
                           flattering-zero trap, and it is why an earlier read
                           of this said 68%.
    substation_band        59% US, 0.5% international
    DCPI / ISO narrative   US markets only

So 13,303 international facilities — 66% of the corpus — fall through every
source and render a status line and coordinates. gem_power is the one
grid-adjacent table with real global reach: 182,428 units, 226 countries, all
geocoded, behind the existing ix_gempow_bbox index (0.27 ms for a 50 km box).

These tests run the real function against a stubbed DB.
"""

import ast
import sys
import types
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "facility_profile_page.py"
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)

SANTIAGO = [("utility-scale solar", 80, 1561.0), ("oil/gas", 3, 481.0),
            ("hydropower", 3, 396.0), ("bioenergy", 1, 14.0)]


class _Cur:
    def __init__(self, rows, boom=False):
        self.rows, self.boom = rows, boom
        self.sql = self.params = None

    def execute(self, q, p=None):
        self.sql, self.params = q, p
        if self.boom:
            raise RuntimeError("db down")

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows, boom=False):
        self.c = _Cur(rows, boom)
        self.closed = False

    def cursor(self):
        return self.c

    def close(self):
        self.closed = True


def _load(rows, boom=False, conn_none=False):
    conn = None if conn_none else _Conn(rows, boom)
    fake = types.ModuleType("main")
    fake.get_read_db = lambda: conn
    sys.modules["main"] = fake
    ns = {"_esc": lambda x: str(x),
          "logger": types.SimpleNamespace(warning=lambda *a, **k: None)}
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_nearby_generation_html":
            exec(compile(ast.Module(body=[n], type_ignores=[]),  # noqa: S102
                         str(SRC), "exec"), ns)
            return ns["_nearby_generation_html"], conn
    raise AssertionError("_nearby_generation_html not found")


# ── it renders real, specific content ────────────────────────────────

def test_renders_the_facts_the_zero_click_queries_ask_for():
    f, _ = _load(SANTIAGO)
    out = f(-33.4489, -70.6693, "Santiago", "CL")
    assert "Power generation nearby" in out
    assert "87 operating generating units" in out      # 80+3+3+1
    assert "2,452 MW" in out                            # 1561+481+396+14
    assert "Santiago, CL" in out
    for fuel in ("Utility-Scale Solar", "Oil/Gas", "Hydropower"):
        assert fuel in out


def test_shares_are_computed_not_asserted():
    f, _ = _load(SANTIAGO)
    out = f(-33.4489, -70.6693, "Santiago", "CL")
    assert "64%" in out, "solar is 1561/2452 = 64%"


def test_it_adds_substantive_word_count():
    """The whole point is turning a 240-word page into one worth clicking."""
    import re
    f, _ = _load(SANTIAGO)
    out = f(-33.4489, -70.6693, "Santiago", "CL")
    words = re.sub(r"<[^>]+>", " ", out).split()
    assert len(words) >= 50, f"only {len(words)} words added"


def test_singular_plural_is_handled():
    f, _ = _load([("nuclear", 1, 900.0)])
    out = f(40.0, -74.0, "X", "US")
    assert "1 operating generating unit " in out or "1 operating generating unit&" in out
    assert "units" not in out.split("totalling")[0]


# ── the bounding box ─────────────────────────────────────────────────

def test_uses_an_indexable_bounding_box_not_a_distance_formula():
    """A great-circle distance per row cannot use ix_gempow_bbox. The measured
    0.27 ms depends on staying a BETWEEN on lat/lng."""
    src = ast.get_source_segment(TEXT, next(
        n for n in TREE.body
        if isinstance(n, ast.FunctionDef) and n.name == "_nearby_generation_html"))
    assert "lat BETWEEN" in src and "lng BETWEEN" in src
    for banned in ("earth_distance", "ll_to_earth", "ST_", "acos(", "haversine"):
        assert banned not in src, f"{banned} would defeat the bbox index"


def test_longitude_span_is_latitude_corrected():
    """A fixed degree span becomes a globe-spanning box near the poles."""
    f, conn = _load(SANTIAGO)
    f(-33.4489, -70.6693, "Santiago", "CL")
    lo_lat, hi_lat, lo_lng, hi_lng = conn.c.params
    lat_span, lng_span = hi_lat - lo_lat, hi_lng - lo_lng
    assert lng_span > lat_span, "longitude must widen away from the equator"
    assert lat_span == pytest.approx(2 * 50.0 / 111.0, rel=0.01)


def test_polar_latitude_does_not_produce_an_infinite_box():
    f, conn = _load(SANTIAGO)
    f(89.999, 0.0, "North", "XX")
    lo_lat, hi_lat, lo_lng, hi_lng = conn.c.params
    assert (hi_lng - lo_lng) < 180, "cos() collapse must be floored"


def test_only_operating_units_are_counted():
    """Planned and cancelled units are in this table too — counting them would
    overstate available generation, which is the opposite of useful."""
    f, conn = _load(SANTIAGO)
    f(-33.4489, -70.6693, "Santiago", "CL")
    assert "ILIKE 'oper%%'" in conn.c.sql or "ILIKE 'oper%'" in conn.c.sql


# ── it stays quiet when it has nothing true to say ───────────────────

@pytest.mark.parametrize("lat,lng,why", [
    (None, None, "no coordinates"),
    ("", "", "empty coordinates"),
    (0.0, 0.0, "null island — a bad geocode, not a site"),
    (999, 999, "out of range"),
    (-33.4, 999, "one coordinate out of range"),
])
def test_bad_coordinates_render_nothing(lat, lng, why):
    f, _ = _load(SANTIAGO)
    assert f(lat, lng, "X", "Y") == "", why


def test_no_nearby_units_renders_nothing():
    """An empty section is worse than no section."""
    f, _ = _load([])
    assert f(-33.4, -70.6, "X", "Y") == ""


def test_zero_capacity_renders_nothing():
    f, _ = _load([("solar", 0, 0.0)])
    assert f(-33.4, -70.6, "X", "Y") == ""


def test_db_failure_renders_nothing_and_never_raises():
    f, _ = _load(SANTIAGO, boom=True)
    assert f(-33.4, -70.6, "X", "Y") == ""


def test_missing_connection_renders_nothing():
    f, _ = _load(SANTIAGO, conn_none=True)
    assert f(-33.4, -70.6, "X", "Y") == ""


def test_connection_is_closed_even_on_the_happy_path():
    f, conn = _load(SANTIAGO)
    f(-33.4489, -70.6693, "Santiago", "CL")
    assert conn.closed, "a leaked read connection saturates the pool"


# ── it is actually wired into the page ───────────────────────────────

def test_the_section_is_rendered_not_merely_defined():
    """A section nothing emits is the failure mode this whole audit kept
    finding."""
    assert "nearby_gen_html = _nearby_generation_html(" in TEXT
    assert "{nearby_gen_html}" in TEXT


def test_the_call_is_fail_soft_at_the_call_site_too():
    """_render_profile must not 500 a whole page over an optional section."""
    i = TEXT.find("nearby_gen_html = _nearby_generation_html(")
    window = TEXT[max(0, i - 300):i + 400]
    assert "try:" in window and "except Exception" in window
