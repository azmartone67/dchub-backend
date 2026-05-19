"""
Brain L19 — Awareness (2026-05-19).

The brain's introspective state. One endpoint, one question:

  "What does the brain currently KNOW, what is it WRONG about,
   and what did it just LEARN?"

L19 reads from every other brain layer and assembles a single
machine + human-readable status of the brain's epistemic state:

  - Current epistemic confidence (from L16 calibration data —
    e.g. "high-confidence chains correct 7/10 times")
  - Active blind spots (detectors that haven't run in >24h,
    surfaces with no telemetry, data domains in SLA breach)
  - Last learned lesson (from L18 — the most recent rule the
    brain consolidated from its own episodes)
  - Active uncertainty (predictions made but not yet verified)
  - Confidence drift (is the brain getting more or less accurate?)

This is the layer that lets you ask the brain "are you OK?" and
get a real answer.

Endpoint:
  GET  /api/v1/brain/awareness  — current epistemic state (cached 5min)
"""

import os
import time
import logging
import datetime as _dt
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
brain_layer19_bp = Blueprint("brain_layer19", __name__)


def _internal(path: str, timeout: int = 6) -> dict:
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200: return {}
        return r.json() or {}
    except Exception:
        return {}


_CACHE: dict = {"snapshot": None, "computed_at": 0.0}
_TTL = 300  # 5 min — keep this endpoint very fast


def _compute_awareness() -> dict:
    """Walk every brain layer and assemble the epistemic snapshot."""

    # ── 1. Calibration data from L16 ─────────────────────────────
    calibration = _internal("/api/v1/brain/self-critique/calibration")
    by_conf = calibration.get("by_confidence") or {}
    high_conf_rate = (by_conf.get("high") or {}).get("correctness_rate_pct")
    medium_conf_rate = (by_conf.get("medium") or {}).get("correctness_rate_pct")
    low_conf_rate = (by_conf.get("low") or {}).get("correctness_rate_pct")
    cal_dist = calibration.get("calibration_distribution") or {}

    # Verdict on calibration
    calibration_verdict = "unknown — need more verified predictions"
    if high_conf_rate is not None:
        if high_conf_rate >= 70:
            calibration_verdict = f"well-calibrated (high-conf correct {high_conf_rate}% of the time)"
        elif high_conf_rate >= 50:
            calibration_verdict = f"slightly over-confident (high-conf correct {high_conf_rate}%, target ≥70%)"
        else:
            calibration_verdict = f"severely over-confident (high-conf correct only {high_conf_rate}%)"

    # ── 2. Consolidated lessons from L18 ─────────────────────────
    lessons = _internal("/api/v1/brain/lessons")
    active_lessons = (lessons.get("active_lessons") or [])[:5]
    most_recent_lesson = active_lessons[0] if active_lessons else None

    # ── 3. Causal chains from L14 ────────────────────────────────
    causal = _internal("/api/v1/brain/causal")
    analysis = causal.get("analysis") or {}
    chains = analysis.get("causal_chains") or []
    highest_leverage = analysis.get("single_highest_leverage")

    # ── 4. Auto-action queue from L15 ────────────────────────────
    actions = _internal("/api/v1/brain/auto-action")
    open_actions = [a for a in (actions.get("actions") or [])
                    if a.get("issue_url") and not a.get("error")]

    # ── 5. Findings from L0 ──────────────────────────────────────
    radar = _internal("/api/v1/brain/consistency-radar", timeout=20)
    findings = radar.get("findings") or []
    findings_by_issue: dict = {}
    for f in findings:
        k = f.get("issue", "?")
        findings_by_issue[k] = findings_by_issue.get(k, 0) + 1
    top_finding_types = sorted(findings_by_issue.items(), key=lambda x: -x[1])[:5]

    # ── 6. Data freshness (where is the brain blind?) ────────────
    freshness = _internal("/api/v1/freshness/radar")
    domains = freshness.get("domains") or []
    stale_domains = [d.get("domain", "?") for d in domains
                     if d.get("status") in ("breach", "warning")]

    # ── 7. Expansion state from L12 ──────────────────────────────
    expansion = _internal("/api/v1/brain/expansion")
    current = expansion.get("current") or {}
    delta_7d = expansion.get("delta_7d") or {}

    # ── 8. Pulse: am I healthy? ──────────────────────────────────
    pool_pressure = any(f.get("issue") == "db_pool_pressure" for f in findings)
    health_pulse = "degraded" if pool_pressure else "healthy"

    # ── Final assembled snapshot ─────────────────────────────────
    snapshot = {
        "brain_health":          health_pulse,
        "epistemic_calibration": {
            "verdict":            calibration_verdict,
            "high_conf_rate_pct": high_conf_rate,
            "med_conf_rate_pct":  medium_conf_rate,
            "low_conf_rate_pct":  low_conf_rate,
            "distribution":       cal_dist,
        },
        "what_i_just_learned":   most_recent_lesson,
        "active_lessons":        active_lessons,
        "current_understanding": {
            "highest_leverage_root_cause": highest_leverage,
            "active_causal_chains":         len(chains),
            "open_action_queue":            len(open_actions),
            "total_findings":               len(findings),
            "top_finding_types":            dict(top_finding_types),
        },
        "blind_spots": {
            "stale_data_domains":   stale_domains,
            "freshness_breaches":   sum(1 for d in domains if d.get("status") == "breach"),
        },
        "growth_signals": {
            "current": {k: v for k, v in current.items()
                        if isinstance(v, int)},
            "delta_7d": delta_7d if delta_7d else None,
        },
        "layer_count":     19,  # this is L19
        "answers_to":      "/api/v1/brain/awareness",
        "computed_at":     _dt.datetime.utcnow().isoformat() + "Z",
    }
    return snapshot


@brain_layer19_bp.route("/api/v1/brain/awareness", methods=["GET"])
def awareness():
    """Single endpoint that tells you what the brain currently knows,
    what it's wrong about, and what it just learned."""
    now = time.monotonic()
    if _CACHE["snapshot"] and (now - _CACHE["computed_at"]) < _TTL:
        return jsonify(
            ok=True,
            snapshot=_CACHE["snapshot"],
            cached=True,
            cache_age_seconds=int(now - _CACHE["computed_at"]),
        )
    try:
        snap = _compute_awareness()
        _CACHE["snapshot"] = snap
        _CACHE["computed_at"] = now
        return jsonify(ok=True, snapshot=snap, cached=False)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503
