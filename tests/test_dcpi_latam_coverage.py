"""r-latam-coverage (2026-09-03) — the four LatAm markets, and the interlock
that keeps Bogota out of Colorado.

WHAT SHIPPED. bogota, mexico-city, santiago and sao-paulo each served 200 at
/markets/<slug> and 404 at /dcpi/<slug>, carrying 31/40/102/55 tracked
facilities, because they were absent from `_MARKETS_HARDCODED` — the only route
into the scoring universe for a non-US market (all three lite scorers are
hard-scoped `country IN ('US','USA')` behind a 2-letter USPS state regex).

★★★ WHY THIS FILE EXISTS RATHER THAN A FEW MORE ROWS IN THE EXISTING TESTS.
Adding the four tuples re-arms the r-country-code-collision defect through a
new door. Colombia's ISO-3166 code is 'CO', which is ALSO Colorado, so
`_is_intl_market`'s last rung (`state not in _US_STATE_CODES`) answers FALSE
for Bogota. Only the operator label 'XM' being in `_INTL_ISO_LABELS` makes the
label rung fire first.

MEASURED on 2026-09-03 by deleting "XM" from that frozenset:

    _is_intl_market(bogota tuple)        -> False
    _live_state_reads_allowed('CO','XM') -> True    # reads COLORADO
    _market_country('CO','XM','bogota')  -> 'US'    # asserts the US in JSON-LD
    _market_country_scope(...)           -> a US bounding-box query

...and all 45 tests in test_dcpi_country_code_state_collision.py,
test_dcpi_jsonld_country.py, test_dcpi_str_coverage_markets.py and
test_dcpi_market_country_scope.py STILL PASSED.

Two independent reasons they could not catch it, both worth keeping:

  1. test_dcpi_jsonld_country's registry sweep flags a row only when
     `country == "US" AND _is_intl_market(row)`. The mutation makes the SECOND
     term False as well, so the guard asks the very predicate that broke
     whether anything broke. A check that routes through the mechanism under
     test cannot witness that mechanism failing.
  2. test_dcpi_country_code_state_collision's CASES list is hand-written. A
     market added later is not in it, so the file is silent about every future
     market — which is exactly when this defect recurs.

So every assertion below reads the market TUPLES and the registration sets
DIRECTLY, and states each market's country as literal ground truth. Nothing
here calls _is_intl_market to decide what is true.
"""
import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402


#: Ground truth, stated by hand. NOT derived from any predicate under test.
#: (slug, display name, state field, ISO label, ISO-3166 country)
LATAM_MARKETS = [
    ("mexico-city", "Mexico City", "MX", "CENACE", "MX"),
    ("bogota",      "Bogotá",      "CO", "XM",     "CO"),
    ("santiago",    "Santiago",    "CL", "CEN",    "CL"),
    ("sao-paulo",   "São Paulo",   "BR", "ONS",    "BR"),
]

_BY_SLUG = {m[0]: m for m in dcpi._MARKETS_HARDCODED
            if isinstance(m, tuple) and len(m) >= 4}


def _iso_defaults():
    """The function-local `iso_defaults` dict, read from source and evaluated.

    It lives inside gather_metrics_for_market so it cannot be imported. Parsing
    the real literal is deliberate and is the convention
    tests/test_dcpi_modeled_source.py already uses: a hand-kept copy in a test
    would drift, and a drifted copy would assert about a dict nobody ships.
    """
    import ast, os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "routes", "dcpi.py"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.find("iso_defaults = {")
    assert i > 0, "iso_defaults literal not found - did it move or get renamed?"
    j = src.find("\n    }", i)
    assert j > i
    d = ast.literal_eval(src[i + len("iso_defaults = "):j] + "\n    }")
    assert len(d) > 40, f"only parsed {len(d)} ISO keys - parser is stale"
    return d


# ── the coverage gap itself ────────────────────────────────────────────────

@pytest.mark.parametrize("slug,name,state,iso,_country", LATAM_MARKETS)
def test_market_is_in_the_only_list_that_can_reach_a_non_us_market(
        slug, name, state, iso, _country):
    assert slug in _BY_SLUG, (
        f"{slug} is not in _MARKETS_HARDCODED. The dynamic loader and both "
        f"lite scorers filter country IN ('US','USA'), so this list is the "
        f"ONLY route in — dropping the tuple 404s /dcpi/{slug} again")
    row = _BY_SLUG[slug]
    assert (row[2], row[3]) == (state, iso), (
        f"{slug} state/ISO drifted: {(row[2], row[3])} != {(state, iso)}")


# ── the interlock (this is the test the mutation must break) ──────────────

@pytest.mark.parametrize("slug,_name,state,iso,_country", LATAM_MARKETS)
def test_no_latam_market_may_read_a_us_states_live_grid_tables(
        slug, _name, state, iso, _country):
    """★ The assertion that fails when 'XM' leaves _INTL_ISO_LABELS.

    `interconnect_queue` and `planned_generators` are US tables keyed on a
    2-letter USPS state code. None of these four markets is in the US, so none
    may read them — regardless of whether its country code happens to spell a
    US state.
    """
    assert dcpi._live_state_reads_allowed(state, iso) is False, (
        f"{slug} (state={state!r}, iso={iso!r}) is reading US state-keyed "
        f"live tables. queue depth drives queue_wait_months (40% of the "
        f"constraint weight) and gen additions carry 20% of the excess "
        f"weight, so this contaminates the published SCORE")


def test_colorado_is_the_trap_and_it_is_a_real_us_state():
    """Pins WHY the operator label has to decide, exactly as 'IN'/'ID' did.

    'CO' is a genuine USPS code, so no allow-list of US state codes can
    separate Bogota from Boulder. Same code, opposite answers, decided purely
    by the grid label.
    """
    assert "CO" in dcpi._US_STATE_CODES, "premise: CO is a real US state code"
    assert dcpi._live_state_reads_allowed("CO", "WECC") is True, (
        "a genuine Colorado market must still read Colorado's tables")
    assert dcpi._live_state_reads_allowed("CO", "XM") is False, (
        "Bogota must not — 'XM' is what separates them")


@pytest.mark.parametrize("slug,_name,state,iso,country", LATAM_MARKETS)
def test_country_is_the_real_one_and_never_the_united_states(
        slug, _name, state, iso, country):
    """Direct equality against hand-stated truth.

    Deliberately NOT `!= "US" or is_intl(...)`: under the mutation Bogota
    resolved to 'US' while _is_intl_market also went False, so any assertion
    phrased through that predicate stays green. == 'CO' does not.
    """
    assert dcpi._market_country(state, iso, slug) == country


@pytest.mark.parametrize("slug,_name,state,iso,_country", LATAM_MARKETS)
def test_facility_scope_excludes_us_rows(slug, _name, state, iso, _country):
    """A US-shaped scope fragment means the market is pooling US facilities."""
    frag, params = dcpi._market_country_scope(iso, state, *_BY_SLUG[slug][4:6])
    assert "NOT IN ('US', 'USA')" in frag, (
        f"{slug} got the US-market scope fragment: {frag!r} {params!r}")


# ── registration completeness: catches the NEXT market, not just these ────

def test_every_operator_label_in_the_market_list_is_registered():
    """Reads the tuples directly, so it fires for any future foreign market.

    A label in neither set is unregistered, and an unregistered label is what
    makes _is_intl_market fall through to the state code — the whole defect.
    The empty label is the deliberate midrand/johannesburg convention.
    """
    unregistered = sorted({
        (m[0], m[3]) for m in dcpi._MARKETS_HARDCODED
        if isinstance(m, tuple) and len(m) >= 4 and (m[3] or "").strip()
        and m[3] not in dcpi._US_DCPI_ISOS
        and m[3] not in dcpi._INTL_ISO_LABELS
    })
    assert unregistered == [], (
        "operator labels in _MARKETS_HARDCODED that are in neither "
        "_US_DCPI_ISOS nor _INTL_ISO_LABELS. Each falls through to the "
        "state-code rung of _is_intl_market: " + repr(unregistered))


# ── the modeled anchors must be real, not WECC's ──────────────────────────

@pytest.mark.parametrize("iso", ["XM", "CEN", "ONS", "CENACE"])
def test_operator_has_its_own_anchors_and_never_fails_open_to_wecc(iso):
    """r-iso-defaults-southeast, on another continent.

    `iso_defaults.get(iso, iso_defaults["WECC"])` fails OPEN, so a missing key
    publishes Western-US grid constants — that is how ~22 Southeast markets
    shipped WECC's curtailment_pct 7.5 and its 500 MW BTM opportunity.
    """
    defaults = _iso_defaults()
    assert iso in defaults, (
        f"{iso} has no iso_defaults row, so every market on it is scored on "
        f"WECC's Western-US constants and stamped 'iso_default_fail_open'")
    wecc = defaults["WECC"]
    assert defaults[iso] != wecc, f"{iso} is a byte-copy of WECC"
    for k in ("queue_wait_months", "reserve_margin_pct", "curtailment_pct",
              "queue_approval_rate_pct", "btm_headroom_mw"):
        assert k in defaults[iso], f"{iso} is missing {k}"


@pytest.mark.parametrize("iso", ["XM", "CEN", "ONS"])
def test_operator_is_attributed_and_not_left_to_a_generic_string(iso):
    """Every iso_defaults key must appear in _ISO_MODELED_REFERENCE, so a new
    operator cannot inherit a wrong or generic provenance line."""
    assert iso in dcpi._ISO_MODELED_REFERENCE
    src = dcpi.modeled_source_for(iso, iso_default_matched=True)
    assert "analyst estimate" in src, "must never claim to be a measurement"
    assert dcpi._ISO_MODELED_REFERENCE[iso] in src


@pytest.mark.parametrize("iso", ["XM", "CEN", "ONS"])
def test_btm_headroom_stays_under_the_threshold_that_fabricated_an_opportunity(iso):
    """derive_top_signals emits '500 MW behind-the-meter industrial headroom'
    at `bh >= 500`. WECC's btm_headroom_mw is exactly 500, which is how that
    sentence got published for ~22 markets that had never been measured. A new
    operator must not land on it by accident."""
    assert _iso_defaults()[iso]["btm_headroom_mw"] < 500


def test_cen_and_cenace_are_two_different_operators():
    """Chile's Coordinador Electrico Nacional and Mexico's CENACE share a
    prefix and nothing else — different country, different grid."""
    d = _iso_defaults()
    assert d["CEN"] != d["CENACE"]
    assert dcpi._ISO_LABEL_COUNTRY["CEN"] == "CL"
    assert dcpi._ISO_LABEL_COUNTRY["CENACE"] == "MX"
