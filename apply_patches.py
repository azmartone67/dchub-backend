#!/usr/bin/env python3
"""
DC Hub Infrastructure Patches — Auto-Apply Script
Run from ~/workspace on Railway:
    python3 apply_patches.py

Applies 4 surgical patches:
  1. main.py: site-score endpoint (multi-table proximity)
  2. main.py: MCP teaser for analyze_site (new fields)
  3. energy_infrastructure_routes.py: require_plan X-Internal-Key bypass
  4. infrastructure_discovery.py: SubstationDiscovery bulk HIFLD pull
"""

import re
import sys
import os
from internal_auth import is_valid_internal_key, get_internal_key_for_client

def apply_patch(filepath, old_marker_start, old_marker_end, new_code, patch_name):
    """Replace code between two marker strings in a file."""
    if not os.path.exists(filepath):
        print(f"  ❌ SKIP {patch_name}: {filepath} not found")
        return False
    
    with open(filepath, 'r') as f:
        content = f.read()
    
    start_idx = content.find(old_marker_start)
    if start_idx == -1:
        print(f"  ❌ SKIP {patch_name}: start marker not found in {filepath}")
        print(f"     Looking for: {old_marker_start[:80]}...")
        return False
    
    end_idx = content.find(old_marker_end, start_idx + len(old_marker_start))
    if end_idx == -1:
        print(f"  ❌ SKIP {patch_name}: end marker not found in {filepath}")
        print(f"     Looking for: {old_marker_end[:80]}...")
        return False
    
    before = content[:start_idx]
    after = content[end_idx:]
    new_content = before + new_code + after
    
    with open(filepath, 'w') as f:
        f.write(new_content)
    
    lines_removed = content[start_idx:end_idx].count('\n')
    lines_added = new_code.count('\n')
    print(f"  ✅ {patch_name}: replaced {lines_removed} lines with {lines_added} lines in {filepath}")
    return True


# =============================================================================
# PATCH 1 — Site Score Multi-Table Proximity (main.py)
# =============================================================================
PATCH1_START = """# =============================================================================
# SITE SCORE ENDPOINT — MCP analyze_site tool
# Combines nearby facilities, substations, connectivity, and state-level risk
# =============================================================================
@app.route('/api/site-score', methods=['GET'])
def api_site_score():"""

PATCH1_END = """@app.route('/api/agents/recommend', methods=['GET'])"""

PATCH1_NEW = """# =============================================================================
# SITE SCORE ENDPOINT — MCP analyze_site tool (v2 — multi-table proximity)
# Combines substations, gas pipelines, power plants, fiber, facilities, risk
# =============================================================================
@app.route('/api/site-score', methods=['GET'])
def api_site_score():
    \"\"\"Composite site suitability score for data center development.\"\"\"
    internal_key = request.headers.get("X-Internal-Key", "")
    if not is_valid_internal_key(internal_key):
        user = getattr(request, "current_user", None)
        plan = (user or {}).get("plan", "free") if isinstance(user, dict) else "free"
        if plan not in ("pro", "enterprise"):
            return jsonify({"error": "plan_required", "message": "Site scoring requires Pro plan.", "success": False}), 403
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    state = request.args.get('state', '').upper()
    capacity = request.args.get('capacity', 0, type=float)

    if not lat or not lon:
        return jsonify({'success': False, 'error': 'lat and lon are required'}), 400

    conn = None
    try:
        conn = get_read_db()
        c = conn.cursor()

        # 1. Nearby facilities (competitive density, ~100km radius)
        c.execute(\"\"\"
            SELECT COUNT(*) as cnt, COALESCE(SUM(power_mw), 0) as total_mw
            FROM discovered_facilities
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
              AND (latitude - %s)*(latitude - %s) + (longitude - %s)*(longitude - %s) < 0.81
        \"\"\", (lat, lat, lon, lon))
        row = c.fetchone()
        nearby_facilities = row[0] or 0
        nearby_mw = float(row[1] or 0)

        # 2. Nearby substations — MULTI-TABLE (~50km radius)
        nearby_substations = 0
        try:
            c.execute(\"\"\"
                SELECT COUNT(*) FROM substations
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                  AND (voltage_kv > 69 OR voltage_kv IS NULL OR voltage_kv = 0)
                  AND (lat - %s)*(lat - %s) + (lng - %s)*(lng - %s) < 0.20
            \"\"\", (lat, lat, lon, lon))
            nearby_substations = c.fetchone()[0] or 0
        except Exception:
            pass

        # Source B: infrastructure_layers table (4,939+ KMZ features)
        infra_substations = 0
        try:
            c.execute(\"\"\"
                SELECT COUNT(*) FROM infrastructure_layers
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                  AND LOWER(layer_type) IN ('substation', 'electric_substation', 'substations', 'power')
                  AND (latitude - %s)*(latitude - %s) + (longitude - %s)*(longitude - %s) < 0.20
            \"\"\", (lat, lat, lon, lon))
            infra_substations = c.fetchone()[0] or 0
        except Exception:
            pass

        total_substations = nearby_substations + infra_substations

        # 3. Nearby gas pipelines (~50km radius)
        nearby_gas_pipelines = 0
        try:
            c.execute(\"\"\"
                SELECT COUNT(*) FROM gas_pipelines
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                  AND status = 'active'
                  AND (lat - %s)*(lat - %s) + (lng - %s)*(lng - %s) < 0.20
            \"\"\", (lat, lat, lon, lon))
            nearby_gas_pipelines = c.fetchone()[0] or 0
        except Exception:
            pass

        # 4. Nearby power plants (~80km radius)
        nearby_power_plants = 0
        nearby_generation_mw = 0
        try:
            c.execute(\"\"\"
                SELECT COUNT(*), COALESCE(SUM(
                    CASE WHEN metadata IS NOT NULL 
                         THEN CAST(NULLIF(metadata->>'capacity_mw', '') AS NUMERIC) 
                         ELSE 0 END
                ), 0)
                FROM infrastructure_layers
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                  AND LOWER(layer_type) IN ('power_plant', 'power_plants', 'generation')
                  AND (latitude - %s)*(latitude - %s) + (longitude - %s)*(longitude - %s) < 0.52
            \"\"\", (lat, lat, lon, lon))
            pp_row = c.fetchone()
            nearby_power_plants = pp_row[0] or 0
            nearby_generation_mw = float(pp_row[1] or 0)
        except Exception:
            pass

        # Fallback: discovered_power_plants table
        try:
            c.execute(\"\"\"
                SELECT COUNT(*), COALESCE(SUM(capacity_mw), 0)
                FROM discovered_power_plants
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                  AND (latitude - %s)*(latitude - %s) + (longitude - %s)*(longitude - %s) < 0.52
            \"\"\", (lat, lat, lon, lon))
            dpp_row = c.fetchone()
            nearby_power_plants += (dpp_row[0] or 0)
            nearby_generation_mw += float(dpp_row[1] or 0)
        except Exception:
            pass

        # 5. Fiber connectivity
        METRO_FIBER_SCORES = {
            'VA': 95, 'NJ': 90, 'NY': 88, 'CA': 88, 'IL': 85,
            'TX': 82, 'GA': 80, 'MA': 82, 'MD': 78, 'WA': 78,
            'PA': 76, 'OR': 75, 'OH': 74, 'FL': 74, 'CO': 73,
            'AZ': 72, 'NC': 72, 'MN': 70, 'MI': 70, 'NV': 70,
            'UT': 69, 'IA': 68, 'TN': 68, 'MO': 67, 'IN': 66,
            'WI': 65, 'KY': 63, 'SC': 62, 'AL': 60, 'NE': 60,
            'KS': 58, 'OK': 56, 'AR': 55, 'LA': 55, 'MS': 52,
            'ID': 55, 'NM': 54, 'MT': 50, 'WV': 50,
        }
        fiber_score = METRO_FIBER_SCORES.get(state, 55)

        fiber_carriers = 0
        try:
            c.execute(\"\"\"
                SELECT COUNT(DISTINCT provider) FROM fiber_carrier_routes
                WHERE UPPER(states_served) LIKE %s OR UPPER(states_served) LIKE %s
            \"\"\", (f'%{state}%', f'%, {state}%'))
            fiber_carriers = c.fetchone()[0] or 0
            if fiber_carriers >= 5:
                fiber_score = min(100, fiber_score + 10)
            elif fiber_carriers >= 2:
                fiber_score = min(100, fiber_score + 5)
        except Exception:
            pass

        if nearby_facilities >= 20:
            fiber_score = min(100, fiber_score + 5)
        elif nearby_facilities >= 5:
            fiber_score = min(100, fiber_score + 3)

        # 6. State-level risk index
        STATE_RISK = {
            'FL': 35, 'TX': 42, 'CA': 38, 'LA': 32, 'OK': 40,
            'KS': 43, 'AL': 36, 'MS': 34, 'GA': 55, 'SC': 58,
            'NC': 60, 'VA': 72, 'MD': 70, 'PA': 75, 'OH': 74,
            'IN': 73, 'IL': 71, 'MO': 65, 'TN': 63, 'KY': 68,
            'WI': 76, 'MN': 74, 'IA': 72, 'NE': 68, 'CO': 70,
            'AZ': 78, 'NV': 80, 'UT': 75, 'ID': 72, 'OR': 68,
            'WA': 70, 'NY': 73, 'NJ': 71, 'CT': 74, 'MA': 75,
            'MI': 72, 'WV': 65, 'AR': 58, 'NM': 72, 'MT': 74,
        }
        risk_score = STATE_RISK.get(state, 65)

        # 7. Sub-scores
        power_score = min(100, 40 + (total_substations * 2) + (nearby_power_plants * 1.5))

        if nearby_gas_pipelines >= 20:
            gas_score = 95
        elif nearby_gas_pipelines >= 10:
            gas_score = 85
        elif nearby_gas_pipelines >= 3:
            gas_score = 70
        elif nearby_gas_pipelines >= 1:
            gas_score = 55
        else:
            gas_score = 30

        if nearby_facilities < 5:
            market_score = 60
        elif nearby_facilities < 20:
            market_score = 85
        elif nearby_facilities < 50:
            market_score = 75
        else:
            market_score = 60

        # 8. Overall composite: power 25%, gas 10%, fiber 15%, market 15%, risk 35%
        overall = round(
            (power_score * 0.25) +
            (gas_score * 0.10) +
            (fiber_score * 0.15) +
            (market_score * 0.15) +
            (risk_score * 0.35)
        , 1)

        return jsonify({
            'success': True,
            'location': {'lat': lat, 'lon': lon, 'state': state},
            'capacity_requested_mw': capacity,
            'overall_score': overall,
            'scores': {
                'power_infrastructure': round(power_score, 1),
                'gas_pipeline_access': round(gas_score, 1),
                'fiber_connectivity': round(fiber_score, 1),
                'market_conditions': round(market_score, 1),
                'risk_resilience': round(risk_score, 1),
            },
            'nearby': {
                'facilities_100km': nearby_facilities,
                'total_capacity_mw': round(nearby_mw, 1),
                'substations_50km': total_substations,
                'gas_pipelines_50km': nearby_gas_pipelines,
                'power_plants_80km': nearby_power_plants,
                'generation_capacity_mw': round(nearby_generation_mw, 1),
                'fiber_carriers_in_state': fiber_carriers,
            },
            'interpretation': (
                'Excellent site' if overall >= 80 else
                'Good site' if overall >= 70 else
                'Viable site' if overall >= 60 else
                'Challenging site'
            ),
            'source': 'DC Hub Site Intelligence',
            'upgrade_url': 'https://dchub.cloud/pricing',
        })

    except Exception as e:
        logger.error(f"site-score error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

"""


# =============================================================================
# PATCH 2 — MCP Teaser analyze_site (main.py)
# =============================================================================
PATCH2_START = """        if tool_name == 'analyze_site':
            # Keep: overall_score, location, interpretation
            # Strip: detailed sub-scores, nearby facilities, power data"""

PATCH2_END = """        elif tool_name == 'get_grid_data':"""

PATCH2_NEW = """        if tool_name == 'analyze_site':
            # Keep: overall_score, location, interpretation
            # Strip: detailed sub-scores, nearby facilities, power/gas/fiber data
            teaser = {
                'success': data.get('success', True),
                'location': data.get('location', {}),
                'overall_score': data.get('overall_score'),
                'interpretation': data.get('interpretation', ''),
                'capacity_requested_mw': data.get('capacity_requested_mw'),
                'scores': {
                    'power_infrastructure': '██ upgrade to see',
                    'gas_pipeline_access': '██ upgrade to see',
                    'fiber_connectivity': '██ upgrade to see',
                    'market_conditions': '██ upgrade to see',
                    'risk_resilience': '██ upgrade to see',
                },
                'nearby': {
                    'facilities_100km': '██',
                    'total_capacity_mw': '██',
                    'substations_50km': '██',
                    'gas_pipelines_50km': '██',
                    'power_plants_80km': '██',
                    'generation_capacity_mw': '██',
                    'fiber_carriers_in_state': '██',
                },
                '_upgrade': {
                    'tier': 'free_teaser',
                    'message': (
                        f"Site score: {data.get('overall_score', 'N/A')} — "
                        f"Developer plan ($49/mo) unlocks detailed power, gas pipeline, fiber, "
                        f"market, and risk sub-scores plus nearby infrastructure counts."
                    ),
                    'url': 'https://dchub.cloud/pricing#developer',
                    'checkout': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
                    'price': '$49/mo',
                }
            }
            return [{"type": "text", "text": json.dumps(teaser)}]

        elif tool_name == 'get_grid_data':"""


# =============================================================================
# PATCH 3 — require_plan bypass (energy_infrastructure_routes.py)
# =============================================================================
PATCH3_START = """def require_plan(min_plan='pro'):
    \"\"\"Lazy require_plan that validates plan at request time\"\"\"
    logger = logging.getLogger(__name__)
    
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                from api_tier_gating import validate_api_key, user_has_access
                
                # Check API key
                api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
                if not api_key:"""

PATCH3_END = """# =============================================================================
# CONFIGURATION
# ============================================================================="""

PATCH3_NEW = """def require_plan(min_plan='pro'):
    \"\"\"Lazy require_plan — validates plan at request time.
    Bypasses for X-Internal-Key (MCP proxy, scheduler, internal sync).
    \"\"\"
    logger = logging.getLogger(__name__)
    
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                # X-Internal-Key bypass (MCP proxy + scheduler calls)
                internal_key = request.headers.get('X-Internal-Key', '')
                if is_valid_internal_key(internal_key):
                    return f(*args, **kwargs)

                from api_tier_gating import validate_api_key, user_has_access
                
                # Check API key
                api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
                if not api_key:
                    auth_header = request.headers.get('Authorization', '')
                    if auth_header.startswith('Bearer '):
                        token = auth_header[7:]
                        if token.startswith('dchub_'):
                            api_key = token
                
                if not api_key:
                    return jsonify({
                        'success': False,
                        'error': 'authentication_required',
                        'message': f'This endpoint requires a {min_plan.title()} plan or higher.',
                        'signup_url': 'https://dchub.cloud/signup',
                    }), 401
                
                valid, info = validate_api_key(api_key)
                if not valid:
                    return jsonify({
                        'success': False,
                        'error': 'invalid_api_key',
                        'message': 'Invalid or inactive API key',
                    }), 401
                
                user_plan = info.get('plan', 'free')
                if not user_has_access(user_plan, min_plan):
                    return jsonify({
                        'success': False,
                        'error': 'plan_upgrade_required',
                        'message': f'This endpoint requires {min_plan.title()} plan. You are on {user_plan.title()}.',
                        'upgrade_url': 'https://dchub.cloud/pricing',
                    }), 403
                
                return f(*args, **kwargs)
            except ImportError:
                return f(*args, **kwargs)
            except Exception as e:
                logger.error(f"Tier gating error: {e}")
                return f(*args, **kwargs)
        return wrapper
    return decorator

# =============================================================================
# CONFIGURATION
# ============================================================================="""


# =============================================================================
# PATCH 4 — SubstationDiscovery bulk HIFLD (infrastructure_discovery.py)
# =============================================================================
PATCH4_START = """class SubstationDiscovery:
    \"\"\"Discover substations from HIFLD, OSM, and learned APIs\"\"\""""

PATCH4_END = """class GasPipelineDiscovery:"""

PATCH4_NEW = """class SubstationDiscovery:
    \"\"\"Discover substations from HIFLD (bulk), OSM, learned APIs, and infrastructure_layers\"\"\"

    OVERPASS_API = "https://overpass-api.de/api/interpreter"

    def __init__(self):
        self.new_substations = 0
        self._market_index = 0
        if not hasattr(SubstationDiscovery, '_hifld_fid_offset'):
            SubstationDiscovery._hifld_fid_offset = 0

    def sync(self):
        logger.info("⚡ Syncing substations...")
        self.new_substations = 0
        self._sync_hifld_substations_bulk()
        self._sync_osm_substations()
        self._sync_from_learned_apis()
        self._sync_infrastructure_layers_substations()
        logger.info(f"   ✅ Substations: {self.new_substations} new")
        return self.new_substations

    def _sync_hifld_substations_bulk(self):
        \"\"\"Bulk nationwide HIFLD substation pull with FID pagination.
        Pulls 5,000 per cycle. Full ~70K coverage in ~14 cycles.\"\"\"
        batch_size = 5000
        fid_start = SubstationDiscovery._hifld_fid_offset
        fid_end = fid_start + batch_size

        logger.info(f"   ⚡ HIFLD substations BULK: FID {fid_start}-{fid_end}...")
        before = self.new_substations

        try:
            features = _query_hifld_paginated(
                HIFLD_APIS['substations'],
                where=f'OBJECTID>{fid_start} AND OBJECTID<={fid_end}',
                max_total=batch_size,
                batch_size=1000
            )

            if not features:
                logger.info(f"   ⚡ HIFLD substations: reached end at FID {fid_start}, resetting to 0")
                SubstationDiscovery._hifld_fid_offset = 0
            else:
                SubstationDiscovery._hifld_fid_offset = fid_end

                for feat in features:
                    attrs = feat.get('attributes', {})
                    geom = feat.get('geometry', {})
                    name = attrs.get('NAME', attrs.get('SUBSTATION', 'Unknown'))
                    operator = attrs.get('OWNER', attrs.get('OPERATOR', attrs.get('UTILITY', '')))
                    voltage = attrs.get('MAX_VOLT', attrs.get('MIN_VOLT', 0)) or 0
                    if voltage and voltage > 1000:
                        voltage = voltage / 1000
                    capacity = attrs.get('MAX_LOAD', attrs.get('CAPACITY', 0)) or 0
                    obj_id = attrs.get('OBJECTID', attrs.get('ID', ''))
                    state_val = attrs.get('STATE', attrs.get('STUSPS', ''))
                    city_val = attrs.get('CITY', attrs.get('COUNTY', ''))

                    lat_val = geom.get('y', geom.get('lat', 0))
                    lng_val = geom.get('x', geom.get('lon', 0))
                    if not lat_val or not lng_val:
                        continue

                    sub = {
                        "name": str(name)[:200],
                        "operator": str(operator)[:100] if operator else 'Unknown',
                        "voltage_kv": voltage,
                        "capacity_mva": capacity,
                        "city": str(city_val)[:100] if city_val else '',
                        "state": str(state_val)[:10] if state_val else '',
                        "lat": lat_val,
                        "lng": lng_val,
                        "source_id": f"hifld_sub_{obj_id}"
                    }
                    self._save_substation(sub, source='hifld')

            logger.info(
                f"   ⚡ HIFLD substations BULK: {len(features)} fetched "
                f"(FID {fid_start}-{fid_end}), {self.new_substations - before} new"
            )
        except Exception as e:
            logger.warning(f"   ⚠️ HIFLD bulk substations failed: {e}")

    def _sync_osm_substations(self):
        \"\"\"OSM substation discovery — 6 markets per cycle\"\"\"
        start = self._market_index % len(DC_MARKETS)
        markets = []
        for i in range(6):
            idx = (start + i) % len(DC_MARKETS)
            markets.append(DC_MARKETS[idx])
        self._market_index = (start + 6) % len(DC_MARKETS)

        for market in markets:
            try:
                query = f\"\"\"
                [out:json][timeout:25];
                (
                  node["power"="substation"](around:80000,{market['lat']},{market['lng']});
                  way["power"="substation"](around:80000,{market['lat']},{market['lng']});
                  node["power"="plant"](around:80000,{market['lat']},{market['lng']});
                );
                out center 100;
                \"\"\"
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                if response.ok:
                    data = response.json()
                    for element in data.get('elements', []):
                        tags = element.get('tags', {})
                        lat = element.get('lat') or element.get('center', {}).get('lat')
                        lng = element.get('lon') or element.get('center', {}).get('lon')
                        if lat and lng:
                            sub = {
                                "name": tags.get('name', f"Substation near {market['name']}")[:200],
                                "operator": tags.get('operator', 'Unknown')[:100],
                                "voltage_kv": self._parse_voltage(tags.get('voltage', '')),
                                "city": market['name'],
                                "state": market['state'],
                                "lat": lat,
                                "lng": lng,
                                "source_id": f"osm_sub_{element.get('id', 0)}"
                            }
                            self._save_substation(sub, source='osm')
                    logger.info(f"   ⚡ OSM substations {market['name']}: {len(data.get('elements', []))} found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM substation sync failed for {market['name']}: {e}")

    def _sync_from_learned_apis(self):
        try:
            conn = get_db()
        try:
                cursor = conn.cursor()
                cursor.execute(\"\"\"
                    SELECT name, location, metadata FROM learned_infrastructure
                    WHERE category = 'power'
                    ORDER BY id DESC LIMIT 200
                \"\"\")
                rows = cursor.fetchall()
                for row in rows:
                    try:
                        if isinstance(row, dict):
                            name_val, location_val, meta_raw = row.get('name'), row.get('location'), row.get('metadata')
                        else:
                            name_val, location_val, meta_raw = row[0], row[1], row[2]
                        meta = json.loads(meta_raw) if meta_raw else {}
                        voltage = meta.get('MAX_VOLT', meta.get('VOLTAGE', meta.get('KV', 0))) or 0
                        if voltage and voltage > 1000:
                            voltage = voltage / 1000
                        sub = {
                            "name": str(name_val)[:200] if name_val else 'Unknown',
                            "operator": str(meta.get('OWNER', meta.get('OPERATOR', 'Discovered')))[:100],
                            "voltage_kv": voltage,
                            "capacity_mva": meta.get('CAPACITY', 0) or 0,
                            "city": location_val.split(',')[0].strip() if location_val else '',
                            "state": location_val.split(',')[-1].strip() if location_val and ',' in location_val else '',
                            "lat": meta.get('LATITUDE', meta.get('LAT', meta.get('Y'))),
                            "lng": meta.get('LONGITUDE', meta.get('LON', meta.get('X'))),
                            "source_id": f"learned_sub_{hash(str(name_val)) % 10**8}"
                        }
                        self._save_substation(sub, source='auto_discovery')
                    except Exception:
                        pass
                logger.warning(f"   ⚠️ Learned API substation sync failed: {e}")

            def _sync_infrastructure_layers_substations(self):
            \"\"\"Cross-populate substations table from infrastructure_layers KMZ records.\"\"\"
            try:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(\"\"\"
                    SELECT il.name, il.latitude, il.longitude, il.source, il.metadata,
                           COALESCE(il.state, '') as state, COALESCE(il.city, '') as city
                    FROM infrastructure_layers il
                    WHERE LOWER(il.layer_type) IN ('substation', 'electric_substation', 'substations', 'power')
                      AND il.latitude IS NOT NULL AND il.longitude IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM substations s 
                        WHERE s.source_id = 'infra_layer_' || CAST(il.id AS TEXT)
                      )
                    LIMIT 2000
                \"\"\")
                rows = cursor.fetchall()
                conn.close()

                for row in rows:
                    if isinstance(row, dict):
                        name_val = row.get('name', 'Substation')
                        lat_val = row.get('latitude')
                        lng_val = row.get('longitude')
                        source_val = row.get('source', 'kmz')
                        meta_raw = row.get('metadata')
                        state_val = row.get('state', '')
                        city_val = row.get('city', '')
                    else:
                        name_val, lat_val, lng_val, source_val, meta_raw, state_val, city_val = row[0], row[1], row[2], row[3], row[4], row[5], row[6]

                    if not lat_val or not lng_val:
                        continue

                    meta = {}
                    if meta_raw:
                        try:
                            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                        except Exception:
                            pass

                    voltage = meta.get('MAX_VOLT', meta.get('VOLTAGE', meta.get('voltage_kv', 0))) or 0
                    if voltage and voltage > 1000:
                        voltage = voltage / 1000

                    sub = {
                        "name": str(name_val)[:200] if name_val else 'Substation',
                        "operator": str(meta.get('OWNER', meta.get('operator', 'Unknown')))[:100],
                        "voltage_kv": voltage,
                        "capacity_mva": meta.get('CAPACITY', meta.get('capacity_mva', 0)) or 0,
                        "city": str(city_val)[:100],
                        "state": str(state_val)[:10],
                        "lat": lat_val,
                        "lng": lng_val,
                        "source_id": f"infra_layer_{hash(f'{name_val}_{lat_val}_{lng_val}') % 10**10}"
                    }
                    self._save_substation(sub, source='infrastructure_layers')

                if rows:
                    logger.info(f"   ⚡ Cross-populated {len(rows)} substations from infrastructure_layers")

            except Exception as e:
                logger.warning(f"   ⚠️ Infrastructure layers substation sync failed: {e}")

            def _parse_voltage(self, voltage_str):
            try:
                if not voltage_str:
                    return 0
                voltage_str = str(voltage_str).replace('kV', '').replace('V', '').strip()
                if ';' in voltage_str:
                    voltage_str = voltage_str.split(';')[0]
                voltage = float(voltage_str)
                if voltage > 1000:
                    voltage = voltage / 1000
                return voltage
            except:
                return 0

            def _save_substation(self, sub, source='discovery'):
            try:
                voltage = sub.get('voltage_kv', 0) or 0
                if 0 < voltage <= 69:
                    return
                source_id = sub.get('source_id', f"{sub['name']}_{sub.get('lat', 0):.4f}_{sub.get('lng', 0):.4f}".replace(" ", "_").lower()[:100])
                rowcount = _safe_write('''
                    INSERT INTO substations
                    (name, operator, voltage_kv, capacity_mva, city, state, lat, lng, source, source_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(source_id) DO NOTHING
                ''', (
                    sub['name'][:200],
                    sub.get('operator', '')[:100],
                    sub.get('voltage_kv', 0),
                    sub.get('capacity_mva', 0),
                    sub.get('city', '')[:100],
                    sub.get('state', ''),
                    sub.get('lat'),
                    sub.get('lng'),
                    source,
                    source_id[:100]
                ))
                if rowcount and rowcount > 0:
                    self.new_substations += 1
            except Exception as e:
                logger.warning(f"Error saving substation: {e}")


        finally:
            conn.close()
class GasPipelineDiscovery:"""


# =============================================================================
# RUN ALL PATCHES
# =============================================================================
if __name__ == '__main__':
    print("=" * 60)
    print("DC Hub Infrastructure Patches — Auto-Apply")
    print("=" * 60)
    
    results = []
    
    # Patch 1: site-score
    print("\n📍 Patch 1: Site Score multi-table proximity (main.py)")
    r1 = apply_patch('main.py', PATCH1_START, PATCH1_END, PATCH1_NEW, "site-score")
    results.append(r1)
    
    # Patch 2: MCP teaser
    print("\n📍 Patch 2: MCP teaser analyze_site new fields (main.py)")
    r2 = apply_patch('main.py', PATCH2_START, PATCH2_END, PATCH2_NEW, "mcp-teaser")
    results.append(r2)
    
    # Patch 3: require_plan bypass
    print("\n📍 Patch 3: require_plan X-Internal-Key bypass (energy_infrastructure_routes.py)")
    r3 = apply_patch('energy_infrastructure_routes.py', PATCH3_START, PATCH3_END, PATCH3_NEW, "require-plan")
    results.append(r3)
    
    # Patch 4: SubstationDiscovery bulk
    print("\n📍 Patch 4: SubstationDiscovery bulk HIFLD pull (infrastructure_discovery.py)")
    r4 = apply_patch('infrastructure_discovery.py', PATCH4_START, PATCH4_END, PATCH4_NEW, "substation-bulk")
    results.append(r4)
    
    # Summary
    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r)
    failed = sum(1 for r in results if not r)
    print(f"✅ {passed}/4 patches applied successfully")
    if failed:
        print(f"❌ {failed}/4 patches failed — check markers above")
        print("   Failed patches may need manual apply (marker text may differ slightly)")
    
    if passed > 0:
        print(f"\nNext steps:")
        print(f"  git add -A")
        print(f'  git commit -m "Infrastructure data layer: multi-table site score, MCP bypass, bulk HIFLD"')
        print(f"  git push origin main")
    
    print("=" * 60)
