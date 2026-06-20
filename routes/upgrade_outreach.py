"""
routes/upgrade_outreach.py — DRAFT-AND-APPROVE upgrade OUTREACH engine (2026-06-19).

The admin CUSTOMER-PORTAL companion: an outreach engine where every email is
DRAFTED by the brain and APPROVED by the operator before it can EVER send. This
is the deliberately-weaker "self-healing not self-directing" posture, applied to
customer outreach: the brain proposes, the human decides.

WHY THIS SHAPE
==============
Billing (users.plan) and the MCP paywall (mcp_dev_keys.tier) track DIFFERENT,
nearly-disjoint populations — the reconciliation we verified shows ~32 paid-plan
accounts but only ~5 real-invoice payers and ~18 paid MCP keys, so ~30 paid-plan
accounts have NO paid key. The four segments below each express a SPECIFIC,
honest reason to reach out, computed from real account data (the same predicate
logic as routes/brain_investigator._gather_paid_reconciliation):

  · keyless_payer    — pays a real invoice but has no paid MCP key (activation
                       gap). CHANNEL=transactional ("you pay — activate the key
                       you're entitled to"). Sendable without marketing consent.
  · comped_paid      — on a paid plan with ZERO real invoices (founding/dev
                       grants). CHANNEL=marketing (convert comp -> real before it
                       lapses). Requires marketing_opt_in + suppression-clear.
  · high_usage_free  — free/none plan, high MCP usage in 30d, AND opted-in.
                       CHANNEL=marketing (free -> paid). Candidate selection
                       MUST filter to opted-in only.
  · at_risk_paid     — real-invoice payer with low/declining recent usage.
                       CHANNEL=transactional (customer-success check-in).

HARD SAFETY RAILS (do not relax)
================================
  1. DRAFT-ONLY. The brain GENERATES drafts; NOTHING sends unless (a) the
     operator clicks approve on that draft AND (b) OUTREACH_SEND_ENABLED is on.
     Generation itself is gated by OUTREACH_GENERATE_ENABLED (LLM cost). BOTH
     default OFF — this module ships DARK.
  2. CONSENT / CAN-SPAM. MARKETING-channel segments (comped_paid,
     high_usage_free) send ONLY through the marketing consent CHOKE-POINT
     routes/marketing_send.send_marketing_email (enforces marketing_opt_in
     default-false + email_suppression + List-Unsubscribe). We NEVER hand-roll a
     Resend POST for marketing. high_usage_free candidate selection filters to
     opted-in users at query time. TRANSACTIONAL segments (keyless_payer,
     at_risk_paid) send via email_fallback.send_email_resilient and STILL honor
     email_suppression before sending.
  3. ADMIN-GATED + READ-ONLY portal. Reuse the brain admin gate (X-Admin-Key /
     X-Internal-Key header OR ?admin_key=). The ONLY writes are to the
     outreach_drafts table. Served under /api/ so the CF worker passes it
     through (/brain/* and bare subpaths hit CF Error-1000).

Endpoints (blueprint upgrade_outreach_bp, ALL admin-gated):
  POST /api/v1/portal/outreach/generate   {segment, max?}  — flag-gated draft gen
  GET  /api/v1/portal/outreach?status=&segment=&limit=     — the review queue
  POST /api/v1/portal/outreach/<id>/approve                — approve [+ send if on]
  POST /api/v1/portal/outreach/<id>/skip                   — skip a draft
  POST /api/v1/portal/outreach/<id>/edit   {subject,body}  — edit before approve

Degrades gracefully with NO DB / NO ANTHROPIC_API_KEY — never raises out of an
endpoint; every model/DB/send touch is wrapped in try/except.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

upgrade_outreach_bp = Blueprint("upgrade_outreach", __name__)


# ── env / flags ──────────────────────────────────────────────────────
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _generate_enabled() -> bool:
    """Gate on LLM cost — draft GENERATION is dark until an operator opts in."""
    return _truthy(os.environ.get("OUTREACH_GENERATE_ENABLED"))


def _send_enabled() -> bool:
    """Gate on actual delivery — even an APPROVED draft only sends when this is
    on. Approve with this OFF parks the draft as 'approved_pending_send'."""
    return _truthy(os.environ.get("OUTREACH_SEND_ENABLED"))


def _gen_cap() -> int:
    """Hard per-run draft cap (defends LLM budget even if a caller asks for
    more). Configurable via OUTREACH_GENERATE_CAP; default 25."""
    try:
        n = int(os.environ.get("OUTREACH_GENERATE_CAP") or "25")
        return n if n > 0 else 25
    except Exception:
        return 25


# Segment -> channel. Single source of truth used by selection AND send routing.
_SEGMENTS = {
    "keyless_payer": "transactional",
    "comped_paid": "marketing",
    "high_usage_free": "marketing",
    "at_risk_paid": "transactional",
}

# Mirror brain_investigator._PAID_PLANS so billing predicates stay aligned.
_PAID_PLANS = ("pro", "founding", "enterprise", "pro_annual", "developer")
_PAID_PLANS_SQL = "('pro','founding','enterprise','pro_annual','developer')"

# high_usage_free threshold: MCP calls in the last 30 days to count as "high".
def _high_usage_threshold() -> int:
    try:
        n = int(os.environ.get("OUTREACH_HIGH_USAGE_30D") or "40")
        return n if n > 0 else 40
    except Exception:
        return 40


_FROM_EMAIL = os.environ.get("DCHUB_OUTREACH_FROM", "alerts@dchub.cloud")
_FROM_NAME = os.environ.get("DCHUB_OUTREACH_FROM_NAME", "DC Hub")


# ── Admin gate (reuse the brain dashboard gate, per the brief) ───────
def _admin_ok() -> bool:
    """Reuse brain_investigator._admin_ok so the gate stays in one place
    (X-Admin-Key / X-Internal-Key header OR ?admin_key=). Falls back to an
    inline internal-key check if that import fails so endpoints are never
    accidentally left open."""
    try:
        from routes.brain_investigator import _admin_ok as _brain_admin_ok
        return bool(_brain_admin_ok())
    except Exception:
        keys = set()
        for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            v = os.environ.get(_n)
            if v:
                keys.add(v)
        sent = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
        return bool(sent) and sent in keys


# ── DB (direct psycopg2, NOT safe_db — DDL is SKIP'd under safe_db) ──
def _conn():
    """Raw psycopg2 connection. Mirrors brain_investigator._conn."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("upgrade_outreach: _conn failed: %s", e)
    return None


def init_outreach_schema() -> None:
    """Bootstrap outreach_drafts via DIRECT psycopg2 (safe_db SKIPs DDL under
    SKIP_DDL=1). Idempotent; never raises."""
    conn = _conn()
    if conn is None:
        logger.warning("upgrade_outreach: no DB; skipping schema init")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS outreach_drafts (
                        id          BIGSERIAL PRIMARY KEY,
                        email       TEXT NOT NULL,
                        segment     TEXT NOT NULL,
                        channel     TEXT NOT NULL,
                        subject     TEXT,
                        body        TEXT,
                        status      TEXT NOT NULL DEFAULT 'drafted',
                        context     JSONB,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        approved_at TIMESTAMPTZ,
                        sent_at     TIMESTAMPTZ,
                        error       TEXT
                    )
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            for ddl in (
                "CREATE INDEX IF NOT EXISTS ix_outreach_drafts_status "
                "ON outreach_drafts (status, segment, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS ix_outreach_drafts_email "
                "ON outreach_drafts (email)",
                "CREATE INDEX IF NOT EXISTS ix_outreach_drafts_created "
                "ON outreach_drafts (created_at DESC)",
            ):
                try:
                    cur.execute(ddl)
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
        logger.info("upgrade_outreach: schema ready")
    finally:
        try: conn.close()
        except Exception: pass


# ── Honest-numbers fence (reuse the brain's banned-figure scanner) ───
def _fence_fabricated_figures(text: str) -> list[str]:
    """Reuse brain_investigator._fence_fabricated_figures so a draft can never
    cite a banned figure (50,000 facilities, $324B, 340+ markets, 96+ platforms).
    Falls back to a no-op (never raises) if that import fails."""
    try:
        from routes.brain_investigator import _fence_fabricated_figures as _f
        return _f(text or "")
    except Exception:
        return []


# ── Step 1: SELECT candidates (the 4 segment predicates) ─────────────
def select_candidates(segment: str, limit: int = 25) -> list[dict]:
    """Return [{email, segment, channel, context}] for a segment, computed from
    REAL account data using the verified reconciliation predicates. Read-only.
    Best-effort: returns [] on any error (never raises).

    SAFETY: high_usage_free is filtered to marketing-opted-in users ONLY at the
    QUERY level (mcp_dev_keys.metadata->>'marketing_opt_in' = 'true'), so a
    non-opted-in free user is NEVER even a candidate for a marketing draft."""
    seg = (segment or "").strip()
    channel = _SEGMENTS.get(seg)
    if not channel:
        return []
    try:
        limit = max(1, min(int(limit or 25), _gen_cap()))
    except Exception:
        limit = 25

    conn = _conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            if seg == "keyless_payer":
                return _select_keyless_payer(cur, channel, limit)
            if seg == "comped_paid":
                return _select_comped_paid(cur, channel, limit)
            if seg == "high_usage_free":
                return _select_high_usage_free(cur, channel, limit)
            if seg == "at_risk_paid":
                return _select_at_risk_paid(cur, channel, limit)
            return []
    except Exception as e:
        logger.warning("upgrade_outreach: select_candidates(%s) failed: %s", seg, e)
        try: conn.rollback()
        except Exception: pass
        return []
    finally:
        try: conn.close()
        except Exception: pass


def _rows(cur, sql, params=()):
    """Run a query, return rows; rollback + [] on error so one bad probe never
    aborts the surrounding transaction."""
    try:
        cur.execute(sql, params)
        return cur.fetchall() or []
    except Exception as e:
        logger.warning("upgrade_outreach: query failed: %s", e)
        try: cur.connection.rollback()
        except Exception: pass
        return []


def _select_keyless_payer(cur, channel, limit) -> list[dict]:
    """users.plan IN paid AND invoices_paid_count>0 AND (no active paid/ent key).
    TRANSACTIONAL: a real payer who hasn't activated the key they're entitled to."""
    rows = _rows(cur,
        "SELECT u.email, u.name, u.plan, COALESCE(u.invoices_paid_count,0) "
        "FROM users u "
        "LEFT JOIN mcp_dev_keys k "
        "  ON lower(k.email)=lower(u.email) "
        " AND COALESCE(k.status,'active')='active' "
        " AND k.tier IN ('paid','enterprise') "
        f"WHERE u.plan IN {_PAID_PLANS_SQL} "
        "  AND COALESCE(u.invoices_paid_count,0) > 0 "
        "  AND k.api_key IS NULL "
        "ORDER BY COALESCE(u.invoices_paid_count,0) DESC "
        "LIMIT %s", (limit,))
    return [{
        "email": (r[0] or "").strip().lower(),
        "segment": "keyless_payer",
        "channel": channel,
        "context": {"name": r[1], "plan": r[2], "invoices_paid_count": int(r[3] or 0),
                    "reason": "Pays a real invoice but has no active paid MCP key "
                              "(activation gap)."},
    } for r in rows if r and r[0]]


def _select_comped_paid(cur, channel, limit) -> list[dict]:
    """users.plan IN paid AND invoices_paid_count=0 (founding/dev grants).
    MARKETING: requires marketing_opt_in + suppression-clear (enforced at query)."""
    rows = _rows(cur,
        "SELECT u.email, u.name, u.plan "
        "FROM users u "
        f"WHERE u.plan IN {_PAID_PLANS_SQL} "
        "  AND COALESCE(u.invoices_paid_count,0) = 0 "
        # MARKETING consent + suppression enforced at the query level:
        "  AND EXISTS (SELECT 1 FROM mcp_dev_keys k "
        "              WHERE lower(k.email)=lower(u.email) "
        "                AND COALESCE(k.status,'active')='active' "
        "                AND k.metadata->>'marketing_opt_in' = 'true') "
        "  AND NOT EXISTS (SELECT 1 FROM email_suppression s "
        "                  WHERE lower(s.email)=lower(u.email)) "
        "ORDER BY u.created_at DESC "
        "LIMIT %s", (limit,))
    return [{
        "email": (r[0] or "").strip().lower(),
        "segment": "comped_paid",
        "channel": channel,
        "context": {"name": r[1], "plan": r[2],
                    "reason": "On a paid plan via a comp/grant with zero real "
                              "invoices — convert to real paid before it lapses."},
    } for r in rows if r and r[0]]


def _select_high_usage_free(cur, channel, limit) -> list[dict]:
    """plan free/none AND MCP calls in 30d > threshold AND marketing_opt_in.
    MARKETING: candidate selection is filtered to OPTED-IN users ONLY (a hard
    consent gate, not just a send-time check). Usage is attributed via
    mcp_call_log.api_key -> mcp_dev_keys.api_key -> mcp_dev_keys.email."""
    threshold = _high_usage_threshold()
    rows = _rows(cur,
        "WITH usage AS ("
        "  SELECT k.email AS email, COUNT(*) AS calls_30d "
        "  FROM mcp_call_log c "
        "  JOIN mcp_dev_keys k ON c.api_key = k.api_key "
        "  WHERE c.timestamp >= NOW() - INTERVAL '30 days' "
        "    AND COALESCE(k.status,'active')='active' "
        "    AND k.metadata->>'marketing_opt_in' = 'true' "      # OPT-IN ONLY
        "  GROUP BY k.email "
        "  HAVING COUNT(*) > %s "
        ") "
        "SELECT u.email, u.name, u.plan, usage.calls_30d "
        "FROM usage "
        "JOIN users u ON lower(u.email)=lower(usage.email) "
        "WHERE COALESCE(u.plan,'free') IN ('free','none','') "
        "  AND NOT EXISTS (SELECT 1 FROM email_suppression s "
        "                  WHERE lower(s.email)=lower(usage.email)) "
        "ORDER BY usage.calls_30d DESC "
        "LIMIT %s", (threshold, limit))
    return [{
        "email": (r[0] or "").strip().lower(),
        "segment": "high_usage_free",
        "channel": channel,
        "context": {"name": r[1], "plan": r[2], "calls_30d": int(r[3] or 0),
                    "reason": "Heavy free-tier MCP usage (%d calls/30d) and "
                              "opted in to marketing — strong free->paid "
                              "candidate." % int(r[3] or 0)},
    } for r in rows if r and r[0]]


def _select_at_risk_paid(cur, channel, limit) -> list[dict]:
    """users.plan IN paid AND invoices_paid_count>0 AND low/declining usage
    (no MCP call in 14d). TRANSACTIONAL: a customer-success check-in."""
    rows = _rows(cur,
        "SELECT u.email, u.name, u.plan, "
        "       (SELECT MAX(c.timestamp) FROM mcp_call_log c "
        "        JOIN mcp_dev_keys k ON c.api_key=k.api_key "
        "        WHERE lower(k.email)=lower(u.email)) AS last_call "
        "FROM users u "
        f"WHERE u.plan IN {_PAID_PLANS_SQL} "
        "  AND COALESCE(u.invoices_paid_count,0) > 0 "
        "  AND NOT EXISTS ("
        "      SELECT 1 FROM mcp_call_log c "
        "      JOIN mcp_dev_keys k ON c.api_key=k.api_key "
        "      WHERE lower(k.email)=lower(u.email) "
        "        AND c.timestamp >= NOW() - INTERVAL '14 days') "
        "ORDER BY COALESCE(u.invoices_paid_count,0) DESC "
        "LIMIT %s", (limit,))
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        last_call = r[3]
        try: last_call = last_call.isoformat() if last_call else None
        except Exception: last_call = str(last_call) if last_call else None
        out.append({
            "email": (r[0] or "").strip().lower(),
            "segment": "at_risk_paid",
            "channel": channel,
            "context": {"name": r[1], "plan": r[2], "last_call": last_call,
                        "reason": "Real-invoice payer with no MCP activity in 14+ "
                                  "days — at-risk; customer-success check-in."},
        })
    return out


# ── Step 2: GENERATE drafts (brain LLM, flag-gated, capped) ──────────
def _call_model(system: str, prompt: str, *, tier: str = "routine",
                max_tokens: int = 700):
    """Reuse the brain's model infra (brain_investigator._call_model -> tier
    selection + resolve_chain fallback). Returns (text, error, model). Degrades
    to (None, 'no_caller', None) if the helper is unavailable. Never raises."""
    try:
        from routes.brain_investigator import _call_model as _bm
        return _bm(system, prompt, tier=tier, max_tokens=max_tokens)
    except Exception as e:
        return None, f"no_caller:{str(e)[:80]}", None


_DRAFT_SYSTEM = (
    "You are the DC Hub outreach writer. DC Hub (https://dchub.cloud) is a live "
    "data-center infrastructure intelligence platform with an MCP server for AI "
    "agents. Write ONE short, honest, personal upgrade/activation email to a "
    "specific account, grounded ONLY in the REAL account facts provided.\n\n"
    "HARD RULES:\n"
    "  - Be SHORT: a subject line + 3-5 sentence body. Warm, direct, no hype.\n"
    "  - Ground the email in the SPECIFIC reason provided (their plan, their "
    "usage, the activation gap, etc). Do NOT invent facts, numbers, names, or "
    "features. If you don't have a number, don't cite one.\n"
    "  - NEVER cite these banned figures: 50,000 facilities, $324B deal value, "
    "340+ markets, 96+ AI platforms. (Real canon: ~21,000 facilities, ~178 "
    "countries, ~232-300 markets, ~2,032 deals — only mention if relevant.)\n"
    "  - Plain language. One clear call to action. No subject-line emoji.\n\n"
    "Output STRICTLY a JSON object, no prose outside it:\n"
    "  {\"subject\": \"<subject line>\", \"body\": \"<plain-text email body>\"}\n"
)

_CHANNEL_FRAMING = {
    "transactional": (
        "CHANNEL: transactional / service. This is NOT a promo — it's helping "
        "them get what they already pay for, or checking in. Tone: helpful, "
        "low-key, no marketing push."
    ),
    "marketing": (
        "CHANNEL: marketing. They have opted in. You may make a clear upgrade "
        "ask, but stay honest and respectful — no pressure, no fake scarcity."
    ),
}


def _draft_prompt(cand: dict) -> str:
    ctx = cand.get("context") or {}
    facts = {
        "email": cand.get("email"),
        "name": ctx.get("name"),
        "plan": ctx.get("plan"),
        "segment": cand.get("segment"),
        "reason_to_reach_out": ctx.get("reason"),
    }
    for k in ("invoices_paid_count", "calls_30d", "last_call"):
        if ctx.get(k) is not None:
            facts[k] = ctx.get(k)
    return (
        _CHANNEL_FRAMING.get(cand.get("channel"), "") + "\n\n"
        "REAL ACCOUNT FACTS (the ONLY facts you may use):\n"
        + json.dumps(facts, default=str) + "\n\n"
        "Write the email now."
    )


def _parse_draft(text: str) -> Optional[dict]:
    """Pull {subject, body} out of a model response (tolerates ```json fences)."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except Exception:
        return None
    subj = (obj.get("subject") or "").strip()
    body = (obj.get("body") or "").strip()
    if not subj or not body:
        return None
    return {"subject": subj[:200], "body": body}


def generate_drafts(segment: str, max_n: int = 10) -> dict:
    """For each candidate in `segment`, draft a tailored email via the brain LLM
    and STORE it in outreach_drafts (status='drafted'). Returns a summary dict.

    GATED: returns {enabled: False, drafts: 0} WITHOUT calling the model when
    OUTREACH_GENERATE_ENABLED is off. Capped at OUTREACH_GENERATE_CAP. Honest-
    numbers fence runs on every draft; a fabricated figure flags the draft as
    'flagged' (operator reviews) rather than silently shipping. Never raises."""
    seg = (segment or "").strip()
    if seg not in _SEGMENTS:
        return {"ok": False, "error": "unknown_segment", "segment": seg}
    if not _generate_enabled():
        # Short-circuit BEFORE any model call — ships dark.
        return {"ok": True, "enabled": False, "drafts": 0,
                "note": "OUTREACH_GENERATE_ENABLED is off — generation ships "
                        "dark. Set it to 1 to enable."}

    try:
        cap = min(int(max_n or 10), _gen_cap())
    except Exception:
        cap = 10
    cap = max(1, cap)

    candidates = select_candidates(seg, limit=cap)
    if not candidates:
        return {"ok": True, "enabled": True, "segment": seg, "candidates": 0,
                "drafts": 0, "note": "no candidates matched this segment"}

    channel = _SEGMENTS[seg]
    drafted = 0
    flagged = 0
    errors = 0
    conn = _conn()
    if conn is None:
        return {"ok": False, "error": "no_db", "segment": seg}
    try:
        with conn.cursor() as cur:
            for cand in candidates[:cap]:
                text, err, _model = _call_model(
                    _DRAFT_SYSTEM, _draft_prompt(cand),
                    tier="routine", max_tokens=700)
                draft = _parse_draft(text) if text else None
                if not draft:
                    errors += 1
                    continue
                # Honest-numbers fence — a banned figure flags for review.
                fence_hits = _fence_fabricated_figures(
                    draft["subject"] + "\n" + draft["body"])
                status = "flagged" if fence_hits else "drafted"
                ctx = dict(cand.get("context") or {})
                if fence_hits:
                    ctx["fence_hits"] = fence_hits
                    flagged += 1
                else:
                    drafted += 1
                try:
                    cur.execute(
                        "INSERT INTO outreach_drafts "
                        "(email, segment, channel, subject, body, status, context) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (cand["email"], seg, channel, draft["subject"],
                         draft["body"], status, json.dumps(ctx, default=str)),
                    )
                    conn.commit()
                except Exception as e:
                    errors += 1
                    logger.warning("upgrade_outreach: draft insert failed: %s", e)
                    try: conn.rollback()
                    except Exception: pass
        return {"ok": True, "enabled": True, "segment": seg, "channel": channel,
                "candidates": len(candidates), "drafts": drafted,
                "flagged": flagged, "errors": errors}
    except Exception as e:
        logger.warning("upgrade_outreach: generate_drafts failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return {"ok": False, "error": "generate_failed", "segment": seg}
    finally:
        try: conn.close()
        except Exception: pass


# ── Review-queue reads + draft mutations ─────────────────────────────
def list_drafts(status: Optional[str] = None, segment: Optional[str] = None,
                limit: int = 100) -> list[dict]:
    """The operator review queue. Read-only; [] on any error."""
    try:
        limit = max(1, min(int(limit or 100), 500))
    except Exception:
        limit = 100
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            where, params = [], []
            if status:
                where.append("status = %s"); params.append(str(status)[:32])
            if segment:
                where.append("segment = %s"); params.append(str(segment)[:32])
            sql = ("SELECT id, email, segment, channel, subject, body, status, "
                   "context, created_at, approved_at, sent_at, error "
                   "FROM outreach_drafts")
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)
            rows = _rows(cur, sql, tuple(params))
            return [_row_to_draft(r) for r in rows]
    except Exception as e:
        logger.warning("upgrade_outreach: list_drafts failed: %s", e)
        return []
    finally:
        try: conn.close()
        except Exception: pass


def _row_to_draft(r) -> dict:
    def _iso(v):
        try: return v.isoformat() if v else None
        except Exception: return str(v) if v else None
    ctx = r[7]
    if isinstance(ctx, str):
        try: ctx = json.loads(ctx)
        except Exception: ctx = None
    return {
        "id": r[0], "email": r[1], "segment": r[2], "channel": r[3],
        "subject": r[4], "body": r[5], "status": r[6], "context": ctx,
        "created_at": _iso(r[8]), "approved_at": _iso(r[9]),
        "sent_at": _iso(r[10]), "error": r[11],
    }


def _get_draft(cur, draft_id: int) -> Optional[dict]:
    rows = _rows(cur,
        "SELECT id, email, segment, channel, subject, body, status, context, "
        "created_at, approved_at, sent_at, error "
        "FROM outreach_drafts WHERE id = %s", (int(draft_id),))
    return _row_to_draft(rows[0]) if rows else None


def edit_draft(draft_id: int, subject: Optional[str], body: Optional[str]) -> Optional[dict]:
    """Operator edits a draft's subject/body BEFORE approval. Cannot edit a
    sent draft. Returns the updated draft, or None."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            d = _get_draft(cur, draft_id)
            if d is None:
                return None
            if d["status"] == "sent":
                return d  # immutable once sent
            sets, params = [], []
            if subject is not None:
                sets.append("subject = %s"); params.append(str(subject)[:200])
            if body is not None:
                sets.append("body = %s"); params.append(str(body))
            if not sets:
                return d
            params.append(int(draft_id))
            cur.execute("UPDATE outreach_drafts SET " + ", ".join(sets)
                        + " WHERE id = %s", tuple(params))
            conn.commit()
            return _get_draft(cur, draft_id)
    except Exception as e:
        logger.warning("upgrade_outreach: edit_draft failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


def skip_draft(draft_id: int) -> Optional[dict]:
    """Operator skips a draft (status='skipped'). Cannot skip a sent draft."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            d = _get_draft(cur, draft_id)
            if d is None:
                return None
            if d["status"] == "sent":
                return d
            cur.execute("UPDATE outreach_drafts SET status='skipped' WHERE id=%s",
                        (int(draft_id),))
            conn.commit()
            return _get_draft(cur, draft_id)
    except Exception as e:
        logger.warning("upgrade_outreach: skip_draft failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


# ── SEND (only reachable via approve, only when OUTREACH_SEND_ENABLED) ─
def _suppressed(cur, email: str) -> bool:
    """email_suppression check (honored for BOTH channels). Reuses the canonical
    is_suppressed (fail-open: a check error returns False, never blocks)."""
    try:
        from routes.email_suppression import is_suppressed
        return bool(is_suppressed(cur, email))
    except Exception:
        return False


def _send_draft(draft: dict) -> dict:
    """Route a single approved draft to its channel's sender. Returns
    {sent: bool, status: str, reason: str|None}. Never raises.

    MARKETING channel goes through routes.marketing_send.send_marketing_email —
    the consent + suppression + List-Unsubscribe choke-point. We NEVER hand-roll
    a Resend POST for marketing. TRANSACTIONAL channel goes through
    email_fallback.send_email_resilient AFTER an explicit suppression check."""
    email = (draft.get("email") or "").strip().lower()
    subject = draft.get("subject") or ""
    body = draft.get("body") or ""
    channel = draft.get("channel")
    # Body is plain text from the model; wrap minimally so HTML senders render it.
    html = "<div>" + (body or "").replace("\n", "<br>") + "</div>"

    if channel == "marketing":
        # MARKETING — the choke-point enforces opt-in/suppression/unsubscribe.
        try:
            from routes.marketing_send import send_marketing_email
        except Exception as e:
            return {"sent": False, "status": "error",
                    "reason": "marketing_chokepoint_unavailable:" + str(e)[:80]}
        try:
            r = send_marketing_email(
                to_email=email, subject=subject, html=html,
                source="upgrade_outreach:" + str(draft.get("segment") or ""),
                to_name=(draft.get("context") or {}).get("name"),
                from_email=_FROM_EMAIL, from_name=_FROM_NAME)
        except Exception as e:
            return {"sent": False, "status": "error",
                    "reason": "marketing_send_raised:" + str(e)[:80]}
        if r.get("suppressed"):
            return {"sent": False, "status": "suppressed", "reason": "suppressed"}
        if r.get("sent"):
            return {"sent": True, "status": "sent", "reason": None}
        return {"sent": False, "status": "error", "reason": r.get("reason")}

    # TRANSACTIONAL — suppression check, then the resilient transactional sender.
    conn = _conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                if _suppressed(cur, email):
                    return {"sent": False, "status": "suppressed",
                            "reason": "suppressed"}
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
    try:
        from email_fallback import send_email_resilient
    except Exception as e:
        return {"sent": False, "status": "error",
                "reason": "transactional_sender_unavailable:" + str(e)[:80]}
    try:
        ok = send_email_resilient(
            to_email=email, subject=subject, html_content=html,
            text_content=body, from_email=_FROM_EMAIL, from_name=_FROM_NAME)
    except Exception as e:
        return {"sent": False, "status": "error",
                "reason": "transactional_send_raised:" + str(e)[:80]}
    if ok:
        return {"sent": True, "status": "sent", "reason": None}
    return {"sent": False, "status": "error", "reason": "send_failed"}


def approve_draft(draft_id: int) -> Optional[dict]:
    """Operator approves a draft. ALWAYS stamps approved_at. THEN:
      · if OUTREACH_SEND_ENABLED is OFF -> status='approved_pending_send'; NO
        sender is called.
      · if ON -> route to the channel sender; on success status='sent' +
        sent_at; on suppression status='suppressed'; on failure status='error'.
    Idempotent-ish: an already-sent draft is returned unchanged. Never raises."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            d = _get_draft(cur, draft_id)
            if d is None:
                return None
            if d["status"] == "sent":
                return d  # never re-send
            # Stamp approval first (durable even if the send leg fails).
            try:
                cur.execute(
                    "UPDATE outreach_drafts SET approved_at = NOW() WHERE id = %s",
                    (int(draft_id),))
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass

            if not _send_enabled():
                # Parked: approved but the global send flag is off.
                cur.execute(
                    "UPDATE outreach_drafts SET status='approved_pending_send' "
                    "WHERE id = %s", (int(draft_id),))
                conn.commit()
                return _get_draft(cur, draft_id)

            # Send-flag ON — route through the channel sender.
            res = _send_draft(d)
            if res.get("sent"):
                cur.execute(
                    "UPDATE outreach_drafts SET status='sent', sent_at=NOW(), "
                    "error=NULL WHERE id = %s", (int(draft_id),))
            elif res.get("status") == "suppressed":
                cur.execute(
                    "UPDATE outreach_drafts SET status='suppressed', "
                    "error='suppressed' WHERE id = %s", (int(draft_id),))
            else:
                cur.execute(
                    "UPDATE outreach_drafts SET status='error', error=%s "
                    "WHERE id = %s",
                    (str(res.get("reason") or "send_failed")[:300], int(draft_id)))
            conn.commit()
            return _get_draft(cur, draft_id)
    except Exception as e:
        logger.warning("upgrade_outreach: approve_draft failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


# ── Endpoints (ALL admin-gated) ──────────────────────────────────────
@upgrade_outreach_bp.post("/api/v1/portal/outreach/generate")
def ep_generate():
    """Generate drafts for a segment. Flag-gated (OUTREACH_GENERATE_ENABLED):
    when off, returns {enabled:false, drafts:0} WITHOUT any model call."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403
    body = request.get_json(silent=True) or {}
    segment = (body.get("segment") or "").strip()
    if segment not in _SEGMENTS:
        return jsonify(ok=False, error="unknown_segment",
                       valid=sorted(_SEGMENTS.keys())), 400
    try:
        max_n = int(body.get("max") or 10)
    except Exception:
        max_n = 10
    result = generate_drafts(segment, max_n=max_n)
    code = 200 if result.get("ok") else 400
    return jsonify(**result), code


@upgrade_outreach_bp.get("/api/v1/portal/outreach")
def ep_queue():
    """The review queue. Optional ?status= & ?segment= & ?limit= filters."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    status = (request.args.get("status") or "").strip() or None
    segment = (request.args.get("segment") or "").strip() or None
    try:
        limit = int(request.args.get("limit") or 100)
    except Exception:
        limit = 100
    drafts = list_drafts(status=status, segment=segment, limit=limit)
    return jsonify(ok=True, count=len(drafts), drafts=drafts,
                   flags={"generate_enabled": _generate_enabled(),
                          "send_enabled": _send_enabled()}), 200


@upgrade_outreach_bp.post("/api/v1/portal/outreach/<int:draft_id>/approve")
def ep_approve(draft_id):
    """Approve a draft. Sends ONLY if OUTREACH_SEND_ENABLED is on; otherwise
    parks it as approved_pending_send (sender never called)."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    d = approve_draft(draft_id)
    if d is None:
        return jsonify(ok=False, error="not_found_or_db_error"), 404
    return jsonify(ok=True, draft=d, send_enabled=_send_enabled()), 200


@upgrade_outreach_bp.post("/api/v1/portal/outreach/<int:draft_id>/skip")
def ep_skip(draft_id):
    """Skip a draft (status='skipped')."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    d = skip_draft(draft_id)
    if d is None:
        return jsonify(ok=False, error="not_found_or_db_error"), 404
    return jsonify(ok=True, draft=d), 200


@upgrade_outreach_bp.post("/api/v1/portal/outreach/<int:draft_id>/edit")
def ep_edit(draft_id):
    """Edit a draft's subject/body before approval."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    body = request.get_json(silent=True) or {}
    subject = body.get("subject")
    new_body = body.get("body")
    if subject is None and new_body is None:
        return jsonify(ok=False, error="subject or body required"), 400
    d = edit_draft(draft_id, subject, new_body)
    if d is None:
        return jsonify(ok=False, error="not_found_or_db_error"), 404
    return jsonify(ok=True, draft=d), 200


def register_upgrade_outreach(app) -> None:
    """Idempotent registration helper for main.py. Best-effort schema init."""
    try:
        init_outreach_schema()
    except Exception as e:
        logger.warning("upgrade_outreach: schema init skipped: %s", e)
    try:
        app.register_blueprint(upgrade_outreach_bp)
    except Exception as e:
        logger.warning("upgrade_outreach already registered: %s", e)
