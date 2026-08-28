"""util/state_geometry.py — one place that turns a coordinate into a US state
(2026-08-28).

`get_water_risk` at (41.14, -104.82) returned state "CO" — with
current_drought_pct 95.9 and dominant_severity "Exceptional (D4)". Those are
the coordinates of CHEYENNE, WYOMING, about ten miles north of the Colorado
line, and the reading served was Colorado's. Nothing in the payload said the
state had been inferred at all.

The resolver behind it returned the NEAREST STATE CENTROID and performed no
containment test whatsoever. Cheyenne is ~194 km from Colorado's centroid and
~299 km from Wyoming's, so Colorado won. Every border in the country had the
same defect, and it worsened with the size and irregularity of the state.

That is the worst failure shape available: not an error a caller can handle,
but a confident wrong answer, on a number that feeds siting decisions.

Coverage was the other half of it. The old table held 44 entries — AK, HI, DE,
NH, RI, VT and DC had none — so a site in Delaware could never resolve to
Delaware. It always inherited a neighbour's water risk.

★ WHY A BOUNDING BOX IS NOT THE FIX ON ITS OWN. NEW YORK's bbox contains
TORONTO (lat 40.49-45.02, lon -79.77..-71.85). A rectangle cannot reject a
point in Ontario — the same blind spot that has put Canadian facilities inside
a "US" box before. So Census point-in-polygon is the PRIMARY path here and the
boxes below are an explicitly-declared FALLBACK, never a silent one. Callers
get the basis alongside the code and must not present a bbox-derived state as
a surveyed one.

★There were already EIGHT separate lat/lon->state tables in this repo when
this was written (eia_gas_bulk_loader.py, scripts/eia_gas_bulk_loader.py,
reveal_endpoints.py, power_plant_intel.py, infrastructure_discovery.py,
infrastructure_gaps.py, air_permitting_patch.py, air_permitting_extras.py,
routes/dcgi.py, and the one this replaced in water_drought_routes.py), with
three different tuple ORDERS between them. This is the shared one; new callers
should use it rather than adding a ninth. The existing ones are deliberately
NOT refactored here — that is a separate change with its own blast radius, and
the same posture util/state_codes.py took for state->FIPS.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# The mapping never moves, so a day is conservative. Bounded so a long-lived
# worker cannot grow this without limit.
_CACHE_TTL = timedelta(hours=24)
_CACHE_MAX = 4096
_cache: dict = {}


def _cache_get(key):
    hit = _cache.get(key)
    if hit and datetime.now(timezone.utc) - hit[1] < _CACHE_TTL:
        return hit[0]
    return None


def _cache_put(key, value):
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(min(_cache, key=lambda k: _cache[k][1]), None)
    _cache[key] = (value, datetime.now(timezone.utc))


def _haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# (min_lat, max_lat, min_lng, max_lng). All 50 states + DC. A state may carry
# more than one box: AK needs two because the Aleutians cross the dateline,
# and a single box spanning it would swallow most of the Pacific.
STATE_BOXES = {
    'AL': [(30.14, 35.01, -88.48, -84.89)], 'AR': [(33.00, 36.50, -94.62, -89.64)],
    'AK': [(51.20, 71.45, -179.15, -129.97), (51.20, 53.02, 172.42, 179.78)],
    'AZ': [(31.33, 37.01, -114.82, -109.04)], 'CA': [(32.53, 42.01, -124.49, -114.13)],
    'CO': [(36.99, 41.01, -109.07, -102.04)], 'CT': [(40.98, 42.06, -73.73, -71.78)],
    'DC': [(38.79, 39.00, -77.12, -76.91)],   'DE': [(38.45, 39.84, -75.79, -75.04)],
    'FL': [(24.39, 31.01, -87.64, -79.97)],   'GA': [(30.35, 35.01, -85.61, -80.83)],
    'HI': [(18.91, 22.24, -160.25, -154.80)], 'IA': [(40.37, 43.51, -96.64, -90.14)],
    'ID': [(41.98, 49.01, -117.25, -111.04)], 'IL': [(36.97, 42.51, -91.52, -87.01)],
    'IN': [(37.77, 41.77, -88.10, -84.78)],   'KS': [(36.99, 40.01, -102.06, -94.58)],
    'KY': [(36.49, 39.15, -89.58, -81.96)],   'LA': [(28.85, 33.03, -94.05, -88.75)],
    'MA': [(41.18, 42.89, -73.51, -69.85)],   'MD': [(37.88, 39.73, -79.49, -75.04)],
    'ME': [(42.97, 47.47, -71.09, -66.94)],   'MI': [(41.69, 48.31, -90.42, -82.12)],
    'MN': [(43.49, 49.39, -97.24, -89.48)],   'MO': [(35.99, 40.62, -95.78, -89.09)],
    'MS': [(30.17, 35.00, -91.66, -88.09)],   'MT': [(44.35, 49.01, -116.06, -104.03)],
    'NC': [(33.83, 36.59, -84.33, -75.45)],   'ND': [(45.93, 49.01, -104.06, -96.55)],
    'NE': [(39.99, 43.01, -104.06, -95.30)],  'NH': [(42.69, 45.31, -72.56, -70.60)],
    'NJ': [(38.92, 41.36, -75.57, -73.88)],   'NM': [(31.33, 37.01, -109.06, -102.99)],
    'NV': [(35.00, 42.01, -120.01, -114.03)], 'NY': [(40.49, 45.02, -79.77, -71.85)],
    'OH': [(38.40, 41.99, -84.83, -80.51)],   'OK': [(33.61, 37.01, -103.01, -94.42)],
    'OR': [(41.98, 46.30, -124.57, -116.46)], 'PA': [(39.71, 42.28, -80.53, -74.68)],
    'RI': [(41.14, 42.02, -71.87, -71.11)],   'SC': [(32.03, 35.22, -83.36, -78.53)],
    'SD': [(42.47, 45.95, -104.06, -96.43)],  'TN': [(34.98, 36.68, -90.32, -81.64)],
    'TX': [(25.83, 36.51, -106.65, -93.50)],  'UT': [(36.99, 42.01, -114.06, -109.03)],
    'VA': [(36.53, 39.47, -83.68, -75.23)],   'VT': [(42.72, 45.02, -73.44, -71.46)],
    'WA': [(45.54, 49.01, -124.85, -116.91)], 'WI': [(42.48, 47.32, -92.89, -86.75)],
    'WV': [(37.20, 40.64, -82.65, -77.71)],   'WY': [(40.99, 45.01, -111.06, -104.04)],
}


# The Census service answers in tens of milliseconds and the mapping never
# moves, so this is generous. The edge budget matters: callers typically run
# this BEFORE another upstream fetch, and admin/POST routes are cut off at 15s
# at the edge. urllib's timeout is per socket operation — connect AND read each
# get the full value — so the real worst case is roughly double the number
# below, which is why it is not 10.
CENSUS_TIMEOUT_S = 4
CENSUS_URL = 'https://geo.fcc.gov/api/census/block/find'


def state_from_census(lat, lng):
    """Authoritative point-in-polygon against Census TIGER geometry.

    Returns a two-letter code, '' for a point that is in no US state (ocean,
    Canada, Mexico, anywhere abroad), or None when the service could not be
    reached. The caller MUST distinguish the last two: '' is an answer, None
    is the absence of one. Conflating them is how a Canadian coordinate
    silently becomes a US state again.
    """
    key = 'census_state_%.4f_%.4f' % (lat, lng)
    hit = _cache_get(key)
    if hit is not None:
        return hit.get('state')
    try:
        qs = urllib.parse.urlencode({
            'latitude': lat, 'longitude': lng,
            'censusYear': 2020, 'format': 'json'})
        req = urllib.request.Request(
            CENSUS_URL + '?' + qs,
            headers={'User-Agent': 'DCHub/1.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=CENSUS_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except Exception as exc:                    # noqa: BLE001 - degrade, never 500
        logger.warning('Census state lookup failed for %s,%s: %s', lat, lng, exc)
        return None
    # A point outside every US state comes back with State.code = null and
    # status OK. That is a real answer ("nowhere"), and it is the one a bbox
    # can never give us — do not turn it into a guess.
    code = ((payload.get('State') or {}).get('code') or '')
    code = code.upper() if isinstance(code, str) else ''
    _cache_put(key, {'state': code})
    return code


def states_containing(lat, lng):
    """Every state whose bounding box contains the point. Fallback only."""
    return sorted(
        st for st, boxes in STATE_BOXES.items()
        for (s, n, w, e) in boxes
        if s <= lat <= n and w <= lng <= e)


def resolve_state(lat, lng):
    """Point -> (state_code, basis, alternatives).

    basis is one of:
      'census_point_in_polygon' — surveyed geometry; trust it
      'bbox_unique'             — Census unreachable, exactly one box contains it
      'bbox_ambiguous'          — several boxes do; nearest box-centre among
                                  THOSE wins and the rest are returned, so the
                                  caller can see the answer was not determined
      'outside_us'              — no US state contains the point
      'undetermined'            — Census unreachable AND no box matched

    A caller that shows the code without the basis is reintroducing the defect
    this module exists for: the whole point is that a guess must not look like
    a measurement.
    """
    if lat is None or lng is None:
        return '', 'undetermined', []
    code = state_from_census(lat, lng)
    if code:
        return code, 'census_point_in_polygon', []
    if code == '':
        return '', 'outside_us', []
    # code is None -> the service was unreachable. Rectangles from here on.
    hits = states_containing(lat, lng)
    if len(hits) == 1:
        return hits[0], 'bbox_unique', []
    if not hits:
        return '', 'undetermined', []
    best, best_d = None, float('inf')
    for st in hits:
        for (s, n, w, e) in STATE_BOXES[st]:
            if not (s <= lat <= n and w <= lng <= e):
                continue
            d = _haversine(lat, lng, (s + n) / 2.0, (w + e) / 2.0)
            if d < best_d:
                best_d, best = d, st
    return best, 'bbox_ambiguous', [st for st in hits if st != best]


BBOX_BASIS_NOTE = (
    'Census geometry was unreachable; this state came from rectangular '
    'bounding boxes, which overlap at state borders and extend past the '
    'national one. Treat it as unverified.')
