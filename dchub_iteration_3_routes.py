"""DC Hub iteration 3 v2 (a.k.a. iteration 4) — semantic search with fuzzy
hydration and grid/state/MW post-vectorize filters.

Changes from v1:
  * _hydrate now tries three strategies in order and reports `hydration_method`:
      1. exact name + provider match
      2. ILIKE fuzzy match (paren-stripped, case-insensitive)
      3. coord proximity (~110m bbox + nearest by squared-distance)
  * semantic_search accepts filter params: grid, states, provider, country,
    status, min_mw, max_mw. When any filter is set, we over-fetch (5× topK)
    from Vectorize and apply filters before trimming to topK.
  * New /api/v1/search/grids endpoint exposes the grid→state-territories map
    so frontends can populate a grid-picker.

Endpoints:

  GET /api/v1/search/semantic?q=<text>&topK=<n>&hydrate=<bool>
                              [&grid=PJM|...] [&states=VA,PA] [&provider=...]
                              [&country=US] [&status=Operational]
                              [&min_mw=N] [&max_mw=N]

  GET /api/v1/search/grids
       List supported ISO/grid names and their state territories.

  GET /api/v1/search/health
       Diagnostic: confirms env vars and a quick embedding round-trip.

Required Railway env vars (any aliases):
    CLOUDFLARE_API_TOKEN  | CF_API_TOKEN
    CLOUDFLARE_ACCOUNT_ID | CF_ACCOUNT_ID | ACC
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.request
import urllib.error
import json as _json
from flask import jsonify, request

logger = logging.getLogger('dchub.iteration3')

VECTORIZE_INDEX = 'dchub-facilities'
EMBED_MODEL     = '@cf/baai/bge-base-en-v1.5'

# Approximate state-level grid territories. Some states span multiple ISOs
# (e.g. parts of TX are in SPP, IL splits MISO/PJM); the table reflects the
# *primary* coverage for filtering purposes. Refine with finer-grained data
# (zip-code → utility → ISO) in a follow-up if needed.
GRID_TERRITORIES = {
    'PJM':    {'PA','NJ','MD','DC','DE','OH','KY','NC','TN','IL','IN','MI','VA','WV'},
    'ERCOT':  {'TX'},
    'CAISO':  {'CA'},
    'SPP':    {'KS','OK','NE','ND','SD','AR','LA'},
    'MISO':   {'IL','IN','IA','MI','MN','MO','MS','MT','ND','SD','WI','AR','KY','LA'},
    'SOCO':   {'GA','AL','MS','FL'},
    'NYISO':  {'NY'},
    'ISO-NE': {'CT','MA','ME','NH','RI','VT'},
    'NWPP':   {'WA','OR','ID','MT','UT','WY'},
    'AESO':   set(),  # Alberta — placeholder for Canadian grids
}


def _cf_creds():
    token = os.environ.get('CLOUDFLARE_API_TOKEN') or os.environ.get('CF_API_TOKEN')
    account = (os.environ.get('CLOUDFLARE_ACCOUNT_ID')
               or os.environ.get('CF_ACCOUNT_ID')
               or os.environ.get('ACC'))
    return token, account


def _cf_post(url, token, payload, timeout=20):
    body = _json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode('utf-8')), resp.status
    except urllib.error.HTTPError as e:
        try:
            return _json.loads(e.read().decode('utf-8')), e.code
        except Exception:
            return {'error': str(e)}, e.code


def _embed(query, token, account):
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{EMBED_MODEL}"
    data, status = _cf_post(url, token, {'text': [query]})
    if status != 200 or not data.get('success'):
        return None, {'embed_status': status, 'embed_error': data.get('errors') or data}
    try:
        return data['result']['data'][0], None
    except (KeyError, IndexError, TypeError):
        return None, {'embed_status': status, 'embed_error': 'malformed-response', 'body': data}


def _vector_query(vector, topK, token, account):
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
           f"/vectorize/v2/indexes/{VECTORIZE_INDEX}/query")
    data, status = _cf_post(url, token, {
        'vector': vector,
        'topK': topK,
        'returnMetadata': 'all',
    })
    if status != 200 or not data.get('success'):
        return None, {'vectorize_status': status, 'vectorize_error': data.get('errors') or data}
    return data['result'].get('matches', []), None


# ============================================================
# Filtering
# ============================================================

def _apply_filters(matches, filters):
    """Post-vectorize filtering. Returns matches that satisfy all active filters."""
    if not filters:
        return matches
    grid = filters.get('grid')
    grid_states = GRID_TERRITORIES.get(grid.upper()) if grid else None

    states_csv = filters.get('states')
    explicit_states = (
        {s.upper().strip() for s in states_csv.split(',') if s.strip()}
        if states_csv else None
    )

    provider_q  = (filters.get('provider') or '').lower().strip()
    country_q   = (filters.get('country')  or '').upper().strip()
    status_q    = (filters.get('status')   or '').lower().strip()
    min_mw      = filters.get('min_mw')
    max_mw      = filters.get('max_mw')

    out = []
    for m in matches:
        md = m.get('metadata') or {}
        st = (md.get('state')    or '').upper()
        co = (md.get('country')  or '').upper()
        pr = (md.get('provider') or '').lower()
        ss = (md.get('status')   or '').lower()
        mw = md.get('power_mw') or 0

        if grid_states is not None and st not in grid_states:
            continue
        if explicit_states is not None and st not in explicit_states:
            continue
        if provider_q and provider_q not in pr:
            continue
        if country_q and co != country_q:
            continue
        if status_q and status_q not in ss:
            continue
        if min_mw is not None and mw < min_mw:
            continue
        if max_mw is not None and mw > max_mw:
            continue
        out.append(m)
    return out


# ============================================================
# Fuzzy hydration — three strategies
# ============================================================

_HYDRATE_COLS = ('id, slug, name, provider, latitude, longitude, '
                 'power_mw, status, city, state, country, source, source_url')


def _safe_float(v):
    try: return float(v) if v is not None else None
    except (TypeError, ValueError): return None


def _row_to_dict(cur, row):
    cols = [d[0] for d in cur.description]
    h = dict(zip(cols, row))
    for k in ('latitude', 'longitude', 'power_mw'):
        if h.get(k) is not None:
            h[k] = _safe_float(h[k])
    return h


def _hydrate(matches):
    if not matches:
        return matches
    try:
        from dchub_iteration_2_routes import _get_pg_conn
    except Exception as e:
        logger.warning("iteration3: hydrate skipped, _get_pg_conn unavailable: %s", e)
        return matches

    try:
        conn = _get_pg_conn()
    except Exception as e:
        logger.warning("iteration3: hydrate connection failed: %s", e)
        return matches

    try:
        cur = conn.cursor()
        for m in matches:
            md = m.get('metadata') or {}
            name = md.get('name')
            prov = md.get('provider')
            lat  = md.get('lat')
            lng  = md.get('lng')

            row = None
            method = None

            # Strategy 1: exact name + provider
            if name:
                try:
                    cur.execute(f"""
                        SELECT {_HYDRATE_COLS} FROM facilities
                        WHERE name = %s AND (%s IS NULL OR provider = %s)
                        LIMIT 1
                    """, (name, prov, prov))
                    row = cur.fetchone()
                    if row: method = 'exact-name-provider'
                except Exception as e:
                    logger.debug("hydrate strat1 err: %s", e)
                    conn.rollback()

            # Strategy 2: ILIKE fuzzy (paren-strip)
            if not row and name:
                try:
                    name_clean = re.sub(r'\s*\([^)]*\)\s*', ' ', name).strip()
                    base = name_clean if len(name_clean) >= 4 else name
                    pattern = f"%{base}%"
                    if prov:
                        cur.execute(f"""
                            SELECT {_HYDRATE_COLS} FROM facilities
                            WHERE name ILIKE %s AND provider ILIKE %s
                            LIMIT 1
                        """, (pattern, f"%{prov}%"))
                    else:
                        cur.execute(f"""
                            SELECT {_HYDRATE_COLS} FROM facilities
                            WHERE name ILIKE %s
                            LIMIT 1
                        """, (pattern,))
                    row = cur.fetchone()
                    if row: method = 'fuzzy-name-ilike'
                except Exception as e:
                    logger.debug("hydrate strat2 err: %s", e)
                    conn.rollback()

            # Strategy 3: coord proximity (~110m bbox)
            if not row and lat is not None and lng is not None:
                try:
                    cur.execute(f"""
                        SELECT {_HYDRATE_COLS} FROM facilities
                        WHERE latitude BETWEEN %s AND %s
                          AND longitude BETWEEN %s AND %s
                        ORDER BY (
                            (latitude - %s) * (latitude - %s) +
                            (longitude - %s) * (longitude - %s)
                        )
                        LIMIT 1
                    """, (lat - 0.001, lat + 0.001, lng - 0.001, lng + 0.001,
                          lat, lat, lng, lng))
                    row = cur.fetchone()
                    if row: method = 'coord-proximity'
                except Exception as e:
                    logger.debug("hydrate strat3 err: %s", e)
                    conn.rollback()

            if row:
                m['hydrated'] = _row_to_dict(cur, row)
                m['hydration_method'] = method
            else:
                m['hydrated'] = None
                m['hydration_method'] = None
        cur.close()
    finally:
        try: conn.close()
        except Exception: pass
    return matches


# ============================================================
# /api/v1/search/semantic
# ============================================================

def semantic_search():
    started = time.time()
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'error': 'q parameter required'}), 400

    try:
        topK = max(1, min(int(request.args.get('topK', 5)), 50))
    except ValueError:
        topK = 5
    hydrate_flag = (request.args.get('hydrate', 'false').lower()
                    in ('true', '1', 'yes', 'y'))

    # Collect filter params (only set keys that are non-empty)
    filters = {}
    for fname in ('grid', 'states', 'provider', 'country', 'status'):
        v = (request.args.get(fname) or '').strip()
        if v: filters[fname] = v
    for mw_key in ('min_mw', 'max_mw'):
        raw = request.args.get(mw_key)
        if raw:
            try: filters[mw_key] = float(raw)
            except ValueError: pass

    # Validate grid filter early so user gets a clear error
    if 'grid' in filters and filters['grid'].upper() not in GRID_TERRITORIES:
        return jsonify({
            'error': 'unknown-grid',
            'grid': filters['grid'],
            'available': sorted(GRID_TERRITORIES.keys()),
        }), 400

    token, account = _cf_creds()
    if not (token and account):
        return jsonify({
            'error': 'cloudflare-credentials-missing',
            'have_token':   bool(token),
            'have_account': bool(account),
        }), 500

    t_embed_start = time.time()
    vector, err = _embed(q, token, account)
    if vector is None:
        return jsonify({'error': 'embed-failed', **(err or {})}), 502
    embed_ms = int((time.time() - t_embed_start) * 1000)

    # If filters are active, over-fetch so post-filter still has enough results
    fetch_K = min(topK * 5, 50) if filters else topK

    t_query_start = time.time()
    matches, err = _vector_query(vector, fetch_K, token, account)
    if matches is None:
        return jsonify({'error': 'vectorize-query-failed', 'embed_ms': embed_ms,
                        **(err or {})}), 502
    query_ms = int((time.time() - t_query_start) * 1000)

    pre_filter_count = len(matches)
    if filters:
        matches = _apply_filters(matches, filters)
    post_filter_count = len(matches)
    matches = matches[:topK]

    # Iteration 5: optional composite reranking (score x log(power_mw+1) x status_weight)
    if (request.args.get('rerank', '').lower() in ('true','1','yes','y')):
        import math as _math
        def _composite(_m):
            md = _m.get('metadata') or {}
            score = _m.get('score') or 0.0
            mw = md.get('power_mw') or 0
            try: mw = float(mw)
            except (TypeError, ValueError): mw = 0
            status = (md.get('status') or '').lower()
            status_weight = 1.2 if 'construction' in status else (1.0 if 'operational' in status else 0.85)
            cs = score * _math.log(1 + mw) * status_weight
            _m['composite_score'] = cs
            return cs
        matches = sorted(matches, key=_composite, reverse=True)

    if hydrate_flag:
        matches = _hydrate(matches)

    flat = []
    for m in matches:
        md = m.get('metadata') or {}
        flat.append({
            'id':                m.get('id'),
            'score':             m.get('score'),
            'name':              md.get('name'),
            'provider':          md.get('provider'),
            'city':              md.get('city'),
            'state':             md.get('state'),
            'country':           md.get('country'),
            'lat':               md.get('lat'),
            'lng':               md.get('lng'),
            'power_mw':          md.get('power_mw'),
            'status':            md.get('status'),
            'hydrated':          m.get('hydrated'),
            'hydration_method': m.get('hydration_method'),
            'composite_score':  m.get('composite_score'),
        })

    return jsonify({
        'query':           q,
        'topK':            topK,
        'count':           len(flat),
        'hydrated':        hydrate_flag,
        'matches':         flat,
        'filters':         filters or None,
        'filter_stats':    {
            'fetched':            pre_filter_count,
            'matched_filters':    post_filter_count,
            'returned':           len(flat),
        },
        'timing_ms': {
            'embed':   embed_ms,
            'query':   query_ms,
            'total':   int((time.time() - started) * 1000),
        },
        'index':           VECTORIZE_INDEX,
        'model':           EMBED_MODEL,
    }), 200


# ============================================================
# /api/v1/search/grids
# ============================================================

def list_grids():
    return jsonify({
        'grids':       sorted(GRID_TERRITORIES.keys()),
        'territories': {k: sorted(v) for k, v in GRID_TERRITORIES.items()},
        'note': ('State-level approximation. Some states span multiple ISOs; '
                 'the listed grid is the primary coverage for filtering.'),
    }), 200


# ============================================================
# /api/v1/search/health
# ============================================================

def semantic_search_health():
    token, account = _cf_creds()
    if not (token and account):
        return jsonify({
            'ok': False,
            'reason': 'cloudflare-credentials-missing',
            'have_token':   bool(token),
            'have_account': bool(account),
        }), 200

    t0 = time.time()
    vec, err = _embed('data center', token, account)
    embed_ms = int((time.time() - t0) * 1000)
    if vec is None:
        return jsonify({'ok': False, 'reason': 'embed-failed',
                        'embed_ms': embed_ms, **(err or {})}), 200

    return jsonify({
        'ok':            True,
        'embed_dims':    len(vec),
        'embed_ms':      embed_ms,
        'index':         VECTORIZE_INDEX,
        'model':         EMBED_MODEL,
        'grids':         sorted(GRID_TERRITORIES.keys()),
        'features': [
            'fuzzy-hydration',
            'grid-filter', 'state-filter',
            'mw-range', 'provider-filter',
            'country-filter', 'status-filter',
        ],
    }), 200


def register_iteration_3_routes(app):
    app.add_url_rule(
        '/api/v1/search/semantic',
        endpoint='it3_semantic_search',
        view_func=semantic_search,
        methods=['GET'],
    )
    app.add_url_rule(
        '/api/v1/search/health',
        endpoint='it3_search_health',
        view_func=semantic_search_health,
        methods=['GET'],
    )
    app.add_url_rule(
        '/api/v1/search/grids',
        endpoint='it3_search_grids',
        view_func=list_grids,
        methods=['GET'],
    )
    logger.info("iteration3 v2 (iteration 4): semantic + health + grids registered "
                "with fuzzy hydration and grid/state/MW filters")
