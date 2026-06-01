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

def _extract_og_image_url(page_url):
    """r51 (2026-05-29): scrape <meta property="og:image"> from a URL.

    Returns the absolute og:image URL string or None. Best-effort —
    any failure (network, HTML parse, missing tag) returns None so the
    caller falls back to the ARTICLE-share path (LinkedIn does its own
    OG scrape downstream).

    We need this because the LinkedIn /v2/ugcPosts ARTICLE share has
    a flaky og:image scrape (5 recent posts shipped without any image
    despite valid og:image tags on dchub.cloud/dcpi/<slug>). The fix
    is to FETCH the image server-side and ATTACH the binary directly
    via /rest/images, which LinkedIn renders 100% of the time.
    """
    if not page_url:
        return None
    try:
        import re as _re
        from urllib.parse import urljoin as _urljoin
        r = requests.get(page_url,
                          headers={'User-Agent': 'DCHub-LinkedInPublisher/1.0'},
                          timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text[:200_000]  # cap to first 200KB; meta tags are in <head>
        # Match either property="og:image" or name="og:image", and either
        # attribute order (content="..." first vs property="..." first).
        m = _re.search(
            r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html, _re.IGNORECASE)
        if not m:
            m = _re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
                html, _re.IGNORECASE)
        if not m:
            return None
        img_url = m.group(1).strip()
        if not img_url.startswith(('http://', 'https://')):
            img_url = _urljoin(page_url, img_url)
        return img_url
    except Exception:
        return None


def _fetch_image_bytes_for_linkedin(image_url):
    """r51 (2026-05-29): fetch image bytes for LinkedIn asset upload.

    Returns bytes (or None). Defensive size cap — LinkedIn rejects
    images <1KB (transparent gif fallbacks) and >5MB. Pattern lifted
    from routes/linkedin_quad_daily.py:_fetch_image_bytes (proven
    working in the 4×/day quad publisher).
    """
    if not image_url:
        return None
    try:
        r = requests.get(image_url,
                          headers={'User-Agent': 'DCHub-LinkedInPublisher/1.0'},
                          timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return None
        data = r.content
        if not (1000 < len(data) < 5_000_000):
            return None
        return data
    except Exception:
        return None


def _upload_image_to_linkedin(image_bytes, access_token, org_id):
    """r51 (2026-05-29): upload binary image to LinkedIn, return image URN.

    Uses the MODERN /rest/images?action=initializeUpload flow (the
    same flow linkedin_poster.post_to_linkedin r50 uses for the
    /rest/posts endpoint). Returns urn:li:image:* on success, or None
    on any failure (caller falls back to legacy ARTICLE share — image
    upload is best-effort, never blocks the post).

    Two-step:
      1) POST /rest/images?action=initializeUpload → {uploadUrl, image URN}
      2) PUT bytes to uploadUrl
    """
    if not image_bytes or not access_token or not org_id:
        return None
    try:
        author = f"urn:li:organization:{org_id}"
        init_headers = {
            'Authorization': f'Bearer {access_token}',
            'LinkedIn-Version': '202601',
            'X-Restli-Protocol-Version': '2.0.0',
            'Content-Type': 'application/json',
        }
        init_resp = requests.post(
            'https://api.linkedin.com/rest/images?action=initializeUpload',
            headers=init_headers,
            json={'initializeUploadRequest': {'owner': author}},
            timeout=15,
        )
        if init_resp.status_code not in (200, 201):
            logger.warning(
                "r51 image initializeUpload failed: %s %s",
                init_resp.status_code, init_resp.text[:200])
            return None
        v = (init_resp.json() or {}).get('value', {})
        upload_url = v.get('uploadUrl')
        image_urn = v.get('image')
        if not (upload_url and image_urn):
            logger.warning("r51 init response missing uploadUrl/image: %s", v)
            return None
        put_resp = requests.put(
            upload_url,
            headers={'Authorization': f'Bearer {access_token}'},
            data=image_bytes,
            timeout=30,
        )
        if put_resp.status_code not in (200, 201):
            logger.warning(
                "r51 image PUT failed: %s %s",
                put_resp.status_code, put_resp.text[:200])
            return None
        return image_urn
    except Exception as e:
        logger.warning("r51 image upload exception: %s", e)
        return None


def _og_today_slug_for(article_url):
    """r64 (2026-05-30): derive a slug for the guaranteed OG fallback card
    https://dchub.cloud/api/v1/og/today/<slug>.png.

    The card endpoint (routes/og_cards.py:og_card) NEVER 404s — an unknown
    slug renders the branded _draw_fallback card — so any slug yields a
    valid 1200x630 PNG. We still try to reuse the post's real slug (last
    path segment of article_url, e.g. /news/<slug>, /dcpi/<slug>,
    /markets/<slug>) so a matching press_releases row produces the richer
    per-story card. With no URL we use a stable constant.
    """
    default = 'dchub-intelligence'
    if not article_url:
        return default
    try:
        import re as _re_slug
        from urllib.parse import urlparse as _urlparse
        path = (_urlparse(article_url).path or '').rstrip('/')
        seg = path.rsplit('/', 1)[-1] if path else ''
        # Strip a trailing .png/.html etc. and keep only slug-safe chars.
        seg = seg.split('?', 1)[0].split('#', 1)[0]
        if '.' in seg:
            seg = seg.rsplit('.', 1)[0]
        seg = _re_slug.sub(r'[^a-zA-Z0-9\-]+', '-', seg).strip('-').lower()
        return seg or default
    except Exception:
        return default


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

    r62 (2026-05-29): respects LINKEDIN_PUBLISHER_DRY_RUN env flag.
    When set to "1"/"true"/"yes", the LinkedIn API call is skipped and
    a synthetic urn:li:share:DRY_RUN:<ts> is returned. The caller
    treats this as success (marks the post 'published') so dry-run
    fully exercises the pipeline including dedup classification + row
    state transitions WITHOUT firing an actual share. Useful for:
      • verifying queue draining behaves correctly after a fix
      • previewing the rewritten content of legacy short posts
      • local-dev smoke tests
    Disable by unsetting the env var.

    r51 (2026-05-29): IMAGE-FIRST path. When article_url is present,
    we now try to fetch its og:image and UPLOAD it directly as a
    LinkedIn /rest/images asset, then attach via /rest/posts. That
    renders a proper image-card post (much higher engagement than the
    text-only or scraped-article shares user was seeing — 5 recent
    posts shipped imageless despite valid og:image tags). On ANY
    image failure we fall back to the previous /v2/ugcPosts ARTICLE
    share, which leans on LinkedIn's own OG scrape. So images are
    always best-effort — they never block the post going out.

    Kill-switch: LINKEDIN_ATTACH_IMAGES=0 disables image upload
    entirely (returns to pre-r51 behaviour). Default = enabled.
    """
    _dry = (os.environ.get('LINKEDIN_PUBLISHER_DRY_RUN', '') or '').strip().lower()
    if _dry in ('1', 'true', 'yes', 'on'):
        _preview = (content_text or '')[:240].replace('\n', ' / ')
        # r51: surface what the image-attach path WOULD have done, so dry-run
        # also exercises the og:image resolution (catches "page has no
        # og:image" before the cron actually fires for real).
        _attach_dry = os.environ.get('LINKEDIN_ATTACH_IMAGES', '1').strip() != '0'
        if not _attach_dry:
            _img_preview = "image=skip(LINKEDIN_ATTACH_IMAGES=0)"
        else:
            # r64 (2026-05-30): mirror the live image-attach decision so dry-run
            # surfaces which source the real post WOULD use:
            #   1. og:image on article_url (existing image-first path), else
            #   2. the GUARANTEED /api/v1/og/today/<slug>.png branded card.
            # An image is now MANDATORY (no text-only NONE) unless even the
            # fallback card can't be fetched.
            _og = ((article_thumbnail_url or _extract_og_image_url(article_url))
                   if article_url else None)
            if _og:
                _img_preview = f"image=would-attach-og({_og})"
            else:
                _slug = _og_today_slug_for(article_url)
                _fallback = f"https://dchub.cloud/api/v1/og/today/{_slug}.png"
                _img_preview = f"image=would-attach-fallback-card({_fallback})"
        logger.warning(
            "LINKEDIN_PUBLISHER_DRY_RUN active — NOT posting (would have sent: %s%s · %s)",
            _preview,
            "..." if len(content_text or '') > 240 else "",
            _img_preview,
        )
        return True, f"urn:li:share:DRY_RUN:{int(time.time())}"
    DCHUB_ORG_ID = (os.environ.get('LINKEDIN_ORG_ID', '110894959') or '110894959').strip()

    # r51: IMAGE-FIRST attempt (modern /rest/posts + /rest/images). Gated by
    # env so operator can disable instantly if LinkedIn API misbehaves.
    _attach_images = os.environ.get('LINKEDIN_ATTACH_IMAGES', '1').strip() != '0'
    if _attach_images and article_url:
        # 1. Resolve OG image — caller can pass article_thumbnail_url to skip
        #    the OG-scrape round-trip (used by press-release publisher which
        #    already knows /api/v1/og/today/<slug>.png).
        _og_url = (article_thumbnail_url
                    or _extract_og_image_url(article_url))
        if _og_url:
            _img_bytes = _fetch_image_bytes_for_linkedin(_og_url)
            if _img_bytes:
                _image_urn = _upload_image_to_linkedin(
                    _img_bytes, access_token, DCHUB_ORG_ID)
                if _image_urn:
                    # Modern /rest/posts shape — IMAGE attached, article_url
                    # also appears as a clickable hyperlink in the body text
                    # (LinkedIn linkifies URLs in `commentary` automatically).
                    _h_post = {
                        'Authorization': f'Bearer {access_token}',
                        'LinkedIn-Version': '202601',
                        'X-Restli-Protocol-Version': '2.0.0',
                        'Content-Type': 'application/json',
                    }
                    _payload = {
                        'author': f'urn:li:organization:{DCHUB_ORG_ID}',
                        'commentary': content_text,
                        'visibility': 'PUBLIC',
                        'distribution': {
                            'feedDistribution': 'MAIN_FEED',
                            'targetEntities': [],
                            'thirdPartyDistributionChannels': [],
                        },
                        'lifecycleState': 'PUBLISHED',
                        'content': {
                            'media': {
                                'id': _image_urn,
                                'title': (article_title or 'DC Hub')[:200],
                                'altText': (article_description
                                              or article_title
                                              or 'DC Hub data center intelligence')[:300],
                            }
                        },
                    }
                    try:
                        _r = requests.post(
                            'https://api.linkedin.com/rest/posts',
                            json=_payload, headers=_h_post, timeout=20)
                        if _r.status_code in (200, 201):
                            _urn = (_r.headers.get('x-restli-id')
                                     or _r.headers.get('X-LinkedIn-Id')
                                     or 'posted-with-image')
                            logger.info(
                                "r51 LinkedIn IMAGE post succeeded: urn=%s "
                                "(article=%s og=%s)", _urn, article_url, _og_url)
                            return True, _urn
                        logger.warning(
                            "r51 /rest/posts (image) failed: %s %s — "
                            "falling through to ARTICLE share",
                            _r.status_code, _r.text[:200])
                    except Exception as _e:
                        logger.warning(
                            "r51 /rest/posts exception: %s — falling through", _e)
                else:
                    logger.info(
                        "r51: image upload returned no URN, falling through "
                        "to ARTICLE share for %s", article_url)
            else:
                logger.info(
                    "r51: couldn't fetch image bytes from %s, falling through",
                    _og_url)
        else:
            logger.info(
                "r51: no og:image found on %s, falling through to ARTICLE share",
                article_url)

    # r64 (2026-05-30): MANDATORY-IMAGE fallback. Reaching here means the
    # image-first path did not attach an image (no article_url, no scrape-able
    # og:image, image fetch/upload/POST failed, OR the page — e.g.
    # /news/<slug> and bare DCPI/digest posts — simply has no og:image). Before
    # r64 those all fell to the text-only shareMediaCategory:NONE branch, which
    # is exactly the imageless LinkedIn posts the operator flagged. We now build
    # a GUARANTEED branded card from /api/v1/og/today/<slug>.png (og_cards.py
    # renders a valid 1200x630 PNG for ANY slug — unknown slugs get the DC Hub
    # _draw_fallback card, never a 404) and attach it via the SAME modern
    # /rest/posts media flow as the image-first block above. Only if even this
    # fallback can't be fetched/uploaded/posted do we fall through to NONE.
    #
    # Gated by the same LINKEDIN_ATTACH_IMAGES kill-switch (=0 → skip straight
    # to the legacy path, pre-r51 behaviour). DRY_RUN already returned above.
    if _attach_images:
        _fb_slug = _og_today_slug_for(article_url)
        _fallback = f"https://dchub.cloud/api/v1/og/today/{_fb_slug}.png"
        _fb_bytes = _fetch_image_bytes_for_linkedin(_fallback)
        if _fb_bytes:
            _fb_urn = _upload_image_to_linkedin(
                _fb_bytes, access_token, DCHUB_ORG_ID)
            if _fb_urn:
                _h_post = {
                    'Authorization': f'Bearer {access_token}',
                    'LinkedIn-Version': '202601',
                    'X-Restli-Protocol-Version': '2.0.0',
                    'Content-Type': 'application/json',
                }
                _payload = {
                    'author': f'urn:li:organization:{DCHUB_ORG_ID}',
                    'commentary': content_text,
                    'visibility': 'PUBLIC',
                    'distribution': {
                        'feedDistribution': 'MAIN_FEED',
                        'targetEntities': [],
                        'thirdPartyDistributionChannels': [],
                    },
                    'lifecycleState': 'PUBLISHED',
                    'content': {
                        'media': {
                            'id': _fb_urn,
                            'title': (article_title or 'DC Hub')[:200],
                            'altText': (article_description
                                          or article_title
                                          or 'DC Hub data center intelligence')[:300],
                        }
                    },
                }
                try:
                    _r = requests.post(
                        'https://api.linkedin.com/rest/posts',
                        json=_payload, headers=_h_post, timeout=20)
                    if _r.status_code in (200, 201):
                        _urn = (_r.headers.get('x-restli-id')
                                 or _r.headers.get('X-LinkedIn-Id')
                                 or 'posted-with-fallback-image')
                        logger.info(
                            "r64 LinkedIn FALLBACK-CARD post succeeded: urn=%s "
                            "(article=%s fallback=%s)",
                            _urn, article_url, _fallback)
                        return True, _urn
                    logger.warning(
                        "r64 /rest/posts (fallback card) failed: %s %s — "
                        "falling through to text-only NONE",
                        _r.status_code, _r.text[:200])
                except Exception as _e:
                    logger.warning(
                        "r64 /rest/posts (fallback card) exception: %s — "
                        "falling through to text-only NONE", _e)
            else:
                logger.warning(
                    "r64: fallback-card upload returned no URN (%s), "
                    "falling through to text-only NONE", _fallback)
        else:
            logger.warning(
                "r64: couldn't fetch fallback card bytes from %s, "
                "falling through to text-only NONE", _fallback)
    else:
        logger.info(
            "r64: LINKEDIN_ATTACH_IMAGES=0 — skipping mandatory-image "
            "fallback, using legacy text/ARTICLE path")

    # LEGACY PATH (pre-r51 behaviour, also r51 fallback when image upload
    # fails / is disabled / no URL in body). Builds an ARTICLE share that
    # leans on LinkedIn's own OG:image scrape — flaky but valid.
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
    # r51 (2026-05-29): extract URL from body so manual publishes also get
    # the IMAGE-attach path (otherwise this endpoint sent imageless text-only
    # posts even after the auto-publisher started attaching images).
    _art_url = None
    _art_title = None
    try:
        import re as _re_url
        _m = _re_url.search(r'https?://[^\s)>\]]+', content_text or '')
        if _m:
            _art_url = _m.group(0).rstrip('.,')
            _first_line = (content_text or '').strip().split('\n', 1)[0].strip()
            _art_title = _first_line[:180] or None
    except Exception:
        _art_url = None
        _art_title = None
    success, result = _post_to_linkedin(content_text, access_token,
                                          article_url=_art_url,
                                          article_title=_art_title)
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

# r62 (2026-05-29): legacy short-DCPI-post detector + auto-rewriter.
# Even after r47.38 fixed the generator, the queue still contains rows
# enqueued before the fix. Publisher drains 1 per 6h, so without a
# rewrite gate those legacy posts can keep landing on LinkedIn for
# weeks. Operator flagged seeing "📍 Coeur d'Alene · WECC · DCPI
# verdict: CAUTION / Excess Power: 44.8/100 · Constraint: 41.1/100 /
# Live page: <link>" on dchub-media's LinkedIn — that's the pre-r47.38
# shape draining out. Fix:
#   1. detect the shape (3-5 lines, 'DCPI verdict:' + 'Excess Power:'
#      + 'Live page:' markers).
#   2. parse market/iso/verdict/excess/constraint out of the body.
#   3. rebuild via _shape_linkedin (the post-r47.38 rich shape) so the
#      post that lands on LinkedIn matches what content_enqueue would
#      produce TODAY for the same DCPI signal.
# This rewrite is persisted back to social_media_posts.content before
# publish so the dchub-media admin queue + audit logs reflect the real
# post that went out.
import re as _re_legacy

_LEGACY_SHORT_DCPI = _re_legacy.compile(
    r'^[^\S\r\n]*📍.+·.+·\s*DCPI verdict:\s*(BUILD|CAUTION|AVOID|HOLD|LOW_SIGNAL)',
    _re_legacy.IGNORECASE | _re_legacy.MULTILINE,
)


def _is_legacy_short_dcpi_shape(text: str) -> bool:
    """Detect the pre-r47.38 short DCPI verdict post shape.

    Pattern (rendered):
      📍 Coeur d'Alene · WECC · DCPI verdict: CAUTION
      Excess Power: 44.8/100 · Constraint: 41.1/100
      Live page: https://dchub.cloud/dcpi/<slug>

    Heuristic: matches the pin+verdict header AND contains
    'Excess Power:' (colon-form is legacy; new shape uses
    'Excess Power N/100' without the colon).
    """
    if not text:
        return False
    if not _LEGACY_SHORT_DCPI.search(text):
        return False
    if 'Excess Power:' not in text:
        return False
    # New rich shape is >= 600 chars; legacy is <= 250. If it's long,
    # it's already been rewritten or was always rich.
    return len(text) <= 400


_LEGACY_HEADER = _re_legacy.compile(
    r'📍\s*(.+?)\s*·\s*(.+?)\s*·\s*DCPI verdict:\s*(BUILD|CAUTION|AVOID|HOLD|LOW_SIGNAL)',
    _re_legacy.IGNORECASE,
)
_LEGACY_SCORES = _re_legacy.compile(
    r'Excess Power:\s*([\d.]+)\s*/\s*100\s*·\s*Constraint:\s*([\d.]+)\s*/\s*100',
    _re_legacy.IGNORECASE,
)
_LEGACY_LINK = _re_legacy.compile(
    r'https?://dchub\.cloud/dcpi/([a-z0-9\-]+)',
    _re_legacy.IGNORECASE,
)


def _parse_legacy_short_dcpi(text: str) -> dict | None:
    """Extract market/iso/verdict/excess/constraint/slug from legacy text.
    Returns None if parsing fails (caller should leave post untouched).
    """
    if not text:
        return None
    hdr = _LEGACY_HEADER.search(text)
    if not hdr:
        return None
    name = (hdr.group(1) or '').strip()
    iso = (hdr.group(2) or '').strip()
    verdict = (hdr.group(3) or '').strip().upper()

    excess = 0.0
    constr = 0.0
    sc = _LEGACY_SCORES.search(text)
    if sc:
        try:
            excess = float(sc.group(1) or 0)
            constr = float(sc.group(2) or 0)
        except (TypeError, ValueError):
            pass

    slug = ''
    lk = _LEGACY_LINK.search(text)
    if lk:
        slug = (lk.group(1) or '').strip().lower()
    if not slug:
        # fall back to a slugified market name
        slug = _re_legacy.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

    return {
        'name': name or '?',
        'slug': slug or 'unknown',
        'verdict': verdict or 'HOLD',
        'iso': iso or '?',
        'excess': excess,
        'constraint': constr,
    }


def _rewrite_legacy_to_rich(text: str) -> str | None:
    """Take a legacy short DCPI post, parse it, and return the rich
    shape from routes/content_enqueue._shape_linkedin (single source of
    truth for the DCPI narrative template).

    Returns the new content string on success, None on failure
    (publisher then falls back to the legacy text — never worse than
    before).
    """
    mover = _parse_legacy_short_dcpi(text)
    if not mover:
        return None
    try:
        # Lazy import to avoid circular dep at module load.
        from routes.content_enqueue import _shape_linkedin
    except Exception:
        return None
    try:
        return _shape_linkedin(mover, None)
    except Exception:
        return None


# r42v (2026-05-26): content classification for per-class daily dedup.
# Without this, the publisher could fire 3 "DCPI verdict" posts in a row
# (operator caught Chantilly/Edison/Buffalo AVOID posts at 3-4m apart).
# Classification is lightweight pattern-match on the first ~200 chars.
def _classify_post_for_dedup(text: str) -> str:
    """Return a coarse class tag so we can rate-limit per-class daily.
    Goal: max 1 'dcpi_verdict' per day, max 1 'partnership_invite' per
    day, etc. Returns 'other' for posts that don't match any known class."""
    if not text:
        return "other"
    t = text[:300].lower()
    # 1. DCPI verdict pin posts (📍 X · ISO · DCPI verdict: AVOID...)
    if "dcpi verdict:" in t or ("📍" in text[:30] and "dcpi" in t):
        return "dcpi_verdict"
    # 2. Partnership-track posts (Switzerland model, open invitation)
    if "switzerland model" in t or "open invitation" in t or "partnerships@dchub.cloud" in t:
        return "partnership_invite"
    # 3. Daily intelligence digest
    if "daily intelligence" in t or "daily digest" in t or "🗞" in text[:30]:
        return "daily_digest"
    # 4. MCP / AI-agent integration pitch
    if "mcp server" in t or "mcp api" in t or "ai agent" in t or "score_facility" in t:
        return "mcp_pitch"
    # 5. Per-tool / per-feature press
    if "tony bishop" in t:
        return "tony_bishop"
    # 6. Capacity/coverage milestones
    if "added" in t and "markets" in t:
        return "coverage_milestone"
    return "other"


# ---------------------------------------------------------------------------
# r63 (2026-05-29) — pre-publish media-judgment guard.
#
# WHY: DC Hub Media posted near-duplicate LinkedIn posts that the existing
# per-CLASS dedup (_classify_post_for_dedup + _seen_classes_today) could not
# catch, because:
#   (1) ENTITY-BLINDNESS — "Montréal 65.2 Excess Power BUILD" and "MCP ~142k
#       tool calls" both fall through to the catch-all "other" class, and two
#       "other" posts never collide. The class tag is too coarse.
#   (2) TODAY-ONLY WINDOW — _seen_classes_today is rebuilt every loop from only
#       posts with published_at LIKE today%, so a 2nd Montréal post 13h later
#       crossed the UTC-midnight boundary and saw an empty seen-set.
#   (3) NO ZERO-STAT GUARD — "DC Hub MCP served 0 AI tool calls" was eligible
#       to publish (embarrassing "0 MCP requests" zero-stat post).
#
# This guard is ENTITY-level and time-windowed: it looks at the actual
# market+verdict and the headline metric of each candidate, compares against a
# rolling N-day window of already-published posts (crosses midnight), and
# hard-blocks zero/null headline stats. It is a pre-publish FILTER, not a
# rewrite — fail-open on any error so it can never make distribution worse.
# ---------------------------------------------------------------------------

# Lookback window for entity-level dedup. 5 days sits inside the spec's
# 3-7d band: long enough to stop the "same Montréal BUILD twice this week"
# repeats, short enough that a genuinely-changed verdict can re-post within
# the week.
_DEDUP_LOOKBACK_DAYS = 5

# Headline-metric extractor. Matches the handful of quotable stats the press
# engine leads with (marketing_engine._pick_daily_topic). Each entry yields a
# (metric_label, numeric_value) so we can (a) block zero/null values and
# (b) dedup on the metric label across the lookback window.
_METRIC_PATTERNS = [
    # "DC Hub MCP served 142,318 AI tool calls in the last 24h"
    ("mcp_tool_calls",
     _re_legacy.compile(r'MCP\s+served\s+([\d,]+)\s+(?:AI\s+)?tool\s+calls', _re_legacy.I)),
    # generic "<N> AI tool calls" / "<N> tool calls" fallback (surge posts)
    ("mcp_tool_calls",
     _re_legacy.compile(r'([\d,]+)\s+(?:AI\s+)?tool\s+calls', _re_legacy.I)),
    # "<N> MCP requests" / "<N> MCP API requests" (the 0-stat case)
    ("mcp_requests",
     _re_legacy.compile(r'([\d,]+)\s+MCP(?:\s+API)?\s+requests', _re_legacy.I)),
    # "added 1,204 facilities in the last 7 days" / coverage milestones
    ("coverage_added",
     _re_legacy.compile(r'added\s+([\d,]+)\s+\w+\s+in\s+the\s+last', _re_legacy.I)),
    # "<N> unique (AI )callers/agents"
    ("unique_callers",
     _re_legacy.compile(r'([\d,]+)\s+unique\s+(?:AI\s+)?(?:callers|agents)', _re_legacy.I)),
]


def _post_headline_signature(text: str) -> dict:
    """Extract the entity signature of a post for dedup + zero-stat checks.

    Returns a dict:
      {
        "market_verdict": "montreal|build" | None,   # market slug + verdict
        "metric_label":   "mcp_tool_calls" | None,    # headline stat kind
        "metric_value":   142318.0 | None,            # parsed numeric value
        "zero_stat":      True | False,               # headline stat is 0/null
      }
    Robust across BOTH the structured "📍 X · ISO · DCPI verdict: BUILD" shape
    AND the free-text "Montréal leads the BUILD ranking ..." shape. Never
    raises — returns an all-None signature on any parse failure (fail-open)."""
    sig = {"market_verdict": None, "metric_label": None,
           "metric_value": None, "zero_stat": False}
    if not text:
        return sig
    try:
        # --- market + verdict ------------------------------------------------
        # 1) structured pin header (reuses the legacy DCPI parser regex)
        m = _LEGACY_HEADER.search(text)
        if m:
            name = (m.group(1) or "").strip().lower()
            verdict = (m.group(3) or "").strip().upper()
            slug = _re_legacy.sub(r'[^a-z0-9]+', '-', name).strip('-')
            if slug and verdict:
                sig["market_verdict"] = f"{slug}|{verdict}"
        # 2) /dcpi/<slug> link + a verdict word anywhere in the body
        if not sig["market_verdict"]:
            lk = _LEGACY_LINK.search(text)
            vd = _re_legacy.search(r'\b(BUILD|AVOID|CAUTION|HOLD)\b', text)
            if lk and vd:
                slug = (lk.group(1) or "").strip().lower()
                if slug:
                    sig["market_verdict"] = f"{slug}|{vd.group(1).upper()}"
        # 3) free-text "<Market> leads the BUILD ranking" / "<Market> flagged
        #    AVOID" (the marketing_engine dcpi_leader / dcpi_warning shapes)
        if not sig["market_verdict"]:
            ft = _re_legacy.search(
                r'^\s*([A-Z][\w .\'\-éÉ]{2,40}?)\s+(?:leads the\s+(BUILD|AVOID)\b'
                r'|flagged\s+(BUILD|AVOID)\b)',
                text, _re_legacy.M)
            if ft:
                name = (ft.group(1) or "").strip().lower()
                verdict = (ft.group(2) or ft.group(3) or "").strip().upper()
                slug = _re_legacy.sub(r'[^a-z0-9]+', '-', name).strip('-')
                if slug and verdict:
                    sig["market_verdict"] = f"{slug}|{verdict}"

        # --- headline metric -------------------------------------------------
        for label, pat in _METRIC_PATTERNS:
            mm = pat.search(text)
            if not mm:
                continue
            try:
                val = float((mm.group(1) or "0").replace(",", ""))
            except (TypeError, ValueError):
                continue
            sig["metric_label"] = label
            sig["metric_value"] = val
            if val <= 0:
                sig["zero_stat"] = True
            break
    except Exception:
        # Fail-open: a parse failure must never block legitimate posts.
        return {"market_verdict": None, "metric_label": None,
                "metric_value": None, "zero_stat": False}
    return sig


# ---------------------------------------------------------------------------
# B3 (2026-05-31) — pre-publish QUALITY score.
#
# WHY: the publisher only had EXISTENCE-style dedup (per-class + entity-level
# + zero-stat) before this. That stops *repeats* and *empty* posts, but a
# thin-but-novel post (no number, no link, no recency cue) still sailed
# through. B3 adds a positive QUALITY signal so we publish FEWER, HIGHER-signal
# posts: a post must clear CONTENT_QUALITY_MIN (default 0.5) on a 0..1 scale.
#
# The score reuses signals this module already computes — there is no rich
# "post" object in this pipeline, a post IS its content text (article_url,
# metric and market are all parsed out of the body), so _quality_score(post)
# takes the content string:
#   (a) concrete non-zero stat  → _post_headline_signature().metric_value
#   (b) freshness               → recency phrases / a current-ish year
#   (c) novelty                 → _classify_post_for_dedup() class is the
#                                 existing dedup-class signal; a generic
#                                 "other" post that names no entity is the
#                                 low-novelty case
#   (d) real article_url/link   → the same https?:// regex the publisher uses
#
# This is an ADDITIONAL conservative gate layered into _should_skip_publish;
# it does NOT touch the daily caps. Fail-OPEN for scoring *errors* (if scoring
# itself throws we log and allow — distribution must never dark-hold on a
# bug), but fail-CLOSED for a confidently-computed low score.
# ---------------------------------------------------------------------------
QUALITY_MIN = float(os.environ.get('CONTENT_QUALITY_MIN', '0.5'))

# Phrases that signal the post references something recent (freshness). Kept
# in sync with the cadence language marketing_engine leads with ("in the last
# 24h / 7 days", "today", "this week"). A current-or-recent 4-digit year also
# counts so dated stats ("2026 interconnection queue") score as fresh.
_FRESHNESS_RE = _re_legacy.compile(
    r'\b(?:today|this week|this month|right now|just|latest|breaking|'
    r'in the last\s+\d+\s*(?:h|hr|hrs|hours|d|day|days|week|weeks)|'
    r'last\s+\d+\s*(?:h|hr|hrs|hours|days|weeks)|'
    r'(?:24h|48h|7\s*days|7d|30\s*days|30d))\b',
    _re_legacy.IGNORECASE)
_URL_RE = _re_legacy.compile(r'https?://[^\s)>\]]+', _re_legacy.IGNORECASE)
_RECENT_YEAR_RE = _re_legacy.compile(r'\b(20[2-3]\d)\b')


def _quality_score(post) -> float:
    """Score a candidate post 0.0–1.0 on publish-worthiness. B3 (2026-05-31).

    `post` is the content text (this pipeline has no richer post object —
    see module note above). Four weighted signals, all reusing existing
    extractors so the score stays consistent with the dedup layer:

      (a) concrete non-zero stat   0.35  — _post_headline_signature parses a
                                           numeric headline metric > 0
      (b) freshness                0.20  — references something recent
      (c) novelty                  0.25  — names a concrete entity/metric
                                           (i.e. NOT the catch-all "other"
                                           dedup class with no signature)
      (d) real article_url/link    0.20  — a dchub.cloud (or any http) link

    Returns a float in [0,1]. A short/empty body floors low. Designed to be
    called inside a try/except by the gate so a raising input fails OPEN."""
    text = (post or "").strip()
    if not text:
        return 0.0

    sig = _post_headline_signature(text)
    score = 0.0

    # (a) concrete, non-zero stat. A parsed headline metric > 0 is the
    # strongest signal; a bare number elsewhere in the body is a weaker
    # partial credit (so "added 1,204 facilities" without a recognised
    # metric label still isn't treated as statless).
    mv = sig.get("metric_value")
    if mv is not None and mv > 0:
        score += 0.35
    elif _re_legacy.search(r'\b\d[\d,]*(?:\.\d+)?\b', text):
        score += 0.15

    # (b) freshness — recency phrase or a recent year.
    if _FRESHNESS_RE.search(text) or _RECENT_YEAR_RE.search(text):
        score += 0.20

    # (c) novelty — reuse the existing dedup-class signal. A post that
    # classifies as anything other than the catch-all "other" is about a
    # known, identifiable topic; an "other" post WITH a parsed entity/metric
    # signature still counts. Only a truly generic "other" post (no class,
    # no market_verdict, no metric_label) is treated as low-novelty.
    cls = _classify_post_for_dedup(text)
    has_entity = bool(sig.get("market_verdict") or sig.get("metric_label"))
    if cls != "other" or has_entity:
        score += 0.25

    # (d) real article_url / link.
    if _URL_RE.search(text):
        score += 0.20

    # Clamp (defensive — weights sum to 1.0 but partial-credit paths could
    # in principle nudge over).
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return round(score, 3)


def _should_skip_publish(cur, content_text: str, platform: str):
    """Pre-publish media-judgment filter. Returns (skip: bool, reason: str).

    Decision order (skip on the FIRST hit):
      (q) QUALITY GATE (B3) — skip if _quality_score(content) is below
          CONTENT_QUALITY_MIN (default 0.5). An ADDITIONAL conservative gate
          so we publish fewer, higher-signal posts. Fail-OPEN if scoring
          itself raises (log + allow), fail-CLOSED on a confident low score.
      (b) ZERO-STAT GUARD — never publish a post whose headline metric parses
          to 0/null ("DC Hub MCP served 0 AI tool calls"). These read as
          "the platform did nothing today" and damage credibility.
      (a) ENTITY DEDUP — skip if the SAME market+verdict OR the SAME headline
          metric was already published (this platform) within the last
          _DEDUP_LOOKBACK_DAYS. Uses a rolling time window queried fresh from
          the DB, so it catches near-dupes posted hours apart ACROSS the UTC
          midnight boundary (the bug the today-only seen-set missed).

    FAIL-OPEN: any DB / parse error returns (False, "") so a transient blip
    never dark-holds the publisher. The caller logs the reason when skip=True.
    """
    # (q) QUALITY GATE (B3, 2026-05-31). Computed FIRST so a thin post is
    # filtered before the dedup DB round-trip. Wrapped so a scoring bug
    # NEVER blocks a post (fail-open); a successfully-computed low score
    # DOES block (fail-closed) — that's the whole point of the gate.
    try:
        _q = _quality_score(content_text or "")
    except Exception as _qe:
        logger.warning(
            "B3 quality-score raised (%s) — failing OPEN, allowing post",
            _qe)
        _q = None
    if _q is not None and _q < QUALITY_MIN:
        return True, (f"low quality score {_q:.3f} < {QUALITY_MIN:.3f} "
                      f"(CONTENT_QUALITY_MIN) — refusing thin/low-signal post")

    sig = _post_headline_signature(content_text or "")

    # (b) zero / null headline stat — hard block, no DB needed.
    if sig.get("zero_stat"):
        return True, (f"zero-stat headline ({sig.get('metric_label')}="
                      f"{sig.get('metric_value')}) — refusing to publish a "
                      f"'platform did nothing' post")

    # Nothing entity-identifiable → let the per-class dedup handle it.
    if not sig.get("market_verdict") and not sig.get("metric_label"):
        return False, ""

    # (a) entity-level lookback across the rolling window (crosses midnight).
    try:
        cutoff = (datetime.utcnow()
                  - timedelta(days=_DEDUP_LOOKBACK_DAYS)).isoformat()
        cur.execute(
            "SELECT content FROM social_media_posts "
            "WHERE status = 'published' AND publish_platform = %s "
            "AND published_at >= %s "
            "ORDER BY published_at DESC LIMIT 60",
            (platform, cutoff))
        rows = cur.fetchall() or []
    except Exception:
        # Fail-open on any DB error.
        return False, ""

    for r in rows:
        prev = r.get('content') if hasattr(r, 'get') else (r[0] if r else '')
        psig = _post_headline_signature(prev or "")
        if (sig.get("market_verdict")
                and psig.get("market_verdict") == sig.get("market_verdict")):
            return True, (f"duplicate market+verdict '{sig['market_verdict']}' "
                          f"already posted to {platform} within "
                          f"{_DEDUP_LOOKBACK_DAYS}d")
        if (sig.get("metric_label")
                and psig.get("metric_label") == sig.get("metric_label")):
            return True, (f"duplicate headline metric '{sig['metric_label']}' "
                          f"already posted to {platform} within "
                          f"{_DEDUP_LOOKBACK_DAYS}d")
    return False, ""


# r42z admin: enqueue a custom-authored post and (optionally) publish
# it immediately. Used when operator writes a specific post (e.g. an
# updated press-release announcement) and wants it on the wire NOW,
# bypassing the 6h cron + per-class dedup. The 'publish_now' flag
# triggers /api/admin/publish/linkedin inline so the post hits LinkedIn
# in one round-trip instead of two.
@content_bp.route('/api/admin/publish/enqueue-custom', methods=['POST'])
def enqueue_custom():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True) or {}
    content = (data.get('content') or '').strip()
    platform = (data.get('platform') or 'linkedin').strip().lower()
    publish_now = bool(data.get('publish_now', False))
    if not content or len(content) < 20:
        return jsonify({'success': False,
                        'error': 'content required (min 20 chars)'}), 400
    if platform not in ('linkedin', 'twitter', 'bluesky'):
        return jsonify({'success': False,
                        'error': "platform must be linkedin|twitter|bluesky"}), 400

    conn = _get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO social_media_posts (content, platform, status, created_at)
            VALUES (%s, %s, 'approved', NOW() ON CONFLICT DO NOTHING)
            RETURNING id
        """, (content, platform))
        row = cur.fetchone()
        new_id = row['id'] if hasattr(row, 'get') else (row[0] if row else None)
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return jsonify({'success': False, 'error': str(e)[:200]}), 500

    out = {'success': True, 'post_id': new_id, 'platform': platform,
           'status': 'approved'}

    if publish_now and platform == 'linkedin':
        access_token = os.environ.get('LINKEDIN_ACCESS_TOKEN', '').strip()
        if not access_token:
            out['published'] = False
            out['publish_error'] = 'LINKEDIN_ACCESS_TOKEN not configured'
            try: conn.close()
            except Exception: pass
            return jsonify(out), 200
        # Pull URL hint for rich link-card share (LinkedIn scrapes og:image)
        try:
            import re as _re_url
            _m = _re_url.search(r'https?://[^\s)>\]]+', content)
            _art_url = _m.group(0).rstrip('.,') if _m else None
            _art_title = (content.strip().split('\n', 1)[0].strip())[:180] or None
        except Exception:
            _art_url = None
            _art_title = None
        ok, result = _post_to_linkedin(content, access_token,
                                         article_url=_art_url,
                                         article_title=_art_title)
        now = datetime.utcnow().isoformat() + 'Z'
        if ok:
            cur.execute("""UPDATE social_media_posts
                              SET status = 'published',
                                  posted_at = %s, published_at = %s,
                                  publish_platform = 'linkedin'
                            WHERE id = %s""", (now, now, new_id))
            conn.commit()
            out['published'] = True
            out['linkedin_post_id'] = result
        else:
            out['published'] = False
            out['publish_error'] = result
    try: conn.close()
    except Exception: pass
    return jsonify(out), 200


# r42v admin: bulk-reject queued posts matching a content pattern.
# Used to clean up stale auto-generated content (Tony Bishop reposts,
# targeted partner-attack posts, etc.) without dropping the whole queue.
@content_bp.route('/api/admin/publish/purge-queue', methods=['POST'])
def purge_queue():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True) or {}
    pattern = (data.get('pattern') or '').strip()
    platform = (data.get('platform') or '').strip().lower() or None
    if not pattern:
        return jsonify({'success': False,
                        'error': 'pattern required (text substring, case-insensitive)'}), 400
    if len(pattern) < 3:
        return jsonify({'success': False,
                        'error': 'pattern too short (min 3 chars)'}), 400
    conn = _get_db()
    cur = conn.cursor()
    try:
        if platform:
            cur.execute("""UPDATE social_media_posts
                              SET status = 'rejected'
                            WHERE status IN ('approved', 'draft')
                              AND platform = %s
                              AND content ILIKE %s
                            RETURNING id""",
                        (platform, f'%{pattern}%'))
        else:
            cur.execute("""UPDATE social_media_posts
                              SET status = 'rejected'
                            WHERE status IN ('approved', 'draft')
                              AND content ILIKE %s
                            RETURNING id""",
                        (f'%{pattern}%',))
        rejected_ids = [r['id'] if hasattr(r, 'get') else r[0] for r in (cur.fetchall() or [])]
        conn.commit()
        return jsonify({
            'success': True,
            'pattern': pattern,
            'platform': platform or 'all',
            'rejected_count': len(rejected_ids),
            'rejected_ids': rejected_ids[:50],  # cap for response size
        }), 200
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify({'success': False, 'error': str(e)[:200]}), 500
    finally:
        try: conn.close()
        except Exception: pass


# r62 (2026-05-29): admin tools for the legacy short-DCPI cleanup.

@content_bp.route('/api/admin/publish/sanitize-queue', methods=['POST'])
def sanitize_queue():
    """Bulk-rewrite all queued (approved/draft) LinkedIn posts that
    match the pre-r47.38 short DCPI shape, in-place to the rich shape.

    Without this, the auto-publisher only rewrites posts as it drains
    them (1 per 6h). With 30+ legacy rows queued, the operator would
    still see ugly posts on dchub-media for weeks. This endpoint
    drains the legacy bucket in one shot.

    Set ?dry_run=1 to preview which rows WOULD be rewritten without
    persisting changes. Returns count + first 5 before/after samples.
    """
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    dry_run = (request.args.get('dry_run', '0') or '0').strip().lower() in ('1', 'true', 'yes')
    conn = _get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, content FROM social_media_posts
             WHERE status IN ('approved', 'draft')
               AND (platform = 'linkedin' OR platform IS NULL)
             ORDER BY created_at ASC
             LIMIT 500
        """)
        rows = cur.fetchall() or []
        samples = []
        rewritten_count = 0
        skipped_count = 0
        for r in rows:
            rid = r['id'] if hasattr(r, 'get') else r[0]
            text = r['content'] if hasattr(r, 'get') else r[1]
            if not _is_legacy_short_dcpi_shape(text or ''):
                continue
            new_text = _rewrite_legacy_to_rich(text or '')
            if not new_text or len(new_text) <= len(text or ''):
                skipped_count += 1
                continue
            if len(samples) < 5:
                samples.append({
                    'id': rid,
                    'before_chars': len(text or ''),
                    'after_chars': len(new_text),
                    'before_preview': (text or '')[:160],
                    'after_preview': new_text[:240],
                })
            if not dry_run:
                try:
                    cur.execute(
                        "UPDATE social_media_posts SET content = %s WHERE id = %s",
                        (new_text, rid),
                    )
                except Exception:
                    skipped_count += 1
                    continue
            rewritten_count += 1
        if not dry_run:
            conn.commit()
        return jsonify({
            'success':         True,
            'dry_run':         dry_run,
            'scanned':         len(rows),
            'rewritten_count': rewritten_count,
            'skipped_count':   skipped_count,
            'samples':         samples,
        }), 200
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify({'success': False, 'error': str(e)[:200]}), 500
    finally:
        try: conn.close()
        except Exception: pass


@content_bp.route('/api/admin/publish/preview-rewrite', methods=['GET', 'POST'])
def preview_rewrite():
    """Preview the legacy-to-rich rewrite for a single queued post or
    for arbitrary text. Never writes. Useful for verifying the new
    template renders before flipping the env flag off.

    Usage:
      GET  /api/admin/publish/preview-rewrite?id=123
      POST /api/admin/publish/preview-rewrite  body: {"content": "..."}
    """
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    raw = None
    rid = request.args.get('id') or (request.get_json(silent=True) or {}).get('id')
    if rid:
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'id must be integer'}), 400
        conn = _get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT content FROM social_media_posts WHERE id = %s", (rid_int,))
            r = cur.fetchone()
            if not r:
                return jsonify({'success': False, 'error': 'not_found'}), 404
            raw = r['content'] if hasattr(r, 'get') else r[0]
        finally:
            try: conn.close()
            except Exception: pass
    else:
        body = request.get_json(silent=True) or {}
        raw = body.get('content') or ''

    is_legacy = _is_legacy_short_dcpi_shape(raw or '')
    parsed = _parse_legacy_short_dcpi(raw or '') if is_legacy else None
    rewritten = _rewrite_legacy_to_rich(raw or '') if is_legacy else None
    return jsonify({
        'success':      True,
        'is_legacy':    is_legacy,
        'parsed':       parsed,
        'original':     raw,
        'original_chars':  len(raw or ''),
        'rewritten':    rewritten,
        'rewritten_chars': len(rewritten or '') if rewritten else 0,
        'dry_run_env':  (os.environ.get('LINKEDIN_PUBLISHER_DRY_RUN', '') or '').strip().lower() in ('1','true','yes','on'),
    }), 200


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
        logger.info("LinkedIn auto-publisher started (initial 2min, then every 6h, cap=LINKEDIN_DAILY_CAP default 6/day)")
        _first = True
        while True:
            # Phase FF+7-fix4 (2026-05-19): hard guarantee that every
            # iteration closes its DB connection, even when sub-operations
            # raise. The earlier loop had `conn = _get_db()` then ~50 lines
            # of work and `conn.close()` only in some branches. When an
            # exception fired mid-way (e.g. RealDictCursor KeyError, network
            # blip), the connection leaked. Across 3 publishers × N
            # iterations, this exhausted Neon's pool — every other endpoint
            # then timed out and Railway marked the container unhealthy.
            # 30-min outage 2026-05-19 was likely this. Wrap in try/finally.
            import traceback as _tb
            conn = None
            try:
                time.sleep(120 if _first else 6 * 3600)
                _first = False
                access_token = os.environ.get('LINKEDIN_ACCESS_TOKEN', '').strip()
                if not access_token:
                    # Loud-when-queued, quiet-when-empty surface
                    _queued = 0
                    try:
                        conn = _get_db()
                        _qcur = conn.cursor()
                        _qcur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved'")
                        _r = _qcur.fetchone() or {}
                        _queued = _r.get('n', 0) if hasattr(_r, 'get') else (_r[0] if _r else 0)
                    except Exception: pass
                    if _queued:
                        logger.warning("Auto-publisher: %s approved post(s) queued but LINKEDIN_ACCESS_TOKEN not set — LinkedIn distribution is DARK", _queued)
                    else:
                        logger.debug("Auto-publisher: No LINKEDIN_ACCESS_TOKEN, skipping")
                    continue
                conn = _get_db()
                cur = conn.cursor()
                today = datetime.utcnow().strftime('%Y-%m-%d')
                cur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'published' AND publish_platform = 'linkedin' AND published_at LIKE %s", (today + '%',))
                _row = cur.fetchone() or {}
                published_today = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                # r88 (2026-05-31): raise the daily cap MODESTLY + make it
                # env-overridable to clear the ~189-approved backlog without
                # spamming LinkedIn. Default 6/day drains ~189 over ~5 weeks.
                # DO NOT uncap — dumping the whole backlog in a day reads as
                # a spam bot and risks LinkedIn throttling/ban. Set
                # LINKEDIN_DAILY_CAP=N in Railway to tune (e.g. 4 to slow,
                # 8 to clear faster); keep it well under ~10/day.
                DAILY_CAP = int(os.environ.get('LINKEDIN_DAILY_CAP', '6'))
                if published_today >= DAILY_CAP:
                    logger.info(f"Auto-publisher: Already published {published_today} today (cap {DAILY_CAP}), skipping")
                    continue  # finally will close conn
                cur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved'")
                _row = cur.fetchone() or {}
                _queued = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                # r42v (2026-05-26): cap drain at 1 per loop iteration to avoid
                # three back-to-back AVOID posts (Chantilly/Edison/Buffalo) at
                # 3m/4m timestamps that looked like a spam bot.
                # r88 (2026-05-31): with a ~189-post backlog, 1/fire * 4 fires
                # never reaches the raised cap. When the queue is large, fill
                # the remaining daily budget THIS fire (each post still spaced
                # ~8s apart, content-class deduped) so DAILY_CAP actually
                # governs the rate; otherwise stay at 1/fire for clean cadence.
                # Same backlog-drain shape the Bluesky loop already uses.
                _remaining_today = max(DAILY_CAP - published_today, 0)
                _drain_budget = _remaining_today if _queued > 10 else 1
                _attempts = 0
                # Track which "content_class" patterns we've published TODAY
                # so we can avoid double-firing the same post type. Pattern
                # detection is lightweight: look at the first line of the
                # body. Each pattern can publish at most 1/24h.
                _seen_classes_today = set()
                try:
                    cur.execute("""SELECT content FROM social_media_posts
                                    WHERE status = 'published'
                                      AND publish_platform = 'linkedin'
                                      AND published_at LIKE %s""", (today + '%',))
                    for (_pub_text,) in cur.fetchall() if False else []:
                        pass
                    # cur.fetchall() returns RealDictRow rows; reread properly
                    cur.execute("""SELECT content FROM social_media_posts
                                    WHERE status = 'published'
                                      AND publish_platform = 'linkedin'
                                      AND published_at LIKE %s""", (today + '%',))
                    for _row in cur.fetchall() or []:
                        _txt = _row.get('content') if hasattr(_row, 'get') else (_row[0] if _row else '')
                        _seen_classes_today.add(_classify_post_for_dedup(_txt or ''))
                except Exception:
                    pass
                while _attempts < _drain_budget:
                    # Find next approved post that's not a duplicate class
                    cur.execute("SELECT id, content FROM social_media_posts WHERE status = 'approved' AND platform = 'linkedin' ORDER BY created_at ASC LIMIT 20")
                    candidates = cur.fetchall() or []
                    if not candidates:
                        cur.execute("SELECT id, content FROM social_media_posts WHERE status = 'approved' ORDER BY created_at ASC LIMIT 20")
                        candidates = cur.fetchall() or []

                    row = None
                    for _cand in candidates:
                        _ctext = _cand.get('content') if hasattr(_cand, 'get') else (_cand[1] if _cand else '')
                        _cls = _classify_post_for_dedup(_ctext or '')
                        if _cls in _seen_classes_today:
                            continue
                        # r63 (2026-05-29): entity-level + zero-stat judgment.
                        # Catches near-dupes the coarse class tag misses (two
                        # "Montréal BUILD" or "MCP tool-call surge" posts both
                        # land in class "other"), AND blocks "0 tool calls"
                        # zero-stat posts. Skipping a candidate here naturally
                        # rotates to a DIFFERENT topic in the same drain.
                        _skip, _why = _should_skip_publish(cur, _ctext or '', 'linkedin')
                        if _skip:
                            logger.warning(
                                "Auto-publisher: SKIPPED LinkedIn candidate %s for dedup/judgment — %s",
                                (_cand.get('id') if hasattr(_cand, 'get') else (_cand[0] if _cand else '?')),
                                _why)
                            continue
                        row = _cand
                        _seen_classes_today.add(_cls)
                        break
                    if not row:
                        logger.debug("Auto-publisher: No approved posts to publish (or all classes/entities already fired)")
                        break
                    post_id = row['id']
                    content_text = row['content']
                    # r62 (2026-05-29): legacy-shape quality gate. If this row
                    # is a pre-r47.38 short DCPI post (📍 X · ISO · DCPI
                    # verdict: VVV / Excess Power: ... / Live page: ...),
                    # rewrite it to the rich narrative shape BEFORE publish.
                    # Without this, queue rows enqueued days ago keep landing
                    # on LinkedIn as "ugly short" posts despite the generator
                    # being fixed. We also persist the rewrite back to the
                    # row so the audit log shows what actually went out.
                    if _is_legacy_short_dcpi_shape(content_text or ''):
                        rewritten = _rewrite_legacy_to_rich(content_text or '')
                        if rewritten and len(rewritten) > len(content_text or ''):
                            try:
                                cur.execute(
                                    "UPDATE social_media_posts SET content = %s WHERE id = %s",
                                    (rewritten, post_id),
                                )
                                conn.commit()
                            except Exception as _e_rw:
                                logger.warning(
                                    "Legacy-rewrite persist failed for post %s: %s",
                                    post_id, _e_rw,
                                )
                            content_text = rewritten
                            logger.info(
                                "Rewrote legacy short-DCPI post %s to rich shape (was %d chars, now %d)",
                                post_id,
                                len(row.get('content') or '') if hasattr(row, 'get') else len(row[1] or ''),
                                len(rewritten),
                            )
                    # Phase FF (#1): promote text-only posts to rich ARTICLE
                    # shares so LinkedIn renders the rotating og:today card (the
                    # "4 designs"). Extract the first URL + a title from the body;
                    # _post_to_linkedin builds an ARTICLE share whose card image
                    # LinkedIn scrapes from that URL's og:image — press-release
                    # pages point og:image at /api/v1/og/today/<slug>.png. This
                    # is the reason posts were weak text-only: line passed no
                    # article_url. FAIL-SAFE: any error / no URL → text-only
                    # (prior behaviour), so it can never make a post worse.
                    _art_url = None
                    _art_title = None
                    try:
                        import re as _re_url
                        _m = _re_url.search(r'https?://[^\s)>\]]+', content_text or '')
                        if _m:
                            _art_url = _m.group(0).rstrip('.,')
                            _first_line = (content_text or '').strip().split('\n', 1)[0].strip()
                            _art_title = _first_line[:180] or None
                    except Exception:
                        _art_url = None
                        _art_title = None
                    success, result = _post_to_linkedin(
                        content_text, access_token,
                        article_url=_art_url, article_title=_art_title)
                    now = datetime.utcnow().isoformat() + 'Z'
                    if success:
                        cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'linkedin' WHERE id = %s", (now, now, post_id))
                        conn.commit()
                        logger.info(f"Auto-published post {post_id} to LinkedIn (drain {_attempts+1}/{_drain_budget}, queued={_queued})")
                    else:
                        logger.warning(f"Auto-publish failed for post {post_id}: {result}")
                        try:
                            cur.execute("UPDATE social_media_posts SET status = 'failed' WHERE id = %s", (post_id,))
                            conn.commit()
                        except Exception: pass
                    _attempts += 1
                    if _attempts < _drain_budget:
                        time.sleep(8)
            except Exception as e:
                # Log FULL traceback so we can diagnose, not just str(e)
                logger.error(f"Auto-publisher error: {type(e).__name__}: {e}")
                logger.error(_tb.format_exc())
            finally:
                # GUARANTEE conn closed every iteration — prevents the pool
                # exhaustion that was likely behind the 2026-05-19 outage.
                if conn is not None:
                    try: conn.close()
                    except Exception: pass

    t = threading.Thread(target=_auto_publish_loop, daemon=True, name="linkedin-auto-publisher")
    t.start()


_twitter_publisher_running = False

def start_twitter_publisher():
    """Phase FF+3 (2026-05-13): parallel auto-publisher for X/Twitter.
    Same shape as the LinkedIn loop. Runs every 6h, max 2/day, gated
    on TWITTER_BEARER_TOKEN OR the OAuth1 quad.

    r41-twitter-disabled (2026-05-25): DISABLED. Three OAuth token
    rotations all failed with 'keys and tokens from a Twitter
    developer App that is attached to a Project' (X API v2 requires
    apps to live inside a Project; the existing app is a legacy
    standalone). Until the dev-portal app is migrated into a Project,
    every cycle just generates 403 log noise. Early-return short-
    circuits the loop entirely. To re-enable: migrate the app, then
    delete this guard.
    """
    global _twitter_publisher_running
    if _twitter_publisher_running:
        return
    if os.environ.get('TWITTER_PUBLISHER_ENABLED', 'false').lower() not in ('1', 'true', 'yes'):
        logger.info("X/Twitter auto-publisher DISABLED — set TWITTER_PUBLISHER_ENABLED=true to re-enable once app is in a Project")
        return
    _twitter_publisher_running = True

    def _twitter_loop():
        # Phase FF+7 (2026-05-18): 2-min first-run delay so posts go out
        # soon after restart instead of 6h dark.
        logger.info("X/Twitter auto-publisher started (initial 2min, then every 6h, max 3/day)")
        _first = True
        while True:
            # Phase FF+7-fix4 (2026-05-19): try/finally guarantees conn.close()
            # to prevent Neon pool exhaustion. Same pattern as LinkedIn loop.
            import traceback as _tb
            conn = None
            try:
                time.sleep(150 if _first else 6 * 3600)
                _first = False
                bearer = os.environ.get('TWITTER_BEARER_TOKEN', '')
                oauth1 = all([os.environ.get(k, '') for k in
                              ('TWITTER_API_KEY', 'TWITTER_API_SECRET',
                               'TWITTER_ACCESS_TOKEN', 'TWITTER_ACCESS_SECRET')])
                if not (bearer or oauth1):
                    _queued = 0
                    try:
                        conn = _get_db()
                        _qcur = conn.cursor()
                        _qcur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved' AND platform = 'twitter'")
                        _r = _qcur.fetchone() or {}
                        _queued = _r.get('n', 0) if hasattr(_r, 'get') else (_r[0] if _r else 0)
                    except Exception: pass
                    if _queued:
                        logger.warning("Twitter auto-publisher: %s approved X post(s) queued but no credentials set — X distribution is DARK", _queued)
                    else:
                        logger.debug("Twitter auto-publisher: no credentials, skipping")
                    continue
                conn = _get_db()
                cur = conn.cursor()
                today = datetime.utcnow().strftime('%Y-%m-%d')
                cur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'published' AND publish_platform = 'twitter' AND published_at LIKE %s", (today + '%',))
                _row = cur.fetchone() or {}
                pub_today = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                if pub_today >= 2:
                    logger.info(f"Twitter auto-publisher: already {pub_today} today, skipping")
                    continue
                cur.execute("SELECT id, content FROM social_media_posts WHERE status = 'approved' AND platform = 'twitter' ORDER BY created_at ASC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    logger.debug("Twitter auto-publisher: no approved Twitter posts")
                    continue
                post_id = row['id']
                content_text = row['content']
                # r63 (2026-05-29): same entity-level + zero-stat judgment as
                # the LinkedIn loop. Twitter picks a single oldest row, so a
                # skip just defers to the next cycle (no rotation here).
                _skip, _why = _should_skip_publish(cur, content_text or '', 'twitter')
                if _skip:
                    logger.warning("Twitter auto-publisher: SKIPPED post %s for dedup/judgment — %s", post_id, _why)
                    continue
                success, result = _post_to_twitter(content_text)
                now = datetime.utcnow().isoformat() + 'Z'
                if success:
                    cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'twitter' WHERE id = %s", (now, now, post_id))
                    conn.commit()
                    logger.info(f"Auto-published post {post_id} to X")
                else:
                    logger.warning(f"Twitter auto-publish failed for {post_id}: {result}")
            except Exception as e:
                logger.error(f"Twitter auto-publisher error: {type(e).__name__}: {e}")
                logger.error(_tb.format_exc())
            finally:
                if conn is not None:
                    try: conn.close()
                    except Exception: pass

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
            # Phase FF+7-fix4 (2026-05-19): try/finally guarantees conn.close()
            # to prevent Neon pool exhaustion. Same pattern as LinkedIn loop.
            import traceback as _tb
            conn = None
            try:
                time.sleep(180 if _first else 6 * 3600)
                _first = False
                handle  = os.environ.get('BLUESKY_HANDLE', '').strip()
                app_pwd = os.environ.get('BLUESKY_APP_PASSWORD', '').strip()
                if not handle or not app_pwd:
                    _queued = 0
                    try:
                        conn = _get_db()
                        _qcur = conn.cursor()
                        _qcur.execute(
                            "SELECT COUNT(*) AS n FROM social_media_posts "
                            "WHERE status = 'approved' AND platform = 'bluesky'")
                        _r = _qcur.fetchone() or {}
                        _queued = _r.get('n', 0) if hasattr(_r, 'get') else (_r[0] if _r else 0)
                    except Exception: pass
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
                # Phase FF+7-fix3 (2026-05-19): RealDictCursor — pull by name.
                cur.execute(
                    "SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'published' "
                    "AND publish_platform = 'bluesky' AND published_at LIKE %s",
                    (today + '%',))
                _row = cur.fetchone() or {}
                pub_today = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                DAILY_CAP = 3
                if pub_today >= DAILY_CAP:
                    logger.info(f"Bluesky auto-publisher: already {pub_today} today, skipping")
                    continue  # finally will close conn

                # Phase FF+7 (2026-05-18): Bluesky was filtering for
                # platform='bluesky' rows ONLY, but auto-press enqueues with
                # platform='linkedin' by default. Result: Bluesky publisher
                # found 0 rows every cycle and stayed silent (0 posts in 7d
                # despite being configured). Match LinkedIn's pattern: try
                # platform-specific first, fall back to any approved post.
                # Also backlog-drain like LinkedIn.
                cur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved'")
                _row = cur.fetchone() or {}
                _queued = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
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
                    # r63 (2026-05-29): entity-level + zero-stat judgment.
                    # Bluesky re-selects the same oldest row each drain
                    # iteration (no exclusion), so on a skip we BREAK to end
                    # this cycle's drain rather than spin on the same row.
                    _skip, _why = _should_skip_publish(cur, content_text or '', 'bluesky')
                    if _skip:
                        logger.warning("Bluesky auto-publisher: SKIPPED post %s for dedup/judgment — %s", post_id, _why)
                        break
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
            except Exception as e:
                logger.error(f"Bluesky auto-publisher error: {type(e).__name__}: {e}")
                logger.error(_tb.format_exc())
            finally:
                if conn is not None:
                    try: conn.close()
                    except Exception: pass

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
