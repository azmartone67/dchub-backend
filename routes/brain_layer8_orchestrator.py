"""
Brain L8 — Orchestrator (2026-05-19).

Reads ALL brain layer outputs (L0 findings + L3 memory + L6 predictions
+ L7 proposals) + funnel data + outreach log + last-24h commits, calls
Claude ONCE, and returns a prioritized action plan with confidence
scores. Replaces the human end-of-session "what to do tonight"
synthesis with an automated 6h cron.

What makes L8 different from L2 (narrative):
  L2 = 3-paragraph prose digest
  L8 = structured action plan with priorities + effort + confidence

Endpoints:
  GET  /api/v1/brain/orchestrator         cached plan (5min TTL)
  POST /api/v1/brain/orchestrator/refresh forces recompute (admin)
"""

import os
import json
import time
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer8_bp = Blueprint("brain_layer8", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

_CACHE = {"plan": None, "computed_at": 0.0}
_TTL = 300


def _internal(path: str, timeout: int = 8) -> dict:
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200: return {}
        return r.json() or {}
    except Exception:
        return {}


def _gather_context() -> dict:
    """Pull every brain layer output + funnel + outreach + commits into one dict."""
    findings = (_internal("/api/v1/brain/consistency-radar", 25).get("findings") or [])
    memory   = _internal("/api/v1/brain/memory/stats")
    predict  = _internal("/api/v1/brain/predictions")
    proposed = _internal("/api/v1/brain/proposed-detectors")
    funnel   = _internal("/api/v1/mcp/funnel")
    ws       = _internal("/api/v1/marketing/worker-status")
    outreach = _internal("/api/v1/media/outreach-log")
    # Phase FF+7 (2026-05-19): include L14 causal chains. L14 finds
    # root-cause groupings across layers — feeding its output into L8's
    # prompt gives the orchestrator a head-start on prioritization
    # ("don't list 46 findings — fix the 2 root causes that produce them").
    causal   = _internal("/api/v1/brain/causal", 6)
    # Phase FF+7 (2026-05-19): also include L11 QA results so L8 knows
    # which surfaces are currently failing/slow without re-probing.
    qa       = _internal("/api/v1/brain/qa-agent", 6)
    # Phase FF+7 (2026-05-19): redeem funnel for actual stage-level leak data
    redeem   = _internal("/api/v1/redeem/funnel-stats", 6)
    # Recent commits via GitHub API
    commits = []
    try:
        import requests
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
        since = (_dt.datetime.utcnow() - _dt.timedelta(hours=24)).isoformat() + "Z"
        h = {"Accept": "application/vnd.github+json"}
        if token: h["Authorization"] = f"Bearer {token}"
        r = requests.get(f"https://api.github.com/repos/{repo}/commits",
                         params={"since": since, "per_page": 30},
                         headers=h, timeout=10)
        if r.status_code == 200:
            commits = [(c.get("commit", {}).get("message") or "").split("\n")[0]
                       for c in (r.json() or [])][:20]
    except Exception: pass

    return {
        "findings_count":     len(findings),
        "findings_by_issue":  _by_issue(findings),
        "top_findings":       [{"issue": f.get("issue"),
                                "url":   f.get("url"),
                                "detail": (f.get("detail") or "")[:160]}
                               for f in findings[:8]],
        "memory_records":     memory.get("total_records", 0),
        "top_recurring":      memory.get("top_recurring_issues", [])[:5],
        "predictions":        (predict.get("predictions") or [])[:5],
        "proposed_detectors_pending": [p for p in (proposed.get("proposals") or [])
                                        if p.get("decision") == "pending"],
        "funnel": {
            "tool_calls_7d":      funnel.get("tool_calls_7d"),
            "upgrade_signals_7d": funnel.get("upgrade_signals_7d"),
            "conversions_30d":    funnel.get("conversions_30d"),
            "keys_by_tier":       funnel.get("keys_by_tier"),
            "addressable_paid_demand": (funnel.get("paid_tool_demand_30d") or [])[:3],
        },
        "publisher": {
            "status":       (ws.get("distribution") or {}).get("status"),
            "queued":       (ws.get("distribution") or {}).get("queued_unpublished"),
            "published_7d": (ws.get("distribution") or {}).get("published_7d"),
        },
        "outreach": {
            "total_sent": outreach.get("total"),
            "replied":    outreach.get("replied"),
            "reply_rate_pct": outreach.get("reply_rate_pct"),
            "recent":     (outreach.get("log") or [])[:5],
        },
        # Phase FF+7: L14 causal chains — pre-grouped root causes that
        # L8 should use as its starting point rather than re-deriving.
        "causal_chains": (causal.get("analysis") or {}).get("causal_chains", []),
        "causal_highest_leverage": (causal.get("analysis") or {}).get("single_highest_leverage"),
        # Phase FF+7: L11 QA — current surface health snapshot.
        "qa_verdict":   qa.get("verdict"),
        "qa_errors":    (qa.get("errors") or [])[:5],
        "qa_slow":      (qa.get("slow_pages") or [])[:5],
        # Phase FF+7: redeem funnel — actual stage-level numbers, not
        # just aggregate "0 conversions". Shows WHERE in the funnel
        # the leak is.
        "redeem_funnel": {
            "paywall_hit": (redeem.get("funnel_counts") or {}).get("paywall_hit"),
            "click":       (redeem.get("funnel_counts") or {}).get("click"),
            "view":        (redeem.get("funnel_counts") or {}).get("view"),
            "submit":      (redeem.get("funnel_counts") or {}).get("submit"),
            "upgrade":     (redeem.get("funnel_counts") or {}).get("upgrade"),
            "biggest_leak": redeem.get("biggest_leak"),
        },
        "recent_commits": commits,
    }


def _by_issue(findings: list[dict]) -> dict:
    by = {}
    for f in findings:
        k = f.get("issue", "?")
        by[k] = by.get(k, 0) + 1
    return dict(sorted(by.items(), key=lambda x: -x[1])[:10])


def _build_prompt(ctx: dict) -> str:
    return f"""You are the DC Hub brain Orchestrator (L8). You see EVERYTHING:
detector findings, memory of past fixes, velocity predictions, proposed
new detectors, funnel state, publisher state, journalist outreach log,
last 24h of git commits, L14's pre-computed CAUSAL CHAINS (cross-layer
root-cause groupings), L11 QA verdict + current errors/slow pages,
and the REAL redeem-funnel stage counts (paywall_hit -> click -> view
-> submit -> upgrade).

Synthesize one prioritized action plan for the founder.

Key heuristic: if `causal_chains` in the context is non-empty, those are
already-grouped root causes (L14 ran cross-layer analysis). Use them
as your starting point — don't re-derive the same conclusions from
individual findings. The `causal_highest_leverage` field names the one
chain L14 marked as highest impact. Promote that to action #1 unless
you have strong reason otherwise.

Context:
{json.dumps(ctx, indent=2, default=str)[:8000]}

Return a JSON object (NO markdown fences) with this exact shape:
{{
  "summary": "<one-paragraph state-of-the-system, ≤3 sentences>",
  "actions": [
    {{
      "title":      "<5-10 word action title>",
      "rationale":  "<1-2 sentence why-this-matters>",
      "effort":     "30s | 5min | 30min | 2h | 1day",
      "confidence": "high | medium | low",
      "priority":   1,
      "category":   "revenue | reliability | distribution | brain | infrastructure",
      "specific_command": "<exact shell command OR null if multi-step>"
    }}
  ],
  "watch_next_24h": [
    "<one short-line metric or signal to monitor>"
  ],
  "celebration":   "<one positive callout — what's working that shouldn't be ignored>"
}}

Order actions by priority (1 = do first). Cap at 5 actions. The founder
has finite attention; ruthless prioritization beats completeness.
Reply with ONLY the JSON object, no preamble."""


def _call_claude(prompt: str) -> dict | None:
    if not _ANTHROPIC_KEY: return None
    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-5",
                  "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45,
        )
        if r.status_code != 200:
            logger.warning(f"L8 Claude {r.status_code}: {r.text[:200]}")
            return None
        body = r.json() or {}
        text = "".join(b.get("text","") for b in (body.get("content") or [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"): text = text[4:].lstrip("\n")
        return json.loads(text)
    except Exception as e:
        logger.warning(f"L8 Claude call failed: {e}")
        return None


@brain_layer8_bp.route("/api/v1/brain/orchestrator", methods=["GET"])
def orchestrator():
    """Cached action plan. 5min TTL — recompute via /refresh."""
    now = time.monotonic()
    if _CACHE["plan"] and (now - _CACHE["computed_at"]) < _TTL:
        return jsonify(
            ok=True,
            plan=_CACHE["plan"],
            cached=True,
            cache_age_seconds=int(now - _CACHE["computed_at"]),
        ), 200
    return jsonify(
        ok=False,
        plan=None,
        note="Plan not yet computed. POST /api/v1/brain/orchestrator/refresh (admin) or wait for 6h cron.",
    ), 200


@brain_layer8_bp.route("/api/v1/brain/orchestrator/refresh",
                          methods=["POST", "GET"])
def orchestrator_refresh():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if request.method == "POST" and _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    if not _ANTHROPIC_KEY:
        return jsonify(ok=False, error="ANTHROPIC_API_KEY not set"), 503

    ctx = _gather_context()
    prompt = _build_prompt(ctx)
    plan = _call_claude(prompt)
    if not plan:
        return jsonify(ok=False, error="Claude call failed"), 503

    _CACHE["plan"] = plan
    _CACHE["computed_at"] = time.monotonic()

    return jsonify(
        ok=True,
        plan=plan,
        based_on={
            "findings_count":   ctx["findings_count"],
            "memory_records":   ctx["memory_records"],
            "predictions":      len(ctx["predictions"]),
            "proposed_detectors_pending": len(ctx["proposed_detectors_pending"]),
            "recent_commits":   len(ctx["recent_commits"]),
            "outreach_sent":    (ctx["outreach"] or {}).get("total_sent"),
        },
        computed_at=_dt.datetime.utcnow().isoformat() + "Z",
    ), 200
