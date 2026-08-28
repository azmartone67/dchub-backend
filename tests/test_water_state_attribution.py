"""r-state-pip (2026-08-28) — a coordinate must resolve to the state it is IN.

MEASURED LIVE against the deployed tool before the fix:

    get_water_risk lat=41.14 lon=-104.82   ->  state "CO"
                                               current_drought_pct 95.9
                                               dominant_severity "Exceptional (D4)"

Those are the coordinates of CHEYENNE, WYOMING, about ten miles north of the
Colorado line. The reading served was COLORADO's, with nothing in the payload
saying the state had been inferred at all.

★ THE MECHANISM. `_estimate_state` returned the NEAREST STATE CENTROID and
performed no containment test whatsoever. Cheyenne is ~194 km from Colorado's
centroid and ~299 km from Wyoming's, so Colorado won. Every border in the
country had the same defect, and it got worse the larger or more irregular the
state. This is the same class as the ISO-3166/USPS collision in
test_dcpi_country_code_state_collision.py: a plausible-looking identifier
resolved by proximity or namesake instead of by geometry.

★ COVERAGE was the other half. `_STATE_CENTERS` held 44 entries. AK, HI, DE,
NH, RI, VT and DC had none, so a site in Delaware could NEVER resolve to
Delaware — it always inherited a neighbour's water risk.

★ WHY A BOUNDING BOX IS NOT THE FIX ON ITS OWN, and why these tests check the
basis and not just the code. NEW YORK's bbox contains TORONTO. A rectangle
cannot reject a point in Ontario, which is the same blind spot that has put
Canadian facilities inside a "US" box before. So Census point-in-polygon is
the primary path and boxes are the declared fallback — and a bbox-derived
state must never come back looking like a surveyed one.

These tests exercise the RESOLVER, offline. The live Census probe is opt-in
(DCHUB_TEST_NETWORK=1) so CI stays deterministic.
"""
import os

import pytest

from util import state_geometry as w
from water_drought_routes import _STATE_FIPS, _estimate_state


@pytest.fixture(autouse=True)
def _clear_cache():
    w._cache.clear()
    yield
    w._cache.clear()


@pytest.fixture
def census_down(monkeypatch):
    """Force the bounding-box fallback: Census unreachable returns None."""
    monkeypatch.setattr(w, 'state_from_census', lambda lat, lng: None)


@pytest.fixture
def census_stub(monkeypatch):
    """Census answers from a table, so the primary path is tested with no network.

    Returns '' for a point in no US state — which is an ANSWER, distinct from
    the None that means "the service did not respond". Conflating those two is
    how a Canadian coordinate would silently become a US state again.
    """
    table = {
        (41.14, -104.82): 'WY', (40.585, -105.084): 'CO',
        (39.158, -75.524): 'DE', (21.31, -157.86): 'HI',
        (36.999, -109.045): 'CO',
        (43.65, -79.38): '', (48.86, 2.35): '',
    }
    monkeypatch.setattr(
        w, 'state_from_census',
        lambda lat, lng: table.get((round(lat, 6), round(lng, 6)), ''))


# ── THE REGRESSION ────────────────────────────────────────────────────────
def test_cheyenne_is_in_wyoming_not_colorado(census_stub):
    state, basis, alternatives = w.resolve_state(41.14, -104.82)
    assert state == 'WY', (
        'Cheyenne resolved to %r — the nearest-centroid bug is back, and '
        "get_water_risk is serving another state's drought reading" % state)
    assert basis == 'census_point_in_polygon'
    assert alternatives == []


def test_cheyenne_is_in_wyoming_even_when_census_is_down(census_down):
    # The fallback must fix the SAME bug, not just the happy path. A fix that
    # only holds while an upstream service is up is not a fix for a border.
    state, basis, _ = w.resolve_state(41.14, -104.82)
    assert state == 'WY', 'bbox fallback regressed to %r' % state
    assert basis == 'bbox_unique'


# ── near-border pairs, each side ──────────────────────────────────────────
# Every expectation below is GROUND TRUTH from the Census block service,
# queried by hand on 2026-08-28 and pasted with the county it named. Two of
# these were guessed wrong on the first pass — (44.90,-111.00) is the
# Yellowstone corner of WYOMING, not Idaho, and (36.90,-101.50) is Texas
# County, OKLAHOMA, not Texas — which is precisely the kind of mistake this
# resolver used to make silently and at scale.
BORDER_POINTS = [
    (41.140, -104.820, 'WY', 'Laramie County — 10 mi N of the CO line'),
    (40.585, -105.084, 'CO', 'Larimer County — the other side of it'),
    (42.050, -104.500, 'WY', 'Goshen County'),
    (41.900, -103.600, 'NE', 'Scotts Bluff County'),
    (45.050, -111.000, 'MT', 'Park County, MT'),
    (44.900, -111.000, 'WY', 'Park County, WY — the Yellowstone corner'),
    (36.900, -101.500, 'OK', 'Texas County, OKLAHOMA'),
    (37.100, -101.500, 'KS', 'Stevens County'),
    (39.100,  -75.400, 'DE', "Kent County"),
    (39.100,  -75.900, 'MD', "Queen Anne's County"),
    (42.100,  -71.500, 'MA', 'Worcester County'),
    (41.900,  -71.500, 'RI', 'Providence County'),
]


@pytest.fixture
def census_truth(monkeypatch):
    """The primary path, answering from the hand-verified table above."""
    table = {(lat, lng): st for lat, lng, st, _ in BORDER_POINTS}
    table.update({(43.65, -79.38): '', (48.86, 2.35): ''})
    monkeypatch.setattr(
        w, 'state_from_census',
        lambda lat, lng: table.get((round(lat, 6), round(lng, 6)), ''))


@pytest.mark.parametrize('lat,lng,expected,where', BORDER_POINTS)
def test_a_border_point_resolves_to_its_own_state(lat, lng, expected, where,
                                                  census_truth):
    state, basis, _ = w.resolve_state(lat, lng)
    assert state == expected, '%s: got %r' % (where, state)
    assert basis == 'census_point_in_polygon'


@pytest.mark.parametrize('lat,lng,expected,where', BORDER_POINTS)
def test_the_fallback_is_never_CONFIDENTLY_wrong(lat, lng, expected, where,
                                                 census_down):
    """The contract a rectangle can actually keep.

    Boxes cannot resolve every border — (41.90,-71.500) is in Rhode Island and
    also inside Massachusetts' box, and no rectangle can separate them. What
    the fallback must NEVER do is return the wrong state while claiming the
    unique-match basis. Either it is right, or it says the answer was not
    determined and names the state it missed. That is the difference between a
    fallback and the bug this file exists for.
    """
    state, basis, alternatives = w.resolve_state(lat, lng)
    if state == expected:
        return
    assert basis == 'bbox_ambiguous', (
        '%s: fallback returned %r (expected %r) with basis %r — a WRONG state '
        'presented as determined is exactly the original defect'
        % (where, state, expected, basis))
    assert expected in alternatives, (
        '%s: fallback returned %r and did not even list %r among %s'
        % (where, state, expected, alternatives))


# ── a point in no US state must not become one ────────────────────────────
@pytest.mark.parametrize('lat,lng,place', [
    (43.65, -79.38, 'Toronto, Ontario'),
    (48.86, 2.35, 'Paris, France'),
])
def test_a_foreign_point_is_not_given_a_us_state(lat, lng, place, census_stub):
    state, basis, _ = w.resolve_state(lat, lng)
    assert state == '', '%s resolved to US state %r' % (place, state)
    assert basis == 'outside_us'


def test_null_island_is_not_a_us_state(census_stub):
    # (0, 0) also pins the old truthiness bug in the caller: `if lat and lng`
    # treated a zero coordinate as absent.
    assert w.resolve_state(0.0, 0.0)[0] == ''


def test_the_resolver_never_falls_back_to_nearest_centroid(census_down):
    # THE ORIGINAL DEFECT, stated directly. Mid-Atlantic, ~1,500 km from the
    # US coast: no box contains it, so the honest answer is "we do not know".
    # The old resolver returned whichever centroid happened to be closest.
    state, basis, _ = w.resolve_state(38.0, -55.0)
    assert state == '', 'invented a state (%r) for a point in the ocean' % state
    assert basis == 'undetermined'


# ── the fallback must declare that it is a fallback ───────────────────────
def test_bbox_ambiguity_is_declared_rather_than_silently_resolved(census_down):
    # Dover, DE sits inside DE's box and its neighbours'. Picking one is fine;
    # picking one SILENTLY is the bug this whole file is about.
    state, basis, alternatives = w.resolve_state(39.158, -75.524)
    assert state == 'DE'
    assert basis == 'bbox_ambiguous'
    assert alternatives, 'the other candidate states were not reported'
    assert 'DE' not in alternatives


def test_bbox_and_census_bases_are_distinguishable(census_stub, monkeypatch):
    # A caller must be able to tell a surveyed state from a guessed one. If
    # both paths ever report the same basis string this guard is the only
    # thing standing between a rectangle and a cited figure.
    surveyed = w.resolve_state(41.14, -104.82)[1]
    w._cache.clear()
    monkeypatch.setattr(w, 'state_from_census', lambda lat, lng: None)
    guessed = w.resolve_state(41.14, -104.82)[1]
    assert surveyed != guessed
    assert surveyed.startswith('census')
    assert guessed.startswith('bbox')


# ── coverage ──────────────────────────────────────────────────────────────
def test_every_state_the_drought_api_accepts_has_geometry():
    # The 44-of-51 gap is why Delaware and Hawaii could never resolve to
    # themselves. _STATE_FIPS is what the USDM call accepts, so it is the
    # population the resolver has to cover.
    missing = sorted(set(_STATE_FIPS) - set(w.STATE_BOXES))
    assert not missing, 'no bounding box for: %s' % missing


def test_boxes_are_well_formed():
    for state, boxes in w.STATE_BOXES.items():
        assert boxes, '%s has no box' % state
        for (s, n, e_w, e_e) in boxes:
            assert s < n, '%s: min_lat %s not below max_lat %s' % (state, s, n)
            assert e_w < e_e, '%s: min_lng %s not below max_lng %s' % (state, e_w, e_e)
            assert -90 <= s <= 90 and -90 <= n <= 90, '%s: latitude out of range' % state
            assert -180 <= e_w <= 180 and -180 <= e_e <= 180, '%s: longitude out of range' % state


def test_back_compat_shim_returns_empty_rather_than_a_guess(census_down):
    # _estimate_state still exists for its other caller (the EIA gas-plant
    # fallback). It must return '' for an undeterminable point — if it ever
    # resumes guessing, that caller starts querying the wrong state's plants.
    assert _estimate_state(38.0, -55.0) == ''
    assert _estimate_state(41.14, -104.82) == 'WY'


# ── the primary path is really wired to Census ────────────────────────────
def test_the_census_lookup_targets_census_geometry():
    # Guards against the resolver quietly becoming bbox-only: the stubs above
    # would keep passing if _state_from_census were never called for real.
    assert 'geo.fcc.gov' in w.CENSUS_URL
    connect, read = w.CENSUS_TIMEOUT
    assert connect + read <= 8, (
        'the worst case here has to stay addable: this call runs BEFORE a USDM '
        'fetch that already allows 15s, under a 15s edge cut-off. It is a '
        '(connect, read) PAIR on purpose — a single number silently doubles')


@pytest.mark.skipif(os.environ.get('DCHUB_TEST_NETWORK') != '1',
                    reason='set DCHUB_TEST_NETWORK=1 to probe the live service')
def test_live_census_service_still_answers_wyoming_for_cheyenne():
    assert w.state_from_census(41.14, -104.82) == 'WY'
    assert w.state_from_census(43.65, -79.38) == '', 'Toronto is not a US state'
