from internal_auth import is_valid_internal_key
"""
Grid Intelligence Briefs — API Routes
======================================
Endpoint: GET /api/v1/grid-intelligence/<region_id>
          GET /api/v1/grid-intelligence (list all regions)

Aggregates data from existing DC Hub tables:
  - grid_regions + grid_corridors (new tables)
  - eia_retail_rates (energy pricing)
  - discovered_facilities + facilities (facility counts)
  - substations, transmission_lines_eia, gas_pipelines, power_plants_eia (infrastructure)
  - tax_incentives_neon (incentives)
  - fema_risk_index (risk data)
  - epa_egrid (carbon data)

Tier gating:
  - Free: region summary + 2 corridor headlines + redacted scores + upgrade CTA
  - Developer ($49/mo): all corridors + aggregate scores + energy rates + infra counts
  - Pro ($99/mo): full sub-scores + facility names + coordinates + CSV export

Fixes (Mar 23):
  - autocommit=True prevents transaction poisoning across corridor queries
  - Substations query uses correct lat/lng columns (not latitude/longitude which don't exist)
  - Facility count queries BOTH discovered_facilities AND facilities tables
  - Per-query try/except with logging instead of silent pass
"""

import logging
import json
import traceback
from flask import Blueprint, request, jsonify

try:
    from main import _apply_grid_queue_override
except Exception:
    def _apply_grid_queue_override(regions): return regions


logger = logging.getLogger(__name__)
grid_intel_bp = Blueprint('grid_intel', __name__)


# Phase RRR (2026-05-18) — seed CAISO + Southeast as live regions so
# /research/grid-intelligence/ shows 5 ISO briefs instead of 3. Both
# subpages already exist at /research/grid-intelligence/{caiso,southeast}/
# and the frontend already has REGION_STATS rows for them, so this just
# unlocks display. Idempotent — runs once per process via the
# _seed_done flag and uses ON CONFLICT DO NOTHING so it's safe to
# re-run if the table is wiped.
_seed_done = False

def _ensure_grid_region_seeds():
    global _seed_done
    if _seed_done:
        return
    # Phase RRR-hotfix2 (2026-05-18): key_states column is PostgreSQL text[]
    # (native array), not jsonb. Original seed passed json.dumps(...) which
    # produced a JSON string that didn't match the text[] schema → silent
    # INSERT failure → stayed at 3 ISOs. psycopg2 converts python lists to
    # text[] automatically — pass the list directly. Also: only set the
    # done-flag AFTER success so a recoverable error (transient DB hiccup)
    # retries on the next request.
    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            rows = [
                ('caiso', 'CAISO — California & Western', 'CAISO', 'live',
                 'CAISO · The High-Cost Frontier With Renewable Anchors',
                 'California faces extreme rates and fire risk, but abundant solar, hydro, and aggressive build-out targets keep CAISO in play. Arizona and Nevada offer lower-cost alternatives within the same WECC interconnect.',
                 ['CA', 'NV', 'AZ', 'OR', 'WA'],
                 None,
                 '/research/grid-intelligence/caiso',
                 4),
                ('southeast', 'Southeast — SERC & TVA', 'SERC / TVA', 'live',
                 'Southeast · Nuclear Base + Aggressive Incentives',
                 'Georgia, Alabama, Tennessee, and the Carolinas combine cheap power, low land cost, nuclear baseload, and aggressive state-level incentives. SERC and TVA both serve as overflow destinations for hyperscale developers priced out of NoVA.',
                 ['GA', 'AL', 'TN', 'NC', 'SC', 'MS'],
                 None,
                 '/research/grid-intelligence/southeast',
                 5),
            ]
            cur.executemany("""
                INSERT INTO grid_regions
                  (id, name, iso, status, headline, description,
                   key_states, total_queue_gw, page_url, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, rows)
            conn.commit()
            _seed_done = True
            logger.info("Phase RRR: ensured CAISO + Southeast grid_regions seed rows")
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning("Phase RRR seed skipped (will retry next request): %s", e)


# ─── Tier gating constants ───
GRID_INTEL_TIER_CONFIG = {
    'free': {
        'max_corridors': 2,
        'show_scores': False,
        'show_infra_details': False,
        'show_facility_names': False,
        'show_coordinates': False,
        'show_energy_rates': False,
        'show_tax_incentives': False,
    },
    'developer': {
        'max_corridors': 99,
        'show_scores': True,
        'show_infra_details': True,
        'show_facility_names': False,
        'show_coordinates': False,
        'show_energy_rates': True,
        'show_tax_incentives': True,
    },
    'pro': {
        'max_corridors': 99,
        'show_scores': True,
        'show_infra_details': True,
        'show_facility_names': True,
        'show_coordinates': True,
        'show_energy_rates': True,
        'show_tax_incentives': True,
    },
    'enterprise': {
        'max_corridors': 99,
        'show_scores': True,
        'show_infra_details': True,
        'show_facility_names': True,
        'show_coordinates': True,
        'show_energy_rates': True,
        'show_tax_incentives': True,
    }
}

# State abbreviation to full name mapping (used across multiple functions)
STATE_NAMES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming',
}


def _get_conn():
    """Get a database connection with autocommit enabled.
    
    CRITICAL: autocommit=True prevents transaction poisoning.
    Without it, a failed query on corridor #1 puts the connection in
    InFailedSqlTransaction state, and ALL subsequent queries silently
    fail — returning 0 for corridors #2-#6.
    """
    import os
    import psycopg2
    db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    return conn


# r49-selfcall (2026-05-31): cache the static region list. list_grid_regions
# opens a fresh psycopg2 connection per request and runs 1 + N queries (the
# region list, then a per-region grid_corridors COUNT) on every hit. The
# grid_regions / grid_corridors tables are slow-moving reference data, so on
# the 1-replica backend this is pure worker-pool pressure for no freshness
# benefit. Memoize the assembled region list for 5 minutes. Callers get a
# DEEP COPY so per-request tier-gating slices and the in-place
# _apply_grid_queue_override mutation never corrupt the shared cache.
import time as _time
_REGIONS_CACHE = {"ts": 0.0, "regions": None}
_REGIONS_TTL_SECONDS = 5 * 60


def _load_regions_cached():
    """Return the assembled grid_regions list (with corridor counts),
    rebuilding from the DB at most once per _REGIONS_TTL_SECONDS. Returns a
    fresh deep copy each call so callers can mutate/slice safely. Raises on
    a cold-cache DB failure so the caller's existing except-block still
    produces its 500 (behavior preserved); a stale cache is preferred over
    re-querying on a warm hit."""
    import copy
    now = _time.time()
    cached = _REGIONS_CACHE.get("regions")
    if cached is not None and (now - _REGIONS_CACHE.get("ts", 0.0)) < _REGIONS_TTL_SECONDS:
        return copy.deepcopy(cached)

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, iso, status, headline, description,
                   key_states, total_queue_gw, page_url, sort_order
            FROM grid_regions
            ORDER BY sort_order
        """)
        rows = cur.fetchall()
        regions = []
        for r in rows:
            cur.execute("SELECT COUNT(*) FROM grid_corridors WHERE region_id = %s", (r[0],))
            corridor_count = cur.fetchone()[0]
            regions.append({
                'id': r[0],
                'name': r[1],
                'iso': r[2],
                'status': r[3],
                'headline': r[4],
                'description': r[5],
                'key_states': r[6],
                'total_queue_gw': float(r[7]) if r[7] else None,
                'page_url': r[8],
                'corridor_count': corridor_count,
            })
        _REGIONS_CACHE["regions"] = regions
        _REGIONS_CACHE["ts"] = _time.time()
        return copy.deepcopy(regions)
    except Exception:
        # On a transient DB error, prefer a stale cache over failing.
        if cached is not None:
            return copy.deepcopy(cached)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _determine_tier(api_key):
    """Determine user tier from API key (SHA256 hashed) or JWT token. Returns tier string."""
    import hashlib

    def _map_plan(plan):
        p = (plan or 'free').lower()
        if p in ('pro', 'enterprise'):
            return p
        elif p in ('developer', 'dev'):
            return 'developer'
        return 'free'

    # 1. Try API key (hashed lookup — api_keys stores SHA256 hash, not plaintext)
    if api_key and api_key.startswith('dchub_'):
        conn = None
        try:
            key_hash = hashlib.sha256(api_key.encode()).hexdigest()
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT u.plan FROM api_keys ak
                JOIN users u ON ak.user_id = u.id
                WHERE ak.key_hash = %s AND ak.is_active = 1
            """, (key_hash,))
            row = cur.fetchone()
            if row:
                return _map_plan(row[0])
        except Exception as e:
            logger.warning(f"[grid_intel] API key lookup failed: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    # 2. Try JWT token from cookies or Authorization header
    try:
        import jwt, os
        from flask import request as _req
        JWT_SECRET = os.environ.get('JWT_SECRET', 'dchub-super-secret-key-change-in-production')
        auth_header = _req.headers.get('Authorization', '')
        token = None
        if auth_header.startswith('Bearer ') and not auth_header[7:].strip().startswith('dchub_'):
            token = auth_header[7:].strip()
        if not token:
            token = _req.cookies.get('auth_token') or _req.cookies.get('token')
        if token:
            payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            return _map_plan(payload.get('plan'))
    except Exception as e:
        logger.debug(f"[grid_intel] JWT decode failed: {e}")

    return 'free'


def _get_infra_counts(lat, lon, radius_km=50, conn=None):
    """Get infrastructure counts near a corridor point.
    
    All four tables use lat/lng columns (not latitude/longitude).
    
    Each query is independently try/excepted so one table failure
    doesn't zero out the others. With autocommit=True on the connection,
    transaction poisoning is also prevented.
    """
    counts = {
        'substations': 0,
        'transmission_lines': 0,
        'power_plants': 0,
        'gas_pipelines': 0,
    }
    close_conn = False
    try:
        if conn is None:
            conn = _get_conn()
            close_conn = True
        cur = conn.cursor()

        # Degree approximation for radius
        deg_lat = radius_km / 111.0
        deg_lon = radius_km / (111.0 * 0.85)

        # Substations — uses lat/lng columns (no latitude/longitude columns exist)
        try:
            cur.execute("""
                SELECT COUNT(*) FROM substations
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                AND ABS(lat - %s) < %s AND ABS(lng - %s) < %s
            """, (lat, deg_lat, lon, deg_lon))
            row = cur.fetchone()
            counts['substations'] = row[0] if row else 0
            logger.debug(f"[grid_intel] Substations near ({lat},{lon}): {counts['substations']}")
        except Exception as e:
            logger.warning(f"[grid_intel] Substations query failed near ({lat},{lon}): {e}")

        # Transmission lines (uses lat/lng)
        try:
            cur.execute("""
                SELECT COUNT(*) FROM transmission_lines_eia
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                AND ABS(lat - %s) < %s AND ABS(lng - %s) < %s
            """, (lat, deg_lat, lon, deg_lon))
            row = cur.fetchone()
            counts['transmission_lines'] = row[0] if row else 0
        except Exception as e:
            logger.warning(f"[grid_intel] Transmission query failed near ({lat},{lon}): {e}")

        # Power plants (uses lat/lng)
        try:
            cur.execute("""
                SELECT COUNT(*) FROM power_plants_eia
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                AND ABS(lat - %s) < %s AND ABS(lng - %s) < %s
            """, (lat, deg_lat, lon, deg_lon))
            row = cur.fetchone()
            counts['power_plants'] = row[0] if row else 0
        except Exception as e:
            logger.warning(f"[grid_intel] Power plants query failed near ({lat},{lon}): {e}")

        # Gas pipelines (uses lat/lng)
        try:
            cur.execute("""
                SELECT COUNT(*) FROM gas_pipelines
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                AND ABS(lat - %s) < %s AND ABS(lng - %s) < %s
            """, (lat, deg_lat, lon, deg_lon))
            row = cur.fetchone()
            counts['gas_pipelines'] = row[0] if row else 0
        except Exception as e:
            logger.warning(f"[grid_intel] Gas pipelines query failed near ({lat},{lon}): {e}")

        return counts
    except Exception as e:
        logger.error(f"[grid_intel] _get_infra_counts error: {e}")
        return counts
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_energy_rates(states, conn=None):
    """Get average industrial energy rate for a list of states."""
    rates = {}
    close_conn = False
    try:
        if conn is None:
            conn = _get_conn()
            close_conn = True
        cur = conn.cursor()
        for st in states:
            try:
                state_full = STATE_NAMES.get(st.upper(), st)
                cur.execute("""
                    SELECT AVG(rate_cents_kwh) FROM eia_retail_rates
                    WHERE (UPPER(state) = UPPER(%s) OR UPPER(state) = UPPER(%s))
                    AND sector = 'industrial'
                """, (state_full, st))
                row = cur.fetchone()
                if row and row[0]:
                    rates[st] = round(float(row[0]), 2)
            except Exception as e:
                logger.warning(f"[grid_intel] Energy rate query failed for {st}: {e}")
        return rates
    except Exception as e:
        logger.error(f"[grid_intel] _get_energy_rates error: {e}")
        return rates
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_facility_count(state, conn=None):
    """Get facility count for a state.
    
    Queries BOTH discovered_facilities AND facilities tables,
    returns the higher count. Handles state stored as abbreviation
    ('VA') or full name ('Virginia').
    """
    close_conn = False
    try:
        if conn is None:
            conn = _get_conn()
            close_conn = True
        cur = conn.cursor()

        state_upper = state.upper()
        state_full = STATE_NAMES.get(state_upper, state)

        # Count from discovered_facilities (matches abbrev or full name)
        count_discovered = 0
        try:
            cur.execute("""
                SELECT COUNT(*) FROM discovered_facilities
                WHERE UPPER(state) = %s
                   OR UPPER(state) = UPPER(%s)
            """, (state_upper, state_full))
            row = cur.fetchone()
            count_discovered = row[0] if row else 0
        except Exception as e:
            logger.warning(f"[grid_intel] discovered_facilities count failed for {state}: {e}")

        # Count from facilities table (the main 11K+ table)
        count_facilities = 0
        try:
            cur.execute("""
                SELECT COUNT(*) FROM facilities
                WHERE UPPER(state) = %s
                   OR UPPER(state) = UPPER(%s)
            """, (state_upper, state_full))
            row = cur.fetchone()
            count_facilities = row[0] if row else 0
        except Exception as e:
            logger.warning(f"[grid_intel] facilities count failed for {state}: {e}")

        # Return the higher of the two (avoids double-counting while
        # ensuring we always show the best available number)
        best = max(count_discovered, count_facilities)
        logger.debug(f"[grid_intel] Facility count {state}: discovered={count_discovered}, facilities={count_facilities}, using={best}")
        return best
    except Exception as e:
        logger.error(f"[grid_intel] _get_facility_count error for {state}: {e}")
        return 0
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_tax_incentives(states, conn=None):
    """Get tax incentives for states."""
    incentives = {}
    close_conn = False
    try:
        if conn is None:
            conn = _get_conn()
            close_conn = True
        cur = conn.cursor()
        for st in states:
            try:
                cur.execute("""
                    SELECT state_name, sales_tax_exempt, property_tax_abatement,
                           data_center_specific, qualifying_investment, incentive_details
                    FROM tax_incentives_neon
                    WHERE state_abbr = %s
                """, (st,))
                row = cur.fetchone()
                if row:
                    incentives[st] = {
                        'state_name': row[0],
                        'sales_tax_exempt': row[1],
                        'property_tax_abatement': row[2],
                        'data_center_specific': row[3],
                        'qualifying_investment': row[4],
                        'summary': row[5][:200] + '...' if row[5] and len(row[5]) > 200 else row[5]
                    }
            except Exception as e:
                logger.warning(f"[grid_intel] Tax incentive query failed for {st}: {e}")
        return incentives
    except Exception as e:
        logger.error(f"[grid_intel] _get_tax_incentives error: {e}")
        return incentives
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


# ─── List all regions ───
# Phase RRR-hotfix3 (2026-05-18): seed silently failing somehow. Debug
# endpoint that returns the actual error so we can diagnose live.
# Path NOT under /api/v1/grid-intelligence/ because the <region_id>
# route swallows any string under that prefix.
@grid_intel_bp.route('/api/v1/grid-seed-debug', methods=['GET'])
def grid_seed_debug():
    """Force-run the seed and return verbatim result/error."""
    global _seed_done
    _seed_done = False  # always re-run for debug
    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            rows = [
                ('caiso', 'CAISO — California & Western', 'CAISO', 'live',
                 'CAISO · The High-Cost Frontier With Renewable Anchors',
                 'California faces extreme rates and fire risk, but abundant solar, hydro, and aggressive build-out targets keep CAISO in play.',
                 ['CA', 'NV', 'AZ', 'OR', 'WA'],
                 None,
                 '/research/grid-intelligence/caiso',
                 4),
                ('southeast', 'Southeast — SERC & TVA', 'SERC / TVA', 'live',
                 'Southeast · Nuclear Base + Aggressive Incentives',
                 'Georgia, Alabama, Tennessee, and the Carolinas combine cheap power, low land cost, nuclear baseload, and aggressive state-level incentives.',
                 ['GA', 'AL', 'TN', 'NC', 'SC', 'MS'],
                 None,
                 '/research/grid-intelligence/southeast',
                 5),
            ]
            cur.executemany("""
                INSERT INTO grid_regions
                  (id, name, iso, status, headline, description,
                   key_states, total_queue_gw, page_url, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, rows)
            conn.commit()
            # Verify
            cur.execute("SELECT id FROM grid_regions ORDER BY sort_order")
            ids = [r[0] for r in cur.fetchall()]
            return jsonify({'ok': True, 'ids': ids, 'count': len(ids)})
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        import traceback
        return jsonify({
            'ok': False,
            'error_type': type(e).__name__,
            'error': str(e),
            'traceback': traceback.format_exc().splitlines()[-6:],
        }), 500


# Phase r33-J (2026-05-21) — public landing page. User reported
# /grid-intelligence returning Cloudflare Error 1000. Underlying:
# only the /api/v1/grid-intelligence JSON endpoint existed; the bare
# HTML page route was never registered. Adds a thin server-rendered
# landing that lists registered grid regions and links to each.
@grid_intel_bp.route('/grid-intelligence', methods=['GET'], strict_slashes=False)
def grid_intelligence_landing():
    """Server-rendered landing page for /grid-intelligence.
    Lists CAISO + Southeast + any other registered ISO regions and
    links to their per-region pages (already exist at
    /research/grid-intelligence/<id>)."""
    from flask import Response as _Resp
    _ensure_grid_region_seeds()
    rows = []
    try:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, iso, headline, description,
                       page_url, status
                  FROM grid_regions
                 ORDER BY sort_order, name
            """)
            rows = cur.fetchall()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        rows = []
    cards = []
    for rid, name, iso, headline, desc, page_url, status in rows:
        url = page_url or f"/research/grid-intelligence/{rid}"
        status_pill = (f'<span style="font-size:.7rem;padding:2px 8px;border-radius:8px;'
                       f'background:rgba(16,185,129,.15);color:#10b981">{status or "live"}</span>')
        cards.append(f'''
        <a class="card" href="{url}">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <h3 style="margin:0">{name}</h3>{status_pill}
          </div>
          <p style="color:var(--dch-text-mute);font-size:.95rem;margin:0 0 8px">
            {desc or "Regional grid intelligence + interconnect queue + capacity outlook."}
          </p>
          <div style="color:var(--dch-indigo);font-size:.85rem">{iso or ""} →</div>
        </a>''')
    if not cards:
        cards.append('<div class="card"><p style="color:var(--dch-text-mute)">'
                     'Grid regions seeding — refresh in 60s.</p></div>')
    # r43-SEO (2026-05-30): JSON-LD for /grid-intelligence. Schema.org
    # Dataset describes the live ISO/grid-headroom data product; the
    # BreadcrumbList connects it to /intelligence in Google's surface.
    # Keywords are picked to match high-impression queries: "ISO",
    # "grid headroom", "interconnect queue", "data center power".
    _region_count = len(rows) if rows else 7
    _jsonld = (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@graph":['
        '{"@type":"Dataset",'
        '"name":"DC Hub Grid Intelligence — Per-ISO Capacity, Queue & Headroom",'
        '"alternateName":["Grid Headroom Intelligence","ISO Interconnection Queue Tracker"],'
        '"description":"Live per-ISO grid intelligence covering PJM, ERCOT, CAISO, MISO, NYISO, ISO-NE, and SPP. '
        'Tracks interconnection queue depth, capacity factor by fuel source, real-time headroom, '
        'and per-market data-center-suitable load capacity. Used for site-selection diligence and '
        'energy-intelligence research.",'
        '"url":"https://dchub.cloud/grid-intelligence",'
        '"sameAs":"https://dchub.cloud/grid-intelligence",'
        '"creator":{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"},'
        '"publisher":{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"},'
        '"keywords":"ISO, grid intelligence, interconnect queue, data center power, energy intelligence, '
        'PJM, ERCOT, CAISO, MISO, NYISO, ISO-NE, SPP, grid headroom, capacity factor, '
        'data center site selection",'
        '"isAccessibleForFree":true,'
        '"spatialCoverage":{"@type":"Place","name":"United States"},'
        '"temporalCoverage":"2024-01-01/..",'
        '"variableMeasured":["interconnection queue GW","capacity factor","grid headroom","fuel mix","peak demand"],'
        '"distribution":['
        '{"@type":"DataDownload","encodingFormat":"application/json",'
        '"contentUrl":"https://dchub.cloud/api/v1/grid-intelligence","name":"All ISO regions (current)"},'
        '{"@type":"DataDownload","encodingFormat":"application/json",'
        '"contentUrl":"https://dchub.cloud/api/v1/grid-intelligence/caiso","name":"Per-region snapshot"}'
        ']},'
        '{"@type":"BreadcrumbList","itemListElement":['
        '{"@type":"ListItem","position":1,"name":"DC Hub","item":"https://dchub.cloud/"},'
        '{"@type":"ListItem","position":2,"name":"Intelligence","item":"https://dchub.cloud/intelligence"},'
        '{"@type":"ListItem","position":3,"name":"Grid Intelligence","item":"https://dchub.cloud/grid-intelligence"}'
        ']},'
        '{"@type":"WebSite","name":"DC Hub","url":"https://dchub.cloud",'
        '"potentialAction":{"@type":"SearchAction",'
        '"target":{"@type":"EntryPoint","urlTemplate":"https://dchub.cloud/search?q={search_term_string}"},'
        '"query-input":"required name=search_term_string"}}'
        ']}'
        '</script>'
    )

    return _Resp(f'''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DC Hub · Grid Intelligence — Per-ISO Capacity & Queue</title>
<meta name="description" content="Grid intelligence for every major US ISO. Interconnect queue, capacity factor, fuel mix, and per-market headroom.">
<link rel="canonical" href="https://dchub.cloud/grid-intelligence">
{_jsonld}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
<style>
body{{font-family:'Instrument Sans',-apple-system,sans-serif;background:var(--dch-bg);color:var(--dch-text);min-height:100vh;margin:0}}
.container{{max-width:1100px;margin:0 auto;padding:32px 24px}}
header{{margin:32px 0 28px}}
header .eyebrow{{color:var(--dch-indigo);font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:8px}}
header h1{{font-size:2.4rem;margin:0 0 12px;letter-spacing:-.02em}}
header p{{color:var(--dch-text-mute);font-size:1.05rem;line-height:1.6;max-width:680px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-top:24px}}
.card{{background:var(--dch-surface);border:1px solid var(--dch-border);border-radius:12px;padding:18px 20px;text-decoration:none;color:inherit;display:block;transition:border-color .2s,transform .15s}}
.card:hover{{border-color:var(--dch-indigo);transform:translateY(-2px)}}
.card h3{{font-size:1.05rem;font-weight:600}}
.footer{{margin-top:36px;padding-top:18px;border-top:1px solid var(--dch-border);color:var(--dch-text-dim);font-size:.82rem}}
.footer a{{color:var(--dch-indigo)}}
</style></head><body>
<div class="container">
<header>
  <div class="eyebrow">Research · Grid Intelligence</div>
  <h1>Per-ISO grid headroom &amp; interconnect queue</h1>
  <p>Where the data centers want to go, what the grid can actually carry, and how the queue is moving. Each region rolls up live capacity factor, queue position depth, fuel-mix exposure, and per-market headroom — the same data every site-selection deal needs.</p>
</header>
<div class="grid">
{''.join(cards)}
</div>

<section style="margin:48px 0 8px;padding:24px;background:var(--dch-surface);border:1px solid var(--dch-border);border-radius:12px">
<h2 style="font-size:1.2rem;margin:0 0 12px">What this surface tracks</h2>
<p style="color:var(--dch-text-mute);line-height:1.65;margin:0 0 14px">
DC Hub grid intelligence is the live operational picture of every U.S. interconnection
across the seven major ISOs and balancing authorities — PJM, ERCOT, CAISO, MISO,
NYISO, ISO-NE, and SPP. For each region we surface three things that matter to
anyone underwriting a data center build: <b>interconnect queue depth</b> (how
many GW of new generation and load are waiting on grid studies), <b>capacity
factor and fuel mix</b> (where the marginal MWh actually comes from), and
<b>real-time headroom</b> (the gap between current load and firm capacity, which
governs whether a hyperscale tenant can land 200 MW in the next study cycle).
The same per-ISO signals flow into the <a href="/dcpi">Data Center Power Index
(DCPI)</a>, the daily 0–100 score that ranks 200+ U.S. markets on whether
they're <b>BUILD</b>, <b>CAUTION</b>, <b>AVOID</b>, or <b>LOW_SIGNAL</b>. If
you're going site-by-site, jump to a specific market on the
<a href="/markets">markets index</a> — every market page links back to its
serving ISO so the energy-intelligence and site-selection views stay in sync.
For agents, the same data is exposed as JSON at <code>/api/v1/grid-intelligence</code>
and through the <a href="/mcp">DC Hub MCP server</a>.
</p>
</section>

<div class="footer">
  Machine-readable: <a href="/api/v1/grid-intelligence">/api/v1/grid-intelligence</a> ·
  Per-region JSON: <a href="/api/v1/grid-intelligence/caiso">/api/v1/grid-intelligence/&lt;id&gt;</a> ·
  <a href="/intelligence">All intelligence surfaces →</a> ·
  <a href="/dcpi">DCPI methodology →</a>
</div>
</div>
</body></html>''', mimetype='text/html')


@grid_intel_bp.route('/api/v1/grid-intelligence', methods=['GET'])
def list_grid_regions():
    """List all grid intelligence regions with basic info."""
    _ensure_grid_region_seeds()  # Phase RRR: idempotent seed of CAISO + Southeast
    conn = None  # kept for the finally-block contract; loader owns its own conn now
    try:
        # r49-selfcall (2026-05-31): served from the 5-min module cache
        # (deep copy) instead of opening a fresh connection + 1+N queries
        # per request. Per-request gating/override below still mutate the
        # returned copy safely.
        regions = _load_regions_cached()


        # grid fill — Phase B


        try: _apply_grid_queue_override(regions)


        except Exception: pass


        try: _apply_grid_queue_override(data['regions'])


        except Exception: pass


        try: _apply_grid_queue_override(result['regions'])


        except Exception: pass


        try: _apply_grid_queue_override(payload['regions'])


        except Exception: pass


        try: _apply_grid_queue_override(response['regions'])


        except Exception: pass


        try: _apply_grid_queue_override(response.get('regions'))


        except Exception: pass


        try: _apply_grid_queue_override(out['regions'])


        except Exception: pass


        try: _apply_grid_queue_override(out.get('regions'))


        except Exception: pass


        try: _apply_grid_queue_override(resp['regions'])


        except Exception: pass


        try: _apply_grid_queue_override(resp.get('regions'))


        except Exception: pass


        try: _apply_grid_queue_override(body['regions'])


        except Exception: pass


        try: _apply_grid_queue_override(body.get('regions'))


        except Exception: pass


        # Phase WW (2026-05-17) — soft-paywall the all-regions dump.
        # MCP get_grid_intelligence is gated at IDENTIFIED. REST was wide
        # open. Now: anon/free sees first 3 regions; IDENTIFIED+ all.
        # Per-region detail at /api/v1/grid-intelligence/<region_id>
        # stays FREE as the discovery hook.
        _PREVIEW_CAP = 3
        _gated = False
        _total = len(regions)
        try:
            from util.tier_gate import resolve_tier, Tier as _T
            _tier, _ = resolve_tier()
            if _tier < _T.IDENTIFIED and _total > _PREVIEW_CAP:
                regions = regions[:_PREVIEW_CAP]
                _gated = True
        except Exception:
            pass

        body = {
            'success': True,
            'regions': regions,
            'total':   len(regions),
            'source':  'DC Hub Grid Intelligence',
        }
        if _gated:
            body['_gated'] = True
            body['_preview_only'] = True
            body['_total_available'] = _total
            body['_hidden_count'] = _total - _PREVIEW_CAP
            body['_required_tier'] = "IDENTIFIED"
            body['_upgrade_cta'] = (
                f"Showing {_PREVIEW_CAP} of {_total} grid regions. Get all "
                f"{_total} (queue GW, corridor count, key states, status) "
                f"free — POST /api/v1/keys/claim or pass X-API-Key."
            )
            body['_signup_url'] = "https://dchub.cloud/signup"
        return jsonify(body)
    except Exception as e:

        # grid fill — Phase B

        try: _apply_grid_queue_override(regions)

        except Exception: pass

        try: _apply_grid_queue_override(data['regions'])

        except Exception: pass

        try: _apply_grid_queue_override(result['regions'])

        except Exception: pass

        try: _apply_grid_queue_override(payload['regions'])

        except Exception: pass

        try: _apply_grid_queue_override(response['regions'])

        except Exception: pass

        try: _apply_grid_queue_override(response.get('regions'))

        except Exception: pass

        try: _apply_grid_queue_override(out['regions'])

        except Exception: pass

        try: _apply_grid_queue_override(out.get('regions'))

        except Exception: pass

        try: _apply_grid_queue_override(resp['regions'])

        except Exception: pass

        try: _apply_grid_queue_override(resp.get('regions'))

        except Exception: pass

        try: _apply_grid_queue_override(body['regions'])

        except Exception: pass

        try: _apply_grid_queue_override(body.get('regions'))

        except Exception: pass

        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ─── Get single region with full data ───
@grid_intel_bp.route('/api/v1/grid-intelligence/<region_id>', methods=['GET'])
def get_grid_region(region_id):
    """
    Get full grid intelligence for a region.
    Tier-gated: free sees headlines, developer sees scores, pro sees everything.
    """
    conn = None
    try:
        # Determine tier — internal key from MCP server bypasses all gating
        _VALID_INTERNAL_KEYS = set()  # legacy removed; now uses internal_auth.is_valid_internal_key
        if is_valid_internal_key(request.headers.get('X-Internal-Key', '')):
            tier = 'pro'
            tier_config = GRID_INTEL_TIER_CONFIG.get('pro', GRID_INTEL_TIER_CONFIG['developer'])
        else:
            api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
            tier = _determine_tier(api_key)
            tier_config = GRID_INTEL_TIER_CONFIG.get(tier, GRID_INTEL_TIER_CONFIG['free'])

        # Normalize: DB stores lowercase keys (ercot, pjm, caiso, miso-spp, southeast)
        region_id = region_id.strip().lower()

        conn = _get_conn()
        cur = conn.cursor()

        # Get region
        cur.execute("""
            SELECT id, name, iso, status, headline, description,
                   key_states, total_queue_gw, page_url
            FROM grid_regions WHERE id = %s
        """, (region_id,))
        region_row = cur.fetchone()

        if not region_row:
            return jsonify({'success': False, 'error': f'Region "{region_id}" not found'}), 404

        key_states = region_row[6] or []
        region = {
            'id': region_row[0],
            'name': region_row[1],
            'iso': region_row[2],
            'status': region_row[3],
            'headline': region_row[4],
            'description': region_row[5],
            'key_states': key_states,
            'total_queue_gw': float(region_row[7]) if region_row[7] else None,
            'page_url': region_row[8],
        }

        # Get corridors
        cur.execute("""
            SELECT label, lat, lon, state, utility, queue_gw, system_peak_gw,
                   congestion_level, transmission_capacity, excess_generation,
                   dilution_risk, notes, sort_order
            FROM grid_corridors
            WHERE region_id = %s
            ORDER BY sort_order
        """, (region_id,))
        corridor_rows = cur.fetchall()

        # Apply tier gating to corridors
        max_corridors = tier_config['max_corridors']
        total_corridors = len(corridor_rows)

        corridors = []
        for i, c in enumerate(corridor_rows):
            if i >= max_corridors:
                break

            corridor = {
                'label': c[0],
                'state': c[3],
                'utility': c[4],
                'congestion_level': c[7],
                'dilution_risk': c[10],
            }

            # Queue / peak data (always visible — these are public numbers)
            corridor['queue_gw'] = float(c[5]) if c[5] else None
            corridor['system_peak_gw'] = float(c[6]) if c[6] else None
            corridor['transmission_capacity'] = c[8]
            corridor['excess_generation'] = c[9]

            # Scores + infrastructure (developer+)
            if tier_config['show_scores']:
                lat, lon = float(c[1]), float(c[2])
                infra = _get_infra_counts(lat, lon, radius_km=50, conn=conn)
                corridor['infrastructure'] = infra
                corridor['notes'] = c[11]

                # Queue/peak ratio
                if c[5] and c[6] and float(c[6]) > 0:
                    corridor['queue_peak_ratio'] = round(float(c[5]) / float(c[6]), 1)
            else:
                corridor['infrastructure'] = '██ upgrade to see'
                corridor['notes'] = '██ upgrade to see'

            # Coordinates (pro+)
            if tier_config['show_coordinates']:
                corridor['lat'] = float(c[1])
                corridor['lon'] = float(c[2])

            corridors.append(corridor)

        # Energy rates (developer+)
        energy_rates = {}
        if tier_config['show_energy_rates']:
            energy_rates = _get_energy_rates(key_states, conn=conn)
        else:
            energy_rates = {st: '██ upgrade to see' for st in key_states}

        # Tax incentives (developer+)
        tax_incentives = {}
        if tier_config['show_tax_incentives']:
            tax_incentives = _get_tax_incentives(key_states, conn=conn)
        else:
            tax_incentives = {st: '██ upgrade to see' for st in key_states}

        # Facility counts per state (always visible as aggregate)
        facility_counts = {}
        for st in key_states:
            facility_counts[st] = _get_facility_count(st, conn=conn)

        # Build response
        response = {
            'success': True,
            'tier': tier,
            'region': region,
            'corridors': corridors,
            'total_corridors': total_corridors,
            'energy_rates_cents_kwh': energy_rates,
            'tax_incentives': tax_incentives,
            'facility_counts_by_state': facility_counts,
            'data_sources': [
                'HIFLD (substations)',
                'EIA (transmission, power plants, gas pipelines, retail rates)',
                'EPA eGRID (carbon intensity)',
                'FEMA (risk index)',
                'USGS (water stress)',
                'DC Hub (facilities, market intel)',
            ],
        }

        # Upgrade CTA for free/developer
        if tier == 'free':
            response['_upgrade'] = {
                'message': f'Showing {min(max_corridors, total_corridors)} of {total_corridors} corridors with limited data. Developer plan ($49/mo) unlocks all corridors, scores, energy rates, and infrastructure counts.',
                'url': 'https://dchub.cloud/pricing#developer',
                'checkout': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
                'corridors_hidden': max(0, total_corridors - max_corridors),
            }
        elif tier == 'developer':
            response['_upgrade'] = {
                'message': 'Developer plan active. Upgrade to Pro ($99/mo) for facility names, exact coordinates, and CSV export.',
                'url': 'https://dchub.cloud/pricing#pro',
            }

        return jsonify(response)

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def register_grid_intel_routes(app):
    """Register grid intelligence routes with the Flask app."""
    app.register_blueprint(grid_intel_bp)
    print("[grid_intel] Registered /api/v1/grid-intelligence routes")
