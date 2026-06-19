"""Self-serve key recovery — POST /api/v1/keys/recover (2026-06-18).

The companion to /api/v1/keys/identify: once an agent has bound an email to a
key (identify), the human (or the agent on their behalf) can recover that key
to the bound inbox without us ever putting the raw key in an HTTP response.

Security model (deliberate, documented):
  * ENUMERATION-SAFE. The response is ALWAYS the same neutral 200 —
    {ok:true, message:"If a DC Hub key is tied to that email, we've sent it
    there."} — whether or not a key exists, whether or not we actually sent
    anything, and even when we're rate-limited. An attacker probing emails
    learns nothing. The raw key is NEVER returned in the body; it only ever
    leaves the system addressed to the email ALREADY bound to that key.
  * The key is sent to the BOUND address we look up, never to an
    attacker-supplied address that differs from what's on file. (Here they're
    the same string by construction — you recover the email you typed — but the
    point is we email what the DB says, not what the request claims unlocks.)
  * PAID account → we email a dashboard SIGN-IN link, not a raw key (paid keys
    rotate in the dashboard; the password-reset / login flow is the right
    recovery surface). Pure FREE key → we email the key itself, to its own
    bound address, with explicit "you or your agent requested this" copy.
  * RATE-LIMITED, soft-fail: <=3 / email / day AND <=10 / ip / day, via a
    best-effort in-memory per-process counter (a backstop, not a fortress —
    a real attacker hits many processes; the enumeration-safety above is the
    actual defense). Over-limit STILL returns the neutral 200, leaking nothing.

Soft-fail contract: any error (DB down, Resend down, rate-limiter glitch) still
returns the SAME neutral 200. We never hand a caller a 4xx/5xx that would tell
them their probe "worked" or "failed".

Honesty: we email TRANSACTIONAL content only (the key / a sign-in link). No
digest, no market-alert promise — those were stripped. marketing_opt_in stays
off; nothing here implies a subscription.
"""
import os
import threading
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, jsonify, request

keys_recover_bp = Blueprint("keys_recover", __name__)

# Same neutral line for every outcome — the heart of enumeration-safety.
_NEUTRAL = "If a DC Hub key is tied to that email, we've sent it there."

# Best-effort in-memory rate limiter. Per-process, resets each UTC day. This is
# a backstop against accidental loops / lazy probing, NOT a security boundary —
# the neutral response is what actually prevents enumeration.
_RL_LOCK = threading.Lock()
_RL_DAY = {"day": None}          # current UTC date string
_RL_EMAIL: dict = {}             # email -> count today
_RL_IP: dict = {}                # ip    -> count today
_MAX_PER_EMAIL = 3
_MAX_PER_IP = 10


def _rate_ok(email: str, ip: str) -> bool:
    """True if this (email, ip) is still under the daily caps. Soft-fail OPEN on
    any internal error (better to send a legit recovery than to wedge it), but
    the caller renders the SAME neutral 200 either way, so a False here only
    suppresses the actual send."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with _RL_LOCK:
            if _RL_DAY["day"] != today:
                _RL_DAY["day"] = today
                _RL_EMAIL.clear()
                _RL_IP.clear()
            ec = _RL_EMAIL.get(email, 0)
            ic = _RL_IP.get(ip, 0)
            if ec >= _MAX_PER_EMAIL or ic >= _MAX_PER_IP:
                return False
            _RL_EMAIL[email] = ec + 1
            _RL_IP[ip] = ic + 1
            return True
    except Exception:
        return True


_EMAIL_RE = None


def _looks_like_email(s: str) -> bool:
    global _EMAIL_RE
    if _EMAIL_RE is None:
        import re
        _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    return bool(s) and len(s) <= 254 and bool(_EMAIL_RE.match(s))


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")


def _recover_html_key(email: str, api_key: str) -> str:
    import html as _html
    # Escape every interpolation: `email` is attacker-controlled (the permissive
    # _looks_like_email regex admits <>"'/), and api_key for defense-in-depth.
    # Without this a crafted address injects arbitrary HTML into outbound mail.
    email = _html.escape(email)
    api_key = _html.escape(api_key)
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:560px;margin:0 auto;color:#1a1a1a;line-height:1.55">
  <h2 style="font-weight:600;margin:0 0 4px">Your DC Hub API key 🛰️</h2>
  <p style="color:#666;margin:0 0 18px">You (or an AI agent acting for you) asked us to send the key tied to this email.</p>
  <p>Here is the DC Hub key bound to <b>{email}</b>:</p>
  <p style="margin:16px 0">
    <code style="background:#f4f4f5;border:1px solid #e4e4e7;border-radius:6px;padding:10px 14px;display:inline-block;font-size:14px">{api_key}</code>
  </p>
  <p style="color:#444;font-size:14px">Add it to your MCP client as header <code>X-API-Key</code>, or pass it to the REST API the same way. Keep it private — anyone with this key can use your quota.</p>
  <p style="color:#444;font-size:14px">If you didn't request this, you can ignore this email — nothing changed, and the key was only ever sent to this address.</p>
  <p style="margin-top:20px">— DC Hub<br><span style="color:#888;font-size:13px">dchub.cloud</span></p>
</div>"""


def _recover_html_signin(email: str) -> str:
    import html as _html
    email = _html.escape(email)  # attacker-controlled — escape before HTML interpolation
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:560px;margin:0 auto;color:#1a1a1a;line-height:1.55">
  <h2 style="font-weight:600;margin:0 0 4px">Sign in to DC Hub 🛰️</h2>
  <p style="color:#666;margin:0 0 18px">You (or an AI agent acting for you) asked us to recover access for this email.</p>
  <p>Your account <b>{email}</b> has a paid plan. For security we don't email paid keys — sign in to your dashboard to copy your API key, rotate it, and see usage:</p>
  <p style="margin:18px 0">
    <a href="https://dchub.cloud/login" style="background:#111;color:#fff;text-decoration:none;padding:11px 20px;border-radius:7px;font-weight:600;display:inline-block">Sign in to your dashboard →</a>
  </p>
  <p style="color:#444;font-size:14px">Forgot your password? Use "Reset password" on the sign-in page — it goes to <b>{email}</b>.</p>
  <p style="color:#444;font-size:14px">If you didn't request this, you can safely ignore this email — nothing changed.</p>
  <p style="margin-top:20px">— DC Hub<br><span style="color:#888;font-size:13px">dchub.cloud</span></p>
</div>"""


def _send(to_email: str, subject: str, html: str) -> bool:
    """Send via the same Resend path the rest of the app uses. Soft-fail to
    False — the HTTP response is neutral regardless."""
    try:
        from main import _resend_email
        return bool(_resend_email(to_email, subject, html,
                                  from_email="hello@dchub.cloud",
                                  from_name="DC Hub"))
    except Exception:
        return False


def _lookup_and_send(email: str) -> None:
    """Resolve the email to a recovery action and send it. Best-effort, fully
    swallowed — NOTHING about success/failure reaches the caller. Order:
      1) paid users account  -> sign-in link (never a raw key)
      2) mcp_dev_keys.email   -> the free key itself, to its own bound address
      3) auto_trial_keys.operator_email -> that trial key, same treatment
    """
    dsn = _dsn()
    if not dsn:
        return
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                # 1) Paid account? Send the dashboard sign-in link, not a key.
                try:
                    cur.execute(
                        "SELECT plan FROM users "
                        "WHERE LOWER(email) = %s "
                        "  AND COALESCE(plan,'free') IN ('pro','founding','enterprise','starter','developer') "
                        "  AND COALESCE(subscription_status,'') = 'active' "
                        "LIMIT 1",
                        (email,),
                    )
                    prow = cur.fetchone()
                except Exception:
                    prow = None
                if prow:
                    _send(email, "Recover your DC Hub access",
                          _recover_html_signin(email))
                    return

                # 2) Pure free key bound to this email.
                try:
                    cur.execute(
                        "SELECT api_key FROM mcp_dev_keys "
                        "WHERE LOWER(email) = %s "
                        "  AND COALESCE(status,'active') = 'active' "
                        "ORDER BY 1 LIMIT 1",
                        (email,),
                    )
                    krow = cur.fetchone()
                except Exception:
                    krow = None
                if krow and krow[0]:
                    _send(email, "Your DC Hub API key",
                          _recover_html_key(email, krow[0]))
                    return

                # 3) Auto-trial key bound via operator_email.
                try:
                    cur.execute(
                        "SELECT api_key FROM auto_trial_keys "
                        "WHERE LOWER(operator_email) = %s "
                        "  AND (expires_at IS NULL OR expires_at > NOW()) "
                        "ORDER BY expires_at DESC LIMIT 1",
                        (email,),
                    )
                    trow = cur.fetchone()
                except Exception:
                    trow = None
                if trow and trow[0]:
                    _send(email, "Your DC Hub trial key",
                          _recover_html_key(email, trow[0]))
                    return
    except Exception:
        # Swallow everything — recovery is best-effort and the response is
        # neutral no matter what.
        return


@keys_recover_bp.route("/api/v1/keys/recover", methods=["POST"])
def recover_key():
    """Enumeration-safe key recovery. Always 200 + the same neutral message.

    Body: {"email": "user@example.com"}
    """
    body = request.get_json(silent=True) or {}
    email = (str(body.get("email") or request.args.get("email") or "")).strip().lower()
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()

    # Even on bad input we return the SAME neutral 200 — never reveal that the
    # input was rejected (which would itself be an oracle).
    if _looks_like_email(email) and _rate_ok(email, ip):
        # Do the lookup+send inline but fully swallowed. (Kept synchronous so a
        # one-replica backend doesn't drop the work; it's a single short query +
        # one Resend POST, both with their own timeouts.)
        try:
            _lookup_and_send(email)
        except Exception:
            pass

    return jsonify(ok=True, message=_NEUTRAL), 200


def register(app):
    app.register_blueprint(keys_recover_bp)
    return True
