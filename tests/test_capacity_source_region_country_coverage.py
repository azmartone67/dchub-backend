"""Capacity Source region search over the countries canonical_stats places.

routes/exclusive_listings.py derives its five region keys from
canonical_stats._COUNTRY_NAME and ._COUNTRY_REGION. A country missing from
either map is not an error anywhere: the listing simply gets `region: null`
in its teaser and matches no region filter. That silence is the defect these
tests exist to catch, so they pin BOTH halves —

  * a country the maps place (the Gulf, added 2026-09-15) is searchable by
    region and carries that region in its teaser, and a region filter
    EXCLUDES the listings outside it; and
  * a country the maps still do not place degrades to null and refuses
    nothing — no exception, no 500, and the rest of the feed unaffected.

The storage stand-in and the row builder are imported from
test_capacity_source_search rather than re-implemented. A second, simpler
stand-in would be a fixture more capable than the real object: it would
accept predicates Postgres would reject and pass while the route was broken.
Rows are injected by rebinding that module's LISTINGS before the store reads
it, so this file adds no row to the shared fixture set and changes none of
its expectations.
"""
import sys
import types

import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

import canonical_stats  # noqa: E402
import routes.exclusive_listings as el  # noqa: E402
from tests import test_capacity_source_search as base  # noqa: E402

_listing = base._listing

# ── the rows ──────────────────────────────────────────────────────────────
# AE by code and by name, because _region_of reads a stored country either
# way. KE is the control for "still unplaced": Nairobi is a real market and
# canonical_stats does not place Kenya, so it stands in for every country the
# map has not reached yet. dfw/fra are the negative control — a region filter
# that returned them would be matching everything.
ROWS = [
    _listing("dxb", market="Dubai", state=None, country="AE", capacity_mw=18.0),
    _listing("auh", market="Abu Dhabi", state=None,
             country="United Arab Emirates", capacity_mw=22.0),
    _listing("nbo", market="Nairobi", state=None, country="KE", capacity_mw=9.0),
    _listing("dfw", market="Dallas", state="TX", country="US", capacity_mw=40.0),
    _listing("fra", market="Frankfurt", state="HE", country="Germany", capacity_mw=30.0),
]
MEA = ["auh", "dxb"]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(base, "LISTINGS", ROWS)
    store = base._Store()
    assert len(store.rows) == len(ROWS), "the store did not read the injected rows"
    monkeypatch.setattr(el, "_fetch", store.fetch)
    monkeypatch.setattr(el, "_conn", base._no_connection)
    app = Flask(__name__)
    app.register_blueprint(el.exclusive_listings_bp)
    store.client = app.test_client()
    return store


def _teasers(env, query=""):
    slugs, body = base._feed(env, query)
    return slugs, {item["slug"]: item for item in body["items"]}


# ── a placed country is searchable and says so ────────────────────────────

def test_a_uae_listing_matches_region_middle_east_africa(env):
    """By code and by name, and the filter EXCLUDES everything else."""
    assert _teasers(env, "region=middle_east_africa")[0] == MEA
    # The same five rows are all there when nothing is filtered, so the list
    # above is the filter discriminating, not the fixture being small.
    assert _teasers(env)[0] == ["auh", "dfw", "dxb", "fra", "nbo"]
    # Every alias the program maps to the key reaches the same two rows.
    for alias in ("mea", "middle east", "Middle-East", "MIDDLE_EAST", "africa"):
        assert _teasers(env, "region=" + alias.replace(" ", "%20"))[0] == MEA, alias
    # ...and the keys that are not it reach neither.
    for other in ("north_america", "latin_america", "europe", "asia_pacific"):
        assert not set(_teasers(env, "region=" + other)[0]) & set(MEA), other


def test_the_uae_teaser_carries_its_region(env):
    teasers = _teasers(env)[1]
    assert teasers["dxb"]["country"] == "AE"
    assert teasers["dxb"]["region"] == "middle_east_africa"
    assert teasers["auh"]["region"] == "middle_east_africa"
    # The neighbours are not all one region either.
    assert teasers["dfw"]["region"] == "north_america"
    assert teasers["fra"]["region"] == "europe"


def test_the_country_search_finds_the_uae_by_code_and_by_name(env):
    assert _teasers(env, "country=AE")[0] == MEA
    assert _teasers(env, "country=United%20Arab%20Emirates")[0] == MEA


# ── an unplaced country degrades, it does not raise ───────────────────────

def test_a_country_the_map_does_not_place_degrades_to_null(env):
    """Kenya is absent from canonical_stats, so `region` is null — and the
    listing is still served, still searchable by country, and no request
    raises."""
    assert "Kenya" not in canonical_stats._COUNTRY_REGION, (
        "Kenya is now placed — pick another country the map has not reached, "
        "or this test no longer exercises the unplaced path")
    slugs, teasers = _teasers(env)
    assert "nbo" in slugs, "an unplaced country dropped the listing from the feed"
    assert teasers["nbo"]["region"] is None
    assert teasers["nbo"]["country"] == "KE"
    # It belongs to no region, so no region filter may claim it.
    for key in el.REGION_KEYS:
        assert "nbo" not in _teasers(env, "region=" + key)[0], key
    # And it is still reachable the way it is stored.
    assert _teasers(env, "country=KE")[0] == ["nbo"]


@pytest.mark.parametrize("country", [
    None, "", "  ", "KE", "Kenya", "ZZ", "Atlantis", "united arab emirates",
])
def test_region_of_never_raises_on_a_country_it_cannot_place(country):
    """_region_of returns a key or None for anything a row may hold."""
    result = el._region_of(country)
    assert result is None or result in el.REGION_KEYS, (country, result)


# ── the maps themselves ───────────────────────────────────────────────────

def test_the_gulf_is_placed_and_reaches_the_search():
    """The countries added 2026-09-15, end to end through the real tables."""
    for code, name in (("AE", "United Arab Emirates"), ("SA", "Saudi Arabia"),
                       ("QA", "Qatar"), ("KW", "Kuwait"), ("BH", "Bahrain"),
                       ("OM", "Oman")):
        assert canonical_stats._COUNTRY_NAME[code] == name
        assert canonical_stats._COUNTRY_REGION[name] == "the Middle East"
        assert el._region_of(code) == el._region_of(name) == "middle_east_africa"
    codes, names = el._region_countries(["middle_east_africa"])
    assert {"AE", "SA", "QA", "KW", "BH", "OM"} <= set(codes)
    assert "united arab emirates" in names


def test_the_two_country_maps_agree_on_the_countries_they_share():
    """canonical_stats._COUNTRY_NAME and facilities_hub._COUNTRY_NAMES are
    joined by country NAME in places, so a code they spell differently is a
    silent mismatch. Every code facilities_hub knows must now be placed."""
    from facilities_hub import _COUNTRY_NAMES
    disagree = {c: (canonical_stats._COUNTRY_NAME[c], _COUNTRY_NAMES[c])
                for c in set(canonical_stats._COUNTRY_NAME) & set(_COUNTRY_NAMES)
                if canonical_stats._COUNTRY_NAME[c] != _COUNTRY_NAMES[c]}
    assert not disagree, f"the two country maps spell these differently: {disagree}"
    unplaced = sorted(c for c in _COUNTRY_NAMES if c not in canonical_stats._COUNTRY_NAME)
    assert not unplaced, (
        f"{unplaced} are facilities countries canonical_stats does not name, so a "
        f"Capacity Source listing there gets region: null")


def test_adding_these_countries_cannot_move_the_published_dcpi_span():
    """The blast-radius claim, held as a test.

    canonical_stats counts its DCPI span from live market ROWS resolved by
    routes.dcpi.market_country — never from _COUNTRY_NAME. market_country can
    only ever return a US code, a code in _ISO_LABEL_COUNTRY, one in
    _MARKET_COUNTRY_BY_SLUG, or one in _STATE_AS_COUNTRY_OK. While no Gulf
    code is in any of those, no scored market can resolve to the Gulf, so
    naming these countries adds nothing to dcpi_countries, dcpi_intl or the
    published region phrase. If that stops being true this fails, and the
    region phrase genuinely changes — which is the moment to re-read it.
    """
    from routes import dcpi
    reachable = (set(dcpi._ISO_LABEL_COUNTRY.values())
                 | set(dcpi._MARKET_COUNTRY_BY_SLUG.values())
                 | set(dcpi._STATE_AS_COUNTRY_OK))
    assert reachable, "the resolver tables are empty — this fence checks nothing"
    gulf = {"AE", "SA", "QA", "KW", "BH", "OM", "IL"}
    assert not (reachable & gulf), (
        f"{sorted(reachable & gulf)} can now resolve from a live market, so the "
        f"published DCPI region phrase may have gained the Middle East. "
        f"Re-verify the canonical counts before shipping.")
