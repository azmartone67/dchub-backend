"""Operator site-code titles on facility pages (2026-09-02, QA sweep
expansion #1 in findings/3_seo.md).

Measured (28d GSC query grain, 2026-08-02..29): "interxion mad1" pos 7.4,
"iad14 data center" 10.2, "fra28 data center" 10.4, "htl05" 10.7,
"digitalrealty ewr12 piscataway" 10.9, "ewr10" 7.7, "dus2" 12.5 — each
13–37 impressions, 0 clicks. Live title for the FR5 page on 2026-09-02:
"Equinix FR5 - Frankfurt, KleyerStrasse — Frankfurt, DE Data Center |
ENTSOE-DE grid | DC Hub".

Guard: 5 positives + 5 negatives on the detector, the headline composer, and
the RENDERED page (title, h1, og:title) with `main` stubbed the way
test_crossover_onramp does. Every assertion is mutation-verified (PR body).
"""
import re
import sys
import types

import pytest

if "main" not in sys.modules:
    sys.modules["main"] = types.SimpleNamespace(
        get_read_db=lambda: None, get_db=lambda: None)

from util.facility_site_code import (   # noqa: E402
    detect_site_code, detect_site_designator, site_code_headline,
    _clean_prefix, _state_token, DENY_PREFIXES, DENY_SUFFIX_WORDS)


POSITIVES = [
    # name, provider, city, expected code, expected headline
    ("Equinix FR5 - Frankfurt, KleyerStrasse", "Equinix", "Frankfurt",
     "FR5", "Equinix FR5 — Frankfurt Data Center"),
    ("Interxion MAD1", "Interxion", "Madrid",
     "MAD1", "Interxion MAD1 — Madrid Data Center"),
    ("Digital Realty IAD14", "Digital Realty", "Ashburn",
     "IAD14", "Digital Realty IAD14 — Ashburn Data Center"),
    ("Piscataway EWR12", "Digital Realty", "Piscataway",
     "EWR12", "Digital Realty EWR12 — Piscataway Data Center"),
    ("DataBank Dallas (DFW2)", "DataBank", "Dallas",
     "DFW2", "DataBank DFW2 — Dallas Data Center"),
    ("Interxion DUS2", "Digital Realty", "Düsseldorf",
     "DUS2", "Digital Realty Interxion DUS2 — Düsseldorf Data Center"),
    ("HTL05", "Equinix", "Hartlepool",
     "HTL05", "Equinix HTL05 — Hartlepool Data Center"),
]

NEGATIVES = [
    # name, provider, city — no code must be detected
    ("Building 3", "Meta", "Prineville"),
    ("Phase 2 Data Center", "Vantage", "Ashburn"),
    ("US1 Data Center", "ColoCo", "Miami"),               # US Route 1
    ("Data Center DC1", "Foo", "Bar"),                    # "Data Center 1"
    ("Equinix FR5 and FR8 campus", "Equinix", "Frankfurt"),  # two codes = campus
    ("Google Data Center Council Bluffs", "Google", "Council Bluffs"),
    ("AWS us-east-1", "Amazon Web Services", "Ashburn"),  # lower-case, hyphen
    ("Google Cloud US-EAST5", "Google", "Columbus"),      # compass region
    ("Node Pole SE1", "Node Pole", "Luleå"),              # price zone
    ("SH130 Corridor Site", "X", "Austin"),               # state highway
    ("Data Center 343593591", "", "West Chicago"),        # OSM junk id
    ("iad14 data center", "Digital Realty", "Ashburn"),   # lower-case never
]


@pytest.mark.parametrize("name,provider,city,code,headline", POSITIVES)
def test_detects_code_and_composes_headline(name, provider, city, code, headline):
    assert detect_site_code(name) == code
    assert site_code_headline(name, provider, city) == headline


@pytest.mark.parametrize("name,provider,city", NEGATIVES)
def test_rejects_non_codes(name, provider, city):
    assert detect_site_code(name) is None, name
    assert site_code_headline(name, provider, city) is None, name


def test_headline_needs_a_city_and_an_operator():
    """No city → no "<City> Data Center"; an unknown operator ("Operator"
    placeholder) with no brand in the name → nothing to lead with."""
    assert site_code_headline("Equinix FR5", "Equinix", "") is None
    assert site_code_headline("Equinix FR5", "Equinix", None) is None
    assert site_code_headline("HTL05", "Operator", "Hartlepool") is None
    assert site_code_headline("HTL05", "", "Hartlepool") is None
    # a brand in the name is enough when the provider column is empty
    assert site_code_headline("Equinix FR5", "", "Frankfurt") == \
        "Equinix FR5 — Frankfurt Data Center"


def test_deny_list_covers_the_documented_ambiguities():
    for pfx in ("US", "SH", "DC", "NO", "SE", "EU", "AI", "MW", "EAST"):
        assert pfx in DENY_PREFIXES, pfx


# ── r-site-code-tail: the designator riding on the code ──────────────
#
# Measured 2026-09-07 on the live publishable universe (39,739 rows): 237
# rendered-identity groups hold >=2 URLs with DIFFERING names, 229 of them
# because every member takes the site-code path and the tail was dropped. The
# pairs below are confirmed distinct buildings that rendered ONE <h1>.

DESIGNATORS = [
    # name, city, expected code, expected designator
    ("SecureIT DCB1.1", "Bettembourg", "DCB1", "DCB1.1"),
    ("SecureIT DCB1.2", "Bettembourg", "DCB1", "DCB1.2"),
    ("Equinix FR2.6", "Frankfurt am Main", "FR2", "FR2.6"),
    ("Equinix FR8.1", "Frankfurt am Main", "FR8", "FR8.1"),
    ("noris network AG ING1 ITA", "Ingolstadt", "ING1", "ING1 ITA"),
    ("noris network AG ING1 ITB", "Ingolstadt", "ING1", "ING1 ITB"),
    ("Centersquare IAD1-A", "Sterling", "IAD1", "IAD1-A"),
    ("Centersquare IAD1-B", "Sterling", "IAD1", "IAD1-B"),
    ("RIC3 DC1", "Sandston", "RIC3", "RIC3 DC1"),
    ("RIC3 DC5", "Sandston", "RIC3", "RIC3 DC5"),
    ("Centersquare Atlanta (ATL1-A/B/C)", "Lithia Springs", "ATL1", "ATL1-A/B/C"),
    ("Centersquare Northern Virginia (IAD1-C/E)", "Sterling", "IAD1", "IAD1-C/E"),
    ("Digital Realty Marseille MRS1/2/3/4", "Marseille", "MRS1", "MRS1/2/3/4"),
    ("Digital Realty Stockholm STO1-6", "Stockholm", "STO1", "STO1-6"),
    ("MICROSOFT BLUE RIDGE (IAD11-12-13) DATA CENTER", "ALDIE",
     "IAD11", "IAD11-12-13"),
    ("Flexential - Nashville/Cool Springs (NAS02/03)", "Franklin",
     "NAS02", "NAS02/03"),
]

# Tails that are a LOCATION or a legal form, not a building. Each one keeps
# rendering the bare code — dropping these is the whole point of
# r-site-code-title, and every one is a live name.
NON_DESIGNATORS = [
    ("Equinix FR5 - Frankfurt, KleyerStrasse", "Frankfurt", "FR5"),
    ("Equinix SG1 - Singapore", "Singapore", "SG1"),
    ("LADC1 - 624 S Grand Ave", "Los Angeles", "LADC1"),
    ("AirTrunk HKG1 Hong Kong", "Hong Kong", "HKG1"),
    ("DataBank Dallas (DFW2)", "Dallas", "DFW2"),
    ("365 Data Centers Nashville (NA1)", "Nashville", "NA1"),
    ("CORESITE REAL ESTATE OR1 LLC", "Hillsboro", "OR1"),
    ("NTT GLOBAL DATA CENTERS VA10 LLC", "Ashburn", "VA10"),
    ("O-NET ABOL1 CO", "Abuja", "ABOL1"),
    ("Mobily JED1 DC", "Jeddah", "JED1"),
    ("UIH BCH4 IDC - Bangkok, Thailand", "Bangkok", "BCH4"),
    ("CYRUSONE CHI6 FACILITY", "Chicago", "CHI6"),
    ("Hetzner Online FSN1 (Falkenstein)", "Falkenstein", "FSN1"),
    ("TYO3 Tokyo Data Center", "Tokyo", "TYO3"),
    ("STOKAB KN3, Kista", "Stockholm", "KN3"),
    ("Data4 Italia - Campus MIL01 - DC10", "Milan", "MIL01"),
    ("Matrix Data Center BM1 (MDC BM1)", "Jakarta", "BM1"),
    ("OVHcloud LIM1 Rechenzentrum", "Limburg", "LIM1"),
]


@pytest.mark.parametrize("name,city,code,ident", DESIGNATORS)
def test_designator_survives_the_collapse(name, city, code, ident):
    # the bare code is UNCHANGED — detect_site_code keeps its contract
    assert detect_site_code(name) == code
    assert detect_site_designator(name, city) == ident
    head = site_code_headline(name, "TestCo", city)
    assert head is not None, name
    assert head.endswith(f"{ident} — {city} Data Center"), head


@pytest.mark.parametrize("name,city,code", NON_DESIGNATORS)
def test_location_and_legal_tails_are_still_dropped(name, city, code):
    assert detect_site_code(name) == code
    assert detect_site_designator(name, city) == code, name
    head = site_code_headline(name, "TestCo", city)
    assert head is not None, name
    assert head.endswith(f"{code} — {city} Data Center"), head


def test_the_two_measured_pairs_no_longer_render_one_headline():
    """The defect this fixes, stated as the pair it was measured on."""
    for a, b, city in (("SecureIT DCB1.1", "SecureIT DCB1.2", "Bettembourg"),
                       ("noris network AG ING1 ITA",
                        "noris network AG ING1 ITB", "Ingolstadt"),
                       ("Centersquare IAD1-A", "Centersquare IAD1-B",
                        "Sterling"),
                       ("RIC3 DC1", "RIC3 DC2", "Sandston")):
        ha = site_code_headline(a, "TestCo", city)
        hb = site_code_headline(b, "TestCo", city)
        assert ha and hb and ha != hb, (a, b, ha, hb)


def test_a_designator_never_creates_a_headline_on_its_own():
    """Every negative in NEGATIVES has no code, so it has no designator —
    the suffix rule can only EXTEND a headline that already existed."""
    for name, _provider, city in NEGATIVES:
        assert detect_site_designator(name, city) is None, name


def test_a_hyphen_before_a_word_is_not_a_designator():
    """The trailing boundary in _SUFFIX_GLUED_RE: without it "FR5-Frankfurt"
    would read a designator "-F" out of the city name."""
    assert detect_site_designator("Equinix FR5-Frankfurt", "Frankfurt") == "FR5"
    assert detect_site_designator("Equinix FR5-FrankfurtWest", "Frankfurt") == "FR5"


def test_a_trailing_city_token_is_not_a_designator():
    """A short all-caps LOCATION token would stutter against the "— <City>"
    that follows it. Denied on THIS row's city, not on a word list."""
    assert detect_site_designator("Switch RNO1 RENO", "Reno") == "RNO1"
    assert detect_site_designator("Switch RNO1 RENO", "Las Vegas") == "RNO1 RENO"


def test_deny_suffix_words_carry_the_measured_three():
    for w in ("LLC", "CO", "DC"):
        assert w in DENY_SUFFIX_WORDS, w
    # and the designators that share their shape are NOT denied
    for w in ("DC1", "DC5", "ITA", "ITB"):
        assert w not in DENY_SUFFIX_WORDS, w


def test_the_rationale_queries_are_byte_identical_to_before():
    """r-site-code-title's own measured queries — none of these names carries
    a designator, so every one must render exactly what it rendered before."""
    assert site_code_headline("Interxion MAD1", "Interxion", "Madrid") == \
        "Interxion MAD1 — Madrid Data Center"
    assert site_code_headline("Digital Realty IAD14", "Digital Realty",
                              "Ashburn") == \
        "Digital Realty IAD14 — Ashburn Data Center"
    assert site_code_headline("Interxion FRA28", "Digital Realty",
                              "Frankfurt") == \
        "Digital Realty Interxion FRA28 — Frankfurt Data Center"
    assert site_code_headline("HTL05", "Equinix", "Hartlepool") == \
        "Equinix HTL05 — Hartlepool Data Center"
    assert site_code_headline("Interxion DUS2", "Digital Realty",
                              "Düsseldorf") == \
        "Digital Realty Interxion DUS2 — Düsseldorf Data Center"


# ── r-prefix-city-run: the city comes out as a run, not a word at a time ──
#
# Measured live 2026-09-07 over all 5,586 rows on this path: removing city
# words individually, anywhere they appeared, cut 69 prefixes in half. Every
# `head` below is taken from a real row's name.

# head, city, expected prefix — the city is a REFERENCE here and comes out
CITY_REMOVED = [
    ("Digital Realty Copenhagen ", "Copenhagen", "Digital Realty"),
    ("DataBank Dallas (", "Dallas", "DataBank"),                 # ")" boundary
    ("365 Data Centers Nashville (", "Nashville", "365"),
    ("CoreSite Chicago Data Center (", "Chicago", "CoreSite"),   # generic after
    ("Centersquare Elk Grove Village Data Center Campus ",
     "Elk Grove Village", "Centersquare"),
    ("Lewisville, TX, ", "Lewisville", "TX"),                    # comma boundary
    ("DartPoints Cincinnati, OH - ", "Cincinnati", "DartPoints OH"),
    ("QTS Irving - Dallas (", "Irving", "QTS Dallas"),           # dash boundary
    ("Piscataway ", "Piscataway", ""),
]

# head, city, expected prefix — the city STARTS a longer name, or is not a
# contiguous run at all. Cutting it out is the defect; the name stays whole.
CITY_KEPT = [
    # the reported case: Elk Grove Village is not Elk Grove
    ("ELK GROVE VILLAGE (", "ELK GROVE", "ELK GROVE VILLAGE"),
    ("Databank Houston West (", "Houston", "Databank Houston West"),
    ("Flexential - Las Vegas/Downtown (", "Las Vegas",
     "Flexential Las Vegas/Downtown"),
    ("Flexential - Salt Lake City/Cottonwood (", "Salt Lake City",
     "Flexential Salt Lake City/Cottonwood"),
    ("Cirion Santiago de Chile - ", "Santiago", "Cirion Santiago de Chile"),
    ("Brighton Digital Exchange ", "Brighton", "Brighton Digital Exchange"),
    ("NTT Berlin 1 Data Center (", "Berlin", "NTT Berlin 1"),
    ("LightEdge San Diego 1 (", "San Diego", "LightEdge San Diego 1"),
    ("icolo.io Maputo One (", "Maputo", "icolo.io Maputo One"),
    ("BR.Digital Foz do Iguaçu (", "Foz do Iguacu",   # accent: no run at all
     "BR.Digital Foz do Iguaçu"),
    ("CDC Auckland ", "Hobsonville, Auckland", "CDC Auckland"),
    ("Equinix Frankfurt ", "Frankfurt am Main", "Equinix Frankfurt"),
]


@pytest.mark.parametrize("head,city,want", CITY_REMOVED)
def test_a_bounded_city_run_comes_out(head, city, want):
    assert _clean_prefix(head, city) == want


@pytest.mark.parametrize("head,city,want", CITY_KEPT)
def test_a_city_that_starts_a_longer_name_stays_whole(head, city, want):
    assert _clean_prefix(head, city) == want


ELK = "ELK GROVE VILLAGE (CHI10-11-12) DATA CENTER"


def test_the_elk_grove_headline_end_to_end():
    """The reported defect, on the live row's own values: the discovered row
    carries provider NULL (-> "" here) and the legacy row repeats the name."""
    for provider in ("", ELK):
        head = site_code_headline(ELK, provider, "ELK GROVE")
        assert head == "ELK GROVE VILLAGE CHI10-11-12 — ELK GROVE Data Center", \
            (provider, head)
        assert not head.startswith("VILLAGE"), head
    # a genuinely distinct operator is still prepended (r-geo-facility-title)
    assert site_code_headline(ELK, "Microsoft", "ELK GROVE") == \
        "Microsoft ELK GROVE VILLAGE CHI10-11-12 — ELK GROVE Data Center"


def test_a_city_word_is_never_removed_from_inside_a_word_run():
    """The invariant, stated directly: every word of the prefix that survives
    keeps its neighbours' order, and no run is broken in the middle."""
    for head, city, want in CITY_KEPT:
        got = _clean_prefix(head, city)
        raw = [w.strip(" -–—:,.()[]|/") for w in head.split()]
        raw = [w for w in raw if w]
        # nothing was dropped out of the middle: the result is a prefix-run of
        # the original word order
        assert got.split() == raw[:len(got.split())], (head, got, raw)


# ── r-prefix-state-abbr: a bare postal code is a location ────────────
#
# Measured live 2026-09-07 over all 5,586 rows on this path: 54 rows / 27
# published URLs carry a bare two-letter abbreviation of their OWN state in
# the operator slot. Every one is CyrusOne ("<City>, <ST>, <CODE>") or
# DartPoints ("<Brand> <City>, <ST> - <CODE>"). Every head below is real.

# head, city, state, country, expected prefix
STATE_REMOVED = [
    ("Lewisville, TX, ", "Lewisville", "TX", "US", ""),
    ("Sterling, VA, ", "Sterling", "VA", "US", ""),
    ("Council Bluffs, IA, ", "Council Bluffs", "IA", "US", ""),
    ("Norwalk, CT, ", "Norwalk", "CT", "US", ""),
    ("DartPoints Cincinnati, OH - ", "Cincinnati", "OH", "US", "DartPoints"),
    ("DartPoints Baton Rouge, LA - ", "Baton Rouge", "LA", "US", "DartPoints"),
    ("DartPoints Spartanburg, SC - ", "Spartanburg", "SC", "US", "DartPoints"),
    # the city is NOT the row's city here (Ladson), so only the state goes
    ("DartPoints Charleston, SC - ", "Ladson", "SC", "US",
     "DartPoints Charleston"),
]

# The looser tests that would break these are the point of the narrow rule.
STATE_KEPT = [
    # a FULL-WORD region that is the operator's own name — 64 live rows
    ("DCI Indonesia (", "Bekasi, Cibitung", "Indonesia", "ID", "DCI Indonesia"),
    ("Digital Realty Osaka - ", "Ibaraki-city", "Osaka", "JP",
     "Digital Realty Osaka"),
    ("Cirion Cordoba - ", "Córdoba", "Cordoba", "AR", "Cirion Cordoba"),
    ("KIO Guatemala 1 (", "Guatemala City", "Guatemala", "GT",
     "KIO Guatemala 1"),
    # "DC" means Data Center on 94 live rows and is not in _US_STATE_ABBR
    ("Artnet DC Gdansk ", "Gdansk", "Gdańsk", "PL", "Artnet DC"),
    ("Artnet DC Gdansk ", "Gdansk", "DC", "US", "Artnet DC"),
    # a two-letter code that is NOT this row's state stays
    ("DartPoints Cincinnati, OH - ", "Cincinnati", "KY", "US",
     "DartPoints OH"),
    # ... and neither is it dropped outside the US
    ("FSIT Dietikon CH-", "Dietikon", "CH", "CH", "FSIT Dietikon CH"),
    # a brand that merely starts with a state-shaped word
    ("US Signal ", "Gilbert", "AZ", "US", "US Signal"),
]


@pytest.mark.parametrize("head,city,state,country,want", STATE_REMOVED)
def test_a_bare_postal_code_leaves_the_operator_slot(head, city, state,
                                                     country, want):
    assert _clean_prefix(head, city, state, country) == want


@pytest.mark.parametrize("head,city,state,country,want", STATE_KEPT)
def test_a_region_that_is_part_of_the_name_stays(head, city, state,
                                                 country, want):
    assert _clean_prefix(head, city, state, country) == want


def test_state_token_answers_only_about_this_row():
    """Not "is this a state" — "is THIS row's state a bare US postal code"."""
    assert _state_token("TX", "US") == "TX"
    assert _state_token("tx", "usa") == "TX"
    assert _state_token("TX", "CA") == ""          # not a US row
    assert _state_token("Texas", "US") == ""       # spelled out, not a code
    assert _state_token("Osaka", "JP") == ""
    assert _state_token(None, "US") == ""
    assert _state_token("", "") == ""
    # ★ "DC" is Data Center on 94 live rows and is deliberately not a state
    assert _state_token("DC", "US") == ""


def test_the_two_measured_shapes_end_to_end():
    assert site_code_headline("Lewisville, TX, DFW2", "CyrusOne",
                              "Lewisville", "TX", "US") == \
        "CyrusOne DFW2 — Lewisville Data Center"
    assert site_code_headline("DartPoints Cincinnati, OH - CVG1", "DartPoints",
                              "Cincinnati", "OH", "US") == \
        "DartPoints CVG1 — Cincinnati Data Center"


def test_state_is_optional_and_absent_means_the_old_behaviour():
    """The signature is additive: state/country default to None, and a caller
    that passes neither gets exactly what it got before this change. Asserted
    as the whole string, so a changed default cannot slip through."""
    assert site_code_headline("Lewisville, TX, DFW2", "CyrusOne",
                              "Lewisville") == \
        "CyrusOne TX DFW2 — Lewisville Data Center"
    assert site_code_headline("DartPoints Cincinnati, OH - CVG1", "DartPoints",
                              "Cincinnati") == \
        "DartPoints OH CVG1 — Cincinnati Data Center"
    # and the one production caller DOES pass them (util.facility_headline)
    import inspect
    import util.facility_headline as fh
    src = inspect.getsource(fh.facility_headline)
    assert "city, state, country)" in src, \
        "facility_headline stopped passing state/country to site_code_headline"


# ── the rendered page ────────────────────────────────────────────────

BASE = {
    "id": 4242, "state": "", "country": "DE", "region": None,
    "latitude": 50.11, "longitude": 8.68, "power_mw": 12,
    "status": "active", "address": "Kleyerstrasse 88",
}


def _render(name, provider, city, **kw):
    import routes.facility_profile_page as fpp
    fac = dict(BASE, name=name, provider=provider, city=city, **kw)
    return fpp._render_profile(fac, "equinix-equinix-fr5-3366f937")


def _title(html):
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1)


def _h1(html):
    return re.search(r"<h1>(.*?)</h1>", html, re.S).group(1)


def _og(html):
    return re.search(r'property="og:title" content="(.*?)"', html).group(1)


def test_rendered_page_leads_with_operator_and_code():
    html = _render("Equinix FR5 - Frankfurt, KleyerStrasse", "Equinix", "Frankfurt")
    # r-title-template (2026-09-09): the title carries the site-code LEAD and
    # the city in the template's separator ("Equinix FR5 · Frankfurt"); the
    # <h1> below still carries the full em-dash headline, which is what this
    # test is really about — the code must LEAD, wherever it is rendered.
    assert _title(html).startswith("Equinix FR5 · Frankfurt"), _title(html)
    assert _title(html).endswith("| DC Hub")
    assert _h1(html) == "Equinix FR5 — Frankfurt Data Center"
    assert _og(html) == "Equinix FR5 — Frankfurt Data Center"
    # the slug/canonical is NOT derived from the headline
    assert 'rel="canonical" href="https://dchub.cloud/facilities/equinix-equinix-fr5-3366f937"' in html
    # JSON-LD keeps the real facility name
    assert '"name": "Equinix FR5 - Frankfurt, KleyerStrasse"' in html


def test_rendered_page_without_a_code_uses_the_plain_template():
    """Was `..._is_byte_identical_to_the_legacy_title`. r-title-template
    (2026-09-09) retired the legacy shape for DISPLAY; the invariant this test
    actually holds is that a name carrying no site code takes the ordinary
    path, not the code path — so it leads with the name, not a code."""
    html = _render("Google Data Center Council Bluffs", "Google", "Council Bluffs")
    assert _title(html).startswith(
        "Google Data Center Council Bluffs · Council Bluffs | ")
    assert _h1(html) == "Google Data Center Council Bluffs"
    assert _og(html) == "Google Data Center Council Bluffs — Data Center"


def test_rendered_page_carries_the_designator():
    """The h1/title/og the crawler actually sees — two halls, two headlines."""
    a = _render("SecureIT DCB1.1", "SecureIT", "Bettembourg")
    b = _render("SecureIT DCB1.2", "SecureIT", "Bettembourg")
    assert _h1(a) == "SecureIT DCB1.1 — Bettembourg Data Center"
    assert _h1(b) == "SecureIT DCB1.2 — Bettembourg Data Center"
    assert _h1(a) != _h1(b) and _title(a) != _title(b)
    assert _og(a) == "SecureIT DCB1.1 — Bettembourg Data Center"
    # the JSON-LD still carries the real row name, untouched
    assert '"name": "SecureIT DCB1.1"' in a


def test_rendered_page_still_drops_the_location_tail():
    """r-site-code-title's own example — unchanged, byte for byte."""
    html = _render("Equinix FR5 - Frankfurt, KleyerStrasse", "Equinix", "Frankfurt")
    assert _h1(html) == "Equinix FR5 — Frankfurt Data Center"


def test_rendered_page_keeps_a_city_that_starts_a_longer_name():
    html = _render(ELK, None, "ELK GROVE")
    assert _h1(html) == "ELK GROVE VILLAGE CHI10-11-12 — ELK GROVE Data Center"
    assert "<h1>VILLAGE" not in html


def test_rendered_page_drops_a_bare_postal_code():
    html = _render("Lewisville, TX, DFW2", "CyrusOne", "Lewisville",
                   state="TX", country="US")
    assert _h1(html) == "CyrusOne DFW2 — Lewisville Data Center"
    assert "CyrusOne TX DFW2" not in html
    # the JSON-LD still carries the real row name
    assert '"name": "Lewisville, TX, DFW2"' in html


def test_rendered_page_with_two_codes_keeps_the_legacy_title():
    html = _render("Equinix FR5 and FR8 campus", "Equinix", "Frankfurt")
    assert _h1(html) == "Equinix FR5 and FR8 campus"
    assert "FR5 — Frankfurt Data Center" not in _title(html)
