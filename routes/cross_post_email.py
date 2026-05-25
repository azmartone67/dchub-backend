"""
cross_post_email.py — daily email to user with best-of-day reshare URL.

Phase ZZZZZ-round47.11 (2026-05-25). The /best-of-day endpoint returns
a personal-share URL, but we want the user to actually USE it. Reading
log files isn't a habit — getting an email is. This endpoint:

  1. Fetches the day's best quad post via best-of-day logic
  2. Builds a short HTML email with one-click reshare button
  3. SMTPs it to the operator (RESEND_API_KEY required)

Cron-wired in cron_heartbeat.py to fire at 21:30 UTC (4:30 PM ET),
after the day's slot_20 quad post has landed.

  POST /api/v1/linkedin-quad/email-best
       ?to=<override email>     — defaults to OPERATOR_EMAIL env
       ?force=1                 — bypass the once-per-day idempotency
"""
import os
import datetime
import urllib.request
import urllib.error
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Blueprint, request, jsonify

cross_post_email_bp = Blueprint("cross_post_email", __name__)

RESEND_KEY     = os.environ.get("RESEND_API_KEY", "").strip()
FROM_EMAIL     = os.environ.get("OUTREACH_FROM_EMAIL", "api@dchub.cloud").strip()
FROM_NAME      = "DC Hub Daily Brief"
OPERATOR_EMAIL = os.environ.get("OPERATOR_EMAIL", "azmartone@gmail.com").strip()


def _fetch_best_of_day():
    """Hit our own /best-of-day endpoint. Internal warmup UA to bypass
    rate-limit. Returns parsed JSON or None on error."""
    try:
        req = urllib.request.Request(
            "https://api.dchub.cloud/api/v1/linkedin-quad/best-of-day",
            headers={"User-Agent": "DCHub-CrossPost/1.0",
                     "X-DC-Internal-Warmup": "1"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _send_email(to_email, subject, html, text):
    if not RESEND_KEY:
        return 503, "RESEND_API_KEY not set"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP("smtp.resend.com", 587, timeout=20) as srv:
            srv.starttls()
            srv.login("resend", RESEND_KEY)
            srv.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        return 200, "smtp_ok"
    except Exception as e:
        return 500, f"{type(e).__name__}: {str(e)[:140]}"


def _already_sent_today():
    """Use a small file as idempotency marker — avoids DB schema add.
    Resets daily by date check."""
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    marker_path = "/tmp/cross_post_email_sent_{}.flag".format(today)
    if os.path.exists(marker_path):
        return True
    try:
        with open(marker_path, "w") as f:
            f.write(datetime.datetime.utcnow().isoformat())
    except Exception:
        pass
    return False


@cross_post_email_bp.route("/api/v1/linkedin-quad/email-best",
                            methods=["POST", "GET"], strict_slashes=False)
def email_best():
    to_email = (request.args.get("to") or OPERATOR_EMAIL).strip()
    force    = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    if not to_email:
        return jsonify({"error": "no_recipient", "hint": "?to=user@example.com or set OPERATOR_EMAIL"}), 400

    if not force and _already_sent_today():
        return jsonify({"skipped": True, "reason": "already_sent_today",
                        "hint": "Pass ?force=1 to resend"}), 200

    best = _fetch_best_of_day()
    if not best or best.get("error"):
        return jsonify({"skipped": True, "reason": "no_best_of_day",
                        "detail": (best or {}).get("error", "fetch_failed")}), 200

    body_preview = (best.get("post_text") or "")[:600]
    share_url    = best.get("personal_share_url") or "#"
    view_url     = best.get("post_view_url") or "#"
    topic        = best.get("topic", "—")
    style        = best.get("style", "—")
    posted_at    = best.get("posted_at", "—")[:16].replace("T", " ")
    char_count   = best.get("char_count", 0)
    today_label  = datetime.datetime.utcnow().strftime("%A, %B %d")

    subject = f"DC Hub daily brief — share today's best post ({topic} · {style})"

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;max-width:600px;margin:0 auto;padding:24px;line-height:1.55;color:#0f172a">
<p style="color:#6366f1;font-size:.78rem;letter-spacing:.15em;text-transform:uppercase;font-weight:600">DC Hub · Daily Cross-Post Brief</p>
<h1 style="font-size:1.6rem;margin:.3em 0;letter-spacing:-.01em">Share today's best post in 30 seconds</h1>
<p style="color:#475569">{today_label} · slot_{best.get('slot_hour','?'):02d} UTC · {topic} · {style} · {char_count} chars</p>

<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:18px;margin:18px 0;font-size:.94rem;white-space:pre-wrap">{body_preview}{'...' if char_count > 600 else ''}</div>

<table cellspacing="0" cellpadding="0" style="margin:20px 0"><tr><td style="background:#6366f1;border-radius:8px"><a href="{share_url}" style="display:inline-block;padding:14px 28px;color:#fff;text-decoration:none;font-weight:600">Reshare to my personal feed →</a></td></tr></table>

<p style="font-size:.88rem;color:#64748b">Or <a href="{view_url}" style="color:#6366f1">view the original post on the company page</a> first.</p>

<hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0">

<p style="font-size:.82rem;color:#94a3b8">This email fires daily at 21:30 UTC (4:30 PM ET) after the day's quad rotation completes. To stop: <code>POST /api/v1/admin/cross-post-email/disable</code> or unset OPERATOR_EMAIL in Railway. To re-send today: <code>?force=1</code>.</p>
</body></html>"""

    text = f"""DC Hub daily cross-post brief — {today_label}

Today's best post: {topic} · {style} ({char_count} chars)

--- post body (first 600 chars) ---
{body_preview}{'...' if char_count > 600 else ''}
---

Share to your personal feed (one click):
{share_url}

View on company page:
{view_url}
"""

    status, detail = _send_email(to_email, subject, html, text)
    return jsonify({
        "sent_to":    to_email,
        "status":     status,
        "detail":     detail,
        "topic":      topic,
        "style":      style,
        "share_url":  share_url,
        "at":         datetime.datetime.utcnow().isoformat() + "Z",
    }), 200 if status == 200 else 502
