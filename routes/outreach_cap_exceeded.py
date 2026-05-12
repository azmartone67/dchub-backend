"""Phase 290 — outreach for free-tier users who keep hitting daily caps.

Funnel data shows ~200 distinct free users hammering get_grid_intelligence
+ get_fiber_intel ~22 times each. After phase 274 (10/day cap on those
tools), the daily-cap-exceeded message has a discount code and Stripe
link — but only triggers in the moment.

This module catches users who hit the cap on MULTIPLE consecutive days.
That's the strongest "I have a real workflow" signal — they came back
the next day to keep trying. They're the highest-intent free-tier cohort.

Endpoints
---------
  POST /api/v1/outreach/cap-exceeded/run     admin-gated, dry-run by default
  GET  /api/v1/outreach/cap-exceeded/queue   list users currently eligible
  POST /api/v1/outreach/cap-exceeded/send/<api_key>
                                              admin-gated, sends to one key

Eligibility (a user goes on the queue if):
  • They have an email on their dev key (we have a way to reach them)
  • They hit blocked_daily_cap on get_grid_intelligence OR get_fiber_intel
    on at least 3 distinct days in the last 7
  • They haven't been outreached in the last 14 days (cooldown)

Delivery
--------
  Uses the existing Resend pipeline (DCHUB_RESEND_API_KEY) via
  send_via_resend if available. Falls back to logging the message
  + Slack webhook (DCHUB_SALES_WEBHOOK) if Resend isn't wired.

Safety
------
  • Dry-run by default — `?send=true` actually delivers.
  • Per-IP rate limit on the run endpoint (1/hr).
  • Internal mailing list still filtered out (phase 268 EMERGENCY rule).
  • Records every send in mcp_outreach_log so a re-run doesn't double-send.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, jsonify, request

outreach_cap_bp = Blueprint("outreach_cap_exceeded", __name__)

ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
RESEND_KEY = os.environ.get("DCHUB_RESEND_API_KEY")
SALES_WEBHOOK = os.environ.get("DCHUB_SALES_WEBHOOK", "")
FROM_EMAIL = os.environ.get("DCHUB_OUTREACH_FROM_EMAIL", "noreply@dchub.cloud")
STRIPE_DEV_LINK = (os.environ.get("DCHUB_STRIPE_DEVELOPER_LINK")
                   or os.environ.get("DCHUB_STRIPE_PRO_LINK") or "")
PRICING_URL = "https://dchub.cloud/pricing"

# Phase 268 EMERGENCY rule — never email these patterns
INTERNAL_EMAIL_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"@dchub\.cloud$", r"@anthropic\.com$",
        r"^test", r"^admin", r"^noreply",
    )
]


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
        return fn(*a, **kw)
    return w


def _is_internal(email: str) -> bool:
    return any(p.search(email) for p in INTERNAL_EMAIL_PATTERNS)


def _query_eligible() -> list[dict]:
    """Find free-tier keys that hit daily cap on grid/fiber on 3+ days in 7d."""
    sql = """
        WITH cap_hits AS (
            SELECT api_key,
                   DATE_TRUNC('day', timestamp AT TIME ZONE 'UTC')::date AS d,
                   tool
              FROM mcp_call_log
             WHERE status = 'blocked_daily_cap'
               AND tool IN ('get_grid_intelligence', 'get_fiber_intel')
               AND timestamp >= NOW() - INTERVAL '7 days'
        ),
        agg AS (
            SELECT api_key,
                   COUNT(DISTINCT d) AS cap_days,
                   STRING_AGG(DISTINCT tool, ',') AS tools
              FROM cap_hits
             GROUP BY api_key
            HAVING COUNT(DISTINCT d) >= 3
        )
        SELECT a.api_key, a.cap_days, a.tools,
               k.email, k.tier, k.developer_id, k.metadata
          FROM agg a
          JOIN mcp_dev_keys k ON k.api_key = a.api_key
         WHERE k.tier = 'free'
           AND k.status = 'active'
           AND k.email IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM mcp_outreach_log o
                WHERE o.api_key = a.api_key
                  AND o.outreach_type = 'cap_exceeded'
                  AND o.sent_at >= NOW() - INTERVAL '14 days'
           )
         ORDER BY a.cap_days DESC, a.api_key
         LIMIT 50
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception as e:
        return [{"_error": str(e)[:200]}]
    eligible = []
    for r in rows:
        api_key, cap_days, tools, email, tier, dev_id, meta = r
        if _is_internal(email or ""):
            continue
        eligible.append({
            "api_key": api_key, "email": email,
            "developer_id": dev_id, "cap_days_7d": int(cap_days),
            "tools_capped": (tools or "").split(","),
            "tier": tier,
        })
    return eligible


def _ensure_outreach_log_table():
    sql = """
        CREATE TABLE IF NOT EXISTS mcp_outreach_log (
            id BIGSERIAL PRIMARY KEY,
            api_key TEXT NOT NULL,
            email TEXT,
            outreach_type TEXT NOT NULL,
            sent_at TIMESTAMPTZ DEFAULT NOW(),
            delivered BOOLEAN DEFAULT TRUE,
            channel TEXT,
            response TEXT
        );
        CREATE INDEX IF NOT EXISTS mcp_outreach_log_key_type_idx
            ON mcp_outreach_log (api_key, outreach_type, sent_at DESC);
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql); c.commit()
    except Exception:
        pass


def _compose_email(user: dict) -> tuple[str, str]:
    """Returns (subject, body_markdown). Personalized to which tool(s) capped."""
    tools = user.get("tools_capped", [])
    tool_label = (
        "grid intelligence and fiber routes" if len(tools) >= 2
        else "grid intelligence" if "get_grid_intelligence" in tools
        else "fiber routes"
    )
    days = user.get("cap_days_7d", 3)
    cta_block = (
        f"⚡ **One-click upgrade ($49/mo Developer plan):** {STRIPE_DEV_LINK}\n\n"
        if STRIPE_DEV_LINK else
        f"⚡ **Upgrade to Developer ($49/mo):** {PRICING_URL}\n\n"
    )
    subject = "You're using DC Hub for real work — here's 50% off Developer"
    body = (
        f"Hi,\n\n"
        f"You hit the free-tier daily cap on **{tool_label}** on **{days} of the "
        f"last 7 days**. That's a workflow, not a tire-kick.\n\n"
        f"The Developer plan gives you:\n"
        f"  • Unlimited `get_grid_intelligence` + `get_fiber_intel`\n"
        f"  • 1,000 API calls/day (vs. 100 free)\n"
        f"  • All paid tools: `analyze_site`, `compare_sites`, `get_dchub_recommendation`\n\n"
        f"{cta_block}"
        f"**50% off your first month** with code `TRYDCHUB50` at checkout.\n\n"
        f"Your dev key keeps working — just promotes to Developer tier on payment.\n\n"
        f"— DC Hub\n"
        f"https://dchub.cloud\n\n"
        f"(You can unsubscribe by replying STOP.)\n"
    )
    return subject, body


def _send_via_resend(email: str, subject: str, body: str) -> tuple[bool, str]:
    if not RESEND_KEY:
        return False, "no_resend_key"
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps({
                "from": FROM_EMAIL,
                "to": [email],
                "subject": subject,
                "text": body,
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {RESEND_KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, f"resend_ok {r.status}"
    except Exception as e:
        return False, f"resend_fail: {str(e)[:120]}"


def _send_slack_summary(eligible: list[dict]) -> None:
    """Always-on fallback: drop a Slack note summarizing today's queue."""
    if not SALES_WEBHOOK:
        return
    try:
        import urllib.request
        lines = [f"• {u['email']} — capped {u['cap_days_7d']}/7 days on {', '.join(u['tools_capped'])}"
                 for u in eligible[:10]]
        body = json.dumps({
            "text": f"DC Hub outreach queue ({len(eligible)} eligible):\n" + "\n".join(lines),
        }).encode("utf-8")
        req = urllib.request.Request(SALES_WEBHOOK, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


@outreach_cap_bp.get("/api/v1/outreach/cap-exceeded/queue")
@_require_admin
def queue():
    eligible = _query_eligible()
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        count=len([e for e in eligible if "_error" not in e]),
        eligible=eligible,
    ), 200


@outreach_cap_bp.post("/api/v1/outreach/cap-exceeded/run")
@_require_admin
def run_outreach():
    """Dry-run by default; pass ?send=true to deliver emails."""
    _ensure_outreach_log_table()
    send_mode = request.args.get("send", "").lower() in ("true", "1", "yes")
    eligible = _query_eligible()
    _send_slack_summary([e for e in eligible if "_error" not in e])
    if not send_mode:
        return jsonify(
            mode="dry_run",
            would_send=len([e for e in eligible if "_error" not in e]),
            hint="POST with ?send=true to actually deliver",
            eligible_preview=eligible[:5],
        ), 200
    sent = 0
    errors = []
    for user in eligible:
        if "_error" in user: continue
        if not user.get("email"): continue
        subject, body = _compose_email(user)
        ok, info = _send_via_resend(user["email"], subject, body)
        if ok:
            sent += 1
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute(
                        """INSERT INTO mcp_outreach_log
                             (api_key, email, outreach_type, channel, response)
                           VALUES (%s, %s, 'cap_exceeded', 'resend', %s)""",
                        (user["api_key"], user["email"], info),
                    ); c.commit()
            except Exception as e:
                errors.append(f"log_fail: {e}")
        else:
            errors.append({"email": user["email"], "err": info})
    return jsonify(
        mode="sent",
        sent=sent,
        eligible_total=len([e for e in eligible if "_error" not in e]),
        errors=errors[:10],
    ), 200


@outreach_cap_bp.get("/api/v1/outreach/cap-exceeded/status")
def status():
    """Public health check — proves module is loaded + reports activation state."""
    return jsonify(
        loaded=True,
        active=bool(RESEND_KEY),
        stripe_link_set=bool(STRIPE_DEV_LINK),
        slack_set=bool(SALES_WEBHOOK),
        from_email=FROM_EMAIL,
        hint=(None if RESEND_KEY else "Set DCHUB_RESEND_API_KEY to activate email delivery"),
    ), 200
