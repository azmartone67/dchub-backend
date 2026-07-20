"""customer_white_glove.py — customer-lifecycle white-glove master shell (2026-07-20).

The human-customer analog of the agent-side white glove (agent_onboarding_master_
shell / model_relations): ONE conductor that MEASURES every paying customer's
lifecycle, CLASSIFIES it into a stage, computes the next-best-action, writes the
stage back to users.lifecycle_stage, and surfaces a daily operator health digest
plus one-click actions — instead of the scattered, mostly-unscheduled pieces
(activation_nudge, winback_outreach, usage_limit_emails, paid_account_health).

Motivated by marvinvitcu@gmail.com (2026-07-20): a $9 Starter customer who paid
for a month+ with ZERO API calls, invisible to every existing nudge (Starter
wasn't a "paid" plan; the nudge wasn't scheduled). The lesson: no single surface
told anyone he was stranded. This is that surface.

LIFECYCLE STAGES (written to users.lifecycle_stage):
  new         paid, < GRACE_HOURS old — welcome grace, no action yet
  stranded    paid, active sub, 0 calls ever, past grace — ACTIVATION motion
  activating  made first calls, active within ACTIVE_DAYS — healthy, watch
  healthy     regular recent usage — no action
  cooling     was active, no calls in COOLING_DAYS..AT_RISK_DAYS — RE-ENGAGE
  at_risk     no calls AT_RISK_DAYS+, or a failed payment, still paying — SAVE
  power       high volume / near daily cap — EXPANSION / upgrade
  churned     subscription canceled/inactive or demoted — WINBACK

SAFETY (touches real paying customers):
  * MEASURE + CLASSIFY + PERSIST the stage always run (read + a users UPDATE of
    lifecycle_stage/last_touched_at only — no customer contact).
  * Customer EMAILS are never sent from here. Stage motions are ROUTED to the
    existing gated senders (activation_nudge, winback) and each of THOSE stays
    dry-run until its own arm flag is set. The primary output is the OPERATOR
    digest — a human decides.
  * CUSTOMER_WHITE_GLOVE_ACT=1 lets the tick fan stranded customers into the
    activation nudge automatically (which itself still needs ACTIVATION_NUDGE_ARM).

Endpoints (admin-keyed):
  GET  /api/v1/admin/customer-white-glove/state    full roster + stage + action
  POST /api/v1/admin/customer-white-glove/tick     measure→classify→persist
  POST /api/v1/admin/customer-white-glove/digest   ?send=true emails the operator
"""
from __future__ import annotations

import datetime
import html as _html
import os
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger("customer_white_glove")
customer_white_glove_bp = Blueprint("customer_white_glove", __name__)

PAID_PLANS = ("starter", "developer", "pro", "paid", "enterprise", "founding")
GRACE_HOURS = 48
ACTIVE_DAYS = 14
COOLING_DAYS = 14
AT_RISK_DAYS = 30
POWER_CALLS_30D = 2000     # heuristic "power user" floor over 30d


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


# Owner / staff accounts that are real Stripe payers only because the founder
# tests the checkout flow — exclude so the customer board isn't self-noise.
_OWNER_MARKERS = ("azmartone", "nicomartone", "jonathan.martone", "martone")


def _is_internal(email: str) -> bool:
    e = (email or "").lower()
    if (not e) or "@dchub.cloud" in e or e.startswith(("qa-", "test", "smoke")):
        return True
    return any(m in e for m in _OWNER_MARKERS)


def _measure():
    """Real PAYING customers only (Stripe customer + a paid invoice — excludes
    the BD-outreach seeds and comped rows that share the 'developer'/'founding'
    plan labels), with TRUE cross-surface usage: web (users.api_calls_total) +
    MCP (mcp_call_log via the customer's mcp_dev_keys key). The MCP count is the
    signal that matters — the product IS the MCP server — and reading only the
    web api_keys store was mis-flagging stranded customers as healthy. Fail-soft."""
    c = _conn()
    if c is None:
        return []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT u.email, u.name, u.plan, u.created_at, u.last_login,
                       u.subscription_status, u.payment_failed_count, u.demoted_at,
                       COALESCE(u.api_calls_total, 0) AS web_calls,
                       COALESCE((SELECT COUNT(*) FROM mcp_call_log ml
                                 JOIN mcp_dev_keys dk ON dk.api_key = ml.api_key
                                 WHERE lower(dk.email) = lower(u.email)), 0) AS mcp_calls,
                       (SELECT MAX(ml.timestamp) FROM mcp_call_log ml
                        JOIN mcp_dev_keys dk ON dk.api_key = ml.api_key
                        WHERE lower(dk.email) = lower(u.email)) AS last_mcp,
                       EXISTS(SELECT 1 FROM email_drip_log d
                              WHERE lower(d.user_email)=lower(u.email)
                                AND d.email_key='activation_nudge') AS nudged,
                       EXISTS(SELECT 1 FROM welcome_email_log w
                              WHERE lower(w.email)=lower(u.email)) AS welcomed
                FROM users u
                WHERE u.plan IN %s AND u.email IS NOT NULL AND u.email <> ''
                  AND u.stripe_customer_id IS NOT NULL AND u.stripe_customer_id <> ''
                  AND COALESCE(u.invoices_paid_count, 0) > 0
            """, (PAID_PLANS,))
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["total_calls"] = int(d.get("web_calls") or 0) + int(d.get("mcp_calls") or 0)
                # recency = most recent of MCP activity or web login
                d["last_used_at"] = d.get("last_mcp") or d.get("last_login")
                rows.append(d)
    except Exception as e:
        logger.warning("[cwg] measure failed: %s", str(e)[:160])
        rows = []
    finally:
        try: c.close()
        except Exception: pass
    return [r for r in rows if not _is_internal(r.get("email"))]


def _age_days(ts, now):
    # users.created_at is TEXT (ISO strings, inconsistent) while api_keys.
    # last_used_at is a real timestamptz — handle both, never raise.
    if ts is None:
        return None
    try:
        if isinstance(ts, str):
            ts = datetime.datetime.fromisoformat(ts.replace("Z", "").strip())
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return (now - ts).total_seconds() / 86400.0
    except Exception:
        return None


def _classify(r, now):
    """→ (stage, action, priority 0-3). Deterministic."""
    sub = (r.get("subscription_status") or "").lower()
    demoted = r.get("demoted_at") is not None
    if demoted or sub in ("canceled", "cancelled", "unpaid", "incomplete_expired"):
        return "churned", "Winback: they canceled/lapsed — send the come-back offer.", 2
    joined_age = _age_days(r.get("created_at"), now)
    calls = int(r.get("total_calls") or 0)
    last_used_age = _age_days(r.get("last_used_at"), now)
    failed = int(r.get("payment_failed_count") or 0)

    if failed > 0:
        return "at_risk", f"Save: {failed} failed payment(s) — dunning / card-update outreach.", 3
    if joined_age is not None and joined_age < (GRACE_HOURS / 24.0):
        return "new", "Grace period — welcome sent; watch for first call.", 0
    if calls == 0:
        act = ("Activation nudge — paid, zero calls past grace."
               + (" ALREADY nudged; needs a human touch." if r.get("nudged") else ""))
        return "stranded", act, 3
    if last_used_age is None:
        return "activating", "Made calls but recency unknown — watch.", 1
    if last_used_age <= ACTIVE_DAYS:
        # power user?
        if calls >= POWER_CALLS_30D:
            return "power", "Expansion: high volume — surface the next tier / bulk export.", 1
        if joined_age is not None and joined_age <= ACTIVE_DAYS:
            return "activating", "Activated recently — nurture toward habit.", 1
        return "healthy", "Healthy recent usage — no action.", 0
    if last_used_age <= AT_RISK_DAYS:
        return "cooling", f"Re-engage: no calls in {int(last_used_age)}d — 'what changed?' check-in.", 2
    return "at_risk", f"Save: idle {int(last_used_age)}d but still paying — churn risk.", 3


def _roster(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = []
    for r in _measure():
        stage, action, prio = _classify(r, now)
        out.append({
            "email": r["email"], "name": r.get("name") or "", "plan": r.get("plan"),
            "stage": stage, "action": action, "priority": prio,
            "total_calls": int(r.get("total_calls") or 0),
            "mcp_calls": int(r.get("mcp_calls") or 0),
            "web_calls": int(r.get("web_calls") or 0),
            "joined_days": round(_age_days(r.get("created_at"), now) or 0, 1),
            "idle_days": (round(_age_days(r.get("last_used_at"), now), 1)
                          if r.get("last_used_at") else None),
            "welcomed": bool(r.get("welcomed")), "nudged": bool(r.get("nudged")),
            "subscription_status": r.get("subscription_status"),
        })
    order = {"stranded": 0, "at_risk": 1, "cooling": 2, "churned": 3,
             "power": 4, "activating": 5, "new": 6, "healthy": 7}
    out.sort(key=lambda x: (order.get(x["stage"], 9), -x["priority"], -x["total_calls"]))
    return out


def _persist_stages(roster):
    """Write the computed stage back to users.lifecycle_stage (+ last_touched_at).
    Read+CLASSIFY drives the column that was previously unpopulated. Fail-soft."""
    c = _conn()
    if c is None:
        return 0
    n = 0
    try:
        with c.cursor() as cur:
            for r in roster:
                try:
                    cur.execute("""
                        UPDATE users SET lifecycle_stage=%s, last_touched_at=NOW(),
                               last_touch_by='customer_white_glove'
                        WHERE lower(email)=lower(%s)
                          AND (lifecycle_stage IS DISTINCT FROM %s OR last_touched_at IS NULL)
                    """, (r["stage"], r["email"], r["stage"]))
                    n += cur.rowcount
                except Exception:
                    pass
    finally:
        try: c.close()
        except Exception: pass
    return n


def _counts(roster):
    out = {}
    for r in roster:
        out[r["stage"]] = out.get(r["stage"], 0) + 1
    return out


@customer_white_glove_bp.route("/api/v1/admin/customer-white-glove/state")
def cwg_state():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    roster = _roster()
    return jsonify(ok=True, total=len(roster), counts=_counts(roster),
                   customers=roster), 200


@customer_white_glove_bp.route("/api/v1/admin/customer-white-glove/tick",
                               methods=["POST"])
def cwg_tick():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    roster = _roster()
    persisted = _persist_stages(roster)
    counts = _counts(roster)
    acted = {"stranded_routed_to_activation": 0}
    # Optional: fan stranded → activation nudge (which itself is arm-gated).
    if os.environ.get("CUSTOMER_WHITE_GLOVE_ACT") == "1":
        try:
            from activation_nudge import run_activation_nudge
            res = run_activation_nudge()   # honors ACTIVATION_NUDGE_ARM internally
            acted["stranded_routed_to_activation"] = res.get("sent", 0)
            acted["activation_armed"] = res.get("armed")
        except Exception as e:
            acted["activation_error"] = str(e)[:120]
    return jsonify(ok=True, total=len(roster), counts=counts,
                   stages_persisted=persisted, acted=acted,
                   note=("Stages written to users.lifecycle_stage. Customer emails "
                         "are NOT sent from here — motions route to the gated "
                         "senders; the operator digest is the human surface.")), 200


def _digest_html(roster):
    counts = _counts(roster)
    def esc(s): return _html.escape(str(s or ""))
    order = ["stranded", "at_risk", "cooling", "churned", "power", "activating"]
    labels = {"stranded": "🔴 Stranded — paid, never activated",
              "at_risk": "🟠 At risk — idle/failed payment, still paying",
              "cooling": "🟡 Cooling — was active, going quiet",
              "churned": "⚫ Churned — canceled/lapsed",
              "power": "🟢 Power users — expansion candidates",
              "activating": "🔵 Activating — nurture toward habit"}
    blocks = []
    for stage in order:
        rows = [r for r in roster if r["stage"] == stage]
        if not rows:
            continue
        trs = "".join(
            f'<tr><td style="padding:5px 10px;border-bottom:1px solid #eee">'
            f'<b>{esc(r["email"])}</b> <span style="color:#888;font-size:12px">'
            f'({esc(r["plan"])} · {r["total_calls"]} calls · joined {r["joined_days"]:.0f}d'
            f'{" · idle "+str(int(r["idle_days"]))+"d" if r.get("idle_days") is not None else ""})</span>'
            f'<br><span style="color:#555;font-size:13px">{esc(r["action"])}</span></td></tr>'
            for r in rows)
        blocks.append(
            f'<h3 style="margin:20px 0 6px;font-size:15px">{labels[stage]} ({len(rows)})</h3>'
            f'<table cellpadding=0 cellspacing=0 width="100%" style="border-collapse:collapse;'
            f'background:#fff;border:1px solid #e2e8f0;border-radius:6px">{trs}</table>')
    healthy = counts.get("healthy", 0) + counts.get("new", 0)
    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f1f5f9;padding:24px">
<div style="max-width:660px;margin:0 auto">
<div style="background:#0f172a;color:#fff;padding:16px 22px;border-radius:8px 8px 0 0">
<strong>DC Hub — customer white-glove health</strong>
<div style="color:#94a3b8;font-size:13px">{len(roster)} paying customers · {healthy} healthy/new ·
{counts.get('stranded',0)} stranded · {counts.get('at_risk',0)} at-risk · {counts.get('cooling',0)} cooling</div>
</div>
<div style="background:#f8fafc;padding:8px 22px 22px;border:1px solid #e2e8f0;border-top:0;border-radius:0 0 8px 8px">
{''.join(blocks) or '<p style="color:#16a34a">All paying customers healthy — nobody needs a touch today.</p>'}
<p style="color:#64748b;font-size:12px;margin-top:18px">Stages written to users.lifecycle_stage. Arm the
activation sends with ACTIVATION_NUDGE_ARM=1; nothing emails a customer from this digest.</p>
</div></div></div>"""


@customer_white_glove_bp.route("/api/v1/admin/customer-white-glove/digest",
                               methods=["POST", "GET"])
def cwg_digest():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    send = request.args.get("send", "").lower() in ("1", "true", "yes")
    roster = _roster()
    _persist_stages(roster)
    recipients = [e.strip() for e in
                  (os.environ.get("DCHUB_BRIEFING_EMAIL")
                   or os.environ.get("BRAIN_DIGEST_EMAIL")
                   or "jonathan@dchub.cloud").split(",") if e.strip()]
    html_body = _digest_html(roster)
    if not send:
        return jsonify(ok=True, dry_run=True, total=len(roster),
                       counts=_counts(roster), recipients=recipients,
                       html_bytes=len(html_body)), 200
    sent = failed = 0
    try:
        import requests as _rq
        subj = (f"DC Hub customer health: {_counts(roster).get('stranded',0)} stranded, "
                f"{_counts(roster).get('at_risk',0)} at-risk")
        for em in recipients:
            try:
                r = _rq.post("https://api.resend.com/emails", timeout=15,
                             json={"from": os.environ.get("DCHUB_FROM_EMAIL",
                                                          "DC Hub <jonathan@dchub.cloud>"),
                                   "to": [em], "subject": subj, "html": html_body},
                             headers={"Authorization":
                                      f"Bearer {os.environ.get('RESEND_API_KEY','').strip()}"})
                sent += 1 if 200 <= r.status_code < 300 else 0
                failed += 0 if 200 <= r.status_code < 300 else 1
            except Exception:
                failed += 1
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500
    return jsonify(ok=True, sent=sent, failed=failed, total=len(roster)), 200
