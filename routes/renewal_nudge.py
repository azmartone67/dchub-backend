"""Day-330 renewal nudge — one-time Pro Annual buyers approaching expiry.

Companion to commit 594756e8 which stamped users.tier_expires_at =
NOW() + 365 days + users.source_plan = 'pro_annual_onetime' on every
one-time $1,188 Pro Annual checkout. Without this nudge, those buyers
silently churn at day 365 (no Stripe-side renewal, no
customer.subscription.* event).

Logic:
  - users.source_plan = 'pro_annual_onetime'
    AND users.email IS NOT NULL
    AND users.tier_expires_at BETWEEN NOW()+20d AND NOW()+35d
    AND no renewal_nudge_log row in the last 7 days
  - For each: render renewal email (Stripe annual + monthly buy links),
    POST to Resend, log to renewal_nudge_log.

Endpoints:
  POST /api/v1/admin/renewal-nudge/send       admin cron entry
  POST /api/v1/admin/renewal-nudge/preview    admin DRY-RUN preview
  GET  /api/v1/renewal-nudge/log              public sent history (recent)

Env vars:
  DCHUB_RESEND_API_KEY              (required — fail-soft if missing)
  DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY (admin gate)
  DCHUB_FROM_NAME                   default "DC Hub"
  DCHUB_FROM_EMAIL                  default "alerts@dchub.cloud"
  DCHUB_RENEWAL_NUDGE_DRY_RUN=1     skip Resend POST, still log "WOULD HAVE SENT"
  DCHUB_RENEWAL_NUDGE_DISABLE=1     kill switch — no-op

Cron: crawler_scheduler.SCHEDULE "renewal_nudge_onetime" 14:00/14:00 UTC
(same-hour pair → one run/day). 50-email per-run safety cap.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


renewal_nudge_bp = Blueprint("renewal_nudge", __name__)


_ADMIN_KEY  = (os.environ.get("DCHUB_ADMIN_KEY")
               or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY")
               or os.environ.get("RESEND_API_KEY") or "").strip()
_FROM_NAME  = os.environ.get("DCHUB_FROM_NAME",  "DC Hub")
_FROM_EMAIL = os.environ.get("DCHUB_FROM_EMAIL", "alerts@dchub.cloud")

# Pro Annual one-time ($1,188 — 50% off list of $2,388). Same link used
# on the checkout flow (api_server.py:979, api_tier_gating.py:67).
_STRIPE_PRO_ANNUAL  = "https://buy.stripe.com/dRm7sM6wW7Zz1edgOCaZi07"
# Pro Monthly fallback ($199/mo) for buyers who'd rather stay flexible.
_STRIPE_PRO_MONTHLY = "https://buy.stripe.com/eVq5kE4oOfs13mleGuaZi0h"

# Nudge window: 20-35 days before expiry. 7-day cooldown on the log.
# 50/run safety cap (typical day-330 cohort will be << 50).
_NUDGE_WINDOW_MIN_DAYS = 20
_NUDGE_WINDOW_MAX_DAYS = 35
_NUDGE_COOLDOWN_DAYS   = 7
_NUDGE_CAP_PER_RUN     = 50


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS renewal_nudge_log (
    id                       BIGSERIAL PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    email                    TEXT NOT NULL,
    sent_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    days_remaining_at_send   INT,
    resend_message_id        TEXT,
    status                   TEXT NOT NULL DEFAULT 'sent',
    delivery_info            TEXT
);
CREATE INDEX IF NOT EXISTS ix_renewal_nudge_log_user_sent
    ON renewal_nudge_log(user_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS ix_renewal_nudge_log_email_sent
    ON renewal_nudge_log(email, sent_at DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _send_via_resend(to_email: str, subject: str, body_html: str) -> tuple[bool, str, str]:
    """POST to Resend. Returns (ok, info_string, message_id)."""
    if not _RESEND_KEY:
        return False, "no_resend_key", ""
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            json={"from": f"{_FROM_NAME} <{_FROM_EMAIL}>",
                  "to": [to_email], "subject": subject, "html": body_html},
            headers={"Authorization": f"Bearer {_RESEND_KEY}"},
            timeout=10,
        )
        if r.status_code < 300:
            try:
                msg_id = (r.json() or {}).get("id", "") or ""
            except Exception:
                msg_id = ""
            return True, f"sent_{r.status_code}", msg_id
        return False, f"status_{r.status_code}_{r.text[:80]}", ""
    except Exception as e:
        return False, f"{type(e).__name__}:{str(e)[:60]}", ""


def _render_nudge_html(email: str, days_remaining: int,
                       expires_at: datetime.datetime | None) -> tuple[str, str]:
    """Returns (subject, body_html)."""
    if expires_at:
        try:
            date_pretty = expires_at.strftime("%B %-d, %Y")
        except Exception:
            date_pretty = expires_at.strftime("%B %d, %Y").replace(" 0", " ")
    else:
        date_pretty = f"in about {days_remaining} days"

    subject = f"Your DC Hub Pro Annual expires in {days_remaining} days"

    # Prefilled-email Stripe links for one-click renewal.
    annual_url  = f"{_STRIPE_PRO_ANNUAL}?prefilled_email={email}"
    monthly_url = f"{_STRIPE_PRO_MONTHLY}?prefilled_email={email}"

    body = f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;max-width:600px;
margin:0 auto;padding:1.5rem;color:#1f2937;line-height:1.55">

<div style="background:linear-gradient(135deg,#1e40af,#0f766e);color:white;padding:1.25rem;border-radius:8px;margin-bottom:1.5rem">
 <h2 style="margin:0;font-size:1.2rem">Your DC Hub Pro Annual renews in {days_remaining} days</h2>
 <p style="margin:.25rem 0 0;color:#d1fae5;font-size:.9rem">Expires {date_pretty}</p>
</div>

<p>Hi,</p>

<p>Quick heads-up: your DC Hub Pro Annual subscription expires on
<strong>{date_pretty}</strong>.</p>

<p>Your usage was significant this year — market intel queries, live
grid lookups, and reports across the platform. (Reply if you'd like
the full breakdown from your dashboard.)</p>

<p style="margin-top:1.5rem"><strong>Renew at the same half-price rate</strong>
($1,188/year, 50% off list — same link as last time):</p>

<p style="text-align:center;margin:1.25rem 0">
 <a href="{annual_url}" style="display:inline-block;background:#10b981;color:white;padding:.75rem 1.5rem;border-radius:6px;font-weight:700;text-decoration:none;font-size:1rem">
   Renew Pro Annual — $1,188 →
 </a>
</p>

<p>Or switch to <strong>Pro Monthly</strong> if you'd prefer to stay
flexible — $199/mo, cancel anytime:</p>

<p style="text-align:center;margin:1.25rem 0">
 <a href="{monthly_url}" style="display:inline-block;background:#1f2937;color:white;padding:.6rem 1.25rem;border-radius:6px;font-weight:600;text-decoration:none;font-size:.95rem">
   Switch to Pro Monthly — $199/mo →
 </a>
</p>

<p style="color:#6b7280;font-size:.9rem;margin-top:2rem">
 Questions? Just reply to this email.
</p>

<p style="color:#1f2937;font-size:.95rem;margin-top:1rem">
 Jonathan<br>
 <span style="color:#6b7280">DC Hub</span>
</p>

<p style="color:#9ca3af;font-size:.75rem;margin-top:2rem;border-top:1px solid #e5e7eb;padding-top:.75rem">
 You're receiving this because you have an active DC Hub Pro Annual subscription. Reply STOP to opt out of renewal reminders.
</p>

</body></html>"""
    return subject, body


def find_eligible_users(cur) -> list[dict]:
    """Returns rows in the nudge window who haven't been nudged in 7 days."""
    out: list[dict] = []
    try:
        cur.execute(f"""
            SELECT u.id, u.email, u.tier_expires_at,
                   u.source_plan,
                   EXTRACT(EPOCH FROM (u.tier_expires_at - NOW())) / 86400.0
                     AS days_remaining
              FROM users u
             WHERE u.source_plan = 'pro_annual_onetime'
               AND u.email IS NOT NULL
               AND u.email <> ''
               AND u.tier_expires_at IS NOT NULL
               AND u.tier_expires_at BETWEEN NOW() + INTERVAL '{_NUDGE_WINDOW_MIN_DAYS} days'
                                          AND NOW() + INTERVAL '{_NUDGE_WINDOW_MAX_DAYS} days'
               AND NOT EXISTS (
                 SELECT 1 FROM renewal_nudge_log r
                  WHERE r.user_id = u.id
                    AND r.sent_at > NOW() - INTERVAL '{_NUDGE_COOLDOWN_DAYS} days'
               )
             ORDER BY u.tier_expires_at ASC
             LIMIT {_NUDGE_CAP_PER_RUN}
        """)
        rows = cur.fetchall()
    except Exception:
        return out
    for r in rows:
        days = int(round(float(r[4] or 0)))
        out.append({
            "user_id":        str(r[0]),
            "email":          (r[1] or "").strip().lower(),
            "tier_expires_at": r[2],
            "source_plan":    r[3],
            "days_remaining": days,
        })
    return out


def run_renewal_nudge(dry_run: bool = False) -> dict:
    """Find candidates, send (or log dry-run), record to renewal_nudge_log."""
    out: dict = {
        "sent":       [],
        "skipped":    [],
        "errors":     [],
        "dry_run":    dry_run,
        "ran_at":     datetime.datetime.utcnow().isoformat() + "Z",
    }
    if os.environ.get("DCHUB_RENEWAL_NUDGE_DISABLE", "").lower() in ("1", "true", "yes"):
        out["errors"].append("kill_switch_DCHUB_RENEWAL_NUDGE_DISABLE")
        return out

    env_dry = os.environ.get("DCHUB_RENEWAL_NUDGE_DRY_RUN", "").lower() in ("1", "true", "yes")
    dry_run = dry_run or env_dry
    out["dry_run"] = dry_run

    c = _conn()
    if c is None:
        out["errors"].append("no_database")
        return out

    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            eligible = find_eligible_users(cur)
            out["eligible_count"] = len(eligible)
            for u in eligible:
                if not u["email"] or "@" not in u["email"]:
                    out["skipped"].append({"user_id": u["user_id"],
                                            "reason": "bad_email"})
                    continue

                subject, body = _render_nudge_html(
                    u["email"], u["days_remaining"], u["tier_expires_at"])

                if dry_run:
                    print(f"📧 [DRY-RUN] WOULD HAVE SENT renewal nudge → "
                          f"{u['email']} (user_id={u['user_id']}, "
                          f"days_remaining={u['days_remaining']})")
                    out["sent"].append({
                        "user_id":        u["user_id"],
                        "email":          u["email"],
                        "days_remaining": u["days_remaining"],
                        "subject":        subject,
                        "dry_run":        True,
                    })
                    continue

                ok, info, msg_id = _send_via_resend(u["email"], subject, body)
                try:
                    cur.execute("""
                        INSERT INTO renewal_nudge_log
                          (user_id, email, days_remaining_at_send,
                           resend_message_id, status, delivery_info)
                        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """, (u["user_id"], u["email"], u["days_remaining"],
                          msg_id, "sent" if ok else "send_failed", info))
                except Exception:
                    pass
                rec = {
                    "user_id":        u["user_id"],
                    "email":          u["email"],
                    "days_remaining": u["days_remaining"],
                    "info":           info,
                    "message_id":     msg_id,
                }
                (out["sent"] if ok else out["errors"]).append(rec)
    finally:
        try: c.close()
        except Exception: pass

    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _check_admin() -> tuple[bool, str]:
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return False, "unauthorized"
    return True, ""


@renewal_nudge_bp.route("/api/v1/admin/renewal-nudge/send", methods=["POST"])
def renewal_nudge_send():
    ok, err = _check_admin()
    if not ok:
        return jsonify(error=err), 401
    dry = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    return jsonify(run_renewal_nudge(dry_run=dry)), 200


@renewal_nudge_bp.route("/api/v1/admin/renewal-nudge/preview", methods=["POST", "GET"])
def renewal_nudge_preview():
    """DRY-RUN: returns the list of users that WOULD be nudged on the next
    run, without POSTing to Resend or writing renewal_nudge_log."""
    ok, err = _check_admin()
    if not ok:
        return jsonify(error=err), 401
    return jsonify(run_renewal_nudge(dry_run=True)), 200


@renewal_nudge_bp.route("/api/v1/renewal-nudge/log", methods=["GET"])
def renewal_nudge_log_recent():
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id, email, sent_at,
                       days_remaining_at_send, status, resend_message_id
                  FROM renewal_nudge_log
                 ORDER BY sent_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "user_id":               r["user_id"],
        "email":                 r["email"],
        "sent_at":               r["sent_at"].isoformat() if r["sent_at"] else None,
        "days_remaining_at_send": int(r["days_remaining_at_send"] or 0),
        "status":                r["status"],
        "resend_message_id":     r["resend_message_id"],
    } for r in rows]
    return jsonify(log=out, count=len(out)), 200
