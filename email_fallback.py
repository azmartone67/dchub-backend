"""
email_fallback.py — resilient email send (SendGrid → Resend fallback) [2026-06-19].

SENDGRID_API_KEY went invalid (401 verified) but many senders were SendGrid-ONLY,
so their emails (health alerts, welcome/onboarding, lifecycle) silently failed.
This single helper tries RESEND (the canonical/live outreach channel — see
dchub_outreach.py / routes/outreach_cron.py), then falls back to SendGrid (the
stale leftover, key 401). So any sender routed through it keeps working on Resend
today, and SendGrid auto-resumes only if its key is rotated AND Resend is down.

Direct HTTP only (no `sendgrid` SDK — not installed in prod; no DB). Returns True
if either provider accepted the message, else False. Never raises.
"""
import os
import json
import logging
import threading
import time
import urllib.request

import requests

log = logging.getLogger("email_fallback")

# ── r-welcome-429 (2026-09-09): ONE process-wide pacer for Resend ────────
# Resend rate-limits at ~2 req/s. On 2026-09-08 the checkout webhook fired
# three emails inside 440ms and every one came back 429; a paying customer got
# no API key and no alarm. This module is a LEAF (main imports it, never the
# reverse), so the lock lives here and main.py's _resend_email delegates to it
# — one lock, one rate, whichever path a send takes. Two independent pacers
# would permit twice the rate and neither would be wrong on its own.
RESEND_MIN_GAP_S = 0.6
RESEND_MAX_ATTEMPTS = 4
_PACE_LOCK = threading.Lock()
_LAST_SEND = [0.0]


def resend_pace():
    """Block until >= RESEND_MIN_GAP_S has elapsed since the last Resend send."""
    with _PACE_LOCK:
        gap = time.time() - _LAST_SEND[0]
        if gap < RESEND_MIN_GAP_S:
            time.sleep(RESEND_MIN_GAP_S - gap)
        _LAST_SEND[0] = time.time()


def resend_retry_wait(status, headers, attempt):
    """Seconds to wait before retrying, or None if this status is terminal.

    429 (rate limit) and 5xx are transient. Any other 4xx — unverified sender,
    malformed address — is NOT: retrying it just burns the window before an
    operator is told. Pure, so both branches are testable without a network.
    """
    if status != 429 and not (isinstance(status, int) and 500 <= status < 600):
        return None
    try:
        wait = float((headers or {}).get('Retry-After') or 0)
    except (TypeError, ValueError):
        wait = 0.0
    return min(wait or (0.5 * (2 ** (attempt - 1))), 8.0)


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
        log.warning("email_fallback: SendGrid (fallback) failed: %s", e)
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
    for attempt in range(1, RESEND_MAX_ATTEMPTS + 1):
        resend_pace()
        try:
            resp = requests.post(
                "https://api.resend.com/emails", json=body, timeout=10,
                headers={"Authorization": f"Bearer {key}",
                         "User-Agent": "dchub-email/1.0"})  # Resend/CF want a UA
        except Exception as e:
            if attempt == RESEND_MAX_ATTEMPTS:
                log.warning("email_fallback: Resend transport failed: %s", e)
                return False
            time.sleep(0.5 * (2 ** (attempt - 1)))
            continue
        if 200 <= resp.status_code < 300:
            return True
        wait = resend_retry_wait(resp.status_code, resp.headers, attempt)
        if wait is None or attempt == RESEND_MAX_ATTEMPTS:
            log.warning("email_fallback: Resend failed (HTTP %s, attempt %s/%s): %s",
                        resp.status_code, attempt, RESEND_MAX_ATTEMPTS,
                        (resp.text or "")[:200])
            return False
        log.info("email_fallback: Resend %s — retry in %.1fs (%s/%s)",
                 resp.status_code, wait, attempt, RESEND_MAX_ATTEMPTS)
        time.sleep(wait)
    return False


def send_email_resilient(to_email, subject, html_content=None, text_content=None,
                         from_email=None, from_name=None) -> bool:
    """Send via SendGrid, falling back to Resend. Returns True if accepted."""
    if not to_email:
        return False
    from_email = from_email or os.environ.get("SENDGRID_FROM_EMAIL", "info@dchub.cloud")
    from_name = from_name or os.environ.get("SENDGRID_FROM_NAME", "DC Hub")
    # Resend FIRST — it's the canonical/live channel (the outreach machine
    # dchub_outreach.py + routes/outreach_cron.py already send Resend-first).
    # SendGrid is the stale leftover (key 401), kept only as a dormant fallback
    # so it auto-resumes IF its key is ever rotated AND Resend is down. This
    # ordering also avoids eating a 401 round-trip on every send.
    if _resend(to_email, subject, html_content, text_content, from_email, from_name):
        return True
    ok = _sendgrid(to_email, subject, html_content, text_content, from_email, from_name)
    if not ok:
        log.warning("email_fallback: NO channel delivered '%s' to %s (Resend + SendGrid both failed)", subject, to_email)
    return ok
