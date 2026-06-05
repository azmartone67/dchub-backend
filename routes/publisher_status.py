"""
publisher_status.py — PUBLIC operational status for the three social
publisher loops (LinkedIn, Twitter/X, Bluesky).

2026-06-05: built so the user (and any operator) can verify the publisher
config WITHOUT going through DCHUB_ADMIN_KEY. The existing admin-gated
endpoints (/api/admin/content/*) require the admin key, which has been
creating friction during config debugging.

WHY PUBLIC IS SAFE
------------------
This endpoint reports OPERATIONAL state only:
  - boot_started (loop thread alive yes/no)
  - boot_disabled_reason (which env gate short-circuited start)
  - last_attempt_at / last_attempt_result (timestamp + coarse outcome class)
  - last_error_class (e.g. 'auth_failed', 'rate_limit' — NOT the raw message)
  - attempts_24h / successes_24h / errors_24h (in-memory counters)
  - env_gates booleans (whether env vars are SET, never their values)

It does NOT expose:
  - any access token, app password, OAuth secret, or env-var value
  - post content, post IDs, queue contents, URNs
  - raw exception messages (which can leak tokens/URLs)
  - DB rows
  - any user/account info

ENDPOINTS
---------
GET /api/v1/dchub-media/publisher-status   (PUBLIC, no auth)

PERFORMANCE
-----------
Pure in-memory dict read — sub-millisecond. No DB query, no network call.
Cache headers prevent CDN caching (state is real-time).
"""

import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, make_response

logger = logging.getLogger(__name__)

publisher_status_bp = Blueprint('publisher_status', __name__)


def _env_set(name: str) -> bool:
    """Return True iff the env var is present AND non-empty after .strip()."""
    try:
        return bool((os.environ.get(name, '') or '').strip())
    except Exception:
        return False


def _twitter_oauth1_quad_set() -> bool:
    """All four Twitter OAuth1 credentials must be set + non-empty for the
    quad to be usable (the publisher's `oauth1 = all([...])` check is
    truthy on non-empty strings, so we mirror that semantic exactly)."""
    keys = ('TWITTER_API_KEY', 'TWITTER_API_SECRET',
            'TWITTER_ACCESS_TOKEN', 'TWITTER_ACCESS_SECRET')
    return all(_env_set(k) for k in keys)


def _bluesky_creds_set() -> bool:
    return _env_set('BLUESKY_HANDLE') and _env_set('BLUESKY_APP_PASSWORD')


@publisher_status_bp.route('/api/v1/dchub-media/publisher-status', methods=['GET'])
def publisher_status():
    """PUBLIC — runtime state of the three publisher loops.

    Intentionally exposes NO secrets (see module docstring). Designed for
    the operator to verify config quickly without an admin key.
    """
    try:
        from content_publisher import get_publisher_status_snapshot
        loops = get_publisher_status_snapshot()
    except Exception as e:
        # If content_publisher failed to load (e.g. import error during
        # boot), report empty loops + the error class so the operator
        # knows something upstream broke.
        logger.warning("publisher-status: snapshot fetch failed: %s", e)
        loops = {
            "linkedin": {"boot_started": False, "boot_disabled_reason": "snapshot_unavailable"},
            "twitter":  {"boot_started": False, "boot_disabled_reason": "snapshot_unavailable"},
            "bluesky":  {"boot_started": False, "boot_disabled_reason": "snapshot_unavailable"},
        }

    env_gates = {
        "DCHUB_AUTOPUB_LEGACY_set":      _env_set('DCHUB_AUTOPUB_LEGACY'),
        "TWITTER_PUBLISHER_ENABLED_set": _env_set('TWITTER_PUBLISHER_ENABLED'),
        "linkedin_token_set":            _env_set('LINKEDIN_ACCESS_TOKEN'),
        "twitter_oauth1_quad_set":       _twitter_oauth1_quad_set(),
        "twitter_bearer_set":            _env_set('TWITTER_BEARER_TOKEN'),
        "bluesky_creds_set":             _bluesky_creds_set(),
    }

    payload = {
        "as_of":     datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "public":    True,
        "leaks_secrets": False,
        "loops":     loops,
        "env_gates": env_gates,
    }

    resp = make_response(jsonify(payload), 200)
    # State is real-time; bypass any CDN cache. Avoids the dchub.cloud
    # 1h edge-cache trap (see ~/dchub-backend/HEALTH_BASELINE.md).
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma']        = 'no-cache'
    return resp


def register_publisher_status(app):
    """Idempotent blueprint registration. Called from main.py alongside
    the other media/publisher blueprints."""
    app.register_blueprint(publisher_status_bp)
    logger.info("Publisher Status (PUBLIC, no auth): GET /api/v1/dchub-media/publisher-status")
