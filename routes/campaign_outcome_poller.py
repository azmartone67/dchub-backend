"""Campaign outcome poller — daily check on halfprice_annual_2026_06 conversions.

Companion to commit d1ac207d (campaign fire). Without this poller, the
operator has to manually check campaign_log + users.source_plan + Stripe
API to know if the 6 customers we emailed converted. This automates that.

Logic (run daily at 19:00 UTC for 14 days, then auto-stops):
  1. SELECT email, sent_at FROM campaign_log WHERE campaign_name = ...
  2. LEFT JOIN users → detect source_plan='pro_annual_onetime' flip
  3. Query Stripe API for any checkout session with customer_email in
     the recipient list within the last 14 days (catches in-flight buyers
     who clicked but didn't complete yet)
  4. Render summary table → POST to Resend → email Jonathan
  5. Stop condition: all 6 converted OR 14 days elapsed

Endpoints:
  GET  /api/v1/admin/campaign/halfprice-annual/outcomes
       Returns the outcome data as JSON. Defaults to dry-run (no email).
       Use ?send=1 to also email the summary.
  POST /api/v1/admin/campaign/halfprice-annual/outcomes/send
       Forces an immediate summary email regardless of stop condition.

Env vars:
  DCHUB_RESEND_API_KEY              (required for summary email; fail-soft)
  STRIPE_SECRET_KEY                 (required for Stripe session lookup)
  DCHUB_OPERATOR_EMAIL              default 'azmartone@gmail.com'
  DCHUB_FROM_EMAIL                  default 'alerts@dchub.cloud'
  DCHUB_FROM_NAME                   default 'DC Hub'
  CAMPAIGN_OUTCOME_POLLER_DISABLE=1 kill switch

Cron: crawler_scheduler.SCHEDULE "campaign_outcome_poll" 19:00/19:00 UTC.
"""

from __future__ import annotations

import os
import datetime
import logging

from flask import Blueprint, jsonify, request


campaign_outcomes_bp = Blueprint("campaign_outcomes", __name__)
logger = logging.getLogger(__name__)


_CAMPAIGN_NAME      = "halfprice_annual_2026_06"
_CAMPAIGN_FIRE_DATE = datetime.date(2026, 6, 6)  # today's fire
_POLL_WINDOW_DAYS   = 14

_OPERATOR_EMAIL = os.environ.get("DCHUB_OPERATOR_EMAIL", "azmartone@gmail.com")
_FROM_NAME      = os.environ.get("DCHUB_FROM_NAME",  "DC Hub")
_FROM_EMAIL     = os.environ.get("DCHUB_FROM_EMAIL", "alerts@dchub.cloud")

_ADMIN_KEY  = (os.environ.get("DCHUB_ADMIN_KEY")
               or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY")
               or os.environ.get("RESEND_API_KEY") or "").strip()
_STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()


def _admin_gate():
    key = (request.headers.get("X-Admin-Key", "")
           or request.args.get("admin_key", "") or "").strip()
    if not _ADMIN_KEY:
        return None, ("admin_not_configured", 503)
    if key != _ADMIN_KEY:
        return None, ("forbidden", 403)
    return key, None


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("campaign_outcomes: db connect failed: %s", e)
        return None


def _query_campaign_outcomes() -> list[dict]:
    """LEFT JOIN campaign_log → users; returns one row per recipient."""
    c = _conn()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT cl.email,
                       cl.sent_at,
                       cl.resend_message_id,
                       u.plan,
                       u.source_plan,
                       u.tier_expires_at,
                       u.stripe_customer_id
                  FROM campaign_log cl
                  LEFT JOIN users u ON LOWER(u.email) = LOWER(cl.email)
                 WHERE cl.campaign_name = %s
                 ORDER BY cl.sent_at ASC
                """,
                (_CAMPAIGN_NAME,),
            )
            rows = cur.fetchall() or []
        return [
            {
                "email":              r[0],
                "sent_at":            r[1].isoformat() if r[1] else None,
                "resend_message_id":  r[2],
                "plan":               r[3] or "unknown",
                "source_plan":        r[4],
                "tier_expires_at":    r[5].isoformat() if r[5] else None,
                "stripe_customer_id": r[6],
                "status":             "CONVERTED" if r[4] == "pro_annual_onetime" else "pending",
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("campaign_outcomes: query failed: %s", e, exc_info=True)
        return []
    finally:
        try:
            c.close()
        except Exception:
            pass


def _query_stripe_sessions(emails: list[str]) -> dict:
    """Pull last-14d Stripe checkout sessions; map by lowercase email."""
    if not _STRIPE_KEY or not emails:
        return {}
    import requests as _r
    since = int((datetime.datetime.utcnow() - datetime.timedelta(days=_POLL_WINDOW_DAYS)).timestamp())
    out: dict[str, list[dict]] = {}
    try:
        resp = _r.get(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(_STRIPE_KEY, ""),
            params={"limit": 100, "created[gte]": since},
            timeout=10,
        )
        if resp.status_code != 200:
            return {}
        emails_lower = {e.lower() for e in emails}
        for s in (resp.json() or {}).get("data", []):
            email = ((s.get("customer_details") or {}).get("email")
                     or s.get("customer_email") or "").lower()
            if email and email in emails_lower:
                out.setdefault(email, []).append({
                    "id":             s.get("id"),
                    "payment_status": s.get("payment_status"),
                    "amount_total":   (s.get("amount_total") or 0) / 100,
                    "mode":           s.get("mode"),
                    "created":        s.get("created"),
                })
    except Exception as e:
        logger.warning("campaign_outcomes: Stripe lookup failed: %s", e)
    return out


def _build_summary_html(outcomes: list[dict], stripe_sessions: dict) -> str:
    today    = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    converted = [o for o in outcomes if o.get("status") == "CONVERTED"]
    pending   = [o for o in outcomes if o.get("status") != "CONVERTED"]
    days_since = (datetime.date.today() - _CAMPAIGN_FIRE_DATE).days

    lines = [
        "<div style='font-family:-apple-system,Segoe UI,sans-serif;color:#1a1a1a;max-width:680px'>",
        f"<h2 style='color:#0066cc;margin-bottom:4px'>Half-price annual — campaign outcomes</h2>",
        f"<p style='color:#666;margin-top:0'>Day {days_since} of {_POLL_WINDOW_DAYS} since fire · {today}</p>",
        "<div style='display:flex;gap:16px;margin:16px 0'>",
        f"<div style='background:#e7f7e3;padding:12px 20px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Converted</div>"
        f"<div style='font-size:24px;font-weight:700;color:#0a7c2e'>{len(converted)} / {len(outcomes)}</div></div>",
        f"<div style='background:#fff5e1;padding:12px 20px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Pending</div>"
        f"<div style='font-size:24px;font-weight:700;color:#a35a00'>{len(pending)}</div></div>",
        f"<div style='background:#e3f0ff;padding:12px 20px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>MRR if all convert</div>"
        f"<div style='font-size:24px;font-weight:700;color:#0066cc'>${len(outcomes) * 99}/mo equiv</div></div>",
        "</div>",
        "<h3 style='margin-top:24px'>Per-recipient status</h3>",
        "<table style='border-collapse:collapse;width:100%;font-size:13px'>",
        "<tr style='background:#f5f5f5;text-align:left'>"
        "<th style='padding:8px;border:1px solid #ddd'>Email</th>"
        "<th style='padding:8px;border:1px solid #ddd'>Sent</th>"
        "<th style='padding:8px;border:1px solid #ddd'>Status</th>"
        "<th style='padding:8px;border:1px solid #ddd'>Plan / Source</th>"
        "<th style='padding:8px;border:1px solid #ddd'>Stripe activity</th>"
        "</tr>",
    ]
    for o in outcomes:
        email = o.get("email", "?")
        sent  = (o.get("sent_at") or "?")[:16].replace("T", " ")
        status = o.get("status", "pending")
        plan = f"{o.get('plan','?')} / {o.get('source_plan') or '—'}"
        ss = stripe_sessions.get(email.lower(), [])
        if ss:
            paid = [s for s in ss if s.get("payment_status") == "paid"]
            if paid:
                ss_text = f"✓ {len(paid)} paid (${sum(s.get('amount_total',0) for s in paid):.0f})"
            else:
                ss_text = f"⏳ {len(ss)} session(s), unpaid"
        else:
            ss_text = "—"
        bg = "#e7f7e3" if status == "CONVERTED" else ("#fff5e1" if ss else "#ffffff")
        lines.append(
            f"<tr style='background:{bg}'>"
            f"<td style='padding:8px;border:1px solid #ddd'><code>{email}</code></td>"
            f"<td style='padding:8px;border:1px solid #ddd'>{sent}</td>"
            f"<td style='padding:8px;border:1px solid #ddd'><strong>{status}</strong></td>"
            f"<td style='padding:8px;border:1px solid #ddd'>{plan}</td>"
            f"<td style='padding:8px;border:1px solid #ddd'>{ss_text}</td>"
            f"</tr>"
        )
    lines.extend([
        "</table>",
        "<hr style='margin:24px 0;border:0;border-top:1px solid #eee'/>",
        f"<p style='color:#666;font-size:12px'>Polling auto-stops when all {len(outcomes)} convert or {_POLL_WINDOW_DAYS} days elapse.</p>",
        "<p style='color:#666;font-size:12px'>",
        "Live dashboard: <a href='https://dchub-backend-production.up.railway.app/admin/funnel-health'>admin/funnel-health</a> · ",
        "Resend events: <a href='https://resend.com/emails'>resend.com/emails</a> (for open/click tracking)",
        "</p>",
        "</div>",
    ])
    return "\n".join(lines)


def _send_summary_email(html_body: str, subject: str | None = None):
    if not _RESEND_KEY:
        return False, "no_resend_key", ""
    if not _OPERATOR_EMAIL:
        return False, "no_operator_email", ""
    import requests as _r
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    subj = subject or f"DC Hub: Half-price annual outcomes — {today}"
    try:
        r = _r.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {_RESEND_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "from":    f"{_FROM_NAME} <{_FROM_EMAIL}>",
                "to":      [_OPERATOR_EMAIL],
                "subject": subj,
                "html":    html_body,
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            try:
                return True, "sent", (r.json() or {}).get("id", "")
            except Exception:
                return True, "sent", ""
        return False, f"status_{r.status_code}_{r.text[:80]}", ""
    except Exception as e:
        return False, f"{type(e).__name__}", ""


def run_campaign_outcome_check(send_summary: bool = True) -> dict:
    """Cron entry — checks campaign outcomes, sends summary email."""
    out = {
        "campaign":            _CAMPAIGN_NAME,
        "ran_at":              datetime.datetime.utcnow().isoformat() + "Z",
        "outcomes":            [],
        "stripe_sessions":     {},
        "converted_count":     0,
        "pending_count":       0,
        "summary_sent":        False,
        "summary_status":      "",
        "summary_msg_id":      "",
        "stop_condition_met":  None,
        "errors":              [],
    }

    # Kill switch.
    if os.environ.get("CAMPAIGN_OUTCOME_POLLER_DISABLE", "").lower() in ("1","true","yes"):
        out["errors"].append("kill_switch_CAMPAIGN_OUTCOME_POLLER_DISABLE")
        return out

    # Auto-stop after 14 days.
    days_since = (datetime.date.today() - _CAMPAIGN_FIRE_DATE).days
    if days_since > _POLL_WINDOW_DAYS:
        out["stop_condition_met"] = f"poll_window_elapsed ({days_since}d > {_POLL_WINDOW_DAYS}d)"
        return out

    outcomes = _query_campaign_outcomes()
    out["outcomes"] = outcomes

    if not outcomes:
        out["errors"].append("no_campaign_log_entries")
        return out

    emails = [o.get("email") for o in outcomes if o.get("email")]
    stripe_sessions = _query_stripe_sessions(emails)
    out["stripe_sessions"] = stripe_sessions

    converted = [o for o in outcomes if o.get("status") == "CONVERTED"]
    out["converted_count"] = len(converted)
    out["pending_count"]   = len(outcomes) - len(converted)

    # Auto-stop after all converted.
    if len(converted) == len(outcomes):
        out["stop_condition_met"] = "all_converted"

    if send_summary:
        html = _build_summary_html(outcomes, stripe_sessions)
        ok, status, msg_id = _send_summary_email(html)
        out["summary_sent"]   = ok
        out["summary_status"] = status
        out["summary_msg_id"] = msg_id
        if not ok:
            out["errors"].append(f"resend:{status}")

    return out


# ────────────────────────── Routes ──────────────────────────

@campaign_outcomes_bp.route(
    "/api/v1/admin/campaign/halfprice-annual/outcomes", methods=["GET"]
)
def halfprice_outcomes_view():
    """Returns the outcome data. Pass ?send=1 to also email the summary."""
    _, err = _admin_gate()
    if err:
        return jsonify({"error": err[0]}), err[1]
    send = (request.args.get("send", "0") or "0").lower() in ("1", "true", "yes")
    return jsonify(run_campaign_outcome_check(send_summary=send)), 200


@campaign_outcomes_bp.route(
    "/api/v1/admin/campaign/halfprice-annual/outcomes/send", methods=["POST"]
)
def halfprice_outcomes_send():
    """Force-send the summary email now (even if no new conversions)."""
    _, err = _admin_gate()
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify(run_campaign_outcome_check(send_summary=True)), 200


def init_campaign_outcome_tables():
    """No new tables — uses existing campaign_log + users."""
    pass
