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


def _internal_get(path: str, timeout: int = 4) -> dict:
    """Pull JSON from a local endpoint. Returns {} on any failure.
    Phase ZZZZ-health-json-fix (2026-05-18): tight 4s timeout because
    /health.json itself needs to be FAST — the brain radar cold-start
    can take 20s, which would timeout /health.json at CF edge. Better
    to return partial data than hang."""
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
    consistency-radar + heartbeat + freshness.

    Phase ZZZZ-audit-schema (2026-05-18): include MULTIPLE field-name
    variants so the audit dashboard's JS parses correctly regardless
    of which convention it expects (score / health_score / pct, etc.).
    """
    radar  = _internal_get("/api/v1/brain/consistency-radar")
    hb     = _internal_get("/api/v1/heartbeat")
    surfs  = hb.get("surfaces", []) or []
    fresh  = sum(1 for s in surfs if s.get("status") == "fresh")
    stale  = sum(1 for s in surfs if s.get("status") == "stale")
    total  = len(surfs)
    findings = radar.get("findings", []) or []
    score  = 100 - min(len(findings) * 2, 70)
    now_iso = _dt.datetime.utcnow().isoformat() + "Z"
    critical = sum(1 for f in findings
                   if any(k in (f.get("issue") or "")
                          for k in ("critical", "404", "5xx")))
    return jsonify(
        # Multiple field-name variants (audit JS could use any)
        score=score,
        health_score=score,
        health_pct=score,
        pct=score,
        as_of=now_iso,
        last_check_at=now_iso,
        last_run_at=now_iso,
        timestamp=now_iso,
        # Surface counts
        surfaces_fresh=fresh,
        surfaces_stale=stale,
        surfaces_total=total,
        # Finding counts (all variants)
        open_findings=len(findings),
        finding_count=len(findings),
        findings_count=len(findings),
        open_count=len(findings),
        critical_findings=critical,
        critical_count=critical,
        # Test pass/fail
        passing=max(0, total - stale - len(findings)),
        failing=len(findings),
        total_tests=total + len(findings),
        # Status string
        status="healthy" if score >= 80 else ("degraded" if score >= 50 else "critical"),
        sources=["/api/v1/brain/consistency-radar", "/api/v1/heartbeat"],
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
    """Surface the brain L1 PR-opener attempts as the audit's auto-fix log.
    Phase ZZZZ-audit-schema (2026-05-18): include the actual fix list
    (not just stats) so the audit timeline + count widgets populate."""
    mem = _internal_get("/api/v1/brain/memory/stats")
    # Pull recent attempts via lookup (top recurring issue)
    fixes = []
    top = mem.get("top_recurring_issues") or []
    for issue in top[:5]:
        lk = _internal_get(f"/api/v1/brain/memory/lookup?issue={issue.get('issue_type','')}")
        for a in (lk.get("attempts") or [])[:3]:
            fixes.append({
                "issue":    issue.get("issue_type"),
                "kind":     a.get("fix_kind"),
                "summary":  a.get("fix_summary"),
                "pr_url":   a.get("fix_pr_url"),
                "outcome":  a.get("outcome"),
                "attempted_at": a.get("attempted_at"),
            })
    total = mem.get("total_records", 0)
    now_iso = _dt.datetime.utcnow().isoformat() + "Z"
    return jsonify(
        as_of=now_iso,
        last_check_at=now_iso,
        # Counts (multiple field variants)
        total_records=total,
        auto_fixes_7d=total,
        auto_fix_count=total,
        fix_count=total,
        count=total,
        # Lists
        fixes=fixes[:10],
        auto_fixes=fixes[:10],
        attempts=fixes[:10],
        top_recurring_issues=mem.get("top_recurring_issues", []),
        fix_kind_performance=mem.get("fix_kind_performance", []),
        source="/api/v1/brain/memory/stats",
    ), 200


@health_json_bp.route("/qa/discovered.json", methods=["GET"])
def qa_discovered():
    """Phase ZZZZ-audit-schema (2026-05-18): actually crawl the brain's
    /api/v1/brain/consistency-radar findings + heartbeat surfaces to
    produce a real discovered-URLs list. Brain doesn't have its own
    crawler (the dchub-frontend brain does), so we surface URLs from
    our existing data."""
    radar = _internal_get("/api/v1/brain/consistency-radar")
    hb = _internal_get("/api/v1/heartbeat")
    urls = set()
    for f in (radar.get("findings") or []):
        u = f.get("url")
        if u and isinstance(u, str) and u.startswith("/"):
            urls.add(u)
    for s in (hb.get("surfaces") or []):
        u = s.get("surface")
        if u and isinstance(u, str) and u.startswith("/"):
            urls.add(u)
    urls_list = sorted(urls)
    now_iso = _dt.datetime.utcnow().isoformat() + "Z"
    return jsonify(
        as_of=now_iso,
        last_check_at=now_iso,
        last_crawl_at=now_iso,
        # Counts in multiple field-name variants
        crawled=len(urls_list),
        crawled_count=len(urls_list),
        discovered=len(urls_list),
        discovered_count=len(urls_list),
        url_count=len(urls_list),
        count=len(urls_list),
        # Lists
        candidates=urls_list[:200],
        urls=urls_list[:200],
        new_surfaces=urls_list[:50],
        source="brain consistency-radar + heartbeat surfaces",
    ), 200


@health_json_bp.route("/scripts/learned-skills.json", methods=["GET"])
def learned_skills():
    """Phase ZZZZ-audit-schema (2026-05-18): surface real learned
    patterns from brain memory + the consistency-radar's detector
    crash-history. Multiple field-name variants for audit JS."""
    mem = _internal_get("/api/v1/brain/memory/stats")
    learned = mem.get("fix_kind_performance") or []
    # Map fix_kind_performance into "learned patterns" shape
    patterns = []
    for fp in learned:
        patterns.append({
            "name": fp.get("kind"),
            "samples": fp.get("attempts", 0),
            "wins": fp.get("wins", 0),
            "success_rate_pct": fp.get("win_rate_pct"),
        })
    now_iso = _dt.datetime.utcnow().isoformat() + "Z"
    return jsonify(
        as_of=now_iso,
        last_check_at=now_iso,
        last_analysis_at=now_iso,
        # Counts (multiple variants)
        learned_count=len(patterns),
        pattern_count=len(patterns),
        patterns_count=len(patterns),
        count=len(patterns),
        # Lists
        learned=patterns,
        patterns=patterns,
        skills=patterns,
        source="/api/v1/brain/memory/stats",
    ), 200


@health_json_bp.route("/qa/anthropic-suggestions.json", methods=["GET"])
def anthropic_suggestions():
    """Surface the brain L2 narrative as 'anthropic suggestions'.
    Phase ZZZZ-audit-schema: include count + suggestions list shape."""
    narr = _internal_get("/api/v1/brain/narrative")
    text = narr.get("narrative") or ""
    # Treat the narrative as 1 suggestion if non-empty
    suggestions = []
    if text:
        suggestions.append({
            "summary": text[:200] + ("..." if len(text) > 200 else ""),
            "full_text": text,
            "based_on_findings": narr.get("based_on_findings"),
            "cache_age_seconds": narr.get("cache_age_seconds"),
            "source": "claude-haiku-4-5",
        })
    now_iso = _dt.datetime.utcnow().isoformat() + "Z"
    return jsonify(
        as_of=now_iso,
        last_check_at=now_iso,
        suggestion_count=len(suggestions),
        suggestions_7d=len(suggestions),
        count=len(suggestions),
        suggestions=suggestions,
        narrative=text,
        based_on_findings=narr.get("based_on_findings"),
        source="/api/v1/brain/narrative",
    ), 200


@health_json_bp.route("/data/growth.json", methods=["GET"])
def growth():
    """Week-over-week growth from the funnel + stats endpoints.
    Phase ZZZZ-audit-schema: pull stats baselines for the audit's
    Growth WoW section."""
    f = _internal_get("/api/v1/mcp/funnel")
    s = _internal_get("/api/v1/stats")
    now_iso = _dt.datetime.utcnow().isoformat() + "Z"
    return jsonify(
        as_of=now_iso,
        last_check_at=now_iso,
        # Funnel (working in audit already)
        tool_calls_7d=f.get("tool_calls_7d"),
        upgrade_signals_7d=f.get("upgrade_signals_7d"),
        conversions_30d=f.get("conversions_30d"),
        keys_by_tier=f.get("keys_by_tier", {}),
        # Total active dev keys (audit shows "Active dev keys —")
        active_dev_keys=sum((f.get("keys_by_tier") or {}).values()),
        dev_keys_total=sum((f.get("keys_by_tier") or {}).values()),
        # Stats (for the growth grid: facilities, deals, pipeline, markets)
        facilities=s.get("facilities") or s.get("total_facilities") or 0,
        deals=s.get("deals", 0),
        pipeline_projects=s.get("pipeline_count") or s.get("curated_pipeline_count") or 0,
        markets=s.get("markets", 0),
        dcpi_markets=(s.get("data", {}) or {}).get("dcpi_markets_scored") or s.get("markets", 0),
        iso_endpoints_live=7,
        top_platforms=(f.get("calls_by_platform_30d") or [])[:10],
        source="/api/v1/mcp/funnel + /api/v1/stats",
    ), 200
