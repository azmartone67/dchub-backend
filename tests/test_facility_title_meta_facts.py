"""Facility SERP titles and meta descriptions carry facts (r-title-facts).

NO NETWORK, NO DB.

WHAT CHANGED (2026-09-10, owner-approved drafts §4–§6). The r-title-template
title (#4296, `{lead} · {City} · {ISO} | DC Hub`):

  * repeated the city when the name already carries it —
    "Google Council Bluffs Data Center · Council Bluffs · MISO | DC Hub";
  * dropped the state, while 9 of the top 19 GSC queries for that page
    contain "iowa";
  * never showed MW;

and the description read "<name> is a data center in Council Bluffs, IA, US.
Power capacity: 350.0 MW. View specs, …" — no status, grid or time-to-power.

HOW THIS FILE CHECKS IT. Every expected render is a LITERAL copied from the
approved drafts, never a re-derivation: an oracle that calls the composer
proves only that it equals itself. Two fixtures carry "China" in the
operator's own name, so checks on the country read the title's location
SEGMENT — `"China" in title` would survive the country being dropped.

The dedup key is guarded in tests/test_facility_title_template.py against an
independent oracle of the pre-template title; it is not re-asserted here.
"""
import html as _html
import pathlib
import re
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.facility_headline import (  # noqa: E402
    DESCRIPTION_LIMIT, TITLE_BUDGET, compose_description, compose_title,
    facility_headline)

COUNCIL_BLUFFS = ("Google Council Bluffs Data Center", "Google",
                  "Council Bluffs", "IA", "US")
FR5_KEEPER = ("Equinix FR5 - Frankfurt, KleyerStrasse", "Equinix", "Frankfurt",
              None, "DE")
MARS = ("Mars Datacenter Ankara-1", "Mars Datacenter", "Ankara", "Ankara", "TR")
COMPASS = ("Compass Goodyear Campus (Phoenix)", "Compass Datacenters",
           "Goodyear", "AZ", "US")
ANTHROPIC_NY = ("Anthropic New York AI Campus", "Anthropic / Fluidstack",
                "New York", "NY", "US")
FR5_DUP = ("Equinix FR5", "Equinix", "Frankfurt", None, "DE")
PLAN_B = ("Plan B Tawa", "Plan B Limited", "Wellington", None, "NZ")

# row · the page's facts · the approved <title> · the approved description
APPROVED = [
    (COUNCIL_BLUFFS,
     dict(power_mw=350, status="Operational", iso="MISO",
          time_to_power_months=19.2),
     "Google Council Bluffs Data Center · Iowa · 350 MW | DC Hub",
     "350 MW operational data center in Council Bluffs, Iowa, on the MISO "
     "grid. ~19 months to power for new builds here. Specs, nearby power and "
     "peer sites on DC Hub."),
    (FR5_KEEPER,
     dict(power_mw=None, status="Operational", iso="ENTSOE-DE",
          time_to_power_months=117.6),
     "Equinix FR5 · Frankfurt, Germany · ENTSOE-DE | DC Hub",
     "Equinix FR5 - Frankfurt, KleyerStrasse: operational data center in "
     "Frankfurt, Germany, on the ENTSOE-DE grid. ~9.8 years to power for new "
     "builds here."),
    (MARS,
     dict(power_mw=None, status="Operational", nearby_generation_mw=1628),
     "Mars Datacenter Ankara-1 · Turkey | DC Hub",
     "Operational data center in Ankara, Turkey. 1,628 MW of operating "
     "generation within 50 km. Specs, nearby power and peer sites on DC Hub."),
    (COMPASS,
     dict(power_mw=100, status="Under Construction", iso="WECC",
          time_to_power_months=12.4),
     "Compass Goodyear Campus (Phoenix) · 100 MW · WECC | DC Hub",
     "100 MW under-construction data center in Goodyear, Arizona, operated by "
     "Compass Datacenters, on the WECC grid. ~12 months to power for new "
     "builds here."),
    (ANTHROPIC_NY,
     dict(power_mw=200, status="Planned", iso="NYISO",
          time_to_power_months=27.5),
     "Anthropic New York AI Campus · 200 MW planned | DC Hub",
     "200 MW planned data center in New York, NY, operated by Anthropic / "
     "Fluidstack, on the NYISO grid. ~28 months to power for new builds "
     "here."),
    (FR5_DUP,
     dict(power_mw=40, status="Operational", iso="ENTSOE-DE",
          time_to_power_months=117.6),
     "Equinix FR5 · Frankfurt, Germany · 40 MW | DC Hub",
     "40 MW operational data center in Frankfurt, Germany, on the ENTSOE-DE "
     "grid. ~9.8 years to power for new builds here. Specs, nearby power & "
     "peers."),
    (PLAN_B,
     dict(power_mw=None, status="Operational", nearby_generation_mw=203),
     "Plan B Tawa · Wellington, New Zealand | DC Hub",
     "Operational data center in Wellington, New Zealand, operated by Plan B "
     "Limited. 203 MW of operating generation within 50 km. Specs, nearby "
     "power & peers."),
]
_IDS = [a[0][0] for a in APPROVED]
_TITLE_FACTS = ("power_mw", "status", "iso")

# An 18-char lead in a place with a short region, so every optional part has
# room: a mutation that lets through a fact the rules refuse is VISIBLE here,
# not hidden by the 60-char budget.
ROOMY = ("Pipeline Site", "Acme", "Tulsa", "OK", "US")


def _segments(title):
    """The title's " · " segments with the brand removed; [0] is the lead."""
    assert title.endswith(" | DC Hub"), title
    return title[: -len(" | DC Hub")].split(" · ")


# ── the approved renders ─────────────────────────────────────────────────

def test_the_budgets_are_the_approved_ones():
    """The tunables themselves — a raised budget would carry every length
    assertion in this file with it."""
    assert TITLE_BUDGET == 60
    assert DESCRIPTION_LIMIT == 160


@pytest.mark.parametrize("row,facts,title,desc", APPROVED, ids=_IDS)
def test_the_approved_title_renders(row, facts, title, desc):
    got = compose_title(*row, **{k: v for k, v in facts.items()
                                 if k in _TITLE_FACTS})
    assert got == title
    assert len(got) <= 60


@pytest.mark.parametrize("row,facts,title,desc", APPROVED, ids=_IDS)
def test_the_approved_description_renders(row, facts, title, desc):
    got = compose_description(*row, **facts)
    assert got == desc
    assert len(got) <= 160


def test_with_no_facts_the_title_is_facility_headlines_title():
    """One composition, two callers: the headline dict and the page."""
    for row, _facts, _t, _d in APPROVED:
        assert compose_title(*row) == facility_headline(*row)["title"], row


# ── the title's rules ────────────────────────────────────────────────────

def test_mw_is_shown_when_it_fits():
    assert compose_title(*ROOMY, power_mw=350) == \
        "Acme Pipeline Site · Tulsa, Oklahoma · 350 MW | DC Hub"
    # thousands separator; one decimal only for a fraction under 10 MW
    assert _segments(compose_title(*ROOMY, power_mw=1200))[-1] == "1,200 MW"
    assert _segments(compose_title(*ROOMY, power_mw=2.5))[-1] == "2.5 MW"
    assert _segments(compose_title(*ROOMY, power_mw="40.0"))[-1] == "40 MW"
    # unparseable or absent: no fact at all, not "None MW"
    for junk in (None, "", "n/a", "350 MW", True):
        assert "MW" not in compose_title(*ROOMY, power_mw=junk), junk


def test_the_fact_falls_back_from_mw_and_phase_to_mw_alone():
    # "200 MW planned" fits beside this name
    assert compose_title(*ANTHROPIC_NY, power_mw=200, status="Planned") == \
        "Anthropic New York AI Campus · 200 MW planned | DC Hub"
    # "100 MW under construction" does not, so MW stands alone — and the ISO
    # gets the room the state qualifier could not use
    assert compose_title(*COMPASS, power_mw=100, status="Under Construction",
                         iso="WECC") == \
        "Compass Goodyear Campus (Phoenix) · 100 MW · WECC | DC Hub"
    # with no MW the phase stands alone, capitalised
    assert _segments(compose_title(*ROOMY, status="under construction"))[-1] \
        == "Under construction"


def test_the_iso_is_the_first_thing_dropped_past_60():
    # the fact and the state fit (58); the ISO would make it 65
    t = compose_title(*COUNCIL_BLUFFS, power_mw=350, status="Operational",
                      iso="MISO")
    assert t == "Google Council Bluffs Data Center · Iowa · 350 MW | DC Hub"
    # take the MW away and the ISO fits again — after the state it lost to
    assert compose_title(*COUNCIL_BLUFFS, status="Operational", iso="MISO") \
        == "Google Council Bluffs Data Center · Iowa · MISO | DC Hub"
    # when shown, it is always the LAST segment (exactly 60 here)
    t = compose_title(*ROOMY, power_mw=350, iso="SPP")
    assert t == "Acme Pipeline Site · Tulsa, Oklahoma · 350 MW · SPP | DC Hub"
    assert len(t) == 60
    # and the not-a-label sentinel is never shown
    assert "UNK" not in compose_title(*ROOMY, iso="UNK")


@pytest.mark.parametrize("mw", [48000, "48000", 48000.0, 5000.5])
def test_an_implausible_mw_never_reaches_the_title_or_the_description(mw):
    """Pipeline rows carry 48,000+ MW; a SERP title would print it as a fact
    about one building. The fixture has room for the fact, so a missing cap
    shows up as a rendered "48,000 MW", not as a budget drop."""
    t = compose_title(*ROOMY, power_mw=mw, status="Planned")
    d = compose_description(*ROOMY, power_mw=mw, status="Planned")
    assert t == "Acme Pipeline Site · Tulsa, Oklahoma · Planned | DC Hub", t
    assert d.startswith("Planned data center in Tulsa, Oklahoma."), d
    assert " MW" not in t and " MW" not in d


def test_the_cap_is_inclusive_at_5000():
    assert _segments(compose_title(*ROOMY, power_mw=5000))[-1] == "5,000 MW"
    assert compose_description(*ROOMY, power_mw=5000).startswith(
        "5,000 MW data center in Tulsa, Oklahoma.")


@pytest.mark.parametrize("mw", [None, 350])
@pytest.mark.parametrize("status", ["Operational", "operational",
                                    " OPERATIONAL ", "active", "Unknown"])
def test_operational_never_appears_in_a_title(status, mw):
    """People searching "equinix fr5 status / down" want outage news, and
    "Operational" in the title reads as a live status claim."""
    t = compose_title(*ROOMY, power_mw=mw, status=status)
    assert "operational" not in t.lower(), t
    assert _segments(t)[-1] == ("350 MW" if mw else "Tulsa, Oklahoma"), t


def test_the_city_is_not_repeated_when_the_name_carries_it():
    t = compose_title(*COUNCIL_BLUFFS)
    assert t == "Google Council Bluffs Data Center · Iowa | DC Hub"
    assert t.count("Council Bluffs") == 1
    # case-insensitively
    t = compose_title("Google Council Bluffs Data Center", "Google",
                      "council bluffs", "IA", "US")
    assert t.lower().count("council bluffs") == 1, t
    # and a city the name does NOT carry is still published
    assert _segments(compose_title(*PLAN_B))[1:] == ["Wellington, New Zealand"]


def test_a_region_the_site_name_carries_is_not_repeated():
    assert compose_title(*ANTHROPIC_NY) == \
        "Anthropic New York AI Campus | DC Hub"


def test_the_operator_prefix_does_not_suppress_the_region():
    """The trap hit while drafting: "is the region already in the title?"
    checked against the WHOLE lead finds "China" in the provider "China
    Telecom" and drops the country. Only the SITE part counts. Asserted on the
    location segment — the operator's name alone satisfies "China" in title."""
    t = compose_title("Beijing Data Center", "China Telecom", "Beijing", None,
                      "CN")
    assert t == "China Telecom Beijing Data Center · China | DC Hub"
    assert _segments(t)[1:] == ["China"]


# ── the description's rules ──────────────────────────────────────────────

def test_the_name_prefix_is_used_only_for_a_site_code_lead():
    # the title shows the SHORT lead "Equinix FR5" …
    assert _segments(compose_title(*FR5_KEEPER))[0] == "Equinix FR5"
    # … so the snippet names the facility in full, and goes on lower-case
    d = compose_description(*FR5_KEEPER, status="Operational")
    assert d.startswith(
        "Equinix FR5 - Frankfurt, KleyerStrasse: operational data center in "
        "Frankfurt, Germany."), d
    # a lead that IS the name gets no prefix, and a capital first letter
    d = compose_description(*PLAN_B, status="Operational")
    assert d.startswith("Operational data center in Wellington, New Zealand"), d
    d = compose_description(*COUNCIL_BLUFFS)
    assert d.startswith("Data center in Council Bluffs, Iowa."), d


def test_the_operator_is_named_only_when_real_and_not_in_the_name():
    # a brand-matched but DIFFERENT operator string is named
    assert ", operated by Compass Datacenters, on the WECC grid." in \
        compose_description(*COMPASS, iso="WECC")
    # an operator the name already contains is not
    assert "operated by" not in compose_description(*COUNCIL_BLUFFS)
    # the "Operator" placeholder and an empty provider are not operators
    for provider in ("Operator", "", None):
        d = compose_description("Pipeline Site", provider, "Tulsa", "OK", "US")
        assert "operated by" not in d, (provider, d)


def test_the_grid_and_time_to_power_in_months_and_in_years():
    d = compose_description(*COUNCIL_BLUFFS, power_mw=350,
                            status="Operational", iso="MISO",
                            time_to_power_months=19.2)
    assert ", on the MISO grid. ~19 months to power for new builds here." in d
    d = compose_description(*FR5_DUP, iso="ENTSOE-DE",
                            time_to_power_months=117.6)
    assert " ~9.8 years to power for new builds here." in d
    # the unit switches at 36 months
    assert " ~35 months to power" in compose_description(
        *ROOMY, time_to_power_months=35.4)
    assert " ~3.0 years to power" in compose_description(
        *ROOMY, time_to_power_months=36)
    # the sentinel never names a grid
    assert "grid" not in compose_description(*ROOMY, iso="UNK")
    # no DCPI verdict word (owner decision)
    d = compose_description(*COUNCIL_BLUFFS, iso="MISO",
                            time_to_power_months=19.2)
    for word in ("AVOID", "BUILD", "CAUTION", "verdict", "DCPI"):
        assert word not in d, word


def test_the_generation_sentence_is_the_fallback_not_an_addition():
    gen = " 1,628 MW of operating generation within 50 km."
    assert gen in compose_description(*MARS, nearby_generation_mw=1628)
    # time-to-power wins when both are known
    d = compose_description(*MARS, time_to_power_months=19,
                            nearby_generation_mw=1628)
    assert " ~19 months to power" in d and "operating generation" not in d
    # nothing known, nothing invented
    d = compose_description(*MARS, status="Operational")
    assert "operating generation" not in d and "to power" not in d, d
    for empty in (0, 0.0, -5, None, "n/a"):
        d = compose_description(*MARS, nearby_generation_mw=empty)
        assert "operating generation" not in d, (empty, d)
    # the radius is the caller's value, not a literal
    assert " within 25 km." in compose_description(
        *MARS, nearby_generation_mw=1628, radius_km=25)


def test_the_description_never_exceeds_160():
    # exactly at the limit: the approved Council Bluffs snippet is 160
    row, facts, _t, desc = APPROVED[0]
    assert len(compose_description(*row, **facts)) == 160 == len(desc)
    # a long operator sheds its clause first; the grid clause and the
    # time-to-power sentence still fit
    long_op = ("Vantage Data Centers Germany Holdings GmbH & Co. KG — "
               "Frankfurt Operations")
    d = compose_description("Vantage Frankfurt Campus", long_op,
                            "Frankfurt am Main", None, "DE", power_mw=1200,
                            status="Under Construction", iso="ENTSOE-DE",
                            time_to_power_months=117.6)
    assert d == ("1,200 MW under-construction data center in Frankfurt am "
                 "Main, Germany, on the ENTSOE-DE grid. ~9.8 years to power "
                 "for new builds here."), d
    # pathological rows still fit
    for row in (("Equinix FR5 - " + "Campus Building " * 20, "Equinix",
                 "Frankfurt", None, "DE"),
                ("Site", "Operator", "Z" * 300, None, "DE"),
                ("Vantage " + "Q" * 200, "Vantage " + "R" * 200, "Paris", None,
                 "FR")):
        d = compose_description(*row, power_mw=40, status="Operational",
                                iso="ENTSOE-DE", time_to_power_months=10)
        assert len(d) <= 160, (len(d), d)


@pytest.mark.parametrize("placeholder",
                         ["Regional", "Unknown", "N/A", "none", "Other"])
def test_a_placeholder_city_reaches_neither_text(placeholder):
    row = ("Shanwei Data Center", "China Telecom", placeholder, None, "CN")
    t = compose_title(*row, power_mw=20, status="Planned", iso="CSG")
    d = compose_description(*row, power_mw=20, status="Planned", iso="CSG",
                            time_to_power_months=30)
    for text in (t, d):
        assert placeholder.lower() not in text.lower(), text
    # and the country — the floor — is still published in both
    assert _segments(t)[1] == "China", t
    assert d.startswith("20 MW planned data center in China, on the CSG grid."), d


# ── through the page ─────────────────────────────────────────────────────

@pytest.fixture
def fpp(monkeypatch):
    """routes.facility_profile_page with a DB-less `main` stubbed in — and put
    back after the test by monkeypatch, so the stub cannot leak into another
    file (tests/test_facility_nearby_generation.py records why that matters)."""
    fake = types.ModuleType("main")
    fake.get_read_db = lambda: None
    fake.get_db = lambda: None
    monkeypatch.setitem(sys.modules, "main", fake)
    import routes.facility_profile_page as mod
    return mod


def _head(page, pattern):
    m = re.search(pattern, page)
    assert m, f"not rendered: {pattern}"
    return _html.unescape(m.group(1))


def _dcpi(iso, ttp):
    return {"market_slug": "test-market", "market_name": "Test Market",
            "iso": iso, "verdict": "AVOID", "excess_power_score": 40.0,
            "constraint_score": 70.0, "time_to_power_months": ttp}


PAGES = [
    # the fact, the region and the ISO drop: a plain lead
    (dict(name=COUNCIL_BLUFFS[0], provider="Google", city="Council Bluffs",
          state="IA", country="US", power_mw=350, status="Operational",
          latitude=41.2619, longitude=-95.8608,
          canonical_slug="google-google-council-bluffs-data-center-e6a28ba0"),
     _dcpi("MISO", 19.2), APPROVED[0],
     "Google Council Bluffs Data Center — Data Center"),
    # a site-code lead with the ISO shown ONCE, and the full-name prefix
    (dict(name=FR5_KEEPER[0], provider="Equinix", city="Frankfurt", state=None,
          country="DE", power_mw=None, status="Operational", latitude=50.1109,
          longitude=8.6821,
          canonical_slug="equinix-equinix-fr5-frankfurt-kleyerstrasse-3366f937"),
     _dcpi("ENTSOE-DE", 117.6), APPROVED[1],
     "Equinix FR5 — Frankfurt Data Center"),
]


@pytest.mark.parametrize("fac,dcpi,approved,og_title", PAGES,
                         ids=[p[0]["name"] for p in PAGES])
def test_the_page_emits_the_composed_title_and_description(
        fpp, monkeypatch, fac, dcpi, approved, og_title):
    monkeypatch.setattr(fpp, "_market_dcpi", lambda *a, **k: dict(dcpi))
    page = fpp._render_profile(dict(fac), fac["canonical_slug"])
    _row, _facts, title, desc = approved
    assert _head(page, r"<title>(.*?)</title>") == title
    assert _head(page, r'<meta name="description" content="([^"]*)">') == desc
    assert _head(page, r'<meta property="og:description" content="([^"]*)">') \
        == desc
    assert _head(page, r'<meta name="twitter:description" content="([^"]*)">') \
        == desc
    # what this change must NOT move
    assert _head(page, r'<meta property="og:title" content="([^"]*)">') \
        == og_title
    assert f"<strong>{_html.escape(fac['name'])}</strong> is a data center " \
           f"operated by {fac['provider']} in " in page
    assert 'content="index, follow"' in page


def test_the_generation_total_in_the_snippet_is_the_one_the_page_prints(
        fpp, monkeypatch):
    monkeypatch.setattr(fpp, "_market_dcpi", lambda *a, **k: None)
    fac = dict(name=MARS[0], provider="Mars Datacenter", city="Ankara",
               state="Ankara", country="TR", power_mw=None,
               status="Operational", latitude=39.9334, longitude=32.8597,
               canonical_slug="mars-datacenter-mars-datacenter-ankara-1-f7bc8f2f",
               _nearby_gen=[("wind", 12, 900.0), ("gas", 3, 500.0),
                            ("solar", 40, 228.0)])
    page = fpp._render_profile(dict(fac), fac["canonical_slug"])
    assert _head(page, r"<title>(.*?)</title>") == APPROVED[2][2]
    assert _head(page, r'<meta name="description" content="([^"]*)">') == \
        APPROVED[2][3]
    m = re.search(r"totalling ([\d,]+) MW within about (\d+) km", page)
    assert m and m.groups() == ("1,628", "50"), "the section's own total moved"
    # no section rendered -> no generation sentence, whatever the rows held
    fac["_nearby_gen"] = [("wind", 0, 900.0)]
    page = fpp._render_profile(dict(fac), fac["canonical_slug"])
    assert "Power generation nearby" not in page
    assert "operating generation" not in _head(
        page, r'<meta name="description" content="([^"]*)">')


SANTIAGO = [("utility-scale solar", 80, 1561.0), ("oil/gas", 3, 481.0),
            ("hydropower", 3, 396.0), ("bioenergy", 1, 14.0)]


@pytest.mark.parametrize("rows", [
    SANTIAGO,
    [("wind", 1, -3.0), ("gas", 1, 10.0)],
    [("", 3, 900.0)],
    [("wind", 0, 5.0)],
    [("wind", 2, 0.0)],
    [("solar", 1)],
    [("solar", 1, 2.0, "extra")],
    "not-a-list",
    None,
], ids=["santiago", "negative-row", "no-fuel", "no-units", "no-mw",
        "short-row", "long-row", "not-a-list", "none"])
def test_the_snippet_total_agrees_with_the_section_it_cites(fpp, rows):
    """Two spellings of one sum. _nearby_generation_html is compiled out of the
    AST on its own by tests/test_facility_nearby_generation.py, so it cannot
    call a shared helper without breaking there; this holds the two together
    on behaviour instead."""
    section = fpp._nearby_generation_html(rows, "Santiago", "CL")
    total = fpp._nearby_generation_total_mw(rows)
    assert bool(section) == (total is not None), (section, total)
    if section:
        assert f"totalling {total:,.0f} MW within" in section
