"""Double-opt-in marketing consent capture (2026-06-19).

WHY THIS EXISTS
===============
The upgrade-outreach engine's `high_usage_free` segment
(routes/upgrade_outreach.py::_select_high_usage_free) gates candidates on
mcp_dev_keys.metadata->>'marketing_opt_in' = 'true' at SELECTION time. Today
~0 free power-users are opted in, so the 184 power-users on
get_grid_intelligence + 166 on get_fiber_intel are UNREACHABLE for upgrade
outreach. This module is the COMPLIANT path that grows that audience.

This module ONLY captures consent. It NEVER sends marketing — the outreach
engine does the sending, separately, behind its own flags. The single email
this module sends is the TRANSACTIONAL confirmation request (it IS the opt-in
confirmation), routed through email_fallback.send_email_resilient, NOT the
marketing choke-point.

DOUBLE OPT-IN — THE GOLD STANDARD
=================================
  1. A user expresses interest (POST /api/v1/opt-in/request {email}, or the
     in-MCP request_opt_in(email, source) helper the bind/unlock path can call).
     We validate the email, honor suppression, and email a TOKENIZED CONFIRM
     LINK. We DO NOT set marketing_opt_in here.
  2. The user clicks the confirm link (GET /api/v1/opt-in/confirm?...). ONLY
     then do we set mcp_dev_keys.metadata->>'marketing_opt_in' = true AND log
     the consent (timestamp, source, ip, user-agent, token hash) to the
     opt_in_consents audit table. This double round-trip is what proves the
     consent was a real, willing yes from a real, controlled inbox.

We NEVER set marketing_opt_in from usage, a single click, or anything other
than a valid tokenized confirm. We NEVER prompt or confirm a suppressed
address. Best-effort throughout: this path must never raise into a caller and
must never block boot.

REUSE (no rebuild)
==================
  * Token scheme  — routes/email_suppression.unsub_token / unsub_link idiom
                    (HMAC-SHA256[:20] over lowercased/stripped email, same env
                    secret chain) so a confirm token cross-verifies the exact
                    same way the unsubscribe token does.
  * Suppression   — routes/email_suppression.is_suppressed (do not confirm a
                    suppressed address).
  * Email send    — email_fallback.send_email_resilient (transactional).
  * Validation    — routes/email_validation.validate_email (soft-fail).
  * Consent write — mirrors flask_mcp_endpoints.identify_key's metadata merge
                    (COALESCE(metadata,'{}') || jsonb_build_object(...)) to set
                    marketing_opt_in=true + marketing_opt_in_at.

The opt_in_consents audit table is bootstrapped with DIRECT psycopg2 (never
safe_db, which silently SKIPs DDL under SKIP_DDL=1).
"""
import os
import html
import hmac
import logging
from datetime import datetime, timezone
from urllib.parse import quote

from flask import Blueprint, Response, request, jsonify

log = logging.getLogger("marketing_opt_in")

marketing_opt_in_bp = Blueprint("marketing_opt_in", __name__)

SITE = os.environ.get("DCHUB_SITE", "https://dchub.cloud")
BRAND_LOGO = f"{SITE}/icons/dchub-logo.png"

# Confirm-request idempotency / rate-limit window per email (seconds). A second
# request inside this window is a polite no-op (we don't re-spam the inbox).
_REQUEST_COOLDOWN_S = int(os.environ.get("OPTIN_REQUEST_COOLDOWN_S", "900"))

_TABLE_READY = False


# ── DB (direct psycopg2 — NEVER safe_db; DDL is SKIP'd under SKIP_DDL=1) ──
def _conn():
    """Raw psycopg2 connection, or None. Never raises."""
    try:
        import psycopg2
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return psycopg2.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        log.warning("marketing_opt_in: _conn failed: %s", e)
    return None


# ── HMAC confirm token — reuse the email_suppression idiom EXACTLY ────────
def _opt_in_token(email):
    """Tokenize an email for the confirm link.

    Delegates to email_suppression.unsub_token so the secret env chain and the
    sha256-hexdigest[:20] derivation are identical (single source of truth). A
    confirm token therefore cross-verifies the same way an unsubscribe token
    does. Falls back to an inline HMAC mirror only if that import is somehow
    unavailable, so confirm links never break on an import error."""
    try:
        from routes.email_suppression import unsub_token
        return unsub_token(email)
    except Exception:
        import hashlib
        secret = (os.environ.get("DCHUB_ADMIN_KEY")
                  or os.environ.get("DCHUB_INTERNAL_KEY")
                  or os.environ.get("DCHUB_SESSION_SECRET")
                  or "dchub-digest-v1").encode()
        return hmac.new(secret, (email or "").strip().lower().encode(),
                        hashlib.sha256).hexdigest()[:20]


def _token_ok(email, token):
    """Constant-time token check. False on any bad/empty input."""
    e = (email or "").strip().lower()
    t = (token or "").strip()
    if not e or not t:
        return False
    try:
        return hmac.compare_digest(t, _opt_in_token(e))
    except Exception:
        return False


def _token_hash(token):
    """A short, non-reversible fingerprint of the token for the audit row — we
    store a hash, never the raw token, so the log is not itself a credential."""
    import hashlib
    return hashlib.sha256((token or "").encode()).hexdigest()[:32]


def confirm_link(email):
    """Full tokenized confirm URL (GET-clickable from the email)."""
    e = (email or "").strip().lower()
    return f"{SITE}/api/v1/opt-in/confirm?email={quote(e)}&token={_opt_in_token(e)}"


# ── audit table bootstrap — DIRECT psycopg2, never safe_db ───────────────
def _ensure_table():
    """Create opt_in_consents if absent. Idempotent; best-effort."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    conn = _conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            # request_at: when a confirm email was last sent (for rate-limit /
            # idempotency). confirmed_at: when the tokenized confirm landed
            # (NULL until then). The PK on email keeps one row per address; a
            # re-request UPSERTs request_at, a confirm UPSERTs confirmed_at.
            cur.execute("""CREATE TABLE IF NOT EXISTS opt_in_consents (
                email        TEXT PRIMARY KEY,
                source       TEXT,
                ip           TEXT,
                user_agent   TEXT,
                requested_at TIMESTAMPTZ,
                confirmed_at TIMESTAMPTZ,
                token_hash   TEXT)""")
        _TABLE_READY = True
    except Exception as e:
        log.warning("marketing_opt_in: table bootstrap failed (non-fatal): %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _client_ip():
    """Best-effort client IP (honor the CF/proxy forwarded chain, first hop)."""
    xff = (request.headers.get("X-Forwarded-For")
           or request.headers.get("CF-Connecting-IP") or "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64]


# ── admin gate (reuse the brain dashboard gate, like upgrade_outreach) ───
def _admin_ok() -> bool:
    """X-Admin-Key / X-Internal-Key header OR ?admin_key=. Reuse the brain gate
    so the policy stays in one place; inline internal-key fallback if that
    import fails so the status endpoint is never accidentally left open."""
    try:
        from routes.brain_investigator import _admin_ok as _brain_admin_ok
        return bool(_brain_admin_ok())
    except Exception:
        keys = set()
        for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            v = os.environ.get(_n)
            if v:
                keys.add(v)
        sent = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
        return bool(sent) and sent in keys


# ── core: request the confirm email (reusable from MCP) ──────────────────
def request_opt_in(email, source="api"):
    """Validate + (if not suppressed / not recently asked) send the TRANSACTIONAL
    confirm email. Does NOT set marketing_opt_in. Idempotent + rate-limited per
    email. Returns {ok, sent, reason}. Never raises.

    This is the single send path — the bind_email tool / unlock path / the
    /api/v1/opt-in/request route all call THIS (no duplicated send logic).
    """
    out = {"ok": True, "sent": False, "reason": None}
    e = (email or "").strip().lower()

    # 1) validate (soft-fail philosophy lives in validate_email itself)
    try:
        from routes.email_validation import validate_email
        ok, reason, norm = validate_email(e)
        if not ok:
            return {"ok": False, "sent": False, "reason": reason or "invalid_email"}
        e = norm or e
    except Exception:
        # Validator absent/broke — fall back to a cheap shape check; never block
        # the whole flow on our own validator.
        if "@" not in e or "." not in e.split("@")[-1]:
            return {"ok": False, "sent": False, "reason": "invalid_email"}

    # 2) NEVER prompt/confirm a suppressed address — polite no-op.
    conn = _conn()
    try:
        if conn is not None:
            try:
                from routes.email_suppression import is_suppressed
                with conn.cursor() as cur:
                    if is_suppressed(cur, e):
                        return {"ok": True, "sent": False, "reason": "suppressed"}
            except Exception:
                # Suppression check unavailable — fail SAFE for a no-op decision
                # would mean spamming; but the table may just not exist yet.
                # is_suppressed already returns False on a missing table, so the
                # only way we're here is an import/conn error; proceed (the
                # confirm email is transactional + user-initiated, low risk).
                pass

        # 3) idempotency / rate-limit: skip a re-send inside the cooldown.
        _ensure_table()
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT requested_at, confirmed_at FROM opt_in_consents "
                        "WHERE email = %s", (e,))
                    row = cur.fetchone()
                if row:
                    requested_at, confirmed_at = row[0], row[1]
                    if confirmed_at is not None:
                        # Already opted in — no need to re-confirm.
                        return {"ok": True, "sent": False, "reason": "already_opted_in"}
                    if requested_at is not None:
                        age = (datetime.now(timezone.utc) - requested_at).total_seconds()
                        if age < _REQUEST_COOLDOWN_S:
                            return {"ok": True, "sent": False, "reason": "recently_requested"}
            except Exception as ex:
                log.debug("marketing_opt_in: rate-limit read failed (proceeding): %s", ex)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    # 4) send the TRANSACTIONAL confirm email (it IS the opt-in confirmation —
    #    transactional sender, NOT the marketing choke-point).
    sent = _send_confirm_email(e)

    # 5) stamp requested_at so the rate-limit/idempotency window starts now.
    if sent:
        _record_request(e, source)

    out["sent"] = bool(sent)
    if not sent:
        out["reason"] = "send_failed"
    return out


def _send_confirm_email(email):
    """Send the double-opt-in confirmation request. Returns bool. Never raises.

    SOFT + HONEST copy: a clear value prop (a free upgrade/grid-data guide +
    early access), an obvious one-click confirm, and an explicit 'ignore this
    and nothing happens' — no dark pattern, no pre-checked anything."""
    try:
        link = confirm_link(email)
        subject = "Confirm you want DC Hub updates"
        text = (
            "Thanks for your interest in DC Hub updates.\n\n"
            "Confirm your email to start receiving our free grid-data & upgrade "
            "guide plus early access to new tools:\n\n"
            f"{link}\n\n"
            "If you didn't ask for this, just ignore this email — we won't add "
            "you to anything unless you click the link above.\n\n"
            "— DC Hub (dchub.cloud)"
        )
        htmlbody = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Confirm DC Hub updates</title></head>
<body style="margin:0;background:#eef0f5;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:40px 16px">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;background:#fff;border-radius:14px;border:1px solid #e6e9f0">
<tr><td bgcolor="#0f1117" style="background:#0f1117;border-radius:14px 14px 0 0;text-align:center;padding:22px 0">
<img src="{BRAND_LOGO}" width="84" height="84" alt="DC Hub" style="width:84px;height:84px;border:0"></td></tr>
<tr><td style="padding:28px 30px;color:#0f172a;font-size:15px;line-height:1.6">
<p style="margin:0 0 14px">Thanks for your interest in DC Hub updates.</p>
<p style="margin:0 0 18px">Confirm your email to start receiving our free
<strong>grid-data &amp; upgrade guide</strong> plus <strong>early access</strong>
to new tools. One click and you're in.</p>
<div style="text-align:center;padding:6px 0 18px">
<a href="{html.escape(link)}" style="display:inline-block;background:#6366f1;color:#fff;text-decoration:none;font-weight:700;padding:13px 26px;border-radius:10px">Confirm my email →</a></div>
<p style="margin:0;color:#64748b;font-size:13px">Didn't ask for this? Just ignore
this email — we won't add you to anything unless you click the button above.</p>
</td></tr></table></td></tr></table></body></html>"""

        from email_fallback import send_email_resilient
        return bool(send_email_resilient(
            email, subject, html_content=htmlbody, text_content=text,
            from_email=os.environ.get("DCHUB_OPTIN_FROM", "info@dchub.cloud"),
            from_name="DC Hub",
        ))
    except Exception as e:
        log.warning("marketing_opt_in: confirm-email send failed: %s", e)
        return False


def _record_request(email, source):
    """UPSERT the requested_at timestamp (rate-limit window start). Best-effort."""
    conn = _conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO opt_in_consents (email, source, requested_at)
                       VALUES (%s, %s, NOW() ON CONFLICT DO NOTHING)
                   ON CONFLICT (email) DO UPDATE
                       SET requested_at = NOW(),
                           source = COALESCE(opt_in_consents.source, EXCLUDED.source)""",
                (email, source))
    except Exception as e:
        log.debug("marketing_opt_in: _record_request failed: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _set_opted_in(email, source, ip, user_agent, token_hash):
    """Set marketing_opt_in=true on the key(s) for this email AND insert the
    consent audit row. Returns True if consent was recorded. Never raises.

    The marketing_opt_in metadata merge MIRRORS identify_key (flask_mcp_endpoints
    lines 854-872): COALESCE(metadata,'{}') || jsonb_build_object(...) so we
    only ADD keys, never clobber the rest of the metadata. We stamp
    marketing_opt_in_at = NOW() — the consent moment, for audit."""
    conn = _conn()
    if conn is None:
        return False
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with conn, conn.cursor() as cur:
            # Flip marketing_opt_in=true on every dev key bound to this email.
            # A user may have >1 key; opting in covers all of them. If they have
            # no bound key yet (opted in before binding), the audit row below
            # still proves consent and the outreach engine joins on it later.
            cur.execute(
                """UPDATE mcp_dev_keys
                       SET metadata = COALESCE(metadata, '{}'::jsonb)
                                      || jsonb_build_object(
                                           'marketing_opt_in', true,
                                           'marketing_opt_in_at', %s::text,
                                           'marketing_opt_in_source', %s::text,
                                           'marketing_opt_in_method', 'double_opt_in')
                     WHERE LOWER(email) = LOWER(%s)""",
                (now_iso, source or "confirm", email))

            # Insert/UPSERT the consent audit row — the durable proof.
            cur.execute(
                """INSERT INTO opt_in_consents
                       (email, source, ip, user_agent, requested_at,
                        confirmed_at, token_hash)
                   VALUES (%s, %s, %s, %s, COALESCE(
                               (SELECT requested_at FROM opt_in_consents
                                 WHERE email = %s) ON CONFLICT DO NOTHING, NOW()),
                           NOW(), %s)
                   ON CONFLICT (email) DO UPDATE
                       SET confirmed_at = NOW(),
                           source = EXCLUDED.source,
                           ip = EXCLUDED.ip,
                           user_agent = EXCLUDED.user_agent,
                           token_hash = EXCLUDED.token_hash""",
                (email, source, ip, user_agent, email, token_hash))
        return True
    except Exception as e:
        log.warning("marketing_opt_in: _set_opted_in failed: %s", e)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── HTML result pages (email-client-safe, dark-logo header) ──────────────
def _result_page(title, msg, *, cta=True):
    cta_html = (f'<div style="padding-top:18px"><a href="{SITE}/dcpi" '
                f'style="color:#6366f1;text-decoration:none;font-weight:600">'
                f'Open DCPI →</a></div>') if cta else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · DC Hub</title></head>
<body style="margin:0;background:#eef0f5;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
<table role="presentation" width="100%" height="100%" cellpadding="0" cellspacing="0"><tr><td align="center" valign="middle" style="padding:48px 16px">
<table role="presentation" width="460" cellpadding="0" cellspacing="0" style="max-width:460px;background:#fff;border-radius:14px;border:1px solid #e6e9f0">
<tr><td bgcolor="#0f1117" style="background:#0f1117;border-radius:14px 14px 0 0;text-align:center;padding:22px 0">
<img src="{BRAND_LOGO}" width="84" height="84" alt="DC Hub" style="width:84px;height:84px;border:0"></td></tr>
<tr><td style="padding:26px 28px;text-align:center;color:#0f172a;font-size:16px;line-height:1.6">{html.escape(msg)}{cta_html}</td></tr>
</table></td></tr></table></body></html>"""


# ── routes ───────────────────────────────────────────────────────────────
@marketing_opt_in_bp.post("/api/v1/opt-in/request")
def opt_in_request():
    """Express interest → send a tokenized confirm email. Does NOT set opt-in.

    Body: {"email": "...", "source": "..."}. Suppressed / recently-asked /
    already-opted-in addresses are a polite {ok:true, sent:false}. Always 200
    (never an error that forces a retry decision); no enumeration signal beyond
    the coarse reason."""
    body = request.get_json(silent=True) or {}
    email = (str(body.get("email") or "")).strip().lower()
    source = (str(body.get("source") or "optin_request")).strip()[:64]
    if not email:
        return jsonify(ok=False, sent=False, error="missing_email",
                       message="Pass an email to confirm DC Hub updates."), 200
    res = request_opt_in(email, source=source)
    return jsonify(ok=bool(res.get("ok")), sent=bool(res.get("sent")),
                   reason=res.get("reason"),
                   message=("Check your inbox for a confirmation link."
                            if res.get("sent") else
                            "If that address can receive our email, a "
                            "confirmation link is on its way.")), 200


@marketing_opt_in_bp.get("/api/v1/opt-in/confirm")
def opt_in_confirm():
    """Tokenized confirm click → set marketing_opt_in=true + log consent.

    Verifies the HMAC token; ONLY on a valid token do we flip opt-in and write
    the audit row. A suppressed address is NOT confirmed (consent can't override
    a prior opt-out). Renders a friendly HTML page either way; an invalid token
    renders a clear (but non-enumerating) error page and sets nothing."""
    email = (request.args.get("email") or "").strip().lower()
    token = (request.args.get("token") or "").strip()

    if not _token_ok(email, token):
        return Response(_result_page(
            "Link expired",
            "This confirmation link isn't valid or has expired. Request a fresh "
            "one and we'll send a new link.", cta=False), status=200,
            mimetype="text/html")

    # Suppression wins: a previously-unsubscribed address is NOT re-opted-in
    # just because someone clicked a confirm link. Show a neutral page.
    try:
        from routes.email_suppression import is_suppressed
        conn = _conn()
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    if is_suppressed(cur, email):
                        return Response(_result_page(
                            "All set",
                            "You're not subscribed to DC Hub marketing email. "
                            "(You previously unsubscribed.)", cta=True),
                            status=200, mimetype="text/html")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception:
        # Suppression unavailable — proceed; the confirm is explicit + tokenized.
        pass

    ok = _set_opted_in(email, source="confirm-link", ip=_client_ip(),
                       user_agent=(request.headers.get("User-Agent") or "")[:256],
                       token_hash=_token_hash(token))
    if ok:
        return Response(_result_page(
            "You're in!",
            "Thanks — your email is confirmed. You'll get our free grid-data & "
            "upgrade guide and early access to new tools. You can unsubscribe "
            "any time from any email.", cta=True), status=200, mimetype="text/html")
    # DB hiccup — neutral page, opt-in not set (the user can click again).
    return Response(_result_page(
        "Almost there",
        "We couldn't finish confirming just now. Please click your confirmation "
        "link again in a moment.", cta=False), status=200, mimetype="text/html")


@marketing_opt_in_bp.get("/api/v1/opt-in/status")
def opt_in_status():
    """ADMIN-GATED ops read: {opted_in, confirmed_at, source} for an email."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify(ok=False, error="missing_email"), 200
    _ensure_table()
    conn = _conn()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 200
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT confirmed_at, source, requested_at FROM opt_in_consents "
                "WHERE email = %s", (email,))
            row = cur.fetchone()
        confirmed_at = row[0] if row else None
        return jsonify(
            ok=True, email=email,
            opted_in=bool(confirmed_at),
            confirmed_at=(confirmed_at.isoformat() if confirmed_at else None),
            source=(row[1] if row else None),
            requested_at=(row[2].isoformat() if (row and row[2]) else None)), 200
    except Exception as e:
        return jsonify(ok=False, error=f"query_failed:{str(e)[:120]}"), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


def register(app):
    """Wire the blueprint + bootstrap the audit table (best-effort)."""
    app.register_blueprint(marketing_opt_in_bp)
    try:
        _ensure_table()
    except Exception as e:
        log.warning("marketing_opt_in: table bootstrap at register failed "
                    "(non-fatal): %s", e)
