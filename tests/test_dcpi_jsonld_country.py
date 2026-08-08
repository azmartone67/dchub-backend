"""GUARD — DCPI JSON-LD must not assert that every market is in the US.

The defect (live 2026-08-08): routes/dcpi.py emitted

    "spatialCoverage": {"@type": "Place",
                        "addressRegion": s['state'], "addressCountry": "US"}

for EVERY market. 61 non-US markets — Tokyo, Singapore, Frankfurt, Johor —
therefore asserted they were in the United States, in the one channel AI
engines lift verbatim into cited answers.

The market's own `state` field cannot substitute: for non-US markets it holds a
country code for most (DE, MY) but a SUBDIVISION for several — perth 'WA',
brisbane 'QL', montreal 'QC', johannesburg 'GP'. 'GP' is itself a valid
ISO-3166 code, for GUADELOUPE, so a naive state-as-country fallback does not
merely fail on Johannesburg — it confidently relocates it.

Pure tests, no database.
"""
import pytest

import routes.dcpi as dcpi


def _place(name, state, iso, slug=None):
    return dcpi._dcpi_place({"market_name": name, "state": state, "iso": iso}, slug)


# ── the regression itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("slug,name,state,iso,country", [
    ("tokyo",      "Tokyo",      "JP", "TEPCO",     "JP"),
    ("singapore",  "Singapore",  "SG", "EMA",       "SG"),
    ("frankfurt",  "Frankfurt",  "DE", "ENTSOE-DE", "DE"),
    ("johor",      "Johor",      "MY", "TNB",       "MY"),
    ("queretaro",  "Queretaro",  "MX", "CENACE",    "MX"),
    ("mumbai",     "Mumbai",     "IN", "POSOCO",    "IN"),
    ("jakarta",    "Jakarta",    "ID", "PLN",       "ID"),
])
def test_foreign_markets_do_not_claim_the_united_states(slug, name, state, iso, country):
    place = _place(name, state, iso, slug)
    assert place["addressCountry"] == country
    assert place["addressCountry"] != "US"


def test_us_markets_still_say_us():
    assert _place("Ashburn", "VA", "PJM", "ashburn")["addressCountry"] == "US"
    assert _place("Dallas", "TX", "ERCOT", "dallas")["addressCountry"] == "US"


def test_every_registered_market_resolves_and_none_lies():
    """Sweep the whole registry: no international market may resolve to US, and
    (today) every market resolves to something."""
    claiming_us, unresolved = [], []
    for m in dcpi._MARKETS_HARDCODED:
        if not (isinstance(m, tuple) and len(m) >= 4):
            continue
        slug, _name, state, iso = m[0], m[1], m[2], m[3]
        country = dcpi._market_country(state, iso, slug)
        if country is None:
            unresolved.append((slug, state, iso))
        elif country == "US" and dcpi._is_intl_market((None, None, state, iso)):
            claiming_us.append((slug, state, iso))
    assert claiming_us == [], "international markets still asserting US"
    assert unresolved == [], (
        "markets with no resolvable country — add the operator label to "
        "_ISO_LABEL_COUNTRY or the slug to _MARKET_COUNTRY_BY_SLUG")


# ── the traps that make `state` unusable as a country ───────────────────────

def test_subdivision_states_resolve_via_the_operator_label():
    """perth 'WA' is Western Australia, not Washington; brisbane 'QL' is not a
    country code at all. The AEMO label is what settles both."""
    assert _place("Perth", "WA", "AEMO", "perth")["addressCountry"] == "AU"
    assert _place("Brisbane", "QL", "AEMO", "brisbane")["addressCountry"] == "AU"


def test_canadian_provinces_are_canada_not_their_own_country():
    for state, iso in (("QC", "HQ"), ("ON", "IESO"), ("AB", "AESO"),
                       ("BC", "BCH"), ("MB", "MH")):
        assert _place("X", state, iso, "x")["addressCountry"] == "CA"


def test_johannesburg_is_south_africa_not_guadeloupe():
    """★ The case a state-as-country fallback gets confidently wrong: 'GP' is
    Gauteng here and Guadeloupe in ISO-3166, and this market has NO operator
    label to fall back on."""
    place = _place("Johannesburg", "GP", "", "johannesburg")
    assert place["addressCountry"] == "ZA"
    assert place["addressCountry"] != "GP"


def test_uk_markets_use_the_iso_code_gb_and_do_not_repeat_it_as_a_region():
    """ISO-3166 alpha-2 for the United Kingdom is GB. 'UK' is the registry's
    spelling of the COUNTRY, so it must not also appear as addressRegion."""
    place = _place("London", "UK", "NGESO", "london")
    assert place["addressCountry"] == "GB"
    assert "addressRegion" not in place


def test_nordpool_spans_four_countries_so_the_market_code_decides():
    """One operator label, four countries — the label cannot decide, and these
    markets do record their own ISO-3166 codes."""
    assert "NORDPOOL" not in dcpi._ISO_LABEL_COUNTRY
    for state in ("SE", "DK", "NO", "FI"):
        assert _place("X", state, "NORDPOOL", "x")["addressCountry"] == state


def test_us_territories_keep_their_own_code():
    assert _place("San Juan", "PR", "PREPA", "san-juan")["addressCountry"] == "PR"
    assert _place("Guam", "GU", "GPA", "guam")["addressCountry"] == "GU"


# ── never guess ─────────────────────────────────────────────────────────────

def test_unknown_market_omits_the_country_rather_than_inventing_one():
    """A Place with no country is honest; a Place in the wrong country is not."""
    assert dcpi._market_country("ZZ", "SOME-NEW-OPERATOR", "atlantis") is None
    place = _place("Atlantis", "ZZ", "SOME-NEW-OPERATOR", "atlantis")
    assert "addressCountry" not in place
    assert place["addressRegion"] == "ZZ"     # what we do know is still said
    assert place["@type"] == "Place"


def test_region_is_not_repeated_when_it_equals_the_country():
    place = _place("Frankfurt", "DE", "ENTSOE-DE", "frankfurt")
    assert "addressRegion" not in place


def test_region_survives_where_it_is_a_real_subdivision():
    assert _place("Ashburn", "VA", "PJM", "ashburn")["addressRegion"] == "VA"
    assert _place("Montreal", "QC", "HQ", "montreal")["addressRegion"] == "QC"


def test_no_hardcoded_us_country_left_in_the_jsonld_emitter():
    """Belt and braces on the emitter: the literal that shipped must be gone.
    Comment lines are stripped first so the note explaining the fix cannot
    satisfy — or trip — this check."""
    import inspect
    src = inspect.getsource(dcpi)
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert code.strip(), "comment-stripping ate the whole module"
    assert '"addressCountry": "US"' not in code
