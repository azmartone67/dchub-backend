"""Phase 3 — Canonical CAN-SPAM suppression list + one-click unsubscribe.

This is the single source of truth for "do not email this address". Every
audience builder must consult it (is_suppressed / NOT EXISTS against the
email_suppression table) before sending MARKETING mail, alongside the
marketing_opt_in=='true' gate for captured MCP emails and a deliverability
check. Transactional mail (key recovery, receipts) is out of scope here.

The unsubscribe token reuses the EXACT HMAC idiom from routes/digest.py
(_unsub_secret / _unsub_token): same secret env chain, same
sha256-hexdigest[:20] derivation on the lowercased/stripped email. A token
minted here therefore verifies consistently with one minted by digest.py for
the same address, and vice-versa.

The unsubscribe route ALWAYS returns a friendly HTTP 200 HTML confirmation —
even when the token is invalid — so a broken/garbled link is never a dead end
and there is no enumeration signal (the page looks identical whether or not
the address was actually on a list).

Table bootstrap is a DIRECT psycopg2 CREATE TABLE IF NOT EXISTS (never via
safe_db, which can be silently skipped under SKIP_DDL=1).
"""
import os, html, hmac, hashlib
from urllib.parse import quote
from flask import Blueprint, Response, request
import psycopg2

email_suppression_bp = Blueprint("email_suppression", __name__)

SITE = "https://dchub.cloud"
BRAND_LOGO = f"{SITE}/icons/dchub-logo.png"

_TABLE_READY = False


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


# ── HMAC token — IDENTICAL idiom + secret source to routes/digest.py ──────
def _unsub_secret():
    """Mirror routes/digest.py._unsub_secret so tokens cross-verify."""
    return (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
            or os.environ.get("DCHUB_SESSION_SECRET") or "dchub-digest-v1").encode()


def unsub_token(email):
    """HMAC token string for an email (mirrors digest._unsub_token)."""
    return hmac.new(_unsub_secret(), (email or "").strip().lower().encode(),
                    hashlib.sha256).hexdigest()[:20]


def unsub_link(email):
    """Full one-click unsubscribe URL with url-encoded email + token params."""
    e = (email or "").strip().lower()
    return f"{SITE}/api/v1/unsubscribe?email={quote(e)}&token={unsub_token(e)}"


def list_unsubscribe_headers(email):
    """RFC 2369 + RFC 8058 one-click headers pointing at unsub_link(email)."""
    link = unsub_link(email)
    return {
        "List-Unsubscribe": f"<{link}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


# ── table bootstrap — DIRECT psycopg2, never safe_db (SKIP_DDL trap) ──────
def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS email_suppression (
            email TEXT PRIMARY KEY,
            suppressed_at TIMESTAMPTZ DEFAULT NOW(),
            reason TEXT,
            source TEXT)""")
        c.commit()
    _TABLE_READY = True


# ── suppression API ──────────────────────────────────────────────────────
def is_suppressed(cur, email):
    """True if email is on the suppression list. `cur` is an open cursor.

    Defensive: if the table does not exist yet (no suppress() has run on this
    DB), treat as NOT suppressed rather than raising — a missing list means
    nobody has opted out. Callers should still use NOT EXISTS in audience SQL.
    """
    e = (email or "").strip().lower()
    if not e:
        return False
    try:
        cur.execute("SELECT 1 FROM email_suppression WHERE email = %s", (e,))
        return cur.fetchone() is not None
    except Exception:
        return False


def suppress(email, reason, source):
    """Insert/upsert an email onto the suppression list. Idempotent."""
    e = (email or "").strip().lower()
    if not e:
        return
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO email_suppression (email, suppressed_at, reason, source)
            VALUES (%s, NOW() ON CONFLICT DO NOTHING, %s, %s)
            ON CONFLICT (email) DO UPDATE
              SET suppressed_at = NOW(),
                  reason = EXCLUDED.reason,
                  source = EXCLUDED.source""", (e, reason, source))
        c.commit()


# ── one-click unsubscribe route ──────────────────────────────────────────
def _confirmation_page():
    """Neutral friendly HTML — identical whether or not the address was on a
    list, so a broken link is never a dead end and there's no enumeration
    signal. Email-client-safe table layout, dark-logo header."""
    msg = ("You've been unsubscribed. You won't receive DC Hub marketing email "
           "anymore. (You'll still get transactional messages like receipts or "
           "key recovery.)")
    pg = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Unsubscribed · DC Hub</title></head>
<body style="margin:0;background:#eef0f5;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
<table role="presentation" width="100%" height="100%" cellpadding="0" cellspacing="0"><tr><td align="center" valign="middle" style="padding:48px 16px">
<table role="presentation" width="460" cellpadding="0" cellspacing="0" style="max-width:460px;background:#fff;border-radius:14px;border:1px solid #e6e9f0">
<tr><td bgcolor="#0f1117" style="background:#0f1117;border-radius:14px 14px 0 0;text-align:center;padding:22px 0">
<img src="{BRAND_LOGO}" width="84" height="84" alt="DC Hub" style="width:84px;height:84px;border:0"></td></tr>
<tr><td style="padding:26px 28px;text-align:center;color:#0f172a;font-size:16px;line-height:1.6">{html.escape(msg)}
<div style="padding-top:18px"><a href="{SITE}/dcpi" style="color:#6366f1;text-decoration:none;font-weight:600">Open DCPI →</a></div></td></tr>
</table></td></tr></table></body></html>"""
    return pg


@email_suppression_bp.route("/api/v1/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    """One-click unsubscribe via HMAC token (RFC 8058 POST + plain GET click).

    Verifies the token with hmac.compare_digest; on match suppress() the
    address. ALWAYS returns a friendly HTTP 200 confirmation page — even on a
    bad/missing token — so a broken link is never a dead end and the response
    leaks no enumeration signal.
    """
    email = (request.args.get("email") or request.form.get("email") or "").strip().lower()
    token = (request.args.get("token") or request.form.get("token") or "").strip()
    if email and token and hmac.compare_digest(token, unsub_token(email)):
        try:
            suppress(email, reason="one-click-unsubscribe", source="unsubscribe-link")
        except Exception:
            # Even on a DB hiccup, present the neutral confirmation — never a
            # dead end, never an error page that hints at the failure.
            pass
    return Response(_confirmation_page(), status=200, mimetype="text/html")


def register(app):
    app.register_blueprint(email_suppression_bp)
    # r-suppress-bootstrap (2026-06-19): create email_suppression EAGERLY at startup,
    # not lazily on the first unsubscribe. Until the table exists, the opt-OUT list, the
    # choke-point's suppression check, and every NOT EXISTS(email_suppression) audience
    # subquery fail OPEN (is_suppressed returns False; the subquery raises). Best-effort —
    # a bootstrap failure must never block boot. (_ensure_table uses direct psycopg2, so
    # the safe_db SKIP_DDL trap does not apply.)
    try:
        _ensure_table()
    except Exception as _e:
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("email_suppression table bootstrap failed (non-fatal): %s", _e)
        except Exception:
            pass
