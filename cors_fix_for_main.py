############################################################
# CORS FIX — Copy-paste into main.py on Replit
# Fixes all 4 CORS errors from the main Replit app
############################################################


# ══════════════════════════════════════════════════════════
# CHANGE 1: Error handler (around line 1236)
# ══════════════════════════════════════════════════════════
# 
# DELETE these lines:
# ──────────────────
# @app.errorhandler(Exception)
# def handle_error(e):
#     return jsonify({
#         'success': False,
#         'error': str(e)
#     }), 500
#
# PASTE this instead:
# ──────────────────

@app.errorhandler(Exception)
def handle_error(e):
    response = jsonify({
        'success': False,
        'error': str(e)
    })
    response.status_code = getattr(e, 'code', 500)
    origin = request.headers.get('Origin', '')
    if origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return response


# ══════════════════════════════════════════════════════════
# CHANGE 2: after_request handler (around line 3151)
# ══════════════════════════════════════════════════════════
#
# FIND these lines (the first 11 lines of add_security_headers):
# ──────────────────
# @app.after_request
# def add_security_headers(response):
#     """Add security headers, smart caching, CORS safety net, and log API calls"""
#     # CORS safety net - ensures ALL /api/ routes get CORS headers for dchub.cloud
#     # This catches any routes that flask-cors might miss (late-registered, etc.)
#     origin = request.headers.get('Origin', '')
#     if origin in ALLOWED_ORIGINS or origin == '':
#         if 'Access-Control-Allow-Origin' not in response.headers:
#             response.headers['Access-Control-Allow-Origin'] = origin or 'https://dchub.cloud'
#             response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
#             response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
#
# REPLACE with:
# ──────────────────

@app.after_request
def add_security_headers(response):
    """Add security headers, smart caching, CORS safety net, and log API calls"""
    origin = request.headers.get('Origin', '')
    # CORS safety net — catches routes flask-cors misses
    if origin in ALLOWED_ORIGINS or origin == '':
        if 'Access-Control-Allow-Origin' not in response.headers:
            response.headers['Access-Control-Allow-Origin'] = origin or 'https://dchub.cloud'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    # Force CORS on ALL error responses so browsers don't mask 500s as CORS blocks
    if response.status_code >= 400 and origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'

    # ← Keep ALL remaining lines of add_security_headers unchanged
    #   (Security headers, Smart caching, API usage tracking)
