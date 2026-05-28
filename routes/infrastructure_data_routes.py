"""
DC Hub — Infrastructure Data Routes v2
═══════════════════════════════════════
New API endpoints for power_plants_eia, transmission_lines_eia, submarine_cables.
Plus: gas-pipelines added to rate limit bypass list.

Register in main.py:
    from routes.infrastructure_data_routes import register_infra_data_routes
    register_infra_data_routes(app, get_pg_connection)

Tables required (already created by bulk loaders):
    - power_plants_eia (13,441 rows)
    - transmission_lines_eia (94K+ rows) 
    - submarine_cables (690 rows)
    - submarine_cable_landings (landing points)
"""
import math
import time
import threading
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
infra_data_bp = Blueprint('infra_data', __name__)

_get_db = None

def register_infra_data_routes(app, get_db_func):
    global _get_db
    _get_db = get_db_func
    app.register_blueprint(infra_data_bp)
    logger.info("✅ Infrastructure Data Routes v2 registered")
    logger.info("   📍 /api/v1/power-plants (13K+ EIA plants)")
    logger.info("   ⚡ /api/v1/transmission-lines (94K+ HIFLD lines)")
    logger.info("   🌊 /api/v1/submarine-cables (690 cables + landings)")
    logger.info("   📡 /api/v1/cable-landing-points (cable_landing_points table)")


def _safe_float(val):
    try:
        return float(val) if val is not None else None
    except:
        return None


# 2026-05-28 — tier gating for the map layer endpoints. power_plants_eia and
# transmission_lines_eia are public EIA/HIFLD datasets, so the gate is lighter
# than the proprietary facility map: free/anon get a capped preview with
# city-level coords; paid tiers get full rows + exact coords. Cookie/key-aware
# detection mirrors the energy paywall; fails closed to anonymous.
_LAYER_PAID = {'pro', 'enterprise', 'founding', 'internal', 'admin'}
_LAYER_CAP = {'anonymous': 50, 'free': 50, 'identified': 100, 'developer': 500}


def _layer_tier():
    def _dec(_t):
        try:
            import jwt as _j, os as _o
            secret = _o.environ.get('JWT_SECRET') or _o.environ.get('SECRET_KEY', '')
            return _j.decode(_t, secret, algorithms=['HS256'])
        except Exception:
            return None
    try:
        from map_tier_gating import _detect_caller_tier
        t, _ = _detect_caller_tier(decode_jwt_func=_dec)
        return (t or 'anonymous').lower()
    except Exception:
        return 'anonymous'


def _layer_cap(tier):
    return 500 if tier in _LAYER_PAID else _LAYER_CAP.get(tier, 50)


# r47.33 (2026-05-26): process-local memo for the heavy land-power-map
# endpoints. Geographic data is the same for any caller hitting the same
# query-param set — power_plants_eia has 13K rows, transmission_lines_eia
# has 94K. Doing the bounding-box scan + ORDER BY on every authed map
# load was the unhidden source of the "really slow" report. Cache by
# normalized query-params; TTL 600s (matches what we'd advertise as
# acceptable lag for static-ish geographic data).
#
# Keyed by (endpoint, normalized-params-tuple). Lock-guarded.
_INFRA_MEMO: dict = {}
_INFRA_LOCK = threading.Lock()
_INFRA_TTL_SECONDS = 600


def _memo_get(key):
    entry = _INFRA_MEMO.get(key)
    if not entry:
        return None
    if (time.time() - entry['t']) > _INFRA_TTL_SECONDS:
        return None
    return entry['v']


def _memo_set(key, value):
    with _INFRA_LOCK:
        _INFRA_MEMO[key] = {'v': value, 't': time.time()}
        # bound memory: at most 200 cached query shapes
        if len(_INFRA_MEMO) > 200:
            oldest = sorted(_INFRA_MEMO.items(), key=lambda kv: kv[1]['t'])[:50]
            for k, _ in oldest:
                _INFRA_MEMO.pop(k, None)


# ═══════════════════════════════════════════════════════════════
# POWER PLANTS API — 13,441 EIA plants with lat/lng
# ═══════════════════════════════════════════════════════════════

@infra_data_bp.route('/api/v1/power-plants', methods=['GET'])
def get_power_plants():
    """Get power plants with spatial filtering.
    
    Query params:
        lat, lng, radius (miles) — bounding box
        state — filter by state
        fuel — filter by primary fuel (solar, natural gas, wind, etc.)
        min_mw — minimum nameplate capacity
        limit — max results (default 200, cap 500)
    """
    # Bulletproof param parsing
    lat = request.args.get('lat', None)
    lng = request.args.get('lng', None)
    radius = request.args.get('radius', 50)
    state_filter = request.args.get('state', '').upper()
    fuel_filter = request.args.get('fuel', '').lower()
    min_mw = request.args.get('min_mw', None)
    limit = request.args.get('limit', 200, type=int)

    try:
        lat = float(lat) if lat is not None else None
    except:
        lat = None
    try:
        lng = float(lng) if lng is not None else None
    except:
        lng = None
    try:
        radius = int(float(radius)) if radius else 50
    except:
        radius = 50
    try:
        min_mw = float(min_mw) if min_mw is not None else None
    except:
        min_mw = None

    _tier = _layer_tier()
    _full = _tier in _LAYER_PAID
    limit = min(limit, _layer_cap(_tier))

    # r47.33: memo by normalized params. Lat/lng quantized to 0.25° so
    # nearby map pans hit the same cache slot. Tier is part of the key so a
    # paid caller's full result is never served to a free/anon caller.
    cache_key = ('power-plants', _tier,
                 round(lat, 2) if lat is not None else None,
                 round(lng, 2) if lng is not None else None,
                 radius, state_filter, fuel_filter, min_mw,
                 min(limit, 500))
    cached = _memo_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()

        query = """SELECT id, plant_id, name, utility_name, state, city, county,
                   primary_fuel, technology, nameplate_capacity_mw, max_output_mw,
                   natural_gas_mw, solar_mw, wind_mw, nuclear_mw, coal_mw,
                   lat, lng FROM power_plants_eia
                   WHERE lat IS NOT NULL AND lng IS NOT NULL"""
        params = []

        if lat is not None and lng is not None:
            lat_d = radius / 69.0
            lng_d = radius / (69.0 * max(math.cos(math.radians(lat)), 0.1))
            query += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            params.extend([lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d])

        if state_filter:
            query += " AND UPPER(state) = %s"
            params.append(state_filter)
        if fuel_filter:
            query += " AND LOWER(primary_fuel) = %s"
            params.append(fuel_filter)
        if min_mw is not None:
            query += " AND nameplate_capacity_mw >= %s"
            params.append(min_mw)

        query += " ORDER BY nameplate_capacity_mw DESC NULLS LAST LIMIT %s"
        params.append(min(limit, 500))

        cur.execute(query, params)
        rows = cur.fetchall()

        plants = []
        for r in rows:
            _lat, _lng = float(r[16]), float(r[17])
            if not _full:
                # city-level (~11km) coords; exact siting stays paywalled
                _lat, _lng = round(_lat, 1), round(_lng, 1)
            plants.append({
                'id': r[0], 'plant_id': r[1], 'name': r[2],
                'utility': r[3], 'state': r[4], 'city': r[5], 'county': r[6],
                'primary_fuel': r[7], 'technology': r[8],
                'capacity_mw': _safe_float(r[9]), 'max_output_mw': _safe_float(r[10]),
                'natural_gas_mw': _safe_float(r[11]), 'solar_mw': _safe_float(r[12]),
                'wind_mw': _safe_float(r[13]), 'nuclear_mw': _safe_float(r[14]),
                'coal_mw': _safe_float(r[15]),
                'lat': _lat, 'lng': _lng
            })

        payload = {
            'success': True,
            'plants': plants,
            'count': len(plants),
            'tier': _tier,
            'filters': {
                'state': state_filter or 'all',
                'fuel': fuel_filter or 'all',
                'min_mw': min_mw,
                'spatial': lat is not None and lng is not None
            },
            '_cache': 'miss',
        }
        if not _full:
            payload['_gated'] = True
            payload['_upgrade_cta'] = (
                "Free preview: capped results with approximate locations. "
                "Upgrade for full coverage + exact coordinates — dchub.cloud/pricing")
            payload['_pricing_url'] = "https://dchub.cloud/pricing"
        _memo_set(cache_key, {**payload, '_cache': 'hit'})
        return jsonify(payload)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except:
                try: conn.close()
                except: pass


# ═══════════════════════════════════════════════════════════════
# TRANSMISSION LINES API — 94K+ HIFLD lines with lat/lng
# ═══════════════════════════════════════════════════════════════

@infra_data_bp.route('/api/v1/transmission-lines', methods=['GET'])
def get_transmission_lines():
    """Get transmission lines with spatial filtering.
    
    Query params:
        lat, lng, radius (miles) — bounding box
        state — filter by state
        min_voltage — minimum voltage in kV
        owner — partial match on owner name
        limit — max results (default 200, cap 500)
    """
    lat = request.args.get('lat', None)
    lng = request.args.get('lng', None)
    radius = request.args.get('radius', 50)
    state_filter = request.args.get('state', '').upper()
    min_voltage = request.args.get('min_voltage', None)
    owner_filter = request.args.get('owner', '')
    limit = request.args.get('limit', 200, type=int)

    try:
        lat = float(lat) if lat is not None else None
    except:
        lat = None
    try:
        lng = float(lng) if lng is not None else None
    except:
        lng = None
    try:
        radius = int(float(radius)) if radius else 50
    except:
        radius = 50
    try:
        min_voltage = float(min_voltage) if min_voltage is not None else None
    except:
        min_voltage = None

    _tier = _layer_tier()
    _full = _tier in _LAYER_PAID
    limit = min(limit, _layer_cap(_tier))

    # r47.33: memo by normalized params — 94K-row table makes this the
    # single most expensive map endpoint. Tier in the key so a paid caller's
    # full result is never served to a free/anon caller.
    cache_key = ('transmission-lines', _tier,
                 round(lat, 2) if lat is not None else None,
                 round(lng, 2) if lng is not None else None,
                 radius, state_filter, min_voltage, owner_filter,
                 min(limit, 500))
    cached = _memo_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()

        query = """SELECT id, owner, voltage_kv, sub_1, sub_2, lat, lng, state
                   FROM transmission_lines_eia
                   WHERE lat IS NOT NULL AND lng IS NOT NULL"""
        params = []

        if lat is not None and lng is not None:
            lat_d = radius / 69.0
            lng_d = radius / (69.0 * max(math.cos(math.radians(lat)), 0.1))
            query += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            params.extend([lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d])

        if state_filter:
            query += " AND UPPER(state) = %s"
            params.append(state_filter)
        if min_voltage is not None:
            query += " AND voltage_kv >= %s"
            params.append(min_voltage)
        if owner_filter:
            query += " AND owner ILIKE %s"
            params.append(f"%{owner_filter}%")

        query += " ORDER BY voltage_kv DESC NULLS LAST LIMIT %s"
        params.append(min(limit, 500))

        cur.execute(query, params)
        rows = cur.fetchall()

        lines = []
        for r in rows:
            _lat, _lng = float(r[5]), float(r[6])
            if not _full:
                _lat, _lng = round(_lat, 1), round(_lng, 1)
            lines.append({
                'id': r[0], 'owner': r[1],
                'voltage_kv': _safe_float(r[2]),
                'substation_1': r[3], 'substation_2': r[4],
                'lat': _lat, 'lng': _lng,
                'state': r[7]
            })

        payload = {
            'success': True,
            'lines': lines,
            'count': len(lines),
            'tier': _tier,
            'filters': {
                'state': state_filter or 'all',
                'min_voltage': min_voltage,
                'owner': owner_filter or 'all',
                'spatial': lat is not None and lng is not None
            },
            '_cache': 'miss',
        }
        if not _full:
            payload['_gated'] = True
            payload['_upgrade_cta'] = (
                "Free preview: capped results with approximate locations. "
                "Upgrade for full coverage + exact coordinates — dchub.cloud/pricing")
            payload['_pricing_url'] = "https://dchub.cloud/pricing"
        _memo_set(cache_key, {**payload, '_cache': 'hit'})
        return jsonify(payload)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except:
                try: conn.close()
                except: pass


# ═══════════════════════════════════════════════════════════════
# SUBMARINE CABLES API — 690 cables worldwide + landing points
# ═══════════════════════════════════════════════════════════════

@infra_data_bp.route('/api/v1/submarine-cables', methods=['GET'])
def get_submarine_cables():
    """Get submarine cables and landing points.
    
    Query params:
        lat, lng, radius (miles) — filter landing points by location
        country — filter landing points by country
        limit — max results (default 100)
    """
    lat = request.args.get('lat', None)
    lng = request.args.get('lng', None)
    radius = request.args.get('radius', 200)
    country_filter = request.args.get('country', '').upper()
    limit = request.args.get('limit', 100, type=int)

    try:
        lat = float(lat) if lat is not None else None
    except:
        lat = None
    try:
        lng = float(lng) if lng is not None else None
    except:
        lng = None
    try:
        radius = int(float(radius)) if radius else 200
    except:
        radius = 200

    # r47.33: memo for submarine-cables (joins two tables)
    cache_key = ('submarine-cables',
                 round(lat, 2) if lat is not None else None,
                 round(lng, 2) if lng is not None else None,
                 radius, country_filter, min(limit, 1000))
    cached = _memo_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()

        # r47.33: align with live Neon schema. The original code expected
        # `name`/`rfs`/`color`/`url` columns that don't exist on the live
        # `submarine_cables` table — actual columns are `cable_name`,
        # `rfs_year`, plus `status` and `source` instead of `color`/`url`.
        cur.execute("""SELECT id, cable_id, cable_name, length_km, rfs_year,
                              owners, status, source
                         FROM submarine_cables LIMIT %s""",
                    [min(limit, 1000)])
        cable_rows = cur.fetchall()
        cables = []
        for r in cable_rows:
            cables.append({
                'id': r[0], 'cable_id': r[1], 'name': r[2],
                'length_km': _safe_float(r[3]), 'rfs_year': r[4],
                'owners': r[5], 'status': r[6], 'source': r[7],
            })

        # Get landing points (with optional spatial filter)
        lp_query = "SELECT id, name, country, lat, lng, cable_ids FROM submarine_cable_landings WHERE lat IS NOT NULL"
        lp_params = []

        if lat is not None and lng is not None:
            lat_d = radius / 69.0
            lng_d = radius / (69.0 * max(math.cos(math.radians(lat)), 0.1))
            lp_query += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            lp_params.extend([lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d])

        if country_filter:
            lp_query += " AND UPPER(country) = %s"
            lp_params.append(country_filter)

        lp_query += " LIMIT %s"
        lp_params.append(min(limit, 500))

        try:
            cur.execute(lp_query, lp_params)
            lp_rows = cur.fetchall()
            landings = []
            for r in lp_rows:
                landings.append({
                    'id': r[0], 'name': r[1], 'country': r[2],
                    'lat': float(r[3]), 'lng': float(r[4]),
                    'cable_ids': r[5]
                })
        except:
            landings = []

        payload = {
            'success': True,
            'cables': cables,
            'cable_count': len(cables),
            'landings': landings,
            'landing_count': len(landings),
            '_cache': 'miss',
        }
        _memo_set(cache_key, {**payload, '_cache': 'hit'})
        return jsonify(payload)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except:
                try: conn.close()
                except: pass


# ═══════════════════════════════════════════════════════════════
# CABLE LANDING POINTS API — frontend land-power-map dependency
# ═══════════════════════════════════════════════════════════════
# r47.33 (2026-05-26): /js/land-power-app.js fires a request to
# /api/v1/cable-landing-points?limit=2000 that previously 404'd because
# the table had no route. The `cable_landing_points` table (9 cols) is
# distinct from `submarine_cable_landings` (7 cols) — the former has
# per-cable city/country attribution, the latter aggregates landings
# with `cable_ids` text. Surface both via dedicated endpoints.

@infra_data_bp.route('/api/v1/cable-landing-points', methods=['GET'])
def get_cable_landing_points():
    """Cable landing points with optional spatial / country filtering.

    Backs the submarine-cable landings overlay on the land-power map.
    Query params:
        lat, lng, radius (miles) — spatial bounding box
        country — exact match (case-insensitive)
        cable_name — partial match (ILIKE)
        limit — max results (default 500, cap 2000)
    """
    lat = request.args.get('lat', None)
    lng = request.args.get('lng', None)
    radius = request.args.get('radius', 200)
    country_filter = request.args.get('country', '').upper()
    cable_name_filter = request.args.get('cable_name', '')
    limit = request.args.get('limit', 500, type=int)

    try:
        lat = float(lat) if lat is not None else None
    except:
        lat = None
    try:
        lng = float(lng) if lng is not None else None
    except:
        lng = None
    try:
        radius = int(float(radius)) if radius else 200
    except:
        radius = 200

    cache_key = ('cable-landing-points',
                 round(lat, 2) if lat is not None else None,
                 round(lng, 2) if lng is not None else None,
                 radius, country_filter, cable_name_filter,
                 min(limit, 2000))
    cached = _memo_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()

        query = """SELECT id, cable_id, cable_name, country, city, lat, lng, source
                     FROM cable_landing_points
                    WHERE lat IS NOT NULL AND lng IS NOT NULL"""
        params = []

        if lat is not None and lng is not None:
            lat_d = radius / 69.0
            lng_d = radius / (69.0 * max(math.cos(math.radians(lat)), 0.1))
            query += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            params.extend([lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d])

        if country_filter:
            query += " AND UPPER(country) = %s"
            params.append(country_filter)

        if cable_name_filter:
            query += " AND cable_name ILIKE %s"
            params.append(f"%{cable_name_filter}%")

        query += " ORDER BY cable_name NULLS LAST LIMIT %s"
        params.append(min(limit, 2000))

        cur.execute(query, params)
        rows = cur.fetchall()

        points = []
        for r in rows:
            points.append({
                'id': r[0],
                'cable_id': r[1],
                'cable_name': r[2],
                'country': r[3],
                'city': r[4],
                'lat': _safe_float(r[5]),
                'lng': _safe_float(r[6]),
                'source': r[7],
            })

        payload = {
            'success': True,
            'points': points,
            'count': len(points),
            'filters': {
                'country': country_filter or 'all',
                'cable_name': cable_name_filter or 'all',
                'spatial': lat is not None and lng is not None,
            },
            '_cache': 'miss',
        }
        _memo_set(cache_key, {**payload, '_cache': 'hit'})
        return jsonify(payload)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except:
                try: conn.close()
                except: pass


# ═══════════════════════════════════════════════════════════════
# INFRASTRUCTURE STATS — Combined counts for all tables
# ═══════════════════════════════════════════════════════════════

@infra_data_bp.route('/api/v1/infrastructure/stats', methods=['GET'])
def get_infrastructure_stats():
    """Get counts from all infrastructure tables."""
    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()

        stats = {}
        tables = [
            ('gas_pipelines', 'gas_pipelines'),
            ('power_plants', 'power_plants_eia'),
            ('transmission_lines', 'transmission_lines_eia'),
            ('submarine_cables', 'submarine_cables'),
            ('submarine_cable_landings', 'submarine_cable_landings'),
            ('substations', 'substations'),
            ('fiber_routes', 'fiber_routes'),
            ('discovered_facilities', 'discovered_facilities'),
        ]

        for key, table in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                stats[key] = cur.fetchone()[0]
            except:
                stats[key] = 0
                conn.rollback()

        return jsonify({
            'success': True,
            'stats': stats,
            'total': sum(stats.values())
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                from main import return_pg_connection
                return_pg_connection(conn)
            except:
                try: conn.close()
                except: pass
