"""
Phase RRR-newsletter (2026-05-18) — public weekly newsletter.

Rewritten as MINIMAL VERSION to isolate why the original failed to
register on Railway. If this works, we'll grow it back to full
functionality. If not, the problem is structural to how routes/*.py
imports are handled at registration time.
"""

from flask import Blueprint, request, jsonify

weekly_public_newsletter_bp = Blueprint("weekly_public_newsletter", __name__)


# AUTO-REPAIR: duplicate route '/api/v1/weekly/ping' also in main.py:1351 — review and remove one
@weekly_public_newsletter_bp.route("/api/v1/weekly/ping", methods=["GET"])
def weekly_ping():
    """Minimal liveness check — returns ok if blueprint is registered."""
    return jsonify(ok=True, blueprint="weekly_public_newsletter",
                   version="minimal-2026-05-18")

# AUTO-REPAIR: duplicate route '/api/v1/weekly/subscribe' also in main.py:1355 — review and remove one

@weekly_public_newsletter_bp.route("/api/v1/weekly/subscribe", methods=["POST", "OPTIONS"])
def subscribe():
    """Public subscribe — minimal version. Stores to weekly_public_subscribers."""
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Methods": "POST, OPTIONS",
                          "Access-Control-Allow-Headers": "Content-Type"})
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify(ok=False, error="invalid_email"), 400

    # Late imports — no module-level DB or main import
    import os
    import secrets
    try:
        import psycopg2
        db_url = (os.environ.get("NEON_DATABASE_URL")
                  or os.environ.get("DATABASE_URL", ""))
        conn = psycopg2.connect(db_url)
        try:
            cur = conn.cursor()
            # Idempotent table create
            cur.execute("""
                CREATE TABLE IF NOT EXISTS weekly_public_subscribers (
                    email             TEXT PRIMARY KEY,
                    subscribed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    unsubscribe_token TEXT NOT NULL,
                    status            TEXT NOT NULL DEFAULT 'active',
                    source            TEXT
                )
            """)
            token = secrets.token_urlsafe(24)
            source = (data.get("source") or request.referrer or "")[:120]
            cur.execute("""
                INSERT INTO weekly_public_subscribers
                    (email, unsubscribe_token, source, status)
                VALUES (%s, %s, %s, 'active')
                ON CONFLICT (email) DO UPDATE
                    SET status = 'active', subscribed_at = NOW()
            """, (email, token, source))
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=f"db_error: {str(e)[:120]}"), 503

    resp = jsonify(ok=True, email=email, status="subscribed",
                   next_send="Monday 13:00 UTC")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
# AUTO-REPAIR: duplicate route '/api/v1/weekly/subscribers' also in main.py:1658 — review and remove one


@weekly_public_newsletter_bp.route("/api/v1/weekly/subscribers", methods=["GET"])
def subscribers_count():
    """Public count (no PII)."""
    import os
    try:
        import psycopg2
        db_url = (os.environ.get("NEON_DATABASE_URL")
                  or os.environ.get("DATABASE_URL", ""))
        conn = psycopg2.connect(db_url)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM weekly_public_subscribers
                 WHERE status = 'active'
            """)
            n = cur.fetchone()[0]
        finally:
            try: conn.close()
            except Exception: pass
        return jsonify(ok=True, active=n), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:120]), 503
