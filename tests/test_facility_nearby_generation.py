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


@pytest.fixture(autouse=True)
def _restore_main_module():
    """Put sys.modules["main"] back after every test in this file.

    ★ THIS FIXTURE IS THE BUG FIX, not housekeeping. Without it, _load()
    installs a fake `main` whose get_read_db() returns a connection whose
    cursor has execute/fetchall but NO fetchone, and whose fetchall() returns
    3-tuples. That fake then leaks into every test that runs AFTER this file.

    pytest walks tests/ alphabetically, so test_fac... lands ahead of
    test_fro... and test_seo... — and ten tests in
    test_frozen_slug_canonical_select.py and test_seo_index_hygiene.py died on

        ValueError: not enough values to unpack (expected 5, got 3)

    in _comparables_html, which unpacks 5 columns and was being handed this
    file's 3-tuples. The companion tell in the CI log was
    "'_Cur' object has no attribute 'fetchone'".

    It reproduced only with the whole suite loaded, which sent me chasing a
    phantom: I first blamed a DB call inside the renderer and rewrote the
    feature to remove it. That change was worth keeping on its own merits — a
    page renderer should not open a connection — but it was not this bug, and
    the failure survived it untouched.

    tests/test_facility_comparables_same_country.py already does exactly this
    save/restore. Copy that pattern in any test that stubs a module."""
    saved = sys.modules.get("main")
    had = "main" in sys.modules
    try:
        yield
    finally:
        if had:
            sys.modules["main"] = saved
        else:
            sys.modules.pop("main", None)


def _fn(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found")


def _load(rows, boom=False, conn_none=False):
    """Return (fetch, render, conn).

    ★ The fetch and the render are SEPARATE functions on purpose — the query
    used to live inside the renderer and turned ten unrelated tests red in the
    full suite (see the module docstring). These tests drive both halves, and
    `render` must never touch a connection."""
    conn = None if conn_none else _Conn(rows, boom)
    fake = types.ModuleType("main")
    fake.get_read_db = lambda: conn
    sys.modules["main"] = fake
    ns = {"_esc": lambda x: str(x), "_RADIUS_KM": 50.0,
          "logger": types.SimpleNamespace(warning=lambda *a, **k: None)}
    for name in ("_nearby_generation_rows", "_nearby_generation_html"):
        exec(compile(ast.Module(body=[_fn(name)], type_ignores=[]),  # noqa: S102
                     str(SRC), "exec"), ns)
    return ns["_nearby_generation_rows"], ns["_nearby_generation_html"], conn


# ── it renders real, specific content ────────────────────────────────

def test_renders_the_facts_the_zero_click_queries_ask_for():
    fetch, render, _ = _load(SANTIAGO)
    out = render(fetch(-33.4489, -70.6693), "Santiago", "CL")
    assert "Power generation nearby" in out
    assert "87 operating generating units" in out      # 80+3+3+1
    assert "2,452 MW" in out                            # 1561+481+396+14
    assert "Santiago, CL" in out
    for fuel in ("Utility-Scale Solar", "Oil/Gas", "Hydropower"):
        assert fuel in out


def test_shares_are_computed_not_asserted():
    fetch, render, _ = _load(SANTIAGO)
    out = render(fetch(-33.4489, -70.6693), "Santiago", "CL")
    assert "64%" in out, "solar is 1561/2452 = 64%"


def test_it_adds_substantive_word_count():
    """The whole point is turning a 240-word page into one worth clicking."""
    import re
    fetch, render, _ = _load(SANTIAGO)
    out = render(fetch(-33.4489, -70.6693), "Santiago", "CL")
    words = re.sub(r"<[^>]+>", " ", out).split()
    assert len(words) >= 50, f"only {len(words)} words added"


def test_singular_plural_is_handled():
    fetch, render, _ = _load([("nuclear", 1, 900.0)])
    out = render(fetch(40.0, -74.0), "X", "US")
    assert "1 operating generating unit " in out or "1 operating generating unit&" in out
    assert "units" not in out.split("totalling")[0]


# ── the bounding box ─────────────────────────────────────────────────

def test_uses_an_indexable_bounding_box_not_a_distance_formula():
    """A great-circle distance per row cannot use ix_gempow_bbox. The measured
    0.27 ms depends on staying a BETWEEN on lat/lng."""
    src = ast.get_source_segment(TEXT, _fn("_nearby_generation_rows"))
    assert "lat BETWEEN" in src and "lng BETWEEN" in src
    for banned in ("earth_distance", "ll_to_earth", "ST_", "acos(", "haversine"):
        assert banned not in src, f"{banned} would defeat the bbox index"


def test_longitude_span_is_latitude_corrected():
    """A fixed degree span becomes a globe-spanning box near the poles."""
    fetch, render, conn = _load(SANTIAGO)
    render(fetch(-33.4489, -70.6693), "Santiago", "CL")
    lo_lat, hi_lat, lo_lng, hi_lng = conn.c.params
    lat_span, lng_span = hi_lat - lo_lat, hi_lng - lo_lng
    assert lng_span > lat_span, "longitude must widen away from the equator"
    assert lat_span == pytest.approx(2 * 50.0 / 111.0, rel=0.01)


def test_polar_latitude_does_not_produce_an_infinite_box():
    fetch, render, conn = _load(SANTIAGO)
    render(fetch(89.999, 0.0), "North", "XX")
    lo_lat, hi_lat, lo_lng, hi_lng = conn.c.params
    assert (hi_lng - lo_lng) < 180, "cos() collapse must be floored"


def test_only_operating_units_are_counted():
    """Planned and cancelled units are in this table too — counting them would
    overstate available generation, which is the opposite of useful."""
    fetch, render, conn = _load(SANTIAGO)
    render(fetch(-33.4489, -70.6693), "Santiago", "CL")
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
    fetch, render, _ = _load(SANTIAGO)
    assert render(fetch(lat, lng), "X", "Y") == "", why


def test_no_nearby_units_renders_nothing():
    """An empty section is worse than no section."""
    fetch, render, _ = _load([])
    assert render(fetch(-33.4, -70.6), "X", "Y") == ""


def test_zero_capacity_renders_nothing():
    fetch, render, _ = _load([("solar", 0, 0.0)])
    assert render(fetch(-33.4, -70.6), "X", "Y") == ""


def test_db_failure_renders_nothing_and_never_raises():
    fetch, render, _ = _load(SANTIAGO, boom=True)
    assert render(fetch(-33.4, -70.6), "X", "Y") == ""


def test_missing_connection_renders_nothing():
    fetch, render, _ = _load(SANTIAGO, conn_none=True)
    assert render(fetch(-33.4, -70.6), "X", "Y") == ""


def test_connection_is_closed_even_on_the_happy_path():
    fetch, render, conn = _load(SANTIAGO)
    render(fetch(-33.4489, -70.6693), "Santiago", "CL")
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


# ── the renderer must never touch a connection ───────────────────────
# This is the regression that turned ten unrelated tests red in the full suite.

def test_render_path_issues_no_sql():
    """_render_profile is called DIRECTLY by many tests with hand-built dicts
    and hand-rolled fake cursors. A query inside the render path handed one
    test's rows to another's 5-column unpack in _comparables_html —
    `ValueError: not enough values to unpack (expected 5, got 3)`, reproducible
    only with the whole suite loaded. Keep the renderer pure."""
    src = ast.get_source_segment(TEXT, _fn("_nearby_generation_html"))
    # Substring matching on short tokens is a trap: "conn" is inside
    # "interconnection options", which is prose this section is supposed to
    # say. Match on real call/keyword shapes instead.
    for banned in ("get_read_db", ".cursor(", ".execute(", "SELECT ",
                   "conn.", "psycopg2"):
        assert banned not in src, (
            f"the renderer touches {banned!r} — fetch in the route and pass "
            f"the rows in")


def test_render_profile_does_not_fetch_generation():
    render_profile = ast.get_source_segment(TEXT, _fn("_render_profile"))
    assert "_nearby_generation_rows(" not in render_profile, \
        "the fetch belongs in the route, not the renderer"
    assert '_nearby_generation_html(' in render_profile
    assert 'fac.get("_nearby_gen")' in render_profile


def test_the_route_fetches_before_rendering():
    """A renderer that is pure is useless if nobody fills the data."""
    i = TEXT.find('fac["_nearby_gen"] = _nearby_generation_rows(')
    j = TEXT.find("html = _render_profile(fac, slug)")
    assert i > 0, "the route never fetches the rows"
    assert i < j, "the fetch must happen before the render"


def test_renderer_survives_a_wrong_shaped_row():
    """A fake or a schema change handing back a different width must render
    nothing, not raise mid-page."""
    _, render, _ = _load(SANTIAGO)
    assert render([("solar", 1)], "X", "Y") == ""
    assert render([("solar", 1, 2.0, "extra")], "X", "Y") == ""
    assert render("not-a-list", "X", "Y") == ""
    assert render(None, "X", "Y") == ""


def test_both_halves_share_one_radius_constant():
    """The prose says 'within about 50 km'. If the fetch box and that sentence
    ever disagree, the page states a distance it did not measure."""
    assert "_RADIUS_KM = 50.0" in TEXT
    fetch_src = ast.get_source_segment(TEXT, _fn("_nearby_generation_rows"))
    render_src = ast.get_source_segment(TEXT, _fn("_nearby_generation_html"))
    assert "_RADIUS_KM" in fetch_src and "_RADIUS_KM" in render_src
    assert "50.0" not in render_src, "the renderer must not hardcode the radius"


# ── the leak guard ───────────────────────────────────────────────────

def test_this_file_restores_the_main_module():
    """A test that stubs sys.modules must put it back.

    Ten tests in two other files died because this one did not. The failure
    surfaced only in the full suite — alphabetically test_fac... runs before
    test_fro... and test_seo... — so every narrower reproduction stayed green
    and the traceback pointed at code this PR never touched."""
    src = pathlib.Path(__file__).read_text()
    assert "@pytest.fixture(autouse=True)" in src
    assert "def _restore_main_module()" in src
    assert "sys.modules.pop(\"main\", None)" in src
    assert "finally:" in src


def test_the_stub_really_is_removed_after_a_load():
    """Behavioural, not structural: after _load() the fake must not survive
    into the next test. The autouse fixture runs between tests, so assert on
    what a FRESH test sees rather than on cleanup we cannot observe here."""
    import sys as _sys
    m = _sys.modules.get("main")
    # Either main is absent, or it is not one of this file's fakes — a fake
    # from _load has get_read_db returning our _Conn, which has no fetchone
    # on its cursor.
    if m is not None:
        conn = getattr(m, "get_read_db", lambda: None)()
        if conn is not None:
            cur = conn.cursor()
            assert hasattr(cur, "fetchone"), (
                "a previous test's fake `main` survived into this one — "
                "_comparables_html unpacks 5 columns and will crash on it")
