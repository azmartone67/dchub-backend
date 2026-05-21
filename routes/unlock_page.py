"""
unlock_page.py — the value-moment magic-link page (identity-capture Increment 2).

Some MCP agents will relay "what's your email?" back to their human and
then POST it to /api/v1/keys/identify themselves. Others will just hand
their human a LINK. This is the path for the latter.

When an agent hits the daily-limit value moment, main.py's
_gate_mcp_result includes a `human_link`:
    https://dchub.cloud/unlock/<token>
The agent relays it. The human opens it, sees ONE email field (no
password, no signup form), submits — and the email is tied to the
agent's dev key, unlocking 4x the daily quota.

Flow:
  agent hits wall  ->  mint_unlock_token(api_key) -> short opaque token
  -> agent relays /unlock/<token>  ->  human opens it
  -> GET shows the one-field form
  -> POST ties email onto mcp_dev_keys (same effect as /keys/identify),
     marks the token used, fires an `email_captured` funnel event

Tokens: 24h expiry, single-use. The raw api_key is stored HERE (the
page has to write the email onto the key row) — but the token itself
is the opaque, short-lived, single-use credential, so the key is never
exposed in a URL or relayed to the human.
"""
import os
import re
import sys
import secrets
from datetime import datetime, timezone

from flask import Blueprint, request, Response

unlock_page_bp = Blueprint("unlock_page", __name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Mirror main.py's MCP_*_DAILY_LIMIT so the page's copy matches reality.
_FREE_LIMIT = int(os.environ.get("MCP_FREE_DAILY_LIMIT", "25"))
_IDENT_LIMIT = int(os.environ.get("MCP_IDENTIFIED_DAILY_LIMIT", "100"))


def _conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=8)
    except Exception as e:
        print(f"[unlock_page] connect failed: {e}", file=sys.stderr)
        return None


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS mcp_unlock_tokens (
    token       TEXT PRIMARY KEY,
    api_key     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),
    used_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS mcp_unlock_tokens_apikey_idx
    ON mcp_unlock_tokens(api_key, created_at DESC);
"""


def init_schema() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[unlock_page] init_schema failed: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


try:
    _SCHEMA_OK = init_schema()
except Exception:
    _SCHEMA_OK = False


def mint_unlock_token(api_key: str):
    """Mint — or reuse a fresh — single-use unlock token for this key.

    Returns the token string, or None if the DB is unavailable / no key.
    Reuses an existing unexpired+unused token so an agent that hits the
    wall repeatedly relays the SAME link instead of a new one each time.
    """
    if not api_key:
        return None
    c = _conn()
    if c is None:
        return None
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """SELECT token FROM mcp_unlock_tokens
                   WHERE api_key = %s AND used_at IS NULL AND expires_at > NOW()
                   ORDER BY created_at DESC LIMIT 1""",
                (api_key,))
            row = cur.fetchone()
            if row:
                return row[0]
            token = secrets.token_urlsafe(9)  # ~12 url-safe chars
            cur.execute(
                "INSERT INTO mcp_unlock_tokens (token, api_key) VALUES (%s, %s)",
                (token, api_key))
            return token
    except Exception as e:
        print(f"[unlock_page] mint_unlock_token failed: {e}", file=sys.stderr)
        return None
    finally:
        try: c.close()
        except Exception: pass


# Deliberately dead-simple page — one field, no password, no nav chrome.
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unlock DC Hub — __HEAD__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
*{box-sizing:border-box}body{margin:0;font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
background:#0a0a0f;color:#e8e8f0;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:1.5rem}
.card{background:#13141d;border:1px solid #262838;border-radius:16px;padding:2.5rem;max-width:440px;width:100%}
h1{font-size:1.5rem;margin:0 0 .5rem}p{color:#9ca3b8;line-height:1.55;margin:.4rem 0 1.3rem}
.unlocks{background:#0e1a14;border:1px solid #1c3a2a;border-radius:10px;padding:.9rem 1.1rem;margin:0 0 1.4rem;font-size:.92rem;color:#7ee2a8}
input{width:100%;padding:.85rem 1rem;font-size:1rem;border-radius:10px;border:1px solid #2e3045;
background:#0a0a12;color:#fff;margin-bottom:.9rem}
button{width:100%;padding:.9rem;font-size:1rem;font-weight:600;border:0;border-radius:10px;
background:#6366f1;color:#fff;cursor:pointer}button:hover{background:#5458e8}
.ok{color:#7ee2a8}.err{color:#f8a4a4}.muted{color:#6b6f85;font-size:.82rem;margin-top:1.2rem}
a{color:#818cf8}
</style></head><body><div class="card">__BODY__</div></body></html>"""

_FORM_BODY = """<h1>Unlock more — free</h1>
<p>Your AI assistant has been using DC Hub. Drop an email to lift its
daily limit and get market alerts. No password, no payment.</p>
<div class="unlocks">&#10003; __IDENT__ MCP calls/day (up from __FREE__)<br>&#10003; Weekly digest of the markets you query<br>&#10003; Alerts when a tracked market moves</div>
<form method="POST">
<input type="email" name="email" placeholder="you@company.com" required autofocus>
<button type="submit">Unlock free &rarr;</button>
</form>
<p class="muted">DC Hub &middot; data-center intelligence. Your email is used only for your key + the digest.</p>"""


def _render(head, body, status=200):
    html = _PAGE.replace("__HEAD__", head).replace("__BODY__", body)
    return Response(html, mimetype="text/html", status=status)


def _form_body(prefix=""):
    return prefix + (_FORM_BODY.replace("__IDENT__", str(_IDENT_LIMIT))
                                .replace("__FREE__", str(_FREE_LIMIT)))


@unlock_page_bp.route("/unlock/<token>", methods=["GET", "POST"])
def unlock(token):
    """The magic-link page. GET shows the one-field form; POST ties the
    email to the key behind the token and marks the token used."""
    token = (token or "").strip()
    if not token or len(token) > 64:
        return _render("Invalid link",
                       '<h1 class="err">Invalid link</h1><p>That unlock link '
                       "doesn't look right. Use the exact link your AI "
                       "assistant gave you.</p>", 400)

    c = _conn()
    if c is None:
        return _render("Try again",
                       '<h1 class="err">Briefly unavailable</h1><p>Our database '
                       "is briefly unavailable — try again in a minute.</p>", 503)
    try:
        with c, c.cursor() as cur:
            cur.execute(
                "SELECT api_key, used_at, expires_at FROM mcp_unlock_tokens WHERE token = %s",
                (token,))
            row = cur.fetchone()
            if not row:
                return _render("Invalid link",
                               '<h1 class="err">Link not found</h1><p>That unlock '
                               "link isn't recognized. Ask your AI assistant for "
                               "a fresh one.</p>", 404)
            api_key, used_at, expires_at = row
            if used_at:
                return _render("Already used",
                               '<h1 class="ok">Already unlocked &#10003;</h1>'
                               "<p>This key is already identified — your AI "
                               "assistant has the higher limit.</p>")
            if expires_at and expires_at < datetime.now(timezone.utc):
                return _render("Expired",
                               '<h1 class="err">Link expired</h1><p>Unlock links '
                               "last 24 hours. Ask your AI assistant for a fresh "
                               "one.</p>", 410)

            if request.method == "GET":
                return _render("Unlock more, free", _form_body())

            # ── POST: capture the email ──
            email = (request.form.get("email") or "").strip().lower()
            if not email or not _EMAIL_RE.match(email) or len(email) > 254:
                return _render("Check the email",
                               _form_body('<p class="err">That email looks off — '
                                          "please try again.</p>"), 400)

            cur.execute(
                """UPDATE mcp_dev_keys
                       SET email = %s,
                           metadata = COALESCE(metadata, '{}'::jsonb)
                                      || jsonb_build_object('identified_at', %s::text,
                                           'identify_source', 'unlock_page')
                     WHERE api_key = %s""",
                (email, datetime.now(timezone.utc).isoformat(), api_key))
            cur.execute(
                "UPDATE mcp_unlock_tokens SET used_at = NOW() WHERE token = %s",
                (token,))
    except Exception as e:
        print(f"[unlock_page] unlock failed: {e}", file=sys.stderr)
        return _render("Try again",
                       '<h1 class="err">We had a hiccup</h1><p>Couldn\'t save that '
                       "right now — try again in a minute.</p>", 500)
    finally:
        try: c.close()
        except Exception: pass

    # Funnel event — best effort. This is the unlock-page arm of the
    # anonymous -> known conversion (the /keys/identify endpoint is the
    # other arm).
    try:
        from routes.redeem_tracking import record_funnel_event
        record_funnel_event(
            "email_captured", source="unlock_page",
            user_agent=request.headers.get("User-Agent"),
            ip=(request.headers.get("X-Forwarded-For")
                or request.remote_addr or ""))
    except Exception:
        pass

    # Phase TT Increment 3: nurture — fire-and-forget welcome email.
    # Deduped per-key, never blocks. `email` + `api_key` are in scope
    # from the POST branch above.
    try:
        from routes.redeem_tracking import send_identify_welcome
        send_identify_welcome(email, api_key)
    except Exception:
        pass

    return _render("Unlocked",
                   '<h1 class="ok">Unlocked &#10003;</h1>'
                   f"<p>Your AI assistant's DC Hub key now gets "
                   f"<strong>{_IDENT_LIMIT} calls/day</strong> (up from "
                   f"{_FREE_LIMIT}), plus the weekly market digest. You can "
                   "close this tab.</p>"
                   '<p class="muted">Need 1,000/day + full data? '
                   '<a href="https://dchub.cloud/pricing">Developer plan — $49/mo</a></p>')
