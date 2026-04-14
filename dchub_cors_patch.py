"""
dchub_cors_patch.py — v2
========================
Drop next to main.py. Add at the very bottom of main.py:

    import dchub_cors_patch

Works under gunicorn (Railway) AND direct python (Replit).
"""

import sys
import logging
import requests as _req

log = logging.getLogger('dchub_cors_patch')
log.info('dchub_cors_patch v2 loading...')

# =============================================================================
# Find the Flask app — safe under gunicorn where __main__ is gunicorn itself
# =============================================================================
def _find_flask_app():
    try:
        from flask import Flask
    except ImportError:
        return None
    for name in ['main', '__main__']:
        mod = sys.modules.get(name)
        if mod and isinstance(getattr(mod, 'app', None), Flask):
            log.info(f'  Found Flask app in sys.modules["{name}"]')
            return mod
    for name, mod in list(sys.modules.items()):
        if mod and isinstance(getattr(mod, 'app', None), Flask):
            log.info(f'  Found Flask app via scan: {name}')
            return mod
    return None

_main = _find_flask_app()

if _main is None:
    log.error('dchub_cors_patch: Could not find Flask app — patch NOT applied')
else:
    app               = _main.app
    request           = _main.request
    jsonify           = _main.jsonify
    get_pg_connection = getattr(_main, 'get_pg_connection', None)
    return_pg_conn    = getattr(_main, 'return_pg_connection', None)

    # =========================================================================
    # SECTION 1 — Neutralise conflicting CORS after_request handlers
    # =========================================================================
    _CONFLICTING = {'add_cors_headers', 'add_security_headers'}

    def _strip_acao(fn):
        def _wrapper(response):
            try:
                response = fn(response)
            except Exception:
                pass
            for h in list(response.headers.keys()):
                if h.lower().startswith('access-control-'):
                    response.headers.discard(h)
            return response
        _wrapper.__name__ = fn.__name__
        return _wrapper

    _funcs = app.after_request_funcs.setdefault(None, [])
    for i, fn in enumerate(_funcs):
        if fn.__name__ in _CONFLICTING:
            _funcs[i] = _strip_acao(fn)
            log.info(f'  Neutralised: {fn.__name__}')

    # =========================================================================
    # SECTION 2 — Static fallback data
    # =========================================================================
    _GAS_PLANTS = [
        {"name": "Lone Star NGL Fractionator",   "lat": 29.72, "lng": -95.40, "state": "TX", "capacity_mmcfd": 800,  "operator": "Enterprise Products", "status": "active"},
        {"name": "Midcoast Energy Plant",        "lat": 30.10, "lng": -93.71, "state": "TX", "capacity_mmcfd": 650,  "operator": "Midcoast Energy",     "status": "active"},
        {"name": "Keystone Gas Plant",           "lat": 31.88, "lng": -102.54,"state": "TX", "capacity_mmcfd": 500,  "operator": "Permian Basin Royalty","status": "active"},
        {"name": "Panhandle Processing",         "lat": 35.52, "lng": -101.10,"state": "TX", "capacity_mmcfd": 420,  "operator": "DCP Midstream",        "status": "active"},
        {"name": "Opal Processing Plant",        "lat": 41.77, "lng": -110.05,"state": "WY", "capacity_mmcfd": 900,  "operator": "Williams Companies",   "status": "active"},
        {"name": "Sherwood Midstream",           "lat": 39.20, "lng": -81.30, "state": "WV", "capacity_mmcfd": 1200, "operator": "EQT Midstream",        "status": "active"},
        {"name": "Permian Basin Processing Hub", "lat": 31.50, "lng": -103.80,"state": "TX", "capacity_mmcfd": 1500, "operator": "Targa Resources",      "status": "active"},
        {"name": "Eagle Ford Gas Plant",         "lat": 28.80, "lng": -98.25, "state": "TX", "capacity_mmcfd": 830,  "operator": "Penn Virginia",        "status": "active"},
        {"name": "Marcellus Shale Plant",        "lat": 41.20, "lng": -76.90, "state": "PA", "capacity_mmcfd": 950,  "operator": "Range Resources",      "status": "active"},
        {"name": "Haynesville Processing",       "lat": 32.55, "lng": -93.70, "state": "LA", "capacity_mmcfd": 670,  "operator": "Kinder Morgan",        "status": "active"},
        {"name": "DJ Basin Processing",          "lat": 40.10, "lng": -104.50,"state": "CO", "capacity_mmcfd": 610,  "operator": "DCP Midstream",        "status": "active"},
        {"name": "Kern River Processing",        "lat": 35.37, "lng": -119.01,"state": "CA", "capacity_mmcfd": 180,  "operator": "Berkshire Hathaway",   "status": "active"},
    ]

    _GAS_COMPRESSORS = [
        {"name": "Katy Hub Compressor",         "lat": 29.79, "lng": -95.82, "state": "TX", "horsepower": 45000, "operator": "Boardwalk Pipeline",  "status": "active"},
        {"name": "Waha Compressor Station",     "lat": 31.17, "lng": -103.97,"state": "TX", "horsepower": 60000, "operator": "Enterprise Products", "status": "active"},
        {"name": "Opal Compressor Station",     "lat": 41.80, "lng": -110.02,"state": "WY", "horsepower": 52000, "operator": "Williams Companies",  "status": "active"},
        {"name": "Transco Station 195",         "lat": 36.60, "lng": -79.40, "state": "VA", "horsepower": 38000, "operator": "Williams Companies",  "status": "active"},
        {"name": "Columbia Gas Compressor",     "lat": 40.12, "lng": -82.94, "state": "OH", "horsepower": 29000, "operator": "TC Energy",           "status": "active"},
        {"name": "ANR Pipeline Station",        "lat": 44.73, "lng": -85.56, "state": "MI", "horsepower": 41000, "operator": "TC Energy",           "status": "active"},
        {"name": "Texas Eastern Compressor",    "lat": 33.18, "lng": -97.09, "state": "TX", "horsepower": 35000, "operator": "Enbridge",            "status": "active"},
        {"name": "Dominion Transmission",       "lat": 39.45, "lng": -80.17, "state": "WV", "horsepower": 31000, "operator": "Dominion Energy",     "status": "active"},
        {"name": "Panhandle Eastern Station",   "lat": 38.93, "lng": -98.60, "state": "KS", "horsepower": 48000, "operator": "Southern Union",      "status": "active"},
        {"name": "Rockies Express Station",     "lat": 41.05, "lng": -104.82,"state": "WY", "horsepower": 55000, "operator": "Tallgrass Energy",    "status": "active"},
        {"name": "Kern River Compressor",       "lat": 37.33, "lng": -118.45,"state": "CA", "horsepower": 26000, "operator": "Berkshire Hathaway",  "status": "active"},
        {"name": "Gulf South Compressor",       "lat": 30.45, "lng": -90.10, "state": "LA", "horsepower": 33000, "operator": "Boardwalk Pipeline",  "status": "active"},
    ]

    _INTERCONNECT_FALLBACK = {
        "success": True, "source": "fallback",
        "note": "Live upstream unavailable — static RTO queue summary (LBNL 2024)",
        "projects": [
            {"rto": "PJM",   "queued_mw": 280000, "active_projects": 1200},
            {"rto": "MISO",  "queued_mw": 320000, "active_projects": 1450},
            {"rto": "CAISO", "queued_mw": 120000, "active_projects": 620},
            {"rto": "ERCOT", "queued_mw": 330000, "active_projects": 1100},
            {"rto": "SPP",   "queued_mw":  98000, "active_projects": 480},
            {"rto": "NYISO", "queued_mw":  45000, "active_projects": 210},
            {"rto": "ISO-NE","queued_mw":  30000, "active_projects": 140},
        ],
        "total_queued_mw": 1223000, "total_projects": 5200,
    }

    # =========================================================================
    # SECTION 3 — DB helpers (reuse main.py's connection pool)
    # =========================================================================
    def _db_conn():
        if get_pg_connection:
            try:
                return get_pg_connection()
            except Exception as e:
                log.warning(f'_db_conn failed: {e}')
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

    # =========================================================================
    # SECTION 4 — Replace the 3 broken view functions in-place
    # =========================================================================
    def _new_interconnect_queue():
        if request.method == 'OPTIONS':
            return '', 204
        status = request.args.get('status', 'active')
        limit  = request.args.get('limit', 3000)
        try:
            r = _req.get(
                f'https://interconnection.fyi/api/queue?status={status}&limit={limit}',
                timeout=8, headers={'Accept': 'application/json', 'User-Agent': 'DCHub/2.0'}
            )
            r.raise_for_status()
            if 'json' not in r.headers.get('Content-Type', ''):
                raise ValueError('Upstream returned non-JSON')
            return jsonify(r.json())
        except Exception as e:
            log.warning(f'interconnect-queue proxy failed: {e}')
            return jsonify(_INTERCONNECT_FALLBACK), 200

    _new_interconnect_queue.__name__ = 'interconnect_queue'

    def _new_gas_processing_plants():
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
                log.warning(f'gas-processing-plants: {e}')
            finally:
                _release(conn)
        return jsonify({"success": True, "features": features,
                        "total": len(features), "source": source})

    _new_gas_processing_plants.__name__ = 'gas_processing_plants'

    def _new_gas_compressor_stations():
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
                log.warning(f'gas-compressor-stations: {e}')
            finally:
                _release(conn)
        return jsonify({"success": True, "features": features,
                        "total": len(features), "source": source})

    _new_gas_compressor_stations.__name__ = 'gas_compressor_stations'

    for _key, _fn in [
        ('interconnect_queue',      _new_interconnect_queue),
        ('gas_processing_plants',   _new_gas_processing_plants),
        ('gas_compressor_stations', _new_gas_compressor_stations),
    ]:
        if _key in app.view_functions:
            app.view_functions[_key] = _fn
            log.info(f'  Replaced endpoint: {_key}')
        else:
            log.warning(f'  Endpoint not found, skipping: {_key}')

    # =========================================================================
    # SECTION 5 — Single authoritative CORS handler, registered LAST so it wins
    # =========================================================================
    _CRED_PREFIXES = (
        '/api/auth/', '/api/stripe/', '/api/v2/alerts',
        '/api/ai-usage/', '/api/v1/land-power/', '/api/land-power/',
    )
    _ALLOWED_ORIGINS = {
        'https://dchub.cloud', 'https://www.dchub.cloud',
        'https://api.dchub.cloud',
        'http://localhost:3000', 'http://localhost:5000',
        'https://dc-hub-replit-fixedzip--azmartone1.replit.app',
    }

    @app.after_request
    def _cors_final(response):
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

    log.info('  _cors_final registered')
    log.info('dchub_cors_patch v2 fully applied — CORS fixed, 3 endpoints replaced')
