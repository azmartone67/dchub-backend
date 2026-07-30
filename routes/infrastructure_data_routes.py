"""
DC Hub — Infrastructure Data Routes v2
═══════════════════════════════════════
New API endpoints for power_plants_eia, transmission_lines_eia, submarine_cables.
Plus: gas-pipelines added to rate limit bypass list.

Register in main.py:
    from routes.infrastructure_data_routes import register_infra_data_routes
    register_infra_data_routes(app, get_pg_connection)

Tables required (already created by bulk loaders):
    - power_plants_eia (~13.4K rows)
    - transmission_lines_eia (~56K rows — a GEOCODED SNAPSHOT with lat/lng and
      NO writer in this repo, so no refresh path. The maintained transmission
      layer is `transmission_lines` (~94.6K rows, routes/transmission_ingest.py),
      which stores no geometry. The two are NOT interchangeable: the map layer
      endpoints below need coordinates and therefore still read the snapshot,
      while COUNT surfaces report the maintained table. Corrected 2026-07-29 —
      this docstring previously claimed "94K+ rows" for the 56K table.)
    - submarine_cables (0 rows live — the subsea ingest has never run; upstream
      population is ~717 cables, proxied by /api/v1/infrastructure/submarine-cables)
    - submarine_cable_landings (0 rows live, same cause; upstream ~1,918 points)
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
    logger.info("   ⚡ /api/v1/transmission-lines (~56K geocoded snapshot; the "
                "maintained ~94.6K layer is transmission_lines, no geometry)")
    logger.info("   🌊 /api/v1/submarine-cables (DB table empty — ingest never ran)")
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
# r-tune 2026-06-11: uncapped — these feed the public land-power map's layer
# toggles (transmission, substations, gas, etc.), a growth surface. Capping to 50
# made toggled layers look broken. Monetize via the MCP/API paywalls instead.
_LAYER_CAP = {'anonymous': 100000, 'free': 100000, 'identified': 100000, 'developer': 100000}


def _layer_tier():
    def _dec(_t):
        try:
            import jwt as _j, os as _o
            secret = _o.environ.get('JWT_SECRET') or _o.environ.get('SECRET_KEY', '')
            return _j.decode(_t, secret, algorithms=['HS256'])
        except Exception:
            return None
    try:
        # fail open for credentialed callers so a paid user is never
        # downgraded to coarse coords by a transient resolution error.
        from map_tier_gating import detect_tier_failopen
        t, _ = detect_tier_failopen(decode_jwt_func=_dec)
        return (t or 'anonymous').lower()
    except Exception:
        return 'anonymous'


def _layer_cap(tier):
    return 100000 if tier in _LAYER_PAID else _LAYER_CAP.get(tier, 100000)


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
            # r-coord-disclosure (2026-07-26, tier-gating QA): the 0.1° rounding
            # was SILENT — an agent computing distances from these coords gets
            # ±10 km error with no warning, which is worse for citation trust
            # than the gate itself. Label the precision explicitly (owner call:
            # rounding for sub-Pro tiers is intended pricing architecture —
            # developer = analytics tier, pro = site-grade precision).
            payload['coord_precision'] = 'approx_0.1deg'
            payload['coord_precision_km'] = 11
            payload['coord_precision_note'] = (
                "lat/lng are rounded to 0.1° (~11 km, city-level) on this tier. "
                "Do not use for distance/adjacency math. Pro returns full-precision "
                "coordinates — dchub.cloud/pricing")
        else:
            payload['coord_precision'] = 'full'
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
            # r-coord-disclosure (2026-07-26, tier-gating QA): the 0.1° rounding
            # was SILENT — an agent computing distances from these coords gets
            # ±10 km error with no warning, which is worse for citation trust
            # than the gate itself. Label the precision explicitly (owner call:
            # rounding for sub-Pro tiers is intended pricing architecture —
            # developer = analytics tier, pro = site-grade precision).
            payload['coord_precision'] = 'approx_0.1deg'
            payload['coord_precision_km'] = 11
            payload['coord_precision_note'] = (
                "lat/lng are rounded to 0.1° (~11 km, city-level) on this tier. "
                "Do not use for distance/adjacency math. Pro returns full-precision "
                "coordinates — dchub.cloud/pricing")
        else:
            payload['coord_precision'] = 'full'
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
#
# 2026-07-29 — DECONTAMINATION. Three defects shipped here. All three
# were live-probed on dchub.cloud before this change; the numbers quoted
# below are what the endpoint actually published.
#
#   1. `total` WAS `sum(stats.values())` — a BLIND sum whose last member
#      is discovered_facilities. It published 305,471 = 282,377
#      infrastructure assets + 23,094 DATA-CENTRE FACILITIES. Facilities
#      are a subset of the built world, not an infrastructure asset
#      class; the two populations must never be summed. The homepage
#      capability tile already refuses this endpoint's `total` and
#      re-sums an explicit allow-list of layer keys for exactly that
#      reason — the endpoint now does the same thing itself, so no
#      consumer has to work around us to get an honest number.
#      A blind sum() is also structurally unsafe: ANY key appended to
#      the member list silently moves a published figure with no review
#      touching the number. Members are now an explicit role-tagged
#      allow-list and the payload states its own basis.
#
#   2. `transmission_lines` COUNTED THE WRONG TABLE. It read
#      `transmission_lines_eia` (56,108) while /api/v1/stats and
#      /api/v1/freshness/radar read `transmission_lines` (94,626) — one
#      concept, 1.7x apart, on keyless endpoints, and BOTH figures
#      render on the homepage simultaneously (hero pill 94.6k vs the
#      capability tile summing 56,108). Neither query filters: both are
#      bare COUNT(*). The divergence is purely the table NAME —
#      substations, gas_pipelines and fiber_routes resolve to the same
#      table on both endpoints and agree exactly (126,841 / 30,918 /
#      55,064). `transmission_lines` is maintained by a documented
#      full-replace ingest (routes/transmission_ingest.py: "~94,619
#      features", "The 94k EIA set supersedes the stale 52k HIFLD") and
#      the freshness radar reports it fresh. `transmission_lines_eia`
#      has NO WRITER ANYWHERE IN THIS REPO — grep for INSERT INTO /
#      CREATE TABLE / DELETE FROM / COPY against it returns nothing — so
#      it cannot be current by construction. `transmission_lines`
#      therefore takes the name, and the stale geocoded table keeps its
#      number under an honest one: `transmission_lines_geocoded_snapshot`.
#      That member is EXCLUDED from the asset total: it is a SUBSET of
#      the same population, so summing both would double-count
#      transmission. Publishing it rather than dropping it keeps the old
#      value observable, so a consumer that sees a number move can find
#      out which population it was looking at.
#      ★ NOT REPOINTED HERE: the SPATIAL consumers of
#      transmission_lines_eia — this file's GET /api/v1/transmission-lines,
#      dchub_mcp_server.py, routes/grid_intelligence_routes.py,
#      routes/energy_discovery_routes.py. They filter on lat/lng, and
#      transmission_lines is ingested with returnGeometry=false ("the
#      target table stores no geometry"), so repointing them would
#      return rows with null coordinates and break the map layer.
#      Whether the live table has since gained coordinates is UNVERIFIED
#      from source alone. This commit fixes the COUNT surfaces only;
#      repointing the map/MCP layers is a separate, DB-verified change.
#
#   3. A bare `except: stats[key] = 0` COLLAPSED missing-table,
#      permission-error and genuinely-empty into the identical published
#      value 0, for all eight members. submarine_cables and
#      submarine_cable_landings both published 0 while DC Hub's OWN
#      keyless /api/v1/infrastructure/submarine-cables returns 717
#      cables / 1,918 landings from TeleGeography on the same deploy. 0
#      was never the population — it is an ingest that has never run
#      (main.py registers subsea_cable_ingestion under entry-point names
#      the module does not define, so it reports 'no callable entry
#      point' and fires nothing). Unmeasured members now emit null plus
#      a machine-readable reason and contribute NOTHING to any total.
#      Never 0.
# ═══════════════════════════════════════════════════════════════

# Role tags decide what a member is allowed to contribute to:
#   'asset'    — an infrastructure asset class. Sums into
#                infrastructure_assets_total.
#   'subset'   — a narrower or stale view of a population already
#                counted by an 'asset' member. Published for legibility,
#                NEVER summed (it would double-count).
#   'facility' — DATA-CENTRE facilities. A different population from
#                infrastructure assets. Published for continuity, NEVER
#                summed into an asset total.
_STATS_MEMBERS = (
    # (published_key,                        table,                      role)
    ('gas_pipelines',                        'gas_pipelines',            'asset'),
    ('power_plants',                         'power_plants_eia',         'asset'),
    ('transmission_lines',                   'transmission_lines',       'asset'),
    ('submarine_cables',                     'submarine_cables',         'asset'),
    ('submarine_cable_landings',             'submarine_cable_landings', 'asset'),
    ('substations',                          'substations',              'asset'),
    ('fiber_routes',                         'fiber_routes',             'asset'),
    ('transmission_lines_geocoded_snapshot', 'transmission_lines_eia',   'subset'),
    ('discovered_facilities',                'discovered_facilities',    'facility'),
)

ASSET_KEYS = tuple(k for k, _t, role in _STATS_MEMBERS if role == 'asset')

# Why a non-asset member is excluded from the asset total. Published in
# the basis block so a consumer can see the exclusion was deliberate and
# does not have to guess whether we simply forgot to add it up.
_EXCLUSION_REASON = {
    'discovered_facilities': (
        'DATA-CENTRE FACILITIES, not an infrastructure asset class. A different '
        'population from the asset layers — facilities sit on top of the built '
        'world rather than being part of it. Summing the two inflates an asset '
        'count with buildings. Use /api/v1/stats/canonical for facility counts.'),
    'transmission_lines_geocoded_snapshot': (
        'A stale geocoded snapshot of the SAME population already counted by '
        '`transmission_lines`, retained under an explicit name so the number is '
        'still observable. Summing both would double-count transmission lines. '
        'Table `transmission_lines_eia` has no writer in the codebase and no '
        'refresh path, so it cannot be current; `transmission_lines` is the '
        'maintained layer and is what /api/v1/stats and the freshness radar '
        'report. Vintage of this snapshot is UNVERIFIED.'),
}

# What a 0 row count MEANS for a given member — so an unmeasured member
# can state why rather than publishing a bare, uninterpretable 0.
_MEMBER_EMPTY_REASON = {
    'submarine_cables': (
        'table exists but holds 0 rows: the subsea ingest has never run '
        '(subsea_cable_ingestion is registered in main.py under entry-point '
        'names it does not define, so it fires nothing). The real population is '
        '~717 cables per TeleGeography, served live and keyless at '
        '/api/v1/infrastructure/submarine-cables. 0 is not the count.'),
    'submarine_cable_landings': (
        'table exists but holds 0 rows: the subsea ingest has never run '
        '(subsea_cable_ingestion is registered in main.py under entry-point '
        'names it does not define, so it fires nothing). The real population is '
        '~1,918 landing points per TeleGeography, served live and keyless at '
        '/api/v1/infrastructure/submarine-cables. 0 is not the count.'),
}
_DEFAULT_EMPTY_REASON = (
    'table present but holds 0 rows. COUNT(*) cannot distinguish "never '
    'populated" from a true zero, and those are different claims, so this is '
    'reported as unmeasured rather than published as a count of 0.')

_ASSET_TOTAL_DEFINITION = (
    'Sum of the DISTINCT physical-infrastructure asset layers only. EXCLUDES '
    'data-centre facilities (a different population) and excludes any member '
    'tagged as a subset of a layer already counted. Unit: one per source record '
    'in the named table — not miles, not megawatts, not deduplicated real-world '
    'structures. Unmeasured members contribute nothing, so when '
    'complete=false the figure is a FLOOR that can only be below reality.')


def _stats_rollback(conn):
    """Clear an aborted transaction so one bad member can't take the rest down."""
    try:
        conn.rollback()
    except Exception:
        pass


def _measure_member(cur, conn, key, table):
    """COUNT(*) one member. Returns (value, reason).

    (int, None) = measured, and the int is > 0.
    (None, str) = UNMEASURED, with a machine-readable reason.

    Never returns 0 as a count. A table that was never populated and a
    table whose true population is zero are DIFFERENT CLAIMS, and
    COUNT(*) alone cannot tell them apart — so 0 is reported as
    unmeasured with a reason instead of being published as a figure.
    """
    if not isinstance(table, str) or not table.replace('_', '').isalnum():
        return None, 'invalid_table_identifier'
    # to_regclass returns NULL for an absent table WITHOUT raising, so an
    # absent member cannot abort the surrounding transaction and drag the
    # other members down with it (the old bare-except path rolled back
    # mid-loop and published 0 for whatever tripped it).
    try:
        cur.execute("SELECT to_regclass(%s)", ('public.' + table,))
        row = cur.fetchone()
    except Exception as exc:
        _stats_rollback(conn)
        return None, 'table_lookup_failed: %s' % type(exc).__name__
    if not row or not row[0]:
        return None, 'table_absent: public.%s does not exist' % table
    try:
        cur.execute("SELECT COUNT(*) FROM %s" % table)
        got = cur.fetchone()
    except Exception as exc:
        _stats_rollback(conn)
        return None, 'count_failed: %s' % type(exc).__name__
    value = (got or [None])[0]
    if value is None:
        return None, 'count_returned_null'
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None, 'count_not_an_integer'
    if value <= 0:
        return None, _MEMBER_EMPTY_REASON.get(key, _DEFAULT_EMPTY_REASON)
    return value, None


def build_infrastructure_stats_payload(measured):
    """Assemble the /api/v1/infrastructure/stats body from measured counts.

    `measured` maps published_key -> (value, reason) exactly as
    _measure_member returns it. PURE: no DB, no Flask, no module globals
    beyond the member table — so tests/test_infra_stats_asset_total.py
    can drive every branch (facility contamination, an unmeasured
    member, the transmission subset) without a database.
    """
    stats = {}
    unmeasured = {}
    summed = []
    for key, _table, role in _STATS_MEMBERS:
        value, reason = measured.get(key, (None, 'not_probed'))
        stats[key] = value
        if value is None:
            unmeasured[key] = reason
        elif role == 'asset':
            summed.append(key)

    asset_total = sum(stats[k] for k in summed) if summed else None
    asset_unmeasured = [k for k in ASSET_KEYS if stats.get(k) is None]

    basis = {
        'definition': _ASSET_TOTAL_DEFINITION,
        'members_summed': summed,
        'members_unmeasured': asset_unmeasured,
        'excluded': {
            key: _EXCLUSION_REASON.get(key, 'not an infrastructure asset layer')
            for key, _t, role in _STATS_MEMBERS if role != 'asset'
        },
        'complete': not asset_unmeasured,
        'is_floor': bool(asset_unmeasured),
    }
    if asset_total is None:
        basis['unavailable_reason'] = (
            'no infrastructure asset layer could be measured — emitting null '
            'rather than 0, because 0 would read as "there are no assets".')

    payload = {
        'success': True,
        'stats': stats,
        'unmeasured': unmeasured,
        'infrastructure_assets_total': asset_total,
        'infrastructure_assets_basis': basis,
    }

    # `total` is DEPRECATED but PRESERVED: it is a public field and
    # unknown consumers already read it, so it keeps its historical
    # composition (asset layers + discovered_facilities) rather than
    # being redefined underneath them. It is no longer a blind
    # sum(stats.values()) — an added member can't silently move it — and
    # it now carries a note saying exactly what it merges, so the
    # contamination is stated instead of implied. Its value did move
    # with this change, because `transmission_lines` was corrected from
    # the stale 56,108 table to the maintained 94,626 one; that is a
    # fixed count for an unchanged concept, and it is called out here
    # rather than left for a consumer to discover.
    legacy_keys = [k for k, _t, role in _STATS_MEMBERS
                   if role in ('asset', 'facility')]
    payload['total'] = sum(stats[k] for k in legacy_keys
                           if stats.get(k) is not None)
    payload['total_note'] = (
        'DEPRECATED — merges two different populations: infrastructure asset '
        'layers PLUS discovered_facilities (data-centre facilities). Do not use '
        'it as an infrastructure-asset count. Use infrastructure_assets_total, '
        'which excludes facilities and publishes its member list in '
        'infrastructure_assets_basis. Unmeasured members contribute nothing. '
        'Composition retained for backward compatibility; note that '
        '`transmission_lines` was corrected on 2026-07-29 from the unmaintained '
        '`transmission_lines_eia` table to the maintained `transmission_lines` '
        'table, so this figure moved without its definition changing.')
    return payload


@infra_data_bp.route('/api/v1/infrastructure/stats', methods=['GET'])
def get_infrastructure_stats():
    """Counts per infrastructure table, plus a facility-free asset total.

    Publishes `infrastructure_assets_total` with its basis and member
    list. Unmeasured members are null + a reason, never 0. See the block
    comment above for why each of those is the way it is.
    """
    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()
        measured = {}
        for key, table, _role in _STATS_MEMBERS:
            measured[key] = _measure_member(cur, conn, key, table)
        return jsonify(build_infrastructure_stats_payload(measured))
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
