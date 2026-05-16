"""
DC Hub Health Endpoint Update
==============================
Replace the /api/health endpoint in main.py (around line 10356-10364)

Find this block:
----------------------------------------------------------------------
@app.route('/api/health', methods=['GET'])
def api_health():
    \"\"\"Lightweight health check — responds instantly without database access.\"\"\"
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'version': APP_VERSION,
        'uptime_seconds': round(time.time() - APP_START_TIME)
    })
----------------------------------------------------------------------

Replace with the block below:
======================================================================
"""

# ---- COPY FROM HERE ----

# AUTO-REPAIR: duplicate route '/api/health' also in main.py:10470 — review and remove one
@app.route('/api/health', methods=['GET'])
def api_health():
    """Health check with data counts for monitoring and failover validation.

    Returns consistent schema across Railway and Replit so the Cloudflare
    Worker, QA scripts, and monitoring tools get the same fields regardless
    of which backend is serving.

    Fast path: if DB is unreachable, still returns healthy with counts = 0
    so the Worker doesn't mark this backend as down for a slow query.
    """
    health = {
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'version': APP_VERSION,
        'uptime_seconds': round(time.time() - APP_START_TIME),
        'environment': 'railway' if IS_RAILWAY else 'replit',
        'source': 'neon',
        'facility_count': 0,
        'deal_count': 0,
        'news_count': 0,
    }

    # Quick counts — 3 simple COUNT(*) queries, <50ms on Neon
    try:
        with pg_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT COUNT(*) FROM facilities")
                health['facility_count'] = cur.fetchone()[0] or 0
            except Exception:
                pass
            try:
                cur.execute("SELECT COUNT(*) FROM deals")
                health['deal_count'] = cur.fetchone()[0] or 0
            except Exception:
                pass
            try:
                cur.execute("SELECT COUNT(*) FROM announcements")
                health['news_count'] = cur.fetchone()[0] or 0
            except Exception:
                pass
    except Exception:
        health['source'] = 'neon-unreachable'

    return jsonify(health)

# ---- COPY TO HERE ----
