"""
CORS Proxy Routes for DC Hub Backend
=====================================
Proxies CORS-blocked API requests through your Replit backend.

Endpoints:
  GET /api/proxy?url=<encoded_url>  - Proxy any whitelisted URL
  GET /api/proxy/health             - Health check

Installation:
1. Save as cors_proxy_routes.py in your Replit project
2. In main.py, add:

   try:
       from cors_proxy_routes import register_cors_proxy
       logger.info("  ✅ cors_proxy_routes")
   except ImportError as e:
       register_cors_proxy = None
       logger.warning(f"  ⚠️ cors_proxy_routes: {e}")

3. At the bottom of main.py (with other registrations):

   if register_cors_proxy:
       register_cors_proxy(app)
       logger.info("✅ CORS Proxy registered")
"""

from flask import Blueprint, jsonify, request, Response
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Try to import requests
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.warning("⚠️ requests library not available for CORS proxy")

cors_proxy_bp = Blueprint('cors_proxy', __name__, url_prefix='/api/proxy')

# Allowed API domains to proxy (whitelist for security)
ALLOWED_DOMAINS = [
    'maps.nccs.nasa.gov',
    # 'hifld-geoplatform.opendata.arcgis.com',  # HIFLD Open decommissioned Aug 2025
    'geo.dot.gov',  # DOT GeoServer — EIA pipeline data
    'services.arcgis.com',
    'services1.arcgis.com',
    'services2.arcgis.com',
    'tiles.arcgis.com',
    'usdmdataservices.unl.edu',
    'droughtmonitor.unl.edu',
    'api.eia.gov',
    'overpass-api.de',
    'overpass.kumi.systems',
    'hazards.fema.gov',
    'fwspublicservices.wim.usgs.gov',
    'earthquake.usgs.gov',
    'www.peeringdb.com',
    'peeringdb.com',
    'opendata.arcgis.com',
]

# Simple rate limiting
_request_counts = {}
RATE_LIMIT = 100  # requests per minute
RATE_WINDOW = 60  # seconds

def is_rate_limited(ip):
    now = datetime.now().timestamp()
    if ip not in _request_counts:
        _request_counts[ip] = {'count': 1, 'start': now}
        return False
    
    record = _request_counts[ip]
    if now - record['start'] > RATE_WINDOW:
        _request_counts[ip] = {'count': 1, 'start': now}
        return False
    
    if record['count'] >= RATE_LIMIT:
        return True
    
    record['count'] += 1
    return False

def is_allowed_domain(url):
    """Check if URL domain is in whitelist"""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname
        return any(
            hostname == domain or hostname.endswith('.' + domain)
            for domain in ALLOWED_DOMAINS
        )
    except:
        return False


# AUTO-REPAIR: duplicate route '/health' also in main.py:3855 — review and remove one
@cors_proxy_bp.route('/health', methods=['GET'])
def proxy_health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'service': 'DC Hub CORS Proxy (Replit)',
        'version': '1.0.0',
        'requests_available': REQUESTS_AVAILABLE,
        'allowed_domains': len(ALLOWED_DOMAINS)
    })


@cors_proxy_bp.route('', methods=['GET', 'POST', 'OPTIONS'])
def proxy_request():
    """
    Main proxy endpoint
    Usage: /api/proxy?url=<encoded_url>
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = Response('')
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 204
    
    if not REQUESTS_AVAILABLE:
        return jsonify({
            'error': True,
            'message': 'Proxy unavailable - requests library not installed'
        }), 503
    
    # Rate limiting
    client_ip = request.remote_addr or 'unknown'
    if is_rate_limited(client_ip):
        return jsonify({
            'error': True,
            'message': 'Rate limit exceeded. Try again in 1 minute.'
        }), 429
    
    # Get target URL
    target_url = request.args.get('url')
    if not target_url:
        return jsonify({
            'error': True,
            'message': 'Missing "url" query parameter',
            'usage': '/api/proxy?url=<encoded_url>'
        }), 400
    
    # Decode URL
    try:
        from urllib.parse import unquote
        decoded_url = unquote(target_url)
    except Exception as e:
        return jsonify({
            'error': True,
            'message': f'Invalid URL encoding: {e}'
        }), 400
    
    # Security check
    if not is_allowed_domain(decoded_url):
        return jsonify({
            'error': True,
            'message': 'Domain not in whitelist',
            'allowed_domains': ALLOWED_DOMAINS
        }), 403
    
    # Proxy the request
    try:
        headers = {
            'User-Agent': 'DC-Hub-Proxy/1.0',
            'Accept': 'application/json, */*'
        }
        
        if request.method == 'POST':
            resp = requests.post(decoded_url, headers=headers, data=request.get_data(), timeout=30)
        else:
            resp = requests.get(decoded_url, headers=headers, timeout=30)
        
        # Create response with CORS headers
        response = Response(resp.content)
        response.headers['Content-Type'] = resp.headers.get('Content-Type', 'application/json')
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['X-Proxy-Status'] = 'success'
        response.headers['X-Original-Status'] = str(resp.status_code)
        
        return response, resp.status_code
        
    except requests.exceptions.Timeout:
        return jsonify({
            'error': True,
            'message': 'Request timed out'
        }), 504
    except requests.exceptions.RequestException as e:
        return jsonify({
            'error': True,
            'message': f'Proxy request failed: {str(e)}'
        }), 502
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return jsonify({
            'error': True,
            'message': f'Proxy error: {str(e)}'
        }), 500


def register_cors_proxy(app):
    """Register CORS proxy routes with Flask app"""
    app.register_blueprint(cors_proxy_bp)
    logger.info("✅ CORS Proxy routes registered")
    logger.info("   GET /api/proxy?url=<encoded_url>")
    logger.info("   GET /api/proxy/health")
