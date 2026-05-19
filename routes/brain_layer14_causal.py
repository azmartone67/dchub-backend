"""
Brain L14 — Causal Reasoner (2026-05-18).

L1-L9 each find symptoms in isolation. L8 prioritizes them. L14 ties
them together: when two layers flag related findings, L14 calls Claude
with the JOINED context and asks for the ROOT CAUSE plus a smallest-safe
fix.

Example causal chains L14 can recognize (input → reasoning → output):

  funnel.upgrade_signals_7d ↑ + funnel.conversions_30d flat
  → "paywall fires but users don't redeem"
  → root cause: redeem page perf OR email-capture friction OR pricing-page CTA
  → action: probe each candidate, propose A/B

  qa.iso_landing failing 3+ runs + auto-fix.iso_endpoint_unreachable
  → "auto-fix layer is firing but not curing"
  → root cause: the fix recipe (trigger workflow) doesn't address the
     actual failure (upstream ISO API down? CORS?)
  → action: read failure body, propose new recipe

  freshness.facilities >SLA + scheduler.facility_discovery runs=0
  → "ingestion job dead, not stale-data problem"
  → root cause: scheduler thread crashed OR admin key mismatch OR
     upstream API broken
  → action: check thread alive + admin key match + upstream

L14 is admin-gated (it can propose PRs); read-only candidates view is
public.

Endpoints:
  GET  /api/v1/brain/causal           — latest causal analysis (cached 1h)
  POST /api/v1/brain/causal/analyze   — admin: re-run with Claude
"""

import os
import json
import time
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer14_bp = Blueprint("brain_layer14", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

_CACHE = {"analysis": None, "computed_at": 0.0}
_TTL = 3600  # 1h — causal analyses are expensive, change slowly


def _internal(path: str, timeout: int = 8) -> dict:
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200: return {}
        return r.json() or {}
    except Exception:
        return {}


def _gather_joined_context() -> dict:
    """Pull state from EVERY layer that has a signal. The point of L14
    is to look across layers — not at any one in isolation.

    Phase FF+7-L18 (2026-05-19): also pulls L18 consolidated lessons +
    L16 calibration data so each L14 analysis is informed by what the
    brain has learned about itself."""
    return {
        "findings":       (_internal("/api/v1/brain/consistency-radar", 20)
                            .get("findings") or [])[:25],
        "funnel":         _internal("/api/v1/mcp/funnel"),
        "freshness":      _internal("/api/v1/freshness/radar"),
        "predictions":    (_internal("/api/v1/brain/predictions")
                            .get("predictions") or [])[:10],
        "proposed":       (_internal("/api/v1/brain/proposed-detectors")
                            .get("proposals") or [])[:5],
        "qa_agent":       _internal("/api/v1/brain/qa-agent"),
        "expansion":      _internal("/api/v1/brain/expansion"),
        "publisher":      _internal("/api/v1/marketing/worker-status"),
        "outreach":       _internal("/api/v1/media/outreach-log"),
        "schedulers":     _internal("/api/schedulers/audit"),
        # L18 lessons — what the brain has distilled from its own history
        "lessons":        (_internal("/api/v1/brain/lessons")
                            .get("active_lessons") or [])[:10],
        # L16 calibration — how often the brain's past predictions
        # at each confidence level have been correct
        "calibration":    _internal("/api/v1/brain/self-critique/calibration"),
    }


def _build_prompt(ctx: dict) -> str:
    return f"""You are the DC Hub brain L14 — the Causal Reasoner.

L1-L9 each report SYMPTOMS in isolation. L8 prioritizes them. Your job
is to find ROOT-CAUSE CHAINS by reading across layers. Most useful
output: "symptom A in layer X plus symptom B in layer Y both stem from
single root cause Z — and here's the smallest-safe fix."

Examples of what good cross-layer reasoning looks like:
  - "upgrade_signals_7d ↑ + conversions_30d flat → paywall fires but
     redeem-page conversion is broken. Probe /api/v1/redeem/funnel-stats
     and look for the actual leak stage."
  - "freshness.facilities >SLA + schedulers.facility_discovery.runs=0
     → ingestion-job dead, not stale-data problem. The fix is in
     scheduler health, not in any data pipeline."
  - "qa.iso_landing chronic-fail + 5 auto-fix attempts all 'success'
     → recipe is firing but not curing. The fix recipe is wrong.
     Examine the actual HTTP body of the failing probe."

Anti-patterns to avoid:
  - Listing each finding separately (that's L8's job, you're going deeper)
  - Suggesting fixes for symptoms — fix root causes
  - Speculating without naming the specific signal that supports your
    claim. If you can't cite a signal, you don't know yet.

╔══════════════════════════════════════════════════════════════════╗
║ META-COGNITION (Phase FF+7-L18, 2026-05-19):                     ║
║ This prompt is INFORMED BY THE BRAIN'S OWN TRACK RECORD.         ║
║                                                                  ║
║ L18 has distilled the brain's recent episodes into NAMED LESSONS ║
║ (see ctx.lessons below). Apply them — if a current chain matches ║
║ a known pattern from past episodes, name the lesson explicitly   ║
║ and use it to set confidence appropriately.                      ║
║                                                                  ║
║ L16 has tracked the brain's calibration (see ctx.calibration).   ║
║ If past "high" confidence chains have been correct <70% of the   ║
║ time, the brain is over-confident — be more conservative on      ║
║ "high" labels this time.                                         ║
║                                                                  ║
║ Use the lessons + calibration as input to your reasoning, not    ║
║ just as observations. They're how the brain learns from itself.  ║
╚══════════════════════════════════════════════════════════════════╝

Context (JSON):
{json.dumps(ctx, indent=2, default=str)[:9500]}

Return a JSON object (NO markdown fences) with this exact shape:

{{
  "summary": "<one-paragraph state of the system, 2-3 sentences>",
  "causal_chains": [
    {{
      "title": "<short noun phrase, <60 chars>",
      "symptoms": [
        "<finding/metric from layer X that participates in this chain>",
        "<finding/metric from layer Y that participates in this chain>"
      ],
      "root_cause_hypothesis": "<one sentence: what's actually broken>",
      "confidence": "high | medium | low",
      "smallest_safe_fix": "<concrete action — a curl, a code edit at file:line, an env var to check>",
      "verification": "<how to know if the fix worked — a curl that flips status, a metric that should change>"
    }}
  ],
  "single_highest_leverage": "<title of the chain that, if fixed first, helps the most other findings>",
  "stop_doing": "<one detector or layer that's adding noise without value — or null>"
}}

Cap at 4 chains. Quality over quantity. Reply with ONLY the JSON object."""


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
                  "max_tokens": 2500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        if r.status_code != 200:
            logger.warning(f"L14 Claude {r.status_code}: {r.text[:200]}")
            return None
        body = r.json() or {}
        text = "".join(b.get("text","") for b in (body.get("content") or [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"): text = text[4:].lstrip("\n")
        return json.loads(text)
    except Exception as e:
        logger.warning(f"L14 Claude call failed: {e}")
        return None


@brain_layer14_bp.route("/api/v1/brain/causal", methods=["GET"])
def causal():
    """Cached causal analysis. 1h TTL."""
    now = time.monotonic()
    if _CACHE["analysis"] and (now - _CACHE["computed_at"]) < _TTL:
        return jsonify(
            ok=True,
            analysis=_CACHE["analysis"],
            cached=True,
            cache_age_seconds=int(now - _CACHE["computed_at"]),
        )
    return jsonify(
        ok=False,
        analysis=None,
        note=("No causal analysis yet. POST /api/v1/brain/causal/analyze "
              "(admin) or wait for cron."),
    )


@brain_layer14_bp.route("/api/v1/brain/causal/analyze",
                        methods=["POST", "GET"])
def analyze():
    # Phase FF+7-emergency (2026-05-19) — KILL SWITCH. Same crash-loop
    # class as L8. Synchronous 30-90s Claude call. Disabled until
    # refactored to background-thread mode. GET /api/v1/brain/causal
    # serves the last cached analysis.
    if os.environ.get("CAUSAL_ANALYZE_ENABLED", "0") != "1":
        return jsonify(
            ok=False,
            disabled=True,
            reason=("Synchronous Claude call was crash-looping the "
                    "container (along with L8 orchestrator). Disabled "
                    "until refactor. GET /api/v1/brain/causal serves "
                    "the last cached analysis."),
        ), 503

    if request.method == "POST" and _ADMIN_KEY:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
    if not _ANTHROPIC_KEY:
        return jsonify(ok=False, error="ANTHROPIC_API_KEY not set"), 503

    ctx = _gather_joined_context()
    prompt = _build_prompt(ctx)
    analysis = _call_claude(prompt)
    if not analysis:
        return jsonify(ok=False, error="Claude call failed"), 503

    _CACHE["analysis"] = analysis
    _CACHE["computed_at"] = time.monotonic()

    return jsonify(
        ok=True,
        analysis=analysis,
        based_on={
            "findings":       len(ctx.get("findings", [])),
            "predictions":    len(ctx.get("predictions", [])),
            "freshness_ok":   bool(ctx.get("freshness")),
            "funnel_ok":      bool(ctx.get("funnel")),
            "schedulers_ok":  bool(ctx.get("schedulers")),
        },
        computed_at=_dt.datetime.utcnow().isoformat() + "Z",
    )
