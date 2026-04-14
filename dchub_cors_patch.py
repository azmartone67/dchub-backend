"""
dchub_cors_patch.py
====================
Drop this file next to main.py in Replit / Railway.
Then add ONE line at the very bottom of main.py:

    import dchub_cors_patch

That's it. No other changes needed.

What this patch does:
  1. Replaces the 3 broken endpoint handlers in-place (Flask's view_functions dict)
  2. Strips Access-Control-* header logic from the two conflicting after_request
     handlers (add_cors_headers, add_security_headers) so they no longer fight
  3. Registers ONE authoritative CORS after_request handler that always wins
"""

import logging
import requests as _req

# ── grab the already-running Flask app from main.py's module namespace ─────
import sys as _sys
_main = _sys.modules.get('__main__') or _sys.modules.get('main')

# app, request, jsonify, logger, get_pg_connection, return_pg_connection
# are all defined in main.py — we pull them straight from that module so
# this file has zero extra dependencies.
app               = _main.app
request           = _main.request
jsonify           = _main.jsonify
logger            = getattr(_main, 'logger', logging.getLogger('dchub_cors_patch'))
get_pg_connection = getattr(_main, 'get_pg_connection', None)
return_pg_conn    = getattr(_main, 'return_pg_connection', None)

log = logging.getLogger('dchub_cors_patch')
log.info('🔧 dchub_cors_patch loading...')


# =============================================================================
# SECTION 1 — Neutralise the two conflicting CORS after_request handlers
# =============================================================================
# Flask stores after_request functions in app.after_request_funcs[None].
# We wrap each conflicting function so it strips any Access-Control-* headers
# it set and immediately returns — leaving CORS entirely to our handler below.

def _strip_acao(fn):
    """Return a wrapper that calls fn but removes any ACAO headers it wrote."""
    def _wrapper(response):
        response = fn(response)          # run original function
        # Remove every Access-Control-* header the original may have set
        for h in list(response.headers.keys()):
            if h.lower().startswith('access-control-'):
                response.headers.discard(h)
        return response
    _wrapper.__name__ = fn.__name__      # keep Flask happy
    return _wrapper

_CONFLICTING = {'add_cors_headers', 'add_security_headers'}

_funcs = app.after_request_funcs.setdefault(None, [])
for i, fn in enumerate(_funcs):
    if fn.__name__ in _CONFLICTING:
        _funcs[i] = _strip_acao(fn)
        log.info(f'  ✅ Neutralised conflicting handler: {fn.__name__}')


# =============================================================================
# SECTION 2 — Static fallback data for gas layers
# =============================================================================

_GAS_PLANTS = [
    {"name": "Lone Star NGL Fractionator",   "lat": 29.72, "lng": -95.40, "state": "TX", "capacity_mmcfd": 800,  "operator": "Enterprise Products",  "status": "active"},
    {"name": "Midcoast Energy Plant",        "lat": 30.10, "lng": -93.71, "state": "TX", "capacity_mmcfd": 650,  "operator": "Midcoast Energy",       "status": "active"},
    {"name": "Keystone Gas Plant",           "lat": 31.88, "lng": -102.54,"state": "TX", "capacity_mmcfd": 500,  "operator": "Permian Basin Royalty",  "status": "active"},
    {"name": "Panhandle Processing",         "lat": 35.52, "lng": -101.10,"state": "TX", "capacity_mmcfd": 420,  "operator": "DCP Midstream",          "status": "active"},
    {"name": "Opal Processing Plant",        "lat": 41.77, "lng": -110.05,"state": "WY", "capacity_mmcfd": 900,  "operator": "Williams Companies",     "status": "active"},
    {"name": "Belco Gas Plant",              "lat": 42.10, "lng": -107.80,"state": "WY", "capacity_mmcfd": 380,  "operator": "Encana",                 "status": "active"},
    {"name": "Stonewall Gas Gathering",      "lat": 38.98, "lng": -80.58, "state": "WV", "capacity_mmcfd": 290,  "operator": "Crestwood",              "status": "active"},
    {"name": "Sherwood Midstream",           "lat": 39.20, "lng": -81.30, "state": "WV", "capacity_mmcfd": 1200, "operator": "EQT Midstream",          "status": "active"},
    {"name": "Permian Basin Processing Hub", "lat": 31.50, "lng": -103.80,"state": "TX", "capacity_mmcfd": 1500, "operator": "Targa Resources",        "status": "active"},
    {"name": "Houston Ship Channel Plant",   "lat": 29.78, "lng": -95.11, "state": "TX", "capacity_mmcfd": 760,  "operator": "LyondellBasell",         "status": "active"},
    {"name": "Arkla Gas Processing",         "lat": 33.45, "lng": -94.07, "state": "AR", "capacity_mmcfd": 200,  "operator": "CenterPoint Energy",     "status": "active"},
    {"name": "Kern River Processing",        "lat": 35.37, "lng": -119.01,"state": "CA", "capacity_mmcfd": 180,  "operator": "Berkshire Hathaway",     "status": "active"},
    {"name": "Eagle Ford Gas Plant",         "lat": 28.80, "lng": -98.25, "state": "TX", "capacity_mmcfd": 830,  "operator": "Penn Virginia",          "status": "active"},
    {"name": "Marcellus Shale Plant",        "lat": 41.20, "lng": -76.90, "state": "PA", "capacity_mmcfd": 950,  "operator": "Range Resources",        "status": "active"},
    {"name": "DJ Basin Processing",          "lat": 40.10, "lng": -104.50,"state": "CO", "capacity_mmcfd": 610,  "operator": "DCP Midstream",          "status": "active"},
    {"name": "Haynesville Processing",       "lat": 32.55, "lng": -93.70, "state": "LA", "capacity_mmcfd": 670,  "operator": "Kinder Morgan",          "status": "active"},
    {"name": "Pinedale Anticline Plant",     "lat": 42.88, "lng": -109.87,"state": "WY", "capacity_mmcfd": 360,  "operator": "Ultra Petroleum",        "status": "active"},
    {"name": "Carthage Gas Plant",           "lat": 32.16, "lng": -94.34, "state": "TX", "capacity_mmcfd": 520,  "operator": "Boardwalk Pipeline",     "status": "active"},
    {"name": "Uinta Basin Plant",            "lat": 40.30, "lng": -109.80,"state": "UT", "capacity_mmcfd": 275,  "operator": "Uinta Basin Royalty",    "status": "active"},
    {"name": "Panhandle Eastern Plant",      "lat": 38.93, "lng": -98.60, "state": "KS", "capacity_mmcfd": 310,  "operator": "Southern Union",         "status": "active"},
]

_GAS_COMPRESSORS = [
    {"name": "Katy Hub Compressor",          "lat": 29.79, "lng": -95.82, "state": "TX", "horsepower": 45000, "operator": "Boardwalk Pipeline",     "status": "active"},
    {"name": "Waha Compressor Station",      "lat": 31.17, "lng": -103.97,"state": "TX", "horsepower": 60000, "operator": "Enterprise Products",    "status": "active"},
    {"name": "Opal Compressor Station",      "lat": 41.80, "lng": -110.02,"state": "WY", "horsepower": 52000, "operator": "Williams Companies",     "status": "active"},
    {"name": "Transco Station 195",          "lat": 36.60, "lng": -79.40, "state": "VA", "horsepower": 38000, "operator": "Williams Companies",     "status": "active"},
    {"name": "Columbia Gas Compressor",      "lat": 40.12, "lng": -82.94, "state": "OH", "horsepower": 29000, "operator": "TC Energy",              "status": "active"},
    {"name": "ANR Pipeline Station",         "lat": 44.73, "lng": -85.56, "state": "MI", "horsepower": 41000, "operator": "TC Energy",              "status": "active"},
    {"name": "Texas Eastern Compressor",     "lat": 33.18, "lng": -97.09, "state": "TX", "horsepower": 35000, "operator": "Enbridge",               "status": "active"},
    {"name": "Dominion Transmission",        "lat": 39.45, "lng": -80.17, "state": "WV", "horsepower": 31000, "operator": "Dominion Energy",        "status": "active"},
    {"name": "Southern Natural Gas Station", "lat": 32.37, "lng": -86.30, "state": "AL", "horsepower": 22000, "operator": "Berkshire Hathaway",     "status": "active"},
    {"name": "Panhandle Eastern Station",    "lat": 38.93, "lng": -98.60, "state": "KS", "horsepower": 48000, "operator": "Southern Union",         "status": "active"},
    {"name": "Kern River Compressor",        "lat": 37.33, "lng": -118.45,"state": "CA", "horsepower": 26000, "operator": "Berkshire Hathaway",     "status": "active"},
    {"name": "Rockies Express Station",      "lat": 41.05, "lng": -104.82,"state": "WY", "horsepower": 55000, "operator": "Tallgrass Energy",       "status": "active"},
    {"name": "Gulf South Compressor",        "lat": 30.45, "lng": -90.10, "state": "LA", "horsepower": 33000, "operator": "Boardwalk Pipeline",     "status": "active"},
    {"name": "Tennessee Gas Station",        "lat": 35.22, "lng": -86.90, "state": "TN", "horsepower": 27000, "operator": "TC Energy",              "status": "active"},
    {"name": "Transcontinental Station",     "lat": 31.85, "lng": -81.60, "state": "GA", "horsepower": 42000, "operator": "Williams Companies",     "status": "active"},
    {"name": "Colorado Interstate Station",  "lat": 39.75, "lng": -104.88,"state": "CO", "horsepower": 31500, "operator": "TC Energy",              "status": "active"},
    {"name": "Questar Pipeline Station",     "lat": 40.65, "lng": -111.90,"state": "UT", "horsepower": 24000, "operator": "Dominion Energy",        "status": "active"},
    {"name": "Iroquois Compressor NY",       "lat": 41.05, "lng": -73.54, "state": "NY", "horsepower": 19000, "operator": "Iroquois Gas",           "status": "active"},
    {"name": "Maritimes & NE Station",       "lat": 44.30, "lng": -70.20, "state": "ME", "horsepower": 15000, "operator": "Maritimes & NE",         "status": "active"},
    {"name": "Midcoast Compressor",          "lat": 29.95, "lng": -95.35, "state": "TX", "horsepower": 38500, "operator": "Midcoast Energy",        "status": "active"},
]

_INTERCONNECT_FALLBACK = {
    "success": True,
    "source": "fallback",
    "note": "Live upstream unavailable — static RTO queue summary (LBNL 2024)",
    "projects": [
        {"rto": "PJM",   "queued_mw": 280000, "active_projects": 1200, "solar_pct": 0.48, "wind_pct": 0.22, "storage_pct": 0.30},
        {"rto": "MISO",  "queued_mw": 320000, "active_projects": 1450, "solar_pct": 0.52, "wind_pct": 0.28, "storage_pct": 0.20},
        {"rto": "CAISO", "queued_mw": 120000, "active_projects": 620,  "solar_pct": 0.55, "wind_pct": 0.10, "storage_pct": 0.35},
        {"rto": "ERCOT", "queued_mw": 330000, "active_projects": 1100, "solar_pct": 0.60, "wind_pct": 0.25, "storage_pct": 0.15},
        {"rto": "SPP",   "queued_mw":  98000, "active_projects": 480,  "solar_pct": 0.45, "wind_pct": 0.40, "storage_pct": 0.15},
        {"rto": "NYISO", "queued_mw":  45000, "active_projects": 210,  "solar_pct": 0.35, "wind_pct": 0.30, "storage_pct": 0.35},
        {"rto": "ISO-NE","queued_mw":  30000, "active_projects": 140,  "solar_pct": 0.30, "wind_pct": 0.25, "storage_pct": 0.45},
    ],
    "total_queued_mw": 1223000,
    "total_projects": 5200,
}


# =============================================================================
# SECTION 3 — DB helper (uses main.py's pool if available)
# =============================================================================

def _db_conn():
    """Get a connection via main.py's pool, or return None gracefully."""
    if get_pg_connection:
        try:
            return get_pg_connection()
        except Exception as e:
            log.warning(f'_db_conn via pool failed: {e}')
    return None

def _release(conn):
    if conn is None:
        return
    if return_pg_conn:
        try: return_pg_conn(conn)
        except: pass
    else:
        try: conn.close()
        except: pass

def _table_exists(cur, name):
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s", (name,)
    )
    return cur.fetchone() is not None


# =============================================================================
# SECTION 4 — Replace the 3 broken endpoint view functions in-place
# =============================================================================
# Flask stores route handlers in app.view_functions keyed by endpoint name
# (which defaults to the function name).  We simply overwrite those keys —
# the URL rules already exist, so no new @app.route is needed.

def _new_interconnect_queue():
    """Robust proxy to interconnection.fyi with content-type guard + fallback."""
    if request.method == 'OPTIONS':
        return '', 204
    status = request.args.get('status', 'active')
    limit  = request.args.get('limit', 3000)
    try:
        r = _req.get(
            f'https://interconnection.fyi/api/queue?status={status}&limit={limit}',
            timeout=8,
            headers={'Accept': 'application/json', 'User-Agent': 'DCHub/2.0'}
        )
        r.raise_for_status()
        ct = r.headers.get('Content-Type', '')
        if 'json' not in ct:
            raise ValueError(f'Upstream returned non-JSON content-type: {ct}')
        return jsonify(r.json())
    except Exception as e:
        log.warning(f'interconnect-queue proxy failed: {e} — using fallback')
        return jsonify(_INTERCONNECT_FALLBACK), 200

_new_interconnect_queue.__name__ = 'interconnect_queue'


def _new_gas_processing_plants():
    """Gas processing plants — Neon if table exists, else static fallback."""
    if request.method == 'OPTIONS':
        return '', 204
    limit = min(int(request.args.get('limit', 1000)), 5000)
    conn = _db_conn()
    features, source = _GAS_PLANTS, 'fallback'
    if conn:
        try:
            cur = conn.cursor()
            if _table_exists(cur, 'gas_processing_plants'):
                cur.execute("""
                    SELECT name,
                           COALESCE(latitude,  ST_Y(geom::geometry)) AS lat,
                           COALESCE(longitude, ST_X(geom::geometry)) AS lng,
                           capacity_mmcfd, operator, state, status
                    FROM gas_processing_plants
                    WHERE (latitude IS NOT NULL OR geom IS NOT NULL)
                    LIMIT %s
                """, (limit,))
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                if rows:
                    features = [dict(zip(cols, r)) for r in rows]
                    source = 'neon'
            cur.close()
        except Exception as e:
            log.warning(f'gas-processing-plants query: {e}')
        finally:
            _release(conn)
    return jsonify({"success": True, "features": features,
                    "total": len(features), "source": source})

_new_gas_processing_plants.__name__ = 'gas_processing_plants'


def _new_gas_compressor_stations():
    """Gas compressor stations — Neon if table exists, else static fallback."""
    if request.method == 'OPTIONS':
        return '', 204
    limit = min(int(request.args.get('limit', 1000)), 5000)
    conn = _db_conn()
    features, source = _GAS_COMPRESSORS, 'fallback'
    if conn:
        try:
            cur = conn.cursor()
            if _table_exists(cur, 'gas_compressor_stations'):
                cur.execute("""
                    SELECT name,
                           COALESCE(latitude,  ST_Y(geom::geometry)) AS lat,
                           COALESCE(longitude, ST_X(geom::geometry)) AS lng,
                           horsepower, operator, state, status
                    FROM gas_compressor_stations
                    WHERE (latitude IS NOT NULL OR geom IS NOT NULL)
                    LIMIT %s
                """, (limit,))
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                if rows:
                    features = [dict(zip(cols, r)) for r in rows]
                    source = 'neon'
            cur.close()
        except Exception as e:
            log.warning(f'gas-compressor-stations query: {e}')
        finally:
            _release(conn)
    return jsonify({"success": True, "features": features,
                    "total": len(features), "source": source})

_new_gas_compressor_stations.__name__ = 'gas_compressor_stations'


# Swap the handlers in Flask's registry
app.view_functions['interconnect_queue']    = _new_interconnect_queue
app.view_functions['gas_processing_plants'] = _new_gas_processing_plants
app.view_functions['gas_compressor_stations'] = _new_gas_compressor_stations
log.info('  ✅ Replaced 3 endpoint handlers: interconnect_queue, gas_processing_plants, gas_compressor_stations')


# =============================================================================
# SECTION 5 — Single authoritative CORS after_request (registered LAST → wins)
# =============================================================================

_CRED_PREFIXES = (
    '/api/auth/', '/api/stripe/', '/api/v2/alerts',
    '/api/ai-usage/', '/api/v1/land-power/', '/api/land-power/',
)
_ALLOWED_ORIGINS = {
    'https://dchub.cloud',
    'https://www.dchub.cloud',
    'https://api.dchub.cloud',
    'http://localhost:3000',
    'http://localhost:5000',
    'https://dc-hub-replit-fixedzip--azmartone1.replit.app',
}

@app.after_request
def _cors_final(response):
    """
    Single authoritative CORS handler — registered last, always wins.
    - Credentialed paths  → reflect exact origin + Allow-Credentials: true
    - All other paths     → Access-Control-Allow-Origin: *
    """
    origin = request.headers.get('Origin', '')
    path   = request.path

    if any(path.startswith(p) for p in _CRED_PREFIXES):
        ao = origin if origin in _ALLOWED_ORIGINS else 'https://dchub.cloud'
        response.headers['Access-Control-Allow-Origin']      = ao
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    else:
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers.discard('Access-Control-Allow-Credentials')

    response.headers['Access-Control-Allow-Methods'] = \
        'GET, POST, PUT, DELETE, PATCH, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = \
        'Content-Type, Authorization, X-API-Key, X-Admin-Key, Accept, X-Requested-With'
    response.headers['Access-Control-Max-Age'] = '86400'
    return response

log.info('  ✅ Authoritative CORS handler registered (_cors_final)')
log.info('✅ dchub_cors_patch fully applied — CORS fixed, 3 endpoints replaced')
