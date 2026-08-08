"""
dchub_cors_patch.py — v3
========================
Drop next to main.py. Add at the very bottom of main.py:

    import dchub_cors_patch

Works under gunicorn (Railway) AND direct python (Replit).
"""

import sys
import logging
import requests as _req

log = logging.getLogger('dchub_cors_patch')
log.info('dchub_cors_patch v3 loading...')

def _remove_acao(response):
    """Remove all Access-Control-* headers safely (Flask Headers has no .discard)."""
    to_remove = [h for h in response.headers.keys()
                 if h.lower().startswith('access-control-')]
    for h in to_remove:
        try:
            del response.headers[h]
        except Exception:
            pass

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
            _remove_acao(response)
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
    # ── DELETED 2026-08-08: _GAS_PLANTS / _GAS_COMPRESSORS ──────────────
    # Two 12-row literals of INVENTED gas processing plants and compressor
    # stations — invented names, invented coordinates, invented capacities
    # and invented horsepower ("Katy Hub Compressor", 45,000 hp; "Permian
    # Basin Processing Hub", 1,500 MMcf/d) — served under `success: true`
    # whenever the DB connection or the table check failed. The only tell was
    # a `source: "fallback"` field beside them.
    #
    # ★ WHY DELETED AND NOT LABELLED
    # /api/v1/gas-compressor-stations sits on the 3600s CDN allowlist
    # (main.py: '/api/v1/gas-compressor-stations': (3600, ...)). A single
    # pool blip during a cache MISS pins fabricated stations at the edge for
    # a full hour, and every subsequent caller gets a HIT — so the DB
    # recovering does not undo it and nothing in our own monitoring can see
    # it, because the origin is healthy the whole time. A disclosure field
    # cannot fix an availability window; only not having the data can.
    #
    # The endpoints now fail honestly: HTTP 503, `success: false`, and a
    # reason. An empty map overlay is a recoverable, visible failure. Twelve
    # invented compressor stations on a siting map are not.

    _INTERCONNECT_FALLBACK = {
        "success": True, "source": "fallback",
        "note": "Live upstream unavailable — static RTO queue summary (LBNL 2024)",
        "projects": [
            {"rto": "PJM",    "queued_mw": 280000, "active_projects": 1200},
            {"rto": "MISO",   "queued_mw": 320000, "active_projects": 1450},
            {"rto": "CAISO",  "queued_mw": 120000, "active_projects": 620},
            {"rto": "ERCOT",  "queued_mw": 330000, "active_projects": 1100},
            {"rto": "SPP",    "queued_mw":  98000, "active_projects": 480},
            {"rto": "NYISO",  "queued_mw":  45000, "active_projects": 210},
            {"rto": "ISO-NE", "queued_mw":  30000, "active_projects": 140},
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

    def _gas_asset_endpoint(table, extra_col, surface):
        """Shared body for the two gas-asset endpoints.

        Serves ONLY real rows. No fabricated fallback: on any failure the
        caller gets 503 + success:false, never a synthetic feature list.

        ★ `total` is the ROW COUNT, not len(features). It used to be
          len(features), so `?limit=3` answered `total: 3` — a caller
          paginating on `total` stopped after one page and a caller
          quoting it published the page size as the size of the dataset.
          The count is computed under the same WHERE clause as the page so
          the two cannot describe different populations.
        """
        if request.method == 'OPTIONS':
            return '', 204
        try:
            limit = min(int(request.args.get('limit', 1000)), 5000)
        except (TypeError, ValueError):
            limit = 1000

        conn = _db_conn()
        if conn is None:
            return jsonify({
                "success": False, "features": [], "total": None,
                "source": "unavailable",
                "error": "database_unavailable",
                "note": (f"No {surface} data is served without a database "
                         "read. This endpoint has no fallback by design "
                         "(fabricated fallbacks removed 2026-08-08)."),
            }), 503
        try:
            cur = conn.cursor()
            if not _table_exists(cur, table):
                cur.close()
                return jsonify({
                    "success": False, "features": [], "total": None,
                    "source": "unavailable", "error": "table_missing",
                    "note": f"{table} is not present in this database.",
                }), 503

            where = "WHERE (latitude IS NOT NULL OR geom IS NOT NULL)"
            cur.execute(f"SELECT COUNT(*) FROM {table} {where}")
            total = int((cur.fetchone() or [0])[0] or 0)
            cur.execute(f"""
                SELECT name,
                       COALESCE(latitude,  ST_Y(geom::geometry)) AS lat,
                       COALESCE(longitude, ST_X(geom::geometry)) AS lng,
                       {extra_col}, operator, state, status
                FROM {table}
                {where}
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            cur.close()
            return jsonify({
                "success": True,
                "features": [dict(zip(cols, r)) for r in rows],
                "count": len(rows),
                "total": total,
                "total_basis": f"COUNT(*) over {table} under the same filter "
                               "as the returned page — not the page size",
                "source": "neon",
            })
        except Exception as e:
            log.warning(f'{surface}: {e}')
            return jsonify({
                "success": False, "features": [], "total": None,
                "source": "unavailable", "error": "query_failed",
                "note": (f"No {surface} data is served without a successful "
                         "database read. This endpoint has no fallback by "
                         "design (fabricated fallbacks removed 2026-08-08)."),
            }), 503
        finally:
            _release(conn)

    def _new_gas_processing_plants():
        return _gas_asset_endpoint('gas_processing_plants', 'capacity_mmcfd',
                                   'gas-processing-plants')

    _new_gas_processing_plants.__name__ = 'gas_processing_plants'

    def _new_gas_compressor_stations():
        return _gas_asset_endpoint('gas_compressor_stations', 'horsepower',
                                   'gas-compressor-stations')

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
            # Safely remove credentials header — Flask Headers uses del not discard
            try:
                del response.headers['Access-Control-Allow-Credentials']
            except Exception:
                pass
        response.headers['Access-Control-Allow-Methods'] = \
            'GET, POST, PUT, DELETE, PATCH, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = \
            'Content-Type, Authorization, X-API-Key, X-Admin-Key, Accept, X-Requested-With'
        response.headers['Access-Control-Max-Age'] = '86400'
        return response

    log.info('  _cors_final registered')
    log.info('dchub_cors_patch v3 fully applied — CORS fixed, 3 endpoints replaced')
