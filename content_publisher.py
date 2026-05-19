"""
Content Publishing Pipeline
Manages draft social media posts and press releases through an approval workflow.
Supports LinkedIn auto-publishing via LINKEDIN_ACCESS_TOKEN.
"""

import os
import sqlite3
import logging
import time
import requests
import threading
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

# phase57_landing — daily landing URL helper for LinkedIn rich-card preview
def _phase30c_landing_url(d=None):
    """Return canonical /api/v1/social/posts/<date> URL for LinkedIn OG card."""
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"


logger = logging.getLogger(__name__)

content_bp = Blueprint('content_publisher', __name__)

DB_PATH = 'dc_nexus.db'

def _get_db(retries=3):
    """Phase RRR-content-publisher-neon (2026-05-18) — MIGRATED from
    sqlite3 to psycopg2/Neon. The SQL queries in this module already
    use %s placeholders (PG style), so they work as-is once the
    connection is PG. On Railway, dc_nexus.db doesn't exist — the old
    sqlite3.connect() was hanging in 30s timeouts and broke 8
    downstream blueprints. Falls back to SQLite ONLY if no Neon URL
    is set (local-dev shim)."""
    last_error = None
    neon_url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if neon_url:
        try:
            import psycopg2
            import psycopg2.extras
            # Use RealDictCursor so cur.fetchone() returns dict (the
            # auto-publish loops reference row['id'], row['content'])
            conn = psycopg2.connect(neon_url, connect_timeout=10,
                                    cursor_factory=psycopg2.extras.RealDictCursor)
            return conn
        except Exception as e:
            logger.warning(f"Neon connect failed, falling back to sqlite: {e}")
            last_error = e
    # Local dev only: SQLite fallback. On Railway this will fail because
    # dc_nexus.db doesn't exist + sqlite3.connect with timeout hangs.
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            return conn
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            last_error = e
            if attempt < retries - 1:
                logger.warning(f"SQLite connect attempt {attempt+1}/{retries} failed: {e}")
                time.sleep(2 * (attempt + 1))
            else:
                logger.error(f"SQLite connect failed after {retries} attempts: {e}")
    raise last_error

def _check_admin(req):
    admin_key = req.headers.get('X-Admin-Key') or req.args.get('admin_key') or req.args.get('key')
    valid_keys = [k for k in [os.environ.get('DCHUB_ADMIN_KEY', '')] if k]
    return admin_key in valid_keys

def init_content_tables():
    """Phase RRR-content-publisher-neon (2026-05-18) — Neon-compatible
    table bootstrap. Creates social_media_posts if missing, then adds
    any missing columns. press_releases is already managed elsewhere
    (routes/press_queue.py etc.) so we leave it alone."""
    conn = _get_db()
    cur = conn.cursor()
    try:
        # social_media_posts — needed by auto-publish loops
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS social_media_posts (
                    id              SERIAL PRIMARY KEY,
                    content         TEXT NOT NULL,
                    platform        TEXT,
                    status          TEXT NOT NULL DEFAULT 'draft',
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    approved_at     TEXT,
                    posted_at       TEXT,
                    published_at    TEXT,
                    publish_platform TEXT,
                    bluesky_uri     TEXT,
                    twitter_id      TEXT,
                    linkedin_urn    TEXT
                )
            """)
            try: conn.commit()
            except Exception: pass
        except Exception as e:
            logger.warning(f"social_media_posts CREATE skipped: {e}")
        # Add missing columns idempotently (cheap on PG with IF NOT EXISTS)
        for col_def in [
            "approved_at TEXT",
            "publish_platform TEXT",
            "published_at TEXT",
            "bluesky_uri TEXT",
            "twitter_id TEXT",
            "linkedin_urn TEXT",
        ]:
            col = col_def.split()[0]
            try:
                cur.execute(f"ALTER TABLE social_media_posts ADD COLUMN IF NOT EXISTS {col_def}")
                try: conn.commit()
                except Exception: pass
            except Exception:
                pass
        logger.info("Content publishing tables initialized (Neon)")
    finally:
        try: conn.close()
        except Exception: pass

@content_bp.route('/api/admin/content/stats', methods=['GET'])
def content_stats():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    conn = _get_db()
    cur = conn.cursor()
    stats = {'draft': 0, 'approved': 0, 'published': 0, 'rejected': 0, 'published_today': 0}
    today = datetime.utcnow().strftime('%Y-%m-%d')
    for table in ['social_media_posts', 'press_releases']:
        for status_val in ['draft', 'approved', 'published', 'rejected']:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE status = %s", (status_val,))
            stats[status_val] += cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE status = 'published' AND published_at LIKE %s", (today + '%',))
        stats['published_today'] += cur.fetchone()[0]
    linkedin_connected = bool(os.environ.get('LINKEDIN_ACCESS_TOKEN', '').strip())
    conn.close()
    return jsonify({'stats': stats, 'linkedin_connected': linkedin_connected})

@content_bp.route('/api/admin/content-queue', methods=['GET'])
def content_queue():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    status_filter = request.args.get('status', 'draft')
    content_type = request.args.get('type', 'social')
    platform_filter = request.args.get('platform', '')
    page = max(1, int(request.args.get('page', 1)))
    limit = min(50, max(1, int(request.args.get('limit', 10))))
    offset = (page - 1) * limit
    conn = _get_db()
    cur = conn.cursor()
    if content_type == 'press':
        base_query = "FROM press_releases WHERE status = %s"
        params = [status_filter]
        if platform_filter:
            base_query += " AND COALESCE(publish_platform, '') = %s"
            params.append(platform_filter)
        cur.execute(f"SELECT COUNT(*) {base_query}", params)
        total = cur.fetchone()[0]
        cur.execute(f"SELECT id, 'press' as type, title || '\\n\\n' || content as content, status, COALESCE(publish_platform, '') as publish_platform, created_at, published_at, approved_at {base_query} ORDER BY created_at DESC LIMIT %s OFFSET %s", params + [limit, offset])
    else:
        base_query = "FROM social_media_posts WHERE status = %s"
        params = [status_filter]
        if platform_filter:
            base_query += " AND platform = %s"
            params.append(platform_filter)
        cur.execute(f"SELECT COUNT(*) {base_query}", params)
        total = cur.fetchone()[0]
        cur.execute(f"SELECT id, 'social' as type, content, status, platform as publish_platform, created_at, COALESCE(posted_at, published_at) as published_at, approved_at {base_query} ORDER BY created_at DESC LIMIT %s OFFSET %s", params + [limit, offset])
    rows = cur.fetchall()
    items = []
    for r in rows:
        items.append({
            'id': r['id'],
            'type': r['type'],
            'content': r['content'],
            'status': r['status'],
            'publish_platform': r['publish_platform'],
            'created_at': r['created_at'],
            'published_at': r['published_at'],
            'approved_at': r['approved_at'] if 'approved_at' in r.keys() else None,
        })
    conn.close()
    return jsonify({'items': items, 'total': total})

@content_bp.route('/api/admin/content/<int:item_id>/approve', methods=['POST'])
def content_approve(item_id):
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    content_type = request.args.get('type', 'social')
    table = 'press_releases' if content_type == 'press' else 'social_media_posts'
    now = datetime.utcnow().isoformat() + 'Z'
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE {table} SET status = 'approved', approved_at = %s WHERE id = %s", (now, item_id))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({'success': False, 'error': 'Not found'}), 404
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@content_bp.route('/api/admin/content/<int:item_id>/reject', methods=['POST'])
def content_reject(item_id):
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    content_type = request.args.get('type', 'social')
    table = 'press_releases' if content_type == 'press' else 'social_media_posts'
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE {table} SET status = 'rejected' WHERE id = %s", (item_id,))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({'success': False, 'error': 'Not found'}), 404
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@content_bp.route('/api/admin/content/<int:item_id>/edit', methods=['POST'])
def content_edit(item_id):
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True)
    new_content = data.get('content', '')
    auto_approve = data.get('auto_approve', False)
    content_type = request.args.get('type', 'social')
    table = 'press_releases' if content_type == 'press' else 'social_media_posts'
    now = datetime.utcnow().isoformat() + 'Z'
    conn = _get_db()
    cur = conn.cursor()
    if auto_approve:
        cur.execute(f"UPDATE {table} SET content = %s, status = 'approved', approved_at = %s WHERE id = %s", (new_content, now, item_id))
    else:
        cur.execute(f"UPDATE {table} SET content = %s WHERE id = %s", (new_content, item_id))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({'success': False, 'error': 'Not found'}), 404
    conn.commit()
    conn.close()
    return jsonify({'success': True})

def _post_to_linkedin(content_text, access_token, article_url=None,
                       article_title=None, article_description=None,
                       article_thumbnail_url=None):
    """Post to DC Hub LinkedIn Company Page (org ID: 110894959).

    Phase HH (2026-05-13): added optional article params. When supplied,
    LinkedIn renders a rich link-card preview with thumbnail + title +
    description (much higher engagement than plain text). Without them,
    falls back to text-only share (legacy behaviour).

    LinkedIn extracts the link-card image from the URL's og:image meta
    tag — buildPressReleaseHtml in _worker.js points that at the
    /api/v1/og/today/<slug>.png dynamic card endpoint.
    """
    DCHUB_ORG_ID = (os.environ.get('LINKEDIN_ORG_ID', '110894959') or '110894959').strip()
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'X-Restli-Protocol-Version': '2.0.0',
    }
    if article_url:
        # Rich link-card share — LinkedIn scrapes the URL for OG tags
        # (title, description, image) and renders a click-through card.
        # We provide hints; LinkedIn uses our values if og:* is missing.
        media_block = {
            "status": "READY",
            "originalUrl": article_url,
        }
        if article_title:
            media_block["title"] = {"text": article_title[:200]}
        if article_description:
            media_block["description"] = {"text": article_description[:300]}
        if article_thumbnail_url:
            # If we know the exact image, hint at it. LinkedIn still
            # scrapes the URL for og:image but this can be a tiebreaker.
            media_block["thumbnails"] = [{"url": article_thumbnail_url}]
        share_content = {
            "shareCommentary": {"text": content_text},
            "shareMediaCategory": "ARTICLE",
            "media": [media_block],
        }
    else:
        # Text-only fallback
        share_content = {
            "shareCommentary": {"text": content_text},
            "shareMediaCategory": "NONE",
        }
    post_body = {
        "author": f"urn:li:organization:{DCHUB_ORG_ID}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": share_content
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }
    resp = requests.post('https://api.linkedin.com/v2/ugcPosts',
                          json=post_body, headers=headers, timeout=15)
    if resp.status_code in (200, 201):
        return True, resp.json().get('id', 'posted')
    return False, f"LinkedIn API error {resp.status_code}: {resp.text[:300]}"


def _delete_linkedin_share(share_urn, access_token):
    """Phase HH (2026-05-13): delete a previously-posted share. Used by
    the /repost-now endpoint when republishing today's press release
    with a new visual card. LinkedIn requires URL-encoding the URN."""
    import urllib.parse as _up
    encoded = _up.quote(share_urn, safe='')
    resp = requests.delete(
        f'https://api.linkedin.com/v2/ugcPosts/{encoded}',
        headers={
            'Authorization': f'Bearer {access_token}',
            'X-Restli-Protocol-Version': '2.0.0',
        },
        timeout=15,
    )
    if resp.status_code in (200, 204):
        return True, "deleted"
    return False, f"LinkedIn delete error {resp.status_code}: {resp.text[:300]}"


def _post_to_twitter(content_text):
    """Post to DC Hub X/Twitter account.

    Phase FF+3 (2026-05-13): added X/Twitter publish path.
    Uses the v2 tweets endpoint with OAuth 2.0 bearer token. Requires:
        TWITTER_BEARER_TOKEN   — user-context OAuth 2.0 token (needs
                                  tweet.write scope, not just read).
    OR for OAuth 1.0a User Context (preferred for posting on behalf
    of @dchubcloud), set all four:
        TWITTER_API_KEY, TWITTER_API_SECRET,
        TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET
    The OAuth 1.0a path uses requests_oauthlib if available; falls
    back to OAuth 2.0 bearer if only the simpler token is set.
    """
    # Try OAuth 1.0a first (the path the X dev platform recommends
    # for posting from a confirmed account).
    # .strip() — env vars pasted via dashboards routinely carry a trailing
    # newline; an unstripped credential silently fails auth (OAuth1 sig
    # mismatch / malformed Bearer header).
    api_key = os.environ.get('TWITTER_API_KEY', '').strip()
    api_sec = os.environ.get('TWITTER_API_SECRET', '').strip()
    acc_tok = os.environ.get('TWITTER_ACCESS_TOKEN', '').strip()
    acc_sec = os.environ.get('TWITTER_ACCESS_SECRET', '').strip()
    if all([api_key, api_sec, acc_tok, acc_sec]):
        try:
            from requests_oauthlib import OAuth1
            auth = OAuth1(api_key, api_sec, acc_tok, acc_sec)
            resp = requests.post(
                'https://api.twitter.com/2/tweets',
                json={'text': content_text[:280]},
                auth=auth,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                data = resp.json().get('data', {})
                return True, data.get('id', 'posted')
            return False, f"X API error {resp.status_code}: {resp.text[:300]}"
        except ImportError:
            # requests_oauthlib isn't installed — fall through to bearer.
            pass
        except Exception as e:
            return False, f"X OAuth1 error: {str(e)[:200]}"

    bearer = os.environ.get('TWITTER_BEARER_TOKEN', '')
    if not bearer:
        return False, "no_twitter_credentials"
    resp = requests.post(
        'https://api.twitter.com/2/tweets',
        json={'text': content_text[:280]},
        headers={'Authorization': f'Bearer {bearer}',
                 'Content-Type': 'application/json'},
        timeout=15,
    )
    if resp.status_code in (200, 201):
        data = resp.json().get('data', {})
        return True, data.get('id', 'posted')
    return False, f"X API error {resp.status_code}: {resp.text[:300]}"

# Phase PP (2026-05-17) — Bluesky AT Protocol publishing.
# DC Hub Media currently amplifies via LinkedIn + email only. Bluesky
# is the fastest-growing dev/research community and has zero competitor
# presence in the data-center-intelligence niche — first-mover advantage.
#
# Auth: BLUESKY_HANDLE + BLUESKY_APP_PASSWORD (app password is generated
# at https://bsky.app/settings/app-passwords — never use account password).
# Free to post unlimited via the public AT Protocol. No approval delay.
def _post_to_bluesky(content_text):
    """Post to DC Hub Bluesky account via AT Protocol.

    Two-step flow:
      1. POST /xrpc/com.atproto.server.createSession with handle + app
         password → returns accessJwt + did
      2. POST /xrpc/com.atproto.repo.createRecord with the jwt + did →
         creates the post in the bsky.feed.post collection

    Bluesky post length cap is 300 graphemes (we truncate to be safe).
    """
    handle  = os.environ.get('BLUESKY_HANDLE', '').strip()
    app_pwd = os.environ.get('BLUESKY_APP_PASSWORD', '').strip()
    if not handle or not app_pwd:
        return False, "no_bluesky_credentials"

    # Step 1 — create session
    try:
        session_resp = requests.post(
            'https://bsky.social/xrpc/com.atproto.server.createSession',
            json={'identifier': handle, 'password': app_pwd},
            timeout=12,
        )
        if session_resp.status_code != 200:
            return False, f"Bluesky session failed {session_resp.status_code}: {session_resp.text[:200]}"
        session = session_resp.json()
        jwt = session.get('accessJwt')
        did = session.get('did')
        if not jwt or not did:
            return False, "Bluesky session missing accessJwt or did"
    except Exception as e:
        return False, f"Bluesky session error: {str(e)[:200]}"

    # Step 2 — create the post record
    try:
        from datetime import datetime as _dt, timezone as _tz
        now_iso = _dt.now(_tz.utc).isoformat().replace('+00:00', 'Z')
        # Bluesky: 300 grapheme limit. Truncate by chars (close enough).
        text = content_text[:297] + '...' if len(content_text) > 300 else content_text
        record_resp = requests.post(
            'https://bsky.social/xrpc/com.atproto.repo.createRecord',
            json={
                'repo':       did,
                'collection': 'app.bsky.feed.post',
                'record':     {
                    'text':       text,
                    'createdAt':  now_iso,
                    '$type':      'app.bsky.feed.post',
                    'langs':      ['en'],
                },
            },
            headers={
                'Authorization': f'Bearer {jwt}',
                'Content-Type':  'application/json',
            },
            timeout=15,
        )
        if record_resp.status_code in (200, 201):
            data = record_resp.json()
            return True, data.get('uri', 'posted')
        return False, f"Bluesky post failed {record_resp.status_code}: {record_resp.text[:200]}"
    except Exception as e:
        return False, f"Bluesky post error: {str(e)[:200]}"


@content_bp.route('/api/admin/publish/bluesky', methods=['POST'])
def publish_bluesky():
    """Admin endpoint: manually push a social_media_posts row to Bluesky.
    Phase PP (2026-05-17) — companion to publish_linkedin / publish_twitter."""
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True) or {}
    post_id = data.get('post_id')
    raw_text = data.get('text', '').strip()

    # Allow either {post_id} (lookup row) OR {text} (one-shot post)
    content_text = raw_text
    conn = None
    if post_id and not raw_text:
        try:
            conn = _get_db()
            cur = conn.cursor()
            cur.execute("SELECT content FROM social_media_posts WHERE id = %s",
                        (post_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'post_not_found'}), 404
            content_text = row[0] or ""
        except Exception as e:
            return jsonify({'success': False, 'error': f'db:{str(e)[:120]}'}), 500
    if not content_text:
        return jsonify({'success': False, 'error': 'post_id_or_text_required'}), 400

    ok, result = _post_to_bluesky(content_text)
    if ok and post_id and conn is not None:
        try:
            cur = conn.cursor()
            from datetime import datetime as _dt2
            now = _dt2.utcnow()
            cur.execute("""UPDATE social_media_posts
                              SET status = %s,
                                  posted_at = %s, published_at = %s,
                                  publish_platform = %s
                            WHERE id = %s""",
                        ('published', now, now, 'bluesky', post_id))
            conn.commit()
        except Exception:
            pass
    if conn is not None:
        try: conn.close()
        except Exception: pass
    return jsonify({
        'success':  ok,
        'platform': 'bluesky',
        'post_id':  post_id,
        'uri':      result if ok else None,
        'error':    None if ok else result,
    }), (200 if ok else 502)


@content_bp.route('/api/admin/publish/linkedin', methods=['POST'])
def publish_linkedin():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True)
    post_id = data.get('post_id')
    if not post_id:
        return jsonify({'success': False, 'error': 'post_id required'}), 400
    access_token = os.environ.get('LINKEDIN_ACCESS_TOKEN', '').strip()
    if not access_token:
        return jsonify({'success': False, 'error': 'LINKEDIN_ACCESS_TOKEN not configured'}), 500
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, content, status, platform FROM social_media_posts WHERE id = %s", (post_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Post not found'}), 404
    if row['status'] not in ('approved', 'draft'):
        conn.close()
        return jsonify({'success': False, 'error': f"Post status is '{row['status']}', must be approved or draft"}), 400
    content_text = row['content']
    success, result = _post_to_linkedin(content_text, access_token)
    now = datetime.utcnow().isoformat() + 'Z'
    if success:
        cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'linkedin' WHERE id = %s", (now, now, post_id))
        conn.commit()
        conn.close()
        logger.info(f"Published post {post_id} to LinkedIn: {result}")
        return jsonify({'success': True, 'linkedin_post_id': result})
    else:
        conn.close()
        logger.warning(f"LinkedIn publish failed for post {post_id}: {result}")
        return jsonify({'success': False, 'error': result})

_auto_publisher_running = False

def start_auto_publisher():
    global _auto_publisher_running
    if _auto_publisher_running:
        return
    _auto_publisher_running = True

    def _auto_publish_loop():
        # Phase FF+7 (2026-05-18): 2-min initial delay (was 6h) so first
        # post lands soon after a container restart. Subsequent loops still
        # honor the 6h cadence.
        logger.info("LinkedIn auto-publisher started (initial 2min, then every 6h, max 3/day)")
        _first = True
        while True:
            try:
                time.sleep(120 if _first else 6 * 3600)
                _first = False
                access_token = os.environ.get('LINKEDIN_ACCESS_TOKEN', '').strip()
                if not access_token:
                    # Phase FF (2026-05-14): make the silent skip loud when
                    # posts are actually piling up undelivered — that's the
                    # actionable signal, vs. a quiet debug when nothing's queued.
                    try:
                        _qc = _get_db(); _qcur = _qc.cursor()
                        _qcur.execute("SELECT COUNT(*) FROM social_media_posts WHERE status = 'approved'")
                        _queued = (_qcur.fetchone() or [0])[0]
                        _qc.close()
                    except Exception:
                        _queued = 0
                    if _queued:
                        logger.warning("Auto-publisher: %s approved post(s) queued but LINKEDIN_ACCESS_TOKEN not set — LinkedIn distribution is DARK", _queued)
                    else:
                        logger.debug("Auto-publisher: No LINKEDIN_ACCESS_TOKEN, skipping")
                    continue
                conn = _get_db()
                cur = conn.cursor()
                today = datetime.utcnow().strftime('%Y-%m-%d')
                cur.execute("SELECT COUNT(*) FROM social_media_posts WHERE status = 'published' AND publish_platform = 'linkedin' AND published_at LIKE %s", (today + '%',))
                published_today = cur.fetchone()[0]
                # Phase FF+7 (2026-05-18): cap raised 2 -> 3/day. With 42
                # queued posts and a 5-day-old oldest, the old 2/day cap
                # meant 21 days to drain. 3/day cuts that to ~14d while
                # staying well below the LinkedIn-spam threshold.
                DAILY_CAP = 3
                if published_today >= DAILY_CAP:
                    logger.info(f"Auto-publisher: Already published {published_today} today, skipping")
                    conn.close()
                    continue
                # Phase FF+7 backlog-drain mode: when there's a real backlog
                # (>10 queued), publish multiple in this cycle to catch up
                # rather than 1 per wake-up.
                cur.execute("SELECT COUNT(*) FROM social_media_posts WHERE status = 'approved'")
                _queued = (cur.fetchone() or [0])[0]
                _drain_budget = (DAILY_CAP - published_today) if _queued > 10 else 1
                _attempts = 0
                while _attempts < _drain_budget:
                    cur.execute("SELECT id, content FROM social_media_posts WHERE status = 'approved' AND platform = 'linkedin' ORDER BY created_at ASC LIMIT 1")
                    row = cur.fetchone()
                    if not row:
                        cur.execute("SELECT id, content FROM social_media_posts WHERE status = 'approved' ORDER BY created_at ASC LIMIT 1")
                        row = cur.fetchone()
                    if not row:
                        logger.debug("Auto-publisher: No approved posts to publish")
                        break
                    post_id = row['id']
                    content_text = row['content']
                    success, result = _post_to_linkedin(content_text, access_token)
                    now = datetime.utcnow().isoformat() + 'Z'
                    if success:
                        cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'linkedin' WHERE id = %s", (now, now, post_id))
                        conn.commit()
                        logger.info(f"Auto-published post {post_id} to LinkedIn (drain {_attempts+1}/{_drain_budget}, queued={_queued})")
                    else:
                        logger.warning(f"Auto-publish failed for post {post_id}: {result}")
                        # Mark as failed so we don't loop on the same broken post
                        try:
                            cur.execute("UPDATE social_media_posts SET status = 'failed' WHERE id = %s", (post_id,))
                            conn.commit()
                        except Exception: pass
                    _attempts += 1
                    if _attempts < _drain_budget:
                        time.sleep(8)  # avoid LinkedIn rate-limit between rapid posts
                conn.close()
            except Exception as e:
                logger.error(f"Auto-publisher error: {e}")

    t = threading.Thread(target=_auto_publish_loop, daemon=True, name="linkedin-auto-publisher")
    t.start()


_twitter_publisher_running = False

def start_twitter_publisher():
    """Phase FF+3 (2026-05-13): parallel auto-publisher for X/Twitter.
    Same shape as the LinkedIn loop. Runs every 6h, max 2/day, gated
    on TWITTER_BEARER_TOKEN OR the OAuth1 quad."""
    global _twitter_publisher_running
    if _twitter_publisher_running:
        return
    _twitter_publisher_running = True

    def _twitter_loop():
        # Phase FF+7 (2026-05-18): 2-min first-run delay so posts go out
        # soon after restart instead of 6h dark.
        logger.info("X/Twitter auto-publisher started (initial 2min, then every 6h, max 3/day)")
        _first = True
        while True:
            try:
                time.sleep(150 if _first else 6 * 3600)
                _first = False
                bearer = os.environ.get('TWITTER_BEARER_TOKEN', '')
                oauth1 = all([os.environ.get(k, '') for k in
                              ('TWITTER_API_KEY', 'TWITTER_API_SECRET',
                               'TWITTER_ACCESS_TOKEN', 'TWITTER_ACCESS_SECRET')])
                if not (bearer or oauth1):
                    # Phase FF (2026-05-14): loud when X posts are queued
                    # but undeliverable; quiet when nothing's waiting.
                    try:
                        _qc = _get_db(); _qcur = _qc.cursor()
                        _qcur.execute("SELECT COUNT(*) FROM social_media_posts WHERE status = 'approved' AND platform = 'twitter'")
                        _queued = (_qcur.fetchone() or [0])[0]
                        _qc.close()
                    except Exception:
                        _queued = 0
                    if _queued:
                        logger.warning("Twitter auto-publisher: %s approved X post(s) queued but no credentials set — X distribution is DARK", _queued)
                    else:
                        logger.debug("Twitter auto-publisher: no credentials, skipping")
                    continue
                conn = _get_db()
                cur = conn.cursor()
                today = datetime.utcnow().strftime('%Y-%m-%d')
                cur.execute("SELECT COUNT(*) FROM social_media_posts WHERE status = 'published' AND publish_platform = 'twitter' AND published_at LIKE %s", (today + '%',))
                pub_today = cur.fetchone()[0]
                if pub_today >= 2:
                    logger.info(f"Twitter auto-publisher: already {pub_today} today, skipping")
                    conn.close()
                    continue
                cur.execute("SELECT id, content FROM social_media_posts WHERE status = 'approved' AND platform = 'twitter' ORDER BY created_at ASC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    logger.debug("Twitter auto-publisher: no approved Twitter posts")
                    conn.close()
                    continue
                post_id = row['id']
                content_text = row['content']
                success, result = _post_to_twitter(content_text)
                now = datetime.utcnow().isoformat() + 'Z'
                if success:
                    cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'twitter' WHERE id = %s", (now, now, post_id))
                    conn.commit()
                    logger.info(f"Auto-published post {post_id} to X")
                else:
                    logger.warning(f"Twitter auto-publish failed for {post_id}: {result}")
                conn.close()
            except Exception as e:
                logger.error(f"Twitter auto-publisher error: {e}")

    t = threading.Thread(target=_twitter_loop, daemon=True,
                         name="twitter-auto-publisher")
    t.start()


# Phase DDD (2026-05-17) — Bluesky auto-publisher loop.
# Phase PP shipped the standalone _post_to_bluesky function. Phase VV
# wired auto-press to enqueue platform='bluesky' rows. Without this loop,
# those rows pile up in social_media_posts forever. Mirrors the LinkedIn
# + Twitter shape: every 6h, max 2/day, gated on BLUESKY_HANDLE +
# BLUESKY_APP_PASSWORD env vars.
_bluesky_publisher_running = False


def start_bluesky_publisher():
    global _bluesky_publisher_running
    if _bluesky_publisher_running:
        return
    _bluesky_publisher_running = True

    def _bsky_loop():
        # Phase FF+7 (2026-05-18): 2-min first-run delay + 3/day cap.
        logger.info("Bluesky auto-publisher started (initial 2min, then every 6h, max 3/day)")
        _first = True
        while True:
            try:
                time.sleep(180 if _first else 6 * 3600)
                _first = False
                handle  = os.environ.get('BLUESKY_HANDLE', '').strip()
                app_pwd = os.environ.get('BLUESKY_APP_PASSWORD', '').strip()
                if not handle or not app_pwd:
                    # Surface the dark state when posts are queued, so
                    # ops can see it's a missing-env problem, not a code bug.
                    try:
                        _qc = _get_db(); _qcur = _qc.cursor()
                        _qcur.execute(
                            "SELECT COUNT(*) FROM social_media_posts "
                            "WHERE status = 'approved' AND platform = 'bluesky'")
                        _queued = (_qcur.fetchone() or [0])[0]
                        _qc.close()
                    except Exception:
                        _queued = 0
                    if _queued:
                        logger.warning("Bluesky auto-publisher: %s approved post(s) queued "
                                        "but BLUESKY_HANDLE/BLUESKY_APP_PASSWORD not set — "
                                        "Bluesky distribution is DARK", _queued)
                    else:
                        logger.debug("Bluesky auto-publisher: no credentials, skipping")
                    continue

                conn = _get_db()
                cur = conn.cursor()
                today = datetime.utcnow().strftime('%Y-%m-%d')
                cur.execute(
                    "SELECT COUNT(*) FROM social_media_posts WHERE status = 'published' "
                    "AND publish_platform = 'bluesky' AND published_at LIKE %s",
                    (today + '%',))
                pub_today = cur.fetchone()[0]
                DAILY_CAP = 3
                if pub_today >= DAILY_CAP:
                    logger.info(f"Bluesky auto-publisher: already {pub_today} today, skipping")
                    conn.close()
                    continue

                # Phase FF+7 (2026-05-18): Bluesky was filtering for
                # platform='bluesky' rows ONLY, but auto-press enqueues with
                # platform='linkedin' by default. Result: Bluesky publisher
                # found 0 rows every cycle and stayed silent (0 posts in 7d
                # despite being configured). Match LinkedIn's pattern: try
                # platform-specific first, fall back to any approved post.
                # Also backlog-drain like LinkedIn.
                cur.execute("SELECT COUNT(*) FROM social_media_posts WHERE status = 'approved'")
                _queued = (cur.fetchone() or [0])[0]
                _drain_budget = (DAILY_CAP - pub_today) if _queued > 10 else 1
                _attempts = 0
                while _attempts < _drain_budget:
                    cur.execute("SELECT id, content FROM social_media_posts "
                                 "WHERE status = 'approved' AND platform = 'bluesky' "
                                 "ORDER BY created_at ASC LIMIT 1")
                    row = cur.fetchone()
                    if not row:
                        # Fallback: any approved post that hasn't been
                        # published to bluesky yet. Re-using a LinkedIn-targeted
                        # post on Bluesky is fine — different audience, same idea.
                        cur.execute(
                            "SELECT id, content FROM social_media_posts "
                            "WHERE status = 'approved' "
                            "AND (publish_platform IS NULL OR publish_platform != 'bluesky') "
                            "ORDER BY created_at ASC LIMIT 1")
                        row = cur.fetchone()
                    if not row:
                        logger.debug("Bluesky auto-publisher: no approved posts")
                        break
                    post_id = row['id']
                    content_text = row['content']
                    ok, result = _post_to_bluesky(content_text)
                    now = datetime.utcnow().isoformat() + 'Z'
                    if ok:
                        cur.execute(
                            "UPDATE social_media_posts SET status = %s, "
                            "       posted_at = %s, published_at = %s, "
                            "       publish_platform = %s WHERE id = %s",
                            ('published', now, now, 'bluesky', post_id))
                        conn.commit()
                        logger.info(f"Auto-published post {post_id} to Bluesky uri={result} (drain {_attempts+1}/{_drain_budget})")
                    else:
                        logger.warning(f"Bluesky auto-publish failed for post {post_id}: {result}")
                        try:
                            cur.execute("UPDATE social_media_posts SET status = 'failed' WHERE id = %s", (post_id,))
                            conn.commit()
                        except Exception: pass
                    _attempts += 1
                    if _attempts < _drain_budget:
                        time.sleep(5)
                conn.close()
            except Exception as e:
                logger.error(f"Bluesky auto-publisher error: {e}")

    t = threading.Thread(target=_bsky_loop, daemon=True,
                         name="bluesky-auto-publisher")
    t.start()


def register_content_publisher(app):
    init_content_tables()
    app.register_blueprint(content_bp)
    # Phase FF+3 (2026-05-13): start both auto-publishers. Each is
    # idempotent on _running flags, so multi-worker boots are safe.
    # Each gate themselves on the relevant env var so unconfigured
    # channels just log a debug and skip.
    try:
        start_auto_publisher()       # LinkedIn
    except Exception as e:
        logger.warning(f"LinkedIn auto-publisher failed to start: {e}")
    try:
        start_twitter_publisher()    # X/Twitter
    except Exception as e:
        logger.warning(f"Twitter auto-publisher failed to start: {e}")
    try:
        start_bluesky_publisher()    # Bluesky (Phase DDD)
    except Exception as e:
        logger.warning(f"Bluesky auto-publisher failed to start: {e}")
    logger.info("Content Publishing Pipeline registered")
    logger.info("   GET  /api/admin/content/stats")
    logger.info("   GET  /api/admin/content-queue")
    logger.info("   POST /api/admin/content/<id>/approve")
    logger.info("   POST /api/admin/content/<id>/reject")
    logger.info("   POST /api/admin/content/<id>/edit")
    logger.info("   POST /api/admin/publish/linkedin")
    logger.info("   POST /api/admin/publish/bluesky")
    logger.info("   Auto-publishers: LinkedIn + X/Twitter + Bluesky (every 6h, max 2/day)")
