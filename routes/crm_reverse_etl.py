"""Phase r74 (2026-06-07) — CRM Reverse ETL
==========================================================================
High-intent capture events → outbound CRM queue → optional async push to
Salesforce / HubSpot. Default = STUB MODE (queue-only, manual CSV export).

The DC Hub funnel already captures rich attribution chains across:
  - mcp_high_intent_sessions  (MCP 3-strike claim)
  - state_visitor_intent      (State of 2026 visitor 2-brief threshold)
  - newsletter_subscribers    (signup)
  - auto_trial_keys           (trial key mint)
  - mcp_dev_keys / users      (paid conversion)

…but none of that ever made it to a CRM. This module is the single funnel
that turns each high-intent event into a CRM-ready lead row with full
attribution chain (referer → page sequence → trigger → trial key →
first MCP call → conversion), idempotently queues it, and (when CRM
credentials are present) async-pushes to Salesforce / HubSpot.

Wiring:
  - capture_event(event_type, payload)  ← called by hooks (see below)
  - flush_outbound_queue(limit=N)       ← called by crawler_scheduler cron
  - admin endpoints under /api/v1/admin/crm/*  (X-Admin-Key gated)
  - admin dashboard at /admin/crm-outbound

Event types:
  - 'mcp_high_intent'           (mcp_high_intent_claim hook on mint)
  - 'state_visitor_high_intent' (state_visitor_claim hook on mint)
  - 'newsletter_signup'         (weekly_newsletter hook on subscribe)
  - 'trial_key_activated'       (auto_trial hook on mint)
  - 'paid_conversion'           (Stripe checkout.session.completed)

Safety:
  - CRM_REVERSE_ETL_DISABLE=1   kill switch (capture is no-op)
  - CRM_REVERSE_ETL_DRY_RUN=1   captures but never pushes
  - Idempotent via UNIQUE(event_type, lead_email, captured_date)
  - Admin-gated read endpoints — PII (emails) never leaves admin scope

PII Note: this queue holds the EMAILS of captured leads. Access is
strictly X-Admin-Key gated. Do NOT expose any public endpoint that
returns lead_email or attribution_chain. The CSV export likewise is
admin-only.
"""
from __future__ import annotations

import os
import json
import logging
import datetime
import hashlib
from typing import Any, Callable

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # we'll fail-soft on push attempts

from flask import Blueprint, request, jsonify, Response

logger = logging.getLogger(__name__)

crm_reverse_etl_bp = Blueprint("crm_reverse_etl", __name__)


# ── env config ───────────────────────────────────────────────────────

CRM_PROVIDER = (os.environ.get("CRM_PROVIDER") or "stub").strip().lower()
if CRM_PROVIDER not in ("salesforce", "hubspot", "stub"):
    CRM_PROVIDER = "stub"

DISABLE = (os.environ.get("CRM_REVERSE_ETL_DISABLE") or "").strip() in ("1", "true", "yes")
DRY_RUN = (os.environ.get("CRM_REVERSE_ETL_DRY_RUN") or "").strip() in ("1", "true", "yes")

SF_INSTANCE_URL = (os.environ.get("SALESFORCE_INSTANCE_URL") or "").rstrip("/")
SF_ACCESS_TOKEN = os.environ.get("SALESFORCE_ACCESS_TOKEN") or ""
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY") or ""
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY") or ""

# Admin gate
DCHUB_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


# ── DB helper (lazy import to avoid circular) ────────────────────────

def _conn():
    """Return a Postgres connection (or None if unavailable)."""
    try:
        from main import get_pg_connection, return_pg_connection  # noqa: F401
        return get_pg_connection()
    except Exception as e:
        logger.warning("[crm_etl] no DB: %s", e)
        return None


def _return(c, error: bool = False):
    try:
        from main import return_pg_connection
        return_pg_connection(c, error=error)
    except Exception:
        try: c.close()
        except Exception: pass


# ── schema ───────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crm_outbound_queue (
    id                  BIGSERIAL PRIMARY KEY,
    event_type          TEXT NOT NULL,
    captured_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    captured_date       DATE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::date,
    lead_email          TEXT,
    lead_session_id     TEXT,
    lead_company        TEXT,
    lead_title          TEXT,
    lead_first_name     TEXT,
    lead_last_name      TEXT,
    attribution_chain   JSONB,
    intent_score        INTEGER NOT NULL DEFAULT 0,
    crm_pushed_at       TIMESTAMPTZ,
    crm_provider        TEXT,
    crm_external_id     TEXT,
    crm_response        JSONB,
    status              TEXT NOT NULL DEFAULT 'queued',
    push_attempts       INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_crm_q_dedup
    ON crm_outbound_queue (event_type, COALESCE(LOWER(lead_email),''),
                           COALESCE(lead_session_id,''), captured_date);
CREATE INDEX IF NOT EXISTS ix_crm_q_status
    ON crm_outbound_queue (status, captured_at DESC);
CREATE INDEX IF NOT EXISTS ix_crm_q_email
    ON crm_outbound_queue (LOWER(lead_email))
    WHERE lead_email IS NOT NULL;
-- r74.1 (2026-06-07): if a previous deploy created the table with a
-- generated `captured_date` STORED column (which requires IMMUTABLE
-- expressions Postgres won't allow with AT TIME ZONE), the INSERT
-- silently fails. Drop+recreate column as a regular date with a DEFAULT
-- so re-deploys upgrade in place.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'crm_outbound_queue'
           AND column_name = 'captured_date'
           AND is_generated = 'ALWAYS'
    ) THEN
        ALTER TABLE crm_outbound_queue
            DROP COLUMN captured_date;
        ALTER TABLE crm_outbound_queue
            ADD COLUMN captured_date DATE NOT NULL
                DEFAULT (NOW() AT TIME ZONE 'UTC')::date;
    END IF;
END$$;
"""


_SCHEMA_READY = False


def _ensure_schema(c):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        try: c.commit()
        except Exception: pass
        _SCHEMA_READY = True
    except Exception as e:
        logger.warning("[crm_etl] schema ensure failed: %s", e)
        try: c.rollback()
        except Exception: pass


# ── attribution chain builder ────────────────────────────────────────

def _build_attribution(c, event_type: str, payload: dict) -> dict:
    """Pull every signal we have on this lead and assemble the chain.

    Returns a dict shaped like:
      {
        "original_referer": "...",
        "first_seen_at":    "...",
        "page_sequence":    [...],
        "trigger_event":    {"type": event_type, "ts": "...", "detail": {...}},
        "trial_key":        {"key": "dch_trial_...", "minted_at": "...", "expires_at": "..."},
        "first_mcp_call":   {"tool": "...", "ts": "..."},
        "li_clicks":        [...],
        "conversion":       {"plan": "...", "stripe_session": "...", "ts": "..."}
      }
    """
    chain = {
        "event_type": event_type,
        "trigger_event": {
            "type": event_type,
            "ts": _now_iso(),
            "detail": {k: v for k, v in payload.items()
                       if k not in ("email", "session_id", "company")},
        },
    }
    email = (payload.get("email") or "").strip().lower() or None
    sid = (payload.get("session_id") or "").strip() or None

    # 1. MCP high-intent session — most-recent matching row
    if email or sid:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT first_hit_at, last_hit_at, paid_call_count_24h,
                              tool_name, mcp_client, user_agent,
                              claim_minted_at, claim_used_at, claim_email,
                              minted_api_key, claim_variant
                         FROM mcp_high_intent_sessions
                        WHERE (claim_email = %s OR mcp_session_id = %s)
                          AND (claim_email IS NOT NULL OR %s IS NOT NULL)
                        ORDER BY last_hit_at DESC NULLS LAST LIMIT 1""",
                    (email, sid, sid),
                )
                r = cur.fetchone()
                if r:
                    chain["mcp_session"] = {
                        "first_hit_at": _iso(r[0]),
                        "last_hit_at":  _iso(r[1]),
                        "paid_call_count_24h": int(r[2] or 0),
                        "tool":          r[3],
                        "mcp_client":    r[5],
                        "user_agent":    (r[5] or "")[:120],
                        "claim_minted_at": _iso(r[6]),
                        "claim_used_at":  _iso(r[7]),
                        "minted_api_key":  (r[9] or "")[:16] + "...",
                        "claim_variant":   r[10],
                    }
        except Exception as e:
            logger.debug("[crm_etl] mcp_high_intent lookup failed: %s", e)

    # 2. State-of-2026 visitor intent
    if email or sid:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT first_seen_at, last_event_at, brief_clicks,
                              time_on_page_seconds, brief_slugs, referer, ua,
                              hi_threshold_hit_at, claim_used_at, minted_api_key
                         FROM state_visitor_intent
                        WHERE (LOWER(email) = %s OR visitor_session_id = %s)
                          AND (email IS NOT NULL OR %s IS NOT NULL)
                        ORDER BY last_event_at DESC NULLS LAST LIMIT 1""",
                    (email, sid, sid),
                )
                r = cur.fetchone()
                if r:
                    chain["state_visitor"] = {
                        "first_seen_at":  _iso(r[0]),
                        "last_event_at":  _iso(r[1]),
                        "brief_clicks":   int(r[2] or 0),
                        "time_on_page_s": int(r[3] or 0),
                        "brief_slugs":    r[4],
                        "original_referer": r[5],
                        "user_agent":     (r[6] or "")[:120],
                        "hi_threshold_hit_at": _iso(r[7]),
                        "claim_used_at":  _iso(r[8]),
                        "minted_api_key": (r[9] or "")[:16] + "...",
                    }
                    if r[5]:
                        chain.setdefault("original_referer", r[5])
        except Exception as e:
            logger.debug("[crm_etl] state_visitor lookup failed: %s", e)

    # 3. Newsletter signup
    if email:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT subscribed_at, source, last_sent_at
                         FROM newsletter_subscribers
                        WHERE LOWER(email) = %s
                          AND unsubscribed_at IS NULL""", (email,))
                r = cur.fetchone()
                if r:
                    chain["newsletter"] = {
                        "subscribed_at": _iso(r[0]),
                        "source":        r[1],
                        "last_sent_at":  _iso(r[2]),
                    }
        except Exception as e:
            logger.debug("[crm_etl] newsletter lookup failed: %s", e)

    # 4. Trial key (auto_trial_keys)
    if email:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT api_key, minted_at, expires_at, minted_for_tool,
                              client_name, operator_name
                         FROM auto_trial_keys
                        WHERE LOWER(operator_email) = %s
                        ORDER BY minted_at DESC LIMIT 1""", (email,))
                r = cur.fetchone()
                if r:
                    chain["trial_key"] = {
                        "key":            (r[0] or "")[:16] + "...",
                        "minted_at":      _iso(r[1]),
                        "expires_at":     _iso(r[2]),
                        "minted_for_tool": r[3],
                        "mcp_client":     r[4],
                        "operator_name":  r[5],
                    }
        except Exception as e:
            logger.debug("[crm_etl] auto_trial lookup failed: %s", e)

    # 5. First MCP call (mcp_upgrade_signals — best-effort)
    if email or sid:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT tool_requested, MIN(created_at), COUNT(*)
                         FROM mcp_upgrade_signals
                        WHERE (LOWER(email) = %s OR session_id = %s)
                          AND (email IS NOT NULL OR session_id IS NOT NULL)
                        GROUP BY tool_requested
                        ORDER BY MIN(created_at) ASC LIMIT 5""",
                    (email, sid))
                rows = cur.fetchall() or []
                if rows:
                    chain["first_mcp_calls"] = [
                        {"tool": rr[0], "first_ts": _iso(rr[1]),
                         "count": int(rr[2] or 0)}
                        for rr in rows
                    ]
        except Exception:
            pass

    # 6. LinkedIn / media link clicks (last 5)
    if email or sid:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT clicked_at, short_code, dest_url, ua
                         FROM media_link_clicks
                        WHERE session_id = %s OR LOWER(email) = %s
                        ORDER BY clicked_at DESC LIMIT 5""",
                    (sid, email))
                rows = cur.fetchall() or []
                if rows:
                    chain["li_clicks"] = [
                        {"ts": _iso(rr[0]), "short_code": rr[1],
                         "dest_url": rr[2], "ua": (rr[3] or "")[:80]}
                        for rr in rows
                    ]
        except Exception:
            pass

    # 7. Paid conversion (users table)
    if email:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT plan, subscription_status, created_at,
                              stripe_customer_id
                         FROM users WHERE LOWER(email) = %s""", (email,))
                r = cur.fetchone()
                if r:
                    chain["conversion"] = {
                        "plan":              r[0],
                        "subscription_status": r[1],
                        "user_created_at":   _iso(r[2]),
                        "stripe_customer":   r[3],
                    }
        except Exception:
            pass

    return chain


# ── intent score ─────────────────────────────────────────────────────

def _compute_intent_score(event_type: str, chain: dict) -> int:
    """0-100 intent score derived from the attribution chain.

    Tier 1 (paid):              100
    Tier 2 (trial activated):    80
    Tier 3 (MCP claim used):     70
    Tier 4 (State HI hit):       60
    Tier 5 (newsletter only):    30
    + bonuses for repeat hits, multi-channel touch, etc.
    """
    score = 0
    if event_type == "paid_conversion":
        score = 100
    elif event_type == "trial_key_activated":
        score = 80
    elif event_type == "mcp_high_intent":
        score = 70
    elif event_type == "state_visitor_high_intent":
        score = 60
    elif event_type == "newsletter_signup":
        score = 30

    # Bonuses (cap at 100)
    if chain.get("mcp_session", {}).get("paid_call_count_24h", 0) >= 5:
        score += 10
    if chain.get("state_visitor", {}).get("brief_clicks", 0) >= 3:
        score += 5
    if chain.get("first_mcp_calls"):
        score += 5
    if chain.get("li_clicks"):
        score += 3
    if (chain.get("newsletter") and chain.get("mcp_session")):
        # multi-channel touch
        score += 7
    return max(0, min(100, score))


# ── company enrichment (Hunter.io) ───────────────────────────────────

_ENRICH_CACHE: dict[str, dict] = {}


def _enrich_company(email: str) -> dict:
    """Best-effort company enrichment via Hunter.io. Returns {} on miss."""
    if not email or "@" not in email or not HUNTER_API_KEY or requests is None:
        return {}
    domain = email.split("@", 1)[1].strip().lower()
    if not domain or domain in ("gmail.com", "yahoo.com", "outlook.com",
                                  "hotmail.com", "icloud.com", "proton.me",
                                  "protonmail.com", "aol.com"):
        return {"company": None, "domain": domain, "is_consumer_email": True}
    if domain in _ENRICH_CACHE:
        return _ENRICH_CACHE[domain]
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 1},
            timeout=8,
        )
        if r.status_code != 200:
            _ENRICH_CACHE[domain] = {}
            return {}
        d = (r.json() or {}).get("data") or {}
        out = {
            "company":  d.get("organization") or d.get("company"),
            "domain":   domain,
            "industry": d.get("industry"),
            "size":     d.get("company_size"),
            "country":  d.get("country"),
            "linkedin": d.get("linkedin"),
        }
        _ENRICH_CACHE[domain] = out
        return out
    except Exception as e:
        logger.debug("[crm_etl] hunter lookup failed for %s: %s", domain, e)
        return {}


# ── canonical capture_event ──────────────────────────────────────────

def capture_event(event_type: str, payload: dict) -> dict:
    """Called by hooks. payload may include: email, session_id, company,
    title, first_name, last_name, ...event-specific keys.

    Returns {ok: bool, queue_id?: int, dedup_skipped?: bool, error?: str}.

    Fail-soft: NEVER raises (so a CRM hiccup can't break the conversion
    flow that called us)."""
    if DISABLE:
        return {"ok": True, "skipped": "disabled"}
    if event_type not in (
        "mcp_high_intent", "state_visitor_high_intent",
        "newsletter_signup", "trial_key_activated", "paid_conversion",
    ):
        return {"ok": False, "error": "bad_event_type"}

    email = (payload.get("email") or "").strip().lower() or None
    sid = (payload.get("session_id") or "").strip() or None
    if not (email or sid):
        return {"ok": False, "error": "no_identifier"}

    c = _conn()
    if c is None:
        logger.info("[crm_etl] no DB; dropping capture event_type=%s", event_type)
        return {"ok": False, "error": "no_db"}
    try:
        _ensure_schema(c)
        # Build the attribution chain
        try:
            chain = _build_attribution(c, event_type, payload)
        except Exception as e:
            logger.warning("[crm_etl] attribution build failed: %s", e)
            chain = {"event_type": event_type,
                     "trigger_event": {"type": event_type, "ts": _now_iso()}}

        # Enrich company (best-effort)
        enrich = _enrich_company(email) if email else {}
        company = (payload.get("company")
                   or enrich.get("company")
                   or (chain.get("conversion") or {}).get("company"))

        score = _compute_intent_score(event_type, chain)

        # Insert / dedup
        try:
            with c.cursor() as cur:
                cur.execute(
                    """INSERT INTO crm_outbound_queue
                          (event_type, lead_email, lead_session_id,
                           lead_company, lead_title, lead_first_name,
                           lead_last_name, attribution_chain, intent_score,
                           status, captured_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                                'queued', (NOW() AT TIME ZONE 'UTC')::date)
                        ON CONFLICT (event_type, COALESCE(LOWER(lead_email),''),
                                     COALESCE(lead_session_id,''), captured_date)
                        DO NOTHING
                        RETURNING id""",
                    (event_type, email, sid, company,
                     payload.get("title"),
                     payload.get("first_name"),
                     payload.get("last_name"),
                     json.dumps(chain, default=str),
                     score),
                )
                r = cur.fetchone()
            c.commit()
            if not r:
                return {"ok": True, "dedup_skipped": True}
            qid = int(r[0])
            logger.info("[crm_etl] captured event=%s email=%s sid=%s score=%d qid=%d",
                        event_type, email, (sid or "")[:12], score, qid)
            return {"ok": True, "queue_id": qid, "score": score}
        except Exception as e:
            try: c.rollback()
            except Exception: pass
            logger.warning("[crm_etl] insert failed: %s", e)
            return {"ok": False, "error": str(e)[:200]}
    finally:
        _return(c)


# ── push functions ───────────────────────────────────────────────────

def push_to_salesforce(lead: dict) -> dict:
    """POST /services/data/v59.0/sobjects/Lead."""
    if requests is None:
        return {"ok": False, "error": "requests_missing"}
    if not (SF_INSTANCE_URL and SF_ACCESS_TOKEN):
        return {"ok": False, "error": "sf_creds_missing"}
    url = f"{SF_INSTANCE_URL}/services/data/v59.0/sobjects/Lead"
    # Map our internal lead → Salesforce Lead sObject
    email = lead.get("lead_email") or ""
    sf_payload = {
        "Email":     email,
        "FirstName": lead.get("lead_first_name") or (email.split("@")[0][:40] if email else "Unknown"),
        "LastName":  lead.get("lead_last_name") or (email.split("@")[0][:40] if email else "Lead"),
        "Company":   lead.get("lead_company") or "Unknown",
        "Title":     lead.get("lead_title") or "",
        "LeadSource": f"DC Hub · {lead.get('event_type')}",
        "Description": f"Intent score: {lead.get('intent_score')}. "
                       f"Attribution: {json.dumps(lead.get('attribution_chain'))[:1500]}",
    }
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {SF_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            data=json.dumps(sf_payload),
            timeout=12,
        )
        if r.status_code in (200, 201):
            j = r.json() or {}
            return {"ok": True, "external_id": j.get("id"),
                    "raw": j, "status_code": r.status_code}
        return {"ok": False, "error": f"sf {r.status_code}",
                "raw": (r.text or "")[:500], "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def push_to_hubspot(lead: dict) -> dict:
    """POST /crm/v3/objects/contacts."""
    if requests is None:
        return {"ok": False, "error": "requests_missing"}
    if not HUBSPOT_API_KEY:
        return {"ok": False, "error": "hs_creds_missing"}
    email = lead.get("lead_email") or ""
    if not email:
        return {"ok": False, "error": "no_email_for_hubspot"}
    hs_payload = {"properties": {
        "email":     email,
        "firstname": lead.get("lead_first_name") or "",
        "lastname":  lead.get("lead_last_name") or "",
        "company":   lead.get("lead_company") or "",
        "jobtitle":  lead.get("lead_title") or "",
        "lifecyclestage": "lead",
        "hs_lead_status": "NEW",
        "dchub_event_type":   lead.get("event_type"),
        "dchub_intent_score": str(lead.get("intent_score") or 0),
        "dchub_attribution":  json.dumps(lead.get("attribution_chain"))[:60000],
    }}
    try:
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers={
                "Authorization": f"Bearer {HUBSPOT_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps(hs_payload),
            timeout=12,
        )
        if r.status_code in (200, 201):
            j = r.json() or {}
            return {"ok": True, "external_id": j.get("id"),
                    "raw": j, "status_code": r.status_code}
        # HubSpot returns 409 on duplicate — treat as success (already in CRM)
        if r.status_code == 409:
            return {"ok": True, "external_id": None, "dup": True,
                    "raw": (r.text or "")[:500], "status_code": r.status_code}
        return {"ok": False, "error": f"hs {r.status_code}",
                "raw": (r.text or "")[:500], "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def push_to_stub(lead: dict) -> dict:
    """Stub: leave the row queued for manual export (CSV)."""
    return {"ok": True, "external_id": None, "stub": True}


def _dispatch_push(lead: dict) -> dict:
    if DRY_RUN:
        return {"ok": True, "external_id": None, "dry_run": True}
    if CRM_PROVIDER == "salesforce":
        return push_to_salesforce(lead)
    if CRM_PROVIDER == "hubspot":
        return push_to_hubspot(lead)
    return push_to_stub(lead)


# ── queue flush ──────────────────────────────────────────────────────

def flush_outbound_queue(limit: int = 100) -> dict:
    """Push queued rows to the configured CRM. Returns a summary.

    Idempotent: only picks status='queued' rows + bumps push_attempts.
    Rows with push_attempts >= 5 are skipped (marked status='failed')."""
    if DISABLE:
        return {"ok": True, "skipped": "disabled"}
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db"}
    pushed = 0
    failed = 0
    skipped = 0
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, event_type, lead_email, lead_session_id,
                          lead_company, lead_title, lead_first_name,
                          lead_last_name, attribution_chain, intent_score,
                          push_attempts
                     FROM crm_outbound_queue
                    WHERE status = 'queued'
                      AND push_attempts < 5
                    ORDER BY captured_at ASC LIMIT %s""", (limit,))
            rows = cur.fetchall() or []
        for r in rows:
            qid = int(r[0])
            lead = {
                "id":               qid,
                "event_type":       r[1],
                "lead_email":       r[2],
                "lead_session_id":  r[3],
                "lead_company":     r[4],
                "lead_title":       r[5],
                "lead_first_name":  r[6],
                "lead_last_name":   r[7],
                "attribution_chain": r[8],
                "intent_score":     int(r[9] or 0),
            }
            result = _dispatch_push(lead)
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            try:
                with c.cursor() as cur:
                    if result.get("ok"):
                        new_status = ("queued_export" if CRM_PROVIDER == "stub"
                                      else "pushed")
                        cur.execute(
                            """UPDATE crm_outbound_queue SET
                                   crm_pushed_at = NOW(),
                                   crm_provider  = %s,
                                   crm_external_id = %s,
                                   crm_response  = %s::jsonb,
                                   status        = %s,
                                   push_attempts = push_attempts + 1,
                                   last_error    = NULL
                                 WHERE id = %s""",
                            (CRM_PROVIDER, result.get("external_id"),
                             json.dumps(result, default=str),
                             new_status, qid))
                        pushed += 1
                    else:
                        attempts = int(r[10] or 0) + 1
                        new_status = "failed" if attempts >= 5 else "queued"
                        cur.execute(
                            """UPDATE crm_outbound_queue SET
                                   push_attempts = push_attempts + 1,
                                   last_error    = %s,
                                   status        = %s,
                                   crm_response  = %s::jsonb
                                 WHERE id = %s""",
                            ((result.get("error") or "")[:300], new_status,
                             json.dumps(result, default=str), qid))
                        failed += 1
                c.commit()
            except Exception as e:
                try: c.rollback()
                except Exception: pass
                logger.warning("[crm_etl] update qid=%s failed: %s", qid, e)
                failed += 1
        return {"ok": True, "pushed": pushed, "failed": failed,
                "skipped": skipped, "provider": CRM_PROVIDER,
                "dry_run": DRY_RUN}
    finally:
        _return(c)


# ── backfill (historical events) ─────────────────────────────────────

def backfill_last_n_days(days: int = 7) -> dict:
    """Walk the source tables and capture_event() any high-intent events
    from the last N days. Idempotent via the unique index.

    Returns counts per event_type."""
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db"}
    counts = {
        "mcp_high_intent": 0, "state_visitor_high_intent": 0,
        "newsletter_signup": 0, "trial_key_activated": 0,
        "paid_conversion": 0,
    }
    try:
        _ensure_schema(c)
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        # 1. MCP high-intent claims (claim_used_at is the conversion moment)
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT claim_email, mcp_session_id, claim_used_at
                         FROM mcp_high_intent_sessions
                        WHERE claim_used_at >= %s""", (cutoff,))
                for em, sid, ts in cur.fetchall():
                    capture_event("mcp_high_intent",
                                  {"email": em, "session_id": sid,
                                   "captured_at_hist": _iso(ts)})
                    counts["mcp_high_intent"] += 1
        except Exception as e:
            logger.warning("[crm_etl backfill] mcp HI failed: %s", e)

        # 2. State-of-2026 visitor HI
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT email, visitor_session_id, claim_used_at
                         FROM state_visitor_intent
                        WHERE claim_used_at >= %s""", (cutoff,))
                for em, sid, ts in cur.fetchall():
                    capture_event("state_visitor_high_intent",
                                  {"email": em, "session_id": sid,
                                   "captured_at_hist": _iso(ts)})
                    counts["state_visitor_high_intent"] += 1
        except Exception as e:
            logger.warning("[crm_etl backfill] state HI failed: %s", e)

        # 3. Newsletter subscribers
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT email, subscribed_at FROM newsletter_subscribers
                        WHERE subscribed_at >= %s
                          AND unsubscribed_at IS NULL""", (cutoff,))
                for em, ts in cur.fetchall():
                    capture_event("newsletter_signup",
                                  {"email": em,
                                   "captured_at_hist": _iso(ts)})
                    counts["newsletter_signup"] += 1
        except Exception as e:
            logger.warning("[crm_etl backfill] newsletter failed: %s", e)

        # 4. Trial keys
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT operator_email, minted_at FROM auto_trial_keys
                        WHERE minted_at >= %s
                          AND operator_email IS NOT NULL""", (cutoff,))
                for em, ts in cur.fetchall():
                    capture_event("trial_key_activated",
                                  {"email": em,
                                   "captured_at_hist": _iso(ts)})
                    counts["trial_key_activated"] += 1
        except Exception as e:
            logger.warning("[crm_etl backfill] trial keys failed: %s", e)

        # 5. Paid conversions (users with active subscription)
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT email, created_at, plan FROM users
                        WHERE created_at >= %s
                          AND subscription_status = 'active'
                          AND email IS NOT NULL""", (cutoff,))
                for em, ts, plan in cur.fetchall():
                    capture_event("paid_conversion",
                                  {"email": em, "plan": plan,
                                   "captured_at_hist": _iso(ts)})
                    counts["paid_conversion"] += 1
        except Exception as e:
            logger.warning("[crm_etl backfill] users failed: %s", e)
        return {"ok": True, "counts": counts, "days": days}
    finally:
        _return(c)


# ── flask endpoints (admin-gated) ───────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key")
            or request.args.get("key") or "").strip()
    return bool(DCHUB_ADMIN_KEY) and sent == DCHUB_ADMIN_KEY


@crm_reverse_etl_bp.route("/api/v1/admin/crm/health", methods=["GET"])
def admin_health():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _conn()
    counts = {}
    if c is not None:
        try:
            _ensure_schema(c)
            with c.cursor() as cur:
                cur.execute(
                    """SELECT status, COUNT(*) FROM crm_outbound_queue
                        GROUP BY status""")
                counts = {r[0]: int(r[1]) for r in cur.fetchall()}
        except Exception as e:
            logger.warning("[crm_etl] health failed: %s", e)
        finally:
            _return(c)
    return jsonify(
        ok=True,
        provider=CRM_PROVIDER,
        disable=DISABLE, dry_run=DRY_RUN,
        sf_configured=bool(SF_INSTANCE_URL and SF_ACCESS_TOKEN),
        hs_configured=bool(HUBSPOT_API_KEY),
        hunter_configured=bool(HUNTER_API_KEY),
        status_counts=counts,
    )


@crm_reverse_etl_bp.route("/api/v1/admin/crm/capture-recent", methods=["POST"])
def admin_capture_recent():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    days = int(request.args.get("days") or 7)
    days = max(1, min(90, days))
    result = backfill_last_n_days(days)
    return jsonify(result)


@crm_reverse_etl_bp.route("/api/v1/admin/crm/flush", methods=["POST"])
def admin_flush():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    limit = int(request.args.get("limit") or 100)
    limit = max(1, min(1000, limit))
    return jsonify(flush_outbound_queue(limit))


@crm_reverse_etl_bp.route("/api/v1/admin/crm/queue", methods=["GET"])
def admin_queue():
    """JSON: most recent N queue rows, optional ?status= filter."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    limit = int(request.args.get("limit") or 100)
    limit = max(1, min(500, limit))
    status_filter = (request.args.get("status") or "").strip()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    rows = []
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            sql = ("""SELECT id, event_type, captured_at, lead_email,
                             lead_session_id, lead_company, intent_score,
                             status, crm_pushed_at, crm_provider,
                             crm_external_id, push_attempts, last_error,
                             attribution_chain
                        FROM crm_outbound_queue""")
            params = []
            if status_filter:
                sql += " WHERE status = %s"
                params.append(status_filter)
            sql += " ORDER BY captured_at DESC LIMIT %s"
            params.append(limit)
            cur.execute(sql, tuple(params))
            for r in cur.fetchall():
                rows.append({
                    "id":               int(r[0]),
                    "event_type":       r[1],
                    "captured_at":      _iso(r[2]),
                    "lead_email":       r[3],
                    "lead_session_id":  r[4],
                    "lead_company":     r[5],
                    "intent_score":     int(r[6] or 0),
                    "status":           r[7],
                    "crm_pushed_at":    _iso(r[8]),
                    "crm_provider":     r[9],
                    "crm_external_id":  r[10],
                    "push_attempts":    int(r[11] or 0),
                    "last_error":       r[12],
                    "attribution_chain": r[13],
                })
    finally:
        _return(c)
    return jsonify(ok=True, count=len(rows), rows=rows,
                   provider=CRM_PROVIDER)


@crm_reverse_etl_bp.route("/api/v1/admin/crm/export.csv", methods=["GET"])
def admin_export_csv():
    """CSV dump of unsent queue rows (status='queued' or 'queued_export').

    For STUB mode: 1-click export to load into a CRM manually."""
    if not _admin_ok():
        return Response("unauthorized\n", status=401, mimetype="text/plain")
    c = _conn()
    if c is None:
        return Response("no_db\n", status=503, mimetype="text/plain")
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, event_type, captured_at, lead_email,
                          lead_company, lead_title, lead_first_name,
                          lead_last_name, intent_score, status,
                          attribution_chain
                     FROM crm_outbound_queue
                    WHERE status IN ('queued', 'queued_export')
                    ORDER BY captured_at DESC""")
            rows = cur.fetchall() or []
    finally:
        _return(c)
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "event_type", "captured_at", "email", "company",
                "title", "first_name", "last_name", "intent_score",
                "status", "attribution_chain_json"])
    for r in rows:
        chain_str = json.dumps(r[10], default=str) if r[10] else ""
        w.writerow([r[0], r[1], _iso(r[2]), r[3] or "", r[4] or "",
                    r[5] or "", r[6] or "", r[7] or "", r[8] or 0,
                    r[9] or "", chain_str])
    return Response(buf.getvalue(),
                    mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=crm_outbound_queue.csv"})


@crm_reverse_etl_bp.route("/admin/crm-outbound", methods=["GET"])
def admin_dashboard():
    """Admin dashboard. Shell renders without the key; ?key= preseeds it."""
    preseed = (request.args.get("key") or "").strip()
    pre_js = json.dumps(preseed) if preseed else "''"
    html = (_DASHBOARD_HTML
            .replace("__PRESEED_KEY__", pre_js)
            .replace("__PROVIDER__", CRM_PROVIDER)
            .replace("__DRY_RUN__", "true" if DRY_RUN else "false"))
    return Response(html, status=200, mimetype="text/html")


# ── helpers ──────────────────────────────────────────────────────────

def _iso(dt) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ── dashboard HTML ───────────────────────────────────────────────────

_DASHBOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>CRM Outbound · DC Hub Admin</title>
<style>
 body{margin:0;padding:24px;background:#0a0a0a;color:#e5e5e5;
      font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:14px}
 h1{font-size:22px;margin:0 0 16px;color:#10b981}
 .row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
 .pill{padding:6px 12px;border-radius:6px;background:#171717;border:1px solid #1f1f1f;
       font-size:12px}
 .pill b{color:#10b981}
 input[type=text]{background:#0f0f0f;border:1px solid #1f1f1f;color:#e5e5e5;
                  padding:8px 12px;border-radius:6px;font-size:13px;width:340px}
 button{background:#10b981;color:#0a0a0a;border:0;padding:8px 14px;
        border-radius:6px;font-weight:600;cursor:pointer;font-size:13px}
 button.ghost{background:#171717;color:#e5e5e5;border:1px solid #1f1f1f}
 button:hover{filter:brightness(1.1)}
 table{width:100%;border-collapse:collapse;font-size:12px;margin-top:14px}
 th{text-align:left;padding:8px;background:#171717;color:#a3a3a3;
    border-bottom:1px solid #1f1f1f;font-weight:600;font-size:11px;text-transform:uppercase}
 td{padding:8px;border-bottom:1px solid #171717;vertical-align:top}
 tr:hover td{background:#0f0f0f}
 .score{padding:2px 8px;border-radius:4px;font-weight:700;font-size:11px;display:inline-block}
 .s-hot{background:#dc2626;color:#fff}
 .s-warm{background:#f59e0b;color:#0a0a0a}
 .s-cool{background:#171717;color:#a3a3a3;border:1px solid #1f1f1f}
 .ev{padding:2px 6px;border-radius:4px;font-size:10px;text-transform:uppercase;letter-spacing:.5px}
 .ev-paid{background:#10b981;color:#0a0a0a}
 .ev-trial{background:#3b82f6;color:#fff}
 .ev-mcp{background:#8b5cf6;color:#fff}
 .ev-state{background:#f59e0b;color:#0a0a0a}
 .ev-news{background:#171717;color:#a3a3a3;border:1px solid #1f1f1f}
 .status{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#a3a3a3}
 .status.pushed{color:#10b981}
 .status.failed{color:#dc2626}
 details{margin:4px 0}
 details summary{cursor:pointer;color:#737373;font-size:11px}
 pre{background:#0f0f0f;border:1px solid #1f1f1f;padding:10px;border-radius:6px;
     font-size:11px;overflow-x:auto;color:#a3a3a3;margin:6px 0}
 .err{color:#dc2626;font-size:11px}
</style></head>
<body>
<h1>CRM Outbound Queue</h1>
<div class="row">
  <span class="pill">Provider: <b id="provider">__PROVIDER__</b></span>
  <span class="pill">Dry-run: <b id="dryrun">__DRY_RUN__</b></span>
  <span class="pill">Queued: <b id="cnt-queued">—</b></span>
  <span class="pill">Pushed: <b id="cnt-pushed">—</b></span>
  <span class="pill">Failed: <b id="cnt-failed">—</b></span>
</div>
<div class="row">
  <input type="text" id="key" placeholder="X-Admin-Key (DCHUB_ADMIN_KEY)" />
  <button onclick="reload()">Refresh</button>
  <button class="ghost" onclick="capture()">Backfill last 7d</button>
  <button class="ghost" onclick="flush()">Flush queue → CRM</button>
  <button class="ghost" onclick="dlCsv()">Download CSV (unsent)</button>
</div>
<div id="msg" style="color:#a3a3a3;font-size:12px;margin:8px 0"></div>
<table id="tbl">
 <thead><tr>
  <th>When</th><th>Event</th><th>Score</th><th>Email</th>
  <th>Company</th><th>Status</th><th>Attribution</th>
 </tr></thead>
 <tbody id="rows"></tbody>
</table>
<script>
const K = __PRESEED_KEY__;
if (K) document.getElementById("key").value = K;

async function api(path, opts={}){
  const k = document.getElementById("key").value.trim();
  if (!k){ alert("Admin key required."); return null; }
  const r = await fetch(path, Object.assign({}, opts,
    { headers: Object.assign({"X-Admin-Key": k}, (opts.headers||{})) }));
  return r;
}
async function reload(){
  const r1 = await api("/api/v1/admin/crm/health");
  if (!r1) return;
  const h = await r1.json();
  if (h.status_counts){
    document.getElementById("cnt-queued").textContent = h.status_counts.queued || 0;
    document.getElementById("cnt-pushed").textContent = h.status_counts.pushed || 0;
    document.getElementById("cnt-failed").textContent = h.status_counts.failed || 0;
  }
  const r2 = await api("/api/v1/admin/crm/queue?limit=200");
  if (!r2) return;
  const j = await r2.json();
  const t = document.getElementById("rows");
  t.innerHTML = "";
  (j.rows||[]).forEach(r=>{
    const tr = document.createElement("tr");
    const score = r.intent_score||0;
    const sc = score>=80 ? "s-hot" : score>=50 ? "s-warm" : "s-cool";
    const evCls = ({
      paid_conversion:"ev-paid", trial_key_activated:"ev-trial",
      mcp_high_intent:"ev-mcp", state_visitor_high_intent:"ev-state",
      newsletter_signup:"ev-news"
    })[r.event_type] || "ev-news";
    tr.innerHTML = `
      <td>${(r.captured_at||"").slice(0,19).replace("T"," ")}</td>
      <td><span class="ev ${evCls}">${(r.event_type||"").replace(/_/g," ")}</span></td>
      <td><span class="score ${sc}">${score}</span></td>
      <td>${r.lead_email||"<em style=color:#525252>—</em>"}</td>
      <td>${r.lead_company||"<em style=color:#525252>—</em>"}</td>
      <td><span class="status ${r.status}">${r.status}</span>${
        r.last_error ? `<div class="err">${r.last_error}</div>` : ""}</td>
      <td><details><summary>chain</summary><pre>${
        JSON.stringify(r.attribution_chain||{}, null, 2)}</pre></details></td>
    `;
    t.appendChild(tr);
  });
  document.getElementById("msg").textContent = `Showing ${j.count||0} rows · provider=${j.provider||"?"}`;
}
async function capture(){
  const r = await api("/api/v1/admin/crm/capture-recent?days=7", {method:"POST"});
  if (!r) return;
  const j = await r.json();
  document.getElementById("msg").textContent = "Backfill: " + JSON.stringify(j.counts||{});
  reload();
}
async function flush(){
  const r = await api("/api/v1/admin/crm/flush?limit=100", {method:"POST"});
  if (!r) return;
  const j = await r.json();
  document.getElementById("msg").textContent = `Flush: pushed=${j.pushed||0} failed=${j.failed||0} provider=${j.provider}`;
  reload();
}
function dlCsv(){
  const k = document.getElementById("key").value.trim();
  if (!k){ alert("Admin key required."); return; }
  window.location.href = "/api/v1/admin/crm/export.csv?key=" + encodeURIComponent(k);
}
if (K) reload();
</script>
</body></html>"""
