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
        # Water stress (usgs_water_stress)
        if lat and lng:
            cur.execute("SELECT *, (ABS(latitude - %s) + ABS(longitude - %s)) as dist FROM usgs_water_stress ORDER BY dist ASC LIMIT 5", (lat, lng))
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                recs = []
                for r in rows:
                    d = dict(zip(cols, r))
                    d.pop("dist", None)
                    recs.append(_safe_json(d))
                enrichment["water_stress"] = {"nearest_sites": recs}
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

def _register_water_route(app):
    @app.route("/api/v1/water/stress", methods=["GET"])
    def api_water_stress():
        conn = None
        try:
            from flask import request, jsonify
            state = request.args.get("state","").upper().strip()
            lat = request.args.get("lat", type=float)
            lng = request.args.get("lng", type=float)
            conn = _get_db()
            cur = conn.cursor()
            if lat is not None and lng is not None:
                cur.execute("SELECT *, (ABS(latitude - %s) + ABS(longitude - %s)) as dist FROM usgs_water_stress ORDER BY dist ASC LIMIT 10", (lat, lng))
            elif state:
                cur.execute("SELECT * FROM usgs_water_stress WHERE UPPER(state) = %s", (state,))
            else:
                cur.execute("SELECT * FROM usgs_water_stress ORDER BY state LIMIT 50")
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d.pop("dist", None)
                rows.append(_safe_json(d))
            cur.close()
            _return_db(conn)
            _g = _site_risk_gate(rows, "site_risk_water")
            if _g is not None:
                return _g
            return jsonify({"source":"USGS","count":len(rows),"data":rows})
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
