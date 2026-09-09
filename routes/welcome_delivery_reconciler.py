"""welcome_delivery_reconciler.py — did every payer actually GET their welcome?
(r-welcome-429, 2026-09-09)

The incident this exists for: lbthrall@gmail.com paid $99 at 23:52:08 UTC on
2026-09-08. All three onboarding emails (api key, welcome, receipt) died on
Resend "HTTP 429 Too Many Requests". `welcome_email_log` recorded
`resend_failed` three times and NOTHING alerted, because the 🚨 in
send_welcome_email_sendgrid lived only in an `except` branch that a swallowed
429 can never reach. The customer sat locked out of a paid account, created a
second free account trying to get in, and made zero API calls.

The retry + outcome-alert in main.py fix THAT bug. This fixes the CLASS: it does
not care why a welcome failed, only that a paid conversion exists with no
successful welcome beside it. Any future transport, template, webhook-race or
provider fault lands in the same net.

Contract:
  POST /api/jobs/welcome-delivery-reconcile   run it (jobs_routes auto-stamps
                                              cron_last_run, so the platform's
                                              external dead-man watches it)
  GET  /api/jobs/welcome-delivery-reconcile   dry read, no alert

★ ZERO IS NOT HEALTH. A scan that can only ever return "nothing found" reads
green when its own query is broken (table renamed, column dropped, timezone
skew). `reconcile_verdict` therefore treats "considered 0 conversions over the
whole lookback" as NO_DATA — a thing to investigate — not as OK. That rule is a
pure function so it can be tested without a database.
"""
from __future__ import annotations

import datetime
import logging
import os

import psycopg2
from flask import Blueprint, jsonify, request

logger = logging.getLogger("welcome_reconciler")
welcome_reconciler_bp = Blueprint("welcome_reconciler", __name__)

# A welcome that has not landed within this long after payment is late enough
# to be a problem, not a race with the webhook.
GRACE_MINUTES = 10
LOOKBACK_HOURS = 168          # 7 days — long enough that a quiet week is real
SENT_STATUSES = ("sent", "sent_via_resend", "sent_manual", "skipped_duplicate")


def reconcile_verdict(considered: int, stranded: int, lookback_hours: int):
    """(verdict, healthy) from the counts. Pure — no DB, no clock.

    considered = paid conversions old enough to have been welcomed.
    stranded   = those with no successful welcome_email_log row.
    """
    if considered == 0:
        return (f"NO_DATA — zero paid conversions in {lookback_hours}h. Either "
                f"genuinely quiet or the reconciler query is broken; a scan that "
                f"cannot find anything must not report health.", False)
    if stranded == 0:
        return (f"OK — {considered} paid conversion(s), all welcomed.", True)
    return (f"STRANDED — {stranded} of {considered} paid conversion(s) have no "
            f"successful welcome email.", False)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")


_STRANDED_SQL = f"""
    SELECT c.user_email, c.plan_to, c.mrr_cents, c.created_at
      FROM mcp_conversions c
     WHERE c.is_test IS NOT TRUE
       AND COALESCE(c.mrr_cents, 0) > 0
       AND c.created_at >= NOW() - INTERVAL '%s hours'
       AND c.created_at <= NOW() - INTERVAL '%s minutes'
       AND NOT EXISTS (
             SELECT 1 FROM welcome_email_log w
              WHERE LOWER(w.email) = LOWER(c.user_email)
                AND w.status = ANY(%s)
                AND w.attempted_at >= c.created_at - INTERVAL '5 minutes'
           )
     ORDER BY c.created_at DESC
     LIMIT 200
"""

_CONSIDERED_SQL = """
    SELECT COUNT(*) FROM mcp_conversions c
     WHERE c.is_test IS NOT TRUE
       AND COALESCE(c.mrr_cents, 0) > 0
       AND c.created_at >= NOW() - INTERVAL '%s hours'
       AND c.created_at <= NOW() - INTERVAL '%s minutes'
"""


def _scan(lookback_hours=LOOKBACK_HOURS, grace_minutes=GRACE_MINUTES):
    dsn = _dsn()
    if not dsn:
        return None, None, "no DATABASE_URL"
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(_CONSIDERED_SQL % (lookback_hours, grace_minutes))
                considered = int(cur.fetchone()[0])
                cur.execute(_STRANDED_SQL % (lookback_hours, grace_minutes, "%s"),
                            (list(SENT_STATUSES),))
                rows = [
                    {"email": r[0], "plan": r[1], "mrr_cents": r[2],
                     "paid_at": r[3].isoformat() if hasattr(r[3], "isoformat") else str(r[3])}
                    for r in cur.fetchall()
                ]
        return considered, rows, None
    except Exception as e:
        return None, None, str(e)[:200]


def _alert(rows, verdict):
    """Tell the operator. Uses the resilient sender, never the no-op stub."""
    admin_to = (os.environ.get("DCHUB_ADMIN_EMAIL") or "jonathan@dchub.cloud").strip()
    items = "".join(
        f"<li><b>{r['email']}</b> — {r['plan']} — ${(r['mrr_cents'] or 0)/100:.2f} "
        f"— paid {r['paid_at']}</li>" for r in rows)
    html = (f"<p>{verdict}</p><ul>{items}</ul>"
            f"<p>Recover each: <code>POST /api/v1/admin/resend-welcome</code> "
            f"(email, name, plan) <b>and</b> <code>POST /api/auth/forgot-password</code> "
            f"— a Stripe-created account has no password, so the welcome email's "
            f"&ldquo;sign in to get your key&rdquo; is a dead end on its own.</p>")
    try:
        from email_fallback import send_email_resilient
        return bool(send_email_resilient(
            admin_to, f"🚨 {len(rows)} paying customer(s) never got a welcome email",
            html_content=html))
    except Exception as e:
        logger.warning("[welcome-reconcile] alert transport failed: %s", str(e)[:120])
        return False


@welcome_reconciler_bp.route("/api/jobs/welcome-delivery-reconcile",
                             methods=["GET", "POST"])
def welcome_delivery_reconcile():
    lookback = request.args.get("hours", type=int) or LOOKBACK_HOURS
    grace = request.args.get("grace_minutes", type=int) or GRACE_MINUTES
    considered, rows, err = _scan(lookback, grace)
    if err:
        # A reconciler that cannot run is NOT a reconciler that found nothing.
        logger.error("[welcome-reconcile] scan failed: %s", err)
        return jsonify(ok=False, error=err, healthy=False,
                       verdict="SCAN_FAILED — could not run, so nothing is proven"), 500

    verdict, healthy = reconcile_verdict(considered, len(rows), lookback)
    alerted = False
    if rows and request.method == "POST":
        alerted = _alert(rows, verdict)

    logger.info("[welcome-reconcile] considered=%s stranded=%s healthy=%s",
                considered, len(rows), healthy)
    return jsonify(ok=True, healthy=healthy, verdict=verdict,
                   considered=considered, stranded=len(rows),
                   lookback_hours=lookback, grace_minutes=grace,
                   alerted=alerted, rows=rows,
                   checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat())


def register_welcome_reconciler(app):
    app.register_blueprint(welcome_reconciler_bp)
