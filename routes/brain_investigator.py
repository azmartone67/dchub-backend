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
                "source": "canonical_stats (is_duplicate=0 AND merged_at IS NULL)",
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

    return evidence


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
                ("Accounts on a paid plan (billing)",
                 f"SELECT COUNT(*) FROM users WHERE plan IN {_PAID_PLANS}",
                 "users.plan"),
                ("Accounts with >=1 real paid invoice (true revenue)",
                 "SELECT COUNT(*) FROM users WHERE COALESCE(invoices_paid_count,0) > 0",
                 "users.invoices_paid_count"),
                ("Paid MCP keys (the paywall population)",
                 "SELECT COUNT(*) FROM mcp_dev_keys WHERE COALESCE(status,'active')='active' "
                 "AND tier IN ('paid','enterprise')",
                 "mcp_dev_keys.tier"),
                ("Paid-plan accounts WITHOUT a paid MCP key (activation gap)",
                 "SELECT COUNT(*) FROM users u LEFT JOIN mcp_dev_keys k "
                 "ON lower(k.email)=lower(u.email) AND COALESCE(k.status,'active')='active' "
                 f"WHERE u.plan IN {_PAID_PLANS} AND (k.tier IS NULL OR k.tier NOT IN ('paid','enterprise'))",
                 "users LEFT JOIN mcp_dev_keys (email)"),
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
                cur.execute(
                    "SELECT issue, url, count, last_seen "
                    "FROM brain_findings "
                    "ORDER BY last_seen DESC NULLS LAST LIMIT %s",
                    (int(limit),),
                )
                rows = cur.fetchall() or []
            except Exception:
                try: conn.rollback()
                except Exception: pass
                rows = []
            for r in rows:
                issue = r[0] if not hasattr(r, "get") else r.get("issue")
                url = r[1] if not hasattr(r, "get") else r.get("url")
                cnt = r[2] if not hasattr(r, "get") else r.get("count")
                out.append({
                    "claim": f"Brain finding: {str(issue or '')[:120]}"
                             + (f" @ {url}" if url else ""),
                    "source": "brain_findings (live detector worklist)",
                    "value": int(cnt) if cnt is not None else None,
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


# ── LLM helper (reuse brain_models tier + resolve_chain fallback) ────
def _call_model(system: str, prompt: str, *, tier: str = "reasoning",
                max_tokens: int = 1500) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """One Anthropic call through the brain's model infra. Returns
    (text, error, model_used). Reuses brain_models.brain_model_for (tier
    selection, Opus 4.8) + resolve_chain (404 fallback). Degrades gracefully:
    returns (None, 'no_api_key', None) when the key is missing. Never raises."""
    if not ANTHROPIC_API_KEY:
        return None, "no_api_key", None
    try:
        from routes.brain_models import brain_model_for, resolve_chain
        models = resolve_chain(brain_model_for(tier))
    except Exception:
        models = ["claude-opus-4-8", "claude-sonnet-4-5"]
    last_err = None
    for i, model in enumerate(models):
        try:
            body = json.dumps({
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")
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
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block.get("text", ""), None, model
            return None, "no_text_block", model
        except urllib.error.HTTPError as e:
            last_err = f"http_{e.code}"
            if e.code in (400, 404, 429) and i + 1 < len(models):
                continue
            return None, last_err, model
        except Exception as e:
            return None, f"call_fail:{repr(e)[:140]}", model
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
    "decomposition, and REAL EVIDENCE gathered from DC Hub's ground-truth data "
    "sources. Reason FROM THE EVIDENCE to a draft recommendation.\n\n"
    "HARD RULES:\n"
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
    "   \"confidence_adjustment\": <-0.5..0.0: how much to LOWER confidence>,\n"
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
    base["evidence"] = evidence
    evidence_block = _evidence_block(evidence)

    # ── Step 1: DECOMPOSE ────────────────────────────────────────────
    dtext, derr, dmodel = _call_model(
        _DECOMPOSE_SYSTEM,
        f"Operator question: {question}\n\nDecompose it.",
        tier="reasoning", max_tokens=700,
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
    reason_prompt = (
        f"Operator question: {question}\n\n"
        f"Decomposition:\n"
        f"  sub-questions: {json.dumps(base['decomposition']['sub_questions'])}\n"
        f"  data needed: {json.dumps(base['decomposition']['data_needed'])}\n\n"
        f"EVIDENCE (ground-truth — cite ONLY these numbers):\n{evidence_block}\n\n"
        f"Reason from the evidence to a draft recommendation."
    )
    rtext, rerr, rmodel = _call_model(
        _REASON_SYSTEM, reason_prompt, tier="reasoning", max_tokens=1500)
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
        _REFUTE_SYSTEM, refute_prompt, tier="challenger", max_tokens=1800)
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
            adj = max(-0.5, min(0.0, adj))
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
                (question[:4000], json.dumps(result)[:200000],
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
                (json.dumps(result)[:200000],
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
