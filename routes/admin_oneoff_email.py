"""admin_oneoff_email.py — operator one-off email (admin-keyed, logged, idempotent).

For a controlled single send that no automated template covers — e.g. a
correction to a customer the activation nudge mis-targeted. Reuses the live
Resend path (email_fallback.send_email_resilient), so the Resend key stays
server-side and never leaves the backend. Every send is recorded in
email_drip_log with the caller-supplied email_key, so it is auditable and
idempotent per (recipient, email_key) — a repeat call is a no-op, not a
double-send.

  POST /api/v1/admin/send-oneoff            (X-Admin-Key)  -> DRY-RUN preview
  POST /api/v1/admin/send-oneoff?confirm=1  (X-Admin-Key)  -> actually sends
  body (JSON): {"to","subject","html","email_key"}
"""
import os
import logging

from flask import Blueprint, request, jsonify

logger = logging.getLogger("admin_oneoff_email")
admin_oneoff_email_bp = Blueprint("admin_oneoff_email", __name__)


def _admin_ok():
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    return bool(os.environ.get("DCHUB_ADMIN_KEY")) and \
        provided == os.environ.get("DCHUB_ADMIN_KEY", "")


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


@admin_oneoff_email_bp.route("/api/v1/admin/send-oneoff", methods=["POST"])
def send_oneoff():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    d = request.get_json(silent=True) or {}
    to = (d.get("to") or "").strip()
    subject = (d.get("subject") or "").strip()
    html = d.get("html") or ""
    email_key = (d.get("email_key") or "oneoff").strip()
    if not to or "@" not in to or not subject or not html:
        return jsonify(ok=False, error="need to, subject, html (and optional email_key)"), 400

    dry = request.args.get("confirm") != "1"
    if dry:
        return jsonify(ok=True, dry_run=True, to=to, subject=subject,
                       email_key=email_key, html_bytes=len(html),
                       note="Add ?confirm=1 to actually send."), 200

    # Idempotency: refuse a duplicate (recipient, email_key).
    try:
        c = _conn()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT 1 FROM email_drip_log WHERE lower(user_email)=lower(%s) "
                            "AND email_key=%s", (to, email_key))
                if cur.fetchone():
                    return jsonify(ok=True, skipped="already_sent",
                                   to=to, email_key=email_key), 200
        finally:
            c.close()
    except Exception:
        pass  # fail open on the pre-check; the ON CONFLICT below is the backstop

    from email_fallback import send_email_resilient
    try:
        sent = send_email_resilient(
            to, subject, html_content=html,
            from_email="jonathan@dchub.cloud", from_name="Jonathan Martone")
    except Exception as e:
        logger.warning("send-oneoff failed for %s: %s", to, e)
        return jsonify(ok=False, error=str(e)[:200]), 500

    if sent:
        try:
            c = _conn()
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO email_drip_log (user_email, email_key, status) "
                        "VALUES (%s, %s, 'sent') ON CONFLICT (user_email, email_key) DO NOTHING",
                        (to, email_key))
            finally:
                c.close()
        except Exception:
            pass
    return jsonify(ok=bool(sent), sent=1 if sent else 0, to=to, email_key=email_key), 200
