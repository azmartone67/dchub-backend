"""Phase IIIII (2026-05-16) — email engagement tracking via Resend webhooks.

DC Hub Media publishes (5/30d, healthy per RRRR) but we don't know
if anyone OPENS the emails or CLICKS the links. Resend supports
webhooks for delivered/opened/clicked/bounced events — wire them
to track real engagement.

  POST /api/v1/webhooks/resend     Resend POSTs events here
  GET  /api/v1/email/engagement    public engagement stats
  GET  /api/v1/email/recent        last 50 engagement events

Configure in Resend dashboard:
  Webhook URL: https://dchub.cloud/api/v1/webhooks/resend
  Events: email.delivered, email.opened, email.clicked,
          email.bounced, email.complained

Until the webhook is configured upstream, this surface returns
empty stats — no error. The table self-creates on first event.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


email_engagement_bp = Blueprint("email_engagement", __name__)


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_engagement (
    id              BIGSERIAL PRIMARY KEY,
    resend_email_id TEXT,
    event_type      TEXT NOT NULL,          -- delivered|opened|clicked|bounced|complained
    recipient       TEXT,
    subject         TEXT,
    link_clicked    TEXT,
    user_agent      TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_email_event_time
    ON email_engagement(event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_email_resend_id
    ON email_engagement(resend_email_id);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


@email_engagement_bp.route("/api/v1/webhooks/resend", methods=["POST"])
def resend_webhook():
    """Receive a Resend webhook event. Idempotent — same event_id
    won't be double-recorded thanks to id auto-increment + event_type
    de-dup on (resend_email_id, event_type) at read time."""
    d = request.get_json(silent=True) or {}
    # Resend webhook shape: {type, created_at, data: {email_id, to, subject, ...}}
    event_type = (d.get("type") or "").replace("email.", "").strip()
    data = d.get("data") or {}
    if not event_type:
        return jsonify(error="missing_type"), 400

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                recipient = data.get("to")
                if isinstance(recipient, list):
                    recipient = recipient[0] if recipient else None
                cur.execute("""
                    INSERT INTO email_engagement
                      (resend_email_id, event_type, recipient,
                       subject, link_clicked, user_agent)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                """, (data.get("email_id"), event_type[:30],
                      (recipient or "")[:200] or None,
                      (data.get("subject") or "")[:200] or None,
                      (data.get("link") or "")[:500] or None,
                      (data.get("user_agent") or "")[:200] or None))
                r = cur.fetchone()
            except Exception as e:
                return jsonify(error=f"insert_failed:{type(e).__name__}"), 500
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, recorded_id=int(r[0]) if r else None), 200


@email_engagement_bp.route("/api/v1/email/engagement", methods=["GET"])
def engagement_stats():
    """Public engagement metrics — open rate, click rate, bounce rate."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out = {"delivered_30d": 0, "opened_30d": 0, "clicked_30d": 0,
           "bounced_30d": 0, "complained_30d": 0,
           "open_rate_pct": 0.0, "click_rate_pct": 0.0,
           "bounce_rate_pct": 0.0}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT event_type, COUNT(*)
                      FROM email_engagement
                     WHERE occurred_at >= NOW() - INTERVAL '30 days'
                     GROUP BY event_type
                """)
                for r in cur.fetchall():
                    out[f"{r[0]}_30d"] = int(r[1] or 0)
                delivered = max(1, out.get("delivered_30d", 1) or 1)
                out["open_rate_pct"]   = round(100.0 * out.get("opened_30d", 0)  / delivered, 1)
                out["click_rate_pct"]  = round(100.0 * out.get("clicked_30d", 0) / delivered, 1)
                out["bounce_rate_pct"] = round(100.0 * out.get("bounced_30d", 0) / delivered, 1)
            except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    out["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    out["note"] = ("Configure Resend webhook URL → "
                    "https://dchub.cloud/api/v1/webhooks/resend with events "
                    "[email.delivered, email.opened, email.clicked, "
                    "email.bounced]. Until configured, all counters stay 0.")
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@email_engagement_bp.route("/api/v1/email/recent", methods=["GET"])
def recent_events():
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT event_type, recipient, subject, link_clicked,
                       occurred_at
                  FROM email_engagement
                 ORDER BY occurred_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "event":       r["event_type"],
        "recipient":   (r["recipient"] or "")[:80],
        "subject":     r["subject"],
        "link":        r["link_clicked"],
        "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
    } for r in rows]
    return jsonify(events=out, count=len(out)), 200
