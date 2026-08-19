"""
Google Search Console Integration for DC Hub
=============================================
Provides automated sitemap submission, indexing status tracking,
and index request functionality via Google Search Console API.

Setup Requirements:
1. Create a Google Cloud project
2. Enable Search Console API
3. Create a service account and download JSON key
4. Add service account email to Search Console as owner
5. Set GOOGLE_SERVICE_ACCOUNT_JSON environment variable

Endpoints:
- GET  /api/gsc/status - Verification and connection status
- POST /api/gsc/verify - Initiate domain verification
- POST /api/gsc/sitemap/submit - Submit sitemap.xml
- GET  /api/gsc/sitemap/status - Check sitemap status
- GET  /api/gsc/indexing - Get indexing statistics
- POST /api/gsc/indexing/request - Request indexing for a URL
- GET  /api/gsc/errors - Get crawl errors
"""

from flask import Blueprint, request, jsonify
import os
import re
import json
import requests
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from db_utils import get_db
from internal_auth import require_internal_or_admin

gsc_bp = Blueprint('google_search_console', __name__)

SITE_URL = os.environ.get('SITE_URL', 'https://dchub.cloud')
GSC_SITE_URL = 'sc-domain:dchub.cloud'
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')
DB_PATH = 'dchub.db'

_cached_token = None
_token_expiry = None

def init_gsc_tables():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS gsc_index_requests (
        id SERIAL PRIMARY KEY,
        url TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        indexed_at TIMESTAMP,
        error TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS gsc_crawl_errors (
        id SERIAL PRIMARY KEY,
        url TEXT NOT NULL,
        error_type TEXT,
        first_detected TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_detected TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved BOOLEAN DEFAULT FALSE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS gsc_sitemap_submissions (
        id SERIAL PRIMARY KEY,
        sitemap_url TEXT NOT NULL,
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending',
        urls_submitted INTEGER DEFAULT 0,
        urls_indexed INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()
    print("✅ Google Search Console tables initialized")

def get_access_token():
    global _cached_token, _token_expiry
    
    if _cached_token and _token_expiry and datetime.now() < _token_expiry:
        return _cached_token
    
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    
    try:
        import jwt
        import time
        
        if os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
            with open(GOOGLE_SERVICE_ACCOUNT_JSON, 'r') as f:
                sa_info = json.load(f)
        else:
            sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        
        now = int(time.time())
        payload = {
            'iss': sa_info['client_email'],
            'sub': sa_info['client_email'],
            'aud': 'https://oauth2.googleapis.com/token',
            'iat': now,
            'exp': now + 3600,
            'scope': 'https://www.googleapis.com/auth/webmasters https://www.googleapis.com/auth/indexing'
        }
        
        # Service-account private key is loaded at runtime from a JSON blob
        # provided via env var (GOOGLE_SERVICE_ACCOUNT_JSON) — never hardcoded.
        signing_material = sa_info['private_key']  # nosemgrep: jwt-python-hardcoded-secret
        signed_jwt = jwt.encode(payload, signing_material, algorithm='RS256')
        
        response = requests.post('https://oauth2.googleapis.com/token', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': signed_jwt
        })
        
        if response.status_code == 200:
            token_data = response.json()
            _cached_token = token_data['access_token']
            _token_expiry = datetime.now() + timedelta(seconds=token_data.get('expires_in', 3600) - 60)
            return _cached_token
        else:
            print(f"⚠️ GSC token error: {response.text}")
            return None
            
    except Exception as e:
        print(f"⚠️ GSC auth error: {e}")
        return None

def require_gsc_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Fail-closed CALLER gate before minting the server->Google token. Covers
        # submit / delete / indexing-request writes AND the GET reads in one place.
        # get_access_token() only authenticates the SERVER to Google, never the
        # caller. The daily cron uses a DIFFERENT, already-admin-gated endpoint
        # (main.py /api/v1/admin/gsc/submit-sitemap -> auto_submit_sitemap), so
        # this does not touch it.
        if not require_internal_or_admin(request):
            return jsonify({'success': False, 'error': 'unauthorized'}), 401
        token = get_access_token()
        if not token:
            return jsonify({
                'success': False,
                'error': 'Google Search Console not configured',
                'setup_instructions': {
                    'step1': 'Create Google Cloud project and enable Search Console API',
                    'step2': 'Create service account and download JSON key',
                    'step3': 'Add service account email to Search Console as owner',
                    'step4': 'Set GOOGLE_SERVICE_ACCOUNT_JSON secret with the JSON content'
                }
            }), 503
        return f(token, *args, **kwargs)
    return decorated

@gsc_bp.route('/api/gsc/status', methods=['GET'])
def gsc_status():
    if not require_internal_or_admin(request):
        return jsonify({'success': False, 'error': 'unauthorized'}), 401
    token = get_access_token()
    
    status = {
        'configured': bool(GOOGLE_SERVICE_ACCOUNT_JSON),
        'authenticated': bool(token),
        'site_url': GSC_SITE_URL,
        'verified': False,
        'sitemaps': [],
        'last_crawl': None
    }
    
    if token:
        try:
            site_encoded = GSC_SITE_URL.replace(':', '%3A').replace('/', '%2F')
            response = requests.get(
                f'https://www.googleapis.com/webmasters/v3/sites/{site_encoded}',
                headers={'Authorization': f'Bearer {token}'}
            )
            if response.status_code == 200:
                site_data = response.json()
                status['verified'] = True
                status['permission_level'] = site_data.get('permissionLevel', 'unknown')
            
            sm_response = requests.get(
                f'https://www.googleapis.com/webmasters/v3/sites/{site_encoded}/sitemaps',
                headers={'Authorization': f'Bearer {token}'}
            )
            if sm_response.status_code == 200:
                sm_data = sm_response.json()
                status['sitemaps'] = sm_data.get('sitemap', [])
                
        except Exception as e:
            status['error'] = str(e)
    
    return jsonify(status)

@gsc_bp.route('/api/gsc/verify', methods=['POST'])
def gsc_verify():
    # Fail-closed CALLER gate BEFORE get_access_token(). This route is not
    # decorated with @require_gsc_auth because it has bespoke 'no token -> manual
    # verification_options' behavior that must still run for a legit admin; so the
    # gate is inline. On success it adds SITE_URL to the Search Console property.
    if not require_internal_or_admin(request):
        return jsonify({'success': False, 'error': 'unauthorized'}), 401
    token = get_access_token()
    
    if not token:
        verification_options = {
            'dns_method': {
                'type': 'DNS TXT Record',
                'instructions': [
                    'Add a TXT record to your DNS',
                    'Record name: @ or dchub.cloud',
                    'Record value: Will be provided by Google Search Console',
                    'Verify in Google Search Console manually'
                ]
            },
            'html_file_method': {
                'type': 'HTML File Upload',
                'instructions': [
                    'Download verification HTML file from Google Search Console',
                    'Upload to your site root (e.g., /googleXXXXXXXX.html)',
                    'Verify in Google Search Console'
                ],
                'auto_generated_endpoint': '/google-site-verification.html'
            },
            'meta_tag_method': {
                'type': 'Meta Tag',
                'instructions': [
                    'Get meta tag from Google Search Console',
                    'Add to <head> section of your homepage',
                    'Set GOOGLE_SITE_VERIFICATION environment variable'
                ]
            }
        }
        
        return jsonify({
            'success': False,
            'message': 'Service account not configured - use manual verification',
            'verification_options': verification_options
        })
    
    try:
        response = requests.post(
            'https://www.googleapis.com/webmasters/v3/sites',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            },
            json={'siteUrl': SITE_URL}
        )
        
        if response.status_code in [200, 204]:
            return jsonify({
                'success': True,
                'message': f'Site {SITE_URL} added to Search Console',
                'next_step': 'Complete verification in Google Search Console'
            })
        else:
            return jsonify({
                'success': False,
                'error': response.text
            }), response.status_code
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@gsc_bp.route('/api/gsc/sitemap/submit', methods=['POST'])
@require_gsc_auth
def submit_sitemap(token):
    data = request.get_json() or {}
    sitemap_url = data.get('sitemap_url', f'{SITE_URL}/sitemap.xml')
    
    try:
        site_encoded = GSC_SITE_URL.replace(':', '%3A').replace('/', '%2F')
        sitemap_encoded = sitemap_url.replace(':', '%3A').replace('/', '%2F')
        
        response = requests.put(
            f'https://www.googleapis.com/webmasters/v3/sites/{site_encoded}/sitemaps/{sitemap_encoded}',
            headers={'Authorization': f'Bearer {token}'}
        )
        
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO gsc_sitemap_submissions (sitemap_url, status) VALUES (%s, %s)''',
                  (sitemap_url, 'submitted' if response.status_code in [200, 204] else 'failed'))
        conn.commit()
        conn.close()
        
        if response.status_code in [200, 204]:
            return jsonify({
                'success': True,
                'message': f'Sitemap {sitemap_url} submitted successfully',
                'sitemap_url': sitemap_url
            })
        else:
            return jsonify({
                'success': False,
                'error': response.text
            }), response.status_code
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@gsc_bp.route('/api/gsc/sitemap/delete', methods=['POST', 'DELETE'])
@require_gsc_auth
def delete_sitemap(token):
    """Remove a sitemap submission from Google Search Console.

    Reversible (a sitemap can always be re-submitted via /api/gsc/sitemap/submit).
    Intended for retiring redundant/legacy sitemaps (e.g. a www.* variant that just
    301s to the apex) so crawl budget isn't spent re-reading them. Does NOT remove
    any URLs from the index — only unsubscribes the sitemap file.

    Body or query: {"sitemap_url": "https://www.dchub.cloud/sitemap.xml"} (required —
    no default, so we never delete the canonical apex sitemap by accident).
    """
    data = request.get_json(silent=True) or {}
    sitemap_url = data.get('sitemap_url') or request.args.get('sitemap_url')
    if not sitemap_url:
        return jsonify({'success': False, 'error': "missing 'sitemap_url'"}), 400

    try:
        site_encoded = GSC_SITE_URL.replace(':', '%3A').replace('/', '%2F')
        sitemap_encoded = sitemap_url.replace(':', '%3A').replace('/', '%2F')

        response = requests.delete(
            f'https://www.googleapis.com/webmasters/v3/sites/{site_encoded}/sitemaps/{sitemap_encoded}',
            headers={'Authorization': f'Bearer {token}'}
        )

        if response.status_code in [200, 204]:
            return jsonify({
                'success': True,
                'message': f'Sitemap {sitemap_url} deleted',
                'sitemap_url': sitemap_url
            })
        return jsonify({
            'success': False,
            'error': response.text,
            'status': response.status_code
        }), response.status_code

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@gsc_bp.route('/api/gsc/sitemap/status', methods=['GET'])
@require_gsc_auth
def sitemap_status(token):
    try:
        site_encoded = GSC_SITE_URL.replace(':', '%3A').replace('/', '%2F')
        
        response = requests.get(
            f'https://www.googleapis.com/webmasters/v3/sites/{site_encoded}/sitemaps',
            headers={'Authorization': f'Bearer {token}'}
        )
        
        if response.status_code == 200:
            data = response.json()
            sitemaps = []
            for sm in data.get('sitemap', []):
                sitemaps.append({
                    'path': sm.get('path'),
                    'last_submitted': sm.get('lastSubmitted'),
                    'last_downloaded': sm.get('lastDownloaded'),
                    'is_pending': sm.get('isPending', False),
                    'is_sitemaps_index': sm.get('isSitemapsIndex', False),
                    'warnings': sm.get('warnings', 0),
                    'errors': sm.get('errors', 0),
                    'contents': sm.get('contents', [])
                })
            
            return jsonify({
                'success': True,
                'sitemaps': sitemaps,
                'total': len(sitemaps)
            })
        else:
            return jsonify({'success': False, 'error': response.text}), response.status_code
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@gsc_bp.route('/api/gsc/indexing', methods=['GET'])
@require_gsc_auth
def indexing_status(token):
    try:
        site_encoded = GSC_SITE_URL.replace(':', '%3A').replace('/', '%2F')
        
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=28)).strftime('%Y-%m-%d')
        
        response = requests.post(
            f'https://www.googleapis.com/webmasters/v3/sites/{site_encoded}/searchAnalytics/query',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            },
            json={
                'startDate': start_date,
                'endDate': end_date,
                'dimensions': ['page'],
                'rowLimit': 100
            }
        )
        
        if response.status_code == 200:
            data = response.json()
            pages = []
            total_clicks = 0
            total_impressions = 0
            
            for row in data.get('rows', []):
                page_data = {
                    'url': row['keys'][0],
                    'clicks': row.get('clicks', 0),
                    'impressions': row.get('impressions', 0),
                    'ctr': round(row.get('ctr', 0) * 100, 2),
                    'position': round(row.get('position', 0) or 0, 1)
                }
                pages.append(page_data)
                total_clicks += page_data['clicks']
                total_impressions += page_data['impressions']
            
            return jsonify({
                'success': True,
                'period': {'start': start_date, 'end': end_date},
                'summary': {
                    'total_clicks': total_clicks,
                    'total_impressions': total_impressions,
                    'indexed_pages': len(pages)
                },
                'pages': pages
            })
        else:
            return jsonify({'success': False, 'error': response.text}), response.status_code
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@gsc_bp.route('/api/gsc/indexing/request', methods=['POST'])
@require_gsc_auth  
def request_indexing(token):
    data = request.get_json() or {}
    url = data.get('url')
    
    if not url:
        return jsonify({'success': False, 'error': 'URL required'}), 400
    
    if not url.startswith(SITE_URL):
        url = f'{SITE_URL}{url}' if url.startswith('/') else f'{SITE_URL}/{url}'
    
    try:
        response = requests.post(
            'https://indexing.googleapis.com/v3/urlNotifications:publish',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            },
            json={
                'url': url,
                'type': 'URL_UPDATED'
            }
        )
        
        conn = get_db()
        c = conn.cursor()
        
        if response.status_code == 200:
            c.execute('''INSERT INTO gsc_index_requests (url, status) VALUES (%s, 'submitted')''', (url,))
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True,
                'message': f'Indexing requested for {url}',
                'url': url,
                'response': response.json()
            })
        else:
            error_msg = response.text
            c.execute('''INSERT INTO gsc_index_requests (url, status, error) VALUES (%s, 'failed', %s)''', 
                      (url, error_msg))
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': False,
                'error': error_msg,
                'note': 'Indexing API has daily quotas. Consider using sitemap submission instead.'
            }), response.status_code
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@gsc_bp.route('/api/gsc/errors', methods=['GET'])
@require_gsc_auth
def crawl_errors(token):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''SELECT url, error_type, first_detected, last_detected, resolved 
                     FROM gsc_crawl_errors ORDER BY last_detected DESC LIMIT 100''')
        errors = []
        for row in c.fetchall():
            errors.append({
                'url': row[0],
                'error_type': row[1],
                'first_detected': row[2],
                'last_detected': row[3],
                'resolved': bool(row[4])
            })
        conn.close()
        
        return jsonify({
            'success': True,
            'errors': errors,
            'total': len(errors),
            'note': 'Crawl errors are synced from Search Console periodically'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@gsc_bp.route('/api/gsc/index-requests', methods=['GET'])
def get_index_requests():
    if not require_internal_or_admin(request):
        return jsonify({'success': False, 'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''SELECT id, url, status, requested_at, indexed_at, error 
                     FROM gsc_index_requests ORDER BY requested_at DESC LIMIT 50''')
        requests_list = []
        for row in c.fetchall():
            requests_list.append({
                'id': row[0],
                'url': row[1],
                'status': row[2],
                'requested_at': row[3],
                'indexed_at': row[4],
                'error': row[5]
            })
        conn.close()
        
        return jsonify({
            'success': True,
            'requests': requests_list,
            'total': len(requests_list)
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------------------------------------------------------
# r-proven-exempt (2026-08-19) — the proven-rankable set the sitemap reads.
#
# main.py's capacity gate (r-thin-sitemap, 2026-08-14) drops any facility URL
# without power_mw. Measured 2026-08-19 that proxy is wrong where it counts: of
# the GSC top-100 pages, the ones the gate DROPPED rank at a median position of
# 8.0 versus 8.2 for the ones it kept, and 13 already-page-1 pages were removed.
# This table is the correction — every facility URL Google has actually served
# an impression for, which _build_sitemap_sections readmits past the gate.
#
# ★ IMPRESSIONS, not clicks. A page at position 8 with impressions and no
#   clicks IS the CTR problem; dropping it from the sitemap forecloses fixing
#   it. GSC only reports a page once it has served an impression, so the row's
#   existence is the signal.
# ★ Refresh is ADDITIVE — it upserts and never deletes. A page that drops out
#   of the trailing window keeps its exemption, because "Google stopped showing
#   it this month" is not evidence it cannot rank, and a shrinking sitemap is
#   the failure mode r-thin-sitemap's own floor exists to catch.
# ---------------------------------------------------------------------------

GSC_PROVEN_TABLE = 'seo_proven_pages'
_FACILITY_URL_RE = re.compile(r'^https?://[^/]+/facilities/([^/?#]+)/?$')

# ★ Must stay in lockstep with main.py's _SITEMAP_PROVEN_MIN_IMPRESSIONS —
#   main.py is the enforcer, this is only for reporting, and a status endpoint
#   that quotes a different threshold than the sitemap applies is worse than
#   one that quotes none. tests/test_sitemap_thin_gate.py pins the two
#   defaults together so they cannot drift apart silently.
PROVEN_MIN_IMPRESSIONS_DEFAULT = 10


def _proven_min_impressions():
    try:
        v = int(str(os.environ.get('SITEMAP_PROVEN_MIN_IMPRESSIONS', '')).strip())
        return v if v > 0 else PROVEN_MIN_IMPRESSIONS_DEFAULT
    except Exception:
        return PROVEN_MIN_IMPRESSIONS_DEFAULT

# ★ ONE string literal, on purpose. scripts/regression_lint.py matches
#   INSERT\s+INTO\s+(\w+)[^;"']* — the character class stops dead at the first
#   quote, so an ON CONFLICT living in a later concatenated fragment is
#   invisible to it and the statement reads as a non-idempotent insert. Keeping
#   the whole statement inside one triple-quoted literal makes the idempotency
#   visible to the linter as well as to a reader. {values} is the only brace.
_PROVEN_UPSERT_SQL = """INSERT INTO seo_proven_pages
    (slug, url, impressions, clicks, position)
VALUES {values}
ON CONFLICT (slug) DO UPDATE SET
    impressions = GREATEST(seo_proven_pages.impressions, EXCLUDED.impressions),
    clicks      = GREATEST(seo_proven_pages.clicks, EXCLUDED.clicks),
    position    = EXCLUDED.position,
    url         = EXCLUDED.url,
    last_seen   = CURRENT_DATE,
    updated_at  = NOW()"""


def _ensure_proven_table():
    """DDL through the ONE blessed path. A PGCursorWrapper silently swallows
    CREATE TABLE whenever SKIP_DDL is set — and it defaults to '1' on Railway,
    which is how mcp_sessions stayed missing for three months (#2196)."""
    from db_utils import ddl_cursor
    with ddl_cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS seo_proven_pages (
            slug         TEXT PRIMARY KEY,
            url          TEXT,
            impressions  INTEGER NOT NULL DEFAULT 0,
            clicks       INTEGER NOT NULL DEFAULT 0,
            position     REAL,
            first_proven DATE NOT NULL DEFAULT CURRENT_DATE,
            last_seen    DATE NOT NULL DEFAULT CURRENT_DATE,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_seo_proven_impressions "
                    "ON seo_proven_pages (impressions DESC)")


def refresh_proven_pages(token, days=90, row_limit=25000, max_pages=10):
    """Pull every facility URL with GSC impressions and upsert it.

    Paginated: searchAnalytics caps a response at rowLimit, so a single call
    silently truncates to the top N by clicks — which would omit exactly the
    high-impression/low-click pages this exists to protect."""
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    site_encoded = GSC_SITE_URL.replace(':', '%3A').replace('/', '%2F')

    rows, start_row = [], 0
    for _ in range(max_pages):
        resp = requests.post(
            f'https://www.googleapis.com/webmasters/v3/sites/{site_encoded}/searchAnalytics/query',
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'},
            json={'startDate': start_date, 'endDate': end_date,
                  'dimensions': ['page'], 'rowLimit': row_limit,
                  'startRow': start_row},
            timeout=90)
        if resp.status_code != 200:
            return {'success': False, 'error': resp.text[:500],
                    'status': resp.status_code}
        batch = resp.json().get('rows', []) or []
        rows.extend(batch)
        if len(batch) < row_limit:
            break
        start_row += len(batch)

    # url -> slug, keeping only real facility profile URLs. /facilities itself
    # and /facilities/<country>/<page> are hub pages, not gated rows.
    seen = {}
    for r in rows:
        keys = r.get('keys') or []
        if not keys:
            continue
        m = _FACILITY_URL_RE.match(keys[0].strip())
        if not m:
            continue
        slug = m.group(1)
        imp = int(r.get('impressions', 0) or 0)
        if imp <= 0:
            continue
        prev = seen.get(slug)
        if prev is None or imp > prev['impressions']:
            seen[slug] = {'url': keys[0], 'impressions': imp,
                          'clicks': int(r.get('clicks', 0) or 0),
                          'position': round(r.get('position', 0) or 0, 2)}

    if not seen:
        return {'success': True, 'facility_urls_with_impressions': 0,
                'upserted': 0, 'window_days': days,
                'note': 'no facility URLs had impressions in the window'}

    _ensure_proven_table()

    conn = get_db()
    try:
        c = conn.cursor()
        # ★ The RAW psycopg2 cursor, not the wrapper. The wrapper probes
        #   SELECT lastval() after any INSERT without RETURNING, and this table
        #   has a TEXT primary key and no sequence — lastval() is undefined, PG
        #   errors, and the open transaction aborts, taking the next statement
        #   with it. Raw also skips the SQLite-ism translator.
        raw = getattr(c, '_cur', c)
        payload = [(s, v['url'], v['impressions'], v['clicks'], v['position'])
                   for s, v in seen.items()]
        # Chunked multi-row VALUES — executemany through the wrapper is a
        # per-row Python loop (one Neon round-trip each).
        upserted = 0
        for i in range(0, len(payload), 500):
            chunk = payload[i:i + 500]
            args = ','.join(['(%s,%s,%s,%s,%s)'] * len(chunk))
            flat = [field for p in chunk for field in p]
            raw.execute(_PROVEN_UPSERT_SQL.format(values=args), flat)
            upserted += len(chunk)
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass

    return {'success': True, 'window_days': days,
            'gsc_rows_scanned': len(rows),
            'facility_urls_with_impressions': len(seen),
            'upserted': upserted,
            'note': 'main.py readmits these past the r-thin-sitemap capacity gate'}


@gsc_bp.route('/api/gsc/proven/refresh', methods=['POST'])
@require_gsc_auth
def proven_refresh(token):
    try:
        days = int(request.args.get('days', 90))
    except Exception:
        days = 90
    try:
        result = refresh_proven_pages(token, days=max(1, min(days, 480)))
        return jsonify(result), (200 if result.get('success') else 502)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@gsc_bp.route('/api/gsc/proven', methods=['GET'])
def proven_status():
    """Read-only: what the sitemap will actually readmit.

    ★ A MISSING TABLE IS 200, NOT 5xx. Before the first refresh the table does
    not exist, which is a normal pre-activation state — the sitemap is designed
    to fail closed to it. Returning 500 made that read as an outage: the CF
    worker turns an origin 5xx into its failover body ("Backend unreachable and
    no cached data available"), so the one endpoint you would check to find out
    whether the feature is live reported the site as down instead. Observed
    2026-08-19 immediately after deploy."""
    if not require_internal_or_admin(request):
        return jsonify({'success': False, 'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute("SELECT to_regclass('public.seo_proven_pages')")
            _reg = c.fetchone()
            if not (_reg and _reg[0]):
                return jsonify({
                    'success': True, 'initialised': False, 'proven_pages': 0,
                    'note': 'seo_proven_pages does not exist yet — the sitemap '
                            'is running on the capacity gate alone (fail-closed, '
                            'this is not an error)',
                    'next': 'POST /api/gsc/proven/refresh'}), 200
            c.execute("SELECT COUNT(*), COALESCE(SUM(clicks),0), "
                      "       COALESCE(SUM(impressions),0), MAX(updated_at) "
                      "FROM seo_proven_pages")
            row = c.fetchone() or (0, 0, 0, None)
            # What the sitemap will ACTUALLY readmit, not just what was stored:
            # the threshold is the policy and the cap is only a backstop.
            c.execute("SELECT COUNT(*) FROM seo_proven_pages WHERE impressions >= %s",
                      (_proven_min_impressions(),))
            qualifying = (c.fetchone() or (0,))[0]
            c.execute("SELECT slug, impressions, clicks, position "
                      "FROM seo_proven_pages ORDER BY clicks DESC LIMIT 20")
            top = [{'slug': r[0], 'impressions': r[1], 'clicks': r[2],
                    'position': r[3]} for r in (c.fetchall() or [])]
        finally:
            try: conn.close()
            except Exception: pass
        return jsonify({'success': True, 'initialised': True,
                        'proven_pages': row[0],
                        'qualifying_at_threshold': qualifying,
                        'min_impressions': _proven_min_impressions(),
                        'total_clicks': row[1], 'total_impressions': row[2],
                        'last_refreshed': str(row[3]) if row[3] else None,
                        'top': top})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def auto_submit_sitemap():
    token = get_access_token()
    if not token:
        return {'success': False, 'error': 'Not configured'}
    
    sitemap_url = f'{SITE_URL}/sitemap.xml'
    
    try:
        site_encoded = GSC_SITE_URL.replace(':', '%3A').replace('/', '%2F')
        sitemap_encoded = sitemap_url.replace(':', '%3A').replace('/', '%2F')
        
        response = requests.put(
            f'https://www.googleapis.com/webmasters/v3/sites/{site_encoded}/sitemaps/{sitemap_encoded}',
            headers={'Authorization': f'Bearer {token}'}
        )
        
        return {
            'success': response.status_code in [200, 204],
            'sitemap_url': sitemap_url,
            'status_code': response.status_code
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}

def register_gsc_routes(app):
    init_gsc_tables()
    app.register_blueprint(gsc_bp)
    print("🔍 Google Search Console API registered:")
    print("   GET  /api/gsc/status - Connection status")
    print("   POST /api/gsc/verify - Initiate verification")
    print("   POST /api/gsc/sitemap/submit - Submit sitemap")
    print("   GET  /api/gsc/sitemap/status - Sitemap status")
    print("   GET  /api/gsc/indexing - Indexing statistics")
    print("   POST /api/gsc/indexing/request - Request URL indexing")
    print("   GET  /api/gsc/errors - Crawl errors")
    return gsc_bp
