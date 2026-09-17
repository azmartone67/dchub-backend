"""
DC Hub — Site Planner: Grid Interconnection Analysis Engine 2.0
============================================================
Pro-only feature. Provides instant site analysis for grid interconnection.

Endpoints:
  POST /api/v1/site-planner/analyze     — Full site analysis (Pro+)
  POST /api/v1/site-planner/compare     — Multi-site comparison (Pro+)
  GET  /api/v1/site-planner/queue-depth — ISO queue depth data (Pro+)
  GET  /api/v1/site-planner/score-config — Scoring weights (admin)

Data sources:
  - Neon PostgreSQL (substations, transmission_lines, power_plants via HIFLD)
  - ISO interconnection queue CSVs (cached weekly)
  - FEMA flood zone API
  - FWS critical habitat API

Drop-in: Import and register in main.py:
    from site_planner import register_site_planner_routes
    register_site_planner_routes(app)
"""

import os
import json
import math
import time
import logging
import traceback
from datetime import datetime, timedelta
from functools import wraps

from util.capacity_pipeline import CP_OK
from routes._slow_tool_cache import cache_tool_response

logger = logging.getLogger('site_planner')

# ─── Scoring Weights (tunable without redeploy) ─────────────────────────────
DEFAULT_SCORING_WEIGHTS = {
    'substation_proximity': {
        'weight': 0.25,
        'thresholds': {
            'excellent': {'max_miles': 3, 'points': 25},
            'good': {'max_miles': 8, 'points': 18},
            'fair': {'max_miles': 15, 'points': 10},
            'poor': {'max_miles': 999, 'points': 2},
        }
    },
    'substation_voltage': {
        'weight': 0.15,
        'thresholds': {
            '765kv_plus': {'min_kv': 500, 'points': 15},
            '345kv': {'min_kv': 345, 'points': 12},
            '230kv': {'min_kv': 230, 'points': 8},
            '138kv': {'min_kv': 138, 'points': 5},
            'below_138kv': {'min_kv': 0, 'points': 2},
        }
    },
    'queue_depth': {
        'weight': 0.20,
        'thresholds': {
            'light': {'max_mw': 500, 'points': 20},
            'moderate': {'max_mw': 1500, 'points': 12},
            'heavy': {'max_mw': 3000, 'points': 6},
            'congested': {'max_mw': 99999, 'points': 2},
        }
    },
    'transmission_proximity': {
        'weight': 0.15,
        'thresholds': {
            'excellent': {'max_miles': 1, 'points': 15},
            'good': {'max_miles': 3, 'points': 11},
            'fair': {'max_miles': 8, 'points': 6},
            'poor': {'max_miles': 999, 'points': 2},
        }
    },
    'environmental': {
        'weight': 0.15,
        'thresholds': {
            'clear': {'max_risk_score': 20, 'points': 15},
            'low_risk': {'max_risk_score': 40, 'points': 11},
            'moderate_risk': {'max_risk_score': 65, 'points': 6},
            'high_risk': {'max_risk_score': 100, 'points': 2},
        }
    },
    'congestion': {
        'weight': 0.08,
        'thresholds': {
            'low': {'max_density': 30, 'points': 8},
            'moderate': {'max_density': 60, 'points': 5},
            'high': {'max_density': 100, 'points': 2},
        }
    },
    'gas_access': {
        'weight': 0.06,
        'thresholds': {
            'excellent': {'max_miles': 3, 'points': 6},
            'good': {'max_miles': 10, 'points': 4},
            'fair': {'max_miles': 20, 'points': 2},
            'limited': {'max_miles': 999, 'points': 0},
        }
    },
    'dc_corridor': {
        'weight': 0.06,
        'thresholds': {
            'strong': {'min_count': 5, 'points': 6},
            'moderate': {'min_count': 2, 'points': 4},
            'weak': {'min_count': 1, 'points': 2},
            'none': {'min_count': 0, 'points': 0},
        }
    },
}

# ─── ISO/RTO Reference Data ─────────────────────────────────────────────────
ISO_REGIONS = {
    'ERCOT': {
        'states': ['TX'],
        'avg_queue_wait_years': 4.2,
        'queue_depth_gw': 380,
        'queue_url': 'https://www.ercot.com/gridinfo/resource/generation_interconnection',
    },
    'PJM': {
        'states': ['PA','NJ','DE','MD','VA','WV','OH','IN','IL','MI','KY','NC','DC','TN'],
        'avg_queue_wait_years': 5.1,
        'queue_depth_gw': 450,
        'queue_url': 'https://www.pjm.com/planning/services-requests/interconnection-queues',
    },
    'MISO': {
        'states': ['MN','WI','IA','MO','AR','MS','LA','ND','SD','MT'],
        'avg_queue_wait_years': 4.8,
        'queue_depth_gw': 520,
        'queue_url': 'https://www.misoenergy.org/planning/generator-interconnection/',
    },
    'CAISO': {
        'states': ['CA'],
        'avg_queue_wait_years': 3.9,
        'queue_depth_gw': 280,
        'queue_url': 'https://www.caiso.com/planning/Pages/GeneratorInterconnection/',
    },
    'SPP': {
        'states': ['KS','OK','NE','NM','WY'],
        'avg_queue_wait_years': 3.5,
        'queue_depth_gw': 190,
        'queue_url': 'https://www.spp.org/engineering/generator-interconnection/',
    },
    'ISO-NE': {
        'states': ['MA','CT','RI','NH','VT','ME'],
        'avg_queue_wait_years': 4.0,
        'queue_depth_gw': 95,
        'queue_url': 'https://www.iso-ne.com/system-planning/interconnection-service/',
    },
    'NYISO': {
        'states': ['NY'],
        'avg_queue_wait_years': 5.5,
        'queue_depth_gw': 120,
        'queue_url': 'https://www.nyiso.com/interconnections',
    },
    'SERC': {
        'states': ['GA','AL','SC','FL'],
        'avg_queue_wait_years': 3.8,
        'queue_depth_gw': 210,
        'queue_url': None,
    },
    'WECC': {
        'states': ['AZ','NV','UT','CO','OR','WA','ID','HI'],
        'avg_queue_wait_years': 3.6,
        'queue_depth_gw': 320,
        'queue_url': 'https://www.wecc.org/SystemStabilityPlanning/Pages/default.aspx',
    },
    'Non-ISO Southeast': {
        'states': ['AK'],
        'avg_queue_wait_years': 2.5,
        'queue_depth_gw': 10,
        'queue_url': None,
    },
}


# ─── Helper: Get DB connection ───────────────────────────────────────────────
def get_neon_connection():
    """Get a PostgreSQL connection to Neon. Uses the same pattern as main.py."""
    try:
        import psycopg2
        db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
        # Clean prefix if present (known Replit issue)
        import re
        db_url = re.sub(r'^[A-Z_]+=', '', db_url).strip("'\"")
        if not db_url:
            logger.error("No NEON_DATABASE_URL configured")
            return None
        conn = psycopg2.connect(db_url, connect_timeout=4)
        return conn
    except Exception as e:
        logger.error(f"Neon connection failed: {e}")
        return None


def execute_query(query, params=None, fetchone=False):
    """Execute a read query against Neon and return results as list of dicts."""
    conn = get_neon_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            columns = [desc[0] for desc in cur.description] if cur.description else []
            if fetchone:
                row = cur.fetchone()
                return dict(zip(columns, row)) if row else None
            rows = cur.fetchall()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.error(f"Query error: {e}\nQuery: {query}\nParams: {params}")
        return None
    finally:
        conn.close()


# ─── Core Analysis Functions ─────────────────────────────────────────────────

def find_nearest_substations(lat, lng, limit=5, max_distance_miles=25):
    """
    Find the nearest substations in the substations table, nearest first.
    PostGIS is not installed in Neon, so this is a Haversine query behind a
    bounding-box pre-filter. The table is the only source: there is no live
    fallback.

    Returns the rows nearest first; [] when the query ran and found no substation
    in the box; None when it did not run (execute_query logged a query error or a
    missing connection). None is not "no substation nearby": nothing near the site
    was looked at. See the end.
    """
    # Skip PostGIS (not installed in Neon) — go straight to Haversine

    # Fallback: Haversine with bounding box pre-filter (FAST)
    # 1 degree lat ≈ 69 miles, 1 degree lng ≈ 69 * cos(lat) miles
    deg_lat = max_distance_miles / 69.0
    deg_lng = max_distance_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    haversine_query = """
        SELECT 
            name,
            state,
            COALESCE(voltage_kv, 0) as voltage_kv,
            operator,
            lat,
            lng,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(lat)) *
                        cos(radians(lng) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(lat))
                    ))
                )
            ) as distance_miles
        FROM substations
        WHERE lat IS NOT NULL 
          AND lng IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    result = execute_query(haversine_query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng,
        limit
    ))
    
    if result:
        return result

    # ★ 2026-09-13 — no live fallback. When this query found no rows or did not run,
    # the lookup used to POST the same box to overpass.kumi.systems (urllib,
    # timeout=3) and serve OpenStreetMap rows tagged source 'OpenStreetMap'. It was
    # removed, not repointed:
    #  - It cost its whole timeout and returned nothing. The host accepted
    #    connections and answered none of 12 requests from a Mac within 3 s or 20 s;
    #    from Railway, 3 of 4 timed out at 30 s and the fourth took ~3 s to report
    #    an empty box.
    #  - overpass-api.de is no steadier from Railway (a 504 after ~12 s, 200s after
    #    1.3-4.4 s), and the backend's OSM loaders log 504s, 429s and 30 s read
    #    timeouts against it from the same egress. A request path would compete
    #    with them for the same per-IP slots.
    #  - An answer would not be the nearest. `out tags center qt 10` returns the
    #    first ten elements in quadtile order: around Ashburn, VA the box holds 380
    #    OpenStreetMap substations, the nearest 0.78 mi away, and the nearest of
    #    the ten returned was 16.1 mi.
    #  - Its rows scored missing tags as readings. No voltage tag became
    #    voltage_kv 0, the lowest voltage tier and a shallower queue estimate; no
    #    name became 'Substation', which the name dedupe in analyze and compare
    #    collapses to one row.

    # [] and None are different answers, and every caller has to keep them apart.
    # [] is a measurement: the query ran and the table holds no substation within
    # max_distance_miles. None is not: the query did not run, so nothing near the
    # site was looked at. The composite score declares power_grid unavailable
    # instead of scoring it, analyze and compare mark their substations
    # unavailable, and the site report does not print "No mapped substation".
    #
    # ★ 2026-09-13 — this returned [] on both paths, so a query error or a missing
    # connection was served as "no substation within 25 mi". The composite score
    # then published power_grid as validated (compute_suitability_score always
    # returns a number) with the substation proximity, voltage and queue depth
    # points silently scored as absent.
    return None if result is None else []


# What the site planner says in place of substations when find_nearest_substations
# returned None. No trailing period: the composite score's caveats append one.
SUBSTATIONS_NOT_MEASURED = (
    "substation lookup did not run: DC Hub's substations table could not be queried, "
    "so no substation near the site was measured (which is not the same as none being "
    "there)")


def _substations_coverage(found, miles=25):
    """(coverage, basis) for what find_nearest_substations returned to a caller that
    searched `miles` around the site, in the composite score's vocabulary."""
    if found is None:
        return 'unavailable', SUBSTATIONS_NOT_MEASURED
    return 'validated', f"DC Hub substations table (HIFLD), within {miles} mi of the site"


# What the site planner says in place of a transmission line when
# find_nearest_transmission_measured reports that it measured nothing. No trailing
# period, for the same reason.
TRANSMISSION_NOT_MEASURED = (
    "transmission lookup did not complete: DC Hub's substations or transmission_lines "
    "table could not be queried, so no transmission line near the site was measured "
    "(which is not the same as none being there)")


def _transmission_coverage(measured, miles=15):
    """(coverage, basis) for what find_nearest_transmission_measured reported to a
    caller that searched `miles` around the site."""
    if not measured:
        return 'unavailable', TRANSMISSION_NOT_MEASURED
    return 'validated', (f"DC Hub transmission_lines table (EIA), anchored at substations "
                         f"within {miles} mi of the site")


# A line's two endpoint substations sit within this many miles of each other in
# all but the tail of the EIA set: of 51,948 lines whose endpoint names each occur
# at one place in `substations`, half span 2.1 mi and 99% span 42.5 mi
# (production rows, 2026-09-13). _line_is_at reads a same-named substation
# farther away than this as a different substation.
_TX_ENDPOINT_MILES = 50.0

# How many of the nearest substation rows step 1 hands to step 2. Over 2,025
# sampled sites at 15 and 25 mi, the anchoring substation was at most row 88.
_TX_NEARBY_SUBSTATIONS = 500


def _haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle miles by the substation queries' own formula (3959 * acos)."""
    v = (math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.cos(math.radians(lng2) - math.radians(lng1))
         + math.sin(math.radians(lat1)) * math.sin(math.radians(lat2)))
    return 3959 * math.acos(max(-1.0, min(1.0, v)))


def find_nearest_transmission(lat, lng, max_distance_miles=15):
    """
    Find the transmission line anchored nearest the site: substations inside the
    radius box, nearest first, matched by exact name to line endpoints, keeping a
    line only where its endpoint names place it at that substation.
    distance_miles is the anchoring substation's distance.
    Returns None when nothing matches, and also when a statement the lookup needs
    did not run. A caller that has to tell those apart reads
    find_nearest_transmission_measured, which does the work. There is no live
    fallback; see the end of that function.
    """
    return find_nearest_transmission_measured(lat, lng, max_distance_miles)[0]


def find_nearest_transmission_measured(lat, lng, max_distance_miles=15):
    """
    find_nearest_transmission, and whether the lookup measured anything:
      (line, True)  a line anchored near the site
      (None, True)  every statement it needed ran, and no line is anchored there
      (None, False) a statement did not run (execute_query logged a query error or
                    a missing connection), so nothing near the site was measured
    """
    # ★ 2026-09-13 — measured, or not measured. execute_query returns None for a
    # statement that did not run, and this lookup read that as a miss at each of its
    # three statements: step 1 as no substation nearby, step 2 as no line
    # (`if not lines`), step 3 by returning None outright. So a query error or a
    # missing connection was served as "no transmission line near the site". The
    # composite score published power_grid as validated with transmission proximity
    # (up to 15 of its points) scored as absent, and its memo kept that for 6 h;
    # analyze and compare served null either way; the site report filled the line in
    # from the substation's own voltage and operator, after its 6 s cap as well.
    # find_nearest_transmission still answers None for both, the miss contract
    # tests/test_transmission_readers_sql.py pins; every caller reads this instead.

    # Step 1: substations near the site, nearest first
    deg_lat = max_distance_miles / 69.0
    deg_lng = max_distance_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))

    nearby_subs_query = """
        SELECT name, lat, lng,
            (3959 * acos(
                LEAST(1.0, GREATEST(-1.0,
                    cos(radians(%s)) * cos(radians(lat)) *
                    cos(radians(lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(lat))
                ))
            )) as distance_miles
        FROM substations
        WHERE lat IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
          AND name IS NOT NULL
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    subs = execute_query(nearby_subs_query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng,
        _TX_NEARBY_SUBSTATIONS
    ))

    if subs is None:
        return None, False

    tx, measured = _anchored_transmission_line(subs) if subs else (None, True)
    if tx or not measured:
        return tx, measured

    # No live fallback: a miss returns (None, True), and every caller handles that.
    # The site report shows the substation's own voltage and operator, the scorer
    # awards no transmission points, and analyze and compare serve null with
    # transmission_coverage 'validated'. A statement that did not run returns
    # (None, False) instead, above and in _anchored_transmission_line: the composite
    # score declares power_grid unavailable and does not memoise it, analyze and
    # compare serve transmission_coverage 'unavailable', and the site report prints
    # the line as not measured.
    #
    # ★ 2026-09-13 — _query_hifld_transmission_live used to run here on every
    # miss, the common case, behind paid routes. It was removed, not repointed.
    # It asked the superseded services1 HIFLD layer for a field that layer does
    # not have (SHAPE_Leng), so ArcGIS answered with error 400 "'outFields'
    # parameter is invalid" and it returned None after the round trip. Fixing
    # the field would not have helped: its lon/lat envelope carried no inSR and
    # both layers are Web Mercator, so it matched 0 lines around Ashburn (198 on
    # services1 and 256 on the EIA layer with inSR=4326). Had it succeeded, its
    # distance_miles of 'N/A (live query)' would have raised in
    # compute_suitability_score's float() and failed the composite score's
    # power_grid factor.
    #
    # Pointing it at the EIA layer would add no lines: transmission_lines is
    # ingested from that layer, by a GitHub runner, because Railway's egress to
    # ArcGIS is unreliable (routes/transmission_ingest.py). And its query asked a
    # different question. The highest voltage in a ±0.3° box is not the nearest
    # line: on the EIA layer that record sat 15-24 mi from the site at Ashburn,
    # Phoenix, Columbus, Dallas and Quincy, while the nearest line was under a
    # mile away. A true distance needs every line's geometry in the box, 139-353
    # KB per call. A spatial answer belongs in the database.
    return None, True


def _anchored_transmission_line(subs):
    """Steps 2-3 of find_nearest_transmission_measured. `subs` is step 1's rows,
    nearest first. Returns (line, measured) the way that function does."""
    # ★ 2026-09-13 — anchored at nearby substations, not a first-word prefix.
    #
    # This reads the MAINTAINED transmission_lines: the EIA set that
    # routes/transmission_ingest.py refreshes weekly, endpoints in from_sub/to_sub,
    # owner in operator. volt_class is served as None — the table does not store
    # HIFLD's VOLT_CLASS, and voltage_kv cannot derive it (the EIA classes overlap).
    #
    # Until this change step 2 served the highest-voltage line NATIONWIDE whose
    # endpoint started with the nearest substation's first word: 797 EIA lines
    # start with WEST, 917 with NORTH. Judged against the EIA line geometry at
    # 1,000 seeded real-named substations (production rows), 473 of the 732 lines
    # it served ran more than 25 mi from the substation they were attributed to;
    # WEST in Arkansas got a 500 kV Alabama Power line into WEST VERNON.
    #
    # Now, the pattern /api/v1/grid/transmission-proximity uses:
    #   1. the substations in the radius box, nearest first (step 1);
    #   2. lines whose from_sub or to_sub EQUALS one of their names, each anchored
    #      at its nearest matching endpoint, voltage_kv > 0 (-999999 is EIA's
    #      'not available', on 15,127 lines);
    #   3. the nearest anchor wins, then the highest voltage, then the lowest id —
    #      but only a line whose endpoint names place it at the anchor
    #      (_line_is_at). A name is still not an identity: nine substations are
    #      named WEST, in six states.
    # Same 1,000 sites: 980 lines served, 963 within 2 mi of their substation.
    # The before/after table is in the PR.
    #
    # Endpoint names are uppercase (0 of 95,569 lines differ from UPPER()); 31,016
    # substation names are not. So names are uppercased HERE, on the parameter
    # side. Never wrap from_sub/to_sub in UPPER()/LOWER(): a function on the column
    # cannot use an index on it, and the DB is C.UTF-8 (byte order), so equality
    # needs none. Do NOT add COLLATE "C" to 'harden' it — an explicit collation is
    # a different collation object and DEFEATS a plain index (see be#3086).
    anchors = {}
    for sub in subs:
        anchors.setdefault(sub['name'].upper(), sub)
    names = list(anchors)

    # Step 2: lines with an endpoint named like a nearby substation
    lines_query = """
        SELECT id, from_sub, to_sub, voltage_kv, operator AS owner, status
        FROM transmission_lines
        WHERE (from_sub = ANY(%s) OR to_sub = ANY(%s))
          AND voltage_kv > 0;
    """
    lines = execute_query(lines_query, (names, names))
    if lines is None:
        return None, False  # the statement did not run: no endpoint was matched
    if not lines:
        return None, True

    candidates = []
    for line in lines:
        ends = [(end, other) for end, other in ((line['from_sub'], line['to_sub']),
                                                (line['to_sub'], line['from_sub']))
                if end in anchors]
        anchor, other = min(ends, key=lambda e: anchors[e[0]]['distance_miles'])
        candidates.append((anchors[anchor]['distance_miles'], -line['voltage_kv'],
                           line['id'], anchor, other, line))
    candidates.sort(key=lambda c: c[:3])

    # Step 3: where do the endpoint names occur?
    spellings = set()
    for _, _, _, anchor, other, _ in candidates:
        spellings.update((anchor, anchors[anchor]['name']))
        if other:
            spellings.add(other)
    places_query = """
        SELECT name, lat, lng FROM substations
        WHERE name = ANY(%s) AND lat IS NOT NULL AND lng IS NOT NULL;
    """
    placed = execute_query(places_query, (sorted(spellings),))
    if placed is None:
        # No endpoint name could be placed, so no candidate can be checked: none is
        # served, and nothing was measured.
        return None, False
    places = {}
    for row in placed:
        places.setdefault(row['name'], []).append((row['lat'], row['lng']))

    for distance, _, _, anchor, other, line in candidates:
        sub = anchors[anchor]
        if _line_is_at(sub, anchor, other, places):
            return {
                'line_name': line['from_sub'],
                'voltage_kv': line['voltage_kv'],
                'owner': line['owner'],
                'status': line['status'],
                'volt_class': None,  # not stored by the maintained table; see above
                'distance_miles': round(distance, 1),
                'matched_substation': sub['name'],
            }, True
    return None, True


def _line_is_at(sub, anchor, other, places):
    """Do a line's endpoint names place it at `sub`, the substation whose name matched
    `anchor`? `other` is the far endpoint's name, None when none is recorded.
    `places` maps a substation name to the coordinates it occurs at."""
    def miles(name):
        return [_haversine_miles(sub['lat'], sub['lng'], lat, lng)
                for lat, lng in places.get(name, ())]

    anchor_elsewhere = any(d > _TX_ENDPOINT_MILES
                           for d in miles(anchor) + miles(sub['name']))
    other_miles = miles(other) if other else []
    if any(d <= _TX_ENDPOINT_MILES for d in other_miles):
        # The far end is a substation near this one, unless BOTH names also occur
        # elsewhere: substations named ALLEN and LINCOLN stand 7 mi apart in
        # Nevada, and the ALLEN -> LINCOLN line is in Indiana. (A self-loop lands
        # here on its own anchor, and stands or falls on that one name.)
        return not (anchor_elsewhere
                    and any(d > _TX_ENDPOINT_MILES for d in other_miles))
    if other_miles:
        return False  # the far end occurs, but nowhere near this substation
    # Nothing places the far end (none recorded, or a name absent from
    # `substations`), so the anchor's own name is the only evidence. Trust it only
    # if it occurs nowhere farther away and matched as stored: a case-folded match
    # cannot see copies kept under the other spelling.
    return not anchor_elsewhere and sub['name'] == anchor


def identify_iso_region(lat, lng, state=None):
    """Identify which ISO/RTO territory a location falls in."""
    # If we have the state, use state mapping
    if state:
        state_upper = state.upper()[:2]
        for iso_name, iso_data in ISO_REGIONS.items():
            if state_upper in iso_data['states']:
                return {
                    'name': iso_name,
                    'avg_queue_wait_years': iso_data['avg_queue_wait_years'],
                    'queue_depth_gw': iso_data['queue_depth_gw'],
                    'queue_url': iso_data.get('queue_url'),
                }
    
    # Coordinate-based fallback (rough bounding boxes)
    if lng < -115 and lat > 32 and lat < 42:
        return {**ISO_REGIONS['CAISO'], 'name': 'CAISO'}
    if lng > -105 and lng < -93 and lat > 25 and lat < 37:
        return {**ISO_REGIONS['ERCOT'], 'name': 'ERCOT'}
    if lng > -90 and lng < -74 and lat > 36 and lat < 43:
        return {**ISO_REGIONS['PJM'], 'name': 'PJM'}
    if lng > -98 and lng < -82 and lat > 37 and lat < 50:
        return {**ISO_REGIONS['MISO'], 'name': 'MISO'}
    if lng > -74 and lng < -67 and lat > 40 and lat < 47:
        return {**ISO_REGIONS['ISO-NE'], 'name': 'ISO-NE'}
    if lng > -80 and lng < -72 and lat > 40 and lat < 45:
        return {**ISO_REGIONS['NYISO'], 'name': 'NYISO'}
    
    # Default to SERC for southeastern US
    return {**ISO_REGIONS['SERC'], 'name': 'SERC'}


def estimate_queue_depth(iso_name, substation_voltage_kv):
    """
    Estimate queue depth for a specific substation based on ISO region
    and voltage class. Higher voltage subs tend to have deeper queues.
    
    In production, this would query the queue_entries table populated
    by the weekly ISO queue CSV ingestion cron job.
    """
    # Try database first
    query = """
        SELECT 
            SUM(capacity_mw) as total_queue_mw,
            COUNT(*) as project_count,
            AVG(EXTRACT(EPOCH FROM (NOW() - request_date)) / 86400 / 365) as avg_age_years
        FROM queue_entries
        WHERE iso = %s
          AND substation_voltage_kv >= %s - 50
          AND substation_voltage_kv <= %s + 50
          AND status IN ('Active', 'Pending', 'Under Study');
    """
    result = execute_query(query, (iso_name, substation_voltage_kv, substation_voltage_kv), fetchone=True)
    
    if result and result.get('total_queue_mw'):
        return {
            'queue_mw': int(result['total_queue_mw']),
            'project_count': int(result['project_count']),
            'avg_age_years': round(result.get('avg_age_years', 0, 0) or 0, 1),
            'source': 'database',
        }
    
    # Fallback: estimate from ISO region averages + voltage scaling
    iso_data = ISO_REGIONS.get(iso_name, ISO_REGIONS['SERC'])
    base_gw = iso_data['queue_depth_gw']
    
    # Higher voltage substations attract more interconnection requests
    voltage_multiplier = 1.0
    if substation_voltage_kv >= 500:
        voltage_multiplier = 1.8
    elif substation_voltage_kv >= 345:
        voltage_multiplier = 1.4
    elif substation_voltage_kv >= 230:
        voltage_multiplier = 1.1
    elif substation_voltage_kv < 138:
        voltage_multiplier = 0.6
    
    estimated_mw = int((base_gw * 1000 / 50) * voltage_multiplier)  # rough per-substation estimate
    
    return {
        'queue_mw': estimated_mw,
        'project_count': int(estimated_mw / 150),  # avg ~150MW per project
        'avg_age_years': iso_data['avg_queue_wait_years'] * 0.6,
        'estimated_wait_years': iso_data['avg_queue_wait_years'],
        'source': 'estimated',
    }


def _in_us_data_coverage(lat, lng):
    """True when the US datasets estimate_congestion and screen_environmental read reach
    this point: the 50 states, DC and the five inhabited territories.

    The same predicate the air-permitting (#3880) and water (#3895) scores use, not a
    third copy of it: Census state polygons, falling back to boxes when the committed
    geometry cannot load. Imported when called, because routes/site_report.py imports
    this module inside its own functions.
    """
    from routes.site_report import _usdm_in_coverage
    return _usdm_in_coverage(lat, lng)


def estimate_congestion(lat, lng, radius_miles=15):
    """
    Estimate grid congestion from local infrastructure density.
    High density of substations + generation = potential congestion.

    Both counts must be readings. Outside US data coverage, or when a count did not run,
    level is 'Unknown' and density_score None, and a count that did not run is served as
    None. A count that ran keeps its value, 0 included.
    """
    # ★ 2026-09-13 — a count that did not run was served as 0. execute_query returns None
    # when it has no connection or the query raises, and this read
    # `result.get('sub_count', 0) if result else 0`, so the failure served density_score 0
    # and level 'Low', the least congested reading there is. A site outside the US served
    # the same 0: the substations table's HIFLD slice spans CONUS, AK, HI and the Pacific
    # (routes/substation_ingest.py), and discovered_power_plants loads from US sources
    # (EIA-860, and OSM queried by US state).
    unknown = {
        'level': 'Unknown',
        'density_score': None,
        'substations_within_radius': None,
        'power_plants_within_radius': None,
        'total_generation_mw': None,
        'radius_miles': radius_miles,
    }
    if not _in_us_data_coverage(lat, lng):
        return unknown

    # Bounding box pre-filter
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT COUNT(*) as sub_count
        FROM substations
        WHERE lat IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s;
    """
    result = execute_query(query, (
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ), fetchone=True)
    sub_count = result.get('sub_count') if result else None
    
    # Also count power plants nearby
    plant_query = """
        SELECT COUNT(*) as plant_count, COALESCE(SUM(capacity_mw), 0) as total_mw
        FROM discovered_power_plants
        WHERE lat IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s;
    """
    plant_result = execute_query(plant_query, (
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ), fetchone=True)
    plant_count = plant_result.get('plant_count') if plant_result else None
    total_gen_mw = plant_result.get('total_mw') if plant_result else None

    if sub_count is None or plant_count is None:
        return dict(unknown,
                    substations_within_radius=sub_count,
                    power_plants_within_radius=plant_count,
                    total_generation_mw=None if total_gen_mw is None else int(total_gen_mw))
    
    # Density scoring (using local DB data only — no external API calls for speed)
    density_score = min(100, (sub_count * 3) + (plant_count * 2))
    
    if density_score > 60:
        level = 'High'
    elif density_score > 30:
        level = 'Moderate'
    else:
        level = 'Low'
    
    return {
        'level': level,
        'density_score': density_score,
        'substations_within_radius': sub_count,
        'power_plants_within_radius': plant_count,
        'total_generation_mw': int(total_gen_mw),
        'radius_miles': radius_miles,
    }


def _arcgis_query_features(resp):
    """The features list of an ArcGIS REST query answer.

    Raises ValueError when the answer is not a query result, so the lookup reads Unknown
    instead of taking the branch for a query that matched nothing: an HTTP error, a body
    that is not JSON, an error object (which ArcGIS sends with HTTP 200), or no features
    list.
    """
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    if 'error' in data:
        raise ValueError(f"ArcGIS error {data['error']}")
    features = data.get('features')
    if not isinstance(features, list):
        raise ValueError("the answer has no features list")
    return features


def screen_environmental(lat, lng):
    """
    Environmental screening using federal APIs.
    Checks: FEMA flood zones, FWS critical habitat, NWI wetlands.
    
    Returns risk scores for each category. A category that was not measured is
    'Unknown', and env_score is None when none of the three was.
    """
    env = {
        'flood_risk': 'Unknown',
        'wetland_risk': 'Unknown',
        'species_risk': 'Unknown',
        'risks_identified': [],
        'env_score': 50,  # default neutral
    }
    
    # ★ 2026-09-13 — a lookup that measured nothing was screened as 'Low'. Three answers
    # took the branch meant for "nothing found here":
    #  - A point outside US data coverage. FEMA NFHL, FWS critical habitat and NWI cover
    #    the US and its territories and answer no features anywhere else. Measured at
    #    50.363083, 9.307306 (Hesse, DE): env_score 90, "No significant environmental
    #    risks identified", and composite-score served it as a validated risk_resilience.
    #  - A query that failed. ArcGIS reports one with HTTP 200 and an error object, which
    #    has no 'features'. Measured 2026-09-13 for US and non-US points alike: the FWS URL
    #    below (layer 1, which that service no longer has) answers {"error": {"code": 400,
    #    "message": "Invalid URL"}}, the NWI URL {"error": {"code": 500, ...}}, and FEMA's
    #    /gis/nfhl base HTTP 404, the base risk_assessment_api.py already records as dead.
    #    So every site screened flood Unknown, species Low and wetlands Low: env_score 90
    #    and the "clear" environmental tier, whatever was at the point.
    #  - FEMA with no flood hazard area at the point. NFHL maps none where no flood map is
    #    in effect (risk_assessment_api.py measured Alaska's interior and Ireland).
    # Each now reads Unknown, and outside coverage the screen sends no request. The three
    # URLs are unchanged here, so while they answer as measured above, every screen inside
    # coverage reads Unknown on all three.
    if not _in_us_data_coverage(lat, lng):
        env['risks_identified'].append(
            'Not checked: FEMA flood zone, FWS critical habitat, NWI wetlands '
            '(their data covers only the US and its territories)')
        env['env_score'] = None
        return env

    # ── FEMA Flood Zone Check ──
    try:
        import requests
        fema_url = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"
        params = {
            'geometry': f'{lng},{lat}',
            'geometryType': 'esriGeometryPoint',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'FLD_ZONE,ZONE_SUBTY,SFHA_TF',
            'returnGeometry': 'false',
            'f': 'json',
        }
        resp = requests.get(fema_url, params=params, timeout=4)
        features = _arcgis_query_features(resp)
        if features:
            zone = features[0]['attributes'].get('FLD_ZONE', '')
            is_sfha = features[0]['attributes'].get('SFHA_TF', 'F')
            if zone in ('A', 'AE', 'AH', 'AO', 'V', 'VE'):
                env['flood_risk'] = 'High'
                env['risks_identified'].append(f'FEMA Flood Zone {zone} (Special Flood Hazard Area)')
            elif zone in ('X', 'B', 'C'):
                env['flood_risk'] = 'Low'
            else:
                env['flood_risk'] = 'Moderate'
                env['risks_identified'].append(f'FEMA Flood Zone {zone}')
        else:
            # No flood hazard area: no flood map is in effect here, which is not low risk.
            env['flood_risk'] = 'Unknown'
    except Exception as e:
        logger.warning(f"FEMA flood check failed: {e}")
        env['flood_risk'] = 'Unknown'
    
    # ── FWS Critical Habitat Check ──
    try:
        import requests
        fws_url = "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/USFWS_Critical_Habitat/FeatureServer/1/query"
        params = {
            'geometry': f'{lng},{lat}',
            'geometryType': 'esriGeometryPoint',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'comname,sciname,status',
            'returnGeometry': 'false',
            'f': 'json',
        }
        resp = requests.get(fws_url, params=params, timeout=4)
        features = _arcgis_query_features(resp)
        if features:
            env['species_risk'] = 'High'
            for f in features[:3]:
                species = f['attributes'].get('comname', 'Unknown species')
                env['risks_identified'].append(f'Critical Habitat: {species}')
        else:
            env['species_risk'] = 'Low'
    except Exception as e:
        logger.warning(f"FWS critical habitat check failed: {e}")
        env['species_risk'] = 'Unknown'
    
    # ── NWI Wetlands Check ──
    try:
        import requests
        nwi_url = "https://fwsprimary.wim.usgs.gov/server/rest/services/Wetlands/MapServer/0/query"
        params = {
            'geometry': f'{lng-0.01},{lat-0.01},{lng+0.01},{lat+0.01}',
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'WETLAND_TYPE,ATTRIBUTE',
            'returnGeometry': 'false',
            'f': 'json',
            'resultRecordCount': 5,
        }
        resp = requests.get(nwi_url, params=params, timeout=4)
        features = _arcgis_query_features(resp)
        if features:
            env['wetland_risk'] = 'Moderate'
            wetland_type = features[0]['attributes'].get('WETLAND_TYPE', 'Wetland')
            env['risks_identified'].append(f'NWI Wetlands: {wetland_type} within 0.6 miles')
        else:
            env['wetland_risk'] = 'Low'
    except Exception as e:
        logger.warning(f"NWI wetlands check failed: {e}")
        env['wetland_risk'] = 'Unknown'
    
    # ── Compute composite environmental score ──
    not_checked = [name for name, key in (('FEMA flood zone', 'flood_risk'),
                                          ('FWS critical habitat', 'species_risk'),
                                          ('NWI wetlands', 'wetland_risk'))
                   if env[key] == 'Unknown']
    if len(not_checked) == 3:
        # Nothing was measured, so there is no score to serve.
        env['env_score'] = None
    else:
        risk_scores = {'High': 30, 'Moderate': 15, 'Low': 0, 'Unknown': 10}
        total_risk = (
            risk_scores.get(env['flood_risk'], 10) +
            risk_scores.get(env['species_risk'], 10) +
            risk_scores.get(env['wetland_risk'], 10)
        )
        env['env_score'] = max(0, min(100, 100 - total_risk))
    if not_checked:
        env['risks_identified'].append('Not checked: ' + ', '.join(not_checked))
    
    if not env['risks_identified']:
        env['risks_identified'].append('No significant environmental risks identified')
    
    return env


def get_generation_mix(lat, lng, radius_miles=25):
    """Get generation mix within radius from discovered_power_plants table."""
    # Bounding box pre-filter
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT 
            COALESCE(fuel_type, 'Unknown') as fuel,
            SUM(COALESCE(capacity_mw, 0)) as total_mw,
            COUNT(*) as plant_count
        FROM discovered_power_plants
        WHERE lat IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        GROUP BY fuel
        ORDER BY total_mw DESC;
    """
    result = execute_query(query, (
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ))
    
    if not result:
        return {'mix': {}, 'total_mw': 0, 'plant_count': 0, 'radius_miles': radius_miles}
    
    total = sum(r['total_mw'] for r in result)
    mix = {}
    for r in result:
        pct = round((r['total_mw'] / total * 100), 1) if total > 0 else 0
        mix[r['fuel']] = {
            'mw': int(r['total_mw']),
            'percentage': pct,
            'plant_count': int(r['plant_count']),
        }
    
    return {
        'mix': mix,
        'total_mw': int(total),
        'plant_count': sum(r['plant_count'] for r in result),
        'radius_miles': radius_miles,
    }


# ─── Enhancement: Nearby Data Center Facilities ─────────────────────────────
def find_nearby_facilities(lat, lng, radius_miles=25, limit=10):
    """
    Find existing data center facilities near the site.
    Uses DC Hub's 13K+ facility database.
    Important context: nearby DCs mean proven infrastructure corridor.
    """
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT 
            name,
            provider,
            city,
            state,
            COALESCE(power_mw, 0) as power_mw,
            status,
            latitude as lat,
            longitude as lng,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(latitude)) *
                        cos(radians(longitude) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(latitude))
                    ))
                )
            ) as distance_miles
        FROM facilities
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND latitude != 0
          AND longitude != 0
          AND latitude BETWEEN %s AND %s
          AND longitude BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    result = execute_query(query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng,
        limit * 3  # Fetch extra to compensate for dedup
    ))
    
    if not result:
        return {'facilities': [], 'count': 0, 'total_power_mw': 0, 'radius_miles': radius_miles}
    
    # Deduplicate by name (keep nearest/first occurrence)
    seen = set()
    unique = []
    for f in result:
        name_key = f.get('name', '').lower().strip()
        if name_key and name_key not in seen and len(unique) < limit:
            seen.add(name_key)
            unique.append(f)
    result = unique
    
    total_mw = sum(f.get('power_mw', 0) for f in result)
    
    return {
        'facilities': result,
        'count': len(result),
        'total_power_mw': int(total_mw),
        'radius_miles': radius_miles,
        'corridor_signal': 'Strong' if len(result) >= 5 else 'Moderate' if len(result) >= 2 else 'Weak',
    }


# ─── Enhancement: Fiber/Connectivity Proximity ──────────────────────────────
def check_fiber_proximity(lat, lng, radius_miles=15):
    """
    Check for fiber routes and connectivity infrastructure nearby.
    Uses fiber_routes table if available.
    """
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    # Check for fiber routes table
    try:
        query = """
            SELECT COUNT(*) as route_count
            FROM fiber_routes
            WHERE start_location IS NOT NULL
              OR end_location IS NOT NULL;
        """
        result = execute_query(query, (), fetchone=True)
        fiber_count = result.get('route_count', 0) if result else 0
    except:
        fiber_count = 0
    
    # Check nearby facilities with connectivity info
    conn_query = """
        SELECT COUNT(*) as connected_dcs,
               COUNT(DISTINCT provider) as providers
        FROM facilities
        WHERE latitude IS NOT NULL
          AND latitude BETWEEN %s AND %s
          AND longitude BETWEEN %s AND %s
          AND connectivity IS NOT NULL
          AND connectivity != '';
    """
    conn_result = execute_query(conn_query, (
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ), fetchone=True)
    
    connected_dcs = conn_result.get('connected_dcs', 0) if conn_result else 0
    providers = conn_result.get('providers', 0) if conn_result else 0
    
    if connected_dcs >= 10:
        connectivity_rating = 'Excellent'
    elif connected_dcs >= 5:
        connectivity_rating = 'Good'
    elif connected_dcs >= 1:
        connectivity_rating = 'Fair'
    else:
        connectivity_rating = 'Limited'
    
    return {
        'connectivity_rating': connectivity_rating,
        'connected_facilities_nearby': connected_dcs,
        'unique_providers': providers,
        'fiber_routes_in_area': fiber_count,
        'radius_miles': radius_miles,
    }


# ─── Enhancement: Power Pricing by ISO ──────────────────────────────────────
# Real average wholesale electricity prices by ISO region ($/MWh)
# Source: EIA, ISO market reports — updated periodically
ISO_POWER_PRICES = {
    'ERCOT': {'avg_price_mwh': 38.50, 'peak_price_mwh': 85.00, 'trend': 'stable', 'renewable_pct': 32},
    'PJM': {'avg_price_mwh': 42.00, 'peak_price_mwh': 110.00, 'trend': 'rising', 'renewable_pct': 12},
    'MISO': {'avg_price_mwh': 35.00, 'peak_price_mwh': 75.00, 'trend': 'stable', 'renewable_pct': 22},
    'CAISO': {'avg_price_mwh': 55.00, 'peak_price_mwh': 180.00, 'trend': 'volatile', 'renewable_pct': 45},
    'SPP': {'avg_price_mwh': 28.00, 'peak_price_mwh': 60.00, 'trend': 'declining', 'renewable_pct': 38},
    'ISO-NE': {'avg_price_mwh': 52.00, 'peak_price_mwh': 140.00, 'trend': 'rising', 'renewable_pct': 15},
    'NYISO': {'avg_price_mwh': 48.00, 'peak_price_mwh': 130.00, 'trend': 'rising', 'renewable_pct': 18},
    'SERC': {'avg_price_mwh': 40.00, 'peak_price_mwh': 90.00, 'trend': 'stable', 'renewable_pct': 10},
}

def get_power_pricing(iso_name):
    """Get wholesale electricity pricing for the ISO region."""
    pricing = ISO_POWER_PRICES.get(iso_name, ISO_POWER_PRICES.get('SERC'))
    return {
        'iso': iso_name,
        'avg_wholesale_price_mwh': pricing['avg_price_mwh'],
        'peak_price_mwh': pricing['peak_price_mwh'],
        'price_trend': pricing['trend'],
        'renewable_percentage': pricing['renewable_pct'],
        'estimated_annual_cost_per_mw': int(pricing['avg_price_mwh'] * 8760),
        'note': 'Wholesale market averages. Actual contract rates vary by utility, load factor, and term.',
    }


# ─── Enhancement: Water Availability Risk ───────────────────────────────────
# State-level water stress indicators (simplified from WRI Aqueduct data)
WATER_STRESS_BY_STATE = {
    # High stress
    'CA': 'High', 'AZ': 'High', 'NV': 'High', 'NM': 'High', 'UT': 'High',
    # Moderate-High
    'TX': 'Moderate-High', 'CO': 'Moderate-High', 'OK': 'Moderate-High', 'KS': 'Moderate-High',
    # Moderate
    'GA': 'Moderate', 'FL': 'Moderate', 'SC': 'Moderate', 'NE': 'Moderate',
    'MT': 'Moderate', 'ID': 'Moderate', 'WY': 'Moderate', 'HI': 'Moderate',
    # Low-Moderate
    'NC': 'Low-Moderate', 'TN': 'Low-Moderate', 'AL': 'Low-Moderate', 'MS': 'Low-Moderate',
    'AR': 'Low-Moderate', 'LA': 'Low-Moderate', 'MO': 'Low-Moderate', 'ND': 'Low-Moderate',
    'SD': 'Low-Moderate',
    # Low
    'VA': 'Low', 'OH': 'Low', 'PA': 'Low', 'NY': 'Low', 'IL': 'Low',
    'WI': 'Low', 'MN': 'Low', 'WA': 'Low', 'OR': 'Low', 'IN': 'Low',
    'MI': 'Low', 'IA': 'Low', 'KY': 'Low', 'WV': 'Low', 'MD': 'Low',
    'DE': 'Low', 'NJ': 'Low', 'CT': 'Low', 'RI': 'Low', 'MA': 'Low',
    'NH': 'Low', 'VT': 'Low', 'ME': 'Low', 'AK': 'Low', 'DC': 'Low',
}

def assess_water_risk(state_code):
    """
    Assess water availability risk for data center cooling.
    Critical for hyperscale facilities that use evaporative cooling.
    """
    stress = WATER_STRESS_BY_STATE.get(state_code, 'Unknown')
    
    risk_scores = {
        'Low': 10, 'Low-Moderate': 25, 'Moderate': 45,
        'Moderate-High': 65, 'High': 85, 'Unknown': 50
    }
    
    recommendations = {
        'Low': 'Favorable for water-cooled facilities. Standard permitting expected.',
        'Low-Moderate': 'Generally adequate supply. Monitor seasonal variations.',
        'Moderate': 'Water management plan recommended. Consider air-cooled alternatives.',
        'Moderate-High': 'Water-efficient cooling strongly recommended. May face permitting scrutiny.',
        'High': 'Water scarcity zone. Air-cooled or closed-loop systems recommended. Expect permitting challenges.',
        'Unknown': 'Water availability data not available for this state.',
    }
    
    return {
        'water_stress_level': stress,
        'water_risk_score': risk_scores.get(stress, 50),
        'state': state_code,
        'recommendation': recommendations.get(stress, ''),
        'cooling_note': 'Modern hyperscale facilities use 1.8L/kWh average. Air-cooled alternatives reduce water use by 90%+.',
    }


def compute_suitability_score(substations, transmission, iso, env, congestion, gas=None, nearby_dcs=None, weights=None):
    """
    Compute 0-100 Interconnection Suitability Score.
    Uses configurable weights so we can tune without redeploying.
    v2.0: Now includes gas access and DC corridor scoring.
    """
    w = weights or DEFAULT_SCORING_WEIGHTS
    score = 0
    breakdown = {}

    def _reading(value, default):
        # A served reading as a float, or the default when there is none.
        # ★ 2026-09-13 — five tiers used to default a falsy reading before converting it,
        # and 0 is falsy, so a real 0 scored as missing:
        # - the three distance tiers defaulted to 999, so a site 0.0 mi away scored in the
        #   farthest tier while 0.04 mi scored "excellent". Transmission and gas distances
        #   are rounded to 0.1 before they are served, so any line or pipeline within
        #   0.05 mi of the site hit it.
        # - the environmental and congestion tiers defaulted to 50, so an env_score of 0,
        #   the worst score on its scale, was scored as risk 50 (moderate_risk), and a
        #   density_score of 0, nothing counted within the radius, as moderate while its
        #   level read Low. A count that did not run is not a 0: estimate_congestion
        #   serves it as density_score None, which scores as the default.
        # None and anything float() cannot read still score as the default; a label such as
        # 'N/A (live query)' used to raise.
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    # 1. Substation proximity
    if substations:
        nearest_dist = _reading(substations[0].get('distance_miles'), 999)
        for tier_name, tier in w['substation_proximity']['thresholds'].items():
            if nearest_dist <= tier['max_miles']:
                points = tier['points']
                score += points
                breakdown['substation_proximity'] = {'points': points, 'tier': tier_name, 'value': f"{nearest_dist:.1f} mi"}
                break
    
    # 2. Substation voltage (use HIGHEST voltage within range, not just nearest)
    if substations:
        voltage = max((s.get('voltage_kv') or 0) for s in substations)
        for tier_name, tier in w['substation_voltage']['thresholds'].items():
            if voltage >= tier['min_kv']:
                points = tier['points']
                score += points
                breakdown['substation_voltage'] = {'points': points, 'tier': tier_name, 'value': f"{voltage} kV"}
                break
    
    # 3. Queue depth (use best voltage for queue estimate)
    if substations and iso:
        best_voltage = max((s.get('voltage_kv') or 0) for s in substations)
        queue = estimate_queue_depth(iso.get('name', 'SERC'), best_voltage)
        queue_mw = queue.get('queue_mw', 2000)
        for tier_name, tier in w['queue_depth']['thresholds'].items():
            if queue_mw <= tier['max_mw']:
                points = tier['points']
                score += points
                breakdown['queue_depth'] = {'points': points, 'tier': tier_name, 'value': f"{queue_mw} MW"}
                break
    
    # 4. Transmission proximity
    if transmission:
        tx_dist = _reading(transmission.get('distance_miles'), 999)
        for tier_name, tier in w['transmission_proximity']['thresholds'].items():
            if tx_dist <= tier['max_miles']:
                points = tier['points']
                score += points
                breakdown['transmission_proximity'] = {'points': points, 'tier': tier_name, 'value': f"{tx_dist:.1f} mi"}
                break
    
    # 5. Environmental
    if env:
        env_risk = 100 - _reading(env.get('env_score'), 50)
        for tier_name, tier in w['environmental']['thresholds'].items():
            if env_risk <= tier['max_risk_score']:
                points = tier['points']
                score += points
                breakdown['environmental'] = {'points': points, 'tier': tier_name, 'value': 'Score N/A' if env.get('env_score') is None else f"Score {env['env_score']}"}
                break
    
    # 6. Congestion
    if congestion:
        density = int(_reading(congestion.get('density_score'), 50))
        for tier_name, tier in w['congestion']['thresholds'].items():
            if density <= tier['max_density']:
                points = tier['points']
                score += points
                breakdown['congestion'] = {'points': points, 'tier': tier_name, 'value': congestion.get('level', 'Unknown')}
                break
    
    # 7. Gas access
    if gas:
        gas_dist = _reading((gas.get('nearest_pipeline', {}) or {}).get('distance_miles'), 999)
        for tier_name, tier in w['gas_access']['thresholds'].items():
            if gas_dist <= tier['max_miles']:
                points = tier['points']
                score += points
                breakdown['gas_access'] = {'points': points, 'tier': tier_name, 'value': f"{gas_dist:.1f} mi"}
                break
    
    # 8. DC corridor strength
    if nearby_dcs:
        dc_count = int(nearby_dcs.get('count') or 0)
        for tier_name, tier in w['dc_corridor']['thresholds'].items():
            if dc_count >= tier['min_count']:
                points = tier['points']
                score += points
                breakdown['dc_corridor'] = {'points': points, 'tier': tier_name, 'value': f"{dc_count} DCs"}
                break
    
    return {
        'score': min(100, score),
        'max_possible': 100,
        'breakdown': breakdown,
        'weights_version': 'v2.0',
    }


# ─── Enhancement: Gas Infrastructure Proximity ──────────────────────────────
def find_nearby_gas_pipelines(lat, lng, radius_miles=25, limit=10):
    """
    Find gas pipelines near the site from 10K+ gas_pipelines table.
    Critical for: gas-fired power generation, dual-fuel capability,
    backup generation, and midstream infrastructure access.
    """
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT 
            name,
            operator,
            pipeline_type,
            COALESCE(diameter_inches, 0) as diameter_inches,
            COALESCE(capacity_mcf, 0) as capacity_mcf,
            status,
            state,
            lat, lng,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(lat)) *
                        cos(radians(lng) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(lat))
                    ))
                )
            ) as distance_miles
        FROM gas_pipelines
        WHERE lat IS NOT NULL AND lng IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    result = execute_query(query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng,
        limit
    ))
    
    if not result:
        return {
            'pipelines': [],
            'count': 0,
            'gas_access': 'None detected',
            'radius_miles': radius_miles,
        }
    
    # Categorize by type
    types = {}
    for p in result:
        pt = p.get('pipeline_type', 'Unknown')
        if pt not in types:
            types[pt] = 0
        types[pt] += 1
    
    nearest = result[0] if result else {}
    nearest_dist = nearest.get('distance_miles', 999)
    
    if nearest_dist < 3:
        gas_access = 'Excellent'
    elif nearest_dist < 10:
        gas_access = 'Good'
    elif nearest_dist < 20:
        gas_access = 'Fair'
    else:
        gas_access = 'Limited'
    
    return {
        'pipelines': result,
        'count': len(result),
        'nearest_pipeline': {
            'name': nearest.get('name', 'Unknown'),
            'operator': nearest.get('operator', 'Unknown'),
            'type': nearest.get('pipeline_type', 'Unknown'),
            'diameter': nearest.get('diameter_inches', 0),
            'capacity_mcf': nearest.get('capacity_mcf', 0),
            'distance_miles': round(nearest_dist, 1),
        },
        'pipeline_types': types,
        'gas_access': gas_access,
        'radius_miles': radius_miles,
    }


# ─── Enhancement: Major Interstate Pipeline Proximity ────────────────────────
def find_major_pipelines(lat, lng, radius_miles=50):
    """
    Find major interstate gas pipelines from discovered_pipelines table.
    These are the big 31 major trunk lines with capacity data (MDth/d).
    """
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT 
            name,
            operator,
            pipeline_type,
            commodity,
            COALESCE(capacity_mdth, 0) as capacity_mdth,
            states_served,
            lat, lng,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(lat)) *
                        cos(radians(lng) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(lat))
                    ))
                )
            ) as distance_miles
        FROM discovered_pipelines
        WHERE lat IS NOT NULL AND lng IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT 5;
    """
    result = execute_query(query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ))
    
    if not result:
        return {'major_pipelines': [], 'count': 0}
    
    return {
        'major_pipelines': [{
            'name': p.get('name', 'Unknown'),
            'operator': p.get('operator', 'Unknown'),
            'capacity_mdth_per_day': p.get('capacity_mdth', 0),
            'states_served': p.get('states_served', ''),
            'distance_miles': round(p.get('distance_miles', 0) or 0, 1),
        } for p in result],
        'count': len(result),
    }


# ─── Enhancement: DC Capacity Pipeline (Planned/Under Construction) ─────────
def get_capacity_pipeline_nearby(lat, lng, state=None, market=None):
    """
    Get data center capacity pipeline projects near the site.
    Shows what's being built — indicates market growth and demand signal.
    Uses capacity_pipeline table (191 projects, 184GW+).
    """
    # Try market match first, then region/state
    results = None
    
    # 2026-07-31: all three arms feed `total_pipeline_mw` in the site plan.
    # The last one especially — "top projects regardless of location", ordered
    # by capacity_mw DESC, is precisely the query the quarantined aggregates
    # dominate. See util/capacity_pipeline.
    if market:
        query = f"""
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE LOWER(market) LIKE LOWER(%s)
              AND {CP_OK}
            ORDER BY capacity_mw DESC
            LIMIT 10;
        """
        results = execute_query(query, (f'%{market}%',))

    if (not results or len(results) == 0) and state:
        query = f"""
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE (LOWER(market) LIKE LOWER(%s)
               OR LOWER(region) LIKE LOWER(%s))
              AND {CP_OK}
            ORDER BY capacity_mw DESC
            LIMIT 10;
        """
        results = execute_query(query, (f'%{state}%', f'%{state}%'))

    if not results:
        # Fallback: get top projects regardless of location
        query = f"""
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE market != 'Unknown'
              AND {CP_OK}
            ORDER BY capacity_mw DESC
            LIMIT 5;
        """
        results = execute_query(query)
    
    if not results:
        return {'projects': [], 'total_pipeline_mw': 0, 'project_count': 0}
    
    total_mw = sum(p.get('capacity_mw', 0) for p in results)
    
    # Phase breakdown
    phases = {}
    for p in results:
        ph = p.get('phase', 'Unknown')
        if ph not in phases:
            phases[ph] = {'count': 0, 'mw': 0}
        phases[ph]['count'] += 1
        phases[ph]['mw'] += p.get('capacity_mw', 0)
    
    return {
        'projects': [{
            'operator': p.get('operator', 'Unknown'),
            'market': p.get('market', 'Unknown'),
            'capacity_mw': int(p.get('capacity_mw', 0)),
            'phase': p.get('phase', 'Unknown'),
            'status': p.get('status', 'Unknown'),
            'completion_date': p.get('completion_date', ''),
            'confidence': p.get('confidence_label', 'low'),
        } for p in results],
        'total_pipeline_mw': int(total_mw),
        'project_count': len(results),
        'phase_breakdown': phases,
        'demand_signal': 'Very Strong' if total_mw > 500 else 'Strong' if total_mw > 100 else 'Moderate' if total_mw > 0 else 'Low',
    }


# ─── Enhancement: Reverse Geocode for Map Clicks ────────────────────────────
def reverse_geocode(lat, lng):
    """Reverse geocode lat/lng to get address, state, county."""
    try:
        import requests
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            'lat': lat,
            'lon': lng,
            'format': 'json',
            'addressdetails': 1,
            'zoom': 14,
        }
        headers = {'User-Agent': 'DCHub-SitePlanner/1.0 (jaz@dchub.cloud)'}
        resp = requests.get(url, params=params, headers=headers, timeout=4)
        data = resp.json()
        
        if data and 'address' in data:
            addr = data['address']
            return {
                'display_name': data.get('display_name', ''),
                'state': addr.get('state', ''),
                'state_code': addr.get('ISO3166-2-lvl4', '').replace('US-', ''),
                'county': addr.get('county', ''),
                'city': addr.get('city') or addr.get('town') or addr.get('village', ''),
            }
    except Exception as e:
        logger.warning(f"Reverse geocode failed: {e}")
    
    return None


# ─── Geocoding Helper ────────────────────────────────────────────────────────
def geocode_address(address):
    """
    Geocode an address to lat/lng.
    Uses Nominatim (free) as primary, with fallback patterns.
    In production, consider Mapbox or Google geocoding for better accuracy.
    """
    try:
        import requests
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': address,
            'format': 'json',
            'limit': 1,
            'countrycodes': 'us',
            'addressdetails': 1,
        }
        headers = {'User-Agent': 'DCHub-SitePlanner/1.0 (jaz@dchub.cloud)'}
        resp = requests.get(url, params=params, headers=headers, timeout=4)
        results = resp.json()
        
        if results:
            r = results[0]
            addr_details = r.get('address', {})
            return {
                'lat': float(r['lat']),
                'lng': float(r['lon']),
                'display_name': r.get('display_name', address),
                'state': addr_details.get('state', ''),
                'state_code': addr_details.get('ISO3166-2-lvl4', '').replace('US-', ''),
                'county': addr_details.get('county', ''),
                'city': addr_details.get('city') or addr_details.get('town') or addr_details.get('village', ''),
            }
    except Exception as e:
        logger.error(f"Geocoding failed for '{address}': {e}")
    
    return None


# ─── Route Registration ──────────────────────────────────────────────────────

def register_site_planner_routes(app):
    """Register all Site Planner endpoints on the Flask app."""
    
    from flask import request as flask_request, jsonify
    
    # Import the auth decorator from main.py
    # Uses lazy import to avoid circular dependency
    def require_pro(f):
        """Decorator: requires Pro plan or higher.

        2026-06-06: the Site Planner "risk assessment" panel was blanking with a
        503 ("0" / "?"). require_plan('pro') can RETURN a transient 503
        (tier_gating_unavailable / "Authentication service unavailable") during a
        cold-start window or a Neon blip — and this decorator only caught
        ImportError, so the 503 propagated and the panel showed nothing. Policy:
        an AUTHENTICATED caller (logged-in JWT or API key) that hits a transient
        gating failure falls through to the handler; anonymous callers stay gated
        (401). Security boundary preserved — we only fail-open on infra errors,
        never for unauthenticated requests.
        """
        @wraps(f)
        def decorated(*args, **kwargs):
            has_auth = bool(
                flask_request.headers.get('Authorization')
                or flask_request.headers.get('X-API-Key')
                or flask_request.args.get('api_key'))
            try:
                from main import require_plan
                resp = require_plan('pro')(f)(*args, **kwargs)
                # Transient gating 503 to an authenticated caller → run anyway.
                try:
                    status = resp[1] if isinstance(resp, tuple) else getattr(resp, 'status_code', 200)
                except Exception:
                    status = 200
                if status == 503 and has_auth:
                    logger.warning("site-planner: gating returned 503 for an authed caller — falling through (transient infra)")
                    return f(*args, **kwargs)
                return resp
            except ImportError:
                if not has_auth:
                    return jsonify({
                        'success': False,
                        'error': 'authentication_required',
                        'message': 'Site Planner requires a Pro subscription',
                        'upgrade_url': 'https://dchub.cloud/pricing',
                    }), 401
                return f(*args, **kwargs)
            except Exception as e:
                # Gating RAISED (not returned). Same policy: authed → through.
                logger.warning(f"site-planner: gating raised {type(e).__name__} — "
                               f"{'falling through (authed)' if has_auth else 'blocking (anon)'}")
                if has_auth:
                    return f(*args, **kwargs)
                return jsonify({
                    'success': False,
                    'error': 'authentication_required',
                    'message': 'Site Planner requires a Pro subscription',
                    'upgrade_url': 'https://dchub.cloud/pricing',
                }), 401
        decorated.__name__ = f.__name__
        return decorated

    # ── OPTIONS preflight for all site-planner routes ──
    @app.route('/api/v1/site-planner/analyze', methods=['OPTIONS'])
    @app.route('/api/v1/site-planner/compare', methods=['OPTIONS'])
    @app.route('/api/v1/site-planner/export', methods=['OPTIONS'])
    def site_planner_preflight():
        """Handle CORS preflight — must return 200 with no auth check."""
        resp = jsonify({'ok': True})
        resp.headers['Access-Control-Allow-Origin'] = flask_request.headers.get('Origin', '*')
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-API-Key, Accept'
        resp.headers['Access-Control-Max-Age'] = '86400'
        return resp, 200

    # ── POST /api/v1/site-planner/analyze ──
# AUTO-REPAIR: duplicate route '/api/v1/site-planner/analyze' also in site_planner.py:1679 — review and remove one
    @app.route('/api/v1/site-planner/analyze', methods=['POST'])
    @require_pro
    def site_planner_analyze():
        """
        Full site analysis. Accepts address or lat/lng coordinates.
        
        Request body:
          { "address": "123 Main St, Dallas, TX" }
          OR
          { "lat": 32.7767, "lng": -96.7970 }
        
        Returns: Complete interconnection analysis report.
        """
        start_time = time.time()
        
        data = flask_request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400
        
        lat = data.get('lat')
        lng = data.get('lng')
        address = data.get('address', '')
        state = data.get('state', '')
        
        # Geocode if address provided
        if address and (not lat or not lng):
            geo = geocode_address(address)
            if not geo:
                return jsonify({
                    'success': False,
                    'error': 'geocoding_failed',
                    'message': f'Could not geocode address: {address}',
                }), 400
            lat = geo['lat']
            lng = geo['lng']
            state = geo.get('state_code', '')
            address = geo.get('display_name', address)
        
        if not lat or not lng:
            return jsonify({'success': False, 'error': 'lat/lng or address required'}), 400
        
        # Reverse geocode if we have coords but no address (map click)
        if (not address or address == '') and lat and lng:
            rev = reverse_geocode(lat, lng)
            if rev:
                address = rev.get('display_name', f'{lat:.4f}, {lng:.4f}')
                if not state:
                    state = rev.get('state_code', '')
        
        try:
            # Phase 1: Proximity Analysis
            substations = find_nearest_substations(lat, lng, limit=5)
            # Read before the dedupe turns None into []: None means the lookup did
            # not run, so the empty list and the null queue served below are
            # unmeasured, not absent, and the response says which.
            substations_coverage, substations_basis = _substations_coverage(substations)
            
            # Deduplicate substations by name
            seen_names = set()
            unique_subs = []
            for s in (substations or []):
                if s.get('name') not in seen_names:
                    seen_names.add(s.get('name'))
                    unique_subs.append(s)
            substations = unique_subs
            
            # No live top-up: analyze serves the substations the local lookup
            # found, even when that is fewer than five. The queue estimate and the
            # suitability score read substations[0] and the highest voltage here,
            # and both handle a short or an empty list.
            #
            # ★ 2026-09-13 — query_substations_live used to top this list up from a
            # live ArcGIS query whenever it held fewer than five names, which also
            # happens when two of the nearest five share a name. It was removed,
            # not repointed. The services1 Electric_Substations service it queried
            # no longer exists: ArcGIS answered HTTP 200 with error 400 "Invalid
            # URL", so it returned [] after a network round trip on every call, and
            # its except never logged because nothing raised.
            #
            # The national layer that still serves substations (services5
            # HDRa0B57OVrv2E1q) is no substitute. It has no OWNER field, so the same
            # query is rejected there as well. 38,479 of its 75,328 names are
            # UNKNOWN<id> placeholders, which the name dedupe above cannot match to
            # the named row the substations table holds at the same coordinates,
            # and that is why routes/substation_ingest.py refuses to write it. And
            # Railway's egress to ArcGIS is unreliable (routes/transmission_ingest.py
            # fetches on a GitHub runner). A spatial answer belongs in the table.
            
            transmission, transmission_measured = find_nearest_transmission_measured(lat, lng)
            # None both when no line is anchored near the site and when a statement
            # did not run; transmission_coverage says which.
            transmission_coverage, transmission_basis = _transmission_coverage(transmission_measured)
            
            # Phase 2: Queue Depth & Scoring
            iso = identify_iso_region(lat, lng, state)
            queue_data = None
            if substations:
                queue_data = estimate_queue_depth(
                    iso.get('name', 'SERC'),
                    substations[0].get('voltage_kv', 0)
                )
            congestion = estimate_congestion(lat, lng)
            
            # Phase 3: Environmental Screening
            env = screen_environmental(lat, lng)
            gen_mix = get_generation_mix(lat, lng)
            
            # Enhanced Analysis
            nearby_dcs = find_nearby_facilities(lat, lng)
            fiber = check_fiber_proximity(lat, lng)
            # Enrich with the real parcel fiber-readiness scorer (carrier_facility_presence
            # + FCC fiber hex). Falls back silently to the check_fiber_proximity result on
            # any error, so existing behavior is preserved.
            try:
                from routes.connectivity_score import score_connectivity
                cc = score_connectivity(lat, lng, 50)
                if cc and not cc.get('error'):
                    _cnt = cc.get('carrier_count', 0)
                    _bucket = cc.get('near_net_bucket', '')
                    # 'unknown' bucket = the carrier dataset does not describe this
                    # region at all. Rating it 'Limited' would re-assert, one layer
                    # up, the absence-as-finding the scorer just stopped publishing.
                    _rating = ('Unknown' if _bucket == 'unknown'
                               else 'Excellent' if _cnt >= 8 and _bucket in ('on-net', 'near-net')
                               else 'Good' if _cnt >= 4
                               else 'Fair' if _cnt >= 1
                               else 'Limited')
                    fiber['connectivity_rating'] = _rating
                    fiber['connected_facilities_nearby'] = _cnt
                    fiber['score'] = cc.get('score')
                    fiber['near_net_bucket'] = _bucket
                    fiber['nearest_carrier_km'] = cc.get('nearest_carrier_km')
                    fiber['carrier_count'] = _cnt
                    fiber['single_carrier_risk'] = cc.get('single_carrier_risk', False)
                    fiber['verdict'] = cc.get('verdict_short', '')
                    fiber['top_carriers'] = cc.get('top_carriers', [])
            except Exception as _cc_e:
                logger.warning(f"connectivity_score enrich failed: {_cc_e}")
            power_pricing = get_power_pricing(iso.get('name', 'SERC'))
            water = assess_water_risk(state)
            
            # Gas & Midstream Infrastructure
            gas = find_nearby_gas_pipelines(lat, lng)
            major_pipes = find_major_pipelines(lat, lng)
            
            # DC Capacity Pipeline (what's being built nearby)
            # Try to extract city/market for better matching
            city_market = address.split(',')[0] if address else ''
            capacity = get_capacity_pipeline_nearby(lat, lng, state=state, market=city_market)
            
            # Compute composite score (v2.0 — includes gas + DC corridor)
            scoring = compute_suitability_score(substations, transmission, iso, env, congestion, gas=gas, nearby_dcs=nearby_dcs)
            
            elapsed = round(time.time() - start_time, 2)
            
            def _clean(obj):
                if isinstance(obj, dict):
                    return {(str(k) if k is None else k): _clean(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_clean(i) for i in obj]
                return obj
            
            return jsonify(_clean({
                'success': True,
                'analysis': {
                    'location': {
                        'address': address,
                        'lat': lat,
                        'lng': lng,
                        'state': state,
                    },
                    'substations': substations or [],
                    'substations_coverage': substations_coverage,
                    'substations_basis': substations_basis,
                    'transmission': transmission,
                    'transmission_coverage': transmission_coverage,
                    'transmission_basis': transmission_basis,
                    'iso': iso,
                    'queue': queue_data,
                    'congestion': congestion,
                    'environmental': env,
                    'generation_mix': gen_mix,
                    'nearby_data_centers': nearby_dcs,
                    'fiber_connectivity': fiber,
                    'power_pricing': power_pricing,
                    'water_risk': water,
                    'gas_infrastructure': gas,
                    'major_pipelines': major_pipes,
                    'capacity_pipeline': capacity,
                    'suitability_score': scoring,
                },
                'meta': {
                    'elapsed_seconds': elapsed,
                    'timestamp': datetime.utcnow().isoformat(),
                    'version': 'v1.2',
                    'data_sources': ['HIFLD', 'FEMA', 'FWS', 'NWI', 'ISO Queue Estimates',
                                     'DC Hub Facilities DB', 'EIA Power Pricing',
                                     'Gas Pipelines (10K+)', 'Capacity Pipeline (191 projects)'],
                },
            }))
        
        except Exception as e:
            logger.error(f"Site analysis failed: {e}\n{traceback.format_exc()}")
            return jsonify({
                'success': False,
                'error': 'analysis_failed',
                'message': str(e),
            }), 500

    # ── POST /api/v1/site-planner/composite-score ──
    @app.route('/api/v1/site-planner/composite-score', methods=['GET', 'POST'])
    @require_pro
    # r-slow-tool-cache (2026-08-31): p50 was 11,553 ms over 41 calls in the 14
    # days to 2026-08-31 — a MEDIAN past the point most MCP clients give up, so
    # roughly half of all agent callers were at risk of timing out on the
    # flagship "should I build here" answer. The handler fans out to ~10
    # sequential gathers (substations, transmission, ISO, congestion,
    # environmental, gas, nearby DCs, fiber, connectivity, water, DCPI); none of
    # them is caller-specific, so the whole response memoises on its inputs.
    # 6 h TTL: every layer behind it is static-to-daily. redis_cache's own
    # cached_endpoint cannot be used here — it returns early for any request
    # with an Authorization/X-API-Key header, and @require_pro means that is
    # every real caller. See routes/_slow_tool_cache.py.
    @cache_tool_response(ttl=6 * 3600, prefix='composite_score',
                         arg_names=('lat', 'lng', 'lon', 'state', 'address'))
    def site_planner_composite_score():
        """Composite site suitability score (0-100) with an EXPLICIT per-factor
        coverage map. Synthesizes power/grid, fiber, natural-hazard risk, water,
        and market/DCPI — but scores ONLY over factors whose data is actually
        sourced (constraint-coverage). Unsourced factors are DECLARED
        `unavailable`, never imputed — so water stays out until the WRI Aqueduct
        ingest lands (the 2026-07-07 paused proxy is never surfaced), and the
        composite is honest about what it does and doesn't know.

        Body: { "lat":.., "lng":.., "state":"VA" }  or  { "address": ".." }.
        This is the honest counterpart to the composite a raw analyze_site dump
        makes you assemble yourself."""
        # GET (MCP callAPI sends query params) or POST JSON. Accept lng or lon.
        data = flask_request.get_json(silent=True) or flask_request.values

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        lat = _f(data.get('lat'))
        lng = _f(data.get('lng') if data.get('lng') is not None else data.get('lon'))
        state = (data.get('state') or '')
        address = (data.get('address') or '')
        if address and (not lat or not lng):
            geo = geocode_address(address)
            if geo:
                lat, lng = geo['lat'], geo['lng']
                state = geo.get('state_code', state) or state
        if not lat or not lng:
            return jsonify({'success': False, 'error': 'lat/lng or address required'}), 400
        if not state:
            try:
                rev = reverse_geocode(lat, lng)
                if rev:
                    state = rev.get('state_code', '') or state
            except Exception:
                pass

        sub = {}
        env = None
        # True when power_grid is unavailable because a lookup failed, not because of
        # anything about the site. Such an answer is not memoised; see the return.
        power_grid_failed = False
        # ── power_grid (real: HIFLD substations/transmission + ISO queue + congestion + gas) ──
        try:
            substations = find_nearest_substations(lat, lng, limit=5)
            transmission, transmission_measured = find_nearest_transmission_measured(lat, lng)
            iso = identify_iso_region(lat, lng, state)
            congestion = estimate_congestion(lat, lng)
            env = screen_environmental(lat, lng)
            gas = find_nearby_gas_pipelines(lat, lng)
            nearby_dcs = find_nearby_facilities(lat, lng)
            # Each lookup that did not run, and the factors compute_suitability_score
            # would have scored as absent without it. That scorer returns a number for
            # any input, so scoring past a lookup that did not run published
            # power_grid as validated with those factors silently missing: substation
            # proximity, voltage and queue depth (up to 60 of its points) for the
            # substations lookup, transmission proximity (up to 15) for the
            # transmission lookup.
            unmeasured = []
            if substations is None:
                unmeasured.append((SUBSTATIONS_NOT_MEASURED,
                                   ['substation proximity', 'voltage', 'queue depth']))
            if not transmission_measured:
                unmeasured.append((TRANSMISSION_NOT_MEASURED, ['transmission proximity']))
            if unmeasured:
                power_grid_failed = True
                left_out = [factor for _, factors in unmeasured for factor in factors]
                sub['power_grid'] = {
                    'score': None, 'coverage': 'unavailable',
                    'basis': ('; '.join(reason for reason, _ in unmeasured)
                              + '; power_grid is declared unavailable rather than scored without '
                              + ', '.join(left_out[:-1])
                              + (' and ' if len(left_out) > 1 else '') + left_out[-1])}
            else:
                scoring = compute_suitability_score(substations, transmission, iso, env,
                                                    congestion, gas=gas, nearby_dcs=nearby_dcs) or {}
                pg = scoring.get('score')
                sub['power_grid'] = {'score': pg,
                                     'coverage': 'validated' if isinstance(pg, (int, float)) else 'unavailable',
                                     'basis': 'substation proximity/voltage, ISO queue depth, transmission, congestion, gas access, DC corridor (HIFLD + ISO)'}
        except Exception as e:
            logger.warning(f"composite power_grid failed: {e}")
            power_grid_failed = True
            sub['power_grid'] = {'score': None, 'coverage': 'unavailable', 'basis': f'gather failed: {type(e).__name__}'}

        # ── fiber (carrier presence + FCC hex) ──
        try:
            fiber = check_fiber_proximity(lat, lng) or {}
            fscore = fiber.get('score')
            try:
                from routes.connectivity_score import score_connectivity
                cc = score_connectivity(lat, lng, 50)
                if cc and not cc.get('error') and cc.get('score') is not None:
                    fscore = cc.get('score')
            except Exception:
                pass
            sub['fiber'] = {'score': fscore,
                            'coverage': 'validated' if isinstance(fscore, (int, float)) else 'unavailable',
                            'basis': 'carrier facility presence + FCC fiber hex'}
        except Exception as e:
            sub['fiber'] = {'score': None, 'coverage': 'unavailable', 'basis': f'{type(e).__name__}'}

        # ── risk_resilience: prefer FEMA NRI (authoritative county hazard) —
        #    resilience = 100 - NRI composite risk; fall back to the site-level
        #    environmental screen (FEMA flood + FWS + NWI) when NRI is out of coverage. ──
        try:
            e = env if isinstance(env, dict) else screen_environmental(lat, lng)
            env_score = e.get('env_score') if isinstance(e, dict) else None
            nri_score = nri_rating = None
            try:
                import urllib.request as _u2
                import urllib.parse as _up2
                import json as _j2
                _nq = _up2.urlencode({
                    'geometry': _j2.dumps({'x': lng, 'y': lat, 'spatialReference': {'wkid': 4326}}),
                    'geometryType': 'esriGeometryPoint', 'inSR': '4326',
                    'spatialRel': 'esriSpatialRelIntersects', 'outFields': 'RISK_SCORE,RISK_RATNG',
                    'returnGeometry': 'false', 'f': 'json'})
                _nu = ('https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/'
                       'National_Risk_Index_Counties/FeatureServer/0/query?' + _nq)
                with _u2.urlopen(_u2.Request(_nu, headers={'User-Agent': 'dchub-composite/1.0'}), timeout=10) as _r:
                    _feats = (_j2.loads(_r.read(1_000_000).decode('utf-8', 'replace')) or {}).get('features') or []
                if _feats:
                    _a = _feats[0].get('attributes') or {}
                    _rs = _a.get('RISK_SCORE')
                    if isinstance(_rs, (int, float)):
                        nri_score = round(max(0.0, min(100.0, 100.0 - _rs)), 1)  # resilience = inverse of hazard
                        nri_rating = _a.get('RISK_RATNG')
            except Exception:
                nri_score = None
            if nri_score is not None:
                sub['risk_resilience'] = {'score': nri_score, 'coverage': 'validated',
                                          'basis': f'FEMA National Risk Index (county hazard: {nri_rating}); resilience = 100 − NRI risk',
                                          'nri_rating': nri_rating}
            else:
                sub['risk_resilience'] = {'score': env_score,
                                          'coverage': 'validated' if isinstance(env_score, (int, float)) else 'unavailable',
                                          'basis': ('FEMA flood + FWS critical habitat + NWI wetlands (NRI out of coverage)'
                                                    if isinstance(env_score, (int, float)) else
                                                    'no FEMA NRI county score here, and the FEMA flood + FWS critical habitat + NWI wetlands screen measured nothing')}
        except Exception as e:
            sub['risk_resilience'] = {'score': None, 'coverage': 'unavailable', 'basis': f'{type(e).__name__}'}

        # ── water: LIVE from the AUTHORITATIVE WRI Aqueduct 4.0 baseline water
        #    stress (Esri Living Atlas, point query). Suitability = 100 − stress
        #    (bws_score 0–5 → 0–100). Outside basin coverage → unavailable. The
        #    paused inverted state proxy is NEVER surfaced. ──
        try:
            import urllib.request as _u4
            import urllib.parse as _up4
            import json as _j4
            _aq = ('https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/'
                   'aqueduct_water_risk/FeatureServer/1/query?' + _up4.urlencode({
                       'geometry': _j4.dumps({'x': lng, 'y': lat, 'spatialReference': {'wkid': 4326}}),
                       'geometryType': 'esriGeometryPoint', 'inSR': '4326',
                       'spatialRel': 'esriSpatialRelIntersects',
                       'outFields': 'bws_score,bws_cat,bws_label,name_1', 'returnGeometry': 'false',
                       'resultRecordCount': '1', 'f': 'json'}))
            with _u4.urlopen(_u4.Request(_aq, headers={'User-Agent': 'dchub-composite/1.0'}), timeout=10) as _r:
                _wf = (_j4.loads(_r.read(1_000_000).decode('utf-8', 'replace')) or {}).get('features') or []
            _wa = (_wf[0].get('attributes') or {}) if _wf else {}
            _bws = _wa.get('bws_score')
            if isinstance(_bws, (int, float)):
                _wscore = round(max(0.0, min(100.0, 100.0 - _bws / 5.0 * 100.0)), 1)  # suitability = low stress
                sub['water'] = {'score': _wscore, 'coverage': 'validated',
                                'basis': f"WRI Aqueduct 4.0 baseline water stress: {_wa.get('bws_label')} ({_wa.get('name_1')})",
                                'bws_score_0_5': _bws, 'bws_category': _wa.get('bws_cat')}
            else:
                sub['water'] = {'score': None, 'coverage': 'unavailable',
                                'basis': 'Outside WRI Aqueduct basin coverage (offshore / no basin)'}
        except Exception as _we:
            logger.warning(f"composite water (Aqueduct) failed: {_we}")
            sub['water'] = {'score': None, 'coverage': 'unavailable', 'basis': 'WRI Aqueduct source unreachable'}

        # ── market_dcpi: declared but unavailable in v1 (no fabricated market score) ──
        sub['market_dcpi'] = {'score': None, 'coverage': 'unavailable',
                              'basis': 'v1: use rank_markets / get_market_dcpi_rank for the DCPI verdict; composite market synthesis planned'}

        # ── composite over VALIDATED factors ONLY (never impute a missing one) ──
        weights = {'power_grid': 0.32, 'fiber': 0.20, 'water': 0.18,
                   'risk_resilience': 0.15, 'market_dcpi': 0.15}
        num = den = 0.0
        validated = []
        for k, w in weights.items():
            s = sub.get(k) or {}
            if s.get('coverage') == 'validated' and isinstance(s.get('score'), (int, float)):
                num += float(s['score']) * w
                den += w
                validated.append(k)
        composite = round(num / den, 1) if den > 0 else None
        verdict = None
        if composite is not None:
            verdict = 'BUILD' if composite >= 70 else 'CAUTION' if composite >= 45 else 'AVOID'

        caveats = [c for c in [
            None if 'power_grid' in validated else f"power_grid: {(sub.get('power_grid') or {}).get('basis') or 'unavailable'}.",
            None if 'water' in validated else f"water: {(sub.get('water') or {}).get('basis') or 'unavailable'}.",
            'market_dcpi: unavailable in v1 — use rank_markets / get_market_dcpi_rank.',
            'natural-hazard layer is FEMA flood + FWS habitat + NWI wetlands only (no seismic/climate-projection layer yet).',
            'advisory only — pair with analyze_site (raw data), get_water_risk, and rank_markets.',
        ] if c]

        # Bound once and returned with its status: the response contract gate reads
        # a success payload through `resp = jsonify(...)` and `return resp, 200`.
        resp = jsonify({
            'success': True,
            '_entity': 'site',
            'location': {'lat': lat, 'lng': lng, 'state': state, 'address': address},
            'composite_score': composite,
            'verdict': verdict,
            'confidence': 'complete' if len(validated) == len(weights) else 'conditional',
            'coverage': {k: (sub.get(k) or {}).get('coverage') for k in weights},
            'coverage_ratio': f'{len(validated)}/{len(weights)}',
            'sub_scores': sub,
            'weights_over_validated': ({k: round(weights[k] / den, 3) for k in validated} if den > 0 else {}),
            'methodology': ('Weighted mean over VALIDATED factors only; unsourced factors are declared '
                            'unavailable, never imputed (constraint-coverage). Grid/fiber/hazard are live; '
                            'water auto-enables when the WRI ingest lands.'),
            'caveats': caveats,
            'meta': {'version': 'v1.0', 'timestamp': datetime.utcnow().isoformat()},
        })
        if power_grid_failed:
            # The memo keeps a 200 for 6 h. This one says a lookup failed, which
            # stops being true when the database answers again, so it goes out
            # no-store and the memo leaves it out (routes/_slow_tool_cache.py).
            resp.headers['Cache-Control'] = 'no-store'
        return resp, 200

    # ── GET/POST /api/v1/site-planner/disaster-risk ──
    @app.route('/api/v1/site-planner/disaster-risk', methods=['GET', 'POST'])
    @require_pro
    def site_planner_disaster_risk():
        """Natural-hazard risk for a lat/lon, grounded in the FEMA National Risk
        Index (NRI) — the authoritative county-level US hazard dataset. LIVE
        point-in-county query; NEVER fabricates. Returns the composite NRI risk
        score + rating + national percentile, all 18 hazard ratings, and the
        elevated 'top' hazards. Points outside US NRI coverage return
        coverage='unavailable' (declared, not estimated)."""
        import urllib.request as _u
        import urllib.parse as _up
        import json as _j
        data = flask_request.get_json(silent=True) or flask_request.values

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        lat = _f(data.get('lat'))
        lng = _f(data.get('lng') if data.get('lng') is not None else data.get('lon'))
        if lat is None or lng is None:
            return jsonify({'success': False, 'error': 'lat/lng required'}), 400

        _HZ = {'AVLN': 'Avalanche', 'CFLD': 'Coastal Flooding', 'CWAV': 'Cold Wave',
               'DRGT': 'Drought', 'ERQK': 'Earthquake', 'HAIL': 'Hail', 'HRCN': 'Hurricane',
               'HWAV': 'Heat Wave', 'IFLD': 'Riverine Flooding', 'ISTM': 'Ice Storm',
               'LNDS': 'Landslide', 'LTNG': 'Lightning', 'SWND': 'Strong Wind',
               'TRND': 'Tornado', 'TSUN': 'Tsunami', 'VLCN': 'Volcanic Activity',
               'WFIR': 'Wildfire', 'WNTW': 'Winter Weather'}
        _ELEV = {'Very High', 'Relatively High'}
        _NRI = ('https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/'
                'National_Risk_Index_Counties/FeatureServer/0/query')
        attrs = None
        try:
            out_fields = 'STATE,COUNTY,RISK_SCORE,RISK_RATNG,RISK_SPCTL,' + ','.join(f'{c}_RISKR' for c in _HZ)
            qs = _up.urlencode({
                'geometry': _j.dumps({'x': lng, 'y': lat, 'spatialReference': {'wkid': 4326}}),
                'geometryType': 'esriGeometryPoint', 'inSR': '4326',
                'spatialRel': 'esriSpatialRelIntersects', 'outFields': out_fields,
                'returnGeometry': 'false', 'f': 'json'})
            req = _u.Request(f'{_NRI}?{qs}', headers={'User-Agent': 'dchub-disaster-risk/1.0'})
            with _u.urlopen(req, timeout=12) as r:
                feats = (_j.loads(r.read(2_000_000).decode('utf-8', 'replace')) or {}).get('features') or []
            if feats:
                attrs = feats[0].get('attributes') or {}
        except Exception as e:
            logger.warning(f"FEMA NRI query failed: {e}")
            attrs = None

        if not attrs:
            return jsonify({'success': True, '_entity': 'risk',
                            'location': {'lat': lat, 'lng': lng},
                            'disaster_risk': None, 'coverage': 'unavailable',
                            'source': 'FEMA National Risk Index (NRI)',
                            'note': ('No NRI county intersects this point (outside US NRI coverage / '
                                     'offshore) or the source was unreachable — declared unavailable, '
                                     'never estimated.')})

        hazards = {}
        for code, name in _HZ.items():
            rr = attrs.get(f'{code}_RISKR')
            if rr and str(rr).strip() and str(rr).strip().lower() not in (
                    'not applicable', 'no rating', 'insufficient data', 'no expected annual losses'):
                hazards[name] = rr
        top = sorted([{'hazard': n, 'rating': r} for n, r in hazards.items() if r in _ELEV],
                     key=lambda h: 0 if h['rating'] == 'Very High' else 1)
        return jsonify({
            'success': True, '_entity': 'risk',
            'location': {'lat': lat, 'lng': lng,
                         'county': attrs.get('COUNTY'), 'state': attrs.get('STATE')},
            'disaster_risk': {
                'composite_score': attrs.get('RISK_SCORE'),
                'rating': attrs.get('RISK_RATNG'),
                'national_percentile': attrs.get('RISK_SPCTL'),
            },
            'hazards': hazards,
            'top_hazards': top,
            'coverage': 'validated',
            'source': 'FEMA National Risk Index (NRI), county-level',
            'methodology': ('Live point-in-county query of the FEMA NRI FeatureServer. Composite = '
                            'Expected Annual Loss × Social Vulnerability ÷ Community Resilience; '
                            'higher score = higher risk. 18 hazards rated Very Low→Very High.'),
            'caveats': ['County-level resolution (not parcel).',
                        'US only — points outside NRI coverage return coverage=unavailable.',
                        'Acute natural hazards; for chronic water stress use get_water_risk (WRI Aqueduct).'],
            'meta': {'version': 'v1.0', 'timestamp': datetime.utcnow().isoformat()},
        })

    # ── GET/POST /api/v1/site-planner/climate-intel ──
    # ── STATIC-SOURCE CACHE (2026-07-28, shell #38 lane 4) ──────────────────
    # get_climate_intel was the slowest tool on the platform (p50 3,120ms) and
    # the single failing check in the agent-wait lane. Cause: THREE SEQUENTIAL
    # external federal calls (USGS ASCE 7 -> ACIS StnMeta -> ACIS StnData), each
    # with a 12s timeout, so worst case is 36s of BLOCKED agent time.
    #
    # ★ Why caching is safe HERE when a response cache generally is not:
    #   the cached object is the UPSTREAM PUBLIC FEDERAL response, not our view
    #   of it. ASCE 7-16 seismic is a published building code (fixed); the NOAA
    #   normals are annual aggregates over a closed 2022-2024 window (fixed).
    #   It is identical for every caller, so there is NO tier axis to leak —
    #   unlike a cached tool RESPONSE, which is tier-varying and is a documented
    #   incident class here. The route is @require_pro, so gating already
    #   happened upstream of anything stored.
    # ★ NEVER cache a failure. A cached "unavailable" would pin a permanent hole
    #   in the data for that coordinate (same lesson as the MCP keyCache, which
    #   refuses to cache a validation failure because it silently downgrades a
    #   paid tier to free).
    # Kill switch, no deploy: CLIMATE_INTEL_CACHE=0
    _CI_CACHE = {}                      # (kind, lat_r, lng_r) -> (expires_at, value)
    _CI_TTL_S = 7 * 24 * 3600           # static sources; a week is conservative
    _CI_MAX   = 4000

    def _ci_key(kind, lat, lng, extra=''):
        # ~1km grid — far finer than either source's own resolution
        return (kind, round(float(lat), 2), round(float(lng), 2), extra)

    def _ci_rkey(key):
        return 'ci:' + ':'.join(str(x) for x in key)

    def _ci_get(key):
        if os.environ.get('CLIMATE_INTEL_CACHE') == '0':
            return None
        hit = _CI_CACHE.get(key)                     # L1: this process
        if hit and hit[0] >= time.time():
            return hit[1]
        if hit:
            _CI_CACHE.pop(key, None)
        # ★★ L2: SHARED across replicas. An in-process dict alone measured ZERO
        # hits in production (10/10 live calls missed) — the backend runs several
        # replicas, so a repeat request lands on a cold one. That is the SAME
        # per-replica-in-memory-state trap that caused the lane-3 instruction
        # contradiction. A process-local cache on a multi-replica service is
        # decoration, not a cache.
        try:
            from redis_cache import cache_get as _rc_get
            v = _rc_get(_ci_rkey(key))               # fails soft to None
            if v:
                _CI_CACHE[key] = (time.time() + _CI_TTL_S, v)   # promote to L1
                return v
        except Exception:
            pass
        return None

    def _ci_put(key, value):
        if os.environ.get('CLIMATE_INTEL_CACHE') == '0':
            return
        if len(_CI_CACHE) >= _CI_MAX:       # bounded; simplest correct eviction
            _CI_CACHE.clear()
        _CI_CACHE[key] = (time.time() + _CI_TTL_S, value)
        try:
            from redis_cache import cache_set as _rc_set
            _rc_set(_ci_rkey(key), value, ttl=_CI_TTL_S)
        except Exception:
            pass

    @app.route('/api/v1/site-planner/climate-intel', methods=['GET', 'POST'])
    @require_pro
    def site_planner_climate_intel():
        """Seismic + climate intel for a lat/lon, grounded STRICTLY in USGS
        (ASCE 7 seismic) and NOAA (climate normals via ACIS). Every number traces
        to a federal source; missing data is declared unavailable, never
        estimated. DC-relevant: seismic drives structural bracing cost; cooling
        degree-days + extreme temps drive cooling design (wet-bulb null when the
        source lacks it — never approximated)."""
        import urllib.request as _u
        import urllib.parse as _up
        import json as _j
        import math as _m
        data = flask_request.get_json(silent=True) or flask_request.values

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        lat = _f(data.get('lat'))
        lng = _f(data.get('lng') if data.get('lng') is not None else data.get('lon'))
        if lat is None or lng is None:
            return jsonify({'success': False, 'error': 'lat/lng required'}), 400
        radius_km = _f(data.get('radius_km') or data.get('search_radius_km')) or 25.0

        # ── seismic (USGS ASCE 7-16 building-codes) ──
        def _seismic_block():
            seismic = {'status': 'unavailable', 'source': 'USGS ASCE 7-16'}
            _k = _ci_key('seismic', lat, lng)
            _c = _ci_get(_k)
            if _c is not None:
                return _c
            try:
                su = ('https://earthquake.usgs.gov/ws/building-codes/asce7-16/calculate?'
                      + _up.urlencode({'latitude': lat, 'longitude': lng, 'riskCategory': 'III',
                                       'siteClass': 'D', 'title': 'dchub'}))
                req = _u.Request(su, headers={'User-Agent': 'dchub-climate-intel/1.0'})
                with _u.urlopen(req, timeout=12) as r:
                    sd = ((_j.loads(r.read(500_000).decode('utf-8', 'replace')) or {})
                          .get('response', {}).get('data', {}))
                pga = sd.get('pga')
                if isinstance(pga, (int, float)):
                    lvl = ('very high' if pga >= 0.6 else 'high' if pga >= 0.3
                           else 'moderate' if pga >= 0.1 else 'low')
                    seismic = {'status': 'available', 'source': 'USGS ASCE 7-16 (earthquake.usgs.gov)',
                               'peak_ground_acceleration_g': pga, 'ss': sd.get('ss'), 's1': sd.get('s1'),
                               'seismic_design_category': sd.get('sdc'), 'hazard_class': lvl,
                               'reference': 'ASCE 7-16, Risk Category III, Site Class D'}
            except Exception as e:
                logger.warning(f"climate-intel seismic failed: {e}")
            if seismic.get('status') == 'available':
                _ci_put(_k, seismic)      # success only — never pin a hole
            return seismic

        # ── climate normals (NOAA via ACIS, tokenless) ──
        def _noaa_block():
            climate = {'status': 'unavailable', 'source': 'NOAA (ACIS/NCEI)'}
            _k = _ci_key('noaa', lat, lng, str(radius_km))
            _c = _ci_get(_k)
            if _c is not None:
                return _c
            try:
                bb = f'{lng - 0.7},{lat - 0.7},{lng + 0.7},{lat + 0.7}'
                mreq = _u.Request('https://data.rcc-acis.org/StnMeta',
                                  data=_j.dumps({'bbox': bb, 'meta': 'name,ll,sids'}).encode(),
                                  headers={'Content-Type': 'application/json',
                                           'User-Agent': 'dchub-climate-intel/1.0'})
                with _u.urlopen(mreq, timeout=12) as r:
                    stns = (_j.loads(r.read(2_000_000).decode('utf-8', 'replace')) or {}).get('meta') or []

                def _hav(la1, lo1, la2, lo2):
                    R = 6371.0
                    p = _m.pi / 180
                    a = (_m.sin((la2 - la1) * p / 2) ** 2
                         + _m.cos(la1 * p) * _m.cos(la2 * p) * _m.sin((lo2 - lo1) * p / 2) ** 2)
                    return 2 * R * _m.asin(min(1.0, _m.sqrt(a)))
                best = None
                for s in stns:
                    ll = s.get('ll')
                    sids = s.get('sids') or []
                    if not ll or not sids:
                        continue
                    d = _hav(lat, lng, ll[1], ll[0])
                    if best is None or d < best[0]:
                        best = (d, s)
                if best is None:
                    climate = {'status': 'unavailable', 'reason': 'no_station_in_area',
                               'source': 'NOAA (ACIS/NCEI)'}
                elif best[0] > radius_km:
                    climate = {'status': 'unavailable_exceeds_radius', 'source': 'NOAA (ACIS/NCEI)',
                               'reason': (f'nearest NOAA station {round(best[0], 1)}km > radius '
                                          f'{radius_km}km — climate normals not estimated')}
                else:
                    s = best[1]
                    sid = (s.get('sids') or [''])[0].split(' ')[0]
                    dreq = _u.Request('https://data.rcc-acis.org/StnData',
                                      data=_j.dumps({'sid': sid, 'sdate': '2022-01-01', 'edate': '2024-12-31',
                                                     'elems': [{'name': 'cdd', 'interval': 'yly', 'duration': 'yly', 'reduce': 'sum', 'base': 65},
                                                               {'name': 'maxt', 'interval': 'yly', 'duration': 'yly', 'reduce': 'max'}]}).encode(),
                                      headers={'Content-Type': 'application/json',
                                               'User-Agent': 'dchub-climate-intel/1.0'})
                    with _u.urlopen(dreq, timeout=12) as r:
                        dd = (_j.loads(r.read(500_000).decode('utf-8', 'replace')) or {}).get('data') or []
                    cdd = maxt = vintage = None
                    for row in reversed(dd):
                        def _n(x):
                            try:
                                return float(x)
                            except (TypeError, ValueError):
                                return None
                        if _n(row[1]) is not None or _n(row[2]) is not None:
                            cdd, maxt, vintage = _n(row[1]), _n(row[2]), row[0]
                            break
                    climate = {'status': 'available', 'source': 'NOAA (ACIS/NCEI)',
                               'reference_station': {'id': sid, 'name': s.get('name'),
                                                     'distance_km': round(best[0], 1)},
                               'cooling_design_metrics': {
                                   'cooling_degree_days_annual': cdd,
                                   'extreme_max_dry_bulb_f': maxt,
                                   'extreme_max_wet_bulb_f': None,
                                   'data_vintage': vintage},
                               'note': ('Annual values, latest available year at the nearest NOAA station. '
                                        'Wet-bulb null (not in source; never estimated).')}
            except Exception as e:
                logger.warning(f"climate-intel normals failed: {e}")
            if climate.get('status') == 'available':
                _ci_put(_k, climate)      # success only — never pin a hole
            return climate

        # ★ The USGS and NOAA branches are INDEPENDENT — run them concurrently so
        #   wall time is max(a,b), not a+b. (The two ACIS calls stay sequential
        #   inside the NOAA branch: StnData needs the station id StnMeta mints.)
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as _ex:
                _fs, _fc = _ex.submit(_seismic_block), _ex.submit(_noaa_block)
                seismic, climate = _fs.result(), _fc.result()
        except Exception as e:                       # fail-soft to sequential
            logger.warning(f"climate-intel parallel failed, falling back: {e}")
            seismic, climate = _seismic_block(), _noaa_block()

        parts = []
        if seismic.get('status') == 'available':
            parts.append(f"seismic {seismic.get('hazard_class')} (PGA {seismic.get('peak_ground_acceleration_g')}g)")
        if climate.get('status') == 'available':
            _cdd = (climate.get('cooling_design_metrics') or {}).get('cooling_degree_days_annual')
            if _cdd is not None:
                parts.append(f"~{int(_cdd)} cooling degree-days/yr")
        return jsonify({
            'success': True, '_entity': 'climate',
            'site_coordinates': {'lat': lat, 'lon': lng},
            'seismic_hazard_usgs': seismic,
            'climate_normals_noaa': climate,
            'overall_climate_summary': ('; '.join(parts) if parts else None),
            'data_availability': {'seismic': seismic.get('status'), 'climate_normals': climate.get('status')},
            'sources': ['USGS ASCE 7-16', 'NOAA ACIS/NCEI'],
            'caveats': ['Seismic = ASCE 7-16 (US); non-US points may return seismic unavailable.',
                        'Climate from the nearest NOAA station (US/territories); beyond radius → unavailable, never interpolated.',
                        'Wet-bulb reported null when the source lacks it — never approximated.'],
            'meta': {'version': 'v1.0', 'timestamp': datetime.utcnow().isoformat()},
        })

# AUTO-REPAIR: duplicate route '/api/v1/site-planner/compare' also in site_planner.py:1680 — review and remove one
    # ── POST /api/v1/site-planner/compare ──
    @app.route('/api/v1/site-planner/compare', methods=['POST'])
    @require_pro
    def site_planner_compare():
        """
        Compare 2-3 sites side by side.
        
        Request body:
          { "sites": [
              { "address": "123 Main St, Dallas, TX" },
              { "lat": 39.0, "lng": -77.5 },
              { "address": "1 Cyclotron Rd, Berkeley, CA" }
          ]}
        """
        data = flask_request.get_json()
        if not data or 'sites' not in data:
            return jsonify({'success': False, 'error': 'sites array required'}), 400
        
        sites = data['sites']
        if len(sites) < 2 or len(sites) > 3:
            return jsonify({'success': False, 'error': '2-3 sites required for comparison'}), 400
        
        results = []
        for site_input in sites:
            # Run full analysis for each site
            lat = site_input.get('lat')
            lng = site_input.get('lng')
            address = site_input.get('address', '')
            
            if address and (not lat or not lng):
                geo = geocode_address(address)
                if geo:
                    lat, lng = geo['lat'], geo['lng']
                    address = geo.get('display_name', address)
            
            if not lat or not lng:
                results.append({'error': f'Could not geocode: {address}'})
                continue
            
            subs = find_nearest_substations(lat, lng, limit=5)
            # Read before the dedupe turns None into []: None means the lookup did not
            # run for this site, which its row and the recommendation both say.
            subs_coverage, subs_basis = _substations_coverage(subs)
            # Deduplicate
            seen = set()
            subs = [s for s in (subs or []) if s.get('name') not in seen and not seen.add(s.get('name'))]
            
            tx, tx_measured = find_nearest_transmission_measured(lat, lng)
            # None both when no line is anchored near this site and when a statement did
            # not run, which its row and the recommendation both say.
            tx_coverage, tx_basis = _transmission_coverage(tx_measured)
            state_code = site_input.get('state', '')
            iso = identify_iso_region(lat, lng, state_code)
            env = screen_environmental(lat, lng)
            congestion = estimate_congestion(lat, lng)
            gen_mix = get_generation_mix(lat, lng)
            nearby_dcs = find_nearby_facilities(lat, lng)
            gas = find_nearby_gas_pipelines(lat, lng)
            power_pricing = get_power_pricing(iso.get('name', 'SERC'))
            water = assess_water_risk(state_code)
            scoring = compute_suitability_score(subs, tx, iso, env, congestion, gas=gas, nearby_dcs=nearby_dcs)
            
            results.append({
                'address': address,
                'lat': lat,
                'lng': lng,
                'score': scoring['score'],
                'nearest_sub_miles': subs[0]['distance_miles'] if subs else None,
                'nearest_sub_voltage': subs[0]['voltage_kv'] if subs else None,
                'nearest_sub_name': subs[0]['name'] if subs else None,
                'substations_coverage': subs_coverage,
                'substations_basis': subs_basis,
                'nearest_tx_miles': tx.get('distance_miles') if tx else None,
                'nearest_tx_voltage': tx.get('voltage_kv') if tx else None,
                'transmission_coverage': tx_coverage,
                'transmission_basis': tx_basis,
                'iso': iso.get('name'),
                'queue_mw': estimate_queue_depth(iso.get('name','SERC'), subs[0].get('voltage_kv',0)).get('queue_mw') if subs else None,
                'congestion': congestion.get('level'),
                'env_score': env.get('env_score'),
                'flood_risk': env.get('flood_risk'),
                'wetland_risk': env.get('wetland_risk'),
                'species_risk': env.get('species_risk'),
                'nearby_dc_count': nearby_dcs.get('count', 0),
                'nearby_dc_corridor': nearby_dcs.get('corridor_signal', 'Unknown'),
                'power_price_mwh': power_pricing.get('avg_wholesale_price_mwh'),
                'water_stress': water.get('water_stress_level', 'Unknown'),
                'connectivity': check_fiber_proximity(lat, lng).get('connectivity_rating', 'Unknown'),
                'generation_mix': gen_mix,
                'score_breakdown': scoring['breakdown'],
            })
        
        # Determine recommendation
        scored = [r for r in results if 'score' in r and 'error' not in r]
        recommendation = None
        if scored:
            best = max(scored, key=lambda r: r['score'])
            recommendation = {
                'best_site': best['address'],
                'score': best['score'],
                'reason': _generate_recommendation_reason(best, scored),
            }
        
        return jsonify({
            'success': True,
            'comparison': results,
            'recommendation': recommendation,
            'timestamp': datetime.utcnow().isoformat(),
        })

    # ── GET /api/v1/site-planner/queue-depth ──
    @app.route('/api/v1/site-planner/queue-depth', methods=['GET'])
    @require_pro
    def site_planner_queue_depth():
        """
        Get queue depth data for all ISO regions or a specific one.
        Query params: %siso=PJM (optional)
        """
        iso_filter = flask_request.args.get('iso', '').upper()
        
        if iso_filter and iso_filter in ISO_REGIONS:
            region = ISO_REGIONS[iso_filter]
            return jsonify({
                'success': True,
                'iso': iso_filter,
                'queue_depth_gw': region['queue_depth_gw'],
                'avg_wait_years': region['avg_queue_wait_years'],
                'queue_url': region.get('queue_url'),
                'states': region['states'],
            })
        
        # Return all regions
        all_regions = {}
        for name, data in ISO_REGIONS.items():
            all_regions[name] = {
                'queue_depth_gw': data['queue_depth_gw'],
                'avg_wait_years': data['avg_queue_wait_years'],
                'queue_url': data.get('queue_url'),
                'states': data['states'],
            }
        
        return jsonify({
            'success': True,
            'regions': all_regions,
            'total_queue_gw': sum(d['queue_depth_gw'] for d in ISO_REGIONS.values()),
        })

    logger.info("✅ Site Planner routes registered (Pro-only)")


# The lookups compare marks on each row: the coverage field the row carries, what the
# recommendation says when that lookup did not run, and the score factors it leaves out.
_UNMEASURED_LOOKUPS = (
    ('substations_coverage', 'substation lookup did not run',
     'substation proximity, voltage and queue depth'),
    ('transmission_coverage', 'transmission lookup did not complete', 'transmission proximity'),
)


def _generate_recommendation_reason(best, all_sites):
    """Generate a human-readable recommendation."""
    reasons = []
    if best.get('score', 0) >= 70:
        reasons.append("strong overall suitability score")
    if best.get('nearest_sub_miles') and best['nearest_sub_miles'] < 5:
        reasons.append(f"close substation access ({best['nearest_sub_miles']:.1f} mi)")
    if best.get('env_score') and best['env_score'] > 70:
        reasons.append("low environmental risk")
    if best.get('congestion') == 'Low':
        reasons.append("low grid congestion")
    
    if not reasons:
        reasons.append("highest composite interconnection suitability")
    
    reason = f"Recommended due to {', '.join(reasons)}."
    # A site whose substation or transmission lookup did not run is scored without
    # that lookup's factors. Say so, and name it. The scores are like-for-like only
    # when every site is missing the same lookups.
    sentences = []
    for field, what, left_out in _UNMEASURED_LOOKUPS:
        unmeasured = [s.get('address') or f"{s.get('lat')}, {s.get('lng')}"
                      for s in all_sites if s.get(field) == 'unavailable']
        if unmeasured:
            sentences.append(
                f"The {what} for {' and '.join(unmeasured)}, so "
                f"{'its score leaves' if len(unmeasured) == 1 else 'their scores leave'} out {left_out}")
    if sentences:
        gaps = {tuple(s.get(field) == 'unavailable' for field, _, _ in _UNMEASURED_LOOKUPS)
                for s in all_sites}
        reason += (' ' + '. '.join(sentences)
                   + ('; the scores are not like-for-like.' if len(gaps) > 1 else '.'))
    return reason
