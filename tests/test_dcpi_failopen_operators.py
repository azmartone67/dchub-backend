"""r-failopen-operators (2026-09-04) — no published market may be scored on
another grid's constants.

MEASURED, live, 2026-09-04. 326 of the 330 slugs linked from /dcpi were read
back from /api/v1/dcpi/scores. EIGHT carried `data_basis_source` =
"...(no ISO-specific calibration matched this market)", and every one of them
published WECC's numbers verbatim — curtailment_pct 7.5, reserve_margin_pct
20.0:

    anchorage          iso='AK'     Alaska Railbelt, an isolated grid
    honolulu, kapolei  iso='HECO'   Hawaiian Electric, five island grids
    johannesburg       iso=''       South Africa
    midrand            iso='UNK'    South Africa
    barueri, osasco    iso='UNK'    Brazil
    bologna            iso='UNK'    Italy

`iso_defaults.get(iso, iso_defaults["WECC"])` fails OPEN, so a market whose
operator has no row here is not marked unknown — it is confidently published
with Western-US mainland parameters. That is r-iso-defaults-southeast, which
put WECC's curtailment 7.5 and its "500 MW behind-the-meter industrial
headroom" on ~22 Southeast markets in 2026-07-28.

★★★ THE TWO WAYS IN ARE DIFFERENT, AND ONLY ONE WAS FENCED.

  1. A CURATED tuple naming an unregistered operator. Fenced since
     2026-09-03 by test_dcpi_latam_coverage's registration sweep.

  2. A RESOLVER MINTING a label the dict has no row for. NOT fenced — and it
     is how anchorage/honolulu/kapolei got there. util/iso_taxonomy's
     STATE_ISO maps AK->'AK' and HI->'HECO', and _normalize_us_isos applies
     it. That mapping is CORRECT: Alaska and Hawaii genuinely are not in the
     Western Interconnection, and rewriting them off WECC was a fix. It just
     moved them onto labels nothing anchored, so a fix for one defect handed
     them another — silently, because the fail-open has no error path.

     A resolver that MINTS labels and a dict that ANCHORS labels must agree on
     the same set. test_every_label_the_resolver_can_mint_has_anchors below is
     that agreement, and it is the assertion this file exists for.

  3. An ORPHAN ROW re-adopting its own bad ISO forever. barueri, osasco,
     bologna and midrand reach the recompute only through
     _load_scored_orphans, which feeds each score row's own (state, iso, lat,
     lon) back in — so 'UNK' rewrites itself every run. Same mechanism as
     r-orphan-geography, same fix: a hardcoded tuple, which wins in
     _build_markets_list.

Nothing here asserts through _is_intl_market or any other predicate under
test — ground truth is stated by hand, for the reason recorded in
tests/test_dcpi_latam_coverage.py.
"""
import ast
import os

import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402
from util.iso_taxonomy import STATE_ISO, MARKET_ISO_OVERRIDES  # noqa: E402


def _iso_defaults():
    """The function-local `iso_defaults` dict, parsed from source.

    It lives inside gather_metrics_for_market and cannot be imported. Parsing
    the real literal is the convention tests/test_dcpi_modeled_source.py set:
    a hand-kept copy would drift, and a drifted copy asserts about a dict
    nobody ships.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "routes", "dcpi.py"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.find("iso_defaults = {")
    assert i > 0, "iso_defaults literal not found - did it move or get renamed?"
    j = src.find("\n    }", i)
    assert j > i
    d = ast.literal_eval(src[i + len("iso_defaults = "):j] + "\n    }")
    assert len(d) > 50, f"only parsed {len(d)} ISO keys - parser is stale"
    return d


#: The eight markets measured on the fail-open, with the operator each should
#: carry. Ground truth, stated by hand.
#:
#: ★They divide by HOW the label reaches them, and the fix differs:
#:
#: PINNED — non-US markets the resolver cannot help (it only rewrites US
#: rows) and that survive solely via _load_scored_orphans, which re-adopts
#: each score row's own stored ISO every recompute. Only a hardcoded tuple
#: beats the re-adopter, so these need a row in _MARKETS_HARDCODED.
PINNED = [
    ("johannesburg", "GP",  "ESKOM"),
    ("midrand",      "GP",  "ESKOM"),
    ("barueri",      "BR",  "ONS"),
    ("osasco",       "BR",  "ONS"),
    ("bologna",      "IT",  "ENTSOE-IT"),
    # anchorage IS curated, and its tuple said "WECC" while
    # _normalize_us_isos rewrote it to "AK" on every build — the two sources
    # disagreed and the resolver won. Pinned to what actually ships.
    ("anchorage",    "AK",  "AK"),
]

#: RESOLVED — US markets with no curated tuple at all. STATE_ISO maps HI to
#: HECO and _normalize_us_isos applies it every build, so the LABEL was
#: already right; only the anchors were missing. Pinning them would add a
#: second source of truth for something the resolver already gets correct,
#: which is the drift r-iso-taxonomy exists to prevent.
RESOLVED = [
    ("honolulu", "HI", "HECO"),
    ("kapolei",  "HI", "HECO"),
]

FIXED = PINNED + RESOLVED

_HARD = {m[0]: m for m in dcpi._MARKETS_HARDCODED
         if isinstance(m, tuple) and len(m) >= 4}


# ── the assertion this file exists for ────────────────────────────────────

def test_the_known_gap_set_is_empty_and_stays_that_way():
    """★ The escape hatch, closed.

    tests/test_dcpi_iso_taxonomy.py::test_iso_defaults_gap_is_exactly_the_known
    _pre_existing_set is the CANONICAL sweep — it compares STATE_ISO's and
    MARKET_ISO_OVERRIDES' range against iso_defaults' keys. It is not
    duplicated here; one invariant, one home.

    What it cannot defend on its own is the shape of its own comparison. It
    asserts `gap == _KNOWN_ISO_DEFAULTS_GAP`, so the cheap way to make a
    future red build green is to append the new label to that set instead of
    adding anchors for it — turning "markets are silently scored on WECC" into
    a documented, permanent silence. That is how AK and HECO sat there.

    So: the sweep proves nothing is uncovered; this proves nothing is EXCUSED.
    """
    from tests import test_dcpi_iso_taxonomy as canon
    assert canon._KNOWN_ISO_DEFAULTS_GAP == frozenset(), (
        "operator labels have been added back to _KNOWN_ISO_DEFAULTS_GAP: "
        f"{sorted(canon._KNOWN_ISO_DEFAULTS_GAP)}. Every label in that set is "
        "a live market being scored on WECC's Western-US constants with no "
        "error anywhere. Add an iso_defaults row instead of widening the set.")


def test_the_canonical_sweep_still_covers_the_resolver_range():
    """Guards the sweep itself against being narrowed to nothing.

    A vacuous canonical check (empty range, or a parser that silently returns
    no keys) would pass while covering nothing, and this file would then be
    asserting an empty set equals an empty set.
    """
    mintable = {v for v in STATE_ISO.values() if v} | \
               {v for v in MARKET_ISO_OVERRIDES.values() if v}
    assert len(mintable) >= 10, (
        f"STATE_ISO/MARKET_ISO_OVERRIDES resolve to only {len(mintable)} "
        "labels — the canonical sweep would be near-vacuous")
    assert {"AK", "HECO"} <= mintable, (
        "the two labels this change closed are no longer mintable — if the "
        "resolver stopped emitting them, this file's premise changed")
    assert {"AK", "HECO"} <= set(_iso_defaults()), "and both must have anchors"


def test_every_operator_on_a_curated_market_has_anchors():
    """The sibling gap: a hand-written tuple naming an unregistered operator.

    The empty label is the deliberate no-operator convention and is excluded —
    but note that as of r-failopen-operators NO hardcoded row uses it, because
    an empty label is itself a fail-open.
    """
    defaults = _iso_defaults()
    missing = sorted({(m[0], m[3]) for m in dcpi._MARKETS_HARDCODED
                      if isinstance(m, tuple) and len(m) >= 4
                      and (m[3] or "").strip() and m[3] not in defaults})
    assert missing == [], f"curated markets with unanchored operators: {missing}"


# ── the eight markets ─────────────────────────────────────────────────────

@pytest.mark.parametrize("slug,state,iso", PINNED)
def test_pinned_market_carries_its_real_operator(slug, state, iso):
    assert slug in _HARD, (
        f"{slug} is not pinned in _MARKETS_HARDCODED, so _load_scored_orphans "
        f"re-adopts its own stored ISO every recompute and any correction is "
        f"overwritten within the day")
    row = _HARD[slug]
    assert row[3] == iso, f"{slug} operator drifted: {row[3]!r} != {iso!r}"
    assert row[2] == state, f"{slug} state drifted: {row[2]!r} != {state!r}"


@pytest.mark.parametrize("slug,state,iso", RESOLVED)
def test_resolved_market_gets_its_operator_from_the_resolver(slug, state, iso):
    """These have no curated tuple, and must not grow one.

    The resolver is already right about them; a tuple would be a second source
    of truth that can drift from it (exactly what anchorage did).
    """
    from util.iso_taxonomy import resolve_iso
    assert resolve_iso(slug, state, default="") == iso
    assert slug not in _HARD, (
        f"{slug} gained a curated tuple — STATE_ISO already resolves {state} "
        f"to {iso}, so this is a second source of truth that can drift")


@pytest.mark.parametrize("slug,state,iso", FIXED)
def test_market_is_not_scored_on_wecc(slug, state, iso):
    """The defect itself, stated as a value comparison.

    Not `iso != ''` — that is the implementation detail. The thing that was
    wrong is that these markets published WECC's numbers.
    """
    d = _iso_defaults()
    assert iso in d, f"{iso} has no anchors, so {slug} still falls through to WECC"
    wecc = d["WECC"]
    assert d[iso] != wecc, f"{slug}'s operator {iso} is a byte-copy of WECC"
    assert d[iso]["curtailment_pct"] != wecc["curtailment_pct"] or \
           d[iso]["reserve_margin_pct"] != wecc["reserve_margin_pct"], (
        f"{slug} would still publish WECC's curtailment 7.5 / reserve 20.0 — "
        "the two fields measured wrong on all eight markets")


@pytest.mark.parametrize("iso", ["AK", "HECO", "ESKOM"])
def test_new_operator_is_attributed(iso):
    """Every iso_defaults key must name the disclosures it was calibrated
    from, so a new operator cannot inherit a generic or wrong attribution."""
    assert iso in dcpi._ISO_MODELED_REFERENCE
    src = dcpi.modeled_source_for(iso, iso_default_matched=True)
    assert "analyst estimate" in src, "must never claim to be a measurement"
    assert dcpi._ISO_MODELED_REFERENCE[iso] in src


@pytest.mark.parametrize("iso", ["AK", "HECO", "ESKOM"])
def test_btm_stays_under_the_threshold_that_fabricates_an_opportunity(iso):
    """derive_top_signals emits '500 MW behind-the-meter industrial headroom'
    at `bh >= 500`, and WECC's btm_headroom_mw is exactly 500 — which is how
    that sentence was published for markets nobody had measured. A new
    operator must not land on it, and for an ISLAND or ISLANDED grid the
    absolute megawatts are the whole point."""
    assert _iso_defaults()[iso]["btm_headroom_mw"] < 500


def test_islanded_us_grids_are_not_treated_as_western_interconnection():
    """Alaska and Hawaii are US markets that are NOT in WECC.

    Both facts matter and they are separate: they must stay OUT of
    _INTL_ISO_LABELS (they are US), while still carrying their own anchors
    (they are not the Western Interconnection).
    """
    for iso in ("AK", "HECO"):
        assert iso not in dcpi._INTL_ISO_LABELS, f"{iso} is a US grid"
        assert iso in _iso_defaults(), f"{iso} still needs its own anchors"


def test_south_africa_is_registered_as_international():
    assert "ESKOM" in dcpi._INTL_ISO_LABELS
    assert dcpi._ISO_LABEL_COUNTRY["ESKOM"] == "ZA"
    for slug in ("johannesburg", "midrand"):
        assert dcpi._market_country(_HARD[slug][2], _HARD[slug][3], slug) == "ZA"
        assert dcpi._live_state_reads_allowed(_HARD[slug][2], _HARD[slug][3]) is False


def test_bolognas_state_is_a_country_not_a_province():
    """'BO' is the Bologna province code AND the ISO-3166 alpha-2 for BOLIVIA
    — the johannesburg 'GP'/Guadeloupe trap one row over. 'IT' matches milan
    and rome and cannot be read as somewhere else."""
    assert _HARD["bologna"][2] == "IT"
    assert _HARD["milan"][2] == "IT" and _HARD["rome"][2] == "IT"
