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
The `loops` / `env_gates` / `deadman` sections are a pure in-memory dict read —
sub-millisecond, no DB, no network.

★2026-08-25: `last_publish` DOES read the DB, behind a 60s process cache, so
the public worst case is one cheap MAX() per platform per minute.

WHY THAT WAS WORTH THE QUERY
----------------------------
Every section above `last_publish` reports the state of THE PROCESS THAT
ANSWERS. The web replica never publishes — the worker holds the leader lock —
so a public read of this endpoint returned, verbatim on 2026-08-25:

    "linkedin": {"attempts_24h": 0, "boot_disabled_reason": "not publish leader"}

for all three platforms, while LinkedIn was in fact publishing 2-3 posts a day.
The honest reading of that payload was "we cannot tell from outside whether
Twitter and Bluesky are dark" — an observability gap, not evidence.

The publish record is DURABLE and replica-independent: it is rows in
social_media_posts. content_publisher._DEADMAN_SUCCESS_SQL already knows how to
ask, per platform, for all three. `last_publish` reuses THAT map rather than
writing a second definition of "published", so the watchdog and this surface
can never disagree about what counts.
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


# ── DB-durable publish record (2026-08-25) ──────────────────────────────────
# The in-memory sections above describe the PROCESS THAT ANSWERS. This one
# describes what actually got published, from rows, so any replica gives the
# same answer.
_LAST_PUBLISH_TTL_S = 60
_last_publish_cache = {"at": 0.0, "value": None}

# Platforms this surface reports on. Kept explicit rather than derived from the
# SQL map's keys so a platform silently disappearing from that map shows up
# here as "unknown", not as a platform that quietly stopped being watched.
_PLATFORMS = ("linkedin", "twitter", "bluesky")


def _last_publish_uncached() -> dict:
    """{platform: {last_success_at, age_hours}} from the DB, or a note.

    ★ Reuses content_publisher._DEADMAN_SUCCESS_SQL — ONE definition of what
      counts as a publish, shared with the 72h-silence watchdog.
    ★ Fail-open: any import/DB problem yields a `note`, never an exception and
      never a fabricated timestamp. A missing answer must not read as "dark".
    """
    import datetime as _dt
    out = {}
    try:
        from content_publisher import _DEADMAN_SUCCESS_SQL, _deadman_last_db_success
    except Exception as e:                 # noqa: BLE001
        return {"note": f"publish record unavailable ({type(e).__name__})"}
    try:
        import psycopg2
        dsn = (os.environ.get('DATABASE_URL') or '').strip()
        if not dsn:
            return {"note": "publish record unavailable (no DATABASE_URL)"}
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                now = _dt.datetime.now(_dt.timezone.utc)
                for plat in _PLATFORMS:
                    if plat not in _DEADMAN_SUCCESS_SQL:
                        out[plat] = {"status": "unknown",
                                     "note": "no publish query defined for this platform"}
                        continue
                    ts = _deadman_last_db_success(cur, plat)
                    if ts is None:
                        # ★ NEVER published is not the same as STOPPED. A
                        # platform that has never once published has no
                        # silence to measure — say so rather than reporting
                        # an infinite age that reads as a failing loop.
                        out[plat] = {"last_success_at": None,
                                     "age_hours": None,
                                     "status": "never_published"}
                        continue
                    age = (now - ts).total_seconds() / 3600.0
                    out[plat] = {"last_success_at": ts.isoformat(),
                                 "age_hours": round(age, 1),
                                 "status": "publishing" if age <= 72 else "silent_over_72h"}
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:                 # noqa: BLE001
        logger.warning("publisher-status: publish-record read failed: %s", type(e).__name__)
        return {"note": f"publish record unavailable ({type(e).__name__})"}
    return out


def _last_publish() -> dict:
    """60s-cached wrapper. The payload carries cache_age_s so a reader can tell
    a fresh answer from a cached one — a cold cache returning a dict is not
    evidence of freshness."""
    import time as _time
    try:
        now = _time.time()
        cached = _last_publish_cache.get("value")
        age = now - float(_last_publish_cache.get("at") or 0)
        if cached is None or age >= _LAST_PUBLISH_TTL_S:
            cached = _last_publish_uncached()
            _last_publish_cache["value"] = cached
            _last_publish_cache["at"] = now
            age = 0.0
        return {"cache_age_s": round(age, 1), "source": "social_media_posts (DB)",
                "by_platform": cached}
    except Exception as e:                 # noqa: BLE001
        # ★ This section is the newest thing on a PUBLIC endpoint that four
        # other sections depend on. It must not be able to 500 the surface it
        # was added to improve.
        logger.warning("publisher-status: last_publish wrapper failed: %s", type(e).__name__)
        return {"cache_age_s": None, "source": "social_media_posts (DB)",
                "by_platform": {"note": f"publish record unavailable ({type(e).__name__})"}}


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

    # ITEM deadman (2026-07-02): the snapshot carries a "deadman" section —
    # the DB-durable 72h-silence watchdog (per-platform last_db_success_at +
    # silent_hours + fired flag, maintained by run_publisher_deadman_check in
    # content_publisher.py). Lift it out of the per-platform loops dict so it
    # renders as its own top-level payload section. Pure in-memory read; no
    # secrets (timestamps + booleans + coarse error class only).
    deadman = None
    try:
        if isinstance(loops, dict):
            deadman = loops.pop("deadman", None)
    except Exception:
        deadman = None

    payload = {
        "as_of":     datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "public":    True,
        "leaks_secrets": False,
        "loops":     loops,
        "env_gates": env_gates,
        "deadman":   deadman or {"note": "deadman state unavailable (snapshot failed or no check has run yet)"},
        # ★ The only section that survives being answered by a non-publishing
        # replica. Read this one before concluding a platform is dark.
        "last_publish": _last_publish(),
        "reading_note": (
            "loops/env_gates/deadman describe THIS process — a web replica "
            "reports 'not publish leader' and zero attempts even while the "
            "worker publishes normally. last_publish is DB-durable and "
            "replica-independent; it is the section that answers 'did this "
            "platform actually publish?'."),
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
            # Cursor may return a tuple OR a dict-like row depending on cursor_factory.
            # Handle both so the row → variables unpack doesn't pick up column NAMES
            # instead of values (which was the "content_preview: 'content'" bug).
            if isinstance(row, dict) or hasattr(row, 'keys'):
                post_id = row['id']
                content_text = row['content']
                created_at = row['created_at']
            else:
                post_id, content_text, created_at = row[0], row[1], row[2]
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
        # _post_to_twitter (and the LinkedIn/Bluesky equivalents) actually return
        # a (success_bool, message_str) tuple — not a URN string or dict as I'd
        # assumed. Handle all three shapes defensively.
        urn = None
        api_error_msg = None
        if isinstance(ret, tuple) and len(ret) >= 2:
            # (success, msg_or_urn)
            success_flag, second = ret[0], ret[1]
            if success_flag:
                urn = second  # message contains the URN/ID on success
            else:
                api_error_msg = second  # error message
            result = {"success": success_flag, "detail": second}
        elif isinstance(ret, dict):
            urn = ret.get('urn') or ret.get('id') or ret.get('post_urn')
            result = ret
        else:
            urn = ret
            result = {"urn": urn}

        success = bool(urn) and not str(urn).startswith('DRY_RUN')

        # If the post function returned a (False, "X API error 403: ...") tuple,
        # raise it so the catch-block classifies and surfaces it properly
        # instead of returning ok:true with a tuple in the urn field.
        if not success and api_error_msg:
            raise RuntimeError(api_error_msg)

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
                # r-xid (2026-07-18): persist the tweet id like the loop path
                # does — the radar keys x_publisher_dead health on it.
                if success and platform == 'twitter' and urn:
                    cur2.execute(
                        "UPDATE social_media_posts SET twitter_id = %s WHERE id = %s",
                        (str(urn)[:64], post_id))
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
