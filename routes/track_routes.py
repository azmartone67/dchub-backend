"""
Visit Tracking Routes
=====================
Simple /api/track/visit endpoint for dchub-protection.js analytics.
Fire-and-forget — always returns 200, never blocks the frontend.

Add to main.py:
    from routes.track_routes import track_bp
    app.register_blueprint(track_bp)
"""

from flask import Blueprint, jsonify, request
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

track_bp = Blueprint('track', __name__)


@track_bp.route('/api/track/visit', methods=['POST'])
def track_visit():
    """Record a page visit for analytics. Fire-and-forget."""
    try:
        data = request.get_json(silent=True) or {}
        page = data.get('page', '/')
        referrer = data.get('referrer', '')
        session_id = data.get('session_id', '')

        # Log for now — can be upgraded to Neon insert later
        logger.info(f"📊 Visit: {page} | ref={referrer[:80]} | sid={session_id[:20]}")

        return jsonify({'success': True, 'tracked': True}), 200
    except Exception as e:
        # Never fail — this is analytics, not critical
        return jsonify({'success': True}), 200
