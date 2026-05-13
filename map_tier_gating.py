"""
map_tier_gating.py — Tiered access control for Map + Land & Power endpoints
=============================================================================

Tier breakdown:
  Anonymous (not signed in)  → Blank map / 401 on Land & Power
  Free     (signed up, $0)   → Map: 50 facilities, name+city+country only, city-level coords
                                L&P: Grid demand names only, 3 heatmap dots
  Developer ($49/mo)         → Map: 1000 facilities, +provider +status +power_mw, full coords
                                L&P: Grid demand + energy prices + full heatmap (no EPA/utility)
  Pro/Enterprise ($199+/mo)  → Everything, no limits

Deploy: upload to Railway repo root, then in main.py add:
    from map_tier_gating import register_map_tier_gating
    register_map_tier_gating(app)
  This REPLACES the existing /api/v1/map and /api/v1/land-power/data routes.

Author: DC Hub / Claude session Mar 18 2026
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify
from internal_auth import is_valid_internal_key, get_internal_key_for_client

logger = logging.getLogger(__name__)

# ─── Tier detection ──────────────────────────────────────────────────────────

def _detect_caller_tier(decode_jwt_func=None):
    """
    Determine the caller's plan tier from the request.
    Returns: (tier, user_info_or_None)
    
    Checks in order:
      1. X-Internal-Key header → 'pro' (internal MCP calls)
      2. AI Wars verification keys → 'pro'
      3. X-API-Key / Bearer token → look up in DB
      4. JWT cookie/header → decode and read plan
      5. dchub.cloud Origin/Referer → check for auth cookie
      6. Default → 'anonymous'
    """
    from flask import request

    # 1. Internal key bypass
    internal_key = request.headers.get('X-Internal-Key', '')
    if is_valid_internal_key(internal_key):
        return 'pro', {'source': 'internal'}

    # 2. AI Wars keys
    try:
        from main import get_ai_wars_key_info
        ai_info = get_ai_wars_key_info()
        if ai_info:
            return ai_info.get('tier', 'pro'), ai_info
    except Exception:
        pass

    # 3. API key (X-API-Key header or query param)
    api_key = request.headers.get('X-API-Key', '') or request.args.get('api_key', '')
    if not api_key:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer ') and auth_header[7:].startswith('dchub_'):
            api_key = auth_header[7:]

    if api_key and api_key.startswith('dchub_'):
        try:
            from main import get_pg_connection, return_pg_connection
            conn = get_pg_connection(retries=1)
            try:
                cur = conn.cursor()
                key_hash = hashlib.sha256(api_key.encode()).hexdigest()
                cur.execute("""
                    SELECT u.plan, u.id, u.email 
                    FROM api_keys ak JOIN users u ON ak.user_id = u.id 
                    WHERE ak.key_hash = %s AND ak.is_active = 1 
                    LIMIT 1
                """, (key_hash,))
                row = cur.fetchone()
                cur.close()
                if row:
                    plan = (row[0] or 'free').lower()
                    return plan, {'user_id': row[1], 'email': row[2], 'plan': plan}
            finally:
                return_pg_connection(conn)
        except Exception as e:
            logger.warning(f"map_tier_gating: API key lookup failed: {e}")

    # 4. JWT token (cookie or Authorization: Bearer)
    token = None
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer ') and not auth_header[7:].startswith('dchub_'):
        token = auth_header[7:].strip()
    if not token:
        # Phase QQ+13 (2026-05-13): also check `dchub_token` cookie.
        # routes/auth_routes.py sets this cookie on login (lines 277,
        # 597) and every OTHER backend tier-check reads it
        # (api_tier_gating, paywall_middleware, main.py). This function
        # was the lone holdout, so every logged-in web-UI user got
        # treated as anonymous → markets/list returned 5 free markets,
        # energy returned the gated paywall card, etc. User reported
        # "Atlanta gated with Developer option, I am an enterprise
        # user" — this is the fix.
        token = (request.cookies.get('auth_token') or
                 request.cookies.get('token') or
                 request.cookies.get('dchub_token'))

    if token and decode_jwt_func:
        try:
            payload = decode_jwt_func(token)
            if payload:
                plan = (payload.get('plan') or 'free').lower()
                return plan, payload
        except Exception:
            pass

    # 5. No auth at all
    return 'anonymous', None


def _normalize_tier(plan):
    """Normalize plan name to tier level."""
    plan = (plan or 'anonymous').lower().strip()
    if plan in ('pro', 'enterprise', 'founding'):
        return 'pro'
    if plan in ('developer',):
        return 'developer'
    if plan in ('free',):
        return 'free'
    return 'anonymous'


# ─── Upgrade CTA builder ────────────────────────────────────────────────────

def _upgrade_cta(tier, feature_name='full map data'):
    """Build tier-appropriate upgrade call to action."""
    if tier == 'anonymous':
        return {
            'action': 'sign_up',
            'message': f'Sign up free at dchub.cloud to access {feature_name}.',
            'url': 'https://dchub.cloud/signup',
        }
    elif tier == 'free':
        return {
            'action': 'upgrade',
            'message': f'Upgrade to Developer ($49/mo) for more {feature_name}. Pro ($199/mo) unlocks everything.',
            'url': 'https://dchub.cloud/pricing#developer',
            'checkout': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
            'price': '$49/mo',
        }
    elif tier == 'developer':
        return {
            'action': 'upgrade',
            'message': f'Upgrade to Pro ($199/mo) for full {feature_name} with no limits.',
            'url': 'https://dchub.cloud/pricing',
        }
    return None  # Pro/Enterprise — no CTA


# ═══════════════════════════════════════════════════════════════════════════════
#  MAP ENDPOINT — /api/v1/map
# ═══════════════════════════════════════════════════════════════════════════════

# Fields visible per tier
MAP_FIELDS = {
    'anonymous': [],  # No data
    'free':      ['name', 'city', 'state', 'country', 'status'],
    'developer': ['name', 'provider', 'city', 'state', 'country', 'status',
                  'power_mw', 'latitude', 'longitude'],
    'pro':       None,  # All fields
}

# Max facilities returned per tier
MAP_LIMITS = {
    'anonymous': 0,
    'free':      50,
    'developer': 1000,
    'pro':       10000,
}


def _gated_map_handler(decode_jwt_func):
    """Tiered map endpoint handler."""
    plan, user_info = _detect_caller_tier(decode_jwt_func)
    tier = _normalize_tier(plan)

    # ── Anonymous: blank map ──
    if tier == 'anonymous':
        return jsonify({
            'success': True,
            'data': [],
            'total': 0,
            'tier': 'anonymous',
            'message': 'Sign up free at dchub.cloud to view data center locations on the map.',
            '_upgrade': _upgrade_cta('anonymous', 'the facility map'),
        }), 200

    # ── Determine limits and fields ──
    max_results = MAP_LIMITS.get(tier, 50)
    allowed_fields = MAP_FIELDS.get(tier)
    user_limit = request.args.get('limit', max_results, type=int)
    limit = min(user_limit, max_results)
    offset = request.args.get('offset', 0, type=int)

    try:
        from main import get_read_db
        conn = get_read_db()
        c = conn.cursor()

        # Always query all fields, strip later
        c.execute("""
            SELECT id, name, provider, city, state, country, market AS region,
                   latitude, longitude, power_mw, status
            FROM discovered_facilities
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ORDER BY power_mw DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, (limit, offset))

        rows = c.fetchall()
        cols = [desc[0] for desc in c.description]

        c.execute("SELECT COUNT(*) FROM discovered_facilities WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
        total = c.fetchone()[0]
        conn.close()

        facilities = []
        for row in rows:
            full = dict(zip(cols, row))

            if tier == 'free':
                # Free: basic fields + city-level coordinates (1 decimal = ~11km)
                fac = {k: full.get(k) for k in MAP_FIELDS['free'] if k in full}
                # Round coords to city-level precision so dots appear in right area
                # but exact address is hidden
                if full.get('latitude') and full.get('longitude'):
                    fac['latitude'] = round(float(full['latitude']), 1)
                    fac['longitude'] = round(float(full['longitude']), 1)
                facilities.append(fac)

            elif tier == 'developer':
                # Developer: more fields + full precision coords
                fac = {k: full.get(k) for k in MAP_FIELDS['developer'] if k in full}
                facilities.append(fac)

            else:
                # Pro/Enterprise: everything
                facilities.append(full)

        response = {
            'success': True,
            'data': facilities,
            'total': total,
            'showing': len(facilities),
            'limit': limit,
            'offset': offset,
            'tier': tier,
        }

        cta = _upgrade_cta(tier, 'map data')
        if cta:
            response['_upgrade'] = cta
            if tier == 'free':
                response['_note'] = (
                    f'Free tier: showing {len(facilities)} of {total} facilities '
                    f'with basic fields and approximate locations. '
                    f'Developer plan ($49/mo) unlocks 1,000 facilities with exact coordinates and power data. '
                    f'Pro plan unlocks all {total} facilities with full specs.'
                )
            elif tier == 'developer':
                response['_note'] = (
                    f'Developer tier: showing {len(facilities)} of {total} facilities. '
                    f'Pro plan ($199/mo) unlocks all {total} facilities with full infrastructure data.'
                )

        return jsonify(response)

    except Exception as e:
        logger.error(f"map_tier_gating /api/v1/map error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
#  LAND & POWER ENDPOINT — /api/v1/land-power/data
# ═══════════════════════════════════════════════════════════════════════════════

# Heatmap markets visible per tier
HEATMAP_FULL = [
    {"name": "Northern Virginia", "lat": 39.0438, "lng": -77.4874, "capacity_mw": 4500, "utilization": 78, "growth": 12},
    {"name": "Dallas-Fort Worth", "lat": 32.7767, "lng": -96.7970, "capacity_mw": 2800, "utilization": 65, "growth": 18},
    {"name": "Phoenix", "lat": 33.4484, "lng": -112.0740, "capacity_mw": 1200, "utilization": 45, "growth": 35},
    {"name": "Chicago", "lat": 41.8781, "lng": -87.6298, "capacity_mw": 1800, "utilization": 72, "growth": 8},
    {"name": "Silicon Valley", "lat": 37.3861, "lng": -121.8906, "capacity_mw": 2200, "utilization": 82, "growth": 6},
    {"name": "Atlanta", "lat": 33.7490, "lng": -84.3880, "capacity_mw": 900, "utilization": 58, "growth": 22},
    {"name": "Portland/Hillsboro", "lat": 45.5231, "lng": -122.6765, "capacity_mw": 600, "utilization": 70, "growth": 15},
    {"name": "Salt Lake City", "lat": 40.7608, "lng": -111.8910, "capacity_mw": 400, "utilization": 42, "growth": 28},
]


def _strip_heatmap(markets, tier):
    """Strip heatmap data based on tier."""
    if tier == 'free':
        # Free: 3 markets, name + lat/lng only (no capacity/utilization/growth)
        return [
            {'name': m['name'], 'lat': m['lat'], 'lng': m['lng']}
            for m in markets[:3]
        ]
    elif tier == 'developer':
        # Developer: all markets, name + lat/lng + capacity (no utilization/growth)
        return [
            {'name': m['name'], 'lat': m['lat'], 'lng': m['lng'], 'capacity_mw': m['capacity_mw']}
            for m in markets
        ]
    else:
        # Pro: everything
        return markets


def _gated_land_power_handler(decode_jwt_func):
    """Tiered Land & Power data endpoint handler."""
    import concurrent.futures

    plan, user_info = _detect_caller_tier(decode_jwt_func)
    tier = _normalize_tier(plan)

    # ── Anonymous: nothing ──
    if tier == 'anonymous':
        return jsonify({
            'success': False,
            'error': 'authentication_required',
            'message': 'Sign up free at dchub.cloud to access Land & Power intelligence.',
            'tier': 'anonymous',
            '_upgrade': _upgrade_cta('anonymous', 'Land & Power data'),
        }), 401

    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    state = request.args.get('state', '')

    result = {
        'success': True,
        'tier': tier,
        'grid_demand': {},
        'energy_prices': {},
        'capacity_heatmap': [],
        'epa_summary': {},
        'utility_territories': [],
    }

    iso_names = {
        'CAISO': 'California ISO', 'ERCOT': 'Electric Reliability Council of Texas',
        'PJM': 'PJM Interconnection', 'NYISO': 'New York ISO',
        'MISO': 'Midcontinent ISO', 'SPP': 'Southwest Power Pool',
        'ISONE': 'ISO New England'
    }
    isos = list(iso_names.keys())

    # ── FREE TIER: grid names only + 3 heatmap dots ──
    if tier == 'free':
        for iso in isos:
            result['grid_demand'][iso] = {
                'iso': iso,
                'iso_name': iso_names[iso],
                'demand_gw': '██ upgrade to see',
                'status': 'gated',
            }
        result['capacity_heatmap'] = _strip_heatmap(HEATMAP_FULL, 'free')
        result['energy_prices'] = {'note': 'Upgrade to Developer ($49/mo) to see energy pricing by state.'}
        result['epa_summary'] = {'note': 'Upgrade to Developer ($49/mo) for EPA facility data.'}
        result['_note'] = (
            'Free tier: showing grid operator names and 3 market locations. '
            'Developer plan ($49/mo) unlocks energy pricing, full heatmap, and grid demand data. '
            'Pro plan ($199/mo) unlocks EPA data, utility territories, and all infrastructure layers.'
        )
        result['_upgrade'] = _upgrade_cta('free', 'Land & Power data')
        return jsonify(result)

    # ── DEVELOPER TIER: grid demand + energy prices + full heatmap ──
    if tier == 'developer':
        # Grid demand — real data
        try:
            from main import gridstatus_get_load
        except ImportError:
            gridstatus_get_load = lambda iso: None

        for iso in isos:
            try:
                data = gridstatus_get_load(iso)
                if data:
                    result['grid_demand'][iso] = {
                        'iso': iso,
                        'iso_name': iso_names[iso],
                        'demand_mw': data['load_mw'],
                        'demand_gw': round(data['load_mw'] / 1000, 2),
                        'timestamp': data['timestamp'],
                        'status': 'live',
                    }
                else:
                    result['grid_demand'][iso] = {
                        'iso': iso, 'iso_name': iso_names[iso],
                        'demand_gw': None, 'status': 'unavailable',
                    }
            except Exception:
                result['grid_demand'][iso] = {
                    'iso': iso, 'iso_name': iso_names[iso],
                    'demand_gw': None, 'status': 'error',
                }

        # Energy prices — real data
        dc_states = ["VA", "TX", "AZ", "CA", "GA", "OH", "IL", "NC", "NV", "OR", "WA", "NJ"]
        try:
            from capacity_headroom_api import fetch_eia_retail_rate
            for st in dc_states:
                try:
                    price = fetch_eia_retail_rate(st)
                    result['energy_prices'][st] = {'state': st, 'price_cents_kwh': price}
                except Exception:
                    result['energy_prices'][st] = {'state': st, 'price_cents_kwh': None}
        except ImportError:
            for st in dc_states:
                result['energy_prices'][st] = {'state': st, 'price_cents_kwh': None}

        # Heatmap — full markets, basic fields
        result['capacity_heatmap'] = _strip_heatmap(HEATMAP_FULL, 'developer')

        # EPA + utility: gated for Developer
        result['epa_summary'] = {'note': 'Upgrade to Pro ($199/mo) for EPA facility data and environmental analysis.'}
        result['utility_territories'] = []

        result['_note'] = (
            'Developer tier: grid demand, energy pricing, and market heatmap included. '
            'Pro plan ($199/mo) adds EPA environmental data, utility territories, and proximity analysis.'
        )
        result['_upgrade'] = _upgrade_cta('developer', 'Land & Power data')
        return jsonify(result)

    # ── PRO / ENTERPRISE: everything ──
    import requests as http_req

    try:
        from main import gridstatus_get_load
    except ImportError:
        gridstatus_get_load = lambda iso: None

    def fetch_iso_demand(iso):
        try:
            data = gridstatus_get_load(iso)
            if data:
                return {
                    'iso': iso, 'iso_name': iso_names.get(iso, iso),
                    'demand_mw': data['load_mw'],
                    'demand_gw': round(data['load_mw'] / 1000, 2),
                    'timestamp': data['timestamp'], 'status': 'live',
                }
            return {'iso': iso, 'iso_name': iso_names.get(iso, iso), 'demand_gw': None, 'status': 'unavailable'}
        except Exception:
            return {'iso': iso, 'iso_name': iso_names.get(iso, iso), 'demand_gw': None, 'status': 'error'}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_iso_demand, iso): iso for iso in isos}
        for future in concurrent.futures.as_completed(futures):
            iso = futures[future]
            try:
                result['grid_demand'][iso] = future.result()
            except Exception:
                result['grid_demand'][iso] = {'iso': iso, 'demand_gw': None, 'status': 'error'}

    # Energy prices
    dc_states = ["VA", "TX", "AZ", "CA", "GA", "OH", "IL", "NC", "NV", "OR", "WA", "NJ"]
    try:
        from capacity_headroom_api import fetch_eia_retail_rate
        for st in dc_states:
            try:
                price = fetch_eia_retail_rate(st)
                result['energy_prices'][st] = {'state': st, 'price_cents_kwh': price}
            except Exception:
                result['energy_prices'][st] = {'state': st, 'price_cents_kwh': None}
    except ImportError:
        for st in dc_states:
            result['energy_prices'][st] = {'state': st, 'price_cents_kwh': None}

    # Full heatmap
    result['capacity_heatmap'] = HEATMAP_FULL

    # EPA data (Pro only)
    if lat and lng:
        try:
            geo_url = f"https://geo.fcc.gov/api/census/block/find%slatitude={lat}&longitude={lng}&format=json"
            epa_state = ''
            try:
                geo_resp = http_req.get(geo_url, timeout=10)
                if geo_resp.status_code == 200:
                    epa_state = geo_resp.json().get('State', {}).get('code', '')
            except Exception:
                pass

            if epa_state:
                epa_url = f"https://data.epa.gov/efservice/ICIS_AIR/STATE_CODE/{epa_state}/JSON/0:10"
                try:
                    epa_resp = http_req.get(epa_url, timeout=15)
                    if epa_resp.status_code == 200:
                        epa_data = epa_resp.json()
                        result['epa_summary'] = {
                            'lat': lat, 'lng': lng, 'state': epa_state,
                            'count': len(epa_data), 'facilities': epa_data[:5],
                        }
                except Exception:
                    pass

            if not result['epa_summary']:
                result['epa_summary'] = {'lat': lat, 'lng': lng, 'count': 0, 'facilities': []}
        except Exception:
            result['epa_summary'] = {'lat': lat, 'lng': lng, 'count': 0, 'error': 'unavailable'}

    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════════
#  CAPACITY HEATMAP — /api/v1/capacity/heatmap/public
# ═══════════════════════════════════════════════════════════════════════════════

def _gated_heatmap_handler(decode_jwt_func):
    """Tiered heatmap endpoint."""
    plan, user_info = _detect_caller_tier(decode_jwt_func)
    tier = _normalize_tier(plan)

    if tier == 'anonymous':
        return jsonify({
            'success': True,
            'data': [],
            'tier': 'anonymous',
            'message': 'Sign up free at dchub.cloud to see capacity heatmap data.',
            '_upgrade': _upgrade_cta('anonymous', 'capacity heatmap'),
        })

    data = _strip_heatmap(HEATMAP_FULL, tier)
    response = {'success': True, 'data': data, 'tier': tier}

    cta = _upgrade_cta(tier, 'capacity heatmap')
    if cta:
        response['_upgrade'] = cta

    return jsonify(response)


# ═══════════════════════════════════════════════════════════════════════════════
#  REGISTRATION — Call from main.py to replace existing routes
# ═══════════════════════════════════════════════════════════════════════════════

def register_map_tier_gating(app, decode_jwt_func=None):
    """
    Register tiered map and Land & Power endpoints.
    
    OVERRIDES these existing routes in main.py by replacing their view_functions:
      - /api/v1/map               (endpoint: api_v1_map)
      - /api/v1/land-power/data   (endpoint: land_power_consolidated)
      - /api/v1/capacity/heatmap/public (endpoint: capacity_heatmap_public)
    
    Usage in main.py (add AFTER decode_jwt is defined, near the bottom):
        from map_tier_gating import register_map_tier_gating
        register_map_tier_gating(app, decode_jwt_func=decode_jwt)
    """
    if decode_jwt_func is None:
        try:
            from main import decode_jwt
            decode_jwt_func = decode_jwt
        except ImportError:
            decode_jwt_func = lambda t: None

    _jwt = decode_jwt_func

    # ── Override existing view functions (no duplicate route registration) ──
    
    def map_gated():
        return _gated_map_handler(_jwt)
    
    def land_power_gated():
        return _gated_land_power_handler(_jwt)
    
    def heatmap_gated():
        return _gated_heatmap_handler(_jwt)

    # Replace the view functions on existing endpoints
    # Original endpoint names from main.py:
    #   api_v1_map, land_power_consolidated, capacity_heatmap_public
    replaced = 0
    
    if 'api_v1_map' in app.view_functions:
        app.view_functions['api_v1_map'] = map_gated
        replaced += 1
    else:
        # Route doesn't exist yet — register it fresh
        app.add_url_rule('/api/v1/map', 'api_v1_map', map_gated, methods=['GET'])
        replaced += 1
    
    if 'land_power_consolidated' in app.view_functions:
        app.view_functions['land_power_consolidated'] = land_power_gated
        replaced += 1
    else:
        app.add_url_rule('/api/v1/land-power/data', 'land_power_consolidated', land_power_gated, methods=['GET'])
        replaced += 1
    
    if 'capacity_heatmap_public' in app.view_functions:
        app.view_functions['capacity_heatmap_public'] = heatmap_gated
        replaced += 1
    else:
        app.add_url_rule('/api/v1/capacity/heatmap/public', 'capacity_heatmap_public', heatmap_gated, methods=['GET'])
        replaced += 1

    logger.info(f"🗺️ Map Tier Gating: ✅ {replaced} endpoints overridden (anon=blank, free=taste, dev=more, pro=all)")
    logger.info("   /api/v1/map              → anon:0, free:50, dev:1000, pro:10000")
    logger.info("   /api/v1/land-power/data   → anon:401, free:names-only, dev:demand+prices, pro:all")
    logger.info("   /api/v1/capacity/heatmap  → anon:empty, free:3dots, dev:basic, pro:full")
