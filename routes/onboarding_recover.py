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


def delivery_verdict(matchable, events, confirmed, days):
    """(verdict, healthy) from the reconciliation counts. Pure, so the rule
    that zero-events is NOT healthy can be tested without a database.

    ★The whole point is that `0` must never read as "nothing to report". A
    count of zero delivery events against a positive send count is the loudest
    fact on this endpoint, not the quietest.
    """
    if matchable == 0:
        return "NO_SENDS — nothing to reconcile in this window.", True
    if events == 0:
        return (
            "BLIND — %d welcome email(s) were handed to Resend in the last %d "
            "days and NOT ONE delivery event has been received. Every "
            "'we welcomed them' claim in this window means sent, not "
            "delivered. Fix is upstream and owner-side: add "
            "https://dchub.cloud/api/v1/webhooks/resend as an endpoint in the "
            "Resend dashboard, then set RESEND_WEBHOOK_SECRET so events are "
            "stored verified=true." % (matchable, days)), False
    if confirmed < matchable:
        return ("PARTIAL — %d of %d sends confirmed delivered; the rest are "
                "unproven, not known-failed." % (confirmed, matchable)), False
    return "CONFIRMED — every send in this window has a delivery event.", True


@onboarding_recover_bp.route("/api/v1/admin/welcome-log/delivery-truth",
                             methods=["GET"])
def delivery_truth():
    """Reconcile what we SENT against what Resend confirmed was DELIVERED.

    ★★★ Every "we welcomed them" claim in this repo means *sent*, not
    *delivered*. welcome_email_log stamps a resend_message_id on each send
    precisely so /api/v1/webhooks/resend can close that loop — and as of
    2026-08-28 email_events holds ONE row all time, a synthetic
    deploy-verify@example.com from 2026-07-17. Zero real events, ever, for any
    customer.

    The failure mode is SILENCE: nothing was broken loudly, so nobody looked.
    This endpoint exists to make the absence say something. It reports a
    verdict rather than a row count, because "0" reads like "nothing to
    report" and what it actually means is "we cannot prove a single customer
    email has ever arrived".

    Note what this can and cannot fix. The route is live and permissive (it
    stores unsigned events with verified=false), so the gap is upstream: no
    endpoint is configured in the Resend dashboard, and RESEND_WEBHOOK_SECRET
    is unset. Both are owner actions; code cannot mint the data.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 500
    try:
        days = max(1, min(365, int(request.args.get("days") or 30)))
    except Exception:
        days = 30
    out = {"ok": True, "window_days": days}
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                # Sends we could match: a sent-prefix status AND a message id.
                # A row without an id is unmatchable by construction, so count
                # it separately instead of quietly holding it against delivery.
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE COALESCE(status,'') LIKE 'sent%%'"
                    "                        AND COALESCE(resend_message_id,'') <> ''"
                    "                        AND resend_message_id <> 'None'),"
                    "       COUNT(*) FILTER (WHERE COALESCE(status,'') LIKE 'sent%%'"
                    "                        AND (COALESCE(resend_message_id,'') = ''"
                    "                             OR resend_message_id = 'None'))"
                    "  FROM welcome_email_log"
                    " WHERE attempted_at > NOW() - (%s || ' days')::interval",
                    (days,))
                matchable, unmatchable = cur.fetchone()
                cur.execute(
                    "SELECT COUNT(*) FROM email_events"
                    " WHERE received_at > NOW() - (%s || ' days')::interval",
                    (days,))
                events = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(DISTINCT w.resend_message_id)"
                    "  FROM welcome_email_log w"
                    "  JOIN email_events e"
                    "    ON e.resend_message_id = w.resend_message_id"
                    " WHERE w.attempted_at > NOW() - (%s || ' days')::interval"
                    "   AND COALESCE(e.event_type,'') = 'email.delivered'",
                    (days,))
                confirmed = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*), MAX(received_at) FROM email_events")
                total_events, last_event = cur.fetchone()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    out.update({
        "sends_with_a_message_id": matchable,
        "sends_without_a_message_id": unmatchable,
        "delivery_events_in_window": events,
        "sends_confirmed_delivered": confirmed,
        "email_events_rows_all_time": total_events,
        "last_event_received_at": (last_event.isoformat()
                                   if last_event is not None else None),
    })
    verdict, healthy = delivery_verdict(matchable, events, confirmed, days)
    out["verdict"] = verdict
    out["healthy"] = healthy
    return jsonify(out)


def register(app):
    app.register_blueprint(onboarding_recover_bp)
    return True
