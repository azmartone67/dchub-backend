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
    """Get SQLite connection with retry logic for lock contention on large DB."""
    last_error = None
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=30)
            # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
            # PRAGMA removed - not needed for PostgreSQL
            # PRAGMA removed - not needed for PostgreSQL
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
    conn = _get_db()
    cur = conn.cursor()
    for col, default in [('approved_at', None), ('publish_platform', None)]:
        for table in ['social_media_posts', 'press_releases']:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                logger.info(f"Added {col} to {table}")
            except sqlite3.OperationalError:
                pass
    for table in ['social_media_posts', 'press_releases']:
        try:
            cur.execute(f"SELECT published_at FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN published_at TEXT")
                logger.info(f"Added published_at to {table}")
            except sqlite3.OperationalError:
                pass
    conn.commit()
    conn.close()
    logger.info("Content publishing tables initialized")

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
    linkedin_connected = bool(os.environ.get('LINKEDIN_ACCESS_TOKEN', ''))
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

def _post_to_linkedin(content_text, access_token):
    """Post to DC Hub LinkedIn Company Page (org ID: 110894959)."""
    DCHUB_ORG_ID = os.environ.get('LINKEDIN_ORG_ID', '110894959')
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'X-Restli-Protocol-Version': '2.0.0',
    }
    post_body = {
        "author": f"urn:li:organization:{DCHUB_ORG_ID}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": content_text},
                "shareMediaCategory": "NONE"
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }
    resp = requests.post('https://api.linkedin.com/v2/ugcPosts', json=post_body, headers=headers, timeout=15)
    if resp.status_code in (200, 201):
        return True, resp.json().get('id', 'posted')
    return False, f"LinkedIn API error {resp.status_code}: {resp.text[:300]}"

@content_bp.route('/api/admin/publish/linkedin', methods=['POST'])
def publish_linkedin():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True)
    post_id = data.get('post_id')
    if not post_id:
        return jsonify({'success': False, 'error': 'post_id required'}), 400
    access_token = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')
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
        logger.info("LinkedIn auto-publisher started (every 6 hours, max 2/day)")
        while True:
            try:
                time.sleep(6 * 3600)
                access_token = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')
                if not access_token:
                    logger.debug("Auto-publisher: No LINKEDIN_ACCESS_TOKEN, skipping")
                    continue
                conn = _get_db()
                cur = conn.cursor()
                today = datetime.utcnow().strftime('%Y-%m-%d')
                cur.execute("SELECT COUNT(*) FROM social_media_posts WHERE status = 'published' AND publish_platform = 'linkedin' AND published_at LIKE %s", (today + '%',))
                published_today = cur.fetchone()[0]
                if published_today >= 2:
                    logger.info(f"Auto-publisher: Already published {published_today} today, skipping")
                    conn.close()
                    continue
                cur.execute("SELECT id, content FROM social_media_posts WHERE status = 'approved' AND platform = 'linkedin' ORDER BY created_at ASC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    cur.execute("SELECT id, content FROM social_media_posts WHERE status = 'approved' ORDER BY created_at ASC LIMIT 1")
                    row = cur.fetchone()
                if not row:
                    logger.debug("Auto-publisher: No approved posts to publish")
                    conn.close()
                    continue
                post_id = row['id']
                content_text = row['content']
                success, result = _post_to_linkedin(content_text, access_token)
                now = datetime.utcnow().isoformat() + 'Z'
                if success:
                    cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'linkedin' WHERE id = %s", (now, now, post_id))
                    conn.commit()
                    logger.info(f"Auto-published post {post_id} to LinkedIn")
                else:
                    logger.warning(f"Auto-publish failed for post {post_id}: {result}")
                conn.close()
            except Exception as e:
                logger.error(f"Auto-publisher error: {e}")

    t = threading.Thread(target=_auto_publish_loop, daemon=True, name="linkedin-auto-publisher")
    t.start()

def register_content_publisher(app):
    init_content_tables()
    app.register_blueprint(content_bp)
    logger.info("Content Publishing Pipeline registered")
    logger.info("   GET  /api/admin/content/stats")
    logger.info("   GET  /api/admin/content-queue")
    logger.info("   POST /api/admin/content/<id>/approve")
    logger.info("   POST /api/admin/content/<id>/reject")
    logger.info("   POST /api/admin/content/<id>/edit")
    logger.info("   POST /api/admin/publish/linkedin")
    logger.info("   Auto-publisher: every 6h, max 2/day")
