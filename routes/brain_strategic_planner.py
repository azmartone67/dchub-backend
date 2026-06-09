"""
brain_strategic_planner.py — Brain Layer-6 Strategic Synthesis (2026-06-06).
==========================================================================

PROBLEM
-------
Layers 1-5 are TACTICAL: detect → diagnose → propose 1-line fix → draft PR.
Working — see the r68 backlog dashboard (commit dfabb3c4, draft PRs
#1031-1035). What's missing is STRATEGIC reasoning: the brain has never
once said "DC Hub should build X next, because the funnel + customer
asks + competitors all point to it."

This module IS that voice. Once a week (Monday 08:00 UTC via the
SCHEDULE harness) it:

  1.  Pulls 6 inputs (see _gather_strategic_context):
      • Funnel:        /api/v1/mcp/funnel  (paywall→signal→code→paid)
      • Page health:   /api/v1/sentinel/page-integrity  (~69 pages)
      • Customer asks: /api/v1/feedback/list?status=open&sort=top
      • Brain backlog: /api/v1/admin/brain/backlog  (stuck issues
                       untried × 23 cycles each — what patterns repeat?)
      • Competitors:   mcp_presence_crawler (drift + new registries)
      • Self-model:    /api/v1/brain/self-model  (what the brain
                       believes about itself: open findings, fix
                       success rate, weakest areas)

  2.  Asks Claude (reasoning tier, opus-4-8 with fallback) for:
      • Top 3 strategic gaps (4-week ship target)
      • Top 3 features competitors have that DC Hub lacks
      • Top 3 funnel optimizations, each with a $/lift estimate
      • 1 wild-card bet — a long-term differentiator
      Each rec gets a ~200-word spec and a list of file scaffolds.

  3.  Writes every rec to brain_strategic_recommendations.

  4.  (Optional, DCHUB_BRAIN_STRATEGIC_DRAFT_PR=1) For up to 5 of the
      recs/week, opens a DRAFT scaffold PR via the same GitHub helpers
      brain_backlog_admin uses. Each PR ships:
        • A spec markdown file under docs/strategic/<slug>.md
        • An empty routes/_proposed_<slug>.py boilerplate with an
          ast.parse-verified Blueprint stub + TODO docstring
      The scaffold PR is NEVER auto-merged. Humans review + flesh out.

SAFETY
------
  · DCHUB_BRAIN_STRATEGIC_DISABLE=1            kill switch
  · DCHUB_BRAIN_STRATEGIC_DRAFT_PR=1           opt-in for scaffold PRs
  · BRAIN_STRATEGIC_WEEKLY_PR_CAP=5            per-week PR cap (NOT per
                                                day — strategic recs are
                                                lower-rate by design)
  · Single Claude call per run (cost-capped: see _estimate_cost)
  · Idempotent: same week_of_iso skips the Claude call (re-renders
                from existing rows). Force re-compute with ?force=1.
  · ANY Claude call failure → degrade chain via brain_models.fallback_for
  · Output goes through json.loads with fence-strip; bad JSON → row
    skipped, never crash the cron.
  · POST /run is admin-gated; GET /digest can be admin or signed-token.

ESTIMATED COST (Opus 4.8 reasoning tier):
  · Input context: ~25 KB JSON → ~6,250 tokens
  · Output: ~3,000 tokens (7 recs × ~400 tokens each)
  · 1M-context beta header NOT required (we cap at 25KB)
  · Per call: ~$0.50 in + ~$0.45 out = ~$0.95
  · Per week: 1 call = ~$1/week
  · Per year: ~$50  (vs. the $5-10/run feared in spec)
  · Falls back to Sonnet-4.5 at ~$0.15/run if Opus 404s.

The "$5-10/run" worry was overblown — we keep the context tight by
truncating each input source to a fixed token budget. See _truncate.

Spec source: 2026-06-06 brief "Arm the brain: STRATEGIC, not just
reactive". Companion file: routes/brain_weekly_digest.py renders the
Monday email.
"""
from __future__ import annotations

import ast
import base64
import datetime as _dt
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

brain_strategic_bp = Blueprint("brain_strategic_planner", __name__)


# ─── Config ─────────────────────────────────────────────────────────

_INTERNAL_BASE = (os.environ.get("INTERNAL_BASE_URL")
                  or "http://localhost:8080").rstrip("/")
_RAILWAY_BASE = "https://dchub-backend-production.up.railway.app"
_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()

# Per-input context budgets (in chars). Total ~ 22 KB which is well
# under the 100 KB the Opus 4.8 1M-context could support but keeps the
# cost predictable. See module docstring for cost math.
_CTX_BUDGET = {
    "funnel":       4000,
    "page_health":  4000,
    "feedback":     4000,
    "backlog":      3500,
    # 2026-06-07: bumped 3000→5000 to accommodate the new 3-layer
    # competitor envelope (presence + universe + signal). The L6
    # self-critique flagged the old 3000-byte budget as part of why
    # competitor_lacks were getting interpolated from tool names.
    "competitors":  5000,
    "self_model":   2500,
    "recent_recs":  1500,
    # 2026-06-07 ROUND 2: pr_outcomes feeds the brain's own track
    # record into the synthesis so it learns from past attempts.
    # ~2 KB holds the last 30d of merged brain-authored PRs +
    # before/after sentinel grades.
    "pr_outcomes":  2000,
    # Task #161 (2026-06-07): brain reads its own dashboards.
    # The daily self-perception module writes wins/losses/adjustments
    # to brain_self_perception. The weekly L6 prompt now sees the
    # last 14d of those self-assessments so the brain factors its
    # own judgment into the next strategic plan. Recursive self-
    # improvement loop closes. ~2 KB holds the last 14d.
    "self_perception": 2000,
}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("DCHUB_BRAIN_STRATEGIC_DISABLE"))


def _draft_pr_enabled() -> bool:
    return _truthy(os.environ.get("DCHUB_BRAIN_STRATEGIC_DRAFT_PR"))


def _weekly_pr_cap() -> int:
    try:
        return max(0, int(os.environ.get(
            "BRAIN_STRATEGIC_WEEKLY_PR_CAP", "5")))
    except Exception:
        return 5


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    import hmac
    return hmac.compare_digest(provided, expected)


# ─── DB ─────────────────────────────────────────────────────────────

def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _week_of_iso(at: Optional[_dt.datetime] = None) -> _dt.date:
    """Return the Monday-anchored ISO week date for `at` (default now)."""
    at = at or _dt.datetime.now(_dt.timezone.utc)
    return (at - _dt.timedelta(days=at.weekday())).date()


def _run_id_for(week_of: _dt.date) -> str:
    return "strategic-" + week_of.strftime("%Y-W%U")


# ─── Context gatherers ──────────────────────────────────────────────

def _http_get_json(path: str, timeout: int = 8) -> dict:
    """Fetch JSON from a backend route. Prefers the EXTERNAL Railway base
    over loopback because the synthesis runs inside a request thread —
    a loopback GET against the same gunicorn worker would deadlock /
    serialize. The external Railway base goes through CF + Railway edge
    so it's served by a sibling worker (or this one's idle slot)."""
    import urllib.request
    headers = {"X-Internal-Probe": "1",
               "User-Agent": "dchub-brain-strategic/1.0"}
    ak = _admin_key()
    if ak:
        headers["X-Admin-Key"] = ak
    # External first — see docstring. Falls back to loopback only if
    # external probes fail (e.g. local pytest without internet).
    for base in (_RAILWAY_BASE, _INTERNAL_BASE):
        try:
            req = urllib.request.Request(f"{base}{path}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="ignore")
                parsed = json.loads(raw)
                # An endpoint legitimately returning an empty body still
                # counts as a successful probe — don't fall through to
                # loopback (would just deadlock).
                return parsed if isinstance(parsed, (dict, list)) else {}
        except Exception:
            continue
    return {}


def _truncate(obj: Any, budget: int) -> str:
    """Serialize + hard-truncate to a char budget, marked clearly."""
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) <= budget:
        return s
    return s[: budget - 18] + " /* …truncated */"


def _gather_strategic_context() -> dict:
    """Pull the 6 inputs the prompt feeds on. All fail-soft → empty
    dicts so a missing endpoint never zeros out the run."""
    # Funnel (7d + 30d): /api/v1/mcp/funnel returns aggregate today;
    # /api/v1/mcp/conversion-funnel?days=30 holds the wider view.
    funnel_now = _http_get_json("/api/v1/mcp/funnel")
    funnel_30d = _http_get_json("/api/v1/mcp/conversion-funnel?days=30")

    # Page-integrity manifest (69 pages tracked per the screenshot)
    page_health = _http_get_json("/api/v1/sentinel/page-integrity")

    # Customer asks — top voted open feedback (not spam, not shipped)
    feedback = _http_get_json(
        "/api/v1/feedback/list?status=open&sort=top&limit=20")

    # Stuck-issue queue — 67 stuck × 23 cycles untried per brain_backlog
    backlog = _http_get_json("/api/v1/admin/brain/backlog")

    # Competitor signal — MCP presence + registry intel + public manifests
    competitors = _gather_competitor_context()

    # Self-model — what the brain believes about itself
    self_model = _http_get_json("/api/v1/brain/self-model")

    # Recent strategic recs (so we don't repeat ourselves week-to-week)
    recent_recs = _read_recent_recs(weeks_back=4)

    # ROUND 2 (2026-06-07): the brain's own track record. Reads the
    # brain_pr_outcomes table directly (filled by brain_pr_outcome_
    # monitor twice daily). The synthesis prompt now sees what the
    # brain proposed in past weeks AND whether sentinel approved or
    # regressed after merge → "last time I patched X for finding Y,
    # sentinel regressed → try Z this round".
    pr_outcomes = _gather_outcomes_context(window_days=30)

    # Task #161 (2026-06-07): brain reads its own dashboards. The daily
    # self-perception module writes wins/losses/adjustments to
    # brain_self_perception. Pulling the last 14d here closes the
    # recursive self-improvement loop: the L6 synthesis now sees how
    # the brain self-assessed yesterday + the past two weeks. Fail-soft
    # if the new module / table isn't deployed yet — empty envelope.
    try:
        from routes.brain_self_perception import (
            gather_self_perception_context as _gsp,
        )
        self_perception = _gsp(window_days=14)
    except Exception as _gsp_e:
        logger.debug(
            "L6 strategic: self_perception import skipped: %s", _gsp_e)
        self_perception = {"_note": "module_not_loaded"}

    # Task #170 (2026-06-07): Brain ROUND 3 — code inventory + arch
    # proposals. The daily scanner writes hotspots; the proposer writes
    # 200-word refactor specs. Pulling both here lets L6 see "your own
    # codebase has these untested critical paths" AND "you proposed
    # these refactors in the last 30d — don't repeat them". Fail-soft.
    try:
        from routes.brain_code_scanner import (
            gather_code_inventory_context as _gci,
        )
        code_inventory = _gci()
    except Exception as _gci_e:
        logger.debug(
            "L6 strategic: code_inventory import skipped: %s", _gci_e)
        code_inventory = {"_note": "module_not_loaded"}
    try:
        from routes.brain_architecture_proposer import (
            gather_recent_proposals as _grp,
        )
        recent_arch_proposals = _grp(window_days=30)
    except Exception as _grp_e:
        logger.debug(
            "L6 strategic: arch_proposals import skipped: %s", _grp_e)
        recent_arch_proposals = []

    return {
        "funnel":          {"now": funnel_now, "d30": funnel_30d},
        "page_health":     page_health,
        "feedback":        feedback,
        "backlog":         backlog,
        "competitors":     competitors,
        "self_model":      self_model,
        "recent_recs":     recent_recs,
        "pr_outcomes":     pr_outcomes,
        "self_perception": self_perception,
        "code_inventory":      code_inventory,
        "recent_arch_proposals": recent_arch_proposals,
    }


def _gather_outcomes_context(window_days: int = 30) -> dict:
    """Pull brain_pr_outcomes for the synthesis context.

    Returns {summary: {success_rate, regression_count, by_outcome},
             recent: [{pr, files, outcome, before, after, ...}, ...]}.

    Fail-soft → empty envelope on miss (table may not exist yet on
    first deploy)."""
    c = _get_db()
    if c is None:
        return {"_note": "no_db"}
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT outcome, COUNT(*) FROM brain_pr_outcomes
                    WHERE brain_authored = TRUE
                      AND created_at > NOW() - INTERVAL '%s days'
                    GROUP BY outcome""", (window_days,))
            by_outcome = {r[0]: int(r[1]) for r in cur.fetchall() or []}
            cur.execute(
                """SELECT pr_number, pr_url, pr_title, sentinel_endpoint,
                          sentinel_before_grade, sentinel_after_grade,
                          outcome, regression_details, files_changed,
                          merged_at
                     FROM brain_pr_outcomes
                    WHERE brain_authored = TRUE
                      AND created_at > NOW() - INTERVAL '%s days'
                    ORDER BY COALESCE(merged_at, created_at) DESC
                    LIMIT 25""", (window_days,))
            recent = []
            for r in cur.fetchall() or []:
                recent.append({
                    "pr_number":  r[0],
                    "pr_url":     r[1],
                    "title":      (r[2] or "")[:120],
                    "endpoint":   r[3],
                    "before":     (float(r[4]) if r[4] is not None else None),
                    "after":      (float(r[5]) if r[5] is not None else None),
                    "outcome":    r[6],
                    "regression": (r[7] or "")[:200],
                    "files":      (r[8] or "")[:300],
                    "merged_at":  str(r[9]) if r[9] else None,
                })
        merged_n = sum(by_outcome.get(k, 0) for k in
                       ("success", "regression", "unknown", "deploy_fail"))
        success = by_outcome.get("success", 0)
        return {
            "window_days":      window_days,
            "by_outcome":       by_outcome,
            "merged_total":     merged_n,
            "success_rate":     (round(success / merged_n, 3)
                                  if merged_n else None),
            "regression_rate":  (round(by_outcome.get("regression", 0)
                                        / merged_n, 3)
                                  if merged_n else None),
            "recent":           recent,
        }
    except Exception as e:
        logger.warning("L6 strategic: pr_outcomes read failed: %s", e)
        return {"_note": "read_failed", "error": str(e)[:160]}
    finally:
        try: c.close()
        except Exception: pass


# ─── Competitor context (2026-06-07) ───────────────────────────────
# The L6 self-critique on its 2026-06-01 live run flagged: "Competitor
# signal context is empty, so all competitor_lacks entries are
# interpolated from tool names rather than cited evidence." Fix:
# wire the planner to the live /mcp/presence/recent endpoint
# (mcp_presence_crawler) + a small curated set of competitor categories
# (other MCP servers + DC/energy/grid registries) so the prompt now has
# something concrete to ground its "DC Hub lacks X" reasoning on.

# Curated competitor universe (NOT scraped — static catalog of the
# real competitive landscape so the prompt can cite specific names).
# Update sparingly; this is the "what the brain knows about its
# market" baseline.
_COMPETITOR_UNIVERSE = {
    "data_center_registries": [
        {"name": "Baxtel", "url": "https://baxtel.com",
         "focus": "global data-center directory, audited specs"},
        {"name": "DCByte", "url": "https://dcbyte.com",
         "focus": "paid market-research subscription (~$30K/yr)"},
        {"name": "DataCenters.com", "url": "https://datacenters.com",
         "focus": "free directory, broker-funded"},
        {"name": "Data Center Map",
         "url": "https://www.datacentermap.com",
         "focus": "free global map, basic facts only"},
        {"name": "Data Center Catalog",
         "url": "https://datacentercatalog.com",
         "focus": "free directory, limited intel"},
    ],
    "energy_grid_data": [
        {"name": "Wood Mackenzie",
         "url": "https://www.woodmac.com",
         "focus": "enterprise energy intel ($$$)"},
        {"name": "ERCOT public API",
         "url": "https://www.ercot.com/mp/data-products",
         "focus": "Texas grid data — free but rate-limited"},
        {"name": "PJM Data Miner 2",
         "url": "https://dataminer2.pjm.com",
         "focus": "PJM ISO data — free, API"},
        {"name": "EIA OpenData",
         "url": "https://www.eia.gov/opendata/",
         "focus": "US energy data — free reference"},
    ],
    "mcp_directories": [
        {"name": "Smithery",   "url": "https://smithery.ai"},
        {"name": "Glama",      "url": "https://glama.ai/mcp"},
        {"name": "LobeHub",    "url": "https://lobehub.com/mcp"},
        {"name": "PulseMCP",   "url": "https://pulsemcp.com"},
        {"name": "mcp.so",     "url": "https://mcp.so"},
    ],
    "what_dc_hub_uniquely_offers": [
        "Real-time ISO grid headroom for 21 grids (none of the above expose this for agents)",
        "Interconnection-queue snapshot (live FERC/ISO data)",
        "Per-rack water/power deal autopsies (Nautilus/MMR/Switch)",
        "33+ MCP tools cited by agent platforms (Groq, Perplexity, etc.)",
        "DCPI composite score for 300+ markets (rebuilt weekly)",
        "Versioned + cited data (vs PDF reports from incumbents)",
    ],
}


def _gather_competitor_context() -> dict:
    """Assemble the competitor-intel envelope the L6 prompt feeds on.

    Three layers (each layer is fail-soft → empty dict on miss):
      1. `presence` — live MCP-presence crawler snapshot
         (/api/v1/mcp/presence/recent) — what other MCP directories
         say about DC Hub, drift, recently-discovered registries
      2. `universe` — curated static catalog of the real competitive
         landscape (DC registries, energy data providers, MCP dirs)
      3. `signal` — recent mcp_presence_* findings from brain_findings
         (fallback path; keeps the legacy data flowing)

    Why static + live: the live crawler covers MCP directories well
    but doesn't track Baxtel/DCByte/etc. (they're not MCP servers).
    The static universe gives the prompt a CITED competitor list so
    competitor_lacks entries can reference real names + URLs."""

    # Layer 1: live MCP presence (now backed by the new /recent endpoint)
    presence = _http_get_json("/api/v1/mcp/presence/recent?days=30")
    # Backwards-compat alias (server registered both paths)
    if not presence:
        presence = _http_get_json("/api/v1/mcp-presence/recent?days=30")
    # Last-ditch fallback: the /status endpoint always works
    if not presence:
        presence = _http_get_json("/api/v1/mcp-presence/status")

    # Layer 3: brain_findings already-filed competitor signals
    findings = _http_get_json(
        "/api/v1/brain/findings?issue_like=mcp_presence&limit=20")

    envelope = {
        "presence": presence or {"_note": "presence_endpoint_unreachable"},
        "universe": _COMPETITOR_UNIVERSE,
        "signal":   findings or {"_note": "no_competitor_findings_yet"},
        "_layers":  ["presence", "universe", "signal"],
        "_pulled_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }

    # Defensive completeness flag — the prompt builder uses this to
    # decide whether competitor_lacks should be marked low-confidence.
    has_real_data = bool(
        (presence and isinstance(presence, dict)
         and (presence.get("active_registries")
              or presence.get("recently_discovered")))
        or (findings and isinstance(findings, (list, dict))
            and findings)
    )
    envelope["_has_real_data"] = has_real_data
    return envelope


def _read_recent_recs(weeks_back: int = 4) -> list:
    """Pull the last ~weeks_back weeks of strategic recs so the prompt
    can de-dup against its own history."""
    c = _get_db()
    if c is None:
        return []
    cutoff = _dt.date.today() - _dt.timedelta(weeks=weeks_back)
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT week_of, kind, title, status, pr_url
                     FROM brain_strategic_recommendations
                    WHERE week_of >= %s
                    ORDER BY week_of DESC, id DESC
                    LIMIT 30""", (cutoff,))
            rows = cur.fetchall() or []
        return [
            {"week_of": str(r[0]), "kind": r[1], "title": r[2],
             "status": r[3], "pr_url": r[4]}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─── Prompt builder ─────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the DC Hub Brain Layer-6 — the Strategic Synthesizer.

Layers 1-5 already handle tactical: detect bug → propose 1-line fix →
open a draft PR. You are NOT that. You are the brain's co-founder voice:
"given what I see in funnel + customer asks + competitors + the backlog
that's been stuck for weeks — what should DC Hub ship NEXT to move the
business?"

Output a single JSON object with EXACTLY this shape (no markdown fences,
no commentary, just the JSON):

{
  "summary": "<2-3 sentence state of the strategic landscape>",
  "top_gaps_4w": [
    {
      "title": "<short noun phrase, <60 chars>",
      "spec": "<~180-word spec: what to build, why it matters, what success looks like>",
      "evidence_keys": ["<source.path1>", "<source.path2>"],
      "dollar_lift_est_usd": <number or null>,
      "confidence": "high|medium|low",
      "file_scaffold": [
        {"path": "routes/<file>.py", "kind": "blueprint_stub"},
        {"path": "docs/strategic/<slug>.md", "kind": "spec_md"}
      ]
    }
  ],
  "competitor_lacks": [
    {
      "title": "<short noun phrase>",
      "spec": "<~150-word spec: competitor X has Y, DC Hub lacks Z, here's the smallest version we could ship>",
      "competitor": "<dcbyte|baxtel|datacenters.com|cushman|other>",
      "evidence_keys": ["..."],
      "confidence": "high|medium|low"
    }
  ],
  "funnel_optimizations": [
    {
      "title": "<short noun phrase>",
      "spec": "<~150-word spec: the leak, the fix, the quantified lift estimate, how to measure>",
      "current_stage_metric": "<e.g. 'paywall→signal conversion=0.4%'>",
      "projected_stage_metric": "<e.g. 'paywall→signal conversion=2%'>",
      "dollar_lift_est_usd": <number or null>,
      "confidence": "high|medium|low"
    }
  ],
  "wildcard_bet": {
    "title": "<short noun phrase>",
    "spec": "<~250-word spec: the bet, why it differentiates DC Hub long-term, what could go wrong>",
    "horizon_months": <integer>,
    "confidence": "low"
  },
  "stop_doing": "<one detector/feature/page that's adding noise without value, or null>",
  "self_critique": "<one sentence: a weakness in this synthesis you'd flag for the operator>"
}

RULES
─────
1. Top-3 gaps means EXACTLY 3 in top_gaps_4w. Same for competitor_lacks
   and funnel_optimizations.
2. Every spec MUST cite at least 1 evidence_key drawn from the context
   below. Bullshit speculation gets you fired.
3. dollar_lift_est_usd is a real number when you can defend it from the
   funnel data, null otherwise. Don't fabricate revenue.
4. Do NOT repeat any title that appears in ctx.recent_recs from the
   past 4 weeks (the brain should not whiplash week-to-week).
5. confidence='high' is reserved for chains you can cite ≥2 evidence
   keys for and where the fix is unambiguous.
6. file_scaffold paths must be SAFE: no main.py, no auth files, no
   secrets. Prefer new routes/_proposed_*.py files + docs/strategic/*.md.
7. Reply with ONLY the JSON object. No prose before or after."""


def _build_prompt(ctx: dict) -> str:
    """Assemble the full prompt with per-input truncation."""
    sections = []
    sections.append("FUNNEL (7-day + 30-day window):\n" +
                    _truncate(ctx.get("funnel"), _CTX_BUDGET["funnel"]))
    sections.append("PAGE HEALTH (sentinel/page-integrity rollup):\n" +
                    _truncate(ctx.get("page_health"),
                              _CTX_BUDGET["page_health"]))
    sections.append("CUSTOMER ASKS (top-voted open feedback):\n" +
                    _truncate(ctx.get("feedback"),
                              _CTX_BUDGET["feedback"]))
    sections.append("BRAIN BACKLOG (stuck × 23 cycles + L5 proposals):\n" +
                    _truncate(ctx.get("backlog"),
                              _CTX_BUDGET["backlog"]))
    sections.append("COMPETITOR SIGNAL (MCP presence drift + new sites):\n" +
                    _truncate(ctx.get("competitors"),
                              _CTX_BUDGET["competitors"]))
    sections.append("SELF-MODEL (what the brain believes about itself):\n" +
                    _truncate(ctx.get("self_model"),
                              _CTX_BUDGET["self_model"]))
    sections.append("RECENT RECS (past 4 weeks — do not repeat):\n" +
                    _truncate(ctx.get("recent_recs"),
                              _CTX_BUDGET["recent_recs"]))
    # ROUND 2 (2026-06-07): own track record. The prompt should explicitly
    # weigh past patches that REGRESSED sentinel as a signal NOT to
    # propose similar patterns. See _gather_outcomes_context.
    sections.append("PR OUTCOMES (past 30d — brain's own track record; "
                    "learn from regressions):\n" +
                    _truncate(ctx.get("pr_outcomes"),
                              _CTX_BUDGET["pr_outcomes"]))
    # Task #161 (2026-06-07): brain's own daily self-assessments. The
    # prompt now sees "here's how you self-assessed yesterday + the
    # past 2 weeks". Wins / losses / adjustments are honest grades the
    # brain wrote about its own output. Use them to AVOID repeating
    # the same losses next week — and to ESCALATE adjustments the
    # operator hasn't acted on.
    sections.append("SELF-PERCEPTION (past 14d daily self-assessments — "
                    "your own wins/losses/adjustments; if a loss repeats "
                    "across days the operator hasn't acted → escalate):\n"
                    + _truncate(ctx.get("self_perception"),
                                _CTX_BUDGET["self_perception"]))

    return ("\n\n".join(sections) +
            "\n\n──── End context. Reply with the JSON object ONLY. ────")


# ─── Claude call (with fallback chain) ──────────────────────────────

def _call_claude(prompt: str) -> Optional[dict]:
    """Call Claude with the strategic prompt. Walks the brain_models
    fallback chain on 404 (opus-4-8 → sonnet-4-5 → haiku-4-5)."""
    if not _ANTHROPIC_KEY:
        logger.warning("L6 strategic: ANTHROPIC_API_KEY unset")
        return None

    try:
        from routes.brain_models import brain_model_for, resolve_chain
        from utils.anthropic_helper import anthropic_messages_url
    except Exception as e:
        logger.warning("L6 strategic: model registry import failed: %s", e)
        return None

    chain = resolve_chain(brain_model_for("reasoning"))
    last_err = None
    for model in chain:
        try:
            import requests
            r = requests.post(
                anthropic_messages_url(),
                headers={
                    "x-api-key":         _ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "User-Agent":        "dchub-brain-strategic/1.0",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    # 2026-06-08 FIX: was 3200 — too small for the ~20-rec JSON
                    # once the prompt was enriched (r63 history+rejection memory).
                    # The response truncated mid-string (~char 8-10k) → "Unterminated
                    # string" JSON parse failure → all models failed → no recs persisted
                    # → the weekly briefing silently never delivered. 16000 fits the
                    # full synthesis with headroom (safe for opus-4-8/sonnet-4-5/haiku-4-5).
                    "max_tokens": 16000,
                    "system":     _SYSTEM_PROMPT,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )
            if r.status_code == 404:
                last_err = f"{model}:404"
                logger.info(
                    "L6 strategic: %s 404, walking fallback chain", model)
                continue
            if r.status_code != 200:
                last_err = f"{model}:{r.status_code}:{r.text[:200]}"
                logger.warning("L6 strategic: %s", last_err)
                continue
            body = r.json() or {}
            text = "".join(
                b.get("text", "")
                for b in (body.get("content") or [])
                if b.get("type") == "text"
            ).strip()
            if text.startswith("```"):
                text = text.split("```", 2)[1] if "```" in text else text
                if text.startswith("json"):
                    text = text[4:].lstrip("\n")
                if text.endswith("```"):
                    text = text.rsplit("```", 1)[0]
            try:
                parsed = json.loads(text)
                parsed["_model_used"] = model
                parsed["_input_tokens_est"] = len(prompt) // 4
                parsed["_output_tokens_est"] = len(text) // 4
                return parsed
            except Exception as je:
                logger.warning(
                    "L6 strategic: JSON parse failed (%s); raw first 200 "
                    "chars: %s", je, text[:200])
                last_err = f"parse:{je}"
                continue
        except Exception as e:
            last_err = f"{model}:exc:{e}"
            logger.warning("L6 strategic: %s exception: %s", model, e)
            continue
    logger.error("L6 strategic: all models failed; last_err=%s", last_err)
    return None


# ─── Persistence ────────────────────────────────────────────────────

_KIND_FIELD_MAP = [
    ("strategic_gap_4w",    "top_gaps_4w"),
    ("competitor_lack",     "competitor_lacks"),
    ("funnel_optimization", "funnel_optimizations"),
]


def _persist_recommendations(payload: dict, week_of: _dt.date,
                              run_id: str) -> int:
    """Write one row per recommendation. Returns count inserted."""
    if not payload:
        return 0
    c = _get_db()
    if c is None:
        return 0
    inserted = 0
    try:
        with c.cursor() as cur:
            for kind, field in _KIND_FIELD_MAP:
                items = payload.get(field) or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    title = (item.get("title") or "").strip()[:200]
                    if not title:
                        continue
                    spec = (item.get("spec") or "")[:6000]
                    scaffold = item.get("file_scaffold") or []
                    dollar = item.get("dollar_lift_est_usd")
                    try:
                        dollar = float(dollar) if dollar is not None else None
                    except Exception:
                        dollar = None
                    conf = (item.get("confidence") or "low")
                    conf_num = {"high": 0.85, "medium": 0.6,
                                "low": 0.35}.get(conf, 0.35)
                    evid = item.get("evidence_keys") or []
                    cur.execute(
                        """INSERT INTO brain_strategic_recommendations(
                            run_id, week_of, kind, title, spec_md,
                            file_scaffold, dollar_lift_est, confidence,
                            evidence_keys, status, strategy_payload
                          ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                          )""",
                        (run_id, week_of, kind, title, spec,
                         json.dumps(scaffold), dollar, conf_num,
                         json.dumps(evid), "new", json.dumps(item)))
                    inserted += 1
            # Wildcard bet (singleton)
            wc = payload.get("wildcard_bet") or {}
            if isinstance(wc, dict) and (wc.get("title") or "").strip():
                cur.execute(
                    """INSERT INTO brain_strategic_recommendations(
                        run_id, week_of, kind, title, spec_md,
                        file_scaffold, dollar_lift_est, confidence,
                        evidence_keys, status, strategy_payload
                      ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                      )""",
                    (run_id, week_of, "wildcard_bet",
                     wc.get("title", "")[:200],
                     (wc.get("spec") or "")[:6000],
                     json.dumps([]), None, 0.35,
                     json.dumps([]), "new", json.dumps(wc)))
                inserted += 1
            # Summary row (stop_doing + self_critique under a meta kind)
            meta = {
                "summary":       payload.get("summary"),
                "stop_doing":    payload.get("stop_doing"),
                "self_critique": payload.get("self_critique"),
                "model_used":    payload.get("_model_used"),
                "in_tokens_est": payload.get("_input_tokens_est"),
                "out_tokens_est": payload.get("_output_tokens_est"),
            }
            cur.execute(
                """INSERT INTO brain_strategic_recommendations(
                    run_id, week_of, kind, title, spec_md,
                    file_scaffold, dollar_lift_est, confidence,
                    evidence_keys, status, strategy_payload
                  ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                  )""",
                (run_id, week_of, "synthesis_meta",
                 (payload.get("summary") or "Weekly synthesis")[:200],
                 (payload.get("summary") or "")[:6000],
                 json.dumps([]), None, 0.5,
                 json.dumps([]), "meta", json.dumps(meta)))
            inserted += 1
        try:
            c.commit()
        except Exception:
            pass
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.error("L6 strategic: persist failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass
    return inserted


def _read_recs_for(week_of: _dt.date) -> list:
    c = _get_db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, week_of, kind, title, spec_md,
                          file_scaffold, dollar_lift_est, confidence,
                          evidence_keys, pr_url, pr_number, status,
                          strategy_payload, created_at
                     FROM brain_strategic_recommendations
                    WHERE week_of = %s
                    ORDER BY
                      CASE kind
                        WHEN 'synthesis_meta'        THEN 0
                        WHEN 'strategic_gap_4w'      THEN 1
                        WHEN 'funnel_optimization'   THEN 2
                        WHEN 'competitor_lack'       THEN 3
                        WHEN 'wildcard_bet'          THEN 4
                        ELSE 5
                      END, id ASC""", (week_of,))
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            try:
                scaffold = json.loads(r[6]) if r[6] else []
            except Exception:
                scaffold = []
            try:
                evid = json.loads(r[9]) if r[9] else []
            except Exception:
                evid = []
            try:
                payload = (r[13] if isinstance(r[13], dict)
                            else (json.loads(r[13]) if r[13] else {}))
            except Exception:
                payload = {}
            out.append({
                "id":             r[0],
                "run_id":         r[1],
                "week_of":        str(r[2]),
                "kind":           r[3],
                "title":          r[4],
                "spec_md":        r[5],
                "file_scaffold":  scaffold,
                "dollar_lift":    float(r[7]) if r[7] is not None else None,
                "confidence":     float(r[8]) if r[8] is not None else None,
                "evidence_keys":  evid,
                "pr_url":         r[10],
                "pr_number":      r[11],
                "status":         r[12],
                "strategy_payload": payload,
                "created_at":     r[14].isoformat() if r[14] else None,
            })
        return out
    except Exception as e:
        logger.error("L6 strategic: read failed: %s", e)
        return []
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─── Scaffold PR opener (optional) ──────────────────────────────────

_SCAFFOLD_BP_TEMPLATE = '''"""
{slug}.py — STRATEGIC SCAFFOLD (auto-drafted by Brain L6, week {week_of}).

This file is a SCAFFOLD only. The brain wrote it to start the conversation
on a feature it thinks DC Hub should ship next. It does not implement the
feature — a human reviews the spec below and turns this into a real route.

SPEC (from brain L6 synthesis):
{spec_block}

Evidence cited by the brain when proposing this:
{evidence_block}

To unblock: implement the routes and remove the NotImplementedError below.
"""
from flask import Blueprint, jsonify

{var_name} = Blueprint("{slug}", __name__)


@{var_name}.route("/api/v1/strategic-scaffold/{slug}", methods=["GET"])
def _scaffold_health():
    """Health probe so the blueprint can register without 500ing.
    Real endpoint TBD by the human reviewer."""
    return jsonify(
        ok=False,
        scaffold=True,
        message=("This is a strategic scaffold drafted by Brain L6. "
                  "It is not implemented yet."),
        spec_doc="docs/strategic/{slug}.md",
    ), 501
'''

_SCAFFOLD_MD_TEMPLATE = '''# Strategic recommendation: {title}

**Drafted by:** Brain Layer-6 (Strategic Synthesis)
**Week of:** {week_of}
**Kind:** {kind}
**Confidence:** {confidence}
**Est. lift:** {dollar_block}

## Spec

{spec}

## Evidence cited by the brain

{evidence_block}

## Suggested file scaffold

{scaffold_block}

---
*Auto-generated by routes/brain_strategic_planner.py. Edit and ship —
the brain has already opened the scaffold PR; a human must flesh it out.*
'''


def _slugify(title: str) -> str:
    s = "".join(c if c.isalnum() else "-" for c in (title or "").lower())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:50] or "strategic-rec"


def _open_scaffold_pr(rec: dict) -> dict:
    """Draft a PR with the spec MD + an empty Blueprint stub. Mirrors
    brain_backlog_admin._open_draft_pr_for_proposal but for a NEW file
    pair instead of a single-file patch."""
    try:
        from routes.brain_pr_opener import (
            _get_default_branch_sha, _create_branch, _commit_file,
            _gh, _GITHUB_TOKEN, _GITHUB_REPO,
        )
    except Exception as e:
        return {"ok": False, "error": f"pr_opener import: {e}"}

    if not _GITHUB_TOKEN:
        return {"ok": False, "error": "GITHUB_TOKEN unset"}

    title = (rec.get("title") or "").strip()
    if not title:
        return {"ok": False, "skipped": "no_title"}

    slug = _slugify(title)
    week_of = rec.get("week_of") or str(_week_of_iso())
    kind = rec.get("kind") or "strategic_rec"
    spec = rec.get("spec_md") or ""
    evid_list = rec.get("evidence_keys") or []
    scaffold = rec.get("file_scaffold") or []
    conf = rec.get("confidence")
    dollar = rec.get("dollar_lift")

    evidence_block = ("\n".join(f"- `{e}`" for e in evid_list)
                      if evid_list else "_(no evidence keys cited)_")
    scaffold_block = ("\n".join(
        f"- `{s.get('path', '?')}` ({s.get('kind', '?')})"
        for s in scaffold if isinstance(s, dict))
        if scaffold else "_(none specified)_")
    spec_block = "\n  ".join(spec.splitlines()[:30]) or "(spec missing)"
    dollar_block = (f"${dollar:,.0f}" if dollar is not None
                    else "_(not quantified)_")
    var_name = f"strategic_{slug.replace('-', '_')}_bp"
    md_path = f"docs/strategic/{slug}.md"
    py_path = f"routes/_proposed_{slug.replace('-', '_')}.py"

    py_content = _SCAFFOLD_BP_TEMPLATE.format(
        slug=slug, week_of=week_of, var_name=var_name,
        spec_block=spec_block, evidence_block=evidence_block,
    )
    md_content = _SCAFFOLD_MD_TEMPLATE.format(
        title=title, week_of=week_of, kind=kind,
        confidence=conf if conf is not None else "?",
        dollar_block=dollar_block, spec=spec,
        evidence_block=evidence_block, scaffold_block=scaffold_block,
    )

    # ast.parse the Python scaffold before pushing — same syntax gate
    # the L5 backlog admin uses.
    try:
        ast.parse(py_content)
    except SyntaxError as se:
        return {"ok": False,
                "skipped": f"scaffold ast.parse failed: {se}"[:200]}

    base_sha = _get_default_branch_sha()
    if not base_sha:
        return {"ok": False, "error": "could not read main SHA"}
    ts = int(time.time())
    branch = f"brain-l6/strategic-{slug}-{ts}"
    if not _create_branch(branch, base_sha):
        return {"ok": False, "error": f"branch create failed: {branch}"}

    commit_msg_md = (
        f"brain-l6(strategic-spec): {title}\n\n"
        f"Auto-drafted by Brain Layer-6 Strategic Synthesis "
        f"(week of {week_of}, kind={kind}).\n"
        f"Confidence: {conf}. Estimated lift: {dollar_block}.\n\n"
        f"This is a SPEC file. A scaffolded blueprint stub lands in the "
        f"same PR. Humans flesh out + un-draft + merge.\n"
    )
    if not _commit_file(md_path, md_content, commit_msg_md, branch, None):
        return {"ok": False, "error": f"commit failed on {md_path}"}

    commit_msg_py = (
        f"brain-l6(strategic-scaffold): {title}\n\n"
        f"Empty Blueprint stub that satisfies ast.parse. See "
        f"{md_path} for the spec.\n"
    )
    if not _commit_file(py_path, py_content, commit_msg_py, branch, None):
        return {"ok": False, "error": f"commit failed on {py_path}"}

    pr_title = f"[brain-l6 strategic-draft] {title}"
    pr_body = (
        f"## Brain Layer-6 strategic scaffold\n\n"
        f"**Kind:** `{kind}`  ·  **Week:** {week_of}  ·  "
        f"**Confidence:** {conf}  ·  **Est. lift:** {dollar_block}\n\n"
        f"### Why\n\n{(spec or '_(spec missing)_').strip()}\n\n"
        f"### What this PR contains\n\n"
        f"- `{md_path}` — the spec the brain wrote\n"
        f"- `{py_path}` — empty Blueprint stub (passes `ast.parse`)\n\n"
        f"### Evidence the brain cited\n\n{evidence_block}\n\n"
        f"### To take this from scaffold → shipped\n\n"
        f"1. Read the spec at `{md_path}`.\n"
        f"2. Flesh out `{py_path}` with the real routes.\n"
        f"3. Wire the blueprint into `main.py` (mirror the pattern at "
        f"e.g. `app.register_blueprint(brain_backlog_admin_bp)`).\n"
        f"4. Un-draft the PR + merge.\n\n"
        f"---\n"
        f"_Auto-generated by `routes/brain_strategic_planner.py`. This is "
        f"a DRAFT PR — humans merge. Kill switch: "
        f"`DCHUB_BRAIN_STRATEGIC_DISABLE=1` (full module) / "
        f"`DCHUB_BRAIN_STRATEGIC_DRAFT_PR=0` (PR opener only)._\n"
    )
    r = _gh("POST", f"/repos/{_GITHUB_REPO}/pulls", {
        "title": pr_title, "head": branch, "base": "main",
        "body": pr_body, "draft": True,
    })
    if r.status_code not in (200, 201):
        return {"ok": False,
                "error": f"PR create {r.status_code}: {r.text[:200]}",
                "branch": branch}
    pr = r.json()
    return {"ok": True, "pr_url": pr.get("html_url"),
            "pr_number": pr.get("number"), "branch": branch,
            "md_path": md_path, "py_path": py_path}


def _mark_pr_on_rec(rec_id: int, pr_url: str, pr_number: int) -> None:
    c = _get_db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE brain_strategic_recommendations
                      SET pr_url=%s, pr_number=%s, status='pr_drafted',
                          updated_at=NOW()
                    WHERE id=%s""",
                (pr_url, pr_number, rec_id))
        try:
            c.commit()
        except Exception:
            pass
    except Exception as e:
        logger.error("L6 strategic: mark_pr failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─── Top-level runners ──────────────────────────────────────────────

def run_strategic_synthesis(force: bool = False,
                              open_prs: Optional[bool] = None) -> dict:
    """End-to-end weekly run. Returns a summary dict (never raises).

    Idempotency: same week_of_iso skips the Claude call unless force=True.
    """
    started = _dt.datetime.now(_dt.timezone.utc)
    week_of = _week_of_iso(started)
    run_id = _run_id_for(week_of)

    if _kill_switch_on():
        return {"ok": False, "reason": "kill_switch_on",
                "env": "DCHUB_BRAIN_STRATEGIC_DISABLE=1"}

    # Idempotency: if we already have rows for this week, just re-render
    # unless force=True. Saves the Claude call + repeated DB inserts.
    existing = _read_recs_for(week_of)
    if existing and not force:
        return {"ok": True, "from_cache": True, "week_of": str(week_of),
                "run_id": run_id, "rec_count": len(existing),
                "note": ("Already computed this week; pass force=1 to "
                          "recompute."),
                "recommendations": existing}

    ctx = _gather_strategic_context()
    prompt = _build_prompt(ctx)
    prompt_chars = len(prompt)
    prompt_tokens_est = prompt_chars // 4

    t0 = time.time()
    payload = _call_claude(prompt)
    claude_ms = int((time.time() - t0) * 1000)
    if not payload:
        return {"ok": False, "reason": "claude_call_failed",
                "week_of": str(week_of), "run_id": run_id,
                "prompt_chars": prompt_chars,
                "prompt_tokens_est": prompt_tokens_est}

    inserted = _persist_recommendations(payload, week_of, run_id)
    recs = _read_recs_for(week_of)

    # Optionally open scaffold PRs
    pr_results = []
    do_prs = (_draft_pr_enabled() if open_prs is None else bool(open_prs))
    if do_prs:
        cap = _weekly_pr_cap()
        opened = 0
        # Only top_gaps_4w + competitor_lacks get PRs; funnel_optimization
        # is a config/copy change (no scaffold), wildcard is exploratory.
        eligible = [r for r in recs
                     if r["kind"] in ("strategic_gap_4w", "competitor_lack")
                     and not r.get("pr_url")]
        for rec in eligible:
            if opened >= cap:
                break
            res = _open_scaffold_pr(rec)
            if res.get("ok"):
                _mark_pr_on_rec(rec["id"], res["pr_url"], res["pr_number"])
                opened += 1
            pr_results.append({"rec_id": rec["id"],
                                "title": rec["title"], **res})

    finished = _dt.datetime.now(_dt.timezone.utc)
    summary = {
        "ok": True,
        "from_cache": False,
        "week_of":           str(week_of),
        "run_id":            run_id,
        "started":           started.isoformat(),
        "finished":          finished.isoformat(),
        "duration_ms":       int((finished - started).total_seconds() * 1000),
        "claude_ms":         claude_ms,
        "model_used":        payload.get("_model_used"),
        "prompt_chars":      prompt_chars,
        "prompt_tokens_est": prompt_tokens_est,
        "output_tokens_est": payload.get("_output_tokens_est"),
        "rec_inserted":      inserted,
        "rec_count":         len(recs),
        "pr_open_enabled":   do_prs,
        "prs_attempted":     len(pr_results),
        "prs_opened":        sum(1 for p in pr_results if p.get("ok")),
        "pr_results":        pr_results,
        "recommendations":   recs,
        "estimated_cost_usd": _estimate_cost(
            payload.get("_input_tokens_est") or prompt_tokens_est,
            payload.get("_output_tokens_est") or 0,
            payload.get("_model_used") or "claude-opus-4-8"),
    }
    return summary


def _estimate_cost(in_tok: int, out_tok: int, model: str) -> float:
    """Rough cost estimate in USD. Public Anthropic pricing as of
    2026-06: opus-4-8 ~ $15/M in + $75/M out;
              sonnet-4-5 ~ $3/M in + $15/M out;
              haiku-4-5 ~ $0.80/M in + $4/M out."""
    rates = {
        "claude-opus-4-8":     (15.0, 75.0),
        "claude-opus-4-7":     (15.0, 75.0),
        "claude-opus-4-5":     (15.0, 75.0),
        "claude-sonnet-4-5":   (3.0,  15.0),
        "claude-haiku-4-5":    (0.80, 4.0),
    }
    r_in, r_out = rates.get(model, (3.0, 15.0))
    return round(in_tok / 1e6 * r_in + out_tok / 1e6 * r_out, 4)


# ─── HTTP routes ────────────────────────────────────────────────────

_BG_STATE = {"last_started": None, "last_finished": None,
             "last_result": None, "running": False}


def _bg_run(force: bool, open_prs: Optional[bool]) -> None:
    _BG_STATE["running"] = True
    _BG_STATE["last_started"] = _dt.datetime.now(
        _dt.timezone.utc).isoformat()
    try:
        result = run_strategic_synthesis(force=force, open_prs=open_prs)
        _BG_STATE["last_result"] = result
    except Exception as e:
        logger.error("L6 strategic background run failed: %s", e,
                      exc_info=True)
        _BG_STATE["last_result"] = {"ok": False, "error": str(e)[:200]}
    finally:
        _BG_STATE["running"] = False
        _BG_STATE["last_finished"] = _dt.datetime.now(
            _dt.timezone.utc).isoformat()


@brain_strategic_bp.route(
    "/api/v1/admin/brain/strategic-synthesis/run", methods=["POST", "GET"])
def strategic_run():
    """Trigger the weekly synthesis. Admin-gated. Fire-and-forget pattern
    (L14 vintage) — returns 202 immediately while the Claude call runs
    in a background thread. Reason: Railway's HTTP gateway hard-caps
    requests at ~40s; an Opus 4.8 reasoning call routinely runs 60-120s.
    Poll GET /api/v1/brain/strategic-synthesis/latest to see the result.

    Synchronous mode available via sync=1 (only useful from a cron that
    already has a long timeout; web clients should never use this).

    Query/body params:
      force=1            recompute even if this week already exists
      open_prs=1|0       override the DCHUB_BRAIN_STRATEGIC_DRAFT_PR env
      sync=1             block until done (for cron use only)"""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    force = _truthy(request.args.get("force")) or _truthy(body.get("force"))
    op = request.args.get("open_prs") or body.get("open_prs")
    open_prs = None if op is None else _truthy(op)
    sync = _truthy(request.args.get("sync")) or _truthy(body.get("sync"))

    if sync:
        result = run_strategic_synthesis(force=force, open_prs=open_prs)
        return jsonify(result), 200

    if _BG_STATE.get("running"):
        return jsonify(
            ok=True, accepted=False,
            reason="background_run_already_in_flight",
            last_started=_BG_STATE.get("last_started"),
            hint=("Poll /api/v1/brain/strategic-synthesis/latest for "
                  "results."),
        ), 202

    import threading as _th
    _th.Thread(
        target=_bg_run, args=(force, open_prs),
        daemon=True, name="brain-l6-strategic",
    ).start()

    return jsonify(
        ok=True, accepted=True,
        started_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        week_of=str(_week_of_iso()),
        note=("Synthesis started in background. Poll "
              "GET /api/v1/brain/strategic-synthesis/latest in ~30-90s. "
              "Cost ~$0.50-$1 depending on which model fires."),
    ), 202


@brain_strategic_bp.route(
    "/api/v1/admin/brain/strategic-synthesis/bg-state", methods=["GET"])
def strategic_bg_state():
    """Read-only view of the background runner's last attempt."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    last = _BG_STATE.get("last_result") or {}
    # Strip the heavy recommendations list — caller can hit /latest for it
    light = {k: v for k, v in last.items()
              if k not in ("recommendations", "pr_results")}
    light["_rec_count_in_last_result"] = len(
        last.get("recommendations") or [])
    return jsonify(
        ok=True,
        running=bool(_BG_STATE.get("running")),
        last_started=_BG_STATE.get("last_started"),
        last_finished=_BG_STATE.get("last_finished"),
        last_result_summary=light,
    ), 200


@brain_strategic_bp.route(
    "/api/v1/brain/strategic-synthesis/latest", methods=["GET"])
def strategic_latest():
    """Read-only: return this week's recommendations (or the most recent
    week with data). Public — read access only, no admin gate."""
    week_of = _week_of_iso()
    recs = _read_recs_for(week_of)
    if not recs:
        c = _get_db()
        if c is not None:
            try:
                with c.cursor() as cur:
                    cur.execute(
                        """SELECT MAX(week_of)
                             FROM brain_strategic_recommendations""")
                    row = cur.fetchone()
                    if row and row[0]:
                        week_of = row[0]
                        recs = _read_recs_for(week_of)
            finally:
                try:
                    c.close()
                except Exception:
                    pass
    if not recs:
        return jsonify(
            ok=True, week_of=None,
            recommendations=[],
            note=("No strategic recommendations yet. POST "
                  "/api/v1/admin/brain/strategic-synthesis/run "
                  "with admin key to seed.")), 200
    # Group by kind for easy rendering
    grouped: dict[str, list] = {}
    for r in recs:
        grouped.setdefault(r["kind"], []).append(r)
    return jsonify(
        ok=True,
        week_of=str(week_of),
        rec_count=len(recs),
        recommendations=recs,
        by_kind=grouped,
    ), 200


@brain_strategic_bp.route(
    "/api/v1/brain/strategic-synthesis/history", methods=["GET"])
def strategic_history():
    """List the past N weeks of synthesis runs (titles only). Public."""
    try:
        weeks = int(request.args.get("weeks") or 12)
    except Exception:
        weeks = 12
    weeks = max(1, min(weeks, 52))
    cutoff = _dt.date.today() - _dt.timedelta(weeks=weeks)

    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT week_of,
                          COUNT(*) FILTER (WHERE kind != 'synthesis_meta'),
                          COUNT(*) FILTER (WHERE pr_url IS NOT NULL),
                          MAX(created_at)
                     FROM brain_strategic_recommendations
                    WHERE week_of >= %s
                    GROUP BY week_of
                    ORDER BY week_of DESC""", (cutoff,))
            rows = cur.fetchall() or []
        return jsonify(
            ok=True, weeks_requested=weeks,
            history=[
                {"week_of": str(r[0]), "rec_count": int(r[1] or 0),
                 "pr_count": int(r[2] or 0),
                 "computed_at": r[3].isoformat() if r[3] else None}
                for r in rows
            ],
        ), 200
    finally:
        try:
            c.close()
        except Exception:
            pass


@brain_strategic_bp.route(
    "/api/v1/admin/brain/strategic-synthesis/preview", methods=["GET"])
def strategic_preview():
    """Dry-run: gather context + return the prompt that WOULD be sent
    to Claude this week, without spending tokens. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    ctx = _gather_strategic_context()
    prompt = _build_prompt(ctx)
    return jsonify(
        ok=True, dry_run=True,
        week_of=str(_week_of_iso()),
        prompt_chars=len(prompt),
        prompt_tokens_est=len(prompt) // 4,
        system_prompt_chars=len(_SYSTEM_PROMPT),
        config={
            "kill_switch":   _kill_switch_on(),
            "pr_opener_on":  _draft_pr_enabled(),
            "weekly_pr_cap": _weekly_pr_cap(),
            "anthropic_key_set": bool(_ANTHROPIC_KEY),
        },
        context_summary={
            k: (len(_truncate(v, _CTX_BUDGET.get(k, 2000)))
                if k in _CTX_BUDGET else "(no budget)")
            for k, v in ctx.items()
        },
        prompt_first_500=prompt[:500],
        prompt_last_500=prompt[-500:],
    ), 200


@brain_strategic_bp.route(
    "/api/v1/admin/brain/strategic-synthesis/status", methods=["GET"])
def strategic_status():
    """Operator-facing config + health check."""
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        pr_opener_enabled=_draft_pr_enabled(),
        weekly_pr_cap=_weekly_pr_cap(),
        anthropic_key_set=bool(_ANTHROPIC_KEY),
        admin_key_set=bool(_admin_key()),
        env_overrides={
            "DCHUB_BRAIN_STRATEGIC_DISABLE":  "kill switch (1=off)",
            "DCHUB_BRAIN_STRATEGIC_DRAFT_PR": "opt-in PR opener (1=on)",
            "BRAIN_STRATEGIC_WEEKLY_PR_CAP":  f"current={_weekly_pr_cap()}",
        },
        endpoints={
            "POST /api/v1/admin/brain/strategic-synthesis/run":
                "trigger the weekly synthesis (admin)",
            "GET  /api/v1/admin/brain/strategic-synthesis/preview":
                "dry-run the prompt without spending tokens (admin)",
            "GET  /api/v1/admin/brain/strategic-synthesis/status":
                "this page",
            "GET  /api/v1/brain/strategic-synthesis/latest":
                "this week's recommendations (public read)",
            "GET  /api/v1/brain/strategic-synthesis/history":
                "past N weeks summary (public read)",
        },
    ), 200
