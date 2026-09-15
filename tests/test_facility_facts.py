"""Operator, street address, status and "What changed" on facility pages.

r-facility-facts (2026-09-15), SEO Step 1. util/facility_facts.py carries the
live measurements behind every rule pinned here. The address fixtures are
values stored in the address column of published pages today, taken from
3,000 live pages sampled from the three facility sitemaps.

Run:  python3 -m pytest tests/test_facility_facts.py -v
"""
import ast
import datetime as dt
import html as _html
import pathlib
import re
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from util.facility_facts import (  # noqa: E402
    CHANGE_FIELDS, CHANGE_LAYER, CHANGE_LIST_MAX, change_items, is_fleet_row,
    real_operator, real_status, street_address)
from util.facility_headline import compose_description, compose_title  # noqa: E402

PAGE = ROOT / "routes" / "facility_profile_page.py"
PAGE_SRC = PAGE.read_text(encoding="utf-8")
PAGE_TREE = ast.parse(PAGE_SRC)
UTC = dt.timezone.utc


def _at(month, day, hour=5):
    return dt.datetime(2026, month, day, hour, 30, tzinfo=UTC)


# (kind, field, old_value, new_value, detected_at), as entity_changes stores them
HISTORY = [
    ("field_change", "power_mw", "12.000000", "63000.000000", _at(9, 15)),
    ("field_change", "status", "Planned", "Operational", _at(9, 14)),
    ("field_change", "is_duplicate", "false", "true", _at(9, 13)),
    ("field_change", "provider", None, "Bothell Data Services", _at(8, 2)),
    ("appeared", None, None, None, _at(8, 1)),
]


# ── the street address ──────────────────────────────────────────────────────

REAL_ADDRESSES = [
    "1950 N Stemmons Fwy",
    "705 Development Court, Poughkeepsie, NY, 12601",
    "No.  111 Lane 3111 West Huancheng Rd. Fengxian District, Shanghai",
    "4762 AND 4764 BAKERS FERRY RD, SW",
    "20544 HIGHWAY 370",
    "Calle 31",
    "Stekkenbergweg",
    "Sheppard Street",
    "Brunel Close, Newark, NG24 2EG, GB",
    "Balázs Béla utca",
    "Rodovia Perito Criminal Engenheiro Antonio Carlos Moraes",
    "環保大道 Wan Po Road",
    "Рябиновая улица",
    "Jl. Sumbawa Blok Bi No.1, Mekarwangi, Cikarang Bar., Bekasi, Jawa Barat",
]

# Also stored in the address column of a published page today. Not one of
# them is an address, and every one was printed in the page's Address tile.
STORED_NON_ADDRESSES = [
    "India", "GB", "CA", "London", "Chicago", "New Mexico", "Mumbai, India",
    "Council Bluffs, Iowa", "it has", "Arizona through", "Australia on",
    "Japan to", "Port of", "MW", "Cambridge Business Park", "FM1957",
    "1 , 印西市, 千葉県, 270-1352, JP",
]


@pytest.mark.parametrize("value", REAL_ADDRESSES)
def test_a_real_street_address_is_published(value):
    assert street_address(value) == " ".join(value.split())


@pytest.mark.parametrize("value", STORED_NON_ADDRESSES)
def test_a_stored_non_address_is_refused(value):
    assert street_address(value) == ""


@pytest.mark.parametrize("value", [
    "near the Bath Road",         # a street name inside prose
    "100 MW campus",              # a measurement beside two words
    "300 jobs created",           # a leading number that counts something
    "Data Hall 3",                # a part of the site
    "Unknown Street",             # a placeholder dressed as a street
    "Street",                     # a street type alone
    "Dublin 15",                  # a postal district
    "40549 Düsseldorf",           # a postal code and a city
    "1 " + "Long " * 40 + "Street",   # over the length cap
    None, "",
])
def test_each_refusal_rule_refuses(value):
    """One value per refusal rule, each of which the OTHER rules would accept
    — so deleting any one rule turns exactly its case green-on-publish."""
    assert street_address(value) == ""


# ── operator, status, fleet ────────────────────────────────────────────────

def test_the_operator_is_real_or_absent():
    assert real_operator("Bothell Data Services", "Kanobe, LLC") == \
        "Bothell Data Services"
    # contained in the name is still an operator worth stating
    assert real_operator("Google", "Google Council Bluffs Data Center") == \
        "Google"
    for placeholder in ("Operator", "operator", "Unknown", "N/A", "none", "",
                        None, "   "):
        assert real_operator(placeholder, "Real DC") == "", placeholder
    # repeating the facility's own name is not
    assert real_operator("university of calgary", "University of Calgary") == ""


def test_the_status_is_real_or_absent():
    assert real_status("operational") == "Operational"
    assert real_status("under construction") == "Under Construction"
    for placeholder in ("Unknown", "unknown", "N/A", "", None):
        assert real_status(placeholder) == "", placeholder


def test_a_fleet_is_a_real_capacity_above_the_cap_and_nothing_else():
    assert is_fleet_row(63000.0) and is_fleet_row("63000")
    assert not is_fleet_row(5000), "the cap is inclusive: 5,000 MW is a site"
    assert is_fleet_row(5000.5)
    for value in (350, None, "", "abc", 0, -5, True, float("inf"),
                  float("nan")):
        assert not is_fleet_row(value), value


# ── what changed ────────────────────────────────────────────────────────────

def test_each_change_reads_as_a_sentence_about_real_values():
    assert change_items(HISTORY) == [
        (dt.date(2026, 9, 14), "Status changed from Planned to Operational"),
        (dt.date(2026, 8, 2), "Operator set to Bothell Data Services"),
        (dt.date(2026, 8, 1), "Added to DC Hub"),
    ], "a fleet capacity and a bookkeeping flag produce no line"


def test_a_change_to_or_from_a_non_value_is_not_dressed_as_one():
    rows = [
        ("field_change", "provider", "Equinix", "Unknown", _at(9, 10)),
        ("field_change", "status", "operational", "Operational", _at(9, 9)),
        ("field_change", "city", "Regional", "Frankfurt", _at(9, 8)),
        ("field_change", "power_mw", "100.000000", "212.000000", _at(9, 7)),
        ("field_change", "power_mw", "63000.000000", "212.000000", _at(9, 6)),
        ("field_change", "state", "WA", "OR", _at(9, 5)),
        ("field_change", "name", "None", "Kanobe, LLC", _at(9, 4)),
    ]
    assert [text for _day, text in change_items(rows)] == [
        "City set to Frankfurt",
        "Reported capacity changed from 100 MW to 212 MW",
        "Reported capacity set to 212 MW",
        "Name set to Kanobe, LLC",
    ]


def test_the_list_is_short_newest_first_and_dated_in_utc():
    rows = [("appeared", None, None, None, _at(8, d)) for d in range(1, 12)]
    items = change_items(rows)
    assert len(items) == CHANGE_LIST_MAX == 5
    assert [day.day for day, _text in items] == [11, 10, 9, 8, 7]
    # 01:30 at UTC+05:00 is the previous day in UTC
    east = dt.timezone(dt.timedelta(hours=5))
    late = dt.datetime(2026, 9, 15, 1, 30, tzinfo=east)
    assert change_items([("appeared", None, None, None, late)])[0][0] == \
        dt.date(2026, 9, 14)
    # malformed rows are skipped, not raised
    assert change_items([None, ("appeared",),
                         ("appeared", None, None, None, None)]) == []


# ── the meta description ───────────────────────────────────────────────────

KANOBE = ("Kanobe, LLC", "Bothell Data Services", "Bothell", "WA", "US")


def test_the_snippet_states_address_operator_and_status():
    d = compose_description(*KANOBE, status="Operational", iso="WECC",
                            time_to_power_months=16.7,
                            address="3301 Monte Villa Parkway")
    assert d.startswith(
        "Operational data center at 3301 Monte Villa Parkway, Bothell, "
        "Washington, operated by Bothell Data Services, on the WECC grid."), d
    assert len(d) <= 160, (len(d), d)


def test_an_address_naming_its_city_is_not_followed_by_the_city_again():
    d = compose_description("Some DC", "Acme", "Poughkeepsie", "NY", "US",
                            address="705 Development Court, Poughkeepsie, NY, "
                                    "12601")
    assert "at 705 Development Court, Poughkeepsie, NY, 12601, operated by " \
           "Acme." in d, d
    # a street NAMED after the city is not a city segment
    d = compose_description("Some DC", "Acme", "Irving", "TX", "US",
                            address="3180 Irving Blvd")
    assert "at 3180 Irving Blvd, Irving, Texas, operated by Acme." in d, d


def test_a_junk_address_or_a_fleet_row_adds_nothing():
    d = compose_description(*KANOBE, status="Operational",
                            address="Arizona through")
    assert "Arizona" not in d and " at " not in d, d
    assert "operated by Bothell Data Services" in d, \
        "a refused address costs the address, not the operator"
    fleet = compose_description("Utility None", "Utility", None, None, "US",
                                power_mw=63000.0, status="Planned",
                                address="1 Riverside Plaza")
    assert "operated by" not in fleet and "Riverside" not in fleet, fleet


def test_the_title_takes_none_of_it():
    """The 09-11 title template is still being measured."""
    import inspect
    params = inspect.signature(compose_title).parameters
    assert "address" not in params and "changes" not in params


# ── the rendered page ───────────────────────────────────────────────────────

ROW = {"id": 2254, "_src_table": "discovered_facilities",
       "name": "Kanobe, LLC", "provider": "Bothell Data Services",
       "city": "Bothell", "state": "WA", "country": "US",
       "status": "Operational", "address": "3301 Monte Villa Parkway",
       "latitude": 47.7794, "longitude": -122.1905, "power_mw": None,
       "canonical_slug": "bothell-data-services-kanobe-llc-968b54db"}


@pytest.fixture
def fpp(monkeypatch):
    """The page module with a DB-less `main`, put back by monkeypatch."""
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


def _hero(page):
    return page.split('<div class="hero">', 1)[1].split(
        '<p class="section-sub"', 1)[0]


def test_the_first_lines_state_operator_address_and_status(fpp):
    hero = _hero(_render(fpp))
    assert re.findall(
        r'<li><span class="fact-label">(\w+)</span> ([^<]+)</li>', hero) == [
        ("Operator", "Bothell Data Services"),
        ("Address", "3301 Monte Villa Parkway"),
        ("Status", "Operational")]
    assert hero.index("<h1>") < hero.index('class="facts"') < \
        hero.index('class="loc"')


def test_a_stored_non_address_appears_nowhere_on_the_page(fpp):
    page = _render(fpp, address="Arizona through")
    assert "Arizona through" not in page
    assert 'stat-label">Address<' not in page
    assert "streetAddress" not in page
    # the anchor: a real address reaches every one of those surfaces
    page = _render(fpp)
    assert 'stat-label">Address<' in page
    assert '"streetAddress": "3301 Monte Villa Parkway"' in page
    assert page.count("3301 Monte Villa Parkway") >= 4, \
        "hero, tile, JSON-LD and the meta description at least"


def test_the_placeholder_operator_is_never_printed_as_a_name(fpp):
    page = _render(fpp, provider=None)
    assert '<span class="fact-label">Operator</span>' not in page
    assert '<div class="prov">' not in page
    assert "operated by Operator" not in page
    assert '"description": "Data center facility in ' in page
    # the anchor: a real operator is still named in the same JSON-LD field
    assert '"description": "Data center facility operated by Bothell Data ' \
           'Services in ' in _render(fpp)


def test_a_fleet_row_gets_none_of_it(fpp):
    page = _render(fpp, name="Utility None", provider="Utility",
                   power_mw=63000.0, address="1 Riverside Plaza",
                   _changes=HISTORY)
    assert 'class="facts"' not in page and "Last updated" not in page
    assert "What changed" not in page and "dateModified" not in page
    assert "Riverside" not in page and "63000" not in page


def test_last_updated_and_datemodified_are_the_newest_listed_change(fpp):
    page = _render(fpp, _changes=HISTORY)
    assert 'Last updated <time datetime="2026-09-14">Sep 14, 2026</time>' in \
        _hero(page)
    assert '"dateModified": "2026-09-14"' in page
    listed = page.split("<h2>What changed</h2>", 1)[1].split("</ul>", 1)[0]
    assert listed.count("<li>") == 3
    assert "Status changed from Planned to Operational" in listed
    assert "63000" not in page and "63,000" not in page


def test_no_recorded_change_states_no_date(fpp):
    page = _render(fpp)
    assert "Last updated" not in page and "dateModified" not in page
    assert "What changed" not in page


def test_the_page_title_does_not_move(fpp):
    title = lambda p: _html.unescape(re.search(r"<title>(.*?)</title>",  # noqa: E731
                                               p).group(1))
    bare = _render(fpp, address=None, status=None)
    full = _render(fpp, _changes=HISTORY)
    assert title(bare) == title(full) == compose_title(
        "Kanobe, LLC", "Bothell Data Services", "Bothell", "WA", "US",
        power_mw=None, status="Operational", iso="")


# ── the read ────────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, rows, boom):
        self.rows, self.boom = rows, boom
        self.sql = self.params = None
        self.calls = 0

    def execute(self, q, p=None):
        self.calls += 1
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
    def __init__(self, rows, boom):
        self.c = _Cur(rows, boom)
        self.closed = False

    def cursor(self):
        return self.c

    def close(self):
        self.closed = True


def _fn(name):
    for n in PAGE_TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} is not a top-level def in {PAGE}")


def _const(name):
    for n in PAGE_TREE.body:
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) == name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(f"{name} not found in {PAGE}")


def _reader(monkeypatch, rows=(), boom=False, conn_none=False):
    conn = None if conn_none else _Conn(list(rows), boom)
    fake = types.ModuleType("main")
    fake.get_read_db = lambda: conn
    monkeypatch.setitem(sys.modules, "main", fake)
    ns = {"_CHANGE_ROWS_LIMIT": _const("_CHANGE_ROWS_LIMIT"),
          "logger": types.SimpleNamespace(warning=lambda *a, **k: None)}
    exec(compile(ast.Module(body=[_fn("_facility_change_rows")],  # noqa: S102
                            type_ignores=[]), str(PAGE), "exec"), ns)
    return ns["_facility_change_rows"], conn


def test_the_history_is_read_under_the_key_the_capture_wrote(monkeypatch):
    from routes.temporal_capture import _LAYERS, _entity_key
    row = ("appeared", None, None, None, _at(8, 1))
    fetch, conn = _reader(monkeypatch, rows=[row])
    assert fetch({"id": 2254, "_src_table": "discovered_facilities"}) == [row]
    sql, params = conn.c.sql, conn.c.params
    assert "FROM entity_changes" in sql
    assert "ORDER BY detected_at DESC" in sql
    assert params[0] == CHANGE_LAYER == "discovered_facilities"
    assert params[1] == _entity_key({"id": 2254}, ("id",)) == "2254"
    assert sorted(params[2]) == sorted(CHANGE_FIELDS)
    assert params[3] == _const("_CHANGE_ROWS_LIMIT") >= CHANGE_LIST_MAX
    assert conn.closed
    # the capture still keys this layer on id, and tracks every labelled field
    spec = next(s for s in _LAYERS if s["layer"] == CHANGE_LAYER)
    assert spec["key"] == ("id",)
    assert set(CHANGE_FIELDS) <= set(spec["track"])


def test_a_row_the_capture_does_not_track_is_never_queried(monkeypatch):
    fetch, conn = _reader(monkeypatch)
    for fac in ({"id": "proj_4b370e5cd96b", "_src_table": "facilities"},
                {"id": None, "_src_table": "discovered_facilities"},
                {"id": 7}):
        assert fetch(fac) == [], fac
    assert conn.c.calls == 0


def test_a_failed_read_costs_the_history_and_nothing_else(monkeypatch):
    fetch, conn = _reader(monkeypatch, boom=True)
    assert fetch({"id": 1, "_src_table": "discovered_facilities"}) == []
    assert conn.closed
    fetch, _conn = _reader(monkeypatch, conn_none=True)
    assert fetch({"id": 1, "_src_table": "discovered_facilities"}) == []


def test_the_route_reads_the_history_before_it_renders():
    src = ast.get_source_segment(PAGE_SRC, _fn("render_facility_profile"))
    i = src.find('fac["_changes"] = _facility_change_rows(fac)')
    j = src.find("html = _render_profile(fac, slug)")
    assert 0 < i < j, (i, j)
