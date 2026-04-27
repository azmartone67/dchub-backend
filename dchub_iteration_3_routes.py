"""DC Hub iteration 3 — semantic search via Cloudflare Vectorize.

Endpoints registered:

  GET /api/v1/search/semantic?q=<text>&topK=<n>&hydrate=<bool>
      Embed q with @cf/baai/bge-base-en-v1.5, query the dchub-facilities
      Vectorize index, return matches with score + metadata. With
      hydrate=true, each match also gets its full Neon facilities row.

  GET /api/v1/search/health
      Diagnostic: confirms env vars and a quick embedding round-trip.

Required Railway env vars (any of these aliases work):
    CLOUDFLARE_API_TOKEN  | CF_API_TOKEN
    CLOUDFLARE_ACCOUNT_ID | CF_ACCOUNT_ID
"""
from __future__ import annotations

import logging
import os
import time
import urllib.request
import urllib.error
import json as _json
from flask import jsonify, request

logger = logging.getLogger('dchub.iteration3')

VECTORIZE_INDEX = 'dchub-facilities'
EMBED_MODEL     = '@cf/baai/bge-base-en-v1.5'


def _cf_creds():
    token = os.environ.get('CLOUDFLARE_API_TOKEN') or os.environ.get('CF_API_TOKEN')
    account = (os.environ.get('CLOUDFLARE_ACCOUNT_ID')
               or os.environ.get('CF_ACCOUNT_ID')
               or os.environ.get('ACC'))
    return token, account


def _cf_post(url, token, payload, timeout=20):
    """POST JSON to Cloudflare API, return parsed dict + status."""
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


def _hydrate(matches):
    """Augment each match with a full Neon facilities row when name+provider match."""
    if not matches:
        return matches
    try:
        from dchub_iteration_2_routes import _get_pg_conn
    except Exception as e:
        logger.warning("iteration3: hydrate skipped, _get_pg_conn unavailable: %s", e)
        return matches
    try:
        conn = _get_pg_conn()
        cur = conn.cursor()
        for m in matches:
            md = m.get('metadata') or {}
            name = md.get('name')
            prov = md.get('provider')
            if not name:
                continue
            try:
                cur.execute("""
                    SELECT id, slug, name, provider, latitude, longitude, power_mw,
                           status, city, state, country, source, source_url
                    FROM facilities
                    WHERE name = %s AND (%s IS NULL OR provider = %s)
                    LIMIT 1
                """, (name, prov, prov))
                r = cur.fetchone()
                if r:
                    cols = [d[0] for d in cur.description]
                    m['hydrated'] = dict(zip(cols, r))
                    if m['hydrated'].get('power_mw') is not None:
                        m['hydrated']['power_mw'] = float(m['hydrated']['power_mw'])
                    if m['hydrated'].get('latitude') is not None:
                        m['hydrated']['latitude'] = float(m['hydrated']['latitude'])
                    if m['hydrated'].get('longitude') is not None:
                        m['hydrated']['longitude'] = float(m['hydrated']['longitude'])
            except Exception as e:
                logger.warning("iteration3: hydrate row failed for %s: %s", name, e)
                conn.rollback()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("iteration3: hydrate connection failed: %s", e)
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

    token, account = _cf_creds()
    if not (token and account):
        return jsonify({
            'error': 'cloudflare-credentials-missing',
            'message': ('Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in '
                        'Railway env (or CF_API_TOKEN / CF_ACCOUNT_ID).'),
            'have_token':   bool(token),
            'have_account': bool(account),
        }), 500

    t_embed_start = time.time()
    vector, err = _embed(q, token, account)
    if vector is None:
        return jsonify({'error': 'embed-failed', **(err or {})}), 502
    embed_ms = int((time.time() - t_embed_start) * 1000)

    t_query_start = time.time()
    matches, err = _vector_query(vector, topK, token, account)
    if matches is None:
        return jsonify({'error': 'vectorize-query-failed', 'embed_ms': embed_ms, **(err or {})}), 502
    query_ms = int((time.time() - t_query_start) * 1000)

    if hydrate_flag:
        matches = _hydrate(matches)

    flat = []
    for m in matches:
        md = m.get('metadata') or {}
        flat.append({
            'id':        m.get('id'),
            'score':     m.get('score'),
            'name':      md.get('name'),
            'provider':  md.get('provider'),
            'city':      md.get('city'),
            'state':     md.get('state'),
            'country':   md.get('country'),
            'lat':       md.get('lat'),
            'lng':       md.get('lng'),
            'power_mw':  md.get('power_mw'),
            'status':    md.get('status'),
            'hydrated':  m.get('hydrated'),
        })

    return jsonify({
        'query':       q,
        'topK':        topK,
        'count':       len(flat),
        'hydrated':    hydrate_flag,
        'matches':     flat,
        'timing_ms': {
            'embed':   embed_ms,
            'query':   query_ms,
            'total':   int((time.time() - started) * 1000),
        },
        'index':       VECTORIZE_INDEX,
        'model':       EMBED_MODEL,
    }), 200


# ============================================================
# /api/v1/search/health  — no q parameter, just verifies wiring
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
        'ok': True,
        'embed_dims': len(vec),
        'embed_ms':   embed_ms,
        'index':      VECTORIZE_INDEX,
        'model':      EMBED_MODEL,
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
    logger.info("iteration3: registered /api/v1/search/semantic and /api/v1/search/health")
