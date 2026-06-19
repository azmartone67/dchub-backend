"""
email_fallback.py — resilient email send (SendGrid → Resend fallback) [2026-06-19].

SENDGRID_API_KEY went invalid (401 verified) but many senders were SendGrid-ONLY,
so their emails (health alerts, welcome/onboarding, lifecycle) silently failed.
This single helper tries SendGrid, then falls back to Resend (the live channel),
so any sender that routes through it keeps working regardless of which provider's
key is valid — and automatically uses SendGrid again if its key is later rotated.

Direct HTTP only (no `sendgrid` SDK — not installed in prod; no DB). Returns True
if either provider accepted the message, else False. Never raises.
"""
import os
import json
import logging
import urllib.request

log = logging.getLogger("email_fallback")


def _sendgrid(to_email, subject, html, text, from_email, from_name):
    key = (os.environ.get("SENDGRID_API_KEY") or "").strip()
    if not key:
        return False
    content = []
    if text:
        content.append({"type": "text/plain", "value": text})
    content.append({"type": "text/html", "value": html or text or ""})
    payload = json.dumps({
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": content,
    }).encode()
    try:
        req = urllib.request.Request("https://api.sendgrid.com/v3/mail/send", data=payload, method="POST")
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.warning("email_fallback: SendGrid failed (%s) — falling back to Resend", e)
        return False


def _resend(to_email, subject, html, text, from_email, from_name):
    key = (os.environ.get("RESEND_API_KEY") or os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
    if not key:
        return False
    body = {"from": f"{from_name} <{from_email}>", "to": [to_email], "subject": subject}
    if html:
        body["html"] = html
    if text:
        body["text"] = text
    if not html and not text:
        body["text"] = ""
    try:
        req = urllib.request.Request("https://api.resend.com/emails", data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "dchub-email/1.0")  # Resend/CF want a UA
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.warning("email_fallback: Resend failed: %s", e)
        return False


def send_email_resilient(to_email, subject, html_content=None, text_content=None,
                         from_email=None, from_name=None) -> bool:
    """Send via SendGrid, falling back to Resend. Returns True if accepted."""
    if not to_email:
        return False
    from_email = from_email or os.environ.get("SENDGRID_FROM_EMAIL", "info@dchub.cloud")
    from_name = from_name or os.environ.get("SENDGRID_FROM_NAME", "DC Hub")
    if _sendgrid(to_email, subject, html_content, text_content, from_email, from_name):
        return True
    ok = _resend(to_email, subject, html_content, text_content, from_email, from_name)
    if not ok:
        log.warning("email_fallback: NO channel delivered '%s' to %s (both SendGrid + Resend failed)", subject, to_email)
    return ok
