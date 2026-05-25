"""
outreach_cron.py — send follow-up email to identified checkout leads.

Phase ZZZZZ-round39 (2026-05-25). identified_checkout_signals table has
13+ captured leads (awaiting_outreach=11) that left an email but didn't
complete Stripe checkout. This module sends each one a follow-up
"complete your Pro upgrade" email within 1 hour of capture.

Provider precedence (uses whichever env var is set):
  1. RESEND_API_KEY        → POST api.resend.com/emails
  2. SENDGRID_API_KEY      → POST api.sendgrid.com/v3/mail/send
  3. (none) → no-op + mark outreach_sent=true with notes="no_provider_configured"

Endpoint:
  POST /api/v1/outreach/process-pending → process up to N leads
  GET  /api/v1/outreach/status          → diagnostic
"""
import os
import json
import datetime
import smtplib
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from contextlib import contextmanager

from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

outreach_cron_bp = Blueprint("outreach_cron", __name__,
                              url_prefix="/api/v1/outreach")

# Env-var driven provider selection
RESEND_KEY    = os.environ.get("RESEND_API_KEY", "").strip()
SENDGRID_KEY  = os.environ.get("SENDGRID_API_KEY", "").strip()
# Default sender: onboarding@resend.dev works WITHOUT domain verification
# (Resend allows sends from their test domain for any account). Once
# dchub.cloud is verified in Resend dashboard, set OUTREACH_FROM_EMAIL=api@dchub.cloud
FROM_EMAIL    = os.environ.get("OUTREACH_FROM_EMAIL", "onboarding@resend.dev").strip()
FROM_NAME     = os.environ.get("OUTREACH_FROM_NAME", "DC Hub").strip()


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _provider():
    if RESEND_KEY: return "resend"
    if SENDGRID_KEY: return "sendgrid"
    return None


def _build_email(lead):
    tool = lead["tool"] or "DC Hub"
    tier = lead["tier"]
    tier_price = {"developer":"$49/mo","pro":"$199/mo","starter":"$9/mo"}.get(tier, "—")
    subject = f"Finish your DC Hub {tier.title()} upgrade — {tier_price}"
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;max-width:560px;margin:40px auto;padding:0 24px;color:#0f172a;line-height:1.55">
<p>Hey,</p>
<p>You started a DC Hub <b>{tier.title()}</b> upgrade ({tier_price}) after hitting the paywall on <code style="background:#e0e7ff;padding:1px 6px;border-radius:3px">{tool}</code> — but didn't finish checkout.</p>
<p>One click to pick back up where you left off (your email is prefilled):</p>
<p style="margin:24px 0"><a href="https://api.dchub.cloud/pricing/upgrade?tool={tool}&tier={tier}&direct=1&ref=outreach"
   style="background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600">Complete checkout →</a></p>
<p style="font-size:.9rem;color:#64748b">What you unlock with {tier.title()}:</p>
<ul style="font-size:.9rem;color:#475569">
<li><b>{ {"developer":"1,000","pro":"10,000","starter":"100"}.get(tier, "1,000") } calls/day</b> across all 25 MCP tools</li>
<li>Full result sizes (no truncation)</li>
<li>{ {"developer":"Export to CSV/JSON", "pro":"All gated tools (analyze_site, compare_sites, get_grid_intelligence, etc)", "starter":"Same data as Developer, lighter cap"}.get(tier, "Full feature set") }</li>
<li>Cancel anytime — Stripe-managed billing</li>
</ul>
<p style="font-size:.85rem;color:#64748b;margin-top:32px">Questions? Reply to this email — we respond within a few hours.</p>
<p style="font-size:.85rem;color:#64748b">— DC Hub team<br><a href="https://dchub.cloud" style="color:#6366f1">dchub.cloud</a></p>
<hr style="border:none;border-top:1px solid #e2e8f0;margin:32px 0 16px">
<p style="font-size:.75rem;color:#94a3b8">You're receiving this because you started a DC Hub Pro signup that wasn't completed. Reply STOP to unsubscribe.</p>
</body></html>"""
    text = (
        f"You started a DC Hub {tier.title()} upgrade ({tier_price}) after hitting "
        f"the paywall on {tool} but didn't finish.\n\n"
        f"Finish here (email prefilled):\n"
        f"https://api.dchub.cloud/pricing/upgrade?tool={tool}&tier={tier}&direct=1&ref=outreach\n\n"
        "— DC Hub\n"
        "Reply STOP to unsubscribe."
    )
    return subject, html, text


def _send_resend(to_email, subject, html, text):
    """r39.4 (2026-05-25): Resend via SMTP (port 587 + STARTTLS), NOT
    HTTPS. The HTTPS API at api.resend.com is fronted by Cloudflare WAF
    which 1010-bans Railway's static outbound IP (162.220.232.99).
    SMTP bypasses HTTP WAF entirely. Resend supports SMTP with:
      host:     smtp.resend.com
      port:     587 (STARTTLS) or 465 (SSL)
      username: resend  (literal)
      password: <RESEND_API_KEY>
    """
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
        return 200, "smtp_send_ok"
    except smtplib.SMTPAuthenticationError as e:
        return 401, f"smtp_auth_failed: {e.smtp_code} {e.smtp_error[:120]}"
    except smtplib.SMTPRecipientsRefused as e:
        return 550, f"recipient_refused: {str(e)[:120]}"
    except smtplib.SMTPDataError as e:
        return e.smtp_code, f"smtp_data_error: {e.smtp_error[:120]}"
    except smtplib.SMTPException as e:
        return 500, f"smtp_error: {type(e).__name__}: {str(e)[:120]}"
    except Exception as e:
        return 0, f"{type(e).__name__}: {str(e)[:120]}"


def _send_sendgrid(to_email, subject, html, text):
    body = json.dumps({
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": FROM_EMAIL, "name": FROM_NAME},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text},
            {"type": "text/html",  "value": html},
        ],
    }).encode()
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=body,
        headers={
            "Authorization": f"Bearer {SENDGRID_KEY}",
            "Content-Type":  "application/json",
            "User-Agent":    "DCHub-Outreach/1.0 (+https://dchub.cloud; api@dchub.cloud)",
            "Accept":        "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read(1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(1024).decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


@outreach_cron_bp.route("/process-pending", methods=["POST", "GET"])
def process_pending():
    started = datetime.datetime.utcnow()
    out = {"at": started.isoformat() + "Z", "provider": _provider(), "results": []}
    if not (_pg and _dsn()):
        out["error"] = "no_db"
        return jsonify(out), 200

    limit = max(1, min(50, int(request.args.get("limit", 25))))
    age_minutes_min = int(request.args.get("min_age_minutes", 5))  # don't spam fresh ones
    # Find awaiting leads
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, tool, tier, captured_at
                FROM identified_checkout_signals
                WHERE outreach_sent = FALSE
                  AND converted = FALSE
                  AND captured_at < NOW() - (%s || ' minutes')::interval
                ORDER BY captured_at ASC
                LIMIT %s
            """, (str(age_minutes_min), limit))
            leads = cur.fetchall()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
        return jsonify(out), 500

    out["candidates"] = len(leads)
    out["sent"] = 0
    out["failed"] = 0
    out["skipped_no_provider"] = 0

    provider = _provider()
    for lead in leads:
        email = lead["email"]
        # Skip obvious test emails to avoid bouncing on @test.com, @dchub.cloud QA
        is_test = any(p in email.lower() for p in ("@test.com", "verify-", "qa-", "final-verify"))
        if is_test:
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("""
                        UPDATE identified_checkout_signals
                           SET outreach_sent = TRUE, outreach_at = NOW(),
                               notes = 'skipped_test_email'
                         WHERE id = %s
                    """, (lead["id"],))
                    c.commit()
                out["results"].append({"id": lead["id"], "email": email, "status": "skipped_test"})
            except Exception: pass
            continue

        if not provider:
            out["skipped_no_provider"] += 1
            # Mark so we don't keep re-evaluating
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("""
                        UPDATE identified_checkout_signals
                           SET outreach_sent = TRUE, outreach_at = NOW(),
                               notes = 'no_provider_configured_set_RESEND_API_KEY_or_SENDGRID_API_KEY'
                         WHERE id = %s
                    """, (lead["id"],))
                    c.commit()
            except Exception: pass
            out["results"].append({"id": lead["id"], "email": email, "status": "no_provider"})
            continue

        subject, html, text = _build_email(lead)
        if provider == "resend":
            status, body = _send_resend(email, subject, html, text)
        else:
            status, body = _send_sendgrid(email, subject, html, text)

        ok = 200 <= status < 300
        try:
            with _conn() as c, c.cursor() as cur:
                if ok:
                    cur.execute("""
                        UPDATE identified_checkout_signals
                           SET outreach_sent = TRUE, outreach_at = NOW(),
                               notes = %s
                         WHERE id = %s
                    """, (f"sent_via_{provider}_status_{status}", lead["id"]))
                    out["sent"] += 1
                else:
                    cur.execute("""
                        UPDATE identified_checkout_signals
                           SET notes = %s
                         WHERE id = %s
                    """, (f"failed_via_{provider}_status_{status}_body_{body[:100]}", lead["id"]))
                    out["failed"] += 1
                c.commit()
        except Exception as e:
            pass

        out["results"].append({
            "id": lead["id"], "email": email, "status": status,
            "ok": ok, "provider": provider,
            "error_body": body[:300] if not ok else None,  # r39.2 — surface Resend error
        })

    out["elapsed_ms"] = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    return jsonify(out), 200 if out["failed"] == 0 else 207


@outreach_cron_bp.route("/status", methods=["GET"])
def status():
    out = {
        "blueprint": "outreach_cron_bp",
        "provider": _provider() or "none_configured",
        "from_email": FROM_EMAIL,
        "needed_env": {
            "RESEND_API_KEY": bool(RESEND_KEY),
            "SENDGRID_API_KEY": bool(SENDGRID_KEY),
            "OUTREACH_FROM_EMAIL": bool(os.environ.get("OUTREACH_FROM_EMAIL")),
        },
    }
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM identified_checkout_signals WHERE outreach_sent=FALSE")
                out["awaiting_outreach"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM identified_checkout_signals WHERE outreach_sent=TRUE")
                out["already_sent"] = cur.fetchone()[0]
        except Exception as e:
            out["db_error"] = str(e)[:140]
    return jsonify(out), 200
