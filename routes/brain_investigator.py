"""
routes/brain_investigator.py — Brain Investigator (2026-06-19). RECOMMEND-ONLY.

The VALUABLE rung. The brain has spent its life as a self-directed janitor:
detectors emit findings, Layer 4/5 attack them, the autopilot opens DRAFT PRs.
This module makes it a THINKING PROBLEM-SOLVING RESOURCE — a human poses a real
business question ("why is reach flat?", "is this customer churning?", "what
should we build next?") and the brain runs a VERIFIED investigation and returns
an evidence-backed recommendation.

It NEVER acts on the recommendation. This is rung-4 institutionalized: the SAME
verify -> track -> adapt -> honest loop, applied to OPEN business questions
instead of code bugs. The brain analyzes; the HUMAN decides and acts.

THE 5-STEP CHAIN (what makes it trustworthy vs confidently-wrong):
  1. DECOMPOSE   — break the question into sub-questions + name the data needed.
  2. GATHER      — pull REAL evidence from a CURATED set of brain data sources
                   (canonical_stats + HEALTH_BASELINE + recent brain findings +
                   honest GROWTH-FUNNEL metrics: MRR, conversions, paid keys,
                   reach as DISTINCT external IPs, retention — via the vetted
                   funnel_health + ai_reach aggregators so the loop-inflation /
                   internal-traffic traps are handled upstream). Every cited
                   figure is grounded in a real source; the model is handed
                   these numbers, it does NOT get to invent one.
  3. REASON      — the model reasons FROM the gathered evidence to a draft
                   hypothesis / recommendation.
  4. REFUTE      — a SEPARATE model pass that tries to BREAK the draft
                   recommendation (adversarial). Its findings fold into the final
                   confidence + caveats. This is the anti-overconfidence step.
  5. SYNTHESIZE  — a final recommendation with CONFIDENCE + CAVEATS + the
                   DECISION-FOR-THE-HUMAN surfaced explicitly.

A post-hoc honest-numbers fence scans every cited figure against the canon
(deals ~2,032, countries ~178, DCPI ~232-300, 21k facilities; banned: 50,000,
$324B) and flags fabrications — a fabricated number lowers confidence and adds a
caveat rather than being silently published.

SAFETY:
  · RECOMMEND-ONLY — produces analysis, NEVER acts (no merges, no sends, no
    writes beyond storing the investigation row).
  · BRAIN_INVESTIGATOR_ENABLED (default OFF) — ships dark. POST /ask returns
    {enabled: false} WITHOUT calling a model when the flag is off.
  · Degrades gracefully with NO ANTHROPIC_API_KEY — returns cannot_investigate,
    never crashes. Every LLM/DB touch is wrapped in try/except.
  · Admin-gated endpoints (reuse brain_mechanical_classifier._admin_ok).

Endpoints (blueprint brain_investigator_bp, admin-gated):
  POST /api/v1/brain/investigate              {question} -> runs investigate, stores,
                                       returns {id, result}. Flag-gated.
  GET  /api/v1/brain/investigate/<id>         -> the stored investigation.
  POST /api/v1/brain/investigate/<id>/grade  {grade} -> records a human grade for
                                       CALIBRATION (the verify->learn loop).

The KEY-based retention cohort summary for ops is served by
routes/retention_cohorts.py at GET /api/v1/mcp/retention/cohorts (the canonical
analyzer). gather_retention_cohorts() here is the brain's GATHER-step Source 6
that reads de-looped, COUNT(DISTINCT api_key) cohort evidence.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, jsonify, request

from utils.anthropic_helper import anthropic_messages_url
from util.json_column import json_for_column

logger = logging.getLogger(__name__)

brain_investigator_bp = Blueprint("brain_investigator", __name__)


# ── env / flags ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """The investigator ships DARK. Default OFF so it can't burn API budget
    or surface half-baked analysis until an operator flips the flag."""
    return _truthy(os.environ.get("BRAIN_INVESTIGATOR_ENABLED"))


# ── Admin gate (reuse the mechanical classifier's, per the brief) ────
def _admin_ok() -> bool:
    """Reuse brain_mechanical_classifier._admin_ok so the gate stays in one
    place. Falls back to an inline internal-key check if that import fails so
    the endpoints are never accidentally left open."""
    try:
        from routes.brain_mechanical_classifier import _admin_ok as _mech_admin_ok
        return bool(_mech_admin_ok())
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


# ── DB (direct psycopg2, NOT safe_db — DDL is SKIP'd under safe_db) ───
def _conn():
    """Raw psycopg2 connection. Mirrors brain_feature_proposer._conn — the
    _iso_common contextmanager crashes on .cursor()."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_investigator: _conn failed: %s", e)
    return None


def init_investigator_schema() -> None:
    """Bootstrap brain_investigations via DIRECT psycopg2 (safe_db SKIPs DDL
    under SKIP_DDL=1). Idempotent; never raises."""
    conn = _conn()
    if conn is None:
        logger.warning("brain_investigator: no DB; skipping schema init")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS brain_investigations (
                        id          BIGSERIAL PRIMARY KEY,
                        question    TEXT NOT NULL,
                        result_json JSONB,
                        confidence  DOUBLE PRECISION,
                        grade       TEXT,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_brain_investigations_created "
                    "ON brain_investigations (created_at DESC)"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
        logger.info("brain_investigator: schema ready")
    finally:
        try: conn.close()
        except Exception: pass


# ── Honest-numbers fence ─────────────────────────────────────────────
# Numbers the brain must NEVER publish (the canon banned them). A cited figure
# matching one of these in the model's output is a FABRICATION -> flag it.
_BANNED_FIGURES = [
    re.compile(r"\b50[,.]?000\b"),         # the inflated facility count
    re.compile(r"\$\s?324\s?B", re.I),     # uncomputable deal-value claim
    re.compile(r"\b324\s?billion\b", re.I),
    re.compile(r"\b340\+?\s+markets\b", re.I),  # DCPI over-claim
    re.compile(r"\b96\+?\s+(?:ai\s+)?platforms\b", re.I),
]


def _fence_fabricated_figures(text: str) -> list[str]:
    """Return human-readable reasons for every banned figure that appears in
    `text` (the model's synthesized recommendation). Empty = clean."""
    hits: list[str] = []
    t = text or ""
    if _BANNED_FIGURES[0].search(t):
        hits.append("cites '50,000' facilities (banned — real is ~21,000 tracked)")
    if _BANNED_FIGURES[1].search(t) or _BANNED_FIGURES[2].search(t):
        hits.append("cites '$324B' deal value (banned — uncomputable; value_usd sparse)")
    if _BANNED_FIGURES[3].search(t):
        hits.append("cites '340+ markets' (banned — DCPI is ~232-300)")
    if _BANNED_FIGURES[4].search(t):
        hits.append("cites '96+ AI platforms' (banned — real is Claude + Cursor)")
    return hits


# ── Step 2: GATHER curated, REAL evidence ────────────────────────────
def gather_evidence() -> list[dict]:
    """Pull GROUND-TRUTH figures from a CURATED set of brain data sources so the
    reasoning + refutation passes reason over REAL numbers, not the model's
    guesses. Each item is {claim, source, value}. Best-effort: any source that
    errors is simply omitted — never raises."""
    evidence: list[dict] = []

    # Source 1: canonical_stats (the honest-numbers source of truth).
    try:
        from canonical_stats import get_canonical_stats
        s = get_canonical_stats() or {}
        if s.get("facilities"):
            evidence.append({
                "claim": "Tracked data-center facilities (discovery pile)",
                "source": "canonical_stats.discovered_facilities COUNT(*)",
                "value": int(s["facilities"]),
            })
        if s.get("facilities_verified"):
            evidence.append({
                "claim": "Verified/active facilities (deduped)",
                "source": "canonical_stats (COALESCE(is_duplicate,0)=0 fleet filter; issue #1539 dropped merged_at)",
                "value": int(s["facilities_verified"]),
            })
        if s.get("countries"):
            evidence.append({
                "claim": "Distinct countries with facilities",
                "source": "canonical_stats COUNT(DISTINCT country)",
                "value": int(s["countries"]),
            })
        if s.get("markets"):
            evidence.append({
                "claim": "DCPI power markets tracked",
                "source": "canonical_stats market_power_scores",
                "value": int(s["markets"]),
            })
        if s.get("isos"):
            evidence.append({
                "claim": "Live US ISOs with grid telemetry",
                "source": "canonical_stats.isos",
                "value": int(s["isos"]),
            })
    except Exception as e:
        logger.warning("brain_investigator: canonical_stats evidence failed: %s", e)

    # Source 2: recent brain findings (the live worklist / detected issues).
    try:
        ev = _gather_recent_findings()
        if ev:
            evidence.extend(ev)
    except Exception as e:
        logger.warning("brain_investigator: findings evidence failed: %s", e)

    # Source 3: HEALTH_BASELINE.md headline signals (curated known-good state).
    try:
        ev = _gather_health_baseline()
        if ev:
            evidence.extend(ev)
    except Exception as e:
        logger.warning("brain_investigator: health-baseline evidence failed: %s", e)

    # Source 4: growth-funnel metrics (traffic/reach, conversion, paid keys,
    # MRR, retention). Without these, ANY growth question is "an inference
    # dressed as a finding" and the refutation correctly nukes confidence to
    # ~0.25. Best-effort: a failure logs + is omitted, never raises.
    try:
        ev = gather_growth_funnel()
        if ev:
            evidence.extend(ev)
    except Exception as e:
        logger.warning("brain_investigator: growth-funnel evidence failed: %s", e)

    # Source 5: paid-monetization RECONCILIATION. Billing (users.plan) and the
    # MCP paywall (mcp_dev_keys.tier) track DIFFERENT populations — conflating
    # them is what produced the "91 paid users vs 18 paid keys" contradiction.
    # Surface both + real-invoice payers + the activation gap so the brain reasons
    # on the true picture. Best-effort.
    try:
        ev = _gather_paid_reconciliation()
        if ev:
            evidence.extend(ev)
    except Exception as e:
        logger.warning("brain_investigator: paid-reconciliation evidence failed: %s", e)

    # Source 6: RETENTION COHORTS (key-based funnel retention + reuse). The
    # brain diagnosed retention as the flywheel leak (~74 agents try / ~1
    # returns) but flagged its own answer ~0.2 confidence for LACK of
    # instrumentation. This source closes that gap: it hands the investigator
    # REAL, de-looped, COUNT(DISTINCT api_key) cohort figures — distinct new
    # keys, return rate, reuse distribution, and retention-by-first-tool — so
    # the next retention investigate() reasons on measured data, not a guess.
    # Best-effort, capped to a handful of items; never raises.
    try:
        ev = gather_retention_cohorts()
        if ev:
            evidence.extend(ev)
    except Exception as e:
        logger.warning("brain_investigator: retention-cohort evidence failed: %s", e)

    # Source 7: FACILITY BREAKDOWNS (verified vs tracked, by country / operator /
    # US ISO) from discovered_facilities. Answers the brain's self-resolving
    # "21k tracked vs ~2k verified" loop with REAL top-N measured breakdowns so
    # REASON cites ground truth instead of looping on a vague gap. Compact (≤4
    # items), so always attached when relevant — the module self-gates on
    # BRAIN_DATA_GATHER_ENABLED (default OFF / dark) and self-caps via a 4s
    # statement_timeout, returning [] when disabled or unreachable. Best-effort:
    # a failure logs + is omitted, mirroring Sources 5/6; NEVER raises.
    try:
        from routes.brain_data_gatherer import gather_facility_breakdowns
        ev = gather_facility_breakdowns()
        if ev:
            evidence.extend(ev)
    except Exception as e:
        logger.warning("brain_investigator: facility-breakdown evidence failed: %s", e)

    # Source 8: CAPABILITY LEDGER (self-knowledge). Hands REASON a compact
    # "ALREADY BUILT — do NOT recommend building" line (incl. the durable-key-
    # on-first-call surface persist_command the brain kept mis-flagging as a
    # gap) so it stops recommending shipped work. Read-only at gather time; the
    # module self-gates on BRAIN_CAPABILITY_LEDGER_ENABLED (default OFF / dark)
    # and returns [] when disabled. Best-effort: a failure logs + is omitted,
    # mirroring Sources 5/6; NEVER raises.
    try:
        from routes.brain_capability_ledger import gather_live_capabilities
        ev = gather_live_capabilities()
        if ev:
            evidence.extend(ev)
    except Exception as e:
        logger.warning("brain_investigator: capability-ledger evidence failed: %s", e)

    # Source 9: SHIPPED STATE (live MCP tool registry + merged PRs, 14d).
    # r-shipstate 2026-07-31: the digest proposed a tool 6x that had shipped
    # ~11h earlier, and two investigations re-litigated diagnoses corrected on
    # 07-28 — the brain had no view of what ALREADY exists or landed. Values
    # arrive pre-chunked <=190 chars because _evidence_block clips at 200.
    # Default ON (kill: BRAIN_SHIPPED_STATE_DISABLE=1) — deliberately NOT the
    # dark Source 7/8 convention: a staleness-killing source shipped dark IS
    # the failure class it exists to fix. Best-effort; NEVER raises.
    try:
        from routes.brain_shipped_state import gather_shipped_state
        ev = gather_shipped_state()
        if ev:
            evidence.extend(ev)
    except Exception as e:
        logger.warning("brain_investigator: shipped-state evidence failed: %s", e)

    return evidence


# ── Step 2c: GATHER honest RETENTION-COHORT evidence ─────────────────
# The actual cohort SQL lives in the CANONICAL standalone analyzer
# routes/retention_cohorts.py (compute_retention_cohorts + the adapter
# retention_cohort_evidence). We REUSE it here rather than hand-rolling a
# second copy of the cohort SQL — same de-loop (mcp_calls_deloop.PROBE_PLATFORMS
# applied to mcp_call_log's pre-classified platform), same COUNT(DISTINCT
# api_key) discipline — so the brain's GATHER-step evidence and the
# GET /api/v1/mcp/retention/cohorts ops endpoint NEVER drift. The brain reads
# the same numbers ops sees.
def gather_retention_cohorts(window_days: int = 30) -> list[dict]:
    """RETENTION COHORT evidence so the brain reasons on REAL key-based funnel
    retention instead of an un-instrumented ~0.2-confidence guess.

    Thin delegating wrapper over the canonical analyzer
    routes.retention_cohorts.retention_cohort_evidence() — that module owns the
    de-looped, COUNT(DISTINCT api_key) cohort SQL (new keys, return rate, reuse
    distribution, retention-by-first-tool, median time-to-2nd-call) and the
    ops endpoint. Reusing it keeps the brain evidence and the endpoint in
    lock-step (no two drifting implementations).

    Returns a CONCISE, CAPPED list of {claim, source, value} items. Best-effort:
    [] on any error / no canonical module / no data; NEVER raises (callers also
    wrap it)."""
    try:
        from routes.retention_cohorts import retention_cohort_evidence
    except Exception as e:
        logger.warning(
            "brain_investigator: retention_cohorts module unavailable: %s", e)
        return []
    try:
        ev = retention_cohort_evidence(days=int(window_days))
        return ev if isinstance(ev, list) else []
    except Exception as e:
        logger.warning(
            "brain_investigator: retention-cohort evidence failed: %s", e)
        return []


_PAID_PLANS = "('pro','founding','enterprise','pro_annual','developer')"


def _gather_paid_reconciliation() -> list[dict]:
    """Honest billing-vs-paywall reconciliation. users.plan (billing) and
    mcp_dev_keys.tier (the MCP paywall) are nearly-disjoint populations; the
    brain must see BOTH plus real-invoice payers and the activation gap, so it
    never reads 'active callers' as 'payers'. Read-only; [] on any error."""
    conn = _conn()
    if conn is None:
        return []
    items: list[dict] = []
    try:
        with conn.cursor() as cur:
            def scalar(sql: str):
                try:
                    cur.execute(sql)
                    row = cur.fetchone()
                    return int(row[0]) if row and row[0] is not None else 0
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    return None
            specs = [
                # NOTE: users.plan is a BILLING FLAG, not proof of payment — it
                # is stamped on founding/dev grants, outreach prospects
                # (partnerships@…), and test accounts. So plan-count != customers.
                # The honest denominator is invoices_paid_count>0. (2026-06-23)
                ("Accounts flagged paid-plan (billing flag — incl. grants/prospects/test, NOT all paying)",
                 f"SELECT COUNT(*) FROM users WHERE plan IN {_PAID_PLANS}",
                 "users.plan"),
                ("Accounts with >=1 real paid invoice (TRUE paying customers)",
                 "SELECT COUNT(*) FROM users WHERE COALESCE(invoices_paid_count,0) > 0",
                 "users.invoices_paid_count"),
                ("Paid-plan FLAGS with NO real invoice (grants/prospects/test — exclude from conversion math)",
                 f"SELECT COUNT(*) FROM users WHERE plan IN {_PAID_PLANS} AND COALESCE(invoices_paid_count,0) = 0",
                 "users.plan + invoices_paid_count"),
                ("Paid MCP keys (the paywall population)",
                 "SELECT COUNT(*) FROM mcp_dev_keys WHERE COALESCE(status,'active')='active' "
                 "AND tier IN ('paid','enterprise')",
                 "mcp_dev_keys.tier"),
                # TRUE activation gap = a REAL payer (has an invoice) who has NO
                # usable key in EITHER table (mcp_dev_keys OR api_keys). The old
                # query counted plan-flags (incl. phantom) and only looked at
                # mcp_dev_keys, missing the api_keys row that actually grants MCP
                # via the validate cross-check — so it over-reported ~27 when the
                # real gap is ~0. (2026-06-23)
                ("REAL payers (>=1 invoice) with NO usable key in either table (TRUE activation gap)",
                 f"SELECT COUNT(*) FROM users u WHERE u.plan IN {_PAID_PLANS} "
                 "AND COALESCE(u.invoices_paid_count,0) > 0 "
                 "AND NOT EXISTS (SELECT 1 FROM mcp_dev_keys k WHERE lower(k.email)=lower(u.email) "
                 "  AND COALESCE(k.status,'active')='active' AND k.tier IN ('paid','enterprise')) "
                 "AND NOT EXISTS (SELECT 1 FROM api_keys ak WHERE ak.user_id::text = u.id::text "
                 "  AND COALESCE(ak.is_active,1) <> 0)",
                 "users(real-invoice) minus keys in both tables"),
            ]
            for claim, sql, src in specs:
                v = scalar(sql)
                if v is not None:
                    items.append({"claim": claim, "source": src, "value": v})
        return items
    except Exception as e:
        logger.warning("brain_investigator: paid reconciliation failed: %s", e)
        return []
    finally:
        try: conn.close()
        except Exception: pass


# ── Step 2b: GATHER honest GROWTH-FUNNEL evidence ────────────────────
def gather_growth_funnel() -> list[dict]:
    """Pull REAL growth-funnel metrics so growth questions ("why is reach
    flat?", "is conversion working?", "is MRR moving?") get answered with
    MEASURED data instead of the model inventing inferences.

    HONEST-NUMBERS DISCIPLINE — we do NOT hand-roll trap-prone SQL. We reuse
    two ALREADY-VETTED honest aggregators:

      · routes.funnel_health._data_cached() — the canonical dashboard blob.
        MRR is users.plan-derived (NOT a raw row count), conversions_30d
        prefers the Stripe-backed mcp_conversions table, paid keys are the
        mcp_dev_keys tier breakdown, and the funnel waterfall uses DISTINCT
        paid users — all the loop-inflation / internal-traffic traps are
        handled INSIDE that module.

      · routes.ai_reach (cached module state) — honest REACH = DISTINCT
        external IPs (private/loopback + internal platforms filtered out),
        NOT loop-inflated request volume. We only read its in-memory cache so
        we never trigger the heavy scan or need a Flask app context here.

    Each item is {claim, source, value}. Read-only. Best-effort: any source
    that errors is simply omitted — this function NEVER raises (callers also
    wrap it, but we are defensive)."""
    out: list[dict] = []

    # ── Honest dashboard KPIs + funnel waterfall (the vetted aggregator) ──
    try:
        from routes.funnel_health import _data_cached
        data = _data_cached() or {}
        kpis = data.get("kpis") or {}
        funnel = data.get("funnel") or {}

        # MRR — users.plan-derived (Stripe-aligned, ~$4.7k), NOT a row count.
        if kpis.get("mrr_usd") is not None:
            out.append({
                "claim": "Monthly recurring revenue (MRR), USD",
                "source": "funnel_health KPIs (users.plan × per-plan price map)",
                "value": int(kpis.get("mrr_usd") or 0),
            })

        # Conversions 30d — canonical mcp_conversions (Stripe-backed),
        # fallback mcp_pair_codes.redeemed_at. NOT raw signal rows.
        if kpis.get("conversions_30d") is not None:
            out.append({
                "claim": "Paid conversions in last 30 days",
                "source": "funnel_health KPIs (mcp_conversions, Stripe-backed)",
                "value": int(kpis.get("conversions_30d") or 0),
            })

        # Paid + enterprise dev keys — the canonical paid-tier metric
        # (mcp_dev_keys.tier in (paid, enterprise)). Surface the honest
        # paid count, not the all-tier total (which is mostly free keys).
        tiers = kpis.get("dev_keys_by_tier") or {}
        if tiers:
            paid_keys = int(tiers.get("paid", 0) or 0) + \
                int(tiers.get("enterprise", 0) or 0)
            out.append({
                "claim": "Paid dev keys (tier in paid+enterprise)",
                "source": "funnel_health KPIs (mcp_dev_keys GROUP BY tier)",
                "value": paid_keys,
            })
        if kpis.get("active_dev_keys") is not None:
            out.append({
                "claim": "Active dev keys (all tiers, mostly free)",
                "source": "funnel_health KPIs (mcp_dev_keys active)",
                "value": int(kpis.get("active_dev_keys") or 0),
            })

        # Tool calls 7d — usage/retention proxy. CITE THE HONEST de-looped
        # number (external AI-agent calls), NOT the gross mcp_call_log count
        # which lumps in our own selfheal/probe/sweep loop traffic (~35-41k vs
        # ~9k real). The inflated count made a non-decline read as a decline.
        # tool_calls_7d_real is the canonical de-loop, identical to the
        # /api/v1/mcp/funnel endpoint definition. Honest-numbers fence.
        if kpis.get("tool_calls_7d_real") is not None:
            out.append({
                "claim": "MCP tool calls in last 7 days (de-looped, external)",
                "source": "funnel_health KPIs (mcp_tool_calls 7d, "
                          "loop/selfheal/probe excluded)",
                "value": int(kpis.get("tool_calls_7d_real") or 0),
            })
        elif kpis.get("tool_calls_7d") is not None:
            # Fallback only if the honest field is unavailable (degraded source).
            out.append({
                "claim": "MCP tool calls in last 7 days (INCL loop/internal "
                         "traffic — over-counts)",
                "source": "funnel_health KPIs (mcp_call_log 7d, gross)",
                "value": int(kpis.get("tool_calls_7d") or 0),
            })

        # Weekly TREND — so the brain can actually judge a DECLINE rather than
        # staring at one absolute number. Sourced from funnel_health's
        # de-looped weekly buckets (calls_week_trend). "This week vs trailing
        # 4-week avg" is the honest decline signal.
        trend = data.get("calls_week_trend") or {}
        if isinstance(trend, dict) and trend.get("last_week_calls") is not None:
            dp = trend.get("delta_pct")
            dp_str = (f"{dp:+.1f}%" if isinstance(dp, (int, float)) else "n/a")
            out.append({
                "claim": ("MCP calls last complete week vs trailing 4-week avg "
                          f"({dp_str}); de-looped external"),
                "source": "funnel_health calls_by_week (mcp_tool_calls, "
                          "loop/selfheal/probe excluded)",
                "value": int(trend.get("last_week_calls") or 0),
            })

        # Funnel waterfall — DISTINCT ACTIVE callers 30d. NOTE: this counts
        # distinct non-internal api_keys that made a call (free + paid), NOT
        # paying accounts. The old "paid users" label was wrong and caused the
        # brain to read it as 91 payers; paid accounts come from Source 5.
        if funnel.get("distinct_paid_users_30d") is not None:
            out.append({
                "claim": "Distinct active MCP callers (free+paid) in last 30 days",
                "source": "funnel_health funnel (DISTINCT non-internal api_key 30d)",
                "value": int(funnel.get("distinct_paid_users_30d") or 0),
            })
        # Top conversion-funnel stage drop, if computed — names the leak.
        drops = funnel.get("stage_drops_pct") or {}
        if isinstance(drops, dict) and drops:
            try:
                worst_stage = max(drops.items(),
                                  key=lambda kv: float(kv[1] or 0))
                out.append({
                    "claim": f"Biggest funnel stage drop: {worst_stage[0]}",
                    "source": "funnel_health funnel (stage_drops_pct)",
                    "value": float(worst_stage[1] or 0),
                })
            except Exception:
                pass
    except Exception as e:
        logger.warning(
            "brain_investigator: funnel_health evidence failed: %s", e)

    # ── Honest REACH = DISTINCT external IPs (read cached state only) ──
    try:
        from routes import ai_reach as _reach_mod
        cached = getattr(_reach_mod, "_cache", {}) or {}
        rdata = cached.get("data")
        if isinstance(rdata, dict):
            if rdata.get("distinct_agents_7d") is not None:
                out.append({
                    "claim": "Distinct external AI agents (reach), ~7d",
                    "source": "ai_reach (DISTINCT public IPs, internal filtered)",
                    "value": int(rdata.get("distinct_agents_7d") or 0),
                })
            if rdata.get("distinct_platforms") is not None:
                out.append({
                    "claim": "Distinct AI platforms reached, ~7d",
                    "source": "ai_reach (DISTINCT external platforms)",
                    "value": int(rdata.get("distinct_platforms") or 0),
                })
    except Exception as e:
        logger.warning("brain_investigator: ai_reach evidence failed: %s", e)

    return out


def _gather_recent_findings(limit: int = 12) -> list[dict]:
    """Recent rows from brain_findings (issue, url, count, last_seen). These are
    the live detected issues — real evidence the model can reason over. Returns
    [] on any error."""
    conn = _conn()
    if conn is None:
        return []
    out: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                # ★★ status='open' is REQUIRED, not cosmetic. This block is
                # labelled to the model as the "live detector worklist", but the
                # query had NO status filter — so a finding the auto-resolver had
                # already retired could still be handed to the brain as live work.
                # It is reachable: 81 findings are currently BOTH resolved AND
                # last_seen<24h (725 within 7d), and 26-299 findings resolve per
                # day. Open findings usually win the recency race because every
                # scan re-stamps their last_seen — which is exactly why this stayed
                # invisible instead of staying safe.
                # ★ Measured honestly: 0 non-open rows are in the top-12 right now,
                #   and the four sampled 404 drafts all PREDATE their finding's
                #   resolution — so this is a latent correctness bug, NOT a
                #   demonstrated cause of past wasted reasoning. Do not claim
                #   otherwise; the timeline was checked and it did not bite.
                # #49 lane 3: count_kind carries the detector's DECLARED
                # meaning of `count`, so a consumer stops inferring it from
                # the issue string. ★TRIED FIRST, NOT ASSUMED: the column is
                # added by brain_findings_writer's self-heal, so on a database
                # that has not run a write since deploy it does not exist yet
                # — and a failed SELECT here lands in the except below, which
                # blanks the ENTIRE live worklist. Falling back to the
                # original projection keeps a missing column from silently
                # emptying the brain's evidence feed.
                _WHERE = ("FROM brain_findings "
                          "WHERE COALESCE(status,'open') = 'open' "
                          "AND resolved_at IS NULL "
                          "ORDER BY last_seen DESC NULLS LAST LIMIT %s")
                rows, has_kind = [], True
                try:
                    cur.execute("SELECT issue, url, count, last_seen, "
                                f"count_kind {_WHERE}", (int(limit),))
                    rows = cur.fetchall() or []
                except Exception:
                    has_kind = False
                    try: conn.rollback()
                    except Exception: pass
                if not has_kind:
                    cur.execute(f"SELECT issue, url, count, last_seen {_WHERE}",
                                (int(limit),))
                    rows = cur.fetchall() or []
            except Exception:
                try: conn.rollback()
                except Exception: pass
                rows = []
                has_kind = False
            for r in rows:
                issue = r[0] if not hasattr(r, "get") else r.get("issue")
                url = r[1] if not hasattr(r, "get") else r.get("url")
                cnt = r[2] if not hasattr(r, "get") else r.get("count")
                kind = ""
                if has_kind:
                    kind = (r[4] if not hasattr(r, "get")
                            else r.get("count_kind")) or ""
                out.append({
                    "claim": f"Brain finding: {str(issue or '')[:120]}"
                             + (f" @ {url}" if url else ""),
                    "source": "brain_findings (live detector worklist)",
                    "value": int(cnt) if cnt is not None else None,
                    # Passed through under BOTH names: `count_kind` is what
                    # brain_work_selector.count_kind_of() reads, and the row
                    # also carries `issue` so the legacy string fallback still
                    # works for detectors that have not declared a type.
                    "count_kind": kind,
                    "issue": str(issue or ""),
                })
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _gather_health_baseline(max_lines: int = 40) -> list[dict]:
    """A couple of curated headline signals from HEALTH_BASELINE.md (the
    known-good snapshot). We surface the file's existence + a short excerpt as
    a single evidence item so the model knows the canonical baseline exists,
    without parsing the whole markdown table. Returns [] if unreadable."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "HEALTH_BASELINE.md")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            head = "".join(f.read().splitlines(keepends=True)[:max_lines])
        excerpt = head.strip()[:800]
        return [{
            "claim": "Health baseline (known-good config + canonical numbers)",
            "source": "HEALTH_BASELINE.md",
            "value": excerpt,
        }]
    except Exception:
        return []


def _evidence_block(evidence: list[dict]) -> str:
    """Render the gathered evidence as a compact, citeable block for prompts."""
    if not evidence:
        return "(no evidence could be gathered from the curated sources)"
    lines = []
    for e in evidence:
        val = e.get("value")
        if isinstance(val, str) and len(val) > 200:
            val = val[:200] + "…"
        lines.append(f"- [{e.get('source')}] {e.get('claim')}: {val}")
    return "\n".join(lines)


# ── Question-TARGETED evidence (2026-07-28, actuation shell #39 lane 2) ──
# THE MEASURED DEFECT: gather_evidence() takes NO ARGUMENTS. Every question got
# the SAME curated bundle — 111 drafts in 30d produced only SEVEN distinct
# evidence-source signatures, and 46 of them shared one. So a question like
# "why does /api/v1/energy/retail/rates 404 171 times?" was handed facility
# counts, ISO counts and funnel KPIs, and NOTHING about that endpoint.
#
# ★ The critics were right and specific. Sampled refutations asked for
#   per-endpoint request logs, status codes, and recency — 21 of 111 explicitly
#   named timestamps/recency. The reasoner then over-read the generic bundle
#   ("7 US ISOs already tracked" inferred from facility counts grouped by ISO)
#   and the critic correctly shredded it. That is not a weak reasoner; it is a
#   reasoner with nothing relevant to reason FROM.
#
# ★★ NOTE — the shell's original stated cause was WRONG and is corrected there
#   too: it claimed retrieval "cites prior findings by id without inlining their
#   content". Inspection of live rows shows prior_work IS fully inlined as text
#   (89/111 drafts) and prior_fixes in 32/111. Prior work was never the gap.
#
# What this adds: when the question names a URL path, pull what the endpoint
# ACTUALLY did — call volume, status split, last-seen, distinct callers — from
# api_endpoint_log. ★Absence is reported as evidence, never as silence.
# ★★ It also surfaces DETECTOR-vs-GROUND-TRUTH contradictions: brain_findings
#    claims 142 404s on /api/v1/energy/retail/rates while api_endpoint_log shows
#    10 calls, ALL 200. Control-checked: the table DOES record 404s (8,991 in
#    30d, top path /api/v1/facility/<id> at 8,329), so a zero here is a real
#    signal and not an instrumentation hole. Either the finding is stale or the
#    404s die at the CF edge before Flask — both are useful, and neither was
#    reachable from the generic bundle.
# Kill switch, no deploy: BRAIN_TARGETED_EVIDENCE=0
_PATH_RE = re.compile(r"(?<![\w.])(/(?:api|admin|mcp|for|markets|facilities|"
                      r"facility|dcpi|grid|vs)/[A-Za-z0-9_./<>\-]{0,80})")
_TARGET_MAX_PATHS = 3


def _extract_paths(question: str) -> list[str]:
    """URL paths named in the question, de-duped, order-preserving, bounded."""
    seen, out = set(), []
    for m in _PATH_RE.finditer(question or ""):
        p = (m.group(1) or "").rstrip(".,;:)'\"")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
        if len(out) >= _TARGET_MAX_PATHS:
            break
    return out


def _http_error_evidence(cur, conn, path: str) -> list[dict]:
    """What Flask's own 4xx/5xx middleware saw for this path.

    ★ This is the table that actually answers "is it 404ing?". brain_http_capture
    is an after_request middleware over EVERY 4xx/5xx, keyed or not, so unlike
    api_endpoint_log it sees anonymous browser/frontend/bot traffic. Pairing the
    two is what turns an absence into a diagnosis:
      keyed-log EMPTY + error-log HOT  -> real 404s from UNKEYED callers; the
                                          request DOES reach Flask; no route matches
      keyed-log EMPTY + error-log COLD -> genuinely not called (or dies at the edge)
    """
    try:
        cur.execute(
            "SELECT status, COUNT(*), MAX(occurred_at) "
            "FROM brain_http_errors "
            "WHERE pattern = %s AND occurred_at > now() - interval '24 hours' "
            "GROUP BY status ORDER BY 2 DESC LIMIT 5",
            (path,),
        )
        rows = cur.fetchall() or []
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return []
    if not rows:
        return [{
            "claim": f"{path} — brain_http_errors (Flask middleware over ALL 4xx/5xx, "
                     f"keyed or not) recorded NOTHING in 24h. Combined with an empty "
                     f"api_endpoint_log this is consistent with the path genuinely not "
                     f"being called, or being terminated before Flask.",
            "source": "brain_http_errors (question-targeted)",
            "value": 0,
        }]
    total = sum(int(r[1] or 0) for r in rows)
    split = ", ".join(f"{int(r[0] or 0)}×{int(r[1] or 0)}" for r in rows)
    newest = max((r[2] for r in rows if r[2]), default=None)
    return [{
        "claim": f"{path} — Flask's OWN error middleware recorded {total} error "
                 f"response(s) in 24h (status split {split}; last "
                 f"{newest.isoformat()[:19] if newest else 'n/a'}). ★So the request "
                 f"DOES reach Flask. If api_endpoint_log is empty for the same path, "
                 f"the callers are UNKEYED (browser/frontend/bot) — that is a coverage "
                 f"difference between the two tables, NOT evidence about the CF edge.",
        "source": "brain_http_errors (question-targeted)",
        "value": total,
    }]


# ── The CODE the question is about ───────────────────────────────────────────
# brain_source_map.resolve_finding_to_sources() has existed and been admin-
# exposed at /admin/brain/source-map for some time, ranked, capped and
# never-raising — and NOTHING in the investigation path ever called it. Listed
# is not delivered; this is the wire.
# ★ NEVER truncate below the resolver's own cap (5). Measured 2026-08-18: for
# "detector:check_shadowed_routes" the TRUE definition
# (routes/brain_consistency_radar.py:8761) came back as candidate #5, and every
# symbol match scored an identical 0.45 — so the ranking does not discriminate
# and a 4-item cut would have dropped the right answer while leaving four wrong
# ones. Truncating a flat ranking is worse than not looking.
_SOURCE_EVIDENCE_MAX = 5
_SNIPPET_MAX_CHARS = 700          # a window, never a whole file


def _source_evidence(question: str) -> list[dict]:
    """Ranked candidate source locations for the SUBJECT of this question.

    ALWAYS returns at least one item. A silent [] is what created the ambiguity
    this whole change exists to remove: the reasoner could not tell "there is no
    code for this" from "nobody looked", so it hedged either way. Say which.
    """
    if (os.environ.get("BRAIN_SOURCE_EVIDENCE") or "").strip() == "0":
        return []
    try:
        from routes.brain_source_map import resolve_finding_to_sources
    except Exception as e:                       # import must never kill the lane
        logger.warning("brain_investigator: source-map import failed: %s", e)
        return [{
            "claim": "SOURCE LOOKUP UNAVAILABLE — brain_source_map could not be "
                     "imported, so no code was retrieved for this question. Treat "
                     "the absence of source below as a TOOLING failure, NOT as "
                     "evidence that the code does not exist. Do not emit a remedy "
                     "block on this run.",
            "source": "brain_source_map (import failed)",
            "value": 0,
        }]

    try:
        cands = resolve_finding_to_sources(question) or []
    except Exception as e:                       # documented never-raises; belt anyway
        logger.warning("brain_investigator: source resolution failed: %s", e)
        cands = []

    if not cands:
        return [{
            "claim": f"NO SOURCE RESOLVED for this question. The repo index was "
                     f"searched by route, filename, table, symbol and free text and "
                     f"matched nothing. This is a MEASURED MISS, not an unattempted "
                     f"lookup — the subject may live in another repo "
                     f"(dchub-frontend / dchub-mcp-server), in config, or in "
                     f"infrastructure. Do NOT guess a file path, and do NOT emit a "
                     f"remedy block.",
            "source": "brain_source_map (question-targeted, 0 candidates)",
            "value": 0,
        }]

    shown = cands[:_SOURCE_EVIDENCE_MAX]
    # A flat ranking must not be read as a ranking. Symbol matches all score an
    # identical 0.45, so "candidate 1" carries no more authority than
    # "candidate 5" — and on the measured check_shadowed_routes case the correct
    # file WAS candidate 5. Say so, or the reasoner anchors on the first row.
    _flat = len({c.get("confidence") for c in shown}) <= 1 and len(shown) > 1
    out: list[dict] = []
    if _flat:
        out.append({
            "claim": f"★ THE {len(shown)} SOURCE CANDIDATES BELOW ARE UNRANKED — they all "
                     f"scored identically, so their ORDER IS MEANINGLESS. Read every one "
                     f"before concluding anything; do not treat the first as the answer.",
            "source": "brain_source_map (ranking is flat)",
            "value": len(shown),
        })
    for i, c in enumerate(shown, 1):
        try:
            snip = str(c.get("snippet") or "")[:_SNIPPET_MAX_CHARS]
            out.append({
                "claim": (
                    f"SOURCE CANDIDATE {i} of {len(shown)}: "
                    f"{c.get('file')}:{int(c.get('line') or 0)} "
                    f"(match={c.get('match_kind')}, confidence={c.get('confidence')}). "
                    f"★ This is a WINDOW around the match, not the whole file: a "
                    f"find string taken from it is NOT known to be unique in that "
                    f"file. Verify uniqueness before emitting any remedy block, and "
                    f"never widen a snippet into a claim about code you were not "
                    f"shown.\n{snip}"
                ),
                "source": "repo source (question-targeted, brain_source_map)",
                "value": c.get("confidence"),
            })
        except Exception:
            continue
    return out or [{
        "claim": "SOURCE CANDIDATES WERE MALFORMED — none could be rendered. "
                 "Treat as a tooling miss, not as absence of code.",
        "source": "brain_source_map (malformed candidates)",
        "value": 0,
    }]


def gather_targeted_evidence(question: str) -> list[dict]:
    """REAL rows about the SUBJECT of this question. [] on anything unexpected."""
    if (os.environ.get("BRAIN_TARGETED_EVIDENCE") or "").strip() == "0":
        return []
    # ★★★ SOURCE FIRST, AND OUTSIDE THE PATH GATE (2026-08-18).
    # Two separate reasons this call sits here and not below `if not paths`:
    #
    # 1. This function only ever read api_endpoint_log + brain_http_errors —
    #    HTTP rows. It NEVER read a line of code. Meanwhile the investigator's
    #    prompt DEMANDS a remedy block containing a find string that "appears
    #    EXACTLY ONCE in that file, copied verbatim". Asking for a verbatim
    #    patch while supplying zero source is unanswerable by construction, and
    #    the model correctly refused: on 2026-08-18, 10 of 15 investigations
    #    closed with some form of "the evidence block contains no source for X",
    #    and the adversarial critics repeatedly noted that nearly all evidence
    #    supplied was unrelated to the finding (MRR, funnel, facility counts).
    #
    # 2. `_extract_paths` finds URL-ish paths. A large share of real findings
    #    have NO path at all — `detector:check_shadowed_routes`,
    #    `env://LINKEDIN_ACCESS_TOKEN`, `table:stripe_webhook_events`. Those
    #    returned [] immediately and got nothing whatsoever. brain_source_map
    #    resolves by route AND filename AND table AND symbol AND free text, so
    #    it is exactly the resolver those questions needed.
    out: list[dict] = _source_evidence(question)
    paths = _extract_paths(question)
    if not paths:
        return out
    conn = _conn()
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            for path in paths:
                try:
                    # NOTE equality, never LIKE — a literal % in SQL with a
                    # params tuple is a documented 500 in this codebase.
                    cur.execute(
                        "SELECT status, COUNT(*), MAX(called_at), "
                        "       COUNT(DISTINCT api_key_prefix) "
                        "FROM api_endpoint_log "
                        "WHERE endpoint_path = %s "
                        "  AND called_at > now() - interval '30 days' "
                        "GROUP BY status ORDER BY 2 DESC LIMIT 6",
                        (path,),
                    )
                    rows = cur.fetchall() or []
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    continue

                if not rows:
                    out.append({
                        "claim": f"{path} — no rows in api_endpoint_log in 30d. "
                                 f"★READ THIS CORRECTLY: api_endpoint_log is an "
                                 f"after_request hook that returns early unless the "
                                 f"request carried an API key ('only track keyed "
                                 f"requests'). Absence here means NO KEYED CALLER hit "
                                 f"it — it does NOT mean the path is uncalled, and it "
                                 f"does NOT imply anything about the CF edge. "
                                 f"Unkeyed browser/frontend/bot traffic is invisible "
                                 f"to this table by construction.",
                        "source": "api_endpoint_log (question-targeted)",
                        "value": 0,
                    })
                    out.extend(_http_error_evidence(cur, conn, path))
                    continue

                total = sum(int(r[1] or 0) for r in rows)
                split = ", ".join(f"{int(r[0] or 0)}×{int(r[1] or 0)}" for r in rows)
                newest = max((r[2] for r in rows if r[2]), default=None)
                callers = max((int(r[3] or 0) for r in rows), default=0)
                out.append({
                    "claim": f"{path} — REAL traffic in 30d: {total} call(s); "
                             f"status split {split}; last seen "
                             f"{newest.isoformat()[:19] if newest else 'n/a'}; "
                             f"{callers} distinct caller prefix(es)",
                    "source": "api_endpoint_log (question-targeted)",
                    "value": total,
                })
                # ★ detector-vs-ground-truth contradiction, stated explicitly
                # ★★ A "contradiction" between these two tables was asserted here
                # and it was WRONG — it compared DIFFERENT POPULATIONS. api_endpoint_log
                # covers KEYED requests only; brain_http_errors is Flask middleware
                # over ALL 4xx/5xx. Zero 404s in the first with hits in the second is
                # the NORMAL state for unkeyed traffic, not a contradiction. Report
                # both tables and let the reasoner combine them.
                if re.search(r"404", question or ""):
                    out.extend(_http_error_evidence(cur, conn, path))
    except Exception as e:
        logger.warning("brain_investigator: targeted evidence failed: %s", e)
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── Corpus recall (RAG): so the brain stops re-investigating seen topics ──
def _recall_prior_work(question: str, k: int = 6) -> list[dict]:
    """Semantic recall of PRIOR findings + recommendations on the same theme,
    via routes.brain_rag.retrieve_context (pgvector HNSW + Cohere embed-v3).

    This is the anti-amnesia step: gather_evidence() only reads point-in-time
    SQL, so without recall the investigator re-reasons every recurring theme
    from scratch. We scope recall to the two PROSE corpora that hold the brain's
    own thinking — brain_findings + brain_strategic_recommendations — so the
    REASON step can build on / supersede prior work instead of repeating it.

    HARD FAIL-SOFT CONTRACT: this must NEVER break the investigation. Any
    failure (missing module, embed API down, DB down, unexpected shape) degrades
    to [] — the exact behaviour the caller had before recall existed. Only the
    question PROSE is embedded; no fabricated/SQL numbers are run through RAG
    (retrieval augments reasoning; metrics stay grounded in the evidence block).
    """
    q = (question or "").strip()
    if not q:
        return []
    try:
        from routes.brain_rag import retrieve_context
        hits = retrieve_context(
            q, k=k,
            corpus=["brain_findings", "brain_strategic_recommendations"],
        )
        return hits if isinstance(hits, list) else []
    except Exception as e:
        logger.warning("brain_investigator: prior-work recall failed: %s", e)
        return []


def _recall_prior_fixes(question: str, k: int = 3) -> list[dict]:
    """FIX-HISTORY recall (r-rag-fix-history 2026-07-18): "have I solved this
    class before?" — semantic recall over the fix_history corpus (closed
    GitHub issues, fix/feat commit postmortems, resolved brain_findings
    episodes) via routes.brain_rag.retrieve_prior_fixes. For a finding-driven
    investigation the caller's question carries the finding's issue+detail, so
    the top hits are the fixes previously shipped for this problem class.

    HARD FAIL-SOFT CONTRACT (same as _recall_prior_work): any failure —
    missing module, empty corpus, embed API down, DB down — degrades to []
    and the investigation proceeds exactly as before. Recall NEVER blocks."""
    q = (question or "").strip()
    if not q:
        return []
    try:
        from routes.brain_rag import retrieve_prior_fixes
        hits = retrieve_prior_fixes(q, k=k)
        return hits if isinstance(hits, list) else []
    except Exception as e:
        logger.warning("brain_investigator: prior-fix recall failed: %s", e)
        return []


def _prior_fixes_block(fixes: list[dict]) -> str:
    """Render recalled prior fixes (title, date, ref) for the REASON prompt.
    Empty/failed recall renders an explicit 'none' marker."""
    if not fixes:
        return "(no prior fixes recalled for this class of problem)"
    lines = []
    for h in fixes:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        date = (h.get("date") or "").strip()
        ref = (h.get("ref") or "").strip()
        bits = [title]
        if date:
            bits.append(date)
        if ref:
            bits.append(ref)
        lines.append("- " + " · ".join(bits))
    if not lines:
        return "(no prior fixes recalled for this class of problem)"
    return "\n".join(lines)


def _prior_work_block(prior: list[dict]) -> str:
    """Render recalled prior findings/recommendations for the REASON prompt.
    Empty/failed recall renders an explicit 'none' marker so the model isn't
    misled into thinking prior work exists."""
    if not prior:
        return "(no prior findings or recommendations recalled on this theme)"
    lines = []
    for h in prior:
        table = (h.get("source_table") or "").strip() or "brain"
        kind = (h.get("kind") or "").strip()
        text = (h.get("text") or "").strip()
        if not text:
            continue
        if len(text) > 400:
            text = text[:400] + "…"
        tag = f"{table}" + (f"/{kind}" if kind else "")
        lines.append(f"- [{tag}] {text}")
    if not lines:
        return "(no prior findings or recommendations recalled on this theme)"
    return "\n".join(lines)


# ── Structured-output schemas (2026-07-04) ───────────────────────────
# Derived from what investigate() consumes: decomp.get(...) /
# draft.get(...) / ref.get(...) — every key a consumer reads is a schema
# property (introspection-tested in tests/test_brain_structured_outputs.py).
# Structured-outputs constraints honoured: additionalProperties=false,
# no numeric/string bound keywords (0.0-1.0 / -0.5..+0.2 stay
# prompt-enforced + clamped in code, exactly as today).

_DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sub_questions": {"type": "array", "items": {"type": "string"}},
        "data_needed": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sub_questions", "data_needed"],
    "additionalProperties": False,
}

_REASON_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string"},
        "reasoning": {"type": "string"},
        "cited_evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "caveats": {"type": "array", "items": {"type": "string"}},
        "decision_for_human": {"type": "string"},
    },
    "required": ["recommendation", "reasoning", "cited_evidence",
                 "confidence", "caveats", "decision_for_human"],
    "additionalProperties": False,
}

_REFUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "weaknesses_found": {"type": "array", "items": {"type": "string"}},
        "survives_scrutiny": {"type": "boolean"},
        "confidence_adjustment": {"type": "number"},
        "added_caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["weaknesses_found", "survives_scrutiny",
                 "confidence_adjustment", "added_caveats"],
    "additionalProperties": False,
}


# ── LLM helper (reuse brain_models tier + resolve_chain fallback) ────
def _call_model(system: str, prompt: str, *, tier: str = "reasoning",
                max_tokens: int = 1500,
                schema: Optional[dict] = None) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """One Anthropic call through the brain's model infra. Returns
    (text, error, model_used). Reuses brain_models.brain_model_for (tier
    selection, Opus 4.8) + resolve_chain (404 fallback). Degrades gracefully:
    returns (None, 'no_api_key', None) when the key is missing. Never raises.

    schema (2026-07-04): when given AND the model supports Anthropic
    structured outputs (verified GA param output_config.format, no beta
    header), the request pins the response to that JSON schema so the text
    block is guaranteed-parseable JSON. FAIL-SOFT: a 400 on a structured
    attempt retries the SAME model with the legacy free-text body before the
    existing 400/404/429 chain-walk; BRAIN_STRUCTURED_OUTPUTS=0 forces the
    legacy path everywhere. The legacy body/behaviour is unchanged."""
    if not ANTHROPIC_API_KEY:
        return None, "no_api_key", None
    try:
        from routes.brain_models import brain_model_for, resolve_chain
        models = resolve_chain(brain_model_for(tier))
    except Exception:
        models = ["claude-opus-4-8", "claude-sonnet-4-5"]
    try:
        from routes import brain_llm_structured as _so
    except Exception:
        _so = None
    last_err = None
    for i, model in enumerate(models):
        _attempts = ((True, False)
                     if (_so is not None and _so.structured_active(model, schema))
                     else (False,))
        _walk_chain = False
        for _structured in _attempts:
            try:
                if _so is not None:
                    body_dict, _ = _so.build_messages_body(
                        model, system,
                        [{"role": "user", "content": prompt}],
                        max_tokens, schema if _structured else None)
                else:
                    body_dict = {
                        "model": model,
                        "max_tokens": max_tokens,
                        "system": system,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                body = json.dumps(body_dict).encode("utf-8")
                req = urllib.request.Request(
                    anthropic_messages_url(),
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": ANTHROPIC_API_KEY,
                        "User-Agent": "dchub-brain/1.0",
                        "Anthropic-Version": "2023-06-01",
                    },
                )
                with urllib.request.urlopen(req, timeout=50) as r:
                    data = json.loads(r.read().decode("utf-8"))
                try:
                    from routes.brain_llm_structured import record_llm_usage
                    record_llm_usage("brain-investigator", model, data)
                except Exception:
                    pass
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", ""), None, model
                return None, "no_text_block", model
            except urllib.error.HTTPError as e:
                last_err = f"http_{e.code}"
                if _structured and e.code == 400:
                    # Structured param plausibly the cause — memoize when the
                    # error body blames it, then retry this SAME model legacy.
                    try:
                        _etext = e.read().decode("utf-8", "replace")
                    except Exception:
                        _etext = ""
                    if _so is not None and _so.looks_like_structured_rejection(
                            e.code, _etext):
                        _so.mark_model_unsupported(model)
                    continue
                if e.code in (400, 404, 429) and i + 1 < len(models):
                    _walk_chain = True
                    break
                return None, last_err, model
            except Exception as e:
                return None, f"call_fail:{repr(e)[:140]}", model
        if _walk_chain:
            continue
    return None, last_err or "all_models_failed", None


def _parse_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of a model response (tolerates ```json fences)."""
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
        return json.loads(s[i:j + 1])
    except Exception:
        return None


# ── Prompts for the 5-step chain ─────────────────────────────────────
_DECOMPOSE_SYSTEM = (
    "You are the DC Hub Brain Investigator. A human operator asked a real "
    "business question about https://dchub.cloud (a live data-center "
    "infrastructure intelligence platform / MCP server). Your FIRST job is to "
    "DECOMPOSE the question — NOT to answer it.\n\n"
    "Output STRICTLY a JSON object, no prose outside it:\n"
    "  {\"sub_questions\": [\"<3-6 specific sub-questions to answer>\"],\n"
    "   \"data_needed\": [\"<named data sources / metrics required>\"]}\n"
)

_REASON_SYSTEM = (
    "You are the DC Hub Brain Investigator. You have a question, its "
    "decomposition, a PRIOR WORK block (findings + recommendations already "
    "produced on this theme), and REAL EVIDENCE gathered from DC Hub's "
    "ground-truth data sources. Reason FROM THE EVIDENCE to a draft "
    "recommendation.\n\n"
    "HARD RULES:\n"
    "  - PRIOR WORK is context, NOT evidence. Do NOT re-derive conclusions the "
    "prior work already reached — explicitly BUILD ON or SUPERSEDE them, and say "
    "which. If your recommendation merely restates prior work, say so and lower "
    "confidence. NEVER cite a number from the PRIOR WORK block as if it were "
    "current evidence — only the EVIDENCE block carries citeable figures.\n"
    "  - Cite ONLY numbers that appear in the EVIDENCE block. NEVER invent a "
    "figure. If a number you'd want isn't in the evidence, say 'not measured' "
    "rather than guessing.\n"
    "  - Banned figures (these are known-false): 50,000 facilities, $324B deal "
    "value, 340+ markets, 96+ AI platforms. Real canon: ~21,000 tracked "
    "facilities, ~178 countries, ~232-300 DCPI markets, ~2,032 deals.\n"
    "  - You RECOMMEND only. You do not act. Surface the decision for the human.\n\n"
    "Output STRICTLY a JSON object:\n"
    "  {\"recommendation\": \"<2-4 sentences: what you'd recommend, grounded in "
    "the evidence>\",\n"
    "   \"reasoning\": \"<how the evidence leads there>\",\n"
    "   \"cited_evidence\": [\"<which evidence items you relied on>\"],\n"
    "   \"confidence\": <0.0-1.0>,\n"
    "   \"caveats\": [\"<what could make this wrong>\"],\n"
    "   \"decision_for_human\": \"<the explicit choice the human must make>\"}\n"
)

_REFUTE_SYSTEM = (
    "You are the DC Hub Brain Investigator's ADVERSARIAL CRITIC. You are given a "
    "DRAFT recommendation and the evidence it was built on. Your ONLY job is to "
    "TRY TO BREAK IT. Find weaknesses: unsupported leaps, evidence that doesn't "
    "actually support the claim, confounders, missing data, and any figure that "
    "isn't backed by the evidence block. Be ruthless but fair.\n\n"
    "Output STRICTLY a JSON object:\n"
    "  {\"weaknesses_found\": [\"<each concrete weakness>\"],\n"
    "   \"survives_scrutiny\": <true|false: does the core recommendation still "
    "hold despite the weaknesses?>,\n"
    "   \"confidence_adjustment\": <-0.5..+0.2: how much to adjust confidence; "
    "negative to LOWER for unaddressed weaknesses, a small POSITIVE only if the "
    "recommendation genuinely survives scrutiny AND the evidence corroborates it>,\n"
    "   \"added_caveats\": [\"<caveats the operator must hear>\"]}\n"
)


def _clamp01(x, default=0.5) -> float:
    try:
        v = float(x)
    except Exception:
        return float(default)
    return max(0.0, min(1.0, v))


# ── The investigation ────────────────────────────────────────────────
def investigate(question: str, *, depth: str = "default") -> dict:
    """Run the 5-step verified investigation chain and return a structured,
    recommend-only result. Best-effort; NEVER raises. Returns a dict with
    cannot_investigate set when the model helper is unavailable or errors.

    depth is accepted for forward-compat (e.g. 'quick' vs 'deep'); the default
    runs the full decompose -> gather -> reason -> refute -> synthesize chain.
    """
    question = (question or "").strip()
    base = {
        "question": question,
        "decomposition": None,
        "evidence": [],
        "recommendation": None,
        "confidence": 0.0,
        "caveats": [],
        "decision_for_human": None,
        "refutation": {"attempted": False, "weaknesses_found": [], "survived": None},
        "model": None,
    }
    if not question:
        base["cannot_investigate"] = "empty_question"
        return base

    # Gather evidence FIRST (it's pure-data, works even without an API key, and
    # the failure mode is graceful — empty list, not a crash).
    try:
        evidence = gather_evidence()
    except Exception as e:
        evidence = []
        logger.warning("brain_investigator: gather_evidence failed: %s", e)
    # ★ Question-TARGETED evidence FIRST in the block, so the model reads what
    #   is actually about its subject before the generic platform bundle.
    try:
        targeted = gather_targeted_evidence(question)
    except Exception as e:
        targeted = []
        logger.warning("brain_investigator: gather_targeted_evidence failed: %s", e)
    evidence = targeted + evidence
    base["evidence"] = evidence
    base["targeted_evidence_count"] = len(targeted)
    evidence_block = _evidence_block(evidence)

    # Corpus recall: pull PRIOR findings + recommendations on this same theme so
    # the REASON step builds on / supersedes them instead of re-investigating a
    # seen topic. Fail-soft: on any RAG error this is [] and behaviour is
    # identical to the pre-recall path (metrics still come only from the SQL
    # evidence block above — recall augments reasoning, not the numbers).
    prior_work = _recall_prior_work(question, k=6)
    base["prior_work"] = prior_work
    prior_work_block = _prior_work_block(prior_work)

    # Fix-history recall: BEFORE reasoning, ask "have I solved this class
    # before?" against the fix_history corpus (closed issues + fix commits +
    # resolved finding episodes) and attach the top hits as prior_fixes so
    # the investigation starts from known solutions instead of from scratch.
    # Fail-soft: recall errors degrade to [] and never block investigation.
    prior_fixes = _recall_prior_fixes(question, k=3)
    base["prior_fixes"] = prior_fixes
    prior_fixes_block = _prior_fixes_block(prior_fixes)

    # ── Step 1: DECOMPOSE ────────────────────────────────────────────
    # 2026-07-01: caps raised 700/1500 → 2000/4000. On fable-5 (reasoning tier
    # since the r85j auto-promote) thinking tokens count toward max_tokens, so
    # the old caps hit stop_reason=max_tokens mid-JSON → unparseable → every
    # investigation died with model_returned_no_recommendation. Same lesson as
    # brain_v2_layer4's "4000, not 800".
    # 2026-07-04 (model-routing right-sizing): DECOMPOSE is a mechanical
    # extraction task — emit 3-6 sub-questions + data-source names as JSON,
    # explicitly NOT answering the question — so it rides the cheap "voice"
    # tier (haiku-4-5), not the fable/opus reasoning tier it launched on.
    # Safe by construction: haiku-4-5 is on the GA structured-outputs list
    # (brain_llm_structured.STRUCTURED_OUTPUT_MODEL_PREFIXES) so
    # _DECOMPOSE_SCHEMA still rides; haiku has no always-on thinking, so the
    # 2000-token cap is pure output headroom; and _call_model still walks
    # resolve_chain(brain_model_for(tier)) — only the tier argument changed.
    # REASON stays on "reasoning" and REFUTE on "challenger" (judgment work).
    dtext, derr, dmodel = _call_model(
        _DECOMPOSE_SYSTEM,
        f"Operator question: {question}\n\nDecompose it.",
        tier="voice", max_tokens=2000, schema=_DECOMPOSE_SCHEMA,
    )
    if derr:
        base["cannot_investigate"] = derr
        return base
    base["model"] = dmodel
    decomp = _parse_json(dtext) or {}
    base["decomposition"] = {
        "sub_questions": decomp.get("sub_questions") or [],
        "data_needed": decomp.get("data_needed") or [],
    }

    # ── Step 3: REASON from the gathered evidence ───────────────────
    # PRIOR WORK block is injected ALONGSIDE the SQL evidence (never in place of
    # it): the model is told to build on / supersede prior findings rather than
    # repeat them, but figures are still cited ONLY from the EVIDENCE block.
    reason_prompt = (
        f"Operator question: {question}\n\n"
        f"Decomposition:\n"
        f"  sub-questions: {json.dumps(base['decomposition']['sub_questions'])}\n"
        f"  data needed: {json.dumps(base['decomposition']['data_needed'])}\n\n"
        f"PRIOR WORK (do not repeat; build on or supersede):\n{prior_work_block}\n\n"
        f"PRIOR FIXES (fixes already shipped for this class of problem — check "
        f"whether one already covers this before recommending new work):\n"
        f"{prior_fixes_block}\n\n"
        f"EVIDENCE (ground-truth — cite ONLY these numbers):\n{evidence_block}\n\n"
        f"Reason from the evidence to a draft recommendation."
    )
    rtext, rerr, rmodel = _call_model(
        _REASON_SYSTEM, reason_prompt, tier="reasoning", max_tokens=4000,
        schema=_REASON_SCHEMA)
    if rerr:
        base["cannot_investigate"] = rerr
        return base
    base["model"] = rmodel or base["model"]
    draft = _parse_json(rtext) or {}
    recommendation = (draft.get("recommendation") or "").strip()
    if not recommendation:
        base["cannot_investigate"] = "model_returned_no_recommendation"
        return base
    confidence = _clamp01(draft.get("confidence"), 0.5)
    caveats = list(draft.get("caveats") or [])
    decision_for_human = (draft.get("decision_for_human") or "").strip() or None

    # ── Step 4: ADVERSARIAL REFUTE (a SEPARATE pass) ────────────────
    refute_prompt = (
        f"Operator question: {question}\n\n"
        f"DRAFT recommendation to attack:\n{recommendation}\n\n"
        f"Draft reasoning: {draft.get('reasoning', '')}\n"
        f"Draft confidence: {confidence}\n\n"
        f"EVIDENCE it was built on:\n{evidence_block}\n\n"
        f"Try to break this recommendation."
    )
    # max_tokens generous: the adversarial pass enumerates weaknesses + caveats
    # and a tight cap truncates the JSON mid-object → unparseable → a refutation
    # that silently contributes nothing while still claiming it ran.
    ftext, ferr, fmodel = _call_model(
        _REFUTE_SYSTEM, refute_prompt, tier="challenger", max_tokens=4000,
        schema=_REFUTE_SCHEMA)
    # 2026-06-20: the adversarial pass IS the brain's trust signal — a confidence
    # that never got refuted (survived=null) is worth far less than one that did.
    # A TRANSIENT failure (read timeout) was silently leaving recommendations
    # un-stress-tested (e.g. the retention 0.4 whose refutation timed out).
    # Retry ONCE on any transient error so far more investigations get a REAL
    # survived/broke verdict; only a second failure records the honest un-tested
    # state + the confidence dock below.
    if ferr:
        ftext, ferr, fmodel = _call_model(
            _REFUTE_SYSTEM, refute_prompt, tier="challenger", max_tokens=4000,
            schema=_REFUTE_SCHEMA)
    refutation = {"attempted": True, "weaknesses_found": [], "survived": None}
    if ferr:
        # The refutation pass failed — be HONEST about it (don't pretend the
        # recommendation survived scrutiny it never got) and dock confidence.
        refutation["attempted"] = False
        refutation["error"] = ferr
        caveats.append("adversarial refutation could not run "
                       f"({ferr}) — recommendation is UN-stress-tested")
        confidence = _clamp01(confidence - 0.15, confidence)
    else:
        ref = _parse_json(ftext)
        if not ref:
            # The pass RAN but its output didn't parse (truncation / non-JSON).
            # Do NOT claim it survived scrutiny it never resolved — say so and
            # dock confidence, so the trust signal stays honest.
            refutation["unparsed"] = True
            caveats.append("adversarial refutation ran but its output could not "
                           "be parsed — treat the recommendation as only "
                           "PARTIALLY stress-tested")
            confidence = _clamp01(confidence - 0.1, confidence)
        else:
            weaknesses = list(ref.get("weaknesses_found") or [])
            survived = ref.get("survives_scrutiny")
            adj = ref.get("confidence_adjustment")
            try:
                adj = float(adj)
            except Exception:
                adj = 0.0
            # r-brain-loop (2026-06-30): two-directional. The clamp used to be
            # min(0.0,...) — confidence could only ever fall, a paralysis ratchet
            # that flattened every item to 0.2-0.4 so nothing ever looked
            # strong/new. Now a recommendation that genuinely SURVIVES refutation
            # with corroboration may rise modestly (+0.2 cap); anything not
            # clearly survived still cannot rise, and the survived-False cap below
            # still holds refuted items at <=0.35.
            _pos_cap = 0.2 if (survived is True) else 0.0
            adj = max(-0.5, min(_pos_cap, adj))
            refutation["weaknesses_found"] = weaknesses
            refutation["survived"] = bool(survived) if survived is not None else None
            refutation["model"] = fmodel
            # Fold the refutation into final confidence + caveats.
            confidence = _clamp01(confidence + adj, confidence)
            if survived is False:
                confidence = _clamp01(min(confidence, 0.35), confidence)
            for c in (ref.get("added_caveats") or []):
                if c and c not in caveats:
                    caveats.append(c)

    # ── Honest-numbers fence: catch any fabricated figure ───────────
    fab = _fence_fabricated_figures(recommendation + " " + " ".join(caveats))
    if fab:
        for f in fab:
            caveats.append(f"FABRICATION FLAGGED: {f}")
        confidence = _clamp01(min(confidence, 0.3), confidence)
        refutation["weaknesses_found"] = list(refutation.get("weaknesses_found") or []) + \
            [f"honest-numbers fence: {f}" for f in fab]
        refutation["fabrication_flagged"] = True

    # ── Step 5: SYNTHESIZE (assemble the final recommend-only result) ─
    base["recommendation"] = recommendation
    base["confidence"] = round(confidence, 3)
    base["caveats"] = caveats
    base["decision_for_human"] = decision_for_human
    base["refutation"] = refutation
    base["reasoning"] = draft.get("reasoning")
    base["cited_evidence"] = draft.get("cited_evidence") or []
    return base


# ── Storage ──────────────────────────────────────────────────────────
def _store_investigation(question: str, result: dict) -> Optional[int]:
    """Persist an investigation row. Returns the new id (or None on failure).
    The ONLY write the investigator performs — recommend-only."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_investigations (question, result_json, confidence) "
                "VALUES (%s, %s, %s) RETURNING id",
                (question[:4000], json_for_column(result, 200000),
                 float(result.get("confidence") or 0.0)),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as e:
        logger.warning("brain_investigator: store failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _enqueue_investigation(question: str) -> Optional[int]:
    """Insert a PENDING row (no result yet) so POST /ask can return immediately
    and a background thread fills it in. The 3-4-pass verified chain is far too
    slow to run inside a request (502 + single-replica flapping)."""
    return _store_investigation(question, {"status": "pending"})


def _update_investigation(inv_id: int, result: dict) -> bool:
    """Fill in a pending investigation with its result. Best-effort."""
    conn = _conn()
    if conn is None or inv_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE brain_investigations SET result_json=%s, confidence=%s WHERE id=%s",
                (json_for_column(result, 200000),
                 float(result.get("confidence") or 0.0), int(inv_id)),
            )
            conn.commit()
            return True
    except Exception as e:
        logger.warning("brain_investigator: update failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


def _run_investigation_async(inv_id: int, question: str, depth: str) -> None:
    """Daemon-thread worker: run the slow verified investigation + store it.
    Never blocks the request, never raises out (recommend-only)."""
    try:
        result = investigate(question, depth=depth)
    except Exception as e:
        result = {"question": question, "cannot_investigate": f"error:{str(e)[:160]}"}
    try:
        _update_investigation(inv_id, result)
    except Exception:
        pass


def _get_investigation(inv_id: int) -> Optional[dict]:
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, question, result_json, confidence, grade, created_at "
                "FROM brain_investigations WHERE id = %s",
                (int(inv_id),),
            )
            r = cur.fetchone()
        if not r:
            return None
        result_json = r[2]
        if isinstance(result_json, str):
            try: result_json = json.loads(result_json)
            except Exception: pass
        created = r[5]
        try: created = created.isoformat()
        except Exception: created = str(created)
        return {
            "id": r[0], "question": r[1], "result": result_json,
            "confidence": r[3], "grade": r[4], "created_at": created,
        }
    except Exception as e:
        logger.warning("brain_investigator: get failed: %s", e)
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _grade_investigation(inv_id: int, grade: str) -> bool:
    """Record a human grade for CALIBRATION (the verify->learn loop). Returns
    True if a row was updated."""
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE brain_investigations SET grade = %s WHERE id = %s",
                (str(grade)[:64], int(inv_id)),
            )
            n = cur.rowcount or 0
            conn.commit()
            return n > 0
    except Exception as e:
        logger.warning("brain_investigator: grade failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


# ── Endpoints (admin-gated) ──────────────────────────────────────────
@brain_investigator_bp.post("/api/v1/brain/investigate")
def ask():
    """Pose a business question. RECOMMEND-ONLY: runs the verified investigation
    chain, stores it, returns {id, result}. Flag-gated — when
    BRAIN_INVESTIGATOR_ENABLED is off this returns {enabled: false} WITHOUT
    calling any model. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403
    if not _enabled():
        # Short-circuit BEFORE any model call.
        return jsonify(ok=True, enabled=False,
                       note="BRAIN_INVESTIGATOR_ENABLED is off — investigator "
                            "ships dark. Set it to 1 to enable."), 200
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify(ok=False, error="question required"), 400
    depth = (body.get("depth") or "default").strip() or "default"
    # SYNCHRONOUS: the verified chain is ~48s (3 model calls) — comfortably under
    # the gunicorn 120s worker timeout. Running it in-request is DURABLE: unlike a
    # fire-and-forget daemon thread, there is no in-flight work for a redeploy /
    # worker-recycle to silently kill (which left rows stuck 'pending'), and any
    # failure is visible to the caller + retryable. Recommend-only; never raises.
    result = investigate(question, depth=depth)
    inv_id = _store_investigation(question, result)
    return jsonify(ok=True, enabled=True, id=inv_id, result=result), 200


@brain_investigator_bp.get("/api/v1/brain/investigate/<int:inv_id>")
def get_ask(inv_id):
    """Fetch a stored investigation. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    rec = _get_investigation(inv_id)
    if rec is None:
        return jsonify(ok=False, error="not_found"), 404
    return jsonify(ok=True, **rec), 200


@brain_investigator_bp.post("/api/v1/brain/investigate/<int:inv_id>/grade")
def grade_ask(inv_id):
    """Record a human grade (good/bad/score) for CALIBRATION — the same
    verify->learn loop, applied to the investigator's own track record.
    Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    body = request.get_json(silent=True) or {}
    grade = (str(body.get("grade") or "")).strip()
    if not grade:
        return jsonify(ok=False, error="grade required"), 400
    ok = _grade_investigation(inv_id, grade)
    if not ok:
        return jsonify(ok=False, error="not_found_or_db_error"), 404
    return jsonify(ok=True, id=inv_id, grade=grade), 200


# NOTE: GET /api/v1/mcp/retention/cohorts is served by routes/retention_cohorts.py
# (the canonical standalone analyzer — richer structured shape: new_keys,
# return_rate, reuse_buckets, multiday_rate, first_tool_mix, retention_by_first_tool,
# median_time_to_2nd_call). gather_retention_cohorts() above stays as the brain's
# GATHER-step evidence Source 6. Both de-loop via the SAME canonical
# mcp_calls_deloop.PROBE_PLATFORMS list, so the endpoint and the evidence agree.
# Defining the route here too would collide at blueprint registration (duplicate
# rule on the same app), so it is owned by retention_cohorts_bp only.


def register_brain_investigator(app) -> None:
    """Idempotent registration helper for main.py. Best-effort schema init."""
    try:
        init_investigator_schema()
    except Exception as e:
        logger.warning("brain_investigator: schema init skipped: %s", e)
    try:
        app.register_blueprint(brain_investigator_bp)
    except Exception as e:
        logger.warning("brain_investigator already registered: %s", e)
