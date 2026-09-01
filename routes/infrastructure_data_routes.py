"""
DC Hub — Infrastructure Data Routes v2
═══════════════════════════════════════
New API endpoints for power_plants_eia, transmission_lines_eia, subsea_cables.
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
    - subsea_cables (691 rows, live-measured 2026-07-29) and
      subsea_landing_points (1,908 rows) — the POPULATED subsea layer, written by
      subsea_cable_ingestion.py (INSERT ... ON CONFLICT (cable_id) / (point_id)).
      ★ These replace `submarine_cables` / `submarine_cable_landings`, which this
      module used to read. Those two tables EXIST and hold 0 rows: the only
      writer for `submarine_cables` anywhere in the repo is the standalone ETL
      01_submarine_cables.py, whose filename begins with a digit so it can never
      be imported as a module, and which is scheduled nowhere;
      `submarine_cable_landings` has no CREATE and no writer at all. They are
      ABANDONED, not pending. Corrected 2026-07-29 — this docstring previously
      said the subsea ingest "has never run", which was false: it ran on
      2026-03-27 and populated the subsea_* pair.
      ★ UNIT: one row per distinct TeleGeography cable id / landing-point id.
      cable_id and point_id are UNIQUE, and live measurement confirms
      rows == distinct ids == distinct names on both tables, with no
      duplicate/visibility flag on either — so COUNT(*) counts cables and
      landing points, not route segments or GIS vertices.
      ★ ATTRIBUTES ARE EMPTY. Live measurement 2026-07-29: of 691 cable rows,
      0 carry owners, length_km, rfs_year, is_planned or rfs_date; of 1,908
      landing-point rows, 0 carry country, country_code or cable_ids. Upstream
      cable-geo.json only publishes id/name/url/geometry per feature. This layer
      is IDENTITY + ROUTE GEOMETRY, and any endpoint returning those attribute
      fields returns nulls — say so rather than implying the data is there.
    - cable_landing_points (0 rows live, 2026-07-29). Per-CABLE-PER-LANDING
      attribution (cable_id + city), a DIFFERENT UNIT from subsea_landing_points
      (one row per landing point). NOT repointed for that reason: swapping it
      would silently change what a row means. Left unmeasured with a reason.
"""
import math
import time
import threading
import logging
from flask import Blueprint, request, jsonify

from util.transmission_tables import (
    GEOCODED_SNAPSHOT_KEY,
    GEOCODED_SNAPSHOT_TABLE,
    coverage as _tx_coverage,
)

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
    logger.info("   🌊 /api/v1/submarine-cables (subsea_cables 691 + "
                "subsea_landing_points 1,908; identity + route geometry only)")
    logger.info("   📡 /api/v1/cable-landing-points (cable_landing_points — 0 rows "
                "live; different unit from subsea_landing_points, not repointed)")


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
        from map_tier_gating import detect_tier_for_data_gate
        t, _ = detect_tier_for_data_gate(decode_jwt_func=_dec)
        return (t or 'anonymous').lower()
    except Exception:
        return 'anonymous'


def _layer_cap(tier):
    return 100000 if tier in _LAYER_PAID else _LAYER_CAP.get(tier, 100000)


# r47.33 (2026-05-26): process-local memo for the heavy land-power-map
# endpoints. Geographic data is the same for any caller hitting the same
# query-param set — power_plants_eia has 13K rows, transmission_lines_eia
# has 56K (2026-07-29: this comment cited ~94K, which is the OTHER table).
# Doing the bounding-box scan + ORDER BY on every authed map
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
# TRANSMISSION LINES API — ~56K geocoded snapshot lines with lat/lng.
# NOT the maintained layer: transmission_lines holds ~94.6K maintained rows and
# stores no geometry, so what this endpoint can show is a 40.7% FLOOR.
# LIVE-VERIFIED 2026-07-29 (/api/v1/admin/schema): transmission_lines has 14
# columns and none of them is a coordinate, which settles the "whether the live
# table has since gained coordinates is UNVERIFIED" question left open by #1922.
# It has not. The repoint is impossible, not merely unattempted.
# (2026-07-29: this banner previously claimed ~94K HIFLD lines.)
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

    # r47.33: memo by normalized params — a ~56K-row bbox scan makes this the
    # single most expensive map endpoint. Tier in the key so a paid caller's
    # full result is never served to a free/anon caller.
    # (2026-07-29: this comment cited a ~94K row count — wrong table.)
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

        # MUST stay on the geocoded snapshot. transmission_lines has 94,626 rows
        # but NO lat/lng (live schema verified 2026-07-29), so repointing this
        # raises UndefinedColumn — and the except-500 below would turn the PAID
        # map layer BLANK rather than fuller. See util/transmission_tables.py.
        query = f"""SELECT id, owner, voltage_kv, sub_1, sub_2, lat, lng, state
                   FROM {GEOCODED_SNAPSHOT_TABLE}
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
        # 2026-07-29 — STATE THE VINTAGE WHERE THE ROWS ARE SERVED. #1922 fixed
        # the COUNT surfaces but deliberately left the spatial layers alone, so
        # this endpoint still served a frozen snapshot with no writer and no
        # timestamp, 38,518 lines behind the maintained table, and said nothing
        # about it. A caller had no way to tell the layer was 40.7% short.
        payload['served_from_key'] = GEOCODED_SNAPSHOT_KEY
        payload['count_is_floor'] = True
        try:
            payload['coverage'] = _tx_coverage(cur)
        except Exception as cov_err:                          # noqa: BLE001
            # Fail soft: never lose the rows because the basis probe failed, and
            # never publish a 0 or a stale coverage figure. None + reason.
            logger.warning(f"transmission coverage probe failed: {cov_err}")
            payload['coverage'] = None
            payload['coverage_unmeasured_reason'] = (
                f"{type(cov_err).__name__}: {str(cov_err)[:160]}")
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
# SUBMARINE CABLES API — subsea_cables (691) + subsea_landing_points (1,908)
#
# 2026-07-29 — WRONG-TABLE REPOINT. This endpoint is keyless and public,
# and it was returning HTTP 200 with
#     {"success": true, "cable_count": 0, "cables": [],
#      "landing_count": 0, "landings": []}
# under a section header claiming "690 cables worldwide + landing points".
# An empty layer published as a success. It read `submarine_cables` and
# `submarine_cable_landings`, which exist and hold 0 rows and have no
# working writer, while the POPULATED pair — subsea_cables (691 rows) and
# subsea_landing_points (1,908 rows), written by subsea_cable_ingestion.py
# and already served at /api/v1/subsea/* — sat in the same database.
#
# ★ WHAT A REPOINT CANNOT FIX, so it is STATED instead of implied: the
#   subsea snapshot is IDENTITY + ROUTE GEOMETRY ONLY. Live column census
#   2026-07-29 over all 691 cable rows: owners set on 0, length_km on 0,
#   rfs_year on 0, is_planned on 0, rfs_date on 0. Over all 1,908 landing
#   points: country on 0, country_code on 0, cable_ids on 0, cable_count>0
#   on 0. TeleGeography's cable-geo.json carries only id/name/url/geometry
#   per feature; the attributes live on per-cable detail documents this
#   ingest does not fetch. So those response fields are null — they are
#   kept for shape compatibility (no consumer ever received a non-null
#   value from the 0-row table) and the payload says why.
#
# ★ THE `country` FILTER CANNOT BE APPLIED and must not answer 0. country
#   is '' on all 1,908 rows, so `WHERE UPPER(country) = 'JP'` matches
#   nothing for EVERY country. Answering 0 would assert "Japan has no
#   cable landings". The filter now reports itself unapplied with a
#   reason and landing_count is null, never 0. Deriving country from the
#   trailing token of `name` ("Aasiaat, Greenland") was considered and
#   rejected: that is inference dressed as data.
# ═══════════════════════════════════════════════════════════════

# Vintage and drift of the subsea snapshot, measured 2026-07-29 against
# TeleGeography live. Published so a consumer knows this is a snapshot and
# not a live bind, and knows which direction it can be wrong in.
_SUBSEA_SNAPSHOT_BASIS = {
    'source': 'TeleGeography submarinecablemap.com (cable-geo.json, '
              'landing-point-geo.json) via subsea_cable_ingestion.py',
    'as_of': '2026-03-27',
    'as_of_basis': 'MAX(updated_at) on subsea_cables and subsea_landing_points, '
                   'read live 2026-07-29',
    'unit': 'one row per distinct TeleGeography cable id / landing-point id. '
            'cable_id and point_id are UNIQUE; live measurement confirms '
            'rows == distinct ids == distinct names on both tables and there is '
            'no duplicate or visibility flag on either. NOT route segments: '
            'upstream ships 717 cable FEATURES for 696 distinct cables '
            '(20 ids carry more than one MultiLineString), and this table '
            'stores one row per cable, not per feature.',
    'attributes_unpopulated': [
        'owners', 'length_km', 'rfs_year', 'is_planned', 'rfs_date',
        'country', 'country_code', 'cable_ids', 'cable_count', 'is_major_hub'],
    'attributes_unpopulated_reason': (
        'live column census 2026-07-29: each of these is empty or null on 100% '
        'of rows. Upstream cable-geo.json publishes only id, name, url and '
        'geometry per feature. This layer is identity + route geometry; the '
        'fields are returned as null rather than omitted so the response shape '
        'does not change, and they must not be read as "zero" or "none".'),
    'drift': (
        'the ingest upserts and never deletes, so this snapshot drifts in BOTH '
        'directions and is NOT a floor. Measured against TeleGeography live on '
        '2026-07-29: 7 cable ids and 15 landing-point ids exist upstream but not '
        'here (added since the snapshot), and 2 cable ids (alpal-2, '
        'confluence-1) and 5 landing-point ids are held here but no longer '
        'exist upstream (withdrawn or renamed since, and nothing removes them). '
        'Net 691 vs 696 upstream distinct cables, 1,908 vs 1,918 upstream '
        'landing points.'),
}


@infra_data_bp.route('/api/v1/submarine-cables', methods=['GET'])
def get_submarine_cables():
    """Get submarine cables and landing points from the populated subsea layer.

    Query params:
        lat, lng, radius (miles) — filter landing points by location
        country — ACCEPTED BUT NOT APPLICABLE: country attribution is
                  unpopulated on every row of this snapshot, so the filter
                  reports itself unapplied rather than answering 0.
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

        # 2026-07-29: reads subsea_cables (691 rows), NOT the abandoned
        # 0-row `submarine_cables`. `status` and `source` do not exist on
        # this table; they are emitted as a stated constant / null below
        # rather than dropped, because the response shape is public.
        cur.execute("""SELECT id, cable_id, name, length_km, rfs_year, owners,
                              is_planned, url
                         FROM subsea_cables
                        ORDER BY cable_id
                        LIMIT %s""",
                    [min(limit, 1000)])
        cables = []
        for r in cur.fetchall():
            cables.append({
                'id': r[0], 'cable_id': r[1], 'name': r[2],
                # ★ length_km / rfs_year / owners / is_planned are null on
                # 100% of rows — see _SUBSEA_SNAPSHOT_BASIS. Not zeros.
                'length_km': _safe_float(r[3]), 'rfs_year': r[4],
                'owners': r[5] or None, 'is_planned': r[6],
                # `status` was a column on the abandoned table and has no
                # equivalent here. null, never an invented 'active'.
                'status': None,
                'source': 'TeleGeography submarinecablemap.com',
                'url': r[7] or None,
            })

        # cable_count is the POPULATION, not the page size. The old code
        # published len(cables), which under the default limit=100 would
        # now read as "100 submarine cables worldwide". Both figures are
        # published, each under a name that says which one it is.
        cur.execute("SELECT COUNT(*) FROM subsea_cables")
        cable_population = int((cur.fetchone() or [0])[0] or 0)

        # ── Landing points ────────────────────────────────────────────
        # The country filter cannot be honoured: country is '' on all
        # 1,908 rows, so applying it would answer 0 for every country on
        # earth. Refuse it explicitly instead of silently answering 0.
        country_unappliable = None
        if country_filter:
            country_unappliable = (
                "country filter NOT APPLIED: country attribution is unpopulated "
                "on every one of the 1,908 subsea_landing_points rows (live "
                "census 2026-07-29), so filtering would match nothing for every "
                "country. Answering 0 would assert this country has no cable "
                "landings, which is a claim this snapshot cannot make. Landing "
                "points are returned unfiltered by country; landing_count is "
                "null because no filtered count is measurable."
            )

        lp_where = "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
        lp_params = []
        spatial = lat is not None and lng is not None
        if spatial:
            lat_d = radius / 69.0
            lng_d = radius / (69.0 * max(math.cos(math.radians(lat)), 0.1))
            lp_where += (" AND latitude BETWEEN %s AND %s"
                         " AND longitude BETWEEN %s AND %s")
            lp_params.extend([lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d])

        cur.execute(
            "SELECT id, point_id, name, country, latitude, longitude, cable_ids"
            " FROM subsea_landing_points " + lp_where +
            " ORDER BY point_id LIMIT %s", lp_params + [min(limit, 500)])
        landings = []
        for r in cur.fetchall():
            landings.append({
                'id': r[0], 'point_id': r[1], 'name': r[2],
                # country is '' on every row; normalise to null so a
                # consumer cannot read '' as a known-blank country.
                'country': (r[3] or None),
                'lat': _safe_float(r[4]), 'lng': _safe_float(r[5]),
                'cable_ids': (r[6] if r[6] not in ('', '[]') else None),
            })

        # Population matching the APPLIED filters (spatial only), not the
        # page size, and not limited by `limit`.
        if country_unappliable:
            landing_population = None
        else:
            cur.execute("SELECT COUNT(*) FROM subsea_landing_points " + lp_where,
                        lp_params)
            landing_population = int((cur.fetchone() or [0])[0] or 0)

        payload = {
            'success': True,
            'cables': cables,
            'cable_count': cable_population,
            'cables_returned': len(cables),
            'landings': landings,
            'landing_count': landing_population,
            'landings_returned': len(landings),
            'counts_basis': {
                'cable_count': 'COUNT(*) on subsea_cables — the whole layer, '
                               'unaffected by `limit`. One row per distinct '
                               'TeleGeography cable id.',
                'cables_returned': 'rows in THIS response, capped by `limit`.',
                'landing_count': 'COUNT(*) on subsea_landing_points matching the '
                                 'APPLIED filters (spatial only), unaffected by '
                                 '`limit`. null when a requested filter could '
                                 'not be applied — never 0.',
                'landings_returned': 'rows in THIS response, capped by `limit`.',
                'filters_applied': {
                    'spatial': spatial,
                    'country': False if country_filter else None,
                },
            },
            'subsea_basis': _SUBSEA_SNAPSHOT_BASIS,
            '_cache': 'miss',
        }
        if country_unappliable:
            payload['unmeasured'] = {'landing_count': country_unappliable}
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
#
# 2026-07-29: `cable_landing_points` measured live at 0 ROWS, and this
# endpoint was publishing {"success": true, "count": 0, "points": []} —
# an unmeasured layer answering as a population of zero.
#
# ★ NOT REPOINTED to subsea_landing_points, deliberately. The two are a
#   DIFFERENT UNIT: cable_landing_points is one row per CABLE-PER-LANDING
#   (it carries cable_id + cable_name + city), while subsea_landing_points
#   is one row per LANDING POINT with cables aggregated into a cable_ids
#   text column — and that column is empty on all 1,908 rows anyway, so
#   the per-cable attribution this endpoint's contract promises cannot be
#   reconstructed from it. Swapping the table would silently change what a
#   row means, which is exactly the defect being fixed elsewhere in this
#   file. It stays unmeasured, with a reason, until a writer exists.
# ★ ALSO OPEN, not fixed here (a frontend repo, different deploy target):
#   dchub-frontend/js/land-power-app.js:11534 reads `lpData.landing_points`
#   from this endpoint, which returns `points`. So the map's submarine
#   landings overlay renders zero markers for a SECOND, independent
#   reason, and would keep rendering zero even against a populated table.

@infra_data_bp.route('/api/v1/cable-landing-points', methods=['GET'])
def get_cable_landing_points():
    """Cable landing points with optional spatial / country filtering.

    Backs the submarine-cable landings overlay on the land-power map.

    UNIT: one row per cable-per-landing (cable_id + city), NOT one row per
    landing point. See the note above for why this is not served from
    subsea_landing_points.

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

        # An empty result has two incompatible meanings: "your filter
        # matched nothing" (a real 0) and "this table was never
        # populated" (unmeasured). Only the second is true here, and
        # publishing 0 for it asserts there are no cable landings on
        # earth. Distinguish them with one extra COUNT(*), and emit null
        # + a reason for the unmeasured case.
        if not points:
            cur.execute("SELECT COUNT(*) FROM cable_landing_points")
            table_rows = int((cur.fetchone() or [0])[0] or 0)
            if table_rows == 0:
                payload['count'] = None
                payload['unmeasured'] = {'count': (
                    'table public.cable_landing_points exists but holds 0 rows '
                    '(live 2026-07-29) — it has no working writer, so this is '
                    '"never populated", not a population of zero. UNIT is one '
                    'row per cable-per-landing (cable_id + city), which is a '
                    'DIFFERENT unit from subsea_landing_points (one row per '
                    'landing point, 1,908 rows) — so this is deliberately not '
                    'repointed there. For landing points as such use '
                    '/api/v1/submarine-cables or /api/v1/subsea/landing-points.')}
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
#      cable features / 1,918 landings from TeleGeography on the same
#      deploy. Unmeasured members now emit null plus a machine-readable
#      reason and contribute NOTHING to any total. Never 0.
#
# ═══════════════════════════════════════════════════════════════
# 2026-07-29 (SAME DAY, SECOND PASS) — THE SUBSEA MEMBERS WERE A
# WRONG-TABLE READ, AND THE CAUSE PUBLISHED ABOVE WAS FALSE.
#
# The pass immediately above correctly stopped publishing 0 for the two
# subsea members, and then published a CONFIDENT EXPLANATION that was
# wrong in every clause. The live `unmeasured` reason read: "the subsea
# ingest has never run (subsea_cable_ingestion is registered in main.py
# under entry-point names it does not define, so it fires nothing)".
# Measured 2026-07-29 against the live database and the live API:
#
#   · The ingest HAS run and DID populate. subsea_cables holds 691 rows,
#     subsea_landing_points holds 1,908, MAX(updated_at) = 2026-03-27,
#     and both are already served at /api/v1/subsea/cables,
#     /api/v1/subsea/landing-points and /api/v1/fiber/summary.
#   · The two bad registrations in main.py are MANUAL internal-key admin
#     endpoints, not the trigger. The real trigger — POST
#     /api/jobs/subsea-sync -> fiber_integration.py -> run_subsea_sync —
#     was wired correctly the whole time. (#1923 separately fixed the one
#     genuine dead-job: subsea_sync sat in dchub-scheduler.py
#     DISABLED_JOBS with a full weekly schedule and no disabled_reason.)
#   · The members were reading the WRONG TABLES. `submarine_cables` and
#     `submarine_cable_landings` exist and hold 0 rows and are ABANDONED:
#     the sole writer for the first is the standalone ETL
#     01_submarine_cables.py, whose filename begins with a digit so it can
#     never be imported as a module and which is scheduled nowhere; the
#     second has no CREATE and no writer at all. The fix is two
#     identifiers, not an ingest.
#
# ★ THE LESSON, which is the reason this block is this long: a
#   confidently-wrong explanation shipped to customers is WORSE than no
#   explanation. "0 is not the count" was right and worth shipping; the
#   causal story bolted onto it was invented from a plausible-looking
#   grep of main.py and never checked against the database that was one
#   query away. An `unmeasured` reason is a PUBLISHED CLAIM and carries
#   the same evidentiary burden as the figure it replaces. If the cause
#   is not measured, say UNVERIFIED — the null already does the honest
#   work on its own.
#
# ★ WHAT THE REPOINTED MEMBERS NOW MEAN — verified before the flip, not
#   assumed, because a repoint that silently counts a different unit is
#   the bug being fixed and not the fix:
#     · UNIT: cable_id and point_id are UNIQUE, and live measurement gives
#       rows == distinct ids == distinct names on BOTH tables
#       (691/691/691 and 1908/1908/1908). Neither table has a duplicate,
#       suppression or visibility column — the only such column anywhere
#       in the subsea/cable family is `status` on the abandoned 0-row
#       `submarine_cables`. So COUNT(*) counts cables and landing points.
#     · NOT SEGMENTS: upstream cable-geo.json ships 717 FEATURES for 696
#       distinct cable ids (20 ids carry more than one MultiLineString;
#       echo alone has 3). This table stores one row per cable id, so it
#       is immune to the hosting_capacity "rows = GIS vertices" class of
#       error. The 717 figure is a SEGMENT count and is corrected
#       wherever it was published as a cable count.
#     · SNAPSHOT, NOT A FLOOR. The ingest upserts and never deletes, so it
#       drifts BOTH ways. Measured against TeleGeography live 2026-07-29:
#       7 cables and 15 landing points exist upstream but not here; 2
#       cables (alpal-2, confluence-1) and 5 landing points are held here
#       but no longer exist upstream. 691 vs 696 upstream distinct cables
#       is therefore explained — it is staleness plus a missing delete
#       path, NOT dropped records at ingest. `complete: true` below means
#       "every member was measured", and must not be read as "every
#       member is live"; each member's vintage is published in
#       `member_basis`.
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
    # 2026-07-29: repointed from the abandoned 0-row `submarine_cables` /
    # `submarine_cable_landings` to the POPULATED pair. The published KEYS
    # are unchanged — they name the concept, and unknown consumers read
    # them — only the table each one counts is corrected.
    ('submarine_cables',                     'subsea_cables',            'asset'),
    ('submarine_cable_landings',             'subsea_landing_points',    'asset'),
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
        'report. Vintage of this snapshot is UNKNOWABLE from the data, not '
        'merely unverified: live schema check 2026-07-29 confirms the table has '
        '12 columns and not one of them is temporal, so it cannot report its '
        'own age. It is older than transmission_lines by construction.'),
}

# What a 0 row count MEANS for a given member — so an unmeasured member
# can state why rather than publishing a bare, uninterpretable 0.
#
# ★ These are the CONTINGENCY reasons, reached only if the table this
#   member counts is genuinely empty. They no longer assert a cause. The
#   text they replaced named a specific cause ("the subsea ingest has
#   never run ... it fires nothing") that measurement disproved in every
#   clause — see the second-pass block above. A reason may state what was
#   observed and what is not knowable from it; it may not invent a
#   mechanism.
_MEMBER_EMPTY_REASON = {
    'submarine_cables': (
        'table public.subsea_cables returned 0 rows on this request. It held '
        '691 rows when last measured (2026-07-29, MAX(updated_at) 2026-03-27), '
        'so 0 indicates a read or ingest problem rather than a real population '
        'of zero — CAUSE UNVERIFIED from this endpoint. Upstream TeleGeography '
        'publishes 696 distinct cables (717 route features); the live proxy is '
        '/api/v1/infrastructure/submarine-cables. 0 is not the count.'),
    'submarine_cable_landings': (
        'table public.subsea_landing_points returned 0 rows on this request. It '
        'held 1,908 rows when last measured (2026-07-29), so 0 indicates a read '
        'or ingest problem rather than a real population of zero — CAUSE '
        'UNVERIFIED from this endpoint. Upstream TeleGeography publishes 1,918 '
        'distinct landing points; the live proxy is '
        '/api/v1/infrastructure/submarine-cables. 0 is not the count.'),
}

# Per-member basis: unit, vintage and known drift for a MEASURED member.
# `complete: true` only ever meant "every member was measured"; without
# this block a consumer could read it as "every member is live". Only
# members whose vintage has actually been measured appear here — an absent
# entry means unstated, never "fresh".
_MEMBER_BASIS = {
    'submarine_cables': {
        'table': 'subsea_cables',
        'unit': 'one row per distinct TeleGeography cable id. cable_id is '
                'UNIQUE and live measurement gives rows == distinct cable_id == '
                'distinct name == 691, with no duplicate/visibility column on '
                'the table. NOT route segments: upstream ships 717 FEATURES for '
                '696 distinct cables (20 ids carry more than one '
                'MultiLineString).',
        'as_of': '2026-03-27',
        'as_of_basis': 'MAX(updated_at) on the table, read live 2026-07-29',
        'is_live_bind': False,
        'drift': 'snapshot, NOT a floor — the ingest upserts and never deletes. '
                 'Measured against TeleGeography live 2026-07-29: 7 cable ids '
                 'exist upstream but not here, and 2 (alpal-2, confluence-1) '
                 'are held here but no longer exist upstream. 691 here vs 696 '
                 'upstream distinct.',
    },
    'submarine_cable_landings': {
        'table': 'subsea_landing_points',
        'unit': 'one row per distinct TeleGeography landing-point id. point_id '
                'is UNIQUE and live measurement gives rows == distinct point_id '
                '== distinct name == 1,908, with no duplicate/visibility column.',
        'as_of': '2026-03-27',
        'as_of_basis': 'MAX(updated_at) on the table, read live 2026-07-29',
        'is_live_bind': False,
        'drift': 'snapshot, NOT a floor — no delete path. Measured 2026-07-29: '
                 '15 landing-point ids exist upstream but not here, 5 are held '
                 'here but no longer exist upstream. 1,908 here vs 1,918 '
                 'upstream distinct.',
    },
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
    'complete=false the figure is a FLOOR that can only be below reality. '
    'complete=true means every member was measured, NOT that every member is a '
    'live bind: members whose source is a dated snapshot are listed with their '
    'vintage and known drift direction in member_basis, and a snapshot member '
    'is not guaranteed to round down.')


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
        'complete_means': (
            'every asset member was MEASURED. It does not mean every member is '
            'a live bind — see member_basis for the vintage of any member whose '
            'vintage has been measured. An absent member_basis entry means '
            'unstated, not fresh.'),
        'is_floor': bool(asset_unmeasured),
        # Only members that were actually measured get a basis published;
        # a null member's basis would describe a figure that isn't there.
        'member_basis': {
            key: _MEMBER_BASIS[key] for key, _t, _r in _STATS_MEMBERS
            if key in _MEMBER_BASIS and stats.get(key) is not None
        },
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
        'Composition retained for backward compatibility. Two corrections moved '
        'this figure on 2026-07-29 without changing its definition: '
        '`transmission_lines` was repointed from the unmaintained '
        '`transmission_lines_eia` table to the maintained `transmission_lines` '
        'table, and `submarine_cables` / `submarine_cable_landings` were '
        'repointed from two abandoned 0-row tables to the populated '
        '`subsea_cables` (691) / `subsea_landing_points` (1,908), which took '
        'them from unmeasured to measured and added 2,599 to every total they '
        'appear in.')
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
