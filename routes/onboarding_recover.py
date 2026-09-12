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
    <li><b>REST API:</b> <code>curl -H "X-API-Key: &lt;your-key&gt;" "https://dchub.cloud/api/v1/facilities?limit=5"</code></li>
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
    mid = None
    err = None
    try:
        from main import _resend_email
        # r-delivery-truth-join (2026-09-12): keep the message id. This lane
        # writes welcome_email_log, so a send logged without one lands in this
        # same endpoint's sends_without_a_message_id forever — the operator
        # resending a welcome by hand could never prove it arrived.
        mid = _resend_email(email, subject, html,
                            from_email="hello@dchub.cloud", from_name="Jonathan at DC Hub")
        sent = bool(mid)
    except Exception as e:
        err = str(e)[:200]
    try:
        from main import _log_welcome_email
        _log_welcome_email(email, f"{plan}:resend", "sent" if sent else "failed",
                           resend_message_id=(mid if sent and mid != 'sent-no-id'
                                              else None))
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


# ★ What this endpoint reconciles, and what it CANNOT — published in the
# response rather than left for the reader to assume. The join is
# welcome_email_log.resend_message_id -> email_events.resend_message_id, so a
# send that writes no log row can never be confirmed here however well it was
# delivered. Measured 2026-09-11: of 23 welcome-subject delivery events in 30
# days, 22 were free-tier welcomes — mail this surface is structurally blind to.
#
# These lanes are OUT OF SCOPE BY CONSTRUCTION, and deliberately so: they must
# not simply start writing welcome_email_log. _claim_welcome_send refuses to
# claim when the address already holds a non-receipt 'sent'/'sent_via_resend'
# row inside 24h, so logging a free signup there would SUPPRESS that customer's
# paid welcome if they upgraded the same day. Widening the surface means giving
# these senders their own delivery-truth table, not borrowing this one.
UNRECONCILED_SENDERS = [
    "free-tier welcome (main.send_free_welcome_email_sendgrid) — no log row",
    "Pro-upgrade welcome (main.send_pro_welcome_email_sendgrid) — no log row",
    "key recovery (routes/keys_recover.py) — sends via main._resend_email, no log row",
    "digests, nudges, campaigns and operator alerts — their own tables or none",
]


def delivery_verdict(matchable, events, confirmed, days, events_all=None):
    """(verdict, healthy) from the reconciliation counts. Pure, so the rule
    that zero-events is NOT healthy can be tested without a database.

    ★The whole point is that `0` must never read as "nothing to report". A
    count of zero delivery events against a positive send count is the loudest
    fact on this endpoint, not the quietest.

    `events` is WELCOME delivery events; `events_all` is every delivery event
    in the window, whatever sent it. r-delivery-truth-join (2026-09-11): this
    function used to be handed the all-senders count, and that made BLIND
    almost impossible to reach — the weekly digest alone posts ~175 events a
    window, so welcome delivery could stop dead and the number beside it would
    still read healthy. Narrowing the input only ever makes this LOUDER.
    `events_all` defaults to `events`, so a four-argument caller is unchanged.
    """
    if events_all is None:
        events_all = events
    if matchable == 0:
        return "NO_SENDS — nothing to reconcile in this window.", True
    if events == 0 and not events_all:
        return (
            "BLIND — %d welcome email(s) were handed to Resend in the last %d "
            "days and NOT ONE delivery event has been received. Every "
            "'we welcomed them' claim in this window means sent, not "
            "delivered. Fix is upstream and owner-side: add "
            "https://dchub.cloud/api/v1/webhooks/resend as an endpoint in the "
            "Resend dashboard, then set RESEND_WEBHOOK_SECRET so events are "
            "stored verified=true." % (matchable, days)), False
    if events == 0:
        return (
            "BLIND — %d welcome email(s) were handed to Resend in the last %d "
            "days and NOT ONE welcome delivery event has been received, while "
            "%d delivery event(s) for other DC Hub mail arrived in the same "
            "window. The webhook is alive, so this is send-side, not "
            "owner-side: the welcome lane is not reaching Resend at all. Do "
            "not read the healthy-looking all-senders count as delivery of "
            "these sends." % (matchable, days, events_all)), False
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
    precisely so /api/v1/webhooks/resend can close that loop.

    ★ 2026-09-11: the upstream gap this docstring used to describe is CLOSED.
    The endpoint IS configured in the Resend dashboard, RESEND_WEBHOOK_SECRET
    IS set, and signature-verified events have arrived continuously since
    2026-08-29 03:10Z. Until #4425 this paragraph still described the state
    before that — a lone synthetic row from a deploy check, no customer events,
    blamed on two owner actions that were in fact done about fourteen hours
    after it was written. It was accurate for those fourteen hours and wrong
    for the fortnight that followed. #4431 then stopped it REPRINTING the
    superseded claim verbatim; this edit drops the live row counts it still
    carried, for that same reason — a count in a docstring is the next stale
    sentence. Read this endpoint's own output, never this paragraph.

    ★ The open question this docstring recorded — why no
    welcome_email_log.resend_message_id had EVER matched an email_events row —
    is ANSWERED and FIXED (r-delivery-truth-join, 2026-09-11). It was never a
    provenance mismatch: Resend's POST /emails `id` and its webhook's
    data.email_id are the same value, and both columns held 36-char UUIDs. The
    two populations simply never overlapped IN TIME. #4198 moved the paid
    welcome off the dead SendGrid import onto main._resend_email, which
    returned a bare bool and discarded the response body — so the send stopped
    recording an id at the moment it started succeeding. Measured before the
    fix: the newest id-carrying row in ANY table in this database was
    2026-08-28, the verified stream began 2026-08-29 03:10Z, and the join had
    therefore never had a row to match on either side. _resend_email now
    returns the message id and the welcome lane stamps it.

    ★ SCOPE, because a surface that quietly omits things is its own failure
    mode: this reconciles welcome_email_log sends only. See
    UNRECONCILED_SENDERS above for the lanes that write no log row and so can
    never reach CONFIRMED here — most delivered welcome-subject mail is the
    free-tier welcome, which is one of them. The response repeats that list.

    The failure mode this exists for is SILENCE: nothing breaks loudly, so
    nobody looks. It reports a verdict rather than a row count, because "0"
    reads like "nothing to report" and what it actually means is "we cannot
    prove a single customer email has ever arrived". The route stays
    permissive (it stores unsigned events with verified=false) so a
    misconfigured secret degrades to unverified rows rather than to no rows.
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
                # Every delivery event in the window, whatever sent it —
                # context, not evidence about these sends. The welcome-scoped
                # count beside it is what the verdict is allowed to read: the
                # weekly digest alone posts ~175 events a window, and feeding
                # THAT to delivery_verdict made BLIND unreachable even if
                # welcome delivery stopped dead. Percent DOUBLED — psycopg2
                # scans the whole query for format specs.
                cur.execute(
                    "SELECT COUNT(*),"
                    "       COUNT(*) FILTER (WHERE subject ILIKE"
                    "                        'Welcome to DC Hub%%')"
                    "  FROM email_events"
                    " WHERE received_at > NOW() - (%s || ' days')::interval",
                    (days,))
                events_all, events = cur.fetchone()
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
        "delivery_events_in_window": events_all,
        "welcome_delivery_events_in_window": events,
        "sends_confirmed_delivered": confirmed,
        "email_events_rows_all_time": total_events,
        "last_event_received_at": (last_event.isoformat()
                                   if last_event is not None else None),
    })
    verdict, healthy = delivery_verdict(matchable, events, confirmed, days,
                                        events_all=events_all)
    out["verdict"] = verdict
    out["healthy"] = healthy
    out["reconciles"] = ("welcome_email_log sends carrying a resend_message_id, "
                         "joined to email_events on that id")
    out["cannot_be_confirmed_here"] = list(UNRECONCILED_SENDERS)
    return jsonify(out)


def register(app):
    app.register_blueprint(onboarding_recover_bp)
    return True
