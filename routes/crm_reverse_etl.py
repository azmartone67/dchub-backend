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
import functools
import hashlib
import inspect
import re
import urllib.parse
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
# ★2026-09-20: .strip() — CRM_PROVIDER two lines up has always stripped and
# these never did. A credential pasted into a dashboard picks up a trailing
# newline or space more often than not, and it goes straight into
# f"Bearer {HUBSPOT_API_KEY}". HubSpot answered a real 401 with
# category=INVALID_AUTHENTICATION and "Authentication credentials not found":
# the request arrived, the header did not parse. hs_configured was True the
# whole time, because a non-empty string is not the same as a usable one.
SF_ACCESS_TOKEN = (os.environ.get("SALESFORCE_ACCESS_TOKEN") or "").strip()
HUBSPOT_API_KEY = (os.environ.get("HUBSPOT_API_KEY") or "").strip()
HUNTER_API_KEY = (os.environ.get("HUNTER_API_KEY") or "").strip()

# HubSpot private-app tokens are documented as `pat-`-prefixed. Checking the
# SHAPE costs nothing and turns "401, go guess" into a named problem — a legacy
# API key, a value pasted with its quotes, or the wrong variable entirely. Only
# the prefix is ever reported; the value is never logged or returned.
#
# ★ DERIVED AT CALL TIME, never cached. As a module constant this was a SECOND
#   source of truth about HUBSPOT_API_KEY, fixed at import — so it could not
#   follow the value it describes, and said "wrong shape" about a perfectly good
#   token supplied later. A sibling test caught it. One value, one reader.
def _hubspot_token_looks_valid() -> bool:
    return HUBSPOT_API_KEY.startswith("pat-")

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
-- 2026-09-21: the last flush each process ROLE ran, readable by every service.
CREATE TABLE IF NOT EXISTS crm_flush_last (
    role     TEXT PRIMARY KEY,
    ran_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    summary  JSONB NOT NULL
);
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
            try: c.rollback()
            except Exception: pass

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
            try: c.rollback()
            except Exception: pass

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
            try: c.rollback()
            except Exception: pass

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
            try: c.rollback()
            except Exception: pass

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
            try: c.rollback()
            except Exception: pass

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
            try: c.rollback()
            except Exception: pass

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
            params={"domain": domain, "limit": 1},
            headers={"Authorization": f"Bearer {HUNTER_API_KEY}"},
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

# ★2026-09-20 — THE LIFECYCLE STAGE WAS HARDCODED "lead" FOR EVERY EVENT.
#
# push_to_hubspot sent `"lifecyclestage": "lead"` and `"hs_lead_status": "NEW"`
# on every row. 24 of the 31 rows waiting to be pushed are `paid_conversion` —
# people who have PAID. Filing a customer at the top of the funnel misreports
# the funnel, and HubSpot deliberately resists walking a contact backwards once
# a stage is set, so the mistake is expensive to undo after the fact rather
# than before.
#
# The vocabulary is CLOSED — capture_event rejects anything outside this set —
# so the mapping is exhaustive by construction and
# test_lifecycle_covers_every_event_type asserts the two stay equal. A default
# would quietly re-file a new event type as a lead, which is the bug again.
CAPTURED_EVENT_TYPES = (
    "mcp_high_intent", "state_visitor_high_intent",
    "newsletter_signup", "trial_key_activated", "paid_conversion",
)

_LIFECYCLE_BY_EVENT = {
    "paid_conversion":           "customer",
    "trial_key_activated":       "salesqualifiedlead",
    "mcp_high_intent":           "marketingqualifiedlead",
    "state_visitor_high_intent": "marketingqualifiedlead",
    "newsletter_signup":         "subscriber",
}

# hs_lead_status is a SALES PROSPECTING field. "NEW" on someone who has already
# paid tells a rep to go work a lead who is a customer. Sent only for stages
# where it means something; omitted entirely otherwise (HubSpot leaves the
# property alone when the key is absent).
_NO_LEAD_STATUS = ("customer",)

# HubSpot's default lifecycle order. Through the API a stage only moves
# FORWARD — a backward move is refused unless the stage is cleared first — so on
# an existing contact ours is sent only when it is strictly ahead of the stored
# one: customer beats lead, never the reverse. 'other' and custom-stage ids have
# no place in this order, so a contact holding one is left alone, not guessed at.
_LIFECYCLE_ORDER = ("subscriber", "lead", "marketingqualifiedlead",
                    "salesqualifiedlead", "opportunity", "customer",
                    "evangelist")


def capture_event(event_type: str, payload: dict) -> dict:
    """Called by hooks. payload may include: email, session_id, company,
    title, first_name, last_name, ...event-specific keys.

    Returns {ok: bool, queue_id?: int, dedup_skipped?: bool, error?: str}.

    Fail-soft: NEVER raises (so a CRM hiccup can't break the conversion
    flow that called us)."""
    if DISABLE:
        return {"ok": True, "skipped": "disabled"}
    if event_type not in CAPTURED_EVENT_TYPES:
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
        # Build the attribution chain. Each sub-query inside is wrapped in
        # a savepoint so a missing/changed source table can't poison the
        # outer transaction and block the INSERT downstream.
        try:
            chain = _build_attribution(c, event_type, payload)
        except Exception as e:
            logger.warning("[crm_etl] attribution build failed: %s", e)
            chain = {"event_type": event_type,
                     "trigger_event": {"type": event_type, "ts": _now_iso()}}
        # Clear any aborted txn state from attribution queries before INSERT.
        try: c.rollback()
        except Exception: pass

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
                "raw": (r.text or "")[:500], "status_code": r.status_code,
                "codes": _provider_error_codes(r.text)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _hubspot_update_props(props: dict, current: dict) -> tuple:
    """(properties to PATCH onto an EXISTING contact, stored values kept).

    The create payload, minus what a create cannot hurt and an update can:
      - blanks: "" CLEARS a HubSpot property. Most rows carry no name or
        company, and sending them would wipe whatever the contact already has.
      - lifecyclestage, unless it moves the contact forward (_LIFECYCLE_ORDER).
      - hs_lead_status, unless the contact ends up at OUR stage (where the
        create would have set it) and has none yet. A rep's status is theirs,
        and NEW on a customer is what _NO_LEAD_STATUS exists to prevent."""
    out = {k: v for k, v in props.items() if v not in (None, "")}
    kept = {}
    ours = props.get("lifecyclestage") or ""
    have = (current.get("lifecyclestage") or "").strip()
    rank = {s: i for i, s in enumerate(_LIFECYCLE_ORDER)}
    forward = not have or (have in rank and ours in rank
                           and rank[ours] > rank[have])
    if not forward:
        out.pop("lifecyclestage", None)
        if have != ours:
            kept["lifecyclestage"] = have
    status_have = (current.get("hs_lead_status") or "").strip()
    at_our_stage = forward or have == ours
    if "hs_lead_status" in out and (status_have or not at_our_stage):
        out.pop("hs_lead_status")
        if status_have:
            kept["hs_lead_status"] = status_have
    return out, kept


def _hubspot_update_existing(email: str, props: dict, headers: dict) -> dict:
    """PATCH the contact keyed by email. ok only if the PATCH itself lands."""
    url = ("https://api.hubapi.com/crm/v3/objects/contacts/"
           + urllib.parse.quote(email, safe="") + "?idProperty=email")
    g = requests.get(url + "&properties=lifecyclestage,hs_lead_status",
                     headers=headers, timeout=12)
    if g.status_code != 200:
        return {"ok": False, "dup": True, "error": f"hs read {g.status_code}",
                "raw": (g.text or "")[:500], "status_code": g.status_code,
                "codes": _provider_error_codes(g.text)}
    current = (g.json() or {}).get("properties") or {}
    update, kept = _hubspot_update_props(props, current)
    p = requests.patch(url, headers=headers,
                       data=json.dumps({"properties": update}), timeout=12)
    if p.status_code == 200:
        j = p.json() or {}
        return {"ok": True, "external_id": j.get("id"), "dup": True,
                "updated": True, "kept": kept, "raw": j,
                "status_code": p.status_code}
    return {"ok": False, "dup": True, "error": f"hs patch {p.status_code}",
            "raw": (p.text or "")[:500], "status_code": p.status_code,
            "codes": _provider_error_codes(p.text)}


def push_to_hubspot(lead: dict) -> dict:
    """POST /crm/v3/objects/contacts; on 409, PATCH the existing contact."""
    if requests is None:
        return {"ok": False, "error": "requests_missing"}
    if not HUBSPOT_API_KEY:
        return {"ok": False, "error": "hs_creds_missing"}
    email = lead.get("lead_email") or ""
    if not email:
        return {"ok": False, "error": "no_email_for_hubspot"}
    _evt = lead.get("event_type") or ""
    _stage = _LIFECYCLE_BY_EVENT.get(_evt, "lead")
    hs_payload = {"properties": {
        "email":     email,
        "firstname": lead.get("lead_first_name") or "",
        "lastname":  lead.get("lead_last_name") or "",
        "company":   lead.get("lead_company") or "",
        "jobtitle":  lead.get("lead_title") or "",
        "lifecyclestage": _stage,
        "dchub_event_type":   lead.get("event_type"),
        "dchub_intent_score": str(lead.get("intent_score") or 0),
        "dchub_attribution":  json.dumps(lead.get("attribution_chain"))[:60000],
    }}
    if _stage not in _NO_LEAD_STATUS:
        hs_payload["properties"]["hs_lead_status"] = "NEW"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers=headers,
            data=json.dumps(hs_payload),
            timeout=12,
        )
        if r.status_code in (200, 201):
            j = r.json() or {}
            return {"ok": True, "external_id": j.get("id"),
                    "raw": j, "status_code": r.status_code}
        # 409 = the contact already exists (an import, an earlier row). That
        # is not delivery: this payload's stage, lead status and attribution
        # are exactly what the existing contact lacks. It used to return ok,
        # so the flusher marked the row pushed and the payload went nowhere.
        if r.status_code == 409:
            return _hubspot_update_existing(email, hs_payload["properties"],
                                            headers)
        return {"ok": False, "error": f"hs {r.status_code}",
                "raw": (r.text or "")[:500], "status_code": r.status_code,
                "codes": _provider_error_codes(r.text)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def push_to_stub(lead: dict) -> dict:
    """Stub: leave the row queued for manual export (CSV)."""
    return {"ok": True, "external_id": None, "stub": True}


def _destination_state() -> tuple:
    """(configured, gap) — is there a destination that _dispatch_push will
    ACTUALLY reach, and if not, what is missing?

    ★ 2026-09-20. This used to be one line:

        configured = bool((SF_INSTANCE_URL and SF_ACCESS_TOKEN) or HUBSPOT_API_KEY)

    It never looked at CRM_PROVIDER, and _dispatch_push switches on nothing
    else. So setting HUBSPOT_API_KEY alone flipped `destination_configured` to
    True and `stalled` to False — alarm off — while every lead still went to
    push_to_stub. Observed live the day it happened: hs_configured=True,
    provider='stub', stalled=False, 31 rows still queued. The old
    stalled_reason set the trap in words: "nothing will ever be pushed until
    HUBSPOT_API_KEY or the Salesforce pair is set". The key is necessary and it
    is not sufficient, and a half-configured destination is the one state where
    going quiet is worse than never having alarmed.

    Mirrors _dispatch_push exactly: DRY_RUN and the stub provider reach no
    destination, whatever credentials are lying around.
    """
    if DRY_RUN:
        return False, "DRY_RUN is on — pushes are simulated and nothing reaches a CRM"
    if CRM_PROVIDER == "hubspot":
        if not HUBSPOT_API_KEY:
            return False, "CRM_PROVIDER='hubspot' but HUBSPOT_API_KEY is empty"
        if not _hubspot_token_looks_valid():
            # Configured, but it will 401. Say so BEFORE a push burns an
            # attempt — the queue gives up on a row after 5.
            return False, ("HUBSPOT_API_KEY is set but does not start with "
                           "'pat-'. HubSpot private-app tokens do; a legacy "
                           "API key, a value pasted with quotes, or the wrong "
                           "variable will 401 with INVALID_AUTHENTICATION.")
        return True, ""
    if CRM_PROVIDER == "salesforce":
        if SF_INSTANCE_URL and SF_ACCESS_TOKEN:
            return True, ""
        return False, ("CRM_PROVIDER='salesforce' but SF_INSTANCE_URL/"
                       "SF_ACCESS_TOKEN are not both set")
    # provider is stub: say which credential is already sitting there unused,
    # because that is the state a reader is most likely to misread as done.
    have = []
    if HUBSPOT_API_KEY:
        have.append("HUBSPOT_API_KEY is set")
    if SF_INSTANCE_URL and SF_ACCESS_TOKEN:
        have.append("the Salesforce pair is set")
    if have:
        return False, (f"{' and '.join(have)}, but CRM_PROVIDER={CRM_PROVIDER!r} "
                       f"— _dispatch_push still routes to push_to_stub. Set "
                       f"CRM_PROVIDER=hubspot (or salesforce) to actually send.")
    return False, (f"CRM_PROVIDER={CRM_PROVIDER!r} and no credential is set — "
                   f"set BOTH CRM_PROVIDER and the matching credential")


def _dispatch_push(lead: dict) -> dict:
    if DRY_RUN:
        return {"ok": True, "external_id": None, "dry_run": True}
    if CRM_PROVIDER == "salesforce":
        return push_to_salesforce(lead)
    if CRM_PROVIDER == "hubspot":
        return push_to_hubspot(lead)
    return push_to_stub(lead)


# ── whose fault is a failed push? ────────────────────────────────────
#
# ★2026-09-21. The flush charged EVERY failure to the row — push_attempts + 1,
# and 'failed' (terminal: the SELECT never picks it again) at 5. But a 401
# (bad / legacy / quoted token), a 403 (missing scope) or a 400
# PROPERTY_DOESNT_EXIST (the portal never created dchub_event_type /
# dchub_intent_score / dchub_attribution) is the SAME answer for every row: it
# describes the destination, not the lead. The flush runs once a day (every
# crm_pushed_at is 07:0x UTC), so five days of a bad key fails the whole queue
# permanently, and nothing says so. Measured live 2026-09-21: 31 rows, 24 of
# them paid_conversion, one already at 3 of 5 on 'hs 401'.
#
# Only a failure that is the ROW's fault may spend the row's budget.
PUSH_FAIL_CONFIG = "config"        # the destination refused the request itself
PUSH_FAIL_TRANSIENT = "transient"  # no answer about this lead: 429, 5xx, network
PUSH_FAIL_LEAD = "lead"            # this row's data was refused — spends 1 attempt

# push_* results that name a missing prerequisite, never a lead.
_CONFIG_SENTINELS = frozenset({"requests_missing", "hs_creds_missing",
                               "sf_creds_missing"})
# Provider codes that describe the portal/org — its auth or its property schema.
# The payload shape is the same for every row, so these fail every row alike.
_CONFIG_ERROR_CODES = frozenset({
    # HubSpot
    "PROPERTY_DOESNT_EXIST", "INVALID_OPTION", "READ_ONLY_VALUE",
    "INVALID_AUTHENTICATION", "EXPIRED_AUTHENTICATION", "MISSING_SCOPES",
    # Salesforce
    "INVALID_FIELD", "INVALID_FIELD_FOR_INSERT_UPDATE", "INVALID_TYPE",
    "INVALID_SESSION_ID", "API_DISABLED_FOR_ORG",
})
_ERROR_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_STATUS_IN_ERROR_RE = re.compile(r"^(?:hs|sf) (\d{3})$")


def _provider_error_codes(text) -> list:
    """Every UPPER_SNAKE code in a provider error body, read from the FULL body.
    `raw` keeps 500 chars, and a HubSpot 400 lists one entry per bad property —
    a long INVALID_EMAIL entry can push PROPERTY_DOESNT_EXIST past the cut."""
    return sorted(set(_ERROR_CODE_RE.findall(text or "")))[:25]


def _push_failure_class(result) -> str:
    """PUSH_FAIL_CONFIG / _TRANSIENT / _LEAD for a failed push result.

    The ONE classifier: the flush calls it on the live result, and /crm/health
    and the requeue call it on the same dict as stored in crm_response (or on
    last_error alone, for rows written before crm_response was kept). So a row
    is judged on the same evidence wherever it is read.

    Anything unrecognised is LEAD — the pre-2026-09-21 behaviour — so this can
    only ever spend FEWER attempts than the old flush did, never more.
    """
    result = result if isinstance(result, dict) else {}
    err = str(result.get("error") or "").strip()
    if err in _CONFIG_SENTINELS:
        return PUSH_FAIL_CONFIG
    status = result.get("status_code")
    if status is None:
        m = _STATUS_IN_ERROR_RE.match(err)
        status = m.group(1) if m else None
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    codes = set(result.get("codes") or ())
    codes |= set(_ERROR_CODE_RE.findall(str(result.get("raw") or "")))
    # 404: a create POSTs to one fixed URL, so "not found" is the endpoint.
    # Not on the 409 path ("dup"): there the read and PATCH are keyed by THIS
    # row's email, so a 404 means that one contact did not resolve (e.g. the
    # address is only a secondary email). That is the row's answer — as
    # config it would retry forever and report the portal as refusing.
    by_email_404 = status == 404 and bool(result.get("dup"))
    if (status in (401, 403, 404) and not by_email_404) \
            or codes & _CONFIG_ERROR_CODES:
        return PUSH_FAIL_CONFIG
    if status == 429 or (status is not None and status >= 500):
        return PUSH_FAIL_TRANSIENT
    if status is None and err != "no_email_for_hubspot":
        # push_* caught an exception (timeout, DNS, reset): no answer came back
        # about this lead at all.
        return PUSH_FAIL_TRANSIENT
    return PUSH_FAIL_LEAD


def _stored_push_result(last_error, crm_response) -> dict:
    """The dict the flush stored for a row's last failed push, in the shape
    _push_failure_class reads. Legacy rows may have last_error and nothing else."""
    resp = crm_response
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except ValueError:
            resp = None
    out = dict(resp) if isinstance(resp, dict) else {}
    if not out.get("error"):
        out["error"] = last_error or ""
    return out


# ── queue flush ──────────────────────────────────────────────────────

# ★2026-09-20: ONE definition of "not yet delivered".
#
# The flusher selected `status = 'queued'`. The stub path parks rows in
# 'queued_export' (crawler_scheduler: "so the CSV export endpoint can vacuum
# them later"), and the CSV export reads BOTH. So every row accumulated while
# provider='stub' was invisible to the one job meant to drain it — permanently,
# not until some retry.
#
# Measured the day a real provider was finally configured: 31 rows, all
# 'queued_export', 24 of them paid_conversion, oldest 105 days. Setting
# CRM_PROVIDER=hubspot changed nothing; `flush?limit=1` returned
# {"ok": true, "pushed": 0, "failed": 0} and moved nothing. A green result
# that means "I looked at zero rows" is the shape this codebase keeps
# re-learning.
#
# 'queued_export' is not a terminal state — it means "parked because there was
# nowhere to send it". Once there is somewhere, it is sendable.
UNSENT_STATUSES = ("queued", "queued_export")


# ── which process flushed, and what did ITS env say? ─────────────────
#
# ★2026-09-21. /crm/health is served by dchub-backend (DCHUB_ROLE=web) and could
# only describe THAT process's CRM env. The scheduled flush never runs there:
# crawler_scheduler starts only where main._ROLE_RUNS_BG — dchub-worker — and
# the two services carry separate env. Measured the same day: health said
# provider 'hubspot', "does not start with 'pat-'"; the worker's 07:01Z flush
# logged "CRM_PROVIDER='stub' and no credential is set". Fixing the key on web
# alone would have turned health green while the worker kept skipping.
#
# So every flush records what it did and what its own env decided — one row
# per process role in crm_flush_last — and health reads the flusher's row next
# to its own view.

# Every role but 'web' runs the scheduler (main._ROLE_RUNS_BG); on Railway only
# DCHUB_ROLE=worker leads, so its row wins over an 'all' process's.
_FLUSHER_ROLES = ("worker", "all")


def _host_identity() -> dict:
    """Non-secret identity of this process: DCHUB_ROLE as main.py resolves it
    (unset means 'all'), plus Railway's service name and replica id."""
    return {
        "role": (os.environ.get("DCHUB_ROLE") or "").strip().lower() or "all",
        "service": (os.environ.get("RAILWAY_SERVICE_NAME") or "").strip() or None,
        "replica": (os.environ.get("RAILWAY_REPLICA_ID") or "").strip()[:8] or None,
    }


def _config_facts() -> dict:
    """What THIS process's CRM env decides, with nothing secret in it. Same keys
    on every service, so two services' answers compare field by field."""
    configured, gap = _destination_state()
    return {"provider": CRM_PROVIDER, "disable": DISABLE, "dry_run": DRY_RUN,
            "destination_configured": configured, "config_gap": gap}


def flush_slot_status(summary) -> str:
    """The dead-man status one flush earned (<= 40 chars; routes.ingest_runs
    reads anything outside _OK_STATUS as a failed run). The ONE classifier: the
    scheduler beats it, and /crm/health judges the flusher's last row with it.

    ★ _run_crm_outbound_flush never raises, so the guard beat 'success' for a
    flush that skipped — the public board read the job healthy while it
    delivered nothing.

    Stub is the default config, so "not configured" alone is not a failure:
    with nothing unsent there is nothing stranded, and it beats `skipped` (OK).
    With leads waiting — or a count that could not be taken — it is a stall.
    The kill switch is an operator's decision: `skipped`. Transient errors fail
    the run only when nothing got through; with pushes landing the destination
    works, and those rows retry next run."""
    s = summary if isinstance(summary, dict) else {}
    if s.get("error"):
        return f"error: {s['error']}"[:40]
    skipped = s.get("skipped")
    if skipped == "disabled":
        return "skipped"
    if skipped == "destination_not_configured":
        return ("skipped" if s.get("unsent") == 0
                else "stalled: destination_not_configured")
    if isinstance(skipped, str) or "pushed" not in s:
        return "error: unrecognised flush summary"
    if s.get("config_errors"):
        return "refused: destination config"
    if s.get("transient_errors") and not s.get("pushed"):
        return "degraded: transient push errors"
    return "success"


_FLUSH_LAST_UPSERT = """
    INSERT INTO crm_flush_last (role, ran_at, summary)
    VALUES (%s, NOW(), %s::jsonb)
    ON CONFLICT (role) DO UPDATE
       SET ran_at = EXCLUDED.ran_at, summary = EXCLUDED.summary"""


def _record_flush(summary: dict, trigger: str) -> dict | None:
    """Store one flush as this role's row: the summary, who ran it, what its env
    decided, and how many rows are still unsent afterwards. Returns the stored
    record, or None when it could not be stored. Never raises — the record
    describes the flush and must not be able to fail it."""
    rec = dict(summary or {})
    rec.update(trigger=trigger, host=_host_identity(), config=_config_facts())
    c = _conn()
    if c is None:
        return None
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM crm_outbound_queue
                    WHERE status = ANY(%s)""",
                (list(UNSENT_STATUSES),))
            row = cur.fetchone()
            rec["unsent"] = int(row[0]) if row and row[0] is not None else None
            cur.execute(_FLUSH_LAST_UPSERT,
                        (rec["host"]["role"], json.dumps(rec, default=str)))
        c.commit()
        return rec
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("[crm_etl] flush summary not recorded: %s", e)
        return None
    finally:
        _return(c)


def _read_last_flushes() -> dict:
    """role -> the last flush that role recorded, plus ran_at and age_hours.
    {} when unreadable: health must still answer."""
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT role, ran_at,
                          EXTRACT(EPOCH FROM (NOW() - ran_at))/3600.0,
                          summary
                     FROM crm_flush_last""")
            for role, ran_at, age_h, summary in cur.fetchall():
                rec = dict(summary if isinstance(summary, dict)
                           else json.loads(summary or "{}"))
                rec["ran_at"] = _iso(ran_at)
                rec["age_hours"] = (round(float(age_h), 1)
                                    if age_h is not None else None)
                out[role] = rec
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("[crm_etl] last flush unreadable: %s", e)
        return {}
    finally:
        _return(c)
    return out


def _who(host) -> str:
    """'dchub-worker[worker]' — service[role], the shape main.py stamps."""
    h = host if isinstance(host, dict) else {}
    return f"{h.get('service') or '?'}[{h.get('role') or '?'}]"


def _config_diff(here: dict, there) -> dict:
    """field -> both values, for every config fact the two processes disagree on."""
    there = there if isinstance(there, dict) else {}
    return {k: {"this_process": v, "flusher": there.get(k)}
            for k, v in here.items() if there.get(k) != v}


def _records_its_summary(flush):
    """Record EVERY exit of the flush — skip, refusal, no DB, a normal run, an
    exception. A record call per `return` is one new early return away from a
    silent gap.

    functools.wraps sets __wrapped__, so inspect.getsource() — which the AST
    guards on the flush read — still returns the flush's own body. The
    wrapper's real signature is pinned, or inspect.signature() would follow
    __wrapped__ too and hide `trigger`."""
    @functools.wraps(flush)
    def flush_and_record(limit: int = 100, trigger: str = "direct") -> dict:
        try:
            out = flush(limit)
        except Exception as e:
            _record_flush({"ok": False, "error": f"exception: {e}"[:200]}, trigger)
            raise
        rec = _record_flush(out, trigger)
        out["recorded"] = rec is not None
        if rec is not None:
            out["unsent"] = rec["unsent"]
        return out
    flush_and_record.__signature__ = inspect.signature(
        flush_and_record, follow_wrapped=False)
    return flush_and_record


@_records_its_summary
def flush_outbound_queue(limit: int = 100) -> dict:
    """Push undelivered rows to the configured CRM. Returns a summary.

    Picks rows in UNSENT_STATUSES. Only a PUSH_FAIL_LEAD failure spends one of
    a row's 5 attempts (status='failed' at 5). A config or transient failure is
    recorded in last_error / crm_response and spends nothing.

    Runs only when _destination_state() — the predicate /crm/health publishes
    as destination_configured — says a real destination will be reached.

    Called as flush_outbound_queue(limit, trigger=...): every exit is stored as
    this process role's row in crm_flush_last (see _records_its_summary), and
    the result gains `recorded` and `unsent`."""
    if DISABLE:
        return {"ok": True, "skipped": "disabled"}
    # ★2026-09-21: the flush used to check DISABLE and the DB, then push every
    # row. With HUBSPOT_API_KEY in a shape health already called unusable
    # ("does not start with 'pat-'"), each daily run spent one attempt on every
    # row. Same predicate as health — called, not restated — so the two cannot
    # disagree about whether pushing is worth an attempt. Not configured means
    # zero rows touched and a result that says why, never a green `pushed: 0`.
    configured, gap = _destination_state()
    if not configured:
        return {"ok": False, "skipped": "destination_not_configured",
                "reason": gap, "rows_touched": 0,
                "provider": CRM_PROVIDER, "dry_run": DRY_RUN}
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db"}
    pushed = 0
    failed = 0
    skipped = 0
    not_charged = {PUSH_FAIL_CONFIG: 0, PUSH_FAIL_TRANSIENT: 0}
    first_error = {}                       # class -> first error seen
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, event_type, lead_email, lead_session_id,
                          lead_company, lead_title, lead_first_name,
                          lead_last_name, attribution_chain, intent_score,
                          push_attempts
                     FROM crm_outbound_queue
                    WHERE status = ANY(%s)
                      AND push_attempts < 5
                    ORDER BY captured_at ASC LIMIT %s""",
                (list(UNSENT_STATUSES), limit))
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
            cls = None if result.get("ok") else _push_failure_class(result)
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
                    elif cls == PUSH_FAIL_LEAD:
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
                    else:
                        # Not this row's fault: record why, spend nothing, and
                        # leave status alone — it is still unsent.
                        cur.execute(
                            """UPDATE crm_outbound_queue SET
                                   last_error    = %s,
                                   crm_response  = %s::jsonb
                                 WHERE id = %s""",
                            ((result.get("error") or "")[:300],
                             json.dumps(result, default=str), qid))
                        not_charged[cls] += 1
                        codes = ",".join(result.get("codes") or [])
                        first_error.setdefault(
                            cls, f"{cls}: {result.get('error')}"
                                 + (f" [{codes}]" if codes else ""))
                c.commit()
            except Exception as e:
                try: c.rollback()
                except Exception: pass
                logger.warning("[crm_etl] update qid=%s failed: %s", qid, e)
                failed += 1
        out = {"ok": not any(not_charged.values()),
               "pushed": pushed, "failed": failed,
               "config_errors": not_charged[PUSH_FAIL_CONFIG],
               "transient_errors": not_charged[PUSH_FAIL_TRANSIENT],
               "skipped": skipped, "provider": CRM_PROVIDER,
               "dry_run": DRY_RUN}
        reason = (first_error.get(PUSH_FAIL_CONFIG)
                  or first_error.get(PUSH_FAIL_TRANSIENT))
        if reason:
            out["reason"] = (f"{reason} — not charged to any row; "
                             f"see /api/v1/admin/crm/health")
        return out
    finally:
        _return(c)


def requeue_config_failures(apply: bool = False) -> dict:
    """Give back the budget the old flush spent on configuration errors.

    Before 2026-09-21 a 401/403/PROPERTY_DOESNT_EXIST charged the row, so a
    status='failed' row may be failed for nothing it did. Selects every failed
    row, classifies its stored last push with _push_failure_class, and resets
    ONLY the PUSH_FAIL_CONFIG ones to status='queued', push_attempts=0
    (last_error and crm_response are kept as the record of what happened).

    Dry run unless apply=True. Either way the result names every row it
    would reset / did reset, and every failed row it is leaving alone."""
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db"}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, event_type, lead_email, push_attempts,
                          last_error, crm_response
                     FROM crm_outbound_queue
                    WHERE status = 'failed'
                    ORDER BY captured_at ASC""")
            rows = cur.fetchall() or []
        reset, kept = [], []
        for r in rows:
            cls = _push_failure_class(_stored_push_result(r[4], r[5]))
            item = {"id": int(r[0]), "event_type": r[1], "lead_email": r[2],
                    "push_attempts": int(r[3] or 0), "last_error": r[4],
                    "failure_class": cls}
            (reset if cls == PUSH_FAIL_CONFIG else kept).append(item)
        out = {"ok": True, "dry_run": not apply,
               "kept_failed": kept, "kept_failed_count": len(kept)}
        if not apply:
            out.update(would_requeue=reset, would_requeue_count=len(reset))
            return out
        n = 0
        if reset:
            with c.cursor() as cur:
                cur.execute(
                    """UPDATE crm_outbound_queue SET
                           status        = 'queued',
                           push_attempts = 0
                         WHERE id = ANY(%s)
                           AND status = 'failed'""",
                    ([x["id"] for x in reset],))
                n = int(cur.rowcount or 0)
            c.commit()
        out.update(requeued=reset, requeued_count=n)
        if n != len(reset):
            out["note"] = (f"{len(reset) - n} row(s) left status='failed' "
                           f"between the read and the write; not reset")
        return out
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return {"ok": False, "error": str(e)[:200]}
    finally:
        _return(c)


_EXISTING_ID_RE = re.compile(r"Existing ID: (\d+)")


def requeue_duplicates(apply: bool = False) -> dict:
    """Send again the rows HubSpot answered 409 for — the contact already existed.

    ★2026-09-21: push_to_hubspot counts a 409 as delivered and writes nothing
    onto the contact that is already there. The first real flush delivered 32
    rows and 15 were 409s (14 paid_conversion) against contacts from the 09-20
    CSV import, so those contacts never got dchub_event_type /
    dchub_intent_score / dchub_attribution or their lifecycle stage — and their
    rows say 'pushed', so no flush looks at them again.

    Selects status='pushed' rows whose stored result is a duplicate that was
    NOT updated (crm_response dup=true, no updated=true) and resets exactly
    those to status='queued', push_attempts=0, for the next flush to re-send.
    That writes our fields only if the running push_to_hubspot updates the
    existing contact on 409; otherwise HubSpot answers 409 again and the row
    goes back to 'pushed' as it was.

    Dry run unless apply=True. Either way the result names every row."""
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db"}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, event_type, lead_email, crm_pushed_at, crm_response
                     FROM crm_outbound_queue
                    WHERE status = 'pushed'
                    ORDER BY captured_at ASC""")
            rows = cur.fetchall() or []
        dups = []
        for r in rows:
            resp = _stored_push_result(None, r[4])
            if resp.get("dup") is not True or resp.get("updated"):
                continue
            found = _EXISTING_ID_RE.search(str(resp.get("raw") or ""))
            dups.append({"id": int(r[0]), "event_type": r[1], "lead_email": r[2],
                         "pushed_at": _iso(r[3]),
                         "existing_contact_id": found.group(1) if found else None})
        out = {"ok": True, "kind": "duplicates", "dry_run": not apply}
        if not apply:
            out.update(would_requeue=dups, would_requeue_count=len(dups))
            return out
        n = 0
        if dups:
            with c.cursor() as cur:
                cur.execute(
                    """UPDATE crm_outbound_queue SET
                           status        = 'queued',
                           push_attempts = 0
                         WHERE id = ANY(%s)
                           AND status = 'pushed'""",
                    ([x["id"] for x in dups],))
                n = int(cur.rowcount or 0)
            c.commit()
        out.update(requeued=dups, requeued_count=n)
        if n != len(dups):
            out["note"] = (f"{len(dups) - n} row(s) left status='pushed' "
                           f"between the read and the write; not reset")
        return out
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return {"ok": False, "error": str(e)[:200]}
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
    inserted = {k: 0 for k in counts}
    errors = []
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
                    r = capture_event("mcp_high_intent",
                                      {"email": em, "session_id": sid,
                                       "captured_at_hist": _iso(ts)})
                    counts["mcp_high_intent"] += 1
                    if r and r.get("queue_id"):
                        inserted["mcp_high_intent"] += 1
                    elif r and r.get("error"):
                        errors.append(("mcp_high_intent", r.get("error")))
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
                    r = capture_event("state_visitor_high_intent",
                                      {"email": em, "session_id": sid,
                                       "captured_at_hist": _iso(ts)})
                    counts["state_visitor_high_intent"] += 1
                    if r and r.get("queue_id"):
                        inserted["state_visitor_high_intent"] += 1
                    elif r and r.get("error"):
                        errors.append(("state_visitor_high_intent", r.get("error")))
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
                    r = capture_event("newsletter_signup",
                                      {"email": em,
                                       "captured_at_hist": _iso(ts)})
                    counts["newsletter_signup"] += 1
                    if r and r.get("queue_id"):
                        inserted["newsletter_signup"] += 1
                    elif r and r.get("error"):
                        errors.append(("newsletter_signup", r.get("error")))
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
                    r = capture_event("trial_key_activated",
                                      {"email": em,
                                       "captured_at_hist": _iso(ts)})
                    counts["trial_key_activated"] += 1
                    if r and r.get("queue_id"):
                        inserted["trial_key_activated"] += 1
                    elif r and r.get("error"):
                        errors.append(("trial_key_activated", r.get("error")))
        except Exception as e:
            logger.warning("[crm_etl backfill] trial keys failed: %s", e)

        # 5. Paid conversions (users with active subscription)
        # users.created_at is TEXT: `created_at >= %s` with a datetime raised
        # "text >= timestamp with time zone" and this step always counted 0
        # (swallowed below). Filter the window in Python (2026-09-25).
        try:
            from routes._users_created_at import parse_users_created_at
            with c.cursor() as cur:
                cur.execute(
                    """SELECT email, created_at, plan FROM users
                        WHERE subscription_status = 'active'
                          AND email IS NOT NULL""")
                for em, ts, plan in cur.fetchall():
                    ts = parse_users_created_at(ts)
                    if ts is None or ts < cutoff:
                        continue
                    r = capture_event("paid_conversion",
                                      {"email": em, "plan": plan,
                                       "captured_at_hist": _iso(ts)})
                    counts["paid_conversion"] += 1
                    if r and r.get("queue_id"):
                        inserted["paid_conversion"] += 1
                    elif r and r.get("error"):
                        errors.append(("paid_conversion", r.get("error")))
        except Exception as e:
            logger.warning("[crm_etl backfill] users failed: %s", e)
        return {"ok": True, "counts": counts,
                "inserted": inserted,
                "errors": errors[:10], "days": days}
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
    oldest_queued_age_h = None
    refused = {}            # last_error -> unsent rows whose last push was a config refusal
    failed_on_config = 0    # status='failed' rows requeue_config_failures would reset
    if c is not None:
        try:
            _ensure_schema(c)
            with c.cursor() as cur:
                cur.execute(
                    """SELECT status, COUNT(*) FROM crm_outbound_queue
                        GROUP BY status""")
                counts = {r[0]: int(r[1]) for r in cur.fetchall()}
                # r-truth (2026-08-19): a queue depth alone reads as "working
                # through a backlog". The age of the OLDEST unsent row is what
                # distinguishes that from "nothing has ever been pushed".
                cur.execute(
                    """SELECT EXTRACT(EPOCH FROM (NOW() - MIN(captured_at)))/3600.0
                         FROM crm_outbound_queue
                        WHERE status = ANY(%s)""",
                    (list(UNSENT_STATUSES),))
                row = cur.fetchone()
                if row and row[0] is not None:
                    oldest_queued_age_h = round(float(row[0]), 1)
                # ★2026-09-21: destination_configured checks the credential's
                # SHAPE. A pat- token that is revoked, or a portal missing the
                # dchub_* properties, passes it and still refuses every push.
                # The flush no longer charges those rows — so without this they
                # would sit unsent with nothing to say why.
                cur.execute(
                    """SELECT status, last_error, crm_response
                         FROM crm_outbound_queue
                        WHERE last_error IS NOT NULL
                          AND status = ANY(%s)""",
                    (list(UNSENT_STATUSES) + ["failed"],))
                for st, err, resp in cur.fetchall():
                    if _push_failure_class(
                            _stored_push_result(err, resp)) != PUSH_FAIL_CONFIG:
                        continue
                    if st == "failed":
                        failed_on_config += 1
                    else:
                        refused[err] = refused.get(err, 0) + 1
        except Exception as e:
            logger.warning("[crm_etl] health failed: %s", e)
        finally:
            _return(c)
    here = _config_facts()
    configured, config_gap = here["destination_configured"], here["config_gap"]
    me = _host_identity()
    # ★2026-09-21: this process is usually web, which never runs the scheduled
    # flush. Judge the FLUSHER's last run — with the same classifier its slot
    # beats — and say where its env differs from ours.
    flushes = _read_last_flushes()
    last_flush = next((dict(flushes[r]) for r in _FLUSHER_ROLES
                       if r in flushes), None)
    diff = {}
    if last_flush is not None:
        last_flush["slot_status"] = flush_slot_status(last_flush)
        diff = _config_diff(here, last_flush.get("config"))
    queued = sum(v for k, v in counts.items() if k in UNSENT_STATUSES)
    if queued and last_flush is not None and last_flush["slot_status"] != "success":
        why = (last_flush.get("reason") or last_flush.get("error")
               or f"skipped={last_flush.get('skipped')}")
        stalled_reason = (
            f"{queued} lead(s) queued and the flusher is not delivering them: "
            f"its last run, on {_who(last_flush.get('host'))} at "
            f"{last_flush.get('ran_at')} ({last_flush.get('age_hours')}h ago), "
            f"was {last_flush['slot_status']!r} — {why}")
        if diff:
            stalled_reason += (
                f". This process ({_who(me)}) has different CRM env "
                f"({', '.join(sorted(diff))}), so its own "
                f"destination_configured={configured} does not describe the "
                f"flusher — set the env where the flush runs.")
    elif queued and last_flush is None and not configured:
        # No flush recorded by a scheduler role yet: our own env is all we have.
        stalled_reason = (f"{queued} lead(s) queued and nothing will push "
                          f"them: {config_gap}")
    elif refused:
        stalled_reason = (
            f"{queued} lead(s) queued; the destination refused the last push "
            f"of {sum(refused.values())} for a configuration reason "
            f"({', '.join(sorted(refused))}). Those rows spend no attempts "
            f"while it lasts — fix the credential or the portal's properties "
            f"and the next flush sends them.")
    else:
        stalled_reason = None
    return jsonify(
        ok=True,
        provider=CRM_PROVIDER,
        disable=DISABLE, dry_run=DRY_RUN,
        sf_configured=bool(SF_INSTANCE_URL and SF_ACCESS_TOKEN),
        hs_configured=bool(HUBSPOT_API_KEY),
        hunter_configured=bool(HUNTER_API_KEY),
        status_counts=counts,
        oldest_queued_age_hours=oldest_queued_age_h,
        # ★ Say the quiet part. With provider='stub' and no destination
        # configured, every capture is written to a table nobody drains — the
        # leads look captured and go nowhere. rob@hedmarkholdings.com's
        # paid_conversion row sat status='queued', push_attempts=0, from the
        # moment he paid. A health endpoint that reports ok:true while that is
        # true is not reporting health.
        destination_configured=configured,
        stalled=bool(stalled_reason),
        stalled_reason=stalled_reason,
        config_gap=config_gap,
        # destination_configured / config_gap / provider describe THIS process
        # (this_process). The scheduled flush runs where last_flush says, and
        # `stalled` follows that row whenever one is recorded.
        this_process=me,
        last_flush=last_flush,
        last_flush_by_role=flushes,
        flusher_config_mismatch=(bool(diff) if last_flush is not None else None),
        flusher_config_diff=diff,
        destination_refusing=bool(refused),
        config_errors={
            "unsent_rows": sum(refused.values()),
            "by_error": refused,
            "failed_rows_requeueable": failed_on_config,
            "requeue": ("POST /api/v1/admin/crm/requeue-config-failures "
                        "(dry run unless ?apply=1)") if failed_on_config else None,
        },
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
    return jsonify(flush_outbound_queue(limit, trigger="admin"))


@crm_reverse_etl_bp.route("/api/v1/admin/crm/requeue-config-failures",
                          methods=["POST"])
def admin_requeue_config_failures():
    """Dry run by default. ?kind=config (the default) lists the failed rows
    whose last push failed for a configuration reason; ?kind=duplicates lists
    the pushed rows HubSpot answered 409 for (see requeue_duplicates).
    ?apply=1 resets exactly the rows listed to status='queued'."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    apply = (request.args.get("apply") or "").strip().lower() in ("1", "true", "yes")
    kind = (request.args.get("kind") or "config").strip().lower()
    requeue = {"config": requeue_config_failures,
               "duplicates": requeue_duplicates}.get(kind)
    if requeue is None:
        return jsonify(ok=False, error=f"unknown kind {kind!r}",
                       kinds=["config", "duplicates"]), 400
    out = requeue(apply=apply)
    out.setdefault("kind", kind)
    return jsonify(out), (200 if out.get("ok") else 503)


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
                             attribution_chain, crm_response
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
                    # ★2026-09-20: crm_response was WRITTEN on every failure and
                    # SELECTed by nothing. last_error keeps only "hs 401" — the
                    # status code — while the provider's own explanation sat in
                    # this column, unreachable. Diagnosing the first real push
                    # took a hand-written SQL query against Neon to read a field
                    # the API already had.
                    "crm_response":     r[14],
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
                    WHERE status = ANY(%s)
                    ORDER BY captured_at DESC""", (list(UNSENT_STATUSES),))
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
  document.getElementById("msg").textContent = (j.ok && typeof j.skipped !== "string")
    ? `Flush: pushed=${j.pushed||0} failed=${j.failed||0} provider=${j.provider}`
    : `Flush did NOT deliver (${j.skipped || j.error || "destination refused"}): ${j.reason || ""}`;
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
