"""
brain_self_perception.py — Brain reads its own dashboards (2026-06-07).
=======================================================================

PROBLEM
-------
Today the brain only WRITES: it proposes fixes, opens draft PRs, runs
strategic synthesis, ships LinkedIn posts, etc. It NEVER reads back the
outcome of what it shipped. Sentinel + funnel-health + media-mix + the
PR-outcome monitor all exist, but the brain itself has no mental model
of "what I shipped last 7 days vs how each system reacted".

This module closes that verification loop.

  1.  Once a day (04:00 UTC, before the 06:00 morning briefing) it
      fans out to every admin dashboard:
         • /admin/funnel-health   (paywall→signal→trial→paid, MRR, A/B)
         • /admin/brain-backlog   (stuck queue, L5 proposals, PR cap)
         • /admin/sentinel-inbox  (69 pages, grades, 5xx streaks)
         • /admin/media-mix       (LinkedIn velocity, topic mix, style A/B)
         • /admin/state-of-2026-pulse (claim health, visitor funnel)
         • /api/v1/qa/state-of-2026 (8 living claims)
         • brain_pr_outcomes      (last 7d of brain-authored PRs)
         • brain_strategic_recommendations (last 7d of recs + status)

  2.  Asks Claude (reasoning tier, opus-4-8 with fallback) the question:
      "Given this snapshot of what the brain shipped last 7d and how
      each system reacted, write a self-assessment: what worked, what
      didn't, what to do differently. Be honest — if a PR regressed
      sentinel, say so."

      Output format is a JSON object:
        {
          "summary": "<1-2 sentence overall state>",
          "wins":         [ {title, evidence, confidence}, ... ],
          "losses":       [ {title, evidence, confidence, root_cause}, ... ],
          "adjustments":  [ {title, change, rationale}, ... ]
        }

  3.  Persists every run to brain_self_perception (id, ran_at, wins,
      losses, adjustments, raw_context_size_kb, claude_cost_cents,
      model_used).

  4.  Strategic synthesis imports gather_self_perception_context() so
      the next L6 prompt sees: "Here's how you self-assessed last week".
      Recursive self-improvement loop closes.

  5.  Morning briefing imports latest_self_perception() so the operator
      reads the brain's own track-record one-liner with their coffee.

  6.  Admin dashboard at /admin/brain-backlog renders a new
      "Brain's Self-Assessment" card on the Strategic tab.

SAFETY
------
  · BRAIN_SELF_PERCEPTION_DISABLE=1  kill switch (default OFF=enabled)
  · BRAIN_SELF_PERCEPTION_DRY_RUN=1  renders without persisting / no
                                      Claude call (returns synthetic
                                      "would have asked X" envelope)
  · Cap 1 run/day — second call on the same UTC date silently no-ops
    and returns the existing row (idempotent like the L6 weekly gate).
  · Skip if no recent activity (≤2 brain-authored PRs in the last 7d
    means there's nothing to assess; return a "skipped: low_activity"
    sentinel row so the dashboard shows "brain was quiet this week").
  · Single Claude call per run (opus-4-8 → sonnet-4-5 → haiku-4-5
    fallback chain via brain_models.resolve_chain).
  · Per-input context budgets keep the prompt under 25 KB / ~6,250
    tokens. Cost per run ~$0.30 in + ~$0.30 out = ~$0.60.
  · Every external probe is fail-soft → empty envelope on miss; never
    raises a 500.
  · POST /run is admin-gated; the dashboard read endpoints are not (so
    the operator can refresh without re-entering the key).

ENDPOINTS
---------
  POST /api/v1/admin/brain/self-perception/run      trigger (admin)
  GET  /api/v1/admin/brain/self-perception/preview  dry-run prompt
  GET  /api/v1/brain/self-perception/latest         most recent row
  GET  /api/v1/brain/self-perception/history        last N rows
  GET  /api/v1/admin/brain/self-perception/status   config + health

CRON
----
  crawler_scheduler.SCHEDULE "brain_self_perception" (4, 4) — once a day
  at 04:00 UTC. Self-gates inside the runner via same-day idempotency
  (one row per UTC date in brain_self_perception). The 06:00 morning
  briefing reads the latest row.

ESTIMATED COST
--------------
  · Input context: ~22 KB JSON → ~5,500 tokens
  · Output: ~1,200 tokens (3 wins + 3 losses + 3 adjustments)
  · Per call: ~$0.08 in + ~$0.09 out = ~$0.17 (opus-4-8)
  · Per year: 365 runs * $0.17 = ~$62  (vs. $50/yr for L6)
  · Falls back to Sonnet-4.5 (~$0.025) if Opus 404s.

Spec source: 2026-06-07 brief "Brain reads its own dashboards — closes
verification loop". Companion: brain_strategic_planner consumes this
table; morning_briefing surfaces the one-liner.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from typing import Any, Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_self_perception_bp = Blueprint("brain_self_perception", __name__)


# ─── Config ─────────────────────────────────────────────────────────

_INTERNAL_BASE = (os.environ.get("INTERNAL_BASE_URL")
                  or "http://localhost:8080").rstrip("/")
_RAILWAY_BASE = "https://dchub-backend-production.up.railway.app"
_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()

# Min brain-authored PRs in last 7d to bother running. Below this we
# skip the Claude call and write a "low_activity" sentinel row so the
# dashboard still shows the gap honestly.
_MIN_RECENT_ACTIVITY = 2

# Per-input context budgets (chars). Keeps the prompt under ~25 KB.
_CTX_BUDGET = {
    "funnel":          3500,
    "backlog":         3500,
    "sentinel":        3500,
    "media":           3000,
    "state_of_2026":   2500,
    "pr_outcomes":     3500,
    "strategic_recs":  2500,
}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("BRAIN_SELF_PERCEPTION_DISABLE"))


def _dry_run_on() -> bool:
    return _truthy(os.environ.get("BRAIN_SELF_PERCEPTION_DRY_RUN"))


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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_self_perception (
    id                    BIGSERIAL PRIMARY KEY,
    ran_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ran_on                DATE NOT NULL DEFAULT CURRENT_DATE,
    summary               TEXT,
    wins                  JSONB NOT NULL DEFAULT '[]'::jsonb,
    losses                JSONB NOT NULL DEFAULT '[]'::jsonb,
    adjustments           JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_context_size_kb   NUMERIC,
    claude_cost_cents     NUMERIC,
    model_used            TEXT,
    activity_window_days  INTEGER DEFAULT 7,
    pr_count_in_window    INTEGER DEFAULT 0,
    skipped_reason        TEXT,
    raw_payload           JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_brain_self_perception_day
    ON brain_self_perception(ran_on);
CREATE INDEX IF NOT EXISTS ix_brain_self_perception_ran_at
    ON brain_self_perception(ran_at DESC);
"""


def _ensure_schema(c) -> None:
    """Create the table + indexes if absent. Defensive — never raises."""
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
        try: c.commit()
        except Exception: pass
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("brain_self_perception _ensure_schema failed: %s", e)


# ─── Context gatherers ─────────────────────────────────────────────

def _http_get_json(path: str, timeout: int = 8) -> dict:
    """Fetch JSON from a backend route. External Railway base first
    (sibling worker) → loopback fallback. Mirrors brain_strategic_planner.

    Returns {} on any failure — never raises."""
    import urllib.request
    headers = {"X-Internal-Probe": "1",
               "User-Agent": "dchub-brain-self-perception/1.0"}
    ak = _admin_key()
    if ak:
        headers["X-Admin-Key"] = ak
        headers["X-Internal-Key"] = ak
    for base in (_RAILWAY_BASE, _INTERNAL_BASE):
        try:
            req = urllib.request.Request(f"{base}{path}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="ignore")
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, (dict, list)) else {}
        except Exception:
            continue
    return {}


def _call_in_process(getter_path: str) -> Optional[dict]:
    """Bypass HTTP and call the data builder directly. Avoids the
    1-worker self-request deadlock (Railway runs one replica). Mirrors
    morning_briefing._call_in_process.

    Returns None on any error → caller falls back to _http_get_json."""
    try:
        if getter_path == "funnel-health":
            from routes.funnel_health import _data_cached
            return _data_cached() or {}
        if getter_path == "brain-backlog":
            from routes.brain_backlog_admin import _backlog_snapshot
            return _backlog_snapshot() or {}
        if getter_path == "sentinel-inbox":
            from routes.site_sentinel import (
                latest_results, consec_failure_state, _grade,
            )
            try: rows = latest_results() or []
            except Exception: rows = []
            try:
                consec = {x["path"]: x
                          for x in (consec_failure_state() or [])}
            except Exception:
                consec = {}
            items = []
            for r in rows:
                p = r.get("path") or ""
                cs = consec.get(p, {})
                items.append({
                    "path": p,
                    "label": r.get("label") or p,
                    "category": r.get("category"),
                    "healthy": bool(r.get("healthy")),
                    "grade": _grade(r, cs),
                    "consecutive_5xx": int(cs.get("consecutive_5xx", 0) or 0),
                })
            healthy = sum(1 for i in items if i["healthy"])
            grades = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
            for i in items:
                grades[i.get("grade", "C")] = grades.get(
                    i.get("grade", "C"), 0) + 1
            return {
                "total":     len(items),
                "healthy":   healthy,
                "unhealthy": len(items) - healthy,
                "grades":    grades,
                "worst": [i for i in items
                          if i.get("grade") in ("D", "F")
                          or i.get("consecutive_5xx", 0) >= 2][:10],
            }
    except Exception:
        return None
    return None


def _truncate(obj: Any, budget: int) -> str:
    """Serialize + hard-truncate to a char budget."""
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) <= budget:
        return s
    return s[: budget - 18] + " /* …truncated */"


def _gather_pr_outcomes(window_days: int = 7) -> dict:
    """Pull brain_pr_outcomes for the window. Mirrors brain_strategic_
    planner._gather_outcomes_context but with a 7d window and a strict
    "did sentinel improve" framing."""
    c = _get_db()
    if c is None:
        return {"_note": "no_db", "window_days": window_days,
                "recent": [], "by_outcome": {}, "merged_total": 0}
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT outcome, COUNT(*) FROM brain_pr_outcomes
                    WHERE brain_authored = TRUE
                      AND COALESCE(merged_at, created_at)
                          > NOW() - INTERVAL '%s days'
                    GROUP BY outcome""", (window_days,))
            by_outcome = {r[0]: int(r[1]) for r in cur.fetchall() or []}
            cur.execute(
                """SELECT pr_number, pr_url, pr_title, sentinel_endpoint,
                          sentinel_before_grade, sentinel_after_grade,
                          outcome, regression_details, files_changed,
                          merged_at, created_at
                     FROM brain_pr_outcomes
                    WHERE brain_authored = TRUE
                      AND COALESCE(merged_at, created_at)
                          > NOW() - INTERVAL '%s days'
                    ORDER BY COALESCE(merged_at, created_at) DESC
                    LIMIT 30""", (window_days,))
            recent = []
            for r in cur.fetchall() or []:
                recent.append({
                    "pr_number":  r[0],
                    "pr_url":     r[1],
                    "title":      (r[2] or "")[:120],
                    "endpoint":   r[3],
                    "before":     (float(r[4]) if r[4] is not None
                                    else None),
                    "after":      (float(r[5]) if r[5] is not None
                                    else None),
                    "outcome":    r[6],
                    "regression": (r[7] or "")[:200],
                    "files":      (r[8] or "")[:200],
                    "merged_at":  str(r[9]) if r[9] else None,
                    "created_at": str(r[10]) if r[10] else None,
                })
        merged_n = sum(by_outcome.get(k, 0) for k in
                       ("success", "regression", "unknown", "deploy_fail"))
        success = by_outcome.get("success", 0)
        return {
            "window_days":     window_days,
            "by_outcome":      by_outcome,
            "merged_total":    merged_n,
            "success_rate":    (round(success / merged_n, 3)
                                 if merged_n else None),
            "regression_rate": (round(by_outcome.get("regression", 0)
                                       / merged_n, 3)
                                 if merged_n else None),
            "recent":          recent,
        }
    except Exception as e:
        logger.warning("self_perception: pr_outcomes read failed: %s", e)
        return {"_note": "read_failed", "error": str(e)[:160],
                "window_days": window_days, "recent": [],
                "by_outcome": {}, "merged_total": 0}
    finally:
        try: c.close()
        except Exception: pass


def _gather_strategic_recs(window_days: int = 7) -> dict:
    """Recent strategic recommendations + whether they spawned PRs."""
    c = _get_db()
    if c is None:
        return {"_note": "no_db", "recs": []}
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT week_of, kind, title, status, pr_url,
                          confidence, dollar_lift_est, created_at
                     FROM brain_strategic_recommendations
                    WHERE created_at > NOW() - INTERVAL '%s days'
                      AND kind != 'synthesis_meta'
                    ORDER BY created_at DESC
                    LIMIT 30""", (window_days,))
            rows = cur.fetchall() or []
        recs = []
        for r in rows:
            recs.append({
                "week_of":     str(r[0]),
                "kind":        r[1],
                "title":       (r[2] or "")[:140],
                "status":      r[3],
                "pr_url":      r[4],
                "confidence":  (float(r[5]) if r[5] is not None else None),
                "dollar_lift": (float(r[6]) if r[6] is not None else None),
            })
        return {"window_days": window_days, "count": len(recs),
                "recs": recs}
    except Exception as e:
        logger.warning("self_perception: strategic_recs read failed: %s", e)
        return {"_note": "read_failed", "error": str(e)[:160], "recs": []}
    finally:
        try: c.close()
        except Exception: pass


def _gather_self_perception_history(window_days: int = 14) -> list:
    """Read the brain's own past self-assessments so the prompt sees its
    own historical track record + avoids whiplash."""
    c = _get_db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT ran_on, summary,
                          jsonb_array_length(wins),
                          jsonb_array_length(losses),
                          jsonb_array_length(adjustments)
                     FROM brain_self_perception
                    WHERE ran_on > CURRENT_DATE - INTERVAL '%s days'
                      AND skipped_reason IS NULL
                    ORDER BY ran_on DESC
                    LIMIT 10""", (window_days,))
            rows = cur.fetchall() or []
        return [{"ran_on": str(r[0]),
                 "summary": (r[1] or "")[:200],
                 "wins": int(r[2] or 0),
                 "losses": int(r[3] or 0),
                 "adjustments": int(r[4] or 0)} for r in rows]
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass


def gather_self_perception() -> dict:
    """Fan out to every admin dashboard the brain owns. Each source is
    fail-soft (empty dict on miss). The total payload size is logged so
    we can track context bloat over time."""
    started = _dt.datetime.now(_dt.timezone.utc)

    # In-process where possible (avoids the 1-worker deadlock),
    # external HTTP otherwise.
    funnel = (_call_in_process("funnel-health")
              or _http_get_json("/api/v1/admin/funnel-health?format=json")
              or {})
    backlog = (_call_in_process("brain-backlog")
               or _http_get_json("/api/v1/admin/brain/backlog")
               or {})
    sentinel = (_call_in_process("sentinel-inbox")
                or _http_get_json("/api/v1/admin/sentinel-inbox")
                or {})

    # Media: aggregate pulse + topic-pulse + style A/B
    media_pulse = _http_get_json("/api/v1/media/pulse") or {}
    media_topics = _http_get_json("/api/v1/media/topic-pulse") or {}
    media_envelope = {
        "verdict": media_pulse.get("verdict"),
        "components": (media_pulse.get("components") or {}),
        "topics": (media_topics.get("topics") or [])[:10],
    }

    # State of 2026: claim health (qa endpoint) + visitor funnel
    state_qa = _http_get_json("/api/v1/qa/state-of-2026") or {}
    state_envelope = {
        "claims": (state_qa.get("claims") or [])[:10],
        "passing": sum(1 for c in (state_qa.get("claims") or [])
                       if c.get("status") == "passing"),
        "total": len(state_qa.get("claims") or []),
    }

    # Brain's own track record + past self-assessments
    pr_outcomes = _gather_pr_outcomes(window_days=7)
    strategic_recs = _gather_strategic_recs(window_days=7)
    past_perceptions = _gather_self_perception_history(window_days=14)

    return {
        "_gathered_at":    started.isoformat(),
        "funnel":          funnel,
        "backlog":         backlog,
        "sentinel":        sentinel,
        "media":           media_envelope,
        "state_of_2026":   state_envelope,
        "pr_outcomes":     pr_outcomes,
        "strategic_recs":  strategic_recs,
        "past_perceptions": past_perceptions,
    }


# Public helper: brain_strategic_planner imports this so the next
# synthesis prompt sees the brain's most recent self-assessment.
def gather_self_perception_context(window_days: int = 14) -> dict:
    """Return the latest self-perception row + a 14d roll-up so the
    strategic planner can feed it into its prompt as "here's how you
    self-assessed last week".

    Shape: {latest: {ran_on, summary, wins, losses, adjustments},
             history: [{ran_on, summary, ...}, ...],
             window_days, _note}.

    Fail-soft → empty envelope on miss (table may not exist yet on first
    deploy)."""
    c = _get_db()
    if c is None:
        return {"_note": "no_db", "latest": None, "history": []}
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT ran_on, summary, wins, losses, adjustments,
                          model_used, claude_cost_cents, skipped_reason,
                          pr_count_in_window
                     FROM brain_self_perception
                    WHERE skipped_reason IS NULL
                    ORDER BY ran_on DESC LIMIT 1""")
            row = cur.fetchone()
            latest = None
            if row:
                wins = row[2] if isinstance(row[2], list) else (
                    json.loads(row[2]) if row[2] else [])
                losses = row[3] if isinstance(row[3], list) else (
                    json.loads(row[3]) if row[3] else [])
                adj = row[4] if isinstance(row[4], list) else (
                    json.loads(row[4]) if row[4] else [])
                latest = {
                    "ran_on": str(row[0]),
                    "summary": row[1],
                    "wins": wins, "losses": losses, "adjustments": adj,
                    "model_used": row[5],
                    "cost_cents": (float(row[6])
                                    if row[6] is not None else None),
                    "pr_count_in_window": int(row[8] or 0),
                }
            cur.execute(
                """SELECT ran_on, summary,
                          jsonb_array_length(wins),
                          jsonb_array_length(losses),
                          jsonb_array_length(adjustments)
                     FROM brain_self_perception
                    WHERE ran_on > CURRENT_DATE - INTERVAL '%s days'
                      AND skipped_reason IS NULL
                    ORDER BY ran_on DESC LIMIT 14""", (window_days,))
            history = []
            for r in (cur.fetchall() or []):
                history.append({
                    "ran_on": str(r[0]),
                    "summary": (r[1] or "")[:200],
                    "wins": int(r[2] or 0),
                    "losses": int(r[3] or 0),
                    "adjustments": int(r[4] or 0),
                })
        return {"window_days": window_days,
                "latest": latest, "history": history}
    except Exception as e:
        return {"_note": "read_failed", "error": str(e)[:160],
                "latest": None, "history": []}
    finally:
        try: c.close()
        except Exception: pass


# Helper used by morning_briefing: a single one-line headline.
def latest_self_perception_one_liner() -> dict:
    """Return {summary, wins_count, losses_count, ran_on} for the most
    recent assessment so the morning briefing can render a one-liner."""
    ctx = gather_self_perception_context(window_days=1)
    latest = ctx.get("latest") or {}
    if not latest:
        return {"summary": None, "wins_count": 0, "losses_count": 0,
                "ran_on": None}
    return {
        "summary": latest.get("summary"),
        "wins_count": len(latest.get("wins") or []),
        "losses_count": len(latest.get("losses") or []),
        "ran_on": latest.get("ran_on"),
    }


# ─── Prompt builder ────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the DC Hub Brain's SELF-ASSESSMENT voice.

Every day, the brain ships things: opens draft PRs, runs strategic
synthesis, posts to LinkedIn, runs the media spike responder, etc.
Today's job: read the dashboards that record what happened next, and
write an honest assessment of what worked, what didn't, and what to do
differently. You are NOT proposing new features (that's L6). You are
GRADING the brain's recent output.

Output a single JSON object with EXACTLY this shape (no markdown
fences, no commentary, just the JSON):

{
  "summary": "<2-3 sentence honest state of the brain's recent output>",
  "wins": [
    {
      "title": "<short noun phrase, <70 chars>",
      "evidence": "<cite specific dashboard signal — e.g. 'sentinel grade improved D→A on /admin/funnel-health after PR #1032'>",
      "confidence": "high|medium|low"
    }
  ],
  "losses": [
    {
      "title": "<short noun phrase>",
      "evidence": "<cite the regression — e.g. 'PR #1029 regressed sentinel B→D on /api/v1/feedback'>",
      "root_cause": "<one sentence on WHY it failed>",
      "confidence": "high|medium|low"
    }
  ],
  "adjustments": [
    {
      "title": "<short noun phrase>",
      "change": "<one sentence: the concrete change to brain behavior, ENV var, or pattern>",
      "rationale": "<one sentence: why this change follows from the evidence above>"
    }
  ]
}

RULES
─────
1. Be HONEST. If a PR regressed sentinel, say so. If LinkedIn velocity
   collapsed, say so. Self-congratulatory assessments get you fired.
   It's OK to have zero wins or zero losses — only cite what's real.
2. EVERY win and every loss MUST cite a SPECIFIC dashboard signal from
   the context below (PR number, sentinel grade, conversion rate, etc.).
   No hand-waving like "media is doing well" without evidence.
3. Aim for 2-4 wins, 1-3 losses, 1-3 adjustments. Anywhere from 0-5 in
   each slot is acceptable; quality > quantity.
4. adjustments are CONCRETE — name an env var to flip, a confidence
   floor to lower, a detector to retire, a copy variant to A/B against.
5. If past_perceptions show you said the SAME thing 3 days running and
   nothing changed, escalate the adjustment ("operator hasn't acted on
   X — propose X' as a kill-switched safer alternative").
6. confidence='high' is reserved for chains with ≥2 cited signals.
7. Reply with ONLY the JSON object. No prose before or after."""


def _build_prompt(ctx: dict) -> str:
    """Assemble the full prompt with per-input truncation."""
    sections = []
    sections.append("FUNNEL HEALTH (paywall→signal→trial→paid + A/B):\n" +
                    _truncate(ctx.get("funnel"), _CTX_BUDGET["funnel"]))
    sections.append("BRAIN BACKLOG (L5 proposals + stuck queue):\n" +
                    _truncate(ctx.get("backlog"), _CTX_BUDGET["backlog"]))
    sections.append("SENTINEL INBOX (69 pages, grades, 5xx streaks):\n" +
                    _truncate(ctx.get("sentinel"),
                              _CTX_BUDGET["sentinel"]))
    sections.append("MEDIA (LinkedIn velocity + topic mix + verdict):\n" +
                    _truncate(ctx.get("media"), _CTX_BUDGET["media"]))
    sections.append("STATE OF 2026 (8 living narrative claims):\n" +
                    _truncate(ctx.get("state_of_2026"),
                              _CTX_BUDGET["state_of_2026"]))
    sections.append("PR OUTCOMES — past 7d, brain-authored ONLY:\n" +
                    _truncate(ctx.get("pr_outcomes"),
                              _CTX_BUDGET["pr_outcomes"]))
    sections.append("STRATEGIC RECS — past 7d (your own proposals):\n" +
                    _truncate(ctx.get("strategic_recs"),
                              _CTX_BUDGET["strategic_recs"]))
    sections.append("PAST SELF-ASSESSMENTS — last 14d (your own history; "
                    "don't whiplash):\n" +
                    _truncate(ctx.get("past_perceptions"), 1500))
    return ("\n\n".join(sections) +
            "\n\n──── End context. Reply with the JSON object ONLY. ────")


# ─── Claude call ───────────────────────────────────────────────────

def _call_claude(prompt: str) -> Optional[dict]:
    """Call Claude (reasoning tier). Walks the brain_models fallback
    chain on 404. Mirrors brain_strategic_planner._call_claude."""
    if not _ANTHROPIC_KEY:
        logger.warning("self_perception: ANTHROPIC_API_KEY unset")
        return None

    try:
        from routes.brain_models import brain_model_for, resolve_chain
        from utils.anthropic_helper import anthropic_messages_url
    except Exception as e:
        logger.warning("self_perception: model import failed: %s", e)
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
                    "User-Agent":        "dchub-brain-self-perception/1.0",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 2200,
                    "system":     _SYSTEM_PROMPT,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )
            if r.status_code == 404:
                last_err = f"{model}:404"
                logger.info(
                    "self_perception: %s 404 → fallback", model)
                continue
            if r.status_code != 200:
                last_err = f"{model}:{r.status_code}:{r.text[:200]}"
                logger.warning("self_perception: %s", last_err)
                continue
            body = r.json() or {}
            text = "".join(
                b.get("text", "")
                for b in (body.get("content") or [])
                if b.get("type") == "text"
            ).strip()
            if text.startswith("```"):
                text = (text.split("```", 2)[1]
                        if "```" in text else text)
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
                    "self_perception: JSON parse failed (%s); first 200: "
                    "%s", je, text[:200])
                last_err = f"parse:{je}"
                continue
        except Exception as e:
            last_err = f"{model}:exc:{e}"
            logger.warning("self_perception: %s exc: %s", model, e)
            continue
    logger.error("self_perception: all models failed; last=%s", last_err)
    return None


def _estimate_cost_cents(in_tok: int, out_tok: int,
                          model: str) -> float:
    """Rough cost estimate in CENTS. Public Anthropic pricing as of
    2026-06: opus-4-8 $15/M in + $75/M out;
              sonnet-4-5 $3/M in + $15/M out;
              haiku-4-5 $0.80/M in + $4/M out."""
    rates = {
        "claude-opus-4-8":     (15.0, 75.0),
        "claude-opus-4-7":     (15.0, 75.0),
        "claude-opus-4-5":     (15.0, 75.0),
        "claude-sonnet-4-5":   (3.0,  15.0),
        "claude-haiku-4-5":    (0.80, 4.0),
    }
    r_in, r_out = rates.get(model, (3.0, 15.0))
    usd = in_tok / 1e6 * r_in + out_tok / 1e6 * r_out
    return round(usd * 100, 3)


# ─── Persistence ───────────────────────────────────────────────────

def persist_self_perception(payload: dict, ctx_size_kb: float,
                              activity_window_days: int,
                              pr_count_in_window: int,
                              skipped_reason: Optional[str] = None
                              ) -> int:
    """Write one row per run. Returns inserted row id (or 0 on miss).

    Idempotency: UNIQUE(ran_on) means a second call same UTC date is a
    no-op (we DO returning id of the existing row instead so the caller
    can fetch + render it)."""
    c = _get_db()
    if c is None:
        return 0
    _ensure_schema(c)
    summary = (payload.get("summary") or "")[:1200] if payload else None
    wins = payload.get("wins") if payload else []
    losses = payload.get("losses") if payload else []
    adj = payload.get("adjustments") if payload else []
    model = payload.get("_model_used") if payload else None
    in_tok = (payload.get("_input_tokens_est") or 0) if payload else 0
    out_tok = (payload.get("_output_tokens_est") or 0) if payload else 0
    cost_cents = _estimate_cost_cents(in_tok, out_tok,
                                       model or "claude-opus-4-8")
    raw_payload = payload or {}
    try:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_self_perception (
                       summary, wins, losses, adjustments,
                       raw_context_size_kb, claude_cost_cents,
                       model_used, activity_window_days,
                       pr_count_in_window, skipped_reason, raw_payload
                   ) VALUES (
                       %s, %s::jsonb, %s::jsonb, %s::jsonb,
                       %s, %s, %s, %s, %s, %s, %s::jsonb
                   )
                   ON CONFLICT (ran_on) DO UPDATE
                       SET summary = COALESCE(EXCLUDED.summary,
                                               brain_self_perception.summary),
                           wins = CASE
                               WHEN jsonb_array_length(EXCLUDED.wins) > 0
                                  THEN EXCLUDED.wins
                               ELSE brain_self_perception.wins END,
                           losses = CASE
                               WHEN jsonb_array_length(EXCLUDED.losses) > 0
                                  THEN EXCLUDED.losses
                               ELSE brain_self_perception.losses END,
                           adjustments = CASE
                               WHEN jsonb_array_length(EXCLUDED.adjustments) > 0
                                  THEN EXCLUDED.adjustments
                               ELSE brain_self_perception.adjustments END,
                           raw_context_size_kb = EXCLUDED.raw_context_size_kb,
                           claude_cost_cents = EXCLUDED.claude_cost_cents,
                           model_used = COALESCE(EXCLUDED.model_used,
                                                  brain_self_perception.model_used),
                           pr_count_in_window = EXCLUDED.pr_count_in_window,
                           skipped_reason = EXCLUDED.skipped_reason,
                           raw_payload = EXCLUDED.raw_payload,
                           ran_at = NOW()
                   RETURNING id""",
                (summary,
                 json.dumps(wins or []),
                 json.dumps(losses or []),
                 json.dumps(adj or []),
                 round(ctx_size_kb, 2), cost_cents, model,
                 activity_window_days, pr_count_in_window,
                 skipped_reason, json.dumps(raw_payload, default=str)))
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        return int(row[0]) if row else 0
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.error("self_perception: persist failed: %s", e)
        return 0
    finally:
        try: c.close()
        except Exception: pass


def _read_row_for_today() -> Optional[dict]:
    """Read today's row (if any) for the idempotency check."""
    c = _get_db()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, ran_at, summary, wins, losses, adjustments,
                          model_used, claude_cost_cents,
                          pr_count_in_window, skipped_reason,
                          raw_context_size_kb
                     FROM brain_self_perception
                    WHERE ran_on = CURRENT_DATE
                    ORDER BY ran_at DESC LIMIT 1""")
            row = cur.fetchone()
        if not row:
            return None
        wins = row[3] if isinstance(row[3], list) else (
            json.loads(row[3]) if row[3] else [])
        losses = row[4] if isinstance(row[4], list) else (
            json.loads(row[4]) if row[4] else [])
        adj = row[5] if isinstance(row[5], list) else (
            json.loads(row[5]) if row[5] else [])
        return {
            "id": int(row[0]),
            "ran_at": row[1].isoformat() if row[1] else None,
            "summary": row[2],
            "wins": wins, "losses": losses, "adjustments": adj,
            "model_used": row[6],
            "cost_cents": (float(row[7])
                            if row[7] is not None else None),
            "pr_count_in_window": int(row[8] or 0),
            "skipped_reason": row[9],
            "context_size_kb": (float(row[10])
                                  if row[10] is not None else None),
        }
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


# ─── Synthesize ─────────────────────────────────────────────────────

def synthesize_self_perception(ctx: dict) -> Optional[dict]:
    """Build the prompt, call Claude. Returns parsed payload or None."""
    prompt = _build_prompt(ctx)
    return _call_claude(prompt)


# ─── Top-level runner ──────────────────────────────────────────────

def run_self_perception(force: bool = False,
                          dry_run: Optional[bool] = None) -> dict:
    """End-to-end daily run. Returns a summary dict (never raises).

    Idempotency: same UTC date skips unless force=True. Low-activity
    skip writes a sentinel row with skipped_reason='low_activity' so the
    dashboard still shows the brain was quiet.
    """
    started = _dt.datetime.now(_dt.timezone.utc)

    if _kill_switch_on():
        return {"ok": False, "reason": "kill_switch_on",
                "env": "BRAIN_SELF_PERCEPTION_DISABLE=1"}

    do_dry = _dry_run_on() if dry_run is None else bool(dry_run)

    # Idempotency: one row per UTC date unless force=True
    existing = _read_row_for_today()
    if existing and not force:
        return {"ok": True, "from_cache": True,
                "ran_on": started.date().isoformat(),
                "row_id": existing.get("id"),
                "note": ("Already ran today; pass force=1 to recompute. "
                          "Cap = 1/day."),
                "row": existing}

    ctx = gather_self_perception()
    ctx_json = json.dumps(ctx, default=str)
    ctx_size_kb = len(ctx_json) / 1024.0

    # Activity gate: skip Claude call if the brain was quiet
    pr_window = ((ctx.get("pr_outcomes") or {}).get("merged_total") or 0)
    rec_window = ((ctx.get("strategic_recs") or {}).get("count") or 0)
    total_activity = pr_window + rec_window
    if total_activity < _MIN_RECENT_ACTIVITY:
        # Write a sentinel row so the dashboard shows we DID run
        row_id = persist_self_perception(
            payload={"summary":
                     (f"Skipped: brain was quiet (only {total_activity} "
                       f"recent items; cutoff={_MIN_RECENT_ACTIVITY})."),
                     "wins": [], "losses": [], "adjustments": [],
                     "_model_used": "none"},
            ctx_size_kb=ctx_size_kb,
            activity_window_days=7,
            pr_count_in_window=pr_window,
            skipped_reason="low_activity",
        )
        return {"ok": True, "skipped": "low_activity",
                "row_id": row_id,
                "pr_count_in_window": pr_window,
                "rec_count_in_window": rec_window,
                "min_activity_required": _MIN_RECENT_ACTIVITY,
                "context_size_kb": round(ctx_size_kb, 2)}

    if do_dry:
        prompt = _build_prompt(ctx)
        return {"ok": True, "dry_run": True,
                "context_size_kb": round(ctx_size_kb, 2),
                "prompt_chars": len(prompt),
                "prompt_tokens_est": len(prompt) // 4,
                "pr_count_in_window": pr_window,
                "rec_count_in_window": rec_window,
                "prompt_first_500": prompt[:500],
                "prompt_last_500":  prompt[-500:]}

    t0 = time.time()
    payload = synthesize_self_perception(ctx)
    claude_ms = int((time.time() - t0) * 1000)
    if not payload:
        return {"ok": False, "reason": "claude_call_failed",
                "context_size_kb": round(ctx_size_kb, 2),
                "claude_ms": claude_ms}

    row_id = persist_self_perception(
        payload=payload,
        ctx_size_kb=ctx_size_kb,
        activity_window_days=7,
        pr_count_in_window=pr_window,
        skipped_reason=None,
    )

    finished = _dt.datetime.now(_dt.timezone.utc)
    return {
        "ok": True, "from_cache": False,
        "ran_on":            started.date().isoformat(),
        "row_id":            row_id,
        "model_used":        payload.get("_model_used"),
        "context_size_kb":   round(ctx_size_kb, 2),
        "claude_ms":         claude_ms,
        "duration_ms":       int((finished - started).total_seconds()
                                  * 1000),
        "input_tokens_est":  payload.get("_input_tokens_est"),
        "output_tokens_est": payload.get("_output_tokens_est"),
        "cost_cents":        _estimate_cost_cents(
            payload.get("_input_tokens_est") or 0,
            payload.get("_output_tokens_est") or 0,
            payload.get("_model_used") or "claude-opus-4-8"),
        "summary":           payload.get("summary"),
        "wins_count":        len(payload.get("wins") or []),
        "losses_count":      len(payload.get("losses") or []),
        "adjustments_count": len(payload.get("adjustments") or []),
        "payload":           {k: v for k, v in payload.items()
                              if not k.startswith("_")},
    }


# ─── HTTP routes ────────────────────────────────────────────────────

@brain_self_perception_bp.route(
    "/api/v1/admin/brain/self-perception/run", methods=["POST", "GET"])
def self_perception_run():
    """Trigger the daily run. Admin-gated.

    Query/body params:
      force=1     bypass same-day idempotency
      dry_run=1   render prompt + skip Claude call"""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    force = (_truthy(request.args.get("force"))
              or _truthy(body.get("force")))
    dr = request.args.get("dry_run") or body.get("dry_run")
    dry = None if dr is None else _truthy(dr)
    result = run_self_perception(force=force, dry_run=dry)
    return jsonify(result), 200


@brain_self_perception_bp.route(
    "/api/v1/admin/brain/self-perception/preview", methods=["GET"])
def self_perception_preview():
    """Dry-run: gather context + return the prompt that WOULD be sent
    to Claude today, without spending tokens. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    ctx = gather_self_perception()
    prompt = _build_prompt(ctx)
    ctx_json = json.dumps(ctx, default=str)
    pr_window = ((ctx.get("pr_outcomes") or {}).get("merged_total") or 0)
    rec_window = ((ctx.get("strategic_recs") or {}).get("count") or 0)
    return jsonify(
        ok=True, dry_run=True,
        ran_on=_dt.date.today().isoformat(),
        context_size_kb=round(len(ctx_json) / 1024.0, 2),
        prompt_chars=len(prompt),
        prompt_tokens_est=len(prompt) // 4,
        activity={
            "pr_count_in_window":   pr_window,
            "rec_count_in_window":  rec_window,
            "min_activity_required": _MIN_RECENT_ACTIVITY,
            "would_skip_low_activity":
                (pr_window + rec_window) < _MIN_RECENT_ACTIVITY,
        },
        config={
            "kill_switch":      _kill_switch_on(),
            "dry_run_env":      _dry_run_on(),
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


@brain_self_perception_bp.route(
    "/api/v1/brain/self-perception/latest", methods=["GET"])
def self_perception_latest():
    """Read-only: return the most recent self-assessment. PUBLIC read."""
    ctx = gather_self_perception_context(window_days=14)
    return jsonify(ok=True, **ctx), 200


@brain_self_perception_bp.route(
    "/api/v1/brain/self-perception/history", methods=["GET"])
def self_perception_history():
    """Last N rows (titles + counts only). PUBLIC read."""
    try:
        days = int(request.args.get("days") or 30)
    except Exception:
        days = 30
    days = max(1, min(days, 180))
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, ran_on, summary,
                          jsonb_array_length(wins),
                          jsonb_array_length(losses),
                          jsonb_array_length(adjustments),
                          model_used, claude_cost_cents,
                          pr_count_in_window, skipped_reason
                     FROM brain_self_perception
                    WHERE ran_on > CURRENT_DATE - INTERVAL '%s days'
                    ORDER BY ran_on DESC LIMIT 60""", (days,))
            rows = cur.fetchall() or []
        return jsonify(
            ok=True, days_requested=days,
            history=[
                {"id": int(r[0]), "ran_on": str(r[1]),
                 "summary": (r[2] or "")[:240],
                 "wins": int(r[3] or 0),
                 "losses": int(r[4] or 0),
                 "adjustments": int(r[5] or 0),
                 "model_used": r[6],
                 "cost_cents": (float(r[7])
                                  if r[7] is not None else None),
                 "pr_count_in_window": int(r[8] or 0),
                 "skipped_reason": r[9]}
                for r in rows],
        ), 200
    finally:
        try: c.close()
        except Exception: pass


@brain_self_perception_bp.route(
    "/api/v1/admin/brain/self-perception/status", methods=["GET"])
def self_perception_status():
    """Operator-facing config + health."""
    today = _read_row_for_today()
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        dry_run_env=_dry_run_on(),
        anthropic_key_set=bool(_ANTHROPIC_KEY),
        admin_key_set=bool(_admin_key()),
        min_activity_required=_MIN_RECENT_ACTIVITY,
        today_already_ran=bool(today),
        today_summary=(today or {}).get("summary"),
        env_overrides={
            "BRAIN_SELF_PERCEPTION_DISABLE": "kill switch (1=off)",
            "BRAIN_SELF_PERCEPTION_DRY_RUN":
                "render prompt only, skip Claude (1=on)",
        },
        endpoints={
            "POST /api/v1/admin/brain/self-perception/run":
                "trigger daily run (admin)",
            "GET  /api/v1/admin/brain/self-perception/preview":
                "dry-run prompt without Claude tokens (admin)",
            "GET  /api/v1/admin/brain/self-perception/status":
                "this page (admin)",
            "GET  /api/v1/brain/self-perception/latest":
                "most recent assessment (public read)",
            "GET  /api/v1/brain/self-perception/history":
                "last N days (public read)",
        },
    ), 200
