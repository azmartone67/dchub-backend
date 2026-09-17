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
  · BRAIN_STRATEGIC_EVIDENCE_DEDUP=0           disable evidence-subject
                                                dedup (ON by default; see
                                                the block above
                                                _EVIDENCE_GENERIC)
  · BRAIN_STRATEGIC_EVIDENCE_DEDUP_WEEKS=16    evidence ledger window
  · BRAIN_STRATEGIC_CITATION_GATE=0            disable draft-time citation
                                                validation (ON by default;
                                                see the block above
                                                evidence_root)
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
import re
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
    # 2026-08-31: 1500 -> 2500. Each recent_recs row now also carries
    # evidence_subjects (see rule 4); at the old budget the extra field
    # truncated rows away, weakening the title half of the same rule.
    "recent_recs":  2500,
    # Seven-levers #32 (2026-07-25): recidivist finding clusters — the
    # fixes that didn't hold, so the planner stops re-proposing them.
    "recidivism":   1200,
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
    # agentic-loop #65 part C (2026-08-22): NEGATIVE results — claims the
    # verifier REFUTED / the owner RETRACTED, proposals rejected as
    # duplicates, failed fixes — recalled from routes.brain_rag
    # .recall_negative_lessons and rendered under _WRONG_SECTION_TITLE.
    "refuted_claims": 2000,
}

# The one string the learn station's "what we got wrong" section is keyed
# on. routes.brain_rag.PLANNER_WRONG_SECTION_TITLE carries the same text so
# the agentic-loop shell can grep the preview prompt for exactly this; the
# pairing is pinned in tests/test_learn_station_shell65c.py.
_WRONG_SECTION_TITLE = "WHAT WE GOT WRONG (do not repeat)"


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("DCHUB_BRAIN_STRATEGIC_DISABLE"))


def _draft_pr_enabled() -> bool:
    return _truthy(os.environ.get("DCHUB_BRAIN_STRATEGIC_DRAFT_PR"))


def _evidence_dedup_enabled() -> bool:
    """Evidence-subject dedup is ON unless explicitly switched off. Unlike
    the other flags here it defaults to ENABLED: it only ever *withholds* a
    scaffold PR, so a wrong default costs one deferred draft, never a bad
    merge. Set BRAIN_STRATEGIC_EVIDENCE_DEDUP=0 to disable."""
    raw = os.environ.get("BRAIN_STRATEGIC_EVIDENCE_DEDUP")
    if raw is None or not str(raw).strip():
        return True
    return _truthy(raw)


def _citation_gate_enabled() -> bool:
    """Draft-time citation validation is ON unless switched off. Like the
    dedup flag it defaults ENABLED: it only ever WITHHOLDS a scaffold PR,
    never a recommendation, so a wrong default costs one deferred draft.
    Set BRAIN_STRATEGIC_CITATION_GATE=0 to disable."""
    raw = os.environ.get("BRAIN_STRATEGIC_CITATION_GATE")
    if raw is None or not str(raw).strip():
        return True
    return _truthy(raw)


def _evidence_dedup_weeks() -> int:
    """How far back the evidence ledger is consulted. Default 16 weeks —
    the three /mcp#workos-oauth-challenge scaffolds spanned six (2026-07-13
    → 2026-08-24), so the old 4-week title window could not have seen the
    first one from the third one even if titles had matched."""
    try:
        return max(1, int(os.environ.get(
            "BRAIN_STRATEGIC_EVIDENCE_DEDUP_WEEKS", "16")))
    except Exception:
        return 16


def _weekly_pr_cap() -> int:
    try:
        return max(0, int(os.environ.get(
            "BRAIN_STRATEGIC_WEEKLY_PR_CAP", "5")))
    except Exception:
        return 5


def _daily_pr_cap() -> int:
    """Hard per-UTC-day ceiling on strategic draft PRs (2026-07-02). The
    weekly cap alone couldn't stop a burst day: it was charged per RUN, and
    3 full runs in one day opened 3× the cap. Even with the weekly cap now
    charged against the DB, a single bad day should never open more than
    this many drafts."""
    try:
        return max(0, int(os.environ.get(
            "BRAIN_STRATEGIC_DAILY_PR_CAP", "5")))
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

    ctx = {
        "funnel":          {"now": funnel_now, "d30": funnel_30d},
        "page_health":     page_health,
        "feedback":        feedback,
        "backlog":         backlog,
        "competitors":     competitors,
        "self_model":      self_model,
        "recent_recs":     recent_recs,
        "recidivism":      _read_recidivism(),
        "pr_outcomes":     pr_outcomes,
        "self_perception": self_perception,
        "code_inventory":      code_inventory,
        "recent_arch_proposals": recent_arch_proposals,
    }
    # RAG recall (behind BRAIN_RAG_ENABLED): semantically retrieve the most
    # relevant PRIOR work + live market context so the synthesis has memory +
    # situational awareness beyond the few recent rows. Fail-soft; the structured
    # sources above are unchanged — this only adds unstructured recall.
    if _truthy(os.environ.get("BRAIN_RAG_ENABLED")):
        try:
            from routes.brain_rag import retrieve_context
            _rag_q = _rag_focus_query(funnel_now, self_perception)
            ctx["retrieved_prior_work"] = retrieve_context(
                _rag_q, k=8, corpus=["brain_findings", "brain_strategic_recommendations"])
            # rollout: also surface the most relevant recent MARKET NEWS + M&A so
            # strategy reacts to what's actually happening, not just the funnel.
            ctx["relevant_news"] = retrieve_context(_rag_q, k=5, corpus="news_articles")
            ctx["relevant_deals"] = retrieve_context(_rag_q, k=4, corpus="deals")
            # r-rag-lessons: recall PAST OUTCOMES (what worked/failed) so strategy
            # doesn't re-recommend approaches that already failed. Fail-soft.
            try:
                from routes.brain_rag import retrieve_lessons
                ctx["retrieved_lessons"] = retrieve_lessons(_rag_q, k=5)
            except Exception:
                pass
            # agentic-loop #65 part C (2026-08-22): what we got WRONG. Claims
            # the verifier REFUTED or the owner RETRACTED (brain_predictions_log
            # outcome gate), proposals the triage rejected as duplicates, and
            # the FAILED rows of the lesson corpora — so the synthesis never
            # re-states a refuted number or re-proposes a rejected idea.
            # ★ _build_prompt hand-picks ctx keys: this key is rendered ONLY
            # because the prompt names "refuted_claims" under
            # _WRONG_SECTION_TITLE — drop either half and the other is inert.
            # Own try: an older brain_rag without the helper costs nothing.
            try:
                from routes.brain_rag import recall_negative_lessons
                ctx["refuted_claims"] = recall_negative_lessons(_rag_q, k=4)
            except Exception:
                pass
        except Exception:
            pass
    return ctx


def _rag_focus_query(funnel_now, self_perception) -> str:
    """Build the RAG query from the LIVE strategic focus (top paid-tool demand +
    recent self-assessed losses) so recall matches THIS week, not a fixed string."""
    parts = ["DC Hub data-center infrastructure growth strategy"]
    try:
        pd = ((funnel_now or {}).get("paid_tool_demand_30d") or [])[:4]
        parts += [str((x or {}).get("tool") or (x or {}).get("name") or "")
                  for x in pd if isinstance(x, dict)]
    except Exception:
        pass
    try:
        sp = self_perception or {}
        latest = sp.get("latest") if isinstance(sp.get("latest"), dict) else {}
        losses = latest.get("losses") or sp.get("losses")
        if isinstance(losses, list):
            parts += [str(x)[:80] for x in losses[:3]]
        elif losses:
            parts.append(str(losses)[:200])
    except Exception:
        pass
    q = " · ".join(p for p in parts if p).strip()
    return q[:600] or "DC Hub growth, conversion, grid & fiber demand, data-moat gaps"


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
        {"name": "Electricity Maps",
         "url": "https://www.electricitymaps.com",
         "focus": "grid carbon-intensity API — the agent-facing grid-data "
                  "competitor (carbon only; no headroom/queue/facility layers)"},
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
    "analyst_research": [
        {"name": "SemiAnalysis",
         "url": "https://semianalysis.com",
         "focus": "AI-infrastructure research + datacenter model "
                  "(subscription prose/spreadsheets — not machine-readable, "
                  "no API/MCP; the analyst mindshare competitor)"},
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


def _read_crawled_gaps(days: int = 45, sample: int = 12) -> dict:
    """Live coverage_gaps rows from the competitor-gap crawler.

    brain-ascension #28 (2026-07-25): the crawler wrote coverage_gaps daily
    but NOTHING strategic ever read the table — the one competitor→product
    path fed on the static universe only, so crawled evidence dead-ended.
    Fail-soft: {} on any miss. created_at is TEXT in this table — compare
    lexically against an ISO cutoff, never cast in SQL."""
    c = _get_db()
    if c is None:
        return {}
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=days)).isoformat()
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT competitor, gap_type, COUNT(*)
                     FROM coverage_gaps
                    WHERE created_at >= %s
                    GROUP BY competitor, gap_type
                    ORDER BY COUNT(*) DESC""", (cutoff,))
            counts = [{"competitor": r[0], "gap_type": r[1], "n": int(r[2])}
                      for r in (cur.fetchall() or [])]
            cur.execute(
                """SELECT competitor, description
                     FROM coverage_gaps
                    WHERE created_at >= %s
                    ORDER BY created_at DESC
                    LIMIT %s""", (cutoff, sample))
            examples = [{"competitor": r[0], "description": (r[1] or "")[:300]}
                        for r in (cur.fetchall() or [])]
        if not counts and not examples:
            return {}
        return {"window_days": days, "counts_by_competitor": counts,
                "recent_examples": examples}
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        return {}


def _read_recidivism(days: int = 60, top: int = 8) -> list:
    """Seven-levers #32 (2026-07-25): clusters of brain_fix_outcomes rows
    with still_broken=TRUE — merged fixes whose finding re-fired afterward.
    On ship day 485 of 1,210 stamped outcomes (40%) were recidivist and
    NOTHING consumed that signal; the planner kept proposing fresh work
    while old fixes silently un-fixed themselves. Grouped by issue_label
    (the reconciler's stable finding key), newest evidence attached, so
    the synthesis can weigh 'this class of fix does not hold' as a
    first-order signal. Fail-soft → [] (the section is skipped)."""
    c = _get_db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            # ★live schema (introspected 2026-07-26): brain_fix_outcomes is
            # the ORIGINAL shape — proposal_id/proposal_kind/checked_at/
            # evidence_note. The reconciler's richer DDL (issue_label,
            # reconciled_at) never ran because the table pre-existed; the
            # first version of this reader used those columns and fail-softed
            # to [] on every call, silently skipping the whole section.
            cur.execute("""
                SELECT COALESCE(NULLIF(proposal_kind, ''), 'unknown') AS kind,
                       COUNT(*) AS n,
                       MAX(checked_at)::date AS latest,
                       (ARRAY_AGG(LEFT(COALESCE(evidence_note, ''), 160)
                                  ORDER BY checked_at DESC))[1] AS ev
                  FROM brain_fix_outcomes
                 WHERE still_broken IS TRUE
                   AND checked_at > NOW() - make_interval(days => %s)
                 GROUP BY 1
                 ORDER BY n DESC, latest DESC
                 LIMIT %s""", (days, top))
            rows = cur.fetchall() or []
        return [{"label": r[0], "recidivist_count": int(r[1]),
                 "latest": str(r[2]), "evidence": (r[3] or "").strip()}
                for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("L6 recidivism read failed: %s", e)
        return []
    finally:
        try:
            c.close()
        except Exception:
            pass


def _gather_competitor_context() -> dict:
    """Assemble the competitor-intel envelope the L6 prompt feeds on.

    Four layers (each layer is fail-soft → empty dict on miss):
      1. `presence` — live MCP-presence crawler snapshot
         (/api/v1/mcp/presence/recent) — what other MCP directories
         say about DC Hub, drift, recently-discovered registries
      2. `universe` — curated static catalog of the real competitive
         landscape (DC registries, energy data providers, MCP dirs)
      3. `signal` — recent mcp_presence_* AND coverage_gap_competitor
         findings from brain_findings
      4. `crawled_gaps` — live coverage_gaps rows the daily crawler
         stages (what competitors list that DC Hub doesn't cover) —
         wired 2026-07-25; previously this evidence dead-ended

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

    # Layer 3: brain_findings already-filed competitor signals — both the
    # MCP-presence stream and the crawler's coverage_gap_competitor stream.
    findings = _http_get_json(
        "/api/v1/brain/findings?issue_like=mcp_presence&limit=20")
    gap_findings = _http_get_json(
        "/api/v1/brain/findings?issue_like=coverage_gap_competitor&limit=10")
    if gap_findings:
        if isinstance(findings, list) and isinstance(gap_findings, list):
            findings = findings + gap_findings
        elif not findings:
            findings = gap_findings

    # Layer 4: the crawler's own table (direct DB read, fail-soft)
    crawled = _read_crawled_gaps()

    envelope = {
        "presence": presence or {"_note": "presence_endpoint_unreachable"},
        "universe": _COMPETITOR_UNIVERSE,
        "signal":   findings or {"_note": "no_competitor_findings_yet"},
        "crawled_gaps": crawled or {"_note": "no_crawled_gaps_in_window"},
        "_layers":  ["presence", "universe", "signal", "crawled_gaps"],
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
        or crawled
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
                """SELECT week_of, kind, title, status, pr_url,
                          evidence_keys
                     FROM brain_strategic_recommendations
                    WHERE week_of >= %s
                    ORDER BY week_of DESC, id DESC
                    LIMIT 30""", (cutoff,))
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            # 2026-08-31: carry the SUBJECTS, not the raw keys — the model
            # needs to see what past recs were ABOUT to obey rule 4, and the
            # normalised subjects are both shorter and directly comparable.
            try:
                keys = (r[5] if isinstance(r[5], list)
                        else json.loads(r[5] or "[]"))
            except Exception:
                keys = []
            out.append(
                {"week_of": str(r[0]), "kind": r[1], "title": r[2],
                 "status": r[3], "pr_url": r[4],
                 # capped: one key-heavy rec must not crowd the others
                 # out of the recent_recs budget below.
                 "evidence_subjects": sorted(evidence_subjects(keys))[:6]})
        return out
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
   past 4 weeks (the brain should not whiplash week-to-week), and do NOT
   propose a rec whose evidence_keys are about a subject already listed
   under evidence_subjects there. Re-titling the same finding is the
   failure this rule exists to stop: three separate scaffolds once shipped
   for one page because each run paraphrased the title while citing the
   same evidence. If a past rec already covers the subject and you believe
   it is still unaddressed, say so in self_critique instead of re-filing
   it — a duplicate scaffold is withheld at PR time regardless.
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
    # Seven-levers #32 (2026-07-25): recidivist findings — 40% of verified
    # fixes re-broke (485/1210 still_broken on the day this shipped) and
    # nothing prioritized them. The synthesis must treat a re-broken cluster
    # as evidence the SHALLOW fix pattern failed and root-cause it instead.
    if ctx.get("recidivism"):
        sections.append("RECIDIVIST FINDINGS (fixes that did NOT hold — "
                        "root-cause these before proposing anything similar; "
                        "a repeat shallow patch here is a wasted merge):\n" +
                        _truncate(ctx.get("recidivism"),
                                  _CTX_BUDGET["recidivism"]))
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

    # RAG recall (2026-07-03, DARK): semantically-retrieved prior findings +
    # recommendations relevant to the strategic focus. Only present when
    # BRAIN_RAG_ENABLED set the key in _gather_strategic_context — otherwise this
    # section is absent and the prompt is byte-identical to today.
    if ctx.get("retrieved_prior_work"):
        sections.append("RELEVANT PRIOR WORK (semantically retrieved from your own "
                        "findings + past recommendations — build on / reuse these; "
                        "do NOT re-propose what's already logged):\n" +
                        _truncate(ctx.get("retrieved_prior_work"), 3500))
    if ctx.get("retrieved_lessons"):
        sections.append("PAST LESSONS (outcomes of prior brain actions — what "
                        "actually WORKED vs FAILED when tried; do NOT recommend an "
                        "approach that already failed, and prefer what worked):\n" +
                        _truncate(ctx.get("retrieved_lessons"), 2500))
    # agentic-loop #65 part C (2026-08-22): the learn station's NEGATIVE
    # results. Present only when _gather_strategic_context recalled at least
    # one (BRAIN_RAG_ENABLED + a refuted/retracted claim, a rejected proposal
    # or a failed fix that matches the focus) — otherwise absent, and the
    # prompt is byte-identical to before this section existed.
    if ctx.get("refuted_claims"):
        sections.append(_WRONG_SECTION_TITLE + " — claims the verifier REFUTED "
                        "or the owner RETRACTED, proposals rejected as duplicates, "
                        "and fixes that FAILED. Never re-state these numbers or "
                        "re-propose these ideas as they stand; if the regime has "
                        "changed, say exactly what changed and frame the new "
                        "version as a NEW claim with its own expectation:\n" +
                        _truncate(ctx.get("refuted_claims"),
                                  _CTX_BUDGET["refuted_claims"]))
    if ctx.get("relevant_news"):
        sections.append("RELEVANT MARKET NEWS (semantically retrieved recent "
                        "coverage relevant to the focus — react to what's actually "
                        "happening in the market):\n" +
                        _truncate(ctx.get("relevant_news"), 2500))
    if ctx.get("relevant_deals"):
        sections.append("RELEVANT M&A DEALS (semantically retrieved comparable "
                        "transactions for market-timing signal):\n" +
                        _truncate(ctx.get("relevant_deals"), 1500))

    # Feature #8 (DARK): per-rec-type STRATEGIC-OUTCOME LEDGER feedback.
    # Only spliced when BRAIN_STRATEGIC_LEDGER_FEEDBACK_ENABLED is set;
    # otherwise the prompt is byte-identical to today. Fully fail-soft —
    # any error leaves the prompt unchanged.
    try:
        from routes import brain_strategic_ledger as _ledger
        if _ledger.ledger_feedback_enabled():
            _fb = _ledger.gather_ledger_feedback(lookback_days=120)
            _fb_txt = _ledger.format_feedback_for_prompt(_fb, budget=1500)
            sections.append(
                "STRATEGIC-OUTCOME LEDGER (did past recs of each type "
                "actually MOVE their target metric 14/30d later? Favour "
                "rec-types with a real hit-rate; discount types that "
                "consistently went flat/regressed; vague recs with no "
                "target_metric are UNVERIFIABLE — propose measurable "
                "recs with an explicit target_metric):\n" + _fb_txt)
    except Exception as _lfe:
        logger.debug("L6 strategic: ledger feedback splice skipped: %s", _lfe)

    return ("\n\n".join(sections) +
            "\n\n──── End context. Reply with the JSON object ONLY. ────")


# ─── Structured-output schema (derived from _persist_recommendations +
# brain_strategic_ledger.record_baseline reads — every key a downstream
# consumer touches is a property here; see tests/test_brain_structured_outputs.py
# which introspects the consumers against this schema) ───────────────
# Structured-outputs constraints honoured: additionalProperties=false on
# every object, no min/max|minLength|minItems>1, enums only on scalars.
# Item counts ("EXACTLY 3") stay prompt-enforced — the API can't express them.

_L6_CONFIDENCE = {"type": "string", "enum": ["high", "medium", "low"]}
_L6_STR_LIST = {"type": "array", "items": {"type": "string"}}
_L6_SCAFFOLD = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "kind": {"type": "string"}},
        "required": ["path", "kind"],
        "additionalProperties": False,
    },
}

_L6_REC_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "top_gaps_4w": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "spec": {"type": "string"},
                    "evidence_keys": _L6_STR_LIST,
                    "dollar_lift_est_usd": {"type": ["number", "null"]},
                    "confidence": _L6_CONFIDENCE,
                    "file_scaffold": _L6_SCAFFOLD,
                    # optional: STRATEGIC-OUTCOME LEDGER reads item.target_metric
                    "target_metric": {"type": "string"},
                },
                "required": ["title", "spec", "evidence_keys",
                             "dollar_lift_est_usd", "confidence",
                             "file_scaffold"],
                "additionalProperties": False,
            },
        },
        "competitor_lacks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "spec": {"type": "string"},
                    "competitor": {"type": "string"},
                    "evidence_keys": _L6_STR_LIST,
                    "confidence": _L6_CONFIDENCE,
                    "dollar_lift_est_usd": {"type": ["number", "null"]},
                    "file_scaffold": _L6_SCAFFOLD,
                    "target_metric": {"type": "string"},
                },
                "required": ["title", "spec", "competitor",
                             "evidence_keys", "confidence"],
                "additionalProperties": False,
            },
        },
        "funnel_optimizations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "spec": {"type": "string"},
                    "current_stage_metric": {"type": "string"},
                    "projected_stage_metric": {"type": "string"},
                    "dollar_lift_est_usd": {"type": ["number", "null"]},
                    "confidence": _L6_CONFIDENCE,
                    "evidence_keys": _L6_STR_LIST,
                    "file_scaffold": _L6_SCAFFOLD,
                    "target_metric": {"type": "string"},
                },
                "required": ["title", "spec", "current_stage_metric",
                             "projected_stage_metric", "dollar_lift_est_usd",
                             "confidence"],
                "additionalProperties": False,
            },
        },
        "wildcard_bet": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "spec": {"type": "string"},
                "horizon_months": {"type": "integer"},
                "confidence": _L6_CONFIDENCE,
            },
            "required": ["title", "spec", "horizon_months", "confidence"],
            "additionalProperties": False,
        },
        "stop_doing": {"type": ["string", "null"]},
        "self_critique": {"type": "string"},
    },
    "required": ["summary", "top_gaps_4w", "competitor_lacks",
                 "funnel_optimizations", "wildcard_bet", "stop_doing",
                 "self_critique"],
    "additionalProperties": False,
}


# ─── Claude call (with fallback chain) ──────────────────────────────

def _call_claude(prompt: str) -> Optional[dict]:
    """Call Claude with the strategic prompt. Walks the brain_models
    fallback chain on 404/timeout (fable-5 → opus-4-8 → sonnet-4-5 →
    haiku-4-5). fable-5 gets its own transport config — see the
    per-model block below."""
    if not _ANTHROPIC_KEY:
        logger.warning("L6 strategic: ANTHROPIC_API_KEY unset")
        return None

    try:
        from routes.brain_models import brain_model_for, resolve_chain
    except Exception as e:
        logger.warning("L6 strategic: model registry import failed: %s", e)
        return None

    try:
        from utils.anthropic_helper import anthropic_messages_url
    except Exception:
        anthropic_messages_url = None  # degrade to direct below

    chain = resolve_chain(brain_model_for("reasoning"))
    last_err = None
    # r-l6-truncation-retry (2026-07-04): models we've already given a
    # bigger-budget retry this synthesis (bounded to one boost per model).
    _escalated: set = set()
    _MAX_TOKENS_CEIL = 60000
    for model in chain:
        # ── Per-model transport (2026-07-02, fable-timeout fix) ──────
        # fable-5: extended thinking is ALWAYS ON and has NO budget knob
        # (`thinking:{type:"disabled"}` and `budget_tokens` both return
        # 400 on fable-5), so reasoning tokens are spent out of
        # max_tokens BEFORE the JSON answer. Two consequences:
        #   · max_tokens needs headroom for thinking + the ~10-16k-char
        #     rec JSON → 32000 (16000 lets a long think truncate the
        #     answer mid-string → "Unterminated string" parse failure).
        #   · wall-clock routinely runs 2-4 min. The CF AI Gateway edge
        #     kills long-poll requests at ~90-100s — every fable run
        #     died with "Read timed out (read timeout=90)" and silently
        #     degraded to sonnet/haiku, so the strategic recs were never
        #     Fable-quality. The fable leg therefore goes DIRECT to
        #     api.anthropic.com (same ANTHROPIC_API_KEY) with a 240s
        #     read budget — fine, we run in a bg thread.
        # Fallback models (opus/sonnet/haiku — no thinking config): keep
        # the AI Gateway (caching + observability) with a smaller 120s
        # read timeout; the gateway's own ~100s edge ceiling makes a
        # longer client budget pointless there anyway.
        _is_fable = model.startswith("claude-fable")
        if _is_fable or anthropic_messages_url is None:
            _url = "https://api.anthropic.com/v1/messages"
        else:
            _url = anthropic_messages_url()
        _timeout = (10, 240) if _is_fable else (10, 120)   # (connect, read)
        _max_tokens = 32000 if _is_fable else 16000
        try:
            import requests
            # ── Structured outputs (2026-07-04) ─────────────────────
            # Verified param: output_config.format={type:"json_schema",
            # schema}, GA (no beta header), supported on the whole chain
            # (fable-5/opus-4-8/sonnet-4-5/haiku-4-5). Kill switch:
            # BRAIN_STRUCTURED_OUTPUTS=0. FAIL-SOFT: any 400 on a
            # structured attempt retries the SAME model with the legacy
            # free-text body (byte-identical to the pre-structured one),
            # then the existing chain-walk handles everything else.
            # 2026-06-08 FIX (max_tokens): was 3200 — too small for the
            # ~20-rec JSON once the prompt was enriched (r63 history+
            # rejection memory). 16000 fits the full synthesis for the
            # non-thinking fallbacks (opus-4-8/sonnet-4-5/haiku-4-5);
            # fable-5 gets 32000 because always-on thinking is billed
            # against max_tokens too — see the per-model transport block
            # above. Structured outputs do NOT change that: a truncated
            # (stop_reason=max_tokens) answer is invalid JSON either way
            # and walks the chain exactly as before.
            try:
                from routes import brain_llm_structured as _so
            except Exception:
                _so = None

            def _post(structured: bool):
                if _so is not None:
                    _body, _applied = _so.build_messages_body(
                        model, _SYSTEM_PROMPT,
                        [{"role": "user", "content": prompt}],
                        _max_tokens,
                        _L6_REC_SCHEMA if structured else None)
                else:
                    _applied = False
                    _body = {
                        "model":      model,
                        "max_tokens": _max_tokens,
                        "system":     _SYSTEM_PROMPT,
                        "messages":   [{"role": "user", "content": prompt}],
                    }
                return requests.post(
                    _url,
                    headers={
                        "x-api-key":         _ANTHROPIC_KEY,
                        "anthropic-version": "2023-06-01",
                        "User-Agent":        "dchub-brain-strategic/1.0",
                        "content-type":      "application/json",
                    },
                    json=_body,
                    timeout=_timeout,
                ), _applied

            _want_structured = (_so is not None and
                                _so.structured_active(model, _L6_REC_SCHEMA))
            r, _structured = _post(_want_structured)
            if _structured and r.status_code == 400:
                # Param rejected (or any other 400 while structured was on):
                # memoize when the error blames output_config, then retry
                # this SAME model on the legacy path before chain-walking.
                if _so.looks_like_structured_rejection(r.status_code, r.text):
                    _so.mark_model_unsupported(model)
                logger.info(
                    "L6 strategic: %s 400 on structured attempt — retrying "
                    "legacy free-text path", model)
                r, _structured = _post(False)
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
            try:
                from routes.brain_llm_structured import record_llm_usage
                record_llm_usage("brain-strategic-planner", model, body)
            except Exception:
                pass
            if body.get("stop_reason") == "max_tokens":
                # fable-5 trap: thinking is billed against max_tokens, so a
                # long think can starve the JSON answer → truncated JSON →
                # parse below fails → the chain walks to None → ZERO recs for
                # the whole week (fail-closed). Rather than accept that, give
                # the SAME model ONE more shot with a bigger budget so the
                # answer completes AFTER thinking, before walking the chain.
                # Bounded (one boost/model, hard 60k ceiling); any non-200 on
                # the retry keeps the original truncated body → parse fails →
                # chain-walk exactly as before (strictly additive, no regression).
                logger.warning(
                    "L6 strategic: %s hit max_tokens=%s — answer likely "
                    "truncated (thinking eats max_tokens)", model, _max_tokens)
                if model not in _escalated and _max_tokens < _MAX_TOKENS_CEIL:
                    _escalated.add(model)
                    _boosted = min(_MAX_TOKENS_CEIL, int(_max_tokens * 1.75))
                    logger.warning(
                        "L6 strategic: %s retrying at max_tokens=%s (was %s) "
                        "for thinking headroom", model, _boosted, _max_tokens)
                    _max_tokens = _boosted
                    try:
                        r, _structured = _post(_want_structured)
                        if r.status_code == 200:
                            body = r.json() or {}
                            try:
                                from routes.brain_llm_structured import record_llm_usage
                                record_llm_usage("brain-strategic-planner", model, body)
                            except Exception:
                                pass
                    except Exception as _re:
                        logger.warning(
                            "L6 strategic: %s boosted-retry exception: %s",
                            model, _re)
            text = "".join(
                b.get("text", "")
                for b in (body.get("content") or [])
                if b.get("type") == "text"
            ).strip()
            # Fence-strip is a LEGACY-path affordance only. A structured
            # response is guaranteed bare JSON — no fences to strip.
            if not _structured and text.startswith("```"):
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
                              run_id: str,
                              ctx: Optional[dict] = None) -> int:
    """Write one row per recommendation. Returns count inserted.

    `ctx` (the in-memory synthesis context) is optional and additive: when
    provided, each verifiable rec also gets a STRATEGIC-OUTCOME LEDGER row
    capturing a baseline metric snapshot (feature #8). The ledger write is
    fully fail-safe — any miss is swallowed and never blocks rec persist.
    """
    if not payload:
        return 0
    c = _get_db()
    if c is None:
        return 0
    # Feature #8: load the ledger module fail-soft. If it isn't deployed
    # yet (or import fails), baseline capture is silently skipped and
    # persistence behaves exactly as before.
    try:
        from routes import brain_strategic_ledger as _ledger
    except Exception as _le:
        _ledger = None
        logger.debug("L6 strategic: ledger module not loaded: %s", _le)
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
                          ) ON CONFLICT DO NOTHING RETURNING id""",
                        (run_id, week_of, kind, title, spec,
                         json.dumps(scaffold), dollar, conf_num,
                         json.dumps(evid), "new", json.dumps(item)))
                    _rec_id = None
                    try:
                        _row = cur.fetchone()
                        _rec_id = _row[0] if _row else None
                    except Exception:
                        _rec_id = None
                    inserted += 1
                    # Feature #8: capture a baseline ledger row (shares
                    # this tx). Verifiable recs snapshot their metric;
                    # vague recs land verifiable=FALSE (never auto-credited).
                    if _ledger is not None and ctx is not None:
                        _ledger.record_baseline(
                            cur, rec_id=_rec_id, run_id=run_id,
                            week_of=week_of, kind=kind, title=title,
                            item=item, ctx=ctx)
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
                      ) ON CONFLICT DO NOTHING""",
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
                  ) ON CONFLICT DO NOTHING""",
                (run_id, week_of, "synthesis_meta",
                 (payload.get("summary") or "Weekly synthesis")[:200],
                 (payload.get("summary") or "")[:6000],
                 json.dumps([]), None, 0.5,
                 json.dumps([]), "meta", json.dumps(meta)))
            inserted += 1
        # 2026-06-16: a swallowed commit failure followed by `return inserted`
        # reports fabricated persistence (same class as the radar freeze — an
        # aborted tx makes commit() silently discard every INSERT while we return
        # the pre-commit count). On commit failure, roll back + log + zero the
        # count so the return reflects what actually landed.
        try:
            c.commit()
        except Exception as _ce:
            try:
                c.rollback()
            except Exception:
                pass
            logger.error("L6 strategic: commit failed — 0 rows persisted: %s", _ce)
            inserted = 0
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.error("L6 strategic: persist failed: %s", e)
        inserted = 0   # rolled back → nothing persisted; don't report a fabricated count
    finally:
        try:
            c.close()
        except Exception:
            pass
    return inserted


def _read_recs_for(week_of: _dt.date) -> Optional[list]:
    """This week's recommendation rows, or None when the DB can't answer.

    2026-07-02 FAIL-CLOSED: this used to return [] on ANY DB error, which
    run_strategic_synthesis read as "no recs yet this week" — so a transient
    DB blip under the every-5-min heartbeat became a full Opus run + PR
    burst (two 5-PR bursts on 07-02, 21 open drafts). None now means
    "unknown" and the caller must abort, not recompute."""
    c = _get_db()
    if c is None:
        return None
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
        return None
    finally:
        try:
            c.close()
        except Exception:
            pass


def _prs_opened_today() -> Optional[int]:
    """How many strategic PRs were opened this UTC day. pr_url is stamped
    only by _mark_pr_on_rec (which also sets updated_at=NOW()), so this
    count backs the per-day cap. None on any DB error — callers treat
    unknown as "open nothing" (fail-closed, same rule as _read_recs_for)."""
    c = _get_db()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*)
                     FROM brain_strategic_recommendations
                    WHERE pr_url IS NOT NULL
                      AND updated_at >= date_trunc('day', NOW())""")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.error("L6 strategic: daily PR count failed: %s", e)
        return None
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


SPEC_ONLY_MARKER = "SPEC-ONLY"

# ★ The title namespace brain-spec-debt-tracker keys on. Its job gate is
# startsWith(title, '[brain-') — widened 2026-08-31 from '[brain-spec]', which
# watched one of the three brain PR-opening paths and missed this one entirely.
SCAFFOLD_TITLE_PREFIX = "[brain-l6 strategic-draft]"


def _scaffold_pr_body(kind, week_of, conf, dollar_block, spec,
                      md_path, py_path, evidence_block) -> str:
    """Body for a strategic-scaffold PR. Pure — no I/O, so it is testable.

    the SPEC-ONLY marker LEADS this body, and that is load-bearing.
    brain-pr-substance-gate classifies a diff of only docs/ + routes/_proposed_*
    as inert, then greps title+body for close/closes/closed/fix/fixes/fixed/
    resolve/resolves/resolved and HARD-FAILS the check on a hit — the
    false-resolution pattern the gate exists to stop. But every word of the
    spec below is LLM prose the brain wrote ABOUT WHAT TO BUILD, so phrases
    like "the epistemics fix" or "not because fixes fail" land in it by
    accident. 19 of the 60 most recent strategic drafts tripped that regex and
    blocked on a required check; the other 41 passed only because their prose
    happened to dodge eight words. That is a coin flip, not a gate.

    brain_pr_opener adopted this marker on 2026-07-18 (r-spec-honesty) for
    exactly this failure. This is the SECOND PR-opening path and it never did.

    The marker narrows nothing: the gate consults it only after the diff is
    ALREADY inert, so a strategic PR that ever carries a runtime file is
    classified on its diff and never reaches the marker's branch at all.
    Position is deliberate too — first line, so it survives the body[:4000]
    truncation the spec-PR janitor and filer dedup both read through.
    """
    return (
        f"**{SPEC_ONLY_MARKER}** — this PR changes no running code and is not "
        f"a fix; it captures a brain strategic recommendation as a spec plus "
        f"an unregistered scaffold, for a human to implement or discard.\n\n"
        f"## Brain Layer-6 strategic scaffold\n\n"
        f"**Kind:** `{kind}`  ·  **Week:** {week_of}  ·  "
        f"**Confidence:** {conf}  ·  **Est. lift:** {dollar_block}\n\n"
        f"### Why\n\n{(spec or '_(spec missing)_').strip()}\n\n"
        f"### What this PR contains\n\n"
        f"- `{md_path}` — the spec the brain wrote\n"
        f"- `{py_path}` — empty Blueprint stub (passes `ast.parse`)\n\n"
        f"### Evidence the brain cited\n\n{evidence_block}\n\n"
        # ★ MARKDOWN CHECKBOXES, not a numbered list. brain-spec-debt-tracker
        # re-files the unchecked items as a tracked issue when this PR merges,
        # and it finds them with `grep -q '^- [ ]'`. The numbered list this
        # replaced was invisible to that grep, so the tracker exited 0 logging
        # "spec was completed before merge" — a false negative that reports as
        # SUCCESS. Merging a scaffold ships nothing (main.py never imports
        # routes/_proposed_*), so the obligation has no other home. Keep every
        # line starting at column 0 with "- [ ] " or the tracker cannot see it.
        # ★ Wording avoids close/fix/resolve — the substance gate's fix-claim
        # regex reads this body, the same discipline brain_pr_opener keeps.
        f"### To take this from scaffold → shipped\n\n"
        f"- [ ] Confirm this is still worth doing\n"
        f"- [ ] Read the spec at `{md_path}`\n"
        f"- [ ] Flesh out `{py_path}` with the real routes\n"
        f"- [ ] Wire the blueprint into `main.py` (mirror the pattern at "
        f"e.g. `app.register_blueprint(brain_backlog_admin_bp)`)\n"
        f"- [ ] Or discard this scaffold if superseded / not worth doing\n\n"
        f"---\n"
        f"_Auto-generated by `routes/brain_strategic_planner.py`. This is "
        f"a DRAFT PR — humans merge. Kill switch: "
        f"`DCHUB_BRAIN_STRATEGIC_DISABLE=1` (full module) / "
        f"`DCHUB_BRAIN_STRATEGIC_DRAFT_PR=0` (PR opener only)._\n"
    )


# ─── Evidence-subject dedup ─────────────────────────────────────────
#
# THIRD iteration of one bug. Both existing dedup passes compare TITLES and
# both query `?state=open`:
#
#   2026-06-28  open_pr_exists          exact title, OPEN PRs only
#   2026-07-02  open_similar_pr_exists  fuzzy title, OPEN PRs only
#
# Neither can see a scaffold PR a human already MERGED, and neither asks
# what the rec is ABOUT. So one sentinel verdict on the page
# /mcp#workos-oauth-challenge produced three separate MERGED scaffolds over
# six weeks — 2026-07-13, 2026-08-17, 2026-08-24 — under three titles that
# never collided on tokens, each citing that same page, each contradicting
# the other two about DCHUB_OAUTH_CHALLENGE_DISABLE. be#3448 then showed the
# verdict itself never held (the server drops the challenge on `initialize`
# BY DESIGN, so anon initialize=200 is correct), and be#3458/be#3459 had to
# delete all three by hand.
#
# The title is the paraphrased part; the cited evidence is not. This gate
# dedups on the evidence SUBJECT, against the full recommendation ledger —
# any status, merged included — instead of the open-PR list.
#
# It bounds amplification, which is the part the generator owns. It cannot
# tell that an upstream finding is false: a bad input is still a bad input,
# and #3448 was the fix for that. What it guarantees is that one bad input
# yields at most one scaffold instead of an unbounded stream.

# Segments that name HOW a value is addressed rather than WHAT it is about.
# The source roots come from _CTX_BUDGET so this list cannot drift out of
# sync with the context the planner is actually given.
_EVIDENCE_GENERIC = frozenset(_CTX_BUDGET) | frozenset((
    # source roots spelled differently in evidence keys than in _CTX_BUDGET
    "competitor_signal", "competitor_features", "page_integrity",
    "competitor", "funnel_now",
    # structural containers and leaf accessors
    "pages", "page", "now", "current", "latest", "prev", "previous",
    "value", "values", "verdict", "status", "state", "reason",
    "last_reason", "count", "total", "summary", "detail", "details",
    "meta", "presence", "universe", "items", "list", "top", "entries",
))

# `foo[bar]` -> `foo.bar`, so bracket and dot spellings of one path collide.
_EVID_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
# Trailing assertion: `...verdict=broken` and `...rate_pct=33.3` are the
# same subject as the bare path. Drop from the first comparator onward.
_EVID_ASSERT_RE = re.compile(r"[=<>!~].*$")


def evidence_subjects(keys) -> frozenset:
    """The set of SUBJECTS an evidence-key list is about.

    Normalises the two spellings the planner actually emits for one path
    and strips the accessor tail, so all three of these resolve to the
    single subject `/mcp#workos-oauth-challenge`:

        page_health.pages[/mcp#workos-oauth-challenge]
        page_health.pages./mcp#workos-oauth-challenge.verdict=broken
        page_health.pages[/mcp#workos-oauth-challenge].last_reason

    Pure and side-effect free — no DB, no network — so the dedup rule is
    testable without either. Unparseable or non-string entries are skipped
    rather than guessed at; a rec whose keys all drop out yields an empty
    set and is therefore never suppressed by this gate.
    """
    out = set()
    for raw in (keys or []):
        if not isinstance(raw, str):
            continue
        k = _EVID_BRACKET_RE.sub(r".\1", raw.strip().lower())
        k = _EVID_ASSERT_RE.sub("", k)
        for seg in k.split("."):
            seg = seg.strip().strip("'\"`,;:()")
            if not seg or seg in _EVIDENCE_GENERIC:
                continue
            # Keep short path-ish segments (`/mcp`), drop short words.
            if len(seg) < 3 and not seg.startswith("/"):
                continue
            out.add(seg)
    return frozenset(out)


# ─── Draft-time citation validation ─────────────────────────────────
#
# Rule 2 of _SYSTEM_PROMPT: "Every spec MUST cite at least 1 evidence_key
# drawn from the context below. Bullshit speculation gets you fired."
# Nothing ever checked. Measured across the 33 scaffolds in the tree on
# 2026-08-31, 34 of 92 cited keys (36%) name a root that is not a context
# source at all:
#
#     competitor_signal   the context key is `competitors`
#     customer_asks       the context key is `feedback`
#     recidivist          the context key is `recidivism`
#     past_lessons, market_news, news, now — no such source exists
#
# Those are not stale citations; they never resolved, on any day. The model
# invents a plausible-sounding source name and the provenance reads as real.
#
# The ROOT check is the part that is provable without a value baseline: the
# set of valid roots is exactly the keys of the context dict the model was
# handed, so "this names no source" is a fact about the schema, not a guess
# about drift. Deeper subpath mismatches are NOT judged here — a wrong
# subpath under a real source is indeterminate, and be#3448 is what happens
# when a probe reports indeterminate as broken.

def evidence_root(key) -> Optional[str]:
    """The source root an evidence key addresses, or None if unusable."""
    if not isinstance(key, str):
        return None
    k = _EVID_BRACKET_RE.sub(r".\1", key.strip().lower())
    k = _EVID_ASSERT_RE.sub("", k)
    for seg in k.split("."):
        seg = seg.strip().strip("'\"`,;:()")
        if seg:
            return seg
    return None


def citations_all_invented(ctx: dict, keys) -> tuple:
    """(True, [roots]) when EVERY cited key names a source the context does
    not have — i.e. the rec's provenance is entirely fabricated.

    Deliberately conservative. Returns (False, ...) when:
      · the context is empty or not a dict — nothing to check against, and
        an unreadable context is not evidence about the citations;
      · the rec cites nothing — that is a separate rule-2 problem, and
        absence of citations is not invented provenance;
      · ANY citation names a real source — a wrong subpath under a real
        root is indeterminate, never grounds to suppress.
    """
    if not isinstance(ctx, dict) or not ctx:
        return False, []
    roots = [evidence_root(k) for k in (keys or [])]
    roots = [r for r in roots if r]
    if not roots:
        return False, []
    bad = [r for r in roots if r not in ctx]
    return (len(bad) == len(roots)), sorted(set(bad))


def _scaffolded_evidence_subjects(weeks_back: Optional[int] = None):
    """{subject: (title, week_of)} for every rec that ALREADY produced a
    scaffold PR inside the window, newest first so the reported prior is the
    most recent one.

    Returns None when the DB cannot answer — the caller treats that as
    "unknown" and withholds, never as "no duplicates".

    `pr_url IS NOT NULL` is the point: a rec that was merely written to the
    ledger left nothing behind to duplicate. Only recs that actually put a
    scaffold in the tree can suppress a later one.
    """
    c = _get_db()
    if c is None:
        return None
    weeks = _evidence_dedup_weeks() if weeks_back is None else weeks_back
    cutoff = _dt.date.today() - _dt.timedelta(weeks=weeks)
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT title, week_of, evidence_keys
                     FROM brain_strategic_recommendations
                    WHERE week_of >= %s
                      AND pr_url IS NOT NULL
                    ORDER BY week_of DESC, id DESC
                    LIMIT 300""", (cutoff,))
            rows = cur.fetchall() or []
    except Exception as e:
        logger.error("L6 strategic: evidence-dedup ledger read failed: %s", e)
        return None
    finally:
        try:
            c.close()
        except Exception:
            pass

    seen: dict = {}
    for title, week_of, evid in rows:
        # evidence_keys is written with json.dumps; depending on the column
        # type the driver hands back either the list or the raw string.
        try:
            keys = evid if isinstance(evid, list) else json.loads(evid or "[]")
        except Exception:
            continue
        for subj in evidence_subjects(keys):
            seen.setdefault(subj, (title, str(week_of)))
    return seen


def _open_scaffold_pr(rec: dict, ctx: Optional[dict] = None) -> dict:
    """Draft a PR with the spec MD + an empty Blueprint stub. Mirrors
    brain_backlog_admin._open_draft_pr_for_proposal but for a NEW file
    pair instead of a single-file patch."""
    try:
        from routes.brain_pr_opener import (
            _get_default_branch_sha, _create_branch, _commit_file,
            _gh, _GITHUB_TOKEN, _GITHUB_REPO, open_pr_exists,
            open_similar_pr_exists,
        )
    except Exception as e:
        return {"ok": False, "error": f"pr_opener import: {e}"}

    if not _GITHUB_TOKEN:
        return {"ok": False, "error": "GITHUB_TOKEN unset"}

    title = (rec.get("title") or "").strip()
    if not title:
        return {"ok": False, "skipped": "no_title"}

    # 2026-06-28: DEDUPE — don't re-draft an idea that already has an open PR.
    # The L6 drafter re-proposed the same strategic ideas every cycle (self-
    # serve checkout 3×, minted-claim repair 2×…), creating a draft graveyard.
    # Skip BEFORE committing a branch so we don't leave orphan branches either.
    if open_pr_exists(f"[brain-l6 strategic-draft] {title}"):
        return {"ok": True, "skipped": "duplicate_open_pr", "title": title}
    # 2026-07-02: exact-match alone missed ~every dup — each Claude run
    # PARAPHRASES the same themes, so titles never matched verbatim (the
    # 07-02 twin bursts were paraphrase pairs). Fuzzy-match (token overlap
    # >= 60%) against the open strategic drafts before opening another.
    if open_similar_pr_exists(f"[brain-l6 strategic-draft] {title}",
                              prefix="[brain-l6 strategic-draft]"):
        return {"ok": True, "skipped": "similar_open_pr", "title": title}

    # 2026-08-31: EVIDENCE-SUBJECT dedup. See the block above
    # _EVIDENCE_GENERIC for the three merged /mcp#workos-oauth-challenge
    # scaffolds this exists to stop. Both checks above are blind to merged
    # PRs and compare only titles, which paraphrase; this compares what the
    # rec cites, against the whole ledger.
    if _evidence_dedup_enabled():
        subjects = evidence_subjects(rec.get("evidence_keys") or [])
        if subjects:
            # Read fresh per rec, deliberately: the runner loop stamps
            # pr_url via _mark_pr_on_rec (which commits) after each open, so
            # a re-read here also stops TWO recs in the SAME run from both
            # scaffolding one subject. Hoisting this out of the loop to save
            # a query would silently drop that intra-run half of the gate.
            prior = _scaffolded_evidence_subjects()
            if prior is None:
                # FAIL CLOSED, same reasoning _read_recs_for adopted on
                # 2026-07-02: unknown is not empty. Opening a duplicate
                # scaffold puts a file in the tree that a human must later
                # delete by hand (be#3458, be#3459); withholding costs one
                # deferred draft that next week's run re-proposes.
                return {"ok": True, "skipped": "evidence_ledger_unreadable",
                        "title": title}
            for subj in sorted(subjects):
                hit = prior.get(subj)
                if hit:
                    return {"ok": True, "skipped": "duplicate_evidence",
                            "title": title, "evidence_subject": subj,
                            "prior_title": hit[0], "prior_week": hit[1]}

    # 2026-08-31: CITATION gate. See the block above evidence_root(). A rec
    # whose every citation names a non-existent source has invented its
    # provenance, and rule 2 of the system prompt already forbids that; this
    # is the first thing that actually enforces it. Withholds the SCAFFOLD
    # only — the rec is still persisted and still shows in the digest, so
    # the idea survives while the fabricated citation stops putting a file
    # in the tree for a human to delete later (be#3458, be#3459).
    if ctx is not None and _citation_gate_enabled():
        invented, bad_roots = citations_all_invented(
            ctx, rec.get("evidence_keys") or [])
        if invented:
            return {"ok": True, "skipped": "citations_all_invented",
                    "title": title, "invented_roots": bad_roots,
                    "context_roots": sorted(ctx)}

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

    pr_title = f"{SCAFFOLD_TITLE_PREFIX} {title}"
    pr_body = _scaffold_pr_body(kind, week_of, conf, dollar_block, spec,
                                md_path, py_path, evidence_block)
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
    if existing is None:
        # FAIL-CLOSED (2026-07-02): unknown ≠ empty. A DB blip here used to
        # look like a fresh week, turning a heartbeat hit into a full Opus
        # run + PR burst. Abort; the next heartbeat retries in 5 min.
        return {"ok": False, "reason": "recs_read_failed",
                "week_of": str(week_of), "run_id": run_id,
                "note": "DB unreadable — aborted rather than recompute."}
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

    inserted = _persist_recommendations(payload, week_of, run_id, ctx=ctx)
    recs = _read_recs_for(week_of)

    # Optionally open scaffold PRs
    pr_results = []
    pr_skip_reason = None
    cap = 0
    do_prs = (_draft_pr_enabled() if open_prs is None else bool(open_prs))
    if do_prs and recs is None:
        # Fail-closed: without a readable rec list we can't know what
        # already has a PR — open nothing this run.
        do_prs, pr_skip_reason = False, "recs_read_failed"
    if do_prs:
        # 2026-07-02: `cap` was a per-RUN local counter, so the "weekly"
        # cap reset on every run — 3 full runs in one day (Thursday force +
        # heartbeat re-runs on DB blips) opened 3× the cap (21 open drafts).
        # Charge PRs already opened this ISO week AND today against the
        # caps; both counts come from the DB, not this process.
        opened_this_week = sum(1 for r in recs if r.get("pr_url"))
        opened_today = _prs_opened_today()
        if opened_today is None:
            do_prs, pr_skip_reason = False, "daily_pr_count_unavailable"
        else:
            cap = min(max(0, _weekly_pr_cap() - opened_this_week),
                      max(0, _daily_pr_cap() - opened_today))
    if do_prs:
        opened = 0
        # Only top_gaps_4w + competitor_lacks get PRs; funnel_optimization
        # is a config/copy change (no scaffold), wildcard is exploratory.
        eligible = [r for r in recs
                     if r["kind"] in ("strategic_gap_4w", "competitor_lack")
                     and not r.get("pr_url")]
        for rec in eligible:
            if opened >= cap:
                break
            # the SAME ctx the model was handed, not a fresh gather: the
            # question is whether it cited what it was actually given.
            res = _open_scaffold_pr(rec, ctx=ctx)
            # NB: dedupe skips return ok=True WITHOUT pr_url — indexing
            # res["pr_url"] there raised KeyError and killed the run.
            if res.get("ok") and res.get("pr_url"):
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
        "rec_count":         len(recs or []),
        "pr_open_enabled":   do_prs,
        "pr_skip_reason":    pr_skip_reason,
        "pr_cap_this_run":   cap,
        "prs_attempted":     len(pr_results),
        "prs_opened":        sum(1 for p in pr_results if p.get("pr_url")),
        "pr_results":        pr_results,
        "recommendations":   recs or [],
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
    recs = _read_recs_for(week_of) or []
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
                        recs = _read_recs_for(week_of) or []
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
