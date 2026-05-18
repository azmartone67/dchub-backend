"""
Phase RRR-newsletter (2026-05-18) — public weekly newsletter for non-customers.

`routes/weekly_digest.py` already sends *personalized* weekly emails to
identified API users (mcp_dev_keys with activity). This module adds the
*public* side: anyone can subscribe with just their email, gets a generic
"this week in data center intelligence" digest every Monday.

Why two systems instead of one:
  - The personalized digest is per-user (tool usage, upgrade tier, recent
    cap hits). Subscribers don't have an API key.
  - The personalized digest dedupes per-key. Public subscribers dedupe
    per-email.
  - The two tables don't overlap conceptually; merging them would force
    nullable foreign keys and confuse the "are they a customer?" check.

Routes:
  POST  /api/v1/weekly/subscribe        — public, takes {email}
  GET   /api/v1/weekly/unsubscribe/<tok> — public, one-click off
  GET   /api/v1/weekly/digest/public    — preview HTML (admin or anyone)
  POST  /api/v1/weekly/send-public      — admin: send to all subscribers
  GET   /api/v1/weekly/subscribers      — admin: count + recent emails
"""

import logging
import os
import secrets
import json as _json
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
weekly_public_newsletter_bp = Blueprint("weekly_public_newsletter", __name__)


def _conn():
    """Return a Neon connection. Uses the same pool accessor as the rest
    of the app via the late-bound import to avoid circular deps at module
    load time."""
    try:
        from main import get_db
        return get_db()
    except Exception:
        import psycopg2
        db_url = (os.environ.get("NEON_DATABASE_URL")
                  or os.environ.get("DATABASE_URL", ""))
        return psycopg2.connect(db_url)


_TABLE_INIT_DONE = False

def _ensure_table():
    """Idempotent CREATE TABLE IF NOT EXISTS. Runs once per process."""
    global _TABLE_INIT_DONE
    if _TABLE_INIT_DONE:
        return
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS weekly_public_subscribers (
                    email             TEXT PRIMARY KEY,
                    subscribed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    unsubscribe_token TEXT NOT NULL,
                    status            TEXT NOT NULL DEFAULT 'active',
                    last_sent_at      TIMESTAMPTZ,
                    source            TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS weekly_subs_status_idx
                  ON weekly_public_subscribers (status)
            """)
            try: conn.commit()
            except Exception: pass
            _TABLE_INIT_DONE = True
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning("weekly_public_subscribers table init failed: %s", e)


def _is_valid_email(email: str) -> bool:
    """Cheap email shape check. Real validation happens at send time."""
    if not email or "@" not in email or len(email) > 254:
        return False
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and len(domain) > 3


@weekly_public_newsletter_bp.route("/api/v1/weekly/subscribe", methods=["POST", "OPTIONS"])
def subscribe():
    """Public subscribe endpoint. POST {email, source?}.
    Idempotent — re-subscribing the same email is a no-op."""
    if request.method == "OPTIONS":
        # CORS preflight
        return ("", 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    _ensure_table()
    data = request.get_json(silent=True) or {}
    # Fall back to form data so the same endpoint works with
    # `<form action=...>` submissions, not just JSON.
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    source = (data.get("source") or request.form.get("source")
              or request.referrer or "")[:120]

    if not _is_valid_email(email):
        return jsonify(ok=False, error="invalid_email"), 400

    token = secrets.token_urlsafe(24)
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO weekly_public_subscribers
                  (email, unsubscribe_token, source, status)
                VALUES (%s, %s, %s, 'active')
                ON CONFLICT (email) DO UPDATE
                  SET status = 'active', subscribed_at = NOW()
                RETURNING (xmax = 0) AS was_inserted
            """, (email, token, source))
            row = cur.fetchone()
            try: conn.commit()
            except Exception: pass
            was_new = bool(row[0]) if row else False
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning("subscribe failed: %s", e)
        return jsonify(ok=False, error="db_error"), 503

    resp = jsonify(
        ok=True,
        email=email,
        status="subscribed" if was_new else "already_subscribed",
        next_send="Monday 13:00 UTC",
    )
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@weekly_public_newsletter_bp.route("/api/v1/weekly/unsubscribe/<token>", methods=["GET"])
def unsubscribe(token: str):
    """One-click unsubscribe — sets status='unsubscribed'."""
    _ensure_table()
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE weekly_public_subscribers
                   SET status = 'unsubscribed'
                 WHERE unsubscribe_token = %s
                RETURNING email
            """, (token,))
            row = cur.fetchone()
            try: conn.commit()
            except Exception: pass
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    if not row:
        return ("Token not found.", 404)
    return (f"<html><body style='font-family:system-ui;text-align:center;padding:40px'>"
            f"<h1>Unsubscribed</h1>"
            f"<p>You'll no longer receive DC Hub Weekly. "
            f"Re-subscribe anytime at <a href='https://dchub.cloud/dc-hub-media/'>"
            f"dchub.cloud/dc-hub-media</a>.</p>"
            f"</body></html>", 200, {"Content-Type": "text/html"})


def _build_public_digest_html() -> tuple[str, str]:
    """Generate the public weekly digest HTML + plain text. Returns (subject, html).

    Pulls live data from /api/v1/marketing/pulse + /api/v1/stats + the
    deals/news tables to assemble a "this week in DC intelligence" summary.
    Failures degrade gracefully — empty sections stay empty rather than
    blocking the send.
    """
    week_of = datetime.now(timezone.utc).strftime("%b %d, %Y")
    subject = f"DC Hub Weekly — {week_of}"

    # Section 1: facilities + countries (always available)
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM facilities")
            total_facilities = cur.fetchone()[0]
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        total_facilities = None

    # Section 2: latest M&A deals (last 7d)
    deals_html = ""
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT date, buyer, seller, type, value_display
                  FROM deals
                 WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                 ORDER BY date DESC
                 LIMIT 5
            """)
            rows = cur.fetchall() or []
        finally:
            try: conn.close()
            except Exception: pass
        if rows:
            deals_html = "<ul style='line-height:1.6'>" + "".join(
                f"<li><b>{r[0]}</b>: {r[1]} → {r[2]} ({r[3]}, {r[4] or 'n/a'})</li>"
                for r in rows) + "</ul>"
        else:
            deals_html = "<p style='color:#888'><i>No new M&A this week.</i></p>"
    except Exception as e:
        logger.warning("digest deals section failed: %s", e)
        deals_html = ""

    # Section 3: recent auto-press headlines
    press_html = ""
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT title, slug, published_at
                  FROM press_releases
                 WHERE published = TRUE
                   AND published_at >= NOW() - INTERVAL '7 days'
                 ORDER BY published_at DESC
                 LIMIT 5
            """)
            rows = cur.fetchall() or []
        finally:
            try: conn.close()
            except Exception: pass
        if rows:
            press_html = "<ul style='line-height:1.6'>" + "".join(
                f"<li><a href='https://dchub.cloud/news/{r[1]}'>{r[0]}</a></li>"
                for r in rows) + "</ul>"
    except Exception as e:
        logger.warning("digest press section failed: %s", e)

    facilities_txt = (f"<b>{total_facilities:,}</b>" if total_facilities
                      else "20,000+")

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{subject}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0A0E1C;color:#E8ECF7;margin:0;padding:0">
<div style="max-width:640px;margin:0 auto;padding:32px 24px">
  <div style="text-align:center;margin-bottom:32px">
    <h1 style="margin:0 0 8px;font-size:28px;letter-spacing:-0.5px">DC Hub Weekly</h1>
    <p style="margin:0;color:#8B92A8;font-size:13px;letter-spacing:1.5px;text-transform:uppercase">{week_of}</p>
  </div>

  <div style="background:#121629;border:1px solid #242940;border-radius:12px;padding:20px;margin-bottom:24px">
    <h2 style="margin:0 0 12px;font-size:18px;color:#4F8FFF">📊 By the numbers</h2>
    <p style="margin:0;line-height:1.6">Tracking {facilities_txt} data center facilities across 178+ countries. <a href="https://dchub.cloud/by-the-numbers" style="color:#4F8FFF">See the live dashboard →</a></p>
  </div>

  <div style="background:#121629;border:1px solid #242940;border-radius:12px;padding:20px;margin-bottom:24px">
    <h2 style="margin:0 0 12px;font-size:18px;color:#a855f7">💰 M&amp;A this week</h2>
    {deals_html or "<p style='color:#888'>No new transactions tracked this week.</p>"}
    <p style="margin:14px 0 0"><a href="https://dchub.cloud/ai-deals" style="color:#a855f7;font-size:13px">All deals →</a></p>
  </div>

  <div style="background:#121629;border:1px solid #242940;border-radius:12px;padding:20px;margin-bottom:24px">
    <h2 style="margin:0 0 12px;font-size:18px;color:#10b981">📰 What we published</h2>
    {press_html or "<p style='color:#888'>Quiet week on the press front.</p>"}
  </div>

  <div style="text-align:center;padding:24px 0">
    <a href="https://dchub.cloud/state-of-the-data-center" style="display:inline-block;padding:14px 28px;background:linear-gradient(135deg,#a855f7,#4F8FFF);color:#fff;text-decoration:none;border-radius:8px;font-weight:600">Read the State of the Data Center Market 2026 →</a>
  </div>

  <hr style="border:none;border-top:1px solid #242940;margin:32px 0">
  <p style="font-size:12px;color:#8B92A8;text-align:center;line-height:1.6">
    You're receiving this because you subscribed at <a href="https://dchub.cloud/dc-hub-media/" style="color:#8B92A8">dchub.cloud/dc-hub-media</a>.<br>
    <a href="{{UNSUBSCRIBE_URL}}" style="color:#8B92A8">Unsubscribe</a> · DC Hub · Martone Advisors LLC
  </p>
</div>
</body>
</html>"""
    return subject, html


@weekly_public_newsletter_bp.route("/api/v1/weekly/digest/public", methods=["GET"])
def preview_public_digest():
    """Preview the current week's digest as HTML. Useful for QA before send."""
    _, html = _build_public_digest_html()
    return (html.replace("{UNSUBSCRIBE_URL}", "https://dchub.cloud/api/v1/weekly/unsubscribe/sample-token"),
            200, {"Content-Type": "text/html"})


def _send_via_resend(to_email: str, subject: str, html: str) -> bool:
    """Use the same Resend helper as dchub_outreach.py."""
    try:
        from dchub_outreach import send_via_resend
        ok, _resp, _id = send_via_resend(
            to_email=to_email,
            subject=subject,
            html_body=html,
            text_body=None,
            reply_to="jonathan@dchub.cloud",
        )
        return bool(ok)
    except Exception as e:
        logger.warning("resend send failed for %s: %s", to_email, e)
        return False


@weekly_public_newsletter_bp.route("/api/v1/weekly/send-public", methods=["POST"])
def send_public_digest():
    """Admin trigger. Sends the current week's digest to every active
    public subscriber. ?dry=true returns counts without sending."""
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if admin_key and provided != admin_key:
        return jsonify(error="unauthorized"), 401

    _ensure_table()
    dry = (request.args.get("dry") or "").lower() in ("1", "true", "yes")

    subject, html_template = _build_public_digest_html()

    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT email, unsubscribe_token
                  FROM weekly_public_subscribers
                 WHERE status = 'active'
            """)
            rows = cur.fetchall() or []
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=f"db: {e}"), 500

    if dry:
        return jsonify(ok=True, mode="dry_run", subject=subject,
                       eligible=len(rows)), 200

    sent = 0
    failed = 0
    for email, token in rows:
        unsub = f"https://dchub.cloud/api/v1/weekly/unsubscribe/{token}"
        html = html_template.replace("{UNSUBSCRIBE_URL}", unsub)
        if _send_via_resend(email, subject, html):
            sent += 1
            # Mark sent
            try:
                conn = _conn()
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE weekly_public_subscribers
                           SET last_sent_at = NOW()
                         WHERE email = %s
                    """, (email,))
                    try: conn.commit()
                    except Exception: pass
                finally:
                    try: conn.close()
                    except Exception: pass
            except Exception:
                pass
        else:
            failed += 1

    return jsonify(ok=True, mode="sent", subject=subject,
                   eligible=len(rows), sent=sent, failed=failed), 200


@weekly_public_newsletter_bp.route("/api/v1/weekly/subscribers", methods=["GET"])
def list_subscribers():
    """Admin visibility: count + recent 25."""
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if admin_key and provided != admin_key:
        return jsonify(error="unauthorized"), 401

    _ensure_table()
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE status = 'active'),
                       COUNT(*) FILTER (WHERE status = 'unsubscribed')
                  FROM weekly_public_subscribers
            """)
            active, unsub = cur.fetchone()
            cur.execute("""
                SELECT email, subscribed_at, status, source, last_sent_at
                  FROM weekly_public_subscribers
                 ORDER BY subscribed_at DESC
                 LIMIT 25
            """)
            recent = [
                {"email": r[0],
                 "subscribed_at": r[1].isoformat() if r[1] else None,
                 "status": r[2],
                 "source": r[3],
                 "last_sent_at": r[4].isoformat() if r[4] else None}
                for r in (cur.fetchall() or [])
            ]
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    return jsonify(ok=True, active=active, unsubscribed=unsub,
                   recent=recent), 200
