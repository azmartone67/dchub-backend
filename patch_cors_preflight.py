#!/usr/bin/env python3
"""
Run on Replit: python3 patch_cors_preflight.py

QA fix: Adds OPTIONS preflight handler to Flask so browsers can make
cross-origin requests to Railway from dchub.cloud without CORS errors.

Inserts a @app.before_request handler that responds to OPTIONS with 204 + CORS headers.
Safe to run multiple times — checks before inserting.
"""

content = open('main.py').read()

PREFLIGHT_BLOCK = '''
# ── CORS preflight handler (added by QA patch) ──────────────────────────────
@app.before_request
def handle_cors_preflight():
    if request.method == "OPTIONS":
        from flask import make_response
        resp = make_response("", 204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Admin-Key'
        resp.headers['Access-Control-Max-Age'] = '86400'
        return resp
# ─────────────────────────────────────────────────────────────────────────────
'''

if 'handle_cors_preflight' in content:
    print('ℹ️  CORS preflight handler already present — no change needed')
else:
    # Insert just before the first @app.route
    idx = content.find('@app.route')
    if idx == -1:
        print('❌  Could not find @app.route — insert manually')
    else:
        content = content[:idx] + PREFLIGHT_BLOCK + content[idx:]
        open('main.py', 'w').write(content)
        print('✅  CORS preflight handler added')
        print('   Run: git add main.py && git commit -m "fix: CORS OPTIONS preflight handler" && git push')
