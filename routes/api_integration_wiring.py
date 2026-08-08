"""
DC Hub API Integration Wiring - March 25, 2026 (v2 — enrichment expansion)
"""
import logging
import math
logger = logging.getLogger("dchub.api_integration")

def _get_db():
    from main import get_pg_connection
    return get_pg_connection()

def _return_db(conn, error=False):
    try:
        from main import return_pg_connection
        return_pg_connection(conn, error=error)
    except:
        try: conn.close()
        except: pass

STATE_ABBR_TO_NAME = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri",
    "MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey",
    "NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio",
    "OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina",
    "SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont",
    "VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
    "DC":"District of Columbia",
}
NAME_TO_STATE_ABBR = {v: k for k, v in STATE_ABBR_TO_NAME.items()}

def _safe_json(row):
    result = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"): result[k] = v.isoformat()
        elif isinstance(v, (int, float, str, bool, type(None))): result[k] = v
        else: result[k] = str(v)
    return result

STATE_TO_EGRID = {
    "AZ":"AZNM","NM":"AZNM","CA":"CAMX","TX":"ERCT","FL":"FRCC","HI":"HIOA",
    "NY":"NYCW","NJ":"NYCW","IL":"RFCM","IN":"RFCM","OH":"RFCW","MI":"RFCM",
    "VA":"SRVC","NC":"SRVC","SC":"SRSO","GA":"SRSO","AL":"SRSO","LA":"SRSO",
    "MS":"SRSO","OR":"NWPP","WA":"NWPP","ID":"NWPP","NV":"NWPP","UT":"NWPP",
    "MT":"NWPP","CO":"RMPA","WY":"RMPA","PA":"RFCE","MD":"RFCE","DE":"RFCE",
    "MN":"MROW","WI":"MROW","IA":"MROW","MO":"SRMW","KS":"SRMW",
    "CT":"NEWE","MA":"NEWE","ME":"NEWE","NH":"NEWE","VT":"NEWE","RI":"NEWE",
    "TN":"SRTV","KY":"SRTV","AR":"SRMV","OK":"SPSO","NE":"SPNO","SD":"SPNO","ND":"SPNO",
}
STATE_TO_BA = {
    "AZ":"SRP","TX":"ERCO","VA":"PJM","NC":"DUKE","GA":"SOCO",
    "IL":"PJM","OH":"PJM","PA":"PJM","NJ":"PJM","MD":"PJM",
    "IN":"MISO","WI":"MISO","MN":"MISO","IA":"MISO","MO":"MISO",
    "NY":"NYIS","CA":"CISO","OR":"BPAT","WA":"BPAT",
    "CO":"PSCO","NV":"NEVP","TN":"TVA","AL":"SOCO","FL":"FPL",
    "LA":"MISO","OK":"SWPP","KS":"SWPP","NE":"SWPP",
    "CT":"ISNE","MA":"ISNE","ME":"ISNE","NH":"ISNE",
}
# EIA energy source codes → human names
EIA_FUEL_CODES = {
    "NG": "Natural Gas", "SUB": "Subbituminous Coal", "BIT": "Bituminous Coal",
    "WAT": "Hydroelectric", "SUN": "Solar", "WND": "Wind", "NUC": "Nuclear",
    "DFO": "Distillate Fuel Oil", "RFO": "Residual Fuel Oil", "PC": "Petroleum Coke",
    "WDS": "Wood/Wood Waste", "LFG": "Landfill Gas", "OBG": "Other Biomass Gas",
    "GEO": "Geothermal", "MWH": "Batteries/Storage", "WH": "Waste Heat",
    "BLQ": "Black Liquor", "AB": "Agricultural Byproducts", "MSW": "Municipal Solid Waste",
    "OG": "Other Gas", "KER": "Kerosene", "JF": "Jet Fuel", "PUR": "Purchased Steam",
    "TDF": "Tire-Derived Fuel", "OBS": "Other Biomass Solids", "LIG": "Lignite Coal",
    "ANT": "Anthracite Coal", "SGC": "Coal-Derived Syngas", "BFG": "Blast Furnace Gas",
    "SC": "Coal Synfuel", "OTH": "Other", "WC": "Waste Coal",
}


def enrich_site_analysis(lat=None, lng=None, state=None):
    enrichment = {}
    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()
        # Carbon (epa_egrid)
        if state:
            mapped = STATE_TO_EGRID.get(state.upper())
            if mapped:
                cur.execute("SELECT * FROM epa_egrid WHERE subregion_code = %s", (mapped,))
                row = cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    enrichment["carbon"] = _safe_json(dict(zip(cols, row)))
        # Climate (nasa_power_climate)
        if lat and lng:
            cur.execute("SELECT *, (ABS(latitude - %s) + ABS(longitude - %s)) as dist FROM nasa_power_climate ORDER BY dist ASC LIMIT 1", (lat, lng))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                d = dict(zip(cols, row))
                d.pop("dist", None)
                enrichment["climate"] = _safe_json(d)
        # Risk (fema_risk_index)
        if state:
            cur.execute("SELECT * FROM fema_risk_index WHERE UPPER(state) = %s ORDER BY risk_score DESC LIMIT 3", (state.upper(),))
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                enrichment["risk"] = {"state": state.upper(), "top_risk_counties": [_safe_json(dict(zip(cols, r))) for r in rows]}
        # Water stress (usgs_water_stress) — distance-bounded, see
        # _water_point_sql: an unbounded nearest-row lookup returns Arizona
        # for a site in Frankfurt.
        if lat and lng:
            _wsql, _wparams = _water_point_sql(lat, lng, WATER_RADIUS_DEFAULT_KM, 5)
            cur.execute(_wsql, _wparams)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            recs = [_water_row(dict(zip(cols, r))) for r in rows]
            enrichment["water_stress"] = {
                "nearest_sites": recs,
                "radius_km": WATER_RADIUS_DEFAULT_KM,
                "measurement": WATER_MEASUREMENT,
                "is_modelled_index": False,
                "measurement_note": WATER_MEASUREMENT_NOTE,
                "limitation": None if recs else (
                    "No USGS monitoring station within %.0f km. %s"
                    % (WATER_RADIUS_DEFAULT_KM, WATER_COVERAGE_NOTE)),
            }
        # Energy rates (eia_retail_rates)
        if state:
            state_full = STATE_ABBR_TO_NAME.get(state.upper(), state)
            cur.execute("SELECT * FROM eia_retail_rates WHERE state = %s AND LOWER(sector) = 'industrial' ORDER BY period DESC LIMIT 3", (state_full,))
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                enrichment["energy_rates"] = {"source": "EIA", "state": state.upper(), "rates": [_safe_json(dict(zip(cols, r))) for r in rows]}

        # ═══ NEW v2 (Mar 25) ═══

        # Nearby Generation (eia_generators — 200K, spatial)
        if lat and lng:
            delta = 0.45  # ~50km
            cur.execute("""
                SELECT energy_source_desc, operating_status,
                       COUNT(*) as generator_count,
                       ROUND(CAST(SUM(nameplate_capacity_mw) AS numeric), 1) as total_capacity_mw,
                       ROUND(CAST(SUM(net_summer_capacity_mw) AS numeric), 1) as total_summer_mw,
                       ROUND(CAST(AVG(operating_year) AS numeric), 0) as avg_vintage
                FROM eia_generators
                WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                  AND nameplate_capacity_mw > 0
                GROUP BY energy_source_desc, operating_status
                ORDER BY total_capacity_mw DESC
            """, (lat - delta, lat + delta, lng - delta, lng + delta))
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                by_fuel = {}
                total_mw = 0
                total_gen = 0
                for r in rows:
                    d = dict(zip(cols, r))
                    fuel = d.get("energy_source_desc") or "Unknown"
                    cap = float(d.get("total_capacity_mw") or 0)
                    cnt = int(d.get("generator_count") or 0)
                    if fuel not in by_fuel:
                        by_fuel[fuel] = {"capacity_mw": 0, "generators": 0}
                    by_fuel[fuel]["capacity_mw"] += cap
                    by_fuel[fuel]["generators"] += cnt
                    total_mw += cap
                    total_gen += cnt
                sorted_fuels = sorted(by_fuel.items(), key=lambda x: x[1]["capacity_mw"], reverse=True)
                fuel_mix = []
                for fuel, data in sorted_fuels:
                    pct = round(data["capacity_mw"] / total_mw * 100, 1) if total_mw > 0 else 0
                    fuel_mix.append({"fuel_type": fuel, "capacity_mw": round(data["capacity_mw"], 1), "share_pct": pct, "generator_count": data["generators"]})
                enrichment["nearby_generation"] = {"source": "EIA-860", "radius_km": 50, "total_capacity_mw": round(total_mw, 1), "total_generators": total_gen, "fuel_mix": fuel_mix[:10]}
            # Top plants nearby (>50MW)
            cur.execute("""
                SELECT DISTINCT ON (plant_id) plant_name, plant_id, state,
                       energy_source_desc, nameplate_capacity_mw, operating_status, operating_year,
                       latitude, longitude
                FROM eia_generators
                WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                  AND nameplate_capacity_mw > 50
                ORDER BY plant_id, nameplate_capacity_mw DESC
            """, (lat - delta, lat + delta, lng - delta, lng + delta))
            plant_rows = cur.fetchall()
            if plant_rows:
                cols = [d[0] for d in cur.description]
                plants = []
                for r in plant_rows:
                    d = dict(zip(cols, r))
                    plat = float(d.get("latitude") or 0)
                    plng = float(d.get("longitude") or 0)
                    dist_km = round(math.sqrt((plat - lat)**2 + (plng - lng)**2) * 111, 1)
                    plants.append({"name": d.get("plant_name"), "plant_id": d.get("plant_id"), "fuel": d.get("energy_source_desc"), "capacity_mw": float(d.get("nameplate_capacity_mw") or 0), "status": d.get("operating_status"), "vintage": d.get("operating_year"), "distance_km": dist_km})
                plants.sort(key=lambda x: x["capacity_mw"], reverse=True)
                if "nearby_generation" not in enrichment:
                    enrichment["nearby_generation"] = {"source": "EIA-860", "radius_km": 50}
                enrichment["nearby_generation"]["largest_plants"] = plants[:10]

        # Fallback: state-level generation summary (deduplicated by plant_id + energy_source)
        if "nearby_generation" not in enrichment and state:
            cur.execute("""
                SELECT energy_source,
                       COUNT(DISTINCT plant_id) as plant_count,
                       ROUND(CAST(SUM(cap) AS numeric), 1) as total_capacity_mw
                FROM (
                    SELECT DISTINCT ON (plant_id, energy_source, nameplate_capacity_mw)
                           plant_id, energy_source, nameplate_capacity_mw as cap
                    FROM eia_generators
                    WHERE UPPER(state) = %s AND nameplate_capacity_mw > 0
                ) deduped
                GROUP BY energy_source
                ORDER BY total_capacity_mw DESC
            """, (state.upper(),))
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                total_mw = sum(float(dict(zip(cols, r)).get("total_capacity_mw") or 0) for r in rows)
                total_plants = sum(int(dict(zip(cols, r)).get("plant_count") or 0) for r in rows)
                fuel_mix = []
                for r in rows:
                    d = dict(zip(cols, r))
                    cap = float(d.get("total_capacity_mw") or 0)
                    code = d.get("energy_source") or "UNK"
                    pct = round(cap / total_mw * 100, 1) if total_mw > 0 else 0
                    fuel_mix.append({"fuel_type": EIA_FUEL_CODES.get(code, code), "fuel_code": code, "capacity_mw": cap, "share_pct": pct, "plant_count": int(d.get("plant_count") or 0)})
                enrichment["nearby_generation"] = {"source": "EIA-860 (state-level, deduplicated)", "scope": "state", "state": state.upper(), "total_capacity_mw": round(total_mw, 1), "total_plants": total_plants, "fuel_mix": fuel_mix[:10]}

        # Gas Infrastructure (eia_gas_consumption + eia_gas_storage)
        if state:
            gas_data = {}
            state_upper = state.upper()
            # eia_gas_consumption uses state_code (abbrev) and state_name (full)
            state_full = STATE_ABBR_TO_NAME.get(state_upper, state_upper)
            cur.execute("SELECT sector, value, units, period FROM eia_gas_consumption WHERE UPPER(state_code) = %s OR UPPER(state_name) = %s ORDER BY period DESC LIMIT 10", (state_upper, state_full.upper()))
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                gas_data["consumption"] = {"source": "EIA Natural Gas", "state": state_upper, "records": [_safe_json(dict(zip(cols, r))) for r in rows]}
            gas_region_map = {"TX":"TX","LA":"LA","OK":"OK","KS":"KS","NM":"NM","PA":"East","NY":"East","NJ":"East","OH":"East","WV":"East","IL":"Midwest","IN":"Midwest","MI":"Midwest","MN":"Midwest","CA":"Pacific","OR":"Pacific","WA":"Pacific","CO":"Mountain","WY":"Mountain","UT":"Mountain","MT":"Mountain","AZ":"Mountain","NV":"Mountain","AL":"South Central","MS":"South Central","AR":"South Central"}
            region = gas_region_map.get(state_upper)
            if region:
                cur.execute("SELECT process_name, series_desc, value, units, period FROM eia_gas_storage WHERE LOWER(region) LIKE %s ORDER BY period DESC LIMIT 10", (f"%{region.lower()}%",))
                rows = cur.fetchall()
                if rows:
                    cols = [d[0] for d in cur.description]
                    gas_data["storage"] = {"source": "EIA Natural Gas Storage", "region": region, "records": [_safe_json(dict(zip(cols, r))) for r in rows]}
            if gas_data:
                enrichment["gas_infrastructure"] = gas_data

        # Grid Generation by RTO (eia_rto_hourly)
        if state:
            ba = STATE_TO_BA.get(state.upper())
            if ba:
                cur.execute("""
                    SELECT respondent_name, fueltype, type_name,
                           ROUND(CAST(AVG(value) AS numeric), 1) as avg_mwh,
                           ROUND(CAST(MAX(value) AS numeric), 1) as peak_mwh,
                           COUNT(*) as data_points
                    FROM eia_rto_hourly WHERE UPPER(respondent) = %s
                    GROUP BY respondent_name, fueltype, type_name ORDER BY avg_mwh DESC
                """, (ba,))
                rows = cur.fetchall()
                if rows:
                    cols = [d[0] for d in cur.description]
                    enrichment["grid_generation"] = {"source": "EIA Hourly Grid Monitor", "balancing_authority": ba, "fuel_breakdown": [_safe_json(dict(zip(cols, r))) for r in rows]}

        # Internet Exchanges (peeringdb_ix)
        if state:
            state_city_map = {"AZ":["Phoenix","Scottsdale","Mesa"],"VA":["Ashburn","Reston","McLean","Sterling","Richmond"],"TX":["Dallas","Houston","Austin","San Antonio","Fort Worth"],"GA":["Atlanta"],"IL":["Chicago"],"NY":["New York","Manhattan","Brooklyn"],"NJ":["Newark","Secaucus","Jersey City"],"CA":["Los Angeles","San Jose","San Francisco","Sacramento","San Diego"],"WA":["Seattle","Tacoma"],"OR":["Portland","Hillsboro"],"FL":["Miami","Jacksonville","Tampa","Orlando"],"TN":["Nashville","Memphis","Knoxville"],"NC":["Charlotte","Raleigh","Durham"],"PA":["Philadelphia","Pittsburgh"],"OH":["Columbus","Cleveland","Cincinnati"],"CO":["Denver"],"NV":["Las Vegas","Reno"],"MN":["Minneapolis"],"MO":["Kansas City","St. Louis"]}
            cities = state_city_map.get(state.upper(), [])
            if cities:
                placeholders = ",".join(["%s"] * len(cities))
                cur.execute(f"SELECT name, city, country, participants, website FROM peeringdb_ix WHERE city IN ({placeholders}) ORDER BY participants DESC", cities)
                rows = cur.fetchall()
                if rows:
                    cols = [d[0] for d in cur.description]
                    ix_list = [_safe_json(dict(zip(cols, r))) for r in rows]
                    total_p = sum(ix.get("participants", 0) for ix in ix_list)
                    enrichment["internet_exchanges"] = {"source": "PeeringDB", "ix_count": len(ix_list), "total_participants": total_p, "exchanges": ix_list[:15]}

        cur.close()
        _return_db(conn)
    except Exception as e:
        logger.error(f"Site enrichment error: {e}")
        enrichment["_error"] = str(e)
        if conn: _return_db(conn, error=True)
    return enrichment

def get_eia_rates_from_neon(state=None):
    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()
        if state:
            state_full = STATE_ABBR_TO_NAME.get(state.upper(), state)
            cur.execute("SELECT * FROM eia_retail_rates WHERE state = %s ORDER BY period DESC", (state_full,))
        else:
            cur.execute("SELECT * FROM eia_retail_rates ORDER BY state, period DESC")
        cols = [d[0] for d in cur.description]
        rows = [_safe_json(dict(zip(cols, r))) for r in cur.fetchall()]
        cur.close()
        _return_db(conn)
        return rows
    except Exception as e:
        logger.error(f"EIA Neon fallback error: {e}")
        if conn: _return_db(conn, error=True)
        return []

def _site_risk_gate(rows, gate_id):
    """r43-H (2026-05-28): the site-risk HTTP endpoints (carbon/climate/risk/
    water) returned full datasets to ANY anonymous caller — bypassing the MCP
    gating where get_water_risk etc. are already Tier.IDENTIFIED. Gate them to
    IDENTIFIED to match: a keyed or logged-in caller (X-API-Key / dchub_token /
    Authorization Bearer — what site-planner-panel.js sends) gets full data;
    anonymous gets a 3-row teaser + sign-up CTA. Returns a gate Response when
    the caller is below IDENTIFIED, else None. Fails OPEN on any helper error so
    a gating glitch can never 500 the endpoint."""
    try:
        from routes.tier_gate import _resolve_caller_tier, _gate_response
        tier, _ = _resolve_caller_tier()
        if str(tier).upper() not in ("FREE", "ANON", "ANONYMOUS", ""):
            return None  # IDENTIFIED+ (free dev key, logged-in, paid) → full data
        sample = rows[:3] if isinstance(rows, list) else []
        total = len(rows) if isinstance(rows, list) else 0
        return _gate_response(
            str(tier).upper() or "FREE", "IDENTIFIED", gate_id,
            preview={"sample": sample, "total_available": total,
                     "note": "Free sign-up (email only, no card) unlocks the full dataset."})
    except Exception:
        return None  # never break the endpoint over a gate error


def _register_carbon_route(app):
    @app.route("/api/v1/carbon", methods=["GET"])
    def api_carbon_intensity():
        conn = None
        try:
            from flask import request, jsonify
            state = request.args.get("state","").upper().strip()
            subregion = request.args.get("subregion","").upper().strip()
            conn = _get_db()
            cur = conn.cursor()
            if state:
                mapped = STATE_TO_EGRID.get(state)
                if not mapped:
                    return jsonify({"error":f"No eGRID mapping for state {state}","available_states":sorted(STATE_TO_EGRID.keys())}), 404
                cur.execute("SELECT * FROM epa_egrid WHERE subregion_code = %s", (mapped,))
            elif subregion:
                cur.execute("SELECT * FROM epa_egrid WHERE subregion_code = %s", (subregion,))
            else:
                cur.execute("SELECT * FROM epa_egrid ORDER BY subregion_code")
            cols = [d[0] for d in cur.description]
            rows = [_safe_json(dict(zip(cols, r))) for r in cur.fetchall()]
            cur.close()
            _return_db(conn)
            _g = _site_risk_gate(rows, "site_risk_carbon")
            if _g is not None:
                return _g
            return jsonify({"source":"EPA eGRID","count":len(rows),"data":rows})
        except Exception as e:
            logger.error(f"Carbon route error: {e}")
            if conn: _return_db(conn, error=True)
            return jsonify({"error":str(e)}), 500

def _register_climate_route(app):
    @app.route("/api/v1/climate", methods=["GET"])
    def api_climate_data():
        conn = None
        try:
            from flask import request, jsonify
            market = request.args.get("market","").strip()
            lat = request.args.get("lat", type=float)
            lng = request.args.get("lng", type=float)
            conn = _get_db()
            cur = conn.cursor()
            if market:
                cur.execute("SELECT * FROM nasa_power_climate WHERE LOWER(location_name) LIKE %s", (f"%{market.lower()}%",))
            elif lat is not None and lng is not None:
                cur.execute("SELECT *, (ABS(latitude - %s) + ABS(longitude - %s)) as dist FROM nasa_power_climate ORDER BY dist ASC LIMIT 3", (lat, lng))
            else:
                cur.execute("SELECT * FROM nasa_power_climate ORDER BY location_name")
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d.pop("dist", None)
                rows.append(_safe_json(d))
            cur.close()
            _return_db(conn)
            _g = _site_risk_gate(rows, "site_risk_climate")
            if _g is not None:
                return _g
            return jsonify({"source":"NASA POWER","count":len(rows),"data":rows})
        except Exception as e:
            logger.error(f"Climate route error: {e}")
            if conn: _return_db(conn, error=True)
            return jsonify({"error":str(e)}), 500

def _register_risk_route(app):
    @app.route("/api/v1/risk", methods=["GET"])
    def api_risk_index():
        conn = None
        try:
            from flask import request, jsonify
            state = request.args.get("state","").upper().strip()
            county = request.args.get("county","").strip()
            top_n = request.args.get("top", type=int)
            conn = _get_db()
            cur = conn.cursor()
            if state and county:
                cur.execute("SELECT * FROM fema_risk_index WHERE UPPER(state) = %s AND LOWER(county) LIKE %s", (state, f"%{county.lower()}%"))
            elif state:
                cur.execute("SELECT * FROM fema_risk_index WHERE UPPER(state) = %s ORDER BY risk_score DESC LIMIT %s", (state, top_n or 10))
            else:
                cur.execute("SELECT * FROM fema_risk_index ORDER BY risk_score DESC LIMIT %s", (top_n or 25,))
            cols = [d[0] for d in cur.description]
            rows = [_safe_json(dict(zip(cols, r))) for r in cur.fetchall()]
            cur.close()
            _return_db(conn)
            _g = _site_risk_gate(rows, "site_risk_fema")
            if _g is not None:
                return _g
            return jsonify({"source":"FEMA NRI","count":len(rows),"data":rows})
        except Exception as e:
            logger.error(f"Risk route error: {e}")
            if conn: _return_db(conn, error=True)
            return jsonify({"error":str(e)}), 500

# ═══════════════════════════════════════════════════════════════════════
# /api/v1/water/stress — location filtering
#
# 2026-08-07: the endpoint accepted lat/lon and threw them away.
#
#   1. The route read `lng`. `server.mjs` (get_water_risk) sends `lon`. So
#      EVERY MCP water call fell through to the unfiltered branch
#      `ORDER BY` `state LIMIT 50`, which returns Arizona first for every
#      point on earth. Ashburn VA got 34 AZ + 16 CA wells, nearest 3,058 km.
#   2. Even on the `lng` path the "distance" was Manhattan distance in raw
#      degrees, unbounded — Frankfurt (no USGS coverage at all) got the
#      arg-min of that, ten New Jersey wells ~6,400 km away, presented with
#      no distance and no caveat.
#
# Fixes, in the order the brief asked for them:
#   1. Real great-circle (haversine) distance in SQL, bounding-box prefiltered.
#   2. Hard radius bound (default 150 km). Outside it: empty + stated reason.
#      NEVER the nearest row regardless of distance.
#   3. Every row carries distance_km, reading_date and reading_age_days; the
#      envelope states plainly that this is observed station data, not a
#      modelled water-stress index.
#   4. Latitude accepted as lat|latitude, longitude as lng|lon|long|longitude,
#      and the envelope echoes which name it actually used.
#
# Anything that cannot be answered for the requested point returns count: 0
# with a `limitation` string. It never falls back to rows from somewhere else.
# ═══════════════════════════════════════════════════════════════════════

WATER_LAT_PARAMS = ("lat", "latitude")
WATER_LON_PARAMS = ("lng", "lon", "long", "longitude")

WATER_RADIUS_DEFAULT_KM = 150.0
WATER_RADIUS_MAX_KM = 500.0
WATER_LIMIT_DEFAULT = 25
WATER_LIMIT_MAX = 200
WATER_STATE_LIMIT_DEFAULT = 500
WATER_STATE_LIMIT_MAX = 2000

WATER_MEASUREMENT = "observed_station_readings"
WATER_SOURCE_DETAIL = (
    "USGS NWIS monitoring stations — individual gauged wells and surface-water "
    "sites, one row per station with its most recent ingested reading."
)
WATER_MEASUREMENT_NOTE = (
    "These are OBSERVED readings from individual USGS monitoring stations. "
    "This is NOT a modelled water-stress index, NOT a basin-level withdrawal "
    "or supply figure, and NOT a scarcity score. A station near a site tells "
    "you the local water level on the reading date; it does not by itself "
    "characterise water availability for a data centre. distance_km is the "
    "great-circle distance from the query point to that station."
)
WATER_COVERAGE_NOTE = (
    "Coverage is the United States only (USGS NWIS), and is sparse even there "
    "— it is a monitoring network, not a grid. A location outside the US, or "
    "in an unmonitored part of the US, correctly returns zero rows."
)

# Great-circle distance in km from (%(qlat)s, %(qlon)s) to the row's
# latitude/longitude. Casts guard against numeric/float4 columns.
_WATER_HAVERSINE_SQL = """(6371.0088 * 2 * asin(sqrt(
        power(sin(radians(CAST(latitude AS double precision) - %(qlat)s) / 2), 2)
        + cos(radians(%(qlat)s)) * cos(radians(CAST(latitude AS double precision)))
        * power(sin(radians(CAST(longitude AS double precision) - %(qlon)s) / 2), 2)
    )))"""


def _water_float_arg(args, names):
    """First present name in `names` parsed as float.

    Returns (value, name_used, error). `error` is set when a name IS present
    but unparseable — that must not be silently treated as "not supplied",
    which is how the original bug hid.
    """
    for n in names:
        try:
            raw = args.get(n)
        except Exception:
            raw = None
        if raw is None:
            continue
        raw = str(raw).strip()
        if raw == "":
            continue
        try:
            return float(raw), n, None
        except (TypeError, ValueError):
            return None, n, "%s=%r is not a number" % (n, raw)
    return None, None, None


def _water_bbox_deltas(lat, radius_km):
    """(dlat, dlon) degrees covering `radius_km` around `lat`.

    Longitude degrees shrink with latitude, so dlon is widened by 1/cos(lat).
    cos() is taken at the POLEWARD edge of the band (lat ± dlat), not at the
    centre: a box sized on the centre latitude is too narrow at its northern
    edge and would drop real stations before the exact distance test ever
    sees them. Near the poles cos() collapses, so fall back to the whole
    range and let the exact haversine predicate do the work.
    """
    dlat = radius_km / 110.574
    edge_lat = min(abs(lat) + dlat, 90.0)
    coslat = math.cos(math.radians(edge_lat))
    if abs(coslat) < 1e-6:
        return dlat, 180.0
    dlon = radius_km / (111.320 * abs(coslat))
    return dlat, min(dlon, 180.0)


def _water_point_sql(lat, lon, radius_km, limit):
    """(sql, params) for 'stations within radius_km of (lat, lon)'.

    Pure — no DB — so the distance predicate and the radius bound can be
    asserted in tests without a database.
    """
    dlat, dlon = _water_bbox_deltas(lat, radius_km)
    params = {
        "qlat": float(lat), "qlon": float(lon),
        "lat_min": lat - dlat, "lat_max": lat + dlat,
        "radius": float(radius_km), "lim": int(limit),
    }
    lon_clause = ""
    if lon - dlon >= -180.0 and lon + dlon <= 180.0:
        # Skipped near the antimeridian (Alaska/Aleutians) where the box wraps;
        # the exact distance predicate below still bounds the result.
        lon_clause = " AND CAST(longitude AS double precision) BETWEEN %(lon_min)s AND %(lon_max)s"
        params["lon_min"] = lon - dlon
        params["lon_max"] = lon + dlon
    sql = (
        "SELECT * FROM ("
        " SELECT s.*, " + _WATER_HAVERSINE_SQL + " AS distance_km"
        " FROM usgs_water_stress s"
        " WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
        "   AND CAST(latitude AS double precision) BETWEEN %(lat_min)s AND %(lat_max)s"
        + lon_clause +
        ") q"
        " WHERE distance_km <= %(radius)s"
        " ORDER BY distance_km ASC"
        " LIMIT %(lim)s"
    )
    return sql, params


def _water_age_days(value):
    """Age in days of a reading date. None when undated/unparseable."""
    from datetime import date, datetime
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    elif isinstance(value, str):
        try:
            value = datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    if not isinstance(value, date):
        return None
    try:
        return (date.today() - value).days
    except Exception:
        return None


def _water_row(raw):
    """One station row, labelled: distance_km + reading date and its age."""
    dist = raw.pop("distance_km", None)
    raw.pop("dist", None)
    age = _water_age_days(raw.get("water_level_date"))
    out = _safe_json(raw)
    if dist is not None:
        try:
            out["distance_km"] = round(float(dist), 2)
        except (TypeError, ValueError):
            out["distance_km"] = None
    else:
        out["distance_km"] = None  # no query point: say so, never omit it
    out["reading_date"] = out.get("water_level_date")
    out["reading_age_days"] = age
    out["measurement"] = WATER_MEASUREMENT
    return out


def _water_envelope(rows, query, limitation=None, truncated=False):
    """Response envelope. Always states what the numbers are and how old."""
    ages = [r.get("reading_age_days") for r in rows if r.get("reading_age_days") is not None]
    dates = sorted([r.get("reading_date") for r in rows if r.get("reading_date")])
    return {
        "source": "USGS",
        "source_detail": WATER_SOURCE_DETAIL,
        "measurement": WATER_MEASUREMENT,
        "is_modelled_index": False,
        "measurement_note": WATER_MEASUREMENT_NOTE,
        "coverage_note": WATER_COVERAGE_NOTE,
        "count": len(rows),
        "truncated": bool(truncated),
        "query": query,
        "reading_age": {
            "newest_reading_date": dates[-1] if dates else None,
            "oldest_reading_date": dates[0] if dates else None,
            "min_age_days": min(ages) if ages else None,
            "max_age_days": max(ages) if ages else None,
        },
        "limitation": limitation,
        "data": rows,
    }


def _register_water_route(app):
    @app.route("/api/v1/water/stress", methods=["GET"])
    def api_water_stress():
        conn = None
        try:
            from flask import request, jsonify

            state = request.args.get("state", "").upper().strip()
            lat, lat_param, lat_err = _water_float_arg(request.args, WATER_LAT_PARAMS)
            lon, lon_param, lon_err = _water_float_arg(request.args, WATER_LON_PARAMS)

            radius_km, _, _radius_err = _water_float_arg(request.args, ("radius_km", "radius"))
            if radius_km is None:
                radius_km = WATER_RADIUS_DEFAULT_KM
            radius_km = max(1.0, min(float(radius_km), WATER_RADIUS_MAX_KM))

            has_point = lat is not None and lon is not None
            query = {
                "lat": lat,
                "lon": lon,
                "state": state or None,
                "radius_km": radius_km if has_point else None,
                "latitude_param_used": lat_param,
                "longitude_param_used": lon_param,
                "accepted_latitude_params": list(WATER_LAT_PARAMS),
                "accepted_longitude_params": list(WATER_LON_PARAMS),
            }

            # ── Geography validation. Every failure returns zero rows with a
            #    reason. None of these paths may fall back to other rows.
            reason = None
            if lat_err or lon_err:
                reason = "Could not read the query point: %s. No rows returned." % (
                    "; ".join([e for e in (lat_err, lon_err) if e]))
            elif (lat is None) != (lon is None):
                reason = (
                    "Both a latitude (%s) and a longitude (%s) are required to "
                    "filter by location; only one was supplied, so no location "
                    "filter could be applied and no rows are returned."
                    % ("|".join(WATER_LAT_PARAMS), "|".join(WATER_LON_PARAMS)))
            elif has_point and not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                reason = "Query point (%s, %s) is out of range. No rows returned." % (lat, lon)
            elif not has_point and not state:
                reason = (
                    "No location supplied. Pass a point (%s and %s, optional "
                    "radius_km, default %d) or state=<US 2-letter code>. This "
                    "endpoint does not return an unfiltered sample, because a "
                    "station list unrelated to your site is not an answer."
                    % ("|".join(WATER_LAT_PARAMS), "|".join(WATER_LON_PARAMS),
                       int(WATER_RADIUS_DEFAULT_KM)))
            if reason:
                return jsonify(_water_envelope([], query, reason))

            conn = _get_db()
            cur = conn.cursor()
            try:
                cur.execute("SET LOCAL statement_timeout = 8000")
            except Exception:
                # An aborted transaction poisons every query after it, so roll
                # back rather than swallowing and 500-ing on the real query.
                try:
                    conn.rollback()
                except Exception:
                    pass

            truncated = False
            limitation = None

            if has_point:
                limit = request.args.get("limit", type=int) or WATER_LIMIT_DEFAULT
                limit = max(1, min(limit, WATER_LIMIT_MAX))
                sql, params = _water_point_sql(lat, lon, radius_km, limit)
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                raw_rows = cur.fetchall()
                rows = [_water_row(dict(zip(cols, r))) for r in raw_rows]
                truncated = len(rows) >= limit

                if not rows:
                    # Say how far the nearest station actually is — the honest
                    # version of what this endpoint used to return as data.
                    nearest = None
                    try:
                        cur.execute(
                            "SELECT " + _WATER_HAVERSINE_SQL + " AS distance_km, state"
                            " FROM usgs_water_stress"
                            " WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
                            " ORDER BY distance_km ASC LIMIT 1",
                            {"qlat": float(lat), "qlon": float(lon)})
                        nr = cur.fetchone()
                        if nr and nr[0] is not None:
                            nearest = (round(float(nr[0]), 1), nr[1])
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    limitation = (
                        "No USGS monitoring station within %.0f km of (%s, %s). %s"
                        % (radius_km, lat, lon, WATER_COVERAGE_NOTE))
                    if nearest:
                        limitation += (
                            " The nearest station in the dataset is %s km away (%s); it is "
                            "deliberately NOT returned, because a reading that far from your "
                            "site says nothing about it."
                            % (nearest[0], nearest[1] or "unknown state"))
            else:
                limit = request.args.get("limit", type=int) or WATER_STATE_LIMIT_DEFAULT
                limit = max(1, min(limit, WATER_STATE_LIMIT_MAX))
                cur.execute(
                    "SELECT * FROM usgs_water_stress WHERE UPPER(state) = %(st)s"
                    " ORDER BY state, site_id LIMIT %(lim)s",
                    {"st": state, "lim": limit})
                cols = [d[0] for d in cur.description]
                rows = [_water_row(dict(zip(cols, r))) for r in cur.fetchall()]
                truncated = len(rows) >= limit
                limitation = (
                    "State-wide station list for %s. No query point was supplied, so "
                    "distance_km is null on every row and these stations may be "
                    "hundreds of km from any particular site. Pass %s/%s for a "
                    "distance-filtered result."
                    % (state, WATER_LAT_PARAMS[0], WATER_LON_PARAMS[0]))
                if not rows:
                    limitation = (
                        "No USGS monitoring stations on record for state=%s. %s"
                        % (state, WATER_COVERAGE_NOTE))

            cur.close()
            _return_db(conn)
            conn = None
            _g = _site_risk_gate(rows, "site_risk_water")
            if _g is not None:
                return _g
            return jsonify(_water_envelope(rows, query, limitation, truncated))
        except Exception as e:
            logger.error(f"Water stress route error: {e}")
            if conn: _return_db(conn, error=True)
            return jsonify({"error":str(e)}), 500

def register_api_integration_routes(app):
    _register_carbon_route(app)
    _register_climate_route(app)
    _register_risk_route(app)
    _register_water_route(app)
    logger.info("API integration routes registered: /carbon, /climate, /risk, /water/stress")
