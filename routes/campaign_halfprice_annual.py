"""Half-price Pro Annual upgrade campaign — outreach to active monthly Pro subs.

Companion to the renewal_nudge / expired_demote lifecycle work that shipped
2026-06-06 (commits 594756e8, ~r65). The Pro Annual one-time payment link
(price_1TecqhJ9ey2ATcQl4Hmp99OU, $1,188) is 50% off list ($2,388) and
effectively $99/mo across 12 months — a 50% discount off the current
$199/mo Pro Monthly subscribers are paying.

This module targets that exact cohort: active recurring Pro Monthly subs
who haven't already gone annual. Math:
  - Current run-rate: 11 × $199 = $2,189/mo recurring
  - If all 11 convert: 11 × $1,188 = $13,068 cash-forward + 2027 retention
  - If 5 convert, 1 churns, 5 stay monthly: still net positive
    (5 × $1,188 = $5,940 forward vs ~$1,000 saved-on-churn)
  - Worst case (everyone bounces): the nudge accelerated decisions
    we'd discover at month-12 anyway

DRY-RUN GATE (high-stakes user action):
  GET  /preview          returns the eligible candidate list + a fire_key
                         (10-minute TTL, in-memory). Safe to spam.
  POST /fire?fire_key=X  requires the fire_key from the most recent preview
                         (rotating per-call). Caps at 50 emails. Idempotent
                         via UNIQUE(campaign_name, email) on campaign_log.

Endpoints:
  GET  /api/v1/admin/campaign/halfprice-annual/preview
  POST /api/v1/admin/campaign/halfprice-annual/fire?fire_key=<x>

Env vars:
  DCHUB_RESEND_API_KEY              (required for live fire — fail-soft)
  DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY (admin gate)
  DCHUB_FROM_NAME                   default "DC Hub"
  DCHUB_FROM_EMAIL                  default "alerts@dchub.cloud"
  CAMPAIGN_DISABLE=1                kill switch — blocks all /fire calls
  DCHUB_HALFPRICE_DRY_RUN=1         forces dry-run even on /fire

Campaign id: 'halfprice_annual_2026_06' (frozen — re-running fire on a row
already in campaign_log for this name is a no-op via UNIQUE constraint).
"""
from __future__ import annotations

import os
import sys
import json
import secrets
import datetime
import logging
from typing import Any

from flask import Blueprint, jsonify, request


logger = logging.getLogger(__name__)

campaign_halfprice_annual_bp = Blueprint(
    "campaign_halfprice_annual", __name__)


# ── Config ────────────────────────────────────────────────────────────

_ADMIN_KEY  = (os.environ.get("DCHUB_ADMIN_KEY")
               or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY")
               or os.environ.get("RESEND_API_KEY") or "").strip()
_FROM_NAME  = os.environ.get("DCHUB_FROM_NAME",  "DC Hub")
_FROM_EMAIL = os.environ.get("DCHUB_FROM_EMAIL", "alerts@dchub.cloud")

# Same Pro Annual link as renewal_nudge.py (and the canonical
# api_server.py / api_tier_gating.py wiring).
_STRIPE_PRO_ANNUAL = "https://buy.stripe.com/dRm7sM6wW7Zz1edgOCaZi07"

# Campaign identity — freeze it so the UNIQUE(campaign_name, email)
# idempotency works across reruns/redeploys.
_CAMPAIGN_NAME = "halfprice_annual_2026_06"

# Safety caps.
_PER_RUN_CAP = 50
_FIRE_KEY_TTL_SECONDS = 600  # 10 minutes

# Fire-key store: {fire_key: expires_at_epoch}. Process-local. Each
# /preview call generates a NEW key and overwrites the previous one
# (only the latest preview's key is honored — single in-flight blast).
_FIRE_KEYS: dict[str, float] = {}


# ── DB plumbing ───────────────────────────────────────────────────────

def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return None
        c = psycopg2.connect(url, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("campaign db_conn failed: %s", e)
        return None


# Belt-and-suspenders schema bootstrap (schema_repair.py also runs this,
# but a cold deploy hitting /preview before /schema/repair would 500).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_log (
    id              SERIAL PRIMARY KEY,
    campaign_name   TEXT NOT NULL,
    user_id         TEXT,
    email           TEXT NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resend_message_id TEXT,
    payload         JSONB,
    UNIQUE(campaign_name, email)
);
CREATE INDEX IF NOT EXISTS ix_campaign_log_campaign ON campaign_log(campaign_name);
CREATE INDEX IF NOT EXISTS ix_campaign_log_sent_at ON campaign_log(sent_at DESC);
"""


def _ensure_schema(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception as e:
        logger.warning("campaign _ensure_schema failed: %s", e)


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[campaign-halfprice] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ── Auth / admin gate ────────────────────────────────────────────────

def _admin_authorized() -> bool:
    """Same gate as expired_demote — accept admin key via header OR query."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key")
                or request.args.get("key") or "")
    if _ADMIN_KEY and provided == _ADMIN_KEY:
        return True
    return False


# ── Fire-key lifecycle ───────────────────────────────────────────────

def _mint_fire_key() -> str:
    """Generate a fresh fire_key, store it with TTL, GC expired ones."""
    now = datetime.datetime.utcnow().timestamp()
    # GC any expired keys before we add.
    expired = [k for k, exp in _FIRE_KEYS.items() if exp <= now]
    for k in expired:
        _FIRE_KEYS.pop(k, None)
    key = secrets.token_urlsafe(24)
    _FIRE_KEYS[key] = now + _FIRE_KEY_TTL_SECONDS
    return key


def _valid_fire_key(key: str) -> bool:
    if not key:
        return False
    now = datetime.datetime.utcnow().timestamp()
    exp = _FIRE_KEYS.get(key)
    if exp is None:
        return False
    if exp <= now:
        _FIRE_KEYS.pop(key, None)
        return False
    return True


# ── Email rendering ──────────────────────────────────────────────────

def _render_email(email: str) -> tuple[str, str, str]:
    """Returns (subject, body_html, body_text_preview)."""
    subject = "Lock in $99/mo Pro for a year — 50% off your current rate"

    # Prefilled-email Stripe link for one-click checkout.
    annual_url = f"{_STRIPE_PRO_ANNUAL}?prefilled_email={email}"

    body_html = f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;max-width:600px;
margin:0 auto;padding:1.5rem;color:#1f2937;line-height:1.55">

<div style="background:linear-gradient(135deg,#1e40af,#0f766e);color:white;padding:1.25rem;border-radius:8px;margin-bottom:1.5rem">
 <h2 style="margin:0;font-size:1.2rem">Lock in $99/mo Pro for a year</h2>
 <p style="margin:.25rem 0 0;color:#d1fae5;font-size:.9rem">50% off your current $199/mo rate — through end of June only</p>
</div>

<p>Hi,</p>

<p>Heads up: we just opened a half-price Pro Annual offer for active
monthly subscribers — <strong>$1,188 paid once for the next 12 months</strong>.
That's effectively <strong>$99/mo</strong>, 50% off your current $199
monthly rate.</p>

<p>We did this for two reasons: (1) the platform's a lot more useful
than it was 6 months ago &mdash; 33 MCP tools, 232 markets in the DCPI,
15 live Market Briefs, the new Map Brief PDF export &mdash; and (2) a few
of you have asked about an annual option specifically.</p>

<p style="margin-top:1.5rem"><strong>If you want it:</strong></p>

<p style="text-align:center;margin:1.25rem 0">
 <a href="{annual_url}" style="display:inline-block;background:#10b981;color:white;padding:.85rem 1.75rem;border-radius:6px;font-weight:700;text-decoration:none;font-size:1.05rem">
   Get Pro Annual &mdash; $1,188 &rarr;
 </a>
</p>

<p style="color:#4b5563;font-size:.92rem">(One-time charge; your monthly
auto-debit stops the moment Stripe processes the annual. No
double-billing.)</p>

<p style="margin-top:1.5rem">This is for current monthly subs only and
goes away end of June. Reply if you want a custom quote or have questions
about anything we shipped this quarter.</p>

<p style="color:#1f2937;font-size:.95rem;margin-top:1.5rem">
 Jonathan<br>
 <span style="color:#6b7280">DC Hub</span>
</p>

<p style="color:#9ca3af;font-size:.75rem;margin-top:2rem;border-top:1px solid #e5e7eb;padding-top:.75rem">
 You're receiving this because you have an active DC Hub Pro Monthly subscription. Reply STOP to opt out.
</p>

</body></html>"""

    # Plain-text preview for the dry-run JSON response.
    body_text_preview = (
        "Heads up: we just opened a half-price Pro Annual offer for active "
        "monthly subscribers — $1,188 paid once for the next 12 months. "
        "That's effectively $99/mo, 50% off your current $199 monthly rate..."
    )[:200]

    return subject, body_html, body_text_preview


# ── Resend integration ───────────────────────────────────────────────

def _send_via_resend(to_email: str, subject: str, body_html: str
                     ) -> tuple[bool, str, str]:
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


# ── Eligibility query ────────────────────────────────────────────────

_ELIGIBILITY_SQL = """
    SELECT u.id, u.email, u.plan, u.created_at, u.source_plan
      FROM users u
     WHERE u.plan = 'pro'
       AND u.subscription_status = 'active'
       AND (u.source_plan IS NULL
            OR u.source_plan IS DISTINCT FROM 'pro_annual_onetime')
       AND u.email IS NOT NULL
       AND u.email <> ''
       -- Exclude founder's self-test accounts (one-shot campaign safety)
       AND LOWER(u.email) NOT IN (
           'azmartone@gmail.com',
           'azmartone@icloud.com',
           'jonathan@dchub.cloud',
           'jonathan.martone@arcadianinfra.com'
       )
       -- Catch every gmail+alias variant used for Stripe testing
       AND LOWER(u.email) NOT LIKE 'azmartone+%@gmail.com'
       AND LOWER(u.email) NOT LIKE 'jonathan%@dchub.%'
       AND NOT EXISTS (
           SELECT 1 FROM campaign_log c
            WHERE c.campaign_name = %s
              AND c.email = u.email
       )
     ORDER BY u.created_at ASC
     LIMIT %s
"""


def _find_eligible(cur) -> list[dict]:
    """Returns active monthly Pro subs who haven't been emailed for this campaign."""
    out: list[dict] = []
    try:
        cur.execute(_ELIGIBILITY_SQL, (_CAMPAIGN_NAME, _PER_RUN_CAP))
        rows = cur.fetchall()
    except Exception as e:
        _log(f"eligibility query failed: {e}")
        return out
    for r in rows:
        uid, email, plan, created_at, src_plan = r
        # created_at may come back as datetime OR ISO string depending on
        # driver / column type — handle both defensively.
        joined_at: str | None = None
        if created_at is not None:
            try:
                joined_at = created_at.isoformat()
            except AttributeError:
                joined_at = str(created_at)
        out.append({
            "user_id":    str(uid) if uid is not None else None,
            "email":      (email or "").strip().lower(),
            "plan":       plan,
            "joined_at":  joined_at,
            "source_plan": src_plan,
        })
    return out


# ── Core orchestration ──────────────────────────────────────────────

def _build_candidates(eligible: list[dict]) -> list[dict]:
    """Enrich eligibility rows with rendered subject + body preview."""
    cands = []
    for u in eligible:
        if not u["email"] or "@" not in u["email"]:
            continue
        subject, _body_html, body_preview = _render_email(u["email"])
        cands.append({
            "user_id":      u["user_id"],
            "email":        u["email"],
            "plan":         u["plan"],
            "joined_at":    u["joined_at"],
            "subject":      subject,
            "body_preview": body_preview[:50],
        })
    return cands


def run_preview() -> dict:
    """DRY-RUN entrypoint. Returns the eligible candidate list + a fresh fire_key."""
    out: dict[str, Any] = {
        "campaign_name":   _CAMPAIGN_NAME,
        "dry_run":         True,
        "ran_at":          datetime.datetime.utcnow().isoformat() + "Z",
        "eligible_count":  0,
        "candidates":      [],
        "fire_key":        None,
        "fire_key_ttl_s":  _FIRE_KEY_TTL_SECONDS,
        "errors":          [],
    }
    c = _db_conn()
    if c is None:
        out["errors"].append("no_database")
        return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            eligible = _find_eligible(cur)
        cands = _build_candidates(eligible)
        out["eligible_count"] = len(cands)
        out["candidates"]     = cands

        # Console sanity-check log for the operator.
        emails_list = ", ".join(c["email"] for c in cands) or "(none)"
        _log(f"PREVIEW: {len(cands)} eligible — {emails_list}")

        # Mint a fresh fire-key only if there's something to fire.
        if cands:
            fk = _mint_fire_key()
            out["fire_key"] = fk
            _log(f"PREVIEW: fire_key={fk} (ttl={_FIRE_KEY_TTL_SECONDS}s)")
        else:
            out["fire_key"] = None
            _log("PREVIEW: nothing to fire (no fire_key minted)")
    except Exception as e:
        out["errors"].append(f"preview: {e}")
        _log(f"PREVIEW error: {e}")
    finally:
        try: c.close()
        except Exception: pass
    return out


def run_fire(fire_key: str) -> dict:
    """LIVE entrypoint. Validates fire_key, iterates, sends, writes log."""
    out: dict[str, Any] = {
        "campaign_name":   _CAMPAIGN_NAME,
        "dry_run":         False,
        "ran_at":          datetime.datetime.utcnow().isoformat() + "Z",
        "checked":         0,
        "sent":            [],
        "skipped":         [],
        "errors":          [],
    }

    # Kill switch.
    if os.environ.get("CAMPAIGN_DISABLE", "").lower() in ("1", "true", "yes"):
        out["errors"].append("kill_switch_CAMPAIGN_DISABLE")
        return out

    # Forced dry-run env (belt-and-suspenders).
    env_dry = os.environ.get("DCHUB_HALFPRICE_DRY_RUN", ""
                             ).lower() in ("1", "true", "yes")

    # Fire-key gate.
    if not _valid_fire_key(fire_key):
        out["errors"].append("invalid_or_expired_fire_key")
        return out

    # Single-use: pop the key so it can't be reused.
    _FIRE_KEYS.pop(fire_key, None)

    c = _db_conn()
    if c is None:
        out["errors"].append("no_database")
        return out

    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            eligible = _find_eligible(cur)
            out["checked"] = len(eligible)

            for u in eligible[:_PER_RUN_CAP]:
                email   = u["email"]
                user_id = u["user_id"]
                if not email or "@" not in email:
                    out["skipped"].append({"user_id": user_id, "reason": "bad_email"})
                    continue

                subject, body_html, _preview = _render_email(email)

                if env_dry:
                    _log(f"FIRE [DRY_RUN_ENV] would have sent → {email}")
                    out["sent"].append({"user_id": user_id, "email": email,
                                         "dry_run": True})
                    continue

                ok, info, msg_id = _send_via_resend(email, subject, body_html)

                # Write campaign_log row even on failure so we don't
                # spam the same user on retry. UNIQUE(campaign_name, email)
                # makes this implicitly idempotent.
                try:
                    payload_json = json.dumps({
                        "info":    info,
                        "subject": subject,
                        "ok":      bool(ok),
                    })
                    cur.execute("""
                        INSERT INTO campaign_log
                          (campaign_name, user_id, email, resend_message_id, payload)
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (campaign_name, email) DO NOTHING
                    """, (_CAMPAIGN_NAME, user_id, email, msg_id, payload_json))
                except Exception as ie:
                    _log(f"log insert failed for {email}: {ie}")

                rec = {
                    "user_id":    user_id,
                    "email":      email,
                    "info":       info,
                    "message_id": msg_id,
                }
                if ok:
                    out["sent"].append(rec)
                    _log(f"FIRE: sent → {email} (msg_id={msg_id})")
                else:
                    out["errors"].append(rec)
                    _log(f"FIRE: failed → {email}: {info}")
    except Exception as e:
        out["errors"].append(f"top-level: {e}")
        _log(f"FIRE top-level error: {e}")
    finally:
        try: c.close()
        except Exception: pass

    return out


# ── HTTP endpoints ───────────────────────────────────────────────────

@campaign_halfprice_annual_bp.route(
    "/api/v1/admin/campaign/halfprice-annual/preview",
    methods=["GET", "POST"])
def halfprice_preview():
    """DRY-RUN: return candidate list + freshly-minted fire_key (10-min TTL)."""
    if not _admin_authorized():
        return jsonify(error="admin_key_required"), 401
    return jsonify(run_preview()), 200


@campaign_halfprice_annual_bp.route(
    "/api/v1/admin/campaign/halfprice-annual/fire",
    methods=["POST"])
def halfprice_fire():
    """LIVE: require fire_key from the most recent preview call."""
    if not _admin_authorized():
        return jsonify(error="admin_key_required"), 401
    fire_key = (request.args.get("fire_key")
                or request.headers.get("X-Fire-Key") or "").strip()
    return jsonify(run_fire(fire_key)), 200


@campaign_halfprice_annual_bp.route(
    "/api/v1/admin/campaign/halfprice-annual/log",
    methods=["GET"])
def halfprice_log():
    """Recent sends for this campaign (operator dashboard convenience)."""
    if not _admin_authorized():
        return jsonify(error="admin_key_required"), 401
    c = _db_conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id, email, sent_at, resend_message_id, payload
                  FROM campaign_log
                 WHERE campaign_name = %s
                 ORDER BY sent_at DESC
                 LIMIT 100
            """, (_CAMPAIGN_NAME,))
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "user_id":           r["user_id"],
        "email":             r["email"],
        "sent_at":           r["sent_at"].isoformat() if r["sent_at"] else None,
        "resend_message_id": r["resend_message_id"],
    } for r in rows]
    return jsonify(campaign=_CAMPAIGN_NAME, log=out, count=len(out)), 200
