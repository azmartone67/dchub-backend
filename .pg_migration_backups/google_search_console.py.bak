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
import json
import requests
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from db_utils import get_db

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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        indexed_at TIMESTAMP,
        error TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS gsc_crawl_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        error_type TEXT,
        first_detected TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_detected TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved BOOLEAN DEFAULT FALSE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS gsc_sitemap_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        
        signed_jwt = jwt.encode(payload, sa_info['private_key'], algorithm='RS256')
        
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
        c.execute('''INSERT INTO gsc_sitemap_submissions (sitemap_url, status) VALUES (?, ?)''',
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
                    'position': round(row.get('position', 0), 1)
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
            c.execute('''INSERT INTO gsc_index_requests (url, status) VALUES (?, 'submitted')''', (url,))
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
            c.execute('''INSERT INTO gsc_index_requests (url, status, error) VALUES (?, 'failed', ?)''', 
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
