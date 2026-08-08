"""The provider-website scrape ingested pages as facilities (2026-08-08).

Every case here is a real row from `discovered_facilities` where
source='providerwebsites', quoted verbatim with its live id. The point of the
file is that the crawler filter and the repair endpoint agree on what "not a
facility" means, and that neither of them can quietly start passing everything.
"""
import pytest

from util.scraped_page_title import (
    is_page_furniture, has_building_grain, leading_place,
)
from routes.facility_scrape_quality import (
    plan_row, FLAG_FURNITURE, FLAG_LOCALE, PAGE_LOCALE,
)

# --- live rows that name no place at all -----------------------------------
FURNITURE = [
    (7721, 'Equinix', "Equinix Smart Hands®"),
    (7725, 'Equinix', "Cages and cabinets"),
    (7727, 'Equinix', "Equinix Flex Space"),
    (7669, 'Equinix', "xScaleEnable multi-megawatt, AI-ready capacity"),
    (8274, 'Equinix', "ColocationInterconnection-ready infrastructure"),
    (8220, 'Equinix', "Data center excellenceStreamline and scale your deployment"),
    (7818, 'QTS', "All LocationsAll Locations"),
    (7750, 'Digital Realty', "See our EMEA facilities"),
    (7740, 'Digital Realty', "See our Americas facilities"),
    (7734, 'Digital Realty', "Security & Compliance"),
    (7735, 'Digital Realty', "Sustainable Data Centers"),
    (7768, 'CyrusOne', "DATA CENTERS"),
    (7822, 'CoreSite', "DATA CENTER LOCATIONS"),
    (7904, 'DataBank', "Edge Strategy"),
    (7952, 'Flexential', "Download Our Fleet Tour Guide"),
    (7930, 'Flexential', "View Locations"),
    (7731, 'Vantage Data Centers', "APAC"),
    (7730, 'Vantage Data Centers', "EMEA"),
    (7817, 'Vantage Data Centers', "North America"),
    (8272, 'Equinix', "Europe, Middle East & Africa"),
    (7886, 'Vantage Data Centers', "French"),
    (7903, 'DataBank', "United Kingdom"),
]

# --- live rows that DO name a building. None of these may be suppressed. ----
REAL_FACILITIES = [
    (7653, "Sterling, VA, NVA1-NVA3"),
    (7805, "London, LON1"),
    (7793, "Frankfurt, FRA1"),
    (7953, "Atlanta - Alpharetta"),
    (7984, "Portland - Hillsboro 3"),
    (7826, "Ashburn I, VA, United States"),
    (7863, "Milan II, Italy"),
    (7981, "Phoenix, Arizona - Deer Valley"),
    (11330, "San Antonio, TX - SAT2-SAT4"),
]

# The collapsed-heading rule is the one that can misfire, because operator
# brands are CamelCase. Each of these tripped it before the brand-strip and
# the 12-char token floor went in.
BRAND_CAMELCASE = [
    ("CyrusOne", "CyrusOne San Antonio, TX - SAT2-SAT4"),
    ("CyrusOne", "CyrusOne"),
    ("DataBank", "DataBank Salt Lake City"),
    ("CoreSite", "CoreSite Santa Clara SV7"),
    ("EdgeConneX", "EdgeConneX Chicago"),
    ("DigitalBridge", "DigitalBridge Ashburn"),
]

# --- live rows shaped like a metro index page. Reported, never written. -----
METRO_LANDING = [
    (8269, "Amsterdam"),
    (7910, "Chicago Data Centers"),
    (7947, "Atlanta, GA"),
    (7885, "Milan, Italy"),
    (8266, "Accra"),
]


@pytest.mark.parametrize("rid,provider,name", FURNITURE)
def test_page_furniture_is_rejected(rid, provider, name):
    assert is_page_furniture(name, provider), \
        f"id={rid} {name!r} must not become a facility"


@pytest.mark.parametrize("rid,name", REAL_FACILITIES + METRO_LANDING)
def test_places_are_not_furniture(rid, name):
    assert not is_page_furniture(name), f"id={rid} {name!r} names a place"


@pytest.mark.parametrize("rid,name", REAL_FACILITIES)
def test_building_grain_detected(rid, name):
    assert has_building_grain(name), f"id={rid} {name!r} identifies a building"


@pytest.mark.parametrize("rid,name", METRO_LANDING)
def test_metro_landing_has_no_building_grain(rid, name):
    assert not has_building_grain(name), f"id={rid} {name!r} is a metro, not a building"


@pytest.mark.parametrize("name,place", [
    ("Frankfurt, FRA1", "Frankfurt"),
    ("Sterling, VA, NVA1-NVA3", "Sterling"),
    ("Ashburn II, VA, United States", "Ashburn"),
    ("Atlanta - Alpharetta", "Atlanta"),
    ("Chicago Data Centers", "Chicago"),
    ("Milan II, Italy", "Milan"),
    ("London, LON1", "London"),
])
def test_leading_place(name, place):
    assert leading_place(name) == place


# --------------------------------------------------------------------------
# plan_row: what the repair would write. The page locale on this scrape is
# 'London', which is why the London cases are the ones that matter.
# --------------------------------------------------------------------------

def test_furniture_is_suppressed_and_demarketed():
    p = plan_row(7721, "Equinix Smart Hands®", "London", "London")
    assert p["rule"] == FLAG_FURNITURE
    assert p["suppress"] is True
    assert p["city_to"] is None


def test_row_named_elsewhere_moves_off_the_page_locale():
    """'CyrusOne Frankfurt, FRA1' is filed under city='London'. Its own name
    is the evidence for where it belongs."""
    p = plan_row(7793, "Frankfurt, FRA1", "London", "London")
    assert p["rule"] == FLAG_LOCALE
    assert p["city_to"] == "Frankfurt"
    assert p["suppress"] is False


def test_genuinely_london_row_is_left_alone():
    """9 of the 312 really are in London. The repair must not move them."""
    assert plan_row(7805, "London, LON1", "London", "London") is None
    assert plan_row(7675, "London", "London", "London") is None


def test_correct_city_still_loses_the_false_market_stamp():
    """All 312 rows carry market='London', including ones whose city is right.
    The market claim is the lie; city is already fine, so city must not move."""
    p = plan_row(7706, "Tokyo", "Tokyo", "London")
    assert p is not None and p["rule"] == FLAG_LOCALE
    assert p["city_to"] == "Tokyo"


def test_country_is_never_in_the_plan():
    """For the bare-metro rows the country is the one correct field — the
    2026-08-07 repair already established that. This module must not touch it.
    """
    for rid, name in [(r, n) for r, _p, n in FURNITURE] + REAL_FACILITIES + METRO_LANDING:
        p = plan_row(rid, name, "London", "London")
        if p is not None:
            assert "country_to" not in p


def test_metro_landing_rows_are_not_suppressed_by_apply():
    """The 181 metro-landing rows are a separate, unsettled question: 50 of
    them are the only row we hold for that provider+city, and the shape also
    catches real single-site campuses. /apply must relocate them, never
    suppress them."""
    for rid, name in METRO_LANDING:
        p = plan_row(rid, name, "London", "London")
        if p is not None:
            assert p["rule"] == FLAG_LOCALE
            assert p["suppress"] is False


# --------------------------------------------------------------------------
# The guard must be able to FAIL. A predicate that returns True for everything
# would pass every assertion above.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("provider,name", BRAND_CAMELCASE)
def test_camelcase_brand_is_not_a_collapsed_heading(provider, name):
    assert not is_page_furniture(name, provider), \
        f"{name!r} is a real facility name, not scraped page furniture"


def test_collapsed_heading_still_fires_with_a_provider_supplied():
    """The brand-strip must not disarm the rule it guards."""
    assert is_page_furniture("xScaleEnable multi-megawatt, AI-ready capacity",
                             "Equinix")
    assert is_page_furniture("All LocationsAll Locations", "QTS")
    assert is_page_furniture("Data center excellenceStreamline and scale "
                             "your deployment", "Equinix")


def test_collapsed_heading_rule_abstains_without_a_provider():
    """'DigitalBridge' is lexically identical to a collapsed heading. The rule
    that cannot tell them apart must not run when nothing can disambiguate it
    — a dropped facility costs more than a kept junk row."""
    assert not is_page_furniture("DigitalBridge Ashburn")
    assert not is_page_furniture("xScaleEnable multi-megawatt, AI-ready capacity")
    # the exact-match rules still work with no provider
    assert is_page_furniture("Equinix Smart Hands®")
    assert is_page_furniture("APAC")


def test_predicate_is_not_vacuous():
    assert not is_page_furniture("Sterling, VA, NVA1-NVA3")
    assert not is_page_furniture("Amsterdam")
    assert not has_building_grain("Amsterdam")
    assert has_building_grain("LON1")
    # CamelCase brands are not collapsed headings
    assert not is_page_furniture("CyrusOne")
    assert not is_page_furniture("DataBank")


def test_keeper_election_excludes_not_a_facility_rows():
    """Suppressing a page title leaves its canonical_slug group with no keeper
    — measured 34/34 alone in their group — and repair_dedup_keeper_election
    would elect the junk row back to is_duplicate=0 on its next run."""
    import repair_dedup_keeper_election as rk
    assert FLAG_FURNITURE in rk.NOT_A_FACILITY_METHODS
    # both CTEs must be filtered: nokeeper decides WHICH groups, ranked decides
    # WHICH ROW, and a leak in either one revives the row.
    assert "FROM eligible\n" in rk.ELECTION_SQL
    assert "FROM eligible d" in rk.ELECTION_SQL
    assert "FROM discovered_facilities d" not in rk.ELECTION_SQL


def test_page_locale_constant_matches_the_scrape():
    assert PAGE_LOCALE == "London"
