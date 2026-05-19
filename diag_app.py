#!/usr/bin/env python3
"""
diag_app.py - Startup diagnostic Flask app.
Temporarily replaces main:app in Procfile to surface import errors.
Hit /api/health to see which module is crashing gunicorn workers.
"""
import os
import sys
import traceback
from datetime import datetime
from flask import Flask, jsonify

app = Flask(__name__)
_ERRORS = {}
_OK = []

_MODULES = [
    'api_monetization',
    'dchub_cors_patch',
    'reveal_endpoints',
    'reveal_cell',
    'search_routes',
    'crawler_scheduler',
    'nlr_intelligence',
    'free_tier_gate',
    'mcp_gatekeeper',
    'db_utils',
    'health_watchdog',
]

for _mod in _MODULES:
    try:
        __import__(_mod)
        _OK.append(_mod)
    except Exception as _e:
        _ERRORS[_mod] = {
            'type': type(_e).__name__,
            'msg': str(_e),
            'trace': traceback.format_exc()[-800:],
        }


# AUTO-REPAIR: duplicate route '/api/health' also in main.py:11698 — review and remove one
# AUTO-REPAIR: duplicate route '/health' also in index_api.py:516 — review and remove one
# AUTO-REPAIR: duplicate route '/' also in main.py:11687 — review and remove one
@app.route('/api/health')
@app.route('/health')
@app.route('/')
def health():
    return jsonify({
        'status': 'diag_mode',
        'timestamp': datetime.utcnow().isoformat(),
        'errors': _ERRORS,
        'ok': _OK,
        'error_count': len(_ERRORS),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
