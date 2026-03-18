"""
DC Hub API Integration Wiring - March 18, 2026 (schema-verified)
"""
import logging
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
                cur.execute("SELECT * FROM fema_risk_index WHERE UPPER(state) = %s AND LOWER(county) LIKE %s ORDER BY risk_score DESC", (state, f"%{county.lower()}%"))
            elif state:
                cur.execute("SELECT * FROM fema_risk_index WHERE UPPER(state) = %s ORDER BY risk_score DESC", (state,))
            elif top_n:
                cur.execute("SELECT * FROM fema_risk_index ORDER BY risk_score DESC LIMIT %s", (min(top_n, 100),))
            else:
                cur.execute("SELECT * FROM fema_risk_index ORDER BY risk_score DESC LIMIT 50")
            cols = [d[0] for d in cur.description]
            rows = [_safe_json(dict(zip(cols, r))) for r in cur.fetchall()]
            cur.close()
            _return_db(conn)
            return jsonify({"source":"FEMA National Risk Index","count":len(rows),"data":rows})
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
            if state:
                cur.execute("SELECT * FROM usgs_water_stress WHERE UPPER(state) = %s ORDER BY county", (state,))
            elif lat is not None and lng is not None:
                cur.execute("SELECT *, (ABS(latitude - %s) + ABS(longitude - %s)) as dist FROM usgs_water_stress ORDER BY dist ASC LIMIT 10", (lat, lng))
            else:
                cur.execute("SELECT * FROM usgs_water_stress ORDER BY state, county LIMIT 100")
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d.pop("dist", None)
                rows.append(_safe_json(d))
            cur.close()
            _return_db(conn)
            return jsonify({"source":"USGS Water Resources","count":len(rows),"data":rows})
        except Exception as e:
            logger.error(f"Water stress route error: {e}")
            if conn: _return_db(conn, error=True)
            return jsonify({"error":str(e)}), 500

def enrich_site_analysis(lat, lng, state=None):
    enrichment = {"carbon":None,"climate":None,"risk":None,"water_stress":None,"energy_rates":None}
    conn = None
    try:
        conn = _get_db()
        cur = conn.cursor()
        if state and state.upper() in STATE_TO_EGRID:
            cur.execute("SELECT * FROM epa_egrid WHERE subregion_code = %s", (STATE_TO_EGRID[state.upper()],))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                enrichment["carbon"] = _safe_json(dict(zip(cols, row)))
        cur.execute("SELECT *, (ABS(latitude - %s) + ABS(longitude - %s)) as dist FROM nasa_power_climate ORDER BY dist ASC LIMIT 1", (lat, lng))
        row = cur.fetchone()
        if row:
            cols = [d[0] for d in cur.description]
            d = dict(zip(cols, row))
            d.pop("dist", None)
            enrichment["climate"] = _safe_json(d)
        if state:
            cur.execute("SELECT * FROM fema_risk_index WHERE UPPER(state) = %s ORDER BY risk_score DESC LIMIT 3", (state.upper(),))
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                enrichment["risk"] = {"state":state.upper(),"top_risk_counties":[_safe_json(dict(zip(cols, r))) for r in rows]}
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
                enrichment["water_stress"] = {"nearest_sites":recs}
        if state:
            state_full = STATE_ABBR_TO_NAME.get(state.upper(), state)
            cur.execute("SELECT * FROM eia_retail_rates WHERE state = %s AND LOWER(sector) = 'industrial' ORDER BY period DESC LIMIT 3", (state_full,))
            rows = cur.fetchall()
            if rows:
                cols = [d[0] for d in cur.description]
                enrichment["energy_rates"] = {"source":"EIA","state":state.upper(),"rates":[_safe_json(dict(zip(cols, r))) for r in rows]}
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

def register_api_integration_routes(app):
    _register_carbon_route(app)
    _register_climate_route(app)
    _register_risk_route(app)
    _register_water_route(app)
    logger.info("API integration routes registered: /carbon, /climate, /risk, /water/stress")
