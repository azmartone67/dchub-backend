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


def _admin_auth_ok() -> bool:
    """Admin auth check matching the pattern in routes/twitter_diagnostic.py."""
    from flask import request as _req
    provided = (_req.headers.get('X-Admin-Key')
                or _req.args.get('admin_key') or '').strip()
    expected = (os.environ.get('DCHUB_ADMIN_KEY')
                or os.environ.get('DCHUB_INTERNAL_KEY') or '').strip()
    return bool(expected) and provided == expected


@publisher_status_bp.route('/api/v1/admin/dchub-media/<platform>/drain-now', methods=['POST'])
def drain_now(platform):
    """Pop the oldest queued post for {platform} (linkedin|twitter|bluesky)
    and fire it RIGHT NOW, bypassing the 6h initial-sleep gate. Returns the
    real result so the operator sees auth/scope errors immediately instead
    of waiting 6h for the first natural tick.

    Admin-gated because:
      - It triggers actual API spend (one tweet/post)
      - Could be used to flood if abused

    Hard cap: 1 post per call. Rate-limit: 1 call per 60 seconds via
    in-memory token.

    Why this exists: the user was watching publisher-status with all
    loops boot_started=true but last_attempt_result=null because the 6h
    initial sleep hadn't elapsed. This unblocks immediate verification.
    """
    if not _admin_auth_ok():
        return jsonify({
            "ok": False,
            "error": "admin_key_required",
            "hint": "POST with X-Admin-Key header OR ?admin_key= query param "
                    "matching DCHUB_ADMIN_KEY env on Railway. If DCHUB_ADMIN_KEY "
                    "isn't set, set it to any random string in Railway "
                    "resourceful-essence → Variables.",
        }), 401

    platform = (platform or '').strip().lower()
    if platform not in ('linkedin', 'twitter', 'bluesky'):
        return jsonify({"ok": False, "error": "platform_must_be_linkedin_twitter_or_bluesky"}), 400

    # In-memory rate limit (60s)
    import time as _time
    global _LAST_DRAIN_BY_PLATFORM
    try:
        _LAST_DRAIN_BY_PLATFORM
    except NameError:
        _LAST_DRAIN_BY_PLATFORM = {}
    now_ts = _time.monotonic()
    last = _LAST_DRAIN_BY_PLATFORM.get(platform, 0.0)
    if now_ts - last < 60:
        return jsonify({
            "ok": False,
            "error": "rate_limited",
            "retry_after_seconds": int(60 - (now_ts - last)),
        }), 429
    _LAST_DRAIN_BY_PLATFORM[platform] = now_ts

    # Import lazily so the public status endpoint above doesn't pull
    # the whole content_publisher module on every request.
    try:
        import content_publisher as cp
    except Exception as e:
        return jsonify({"ok": False, "error": "import_failed", "detail": str(e)[:200]}), 500

    # Pull oldest queued post
    try:
        conn = cp._get_db()
        with conn.cursor() as cur:
            # Same platform-name conventions as the loops use
            platform_filter = {
                'linkedin': "publish_platform IN ('linkedin','all') OR platform = 'linkedin'",
                'twitter':  "publish_platform IN ('twitter','x','all') OR platform = 'twitter'",
                'bluesky':  "publish_platform IN ('bluesky','all') OR platform = 'bluesky'",
            }[platform]
            # Schema (from content_publisher.py:init_content_tables, line ~84):
            # id, content, platform, status, created_at, posted_at, published_at,
            # publish_platform, bluesky_uri, twitter_id, linkedin_urn, accelerate,
            # viral_score. NO content_text, NO slug, NO generated_at columns.
            cur.execute(f"""
                SELECT id, content, created_at
                  FROM social_media_posts
                 WHERE status = 'approved'
                   AND ({platform_filter})
                 ORDER BY created_at ASC
                 LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return jsonify({
                    "ok": False,
                    "error": "no_queued_posts",
                    "platform": platform,
                    "hint": "Queue is empty for this platform. Nothing to drain.",
                }), 404
            post_id, content_text, created_at = row
            slug = None  # social_media_posts doesn't carry slug — that lives on press_releases
    except Exception as e:
        return jsonify({"ok": False, "error": "db_read_failed",
                        "detail": str(e)[:200]}), 500

    # Fire the platform-specific post function
    poster = {
        'linkedin': '_post_to_linkedin',
        'twitter':  '_post_to_twitter',
        'bluesky':  '_post_to_bluesky',
    }[platform]
    post_fn = getattr(cp, poster, None)
    if not post_fn:
        return jsonify({"ok": False, "error": f"poster_function_missing: {poster}"}), 500

    result = {}
    fire_error = None
    try:
        ret = post_fn(content_text)
        # post functions return either a URN string, a dict with 'urn', or None
        if isinstance(ret, dict):
            urn = ret.get('urn') or ret.get('id') or ret.get('post_urn')
            result = ret
        else:
            urn = ret
            result = {"urn": urn}

        success = bool(urn) and not str(urn).startswith('DRY_RUN')

        # Update DB row
        try:
            with conn.cursor() as cur2:
                cur2.execute("""
                    UPDATE social_media_posts
                       SET status = %s,
                           posted_at = NOW(),
                           publish_platform = %s
                     WHERE id = %s
                """, ('published' if success else 'failed', platform, post_id))
            conn.commit()
        except Exception as e:
            logger.warning(f"[drain-now] DB UPDATE failed: {e}")

        # Persist URN if linkedin (the _persist_linkedin_urn helper exists
        # from the master-shell engagement loop work)
        if success and platform == 'linkedin':
            try:
                cp._persist_linkedin_urn(conn.cursor(), post_id, urn, content_text)
                conn.commit()
            except Exception:
                pass

        # Record into publisher state machine so /publisher-status reflects it
        try:
            cp._record_attempt(platform, 'ok' if success else 'error',
                               error_class=None if success else 'no_urn_returned')
        except Exception:
            pass

        return jsonify({
            "ok": success,
            "platform": platform,
            "post_id": post_id,
            "slug": slug,
            "urn": urn,
            "content_preview": (content_text or '')[:120] + ('…' if content_text and len(content_text) > 120 else ''),
            "status_snapshot": cp.get_publisher_status_snapshot().get('loops', {}).get(platform, {}),
        }), 200 if success else 502

    except Exception as e:
        # Classify the error so the operator sees the same error_class
        # the publisher-status endpoint shows
        err_class = 'unknown'
        msg = str(e)
        if '401' in msg or 'unauthorized' in msg.lower():
            err_class = 'auth_failed'
        elif '403' in msg or 'forbidden' in msg.lower():
            err_class = 'forbidden_scope'
        elif '429' in msg or 'rate' in msg.lower():
            err_class = 'rate_limit'
        elif 'timeout' in msg.lower() or 'timed out' in msg.lower():
            err_class = 'network_timeout'

        try:
            cp._record_attempt(platform, 'error', error_class=err_class)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

        return jsonify({
            "ok": False,
            "platform": platform,
            "post_id": post_id,
            "error_class": err_class,
            "error_detail": msg[:300],
            "hint": {
                'auth_failed': "OAuth token doesn't have the scope it needs. "
                                "For X: regenerate the Access Token AFTER setting "
                                "App permissions to Read+Write.",
                'forbidden_scope': "Token is valid but lacks write permission.",
                'rate_limit': "API rate limit hit — try again in 15 min.",
            }.get(err_class, "Unclassified — paste error_detail back for diagnosis."),
        }), 502


def register_publisher_status(app):
    """Idempotent blueprint registration. Called from main.py alongside
    the other media/publisher blueprints."""
    app.register_blueprint(publisher_status_bp)
    logger.info("Publisher Status (PUBLIC, no auth): GET /api/v1/dchub-media/publisher-status")
    logger.info("Publisher Drain (admin-gated): POST /api/v1/admin/dchub-media/<platform>/drain-now")
