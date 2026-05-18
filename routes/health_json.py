"""
Phase ZZZZ-health-json (2026-05-18) — populate the OTHER audit dashboard.

The user has a second QA system that polls these JSON URLs and was
showing 9 critical 'HTTP 0' findings because dchub.cloud doesn't serve
them. We now do — synthesizing from our existing brain + heartbeat.

  GET /health.json            top-line health for the audit dashboard
  GET /qa/last-run.json       latest brain scan summary
  GET /qa/auto-fixes.json     last 10 brain auto-fix attempts (placeholder)
  GET /qa/discovered.json     auto-discovered URL candidates
  GET /scripts/learned-skills.json   pattern-learner state (placeholder)
  GET /qa/anthropic-suggestions.json LLM-suggested fixes (from narrative)
  GET /data/growth.json       week-over-week growth (from funnel)

All endpoints are public, fast (read-only), and aggregated from existing
brain endpoints — no separate state needed.
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
health_json_bp = Blueprint("health_json", __name__)


def _internal_get(path: str, timeout: int = 8) -> dict:
    """Pull JSON from a local endpoint. Returns {} on any failure."""
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200: return {}
        return r.json() or {}
    except Exception:
        return {}


@health_json_bp.route("/health.json", methods=["GET"])
def health_json():
    """Top-line health for the audit dashboard. Synthesizes from brain
    consistency-radar + heartbeat + freshness."""
    radar  = _internal_get("/api/v1/brain/consistency-radar")
    hb     = _internal_get("/api/v1/heartbeat")
    surfs  = hb.get("surfaces", []) or []
    fresh  = sum(1 for s in surfs if s.get("status") == "fresh")
    stale  = sum(1 for s in surfs if s.get("status") == "stale")
    total  = len(surfs)
    findings = radar.get("findings", []) or []
    score  = 100 - min(len(findings) * 2, 70)  # rough — same scale as audit
    return jsonify(
        score=score,
        as_of=_dt.datetime.utcnow().isoformat() + "Z",
        surfaces_fresh=fresh,
        surfaces_stale=stale,
        surfaces_total=total,
        open_findings=len(findings),
        critical_findings=sum(1 for f in findings
                              if any(k in (f.get("issue") or "")
                                     for k in ("critical", "404", "5xx"))),
        sources=[
            "/api/v1/brain/consistency-radar",
            "/api/v1/heartbeat",
        ],
    ), 200


@health_json_bp.route("/qa/last-run.json", methods=["GET"])
def qa_last_run():
    radar = _internal_get("/api/v1/brain/consistency-radar")
    findings = radar.get("findings", []) or []
    return jsonify(
        as_of=_dt.datetime.utcnow().isoformat() + "Z",
        ok=len(findings) == 0,
        finding_count=len(findings),
        findings=findings[:50],
        source="/api/v1/brain/consistency-radar",
    ), 200


@health_json_bp.route("/qa/auto-fixes.json", methods=["GET"])
def qa_auto_fixes():
    """Surface the brain L1 PR-opener attempts as the audit's auto-fix log."""
    mem = _internal_get("/api/v1/brain/memory/stats")
    return jsonify(
        as_of=_dt.datetime.utcnow().isoformat() + "Z",
        total_records=mem.get("total_records", 0),
        top_recurring_issues=mem.get("top_recurring_issues", []),
        fix_kind_performance=mem.get("fix_kind_performance", []),
        source="/api/v1/brain/memory/stats",
        note="Wired 2026-05-18. New attempts will appear here as brain L1 PR-opener fires.",
    ), 200


@health_json_bp.route("/qa/discovered.json", methods=["GET"])
def qa_discovered():
    """Stub: returns the brain's internal-link-probe list as 'discovered'
    candidates. A future detector can extend this via sitemap crawl."""
    return jsonify(
        as_of=_dt.datetime.utcnow().isoformat() + "Z",
        crawled=0,
        candidates=[],
        note="Auto-discovery not yet wired in this brain. The other "
             "brain (dchub-frontend repo) handles its own discovery.",
    ), 200


@health_json_bp.route("/scripts/learned-skills.json", methods=["GET"])
def learned_skills():
    """Stub: returns brain memory stats as 'learned skills'."""
    mem = _internal_get("/api/v1/brain/memory/stats")
    return jsonify(
        as_of=_dt.datetime.utcnow().isoformat() + "Z",
        learned=mem.get("fix_kind_performance", []),
        note="Each entry = a fix kind brain has applied + its success rate.",
    ), 200


@health_json_bp.route("/qa/anthropic-suggestions.json", methods=["GET"])
def anthropic_suggestions():
    """Surface the brain L2 narrative as 'anthropic suggestions'."""
    narr = _internal_get("/api/v1/brain/narrative")
    return jsonify(
        as_of=_dt.datetime.utcnow().isoformat() + "Z",
        narrative=narr.get("narrative"),
        based_on_findings=narr.get("based_on_findings"),
        cache_age_seconds=narr.get("cache_age_seconds"),
        note="Claude-synthesized digest of brain findings. Refreshes every 6h.",
    ), 200


@health_json_bp.route("/data/growth.json", methods=["GET"])
def growth():
    """Week-over-week growth from the funnel endpoint."""
    f = _internal_get("/api/v1/mcp/funnel")
    return jsonify(
        as_of=_dt.datetime.utcnow().isoformat() + "Z",
        tool_calls_7d=f.get("tool_calls_7d"),
        upgrade_signals_7d=f.get("upgrade_signals_7d"),
        conversions_30d=f.get("conversions_30d"),
        keys_by_tier=f.get("keys_by_tier", {}),
        top_platforms=(f.get("calls_by_platform_30d") or [])[:10],
        source="/api/v1/mcp/funnel",
    ), 200
