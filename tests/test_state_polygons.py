"""The state resolver must not need a network, and must not invent a state.

r-state-pip shipped Census point-in-polygon as the PRIMARY path with a
bounding-box FALLBACK for when geo.fcc.gov did not answer. Measured against
that build on 2026-08-29, with the service forced down:

    Toronto, Ontario  (43.6532, -79.3832)  ->  'NY'  basis 'bbox_unique'
    Ashburn, Virginia (39.0400, -77.4900)  ->  'MD'  basis 'bbox_ambiguous'

state_geometry's own docstring names the first case — "NEW YORK's bbox
contains TORONTO" — as the reason a rectangle is not the fix. The fallback
produced it anyway, under the MORE confident of the two bbox bases, because
only one box matched. The existing suite could not catch it: every
foreign-point test ran with the primary path UP (`census_stub`), and every
`census_down` test used a domestic point.

The fix ships the same Census geometry in the repo, so the fallback is
reached only on a deploy whose data file is missing. These tests pin that the
offline path is real — they run with `requests` sabotaged, so anything that
still resolves proves it never touched the network.
"""
import os

import pytest

from util import state_geometry as w
from util import state_polygons as poly
from water_drought_routes import _STATE_FIPS

ARTIFACT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'data', 'geo', 'us_state_boundaries.json.gz')


@pytest.fixture
def no_network(monkeypatch):
    """Any HTTP attempt is a test failure, not a slow path."""
    def explode(*a, **k):
        raise AssertionError('the resolver reached for the network')
    monkeypatch.setattr(w.requests, 'get', explode)
    w._cache.clear()


# ── the artifact itself ───────────────────────────────────────────────────
def test_the_boundary_artifact_ships():
    # A committed data file is the whole mechanism. If it stops being packaged
    # the resolver silently degrades to the HTTP path and then to rectangles,
    # which is the state this change exists to leave.
    assert os.path.exists(ARTIFACT), 'boundary dataset missing: %s' % ARTIFACT
    assert poly.load_error() is None, 'dataset present but unloadable: %s' % poly.load_error()
    meta = poly.boundary_source()
    assert 'cb_2023_us_state_500k' in meta['source']
    assert meta['resolution'] == '500k', (
        'resolution is load-bearing: 20m generalises borders by kilometres, '
        'which is the same order as the error being fixed')


def test_every_state_the_drought_api_accepts_has_polygon_geometry():
    # Mirrors the bbox coverage guard. _STATE_FIPS is what the USDM call
    # accepts, so it is the population the PRIMARY path has to cover — the old
    # centroid table held 44 of 51 and Delaware could never resolve to itself.
    have = {code for code, _bbox, _rings in poly._get_index()}
    missing = sorted(set(_STATE_FIPS) - have)
    assert not missing, 'no polygon geometry for: %s' % missing


def test_territories_are_carried_so_they_do_not_fall_through():
    # A point in San Juan must be PR, not the nearest mainland state. The
    # territories are excluded from _STATE_FIPS but must exist in the geometry
    # or they resolve to '' and, worse, could reach the bbox tier.
    assert poly.state_containing(18.4655, -66.1057) == 'PR'
    have = {code for code, _b, _r in poly._get_index()}
    assert poly.TERRITORY_CODES <= have


# ── the offline path is real ──────────────────────────────────────────────
OFFLINE_POINTS = [
    (41.140, -104.820, 'WY', 'Cheyenne — the original defect'),
    (40.585, -105.084, 'CO', 'Fort Collins — the other side of that line'),
    (39.040,  -77.490, 'VA', 'Ashburn — the largest DC market on earth'),
    (38.9072, -77.0369, 'DC', 'Washington DC — absent from the old table'),
    (39.158,  -75.524, 'DE', 'Dover — absent from the old table'),
    (44.2601, -72.5754, 'VT', 'Montpelier — absent from the old table'),
    (41.8240, -71.4128, 'RI', 'Providence — absent from the old table'),
    (61.2181, -149.9003, 'AK', 'Anchorage — absent from the old table'),
    (21.3069, -157.8583, 'HI', 'Honolulu — absent from the old table'),
    (44.900, -111.000, 'WY', 'the Yellowstone corner'),
    (45.050, -111.000, 'MT', 'the other side of it'),
    (36.900, -101.500, 'OK', 'Texas County, OKLAHOMA'),
]


@pytest.mark.parametrize('lat,lng,expected,where', OFFLINE_POINTS)
def test_resolves_with_no_network_at_all(lat, lng, expected, where, no_network):
    state, basis, _ = w.resolve_state(lat, lng)
    assert state == expected, '%s: got %r' % (where, state)
    assert basis == 'census_point_in_polygon', (
        '%s resolved via %r — a bbox basis here means the offline dataset did '
        'not answer and the rectangles did' % (where, basis))


# ── THE REGRESSION this file is named for ─────────────────────────────────
FOREIGN_POINTS = [
    (43.6532, -79.3832, 'Toronto, Ontario — inside NEW YORK\'s bounding box'),
    (45.5019, -73.5674, 'Montreal, Quebec'),
    (49.2827, -123.1207, 'Vancouver, British Columbia'),
    (42.3149, -83.0364, 'Windsor, Ontario — across the river from Detroit'),
    (48.8600,   2.3500, 'Paris, France'),
    (38.0000, -55.0000, 'mid-Atlantic, ~1,500 km offshore'),
    (0.0,       0.0,    'Null Island'),
]


@pytest.mark.parametrize('lat,lng,place', FOREIGN_POINTS)
def test_a_foreign_point_is_not_given_a_us_state_without_a_network(
        lat, lng, place, no_network):
    """The case the previous suite could not see.

    Its foreign-point test ran with the primary path UP, so it proved the
    SERVICE says 'nowhere' — never that the resolver says so on its own.
    """
    state, basis, _ = w.resolve_state(lat, lng)
    assert state == '', '%s resolved to US state %r' % (place, state)
    assert basis == 'outside_us', '%s got basis %r' % (place, basis)


def test_the_bbox_tier_is_what_used_to_answer_toronto():
    """Pins WHY the offline dataset is needed, not just that it works.

    If this ever stops returning a US state, the rectangles got fixed and the
    docstrings above should stop claiming they cannot be.
    """
    assert w.states_containing(43.6532, -79.3832) == ['NY'], (
        'Toronto no longer lands in exactly one state box — re-read whether '
        'the bbox tier still needs to be last resort')


# ── the three-value contract ──────────────────────────────────────────────
def test_empty_and_none_are_different_answers(monkeypatch):
    # '' means "nowhere in the US". None means "no geometry was consulted".
    # resolve_state routes them to opposite outcomes — outside_us versus the
    # bbox tier — so collapsing them re-opens the Canadian-coordinate hole.
    assert poly.state_containing(43.6532, -79.3832) == ''
    monkeypatch.setattr(poly, '_index', [])
    monkeypatch.setattr(poly, '_load_error', 'simulated')
    assert poly.state_containing(41.14, -104.82) is None


def test_a_missing_dataset_degrades_to_http_rather_than_to_rectangles(monkeypatch):
    # Ordering guard: with the artifact gone the HTTP service must be tried
    # BEFORE the boxes, because it is the only remaining source that can say
    # "nowhere".
    monkeypatch.setattr(poly, '_index', [])
    monkeypatch.setattr(poly, '_load_error', 'simulated')
    w._cache.clear()
    called = {}

    def fake_service(lat, lng):
        called['hit'] = (lat, lng)
        return 'WY'
    monkeypatch.setattr(w, '_state_from_census_service', fake_service)
    state, basis, _ = w.resolve_state(41.14, -104.82)
    assert called.get('hit') == (41.14, -104.82), 'the HTTP fallback was skipped'
    assert (state, basis) == ('WY', 'census_point_in_polygon')


# ── why one mutation could not be killed ──────────────────────────────────
def test_no_area_has_an_interior_hole():
    """Documents a deliberate mutation survivor.

    _ring_contains is counted EVEN-ODD across an area's rings, so an interior
    hole would subtract itself. Replacing that with first-ring-wins leaves the
    whole suite green — because this vintage contains no hole at all, and no
    real coordinate can tell the two apart.

    The even-odd form is kept anyway (it is the correct general algorithm and
    costs nothing), and the invariant it depends on is pinned here instead. If
    a future Census vintage introduces a hole this fails, which is the signal
    that the mutation has become killable and needs a real coordinate.

    Shapefile convention: outer rings are clockwise (negative shoelace area),
    holes counter-clockwise.
    """
    def signed_area(xs, ys):
        n = len(xs)
        return sum(xs[i] * ys[(i + 1) % n] - xs[(i + 1) % n] * ys[i]
                   for i in range(n)) / 2.0

    holes = [(code, i) for code, _bbox, rings in poly._get_index()
             for i, (_rb, xs, ys) in enumerate(rings) if signed_area(xs, ys) > 0]
    assert not holes, (
        'this vintage introduces interior holes (%s) — the even-odd branch in '
        '_ring_contains is now reachable and needs a coordinate that proves it'
        % holes[:5])
