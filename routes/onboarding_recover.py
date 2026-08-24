"""Onboarding recovery + welcome-email audit (2026-06-18).

Two gaps this closes:
  1. `welcome_email_log` was WRITE-ONLY — nothing ever read it, so "did this
     paying customer get their welcome email?" was unanswerable. GET
     /api/v1/admin/welcome-log makes it auditable.
  2. When the automatic welcome email hiccups (SendGrid cap, webhook race), there
     was no one-click way to recover a customer. POST /api/v1/admin/resend-welcome
     sends a clean onboarding email (via the verified Resend sender) + logs it.

Context: marvinvitcu@gmail.com (Starter $9/mo, 2026-06-18) paid, got a working key,
but Stripe sent no receipt and our welcome send wasn't verifiable — exactly the
case this tool is for. The email points to the dashboard for the key (we never
email a raw key).
"""
import os
import psycopg2
from flask import Blueprint, jsonify, request
from ai_surface_canon import canon_text
_CANON_FAC = canon_text("{canon_facilities}")
# ★2026-08-23 — the welcome email promised "4,000+ tracked M&A deals" against a
# live ~1,900 distinct: a >2x over-claim in the first thing a new payer reads,
# and a stale_markers value. Canon phrase, never a literal.
_CANON_DEALS = canon_text("{canon_deals}")

onboarding_recover_bp = Blueprint("onboarding_recover", __name__)


def _admin_ok() -> bool:
    keys = set()
    for n in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "INTERNAL_KEY"):
        v = os.environ.get(n)
        if v:
            keys.add(v)
    sent = (request.headers.get("X-Admin-Key")
            or request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return bool(sent) and sent in keys


def _welcome_html(name: str, plan: str, email: str) -> str:
    hi = f"Hi {name.split()[0]}," if name else "Hi there,"
    plan_label = (plan or "Starter").title()
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:560px;margin:0 auto;color:#1a1a1a;line-height:1.55">
  <h2 style="font-weight:600;margin:0 0 4px">Welcome to DC Hub 🛰️</h2>
  <p style="color:#666;margin:0 0 20px">The live infrastructure data layer for data centers & power.</p>
  <p>{hi}</p>
  <p>Your <b>{plan_label}</b> plan is active — thank you for subscribing. Here's everything you need to get started:</p>
  <p style="margin:18px 0">
    <a href="https://dchub.cloud/login" style="background:#111;color:#fff;text-decoration:none;padding:11px 20px;border-radius:7px;font-weight:600;display:inline-block">Sign in & get your API key →</a>
  </p>
  <p style="color:#444;font-size:14px">Sign in with <b>{email}</b> to open your dashboard, copy your API key, and see your usage.</p>
  <h3 style="font-size:15px;margin:22px 0 6px">Quick start</h3>
  <ul style="color:#444;font-size:14px;padding-left:18px;margin:0 0 16px">
    <li><b>REST API:</b> <code>curl https://dchub.cloud/api/v1/search/facilities -H "X-API-Key: &lt;your-key&gt;"</code></li>
    <li><b>MCP (for AI agents):</b> add <code>https://dchub.cloud/mcp</code> with header <code>X-API-Key: &lt;your-key&gt;</code></li>
    <li><b>Playground:</b> <a href="https://dchub.cloud/playground">dchub.cloud/playground</a> — try queries in the browser.</li>
  </ul>
  <p style="color:#444;font-size:14px">Your plan unlocks {_CANON_FAC} facilities across 170+ countries, DCPI market scores, live grid &amp; fiber data, and {_CANON_DEALS} tracked M&amp;A deals.</p>
  <p style="color:#444;font-size:14px">Questions, or something not working? Just reply to this email — it reaches me directly.</p>
  <p style="margin-top:20px">— Jonathan<br><span style="color:#888;font-size:13px">DC Hub · dchub.cloud</span></p>
</div>"""


@onboarding_recover_bp.route("/api/v1/admin/resend-welcome", methods=["POST"])
def resend_welcome():
    """Send a clean onboarding email to a (paying) customer + log it. Admin-gated.
    Body/params: email (required), name, plan."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    body = request.get_json(silent=True) or {}
    email = (request.args.get("email") or body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify(ok=False, error="valid ?email= required"), 400
    name = (request.args.get("name") or body.get("name") or "").strip()
    plan = (request.args.get("plan") or body.get("plan") or "Starter").strip()
    subject = "Welcome to DC Hub — your account is live"
    html = _welcome_html(name, plan, email)
    sent = False
    err = None
    try:
        from main import _resend_email
        sent = bool(_resend_email(email, subject, html,
                                  from_email="hello@dchub.cloud", from_name="Jonathan at DC Hub"))
    except Exception as e:
        err = str(e)[:200]
    try:
        from main import _log_welcome_email
        _log_welcome_email(email, f"{plan}:resend", "sent" if sent else "failed")
    except Exception:
        pass
    return jsonify(ok=sent, email=email, plan=plan, sent=sent, error=err), (200 if sent else 502)


@onboarding_recover_bp.route("/api/v1/admin/welcome-log", methods=["GET"])
def welcome_log():
    """Read welcome_email_log (the previously write-only table) so onboarding is
    auditable. Optional ?email= filter. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 500
    email = (request.args.get("email") or "").strip().lower()
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                if email:
                    cur.execute("SELECT * FROM welcome_email_log WHERE LOWER(email)=%s "
                                "ORDER BY 1 DESC LIMIT 50", (email,))
                else:
                    cur.execute("SELECT * FROM welcome_email_log ORDER BY 1 DESC LIMIT 50")
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return jsonify(ok=True, email=email or "(all)", count=len(rows),
                       rows=[{k: str(v) for k, v in r.items()} for r in rows])
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


def register(app):
    app.register_blueprint(onboarding_recover_bp)
    return True
