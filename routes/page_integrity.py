"""
page_integrity.py — Phase r33 (2026-05-24).

The user's recurring question: "is every page on the site linked to
brain — dynamic, agentic, learning, evolving — avoiding stale content
and errors?". Answer: build a single endpoint that walks the sentinel
manifest, the brain heal-findings, and the surface_brain registry,
and reports a one-row-per-URL integrity score.

Per-URL signals:
  - in_sentinel_manifest:  the URL is being page-health probed
  - has_max_age:            sentinel will alert if the page goes stale
  - is_brain_surface:        registered with surface_brain (events/clicks)
  - has_recent_findings:     brain's healer has flagged it in last 7d
  - currently_healthy:       last sentinel scan passed
  - integrity_score (0-100): weighted composite
  - verdict (alive/stale/orphan/broken): single-word state

The whole site's integrity is the mean of per-URL integrity_score.

Endpoint:
  GET /api/v1/sentinel/page-integrity        full per-URL report
  GET /api/v1/sentinel/page-integrity?weak=1 only the weak ones (score < 60)
"""
from __future__ import annotations

import datetime
from flask import Blueprint, jsonify, request


page_integrity_bp = Blueprint("page_integrity", __name__)


def _safe_load_manifest():
    try:
        from routes.site_sentinel import _MANIFEST
        return list(_MANIFEST or [])
    except Exception:
        return []


def _safe_load_sentinel_results():
    """Return {path: latest_result_dict} from site_sentinel."""
    try:
        from routes.site_sentinel import latest_results
        return {r.get("path"): r for r in (latest_results() or []) if r.get("path")}
    except Exception:
        return {}


def _safe_load_brain_surfaces():
    """Return set of paths that are registered with surface_brain."""
    try:
        from routes.surface_brain import list_surfaces
        out = set()
        for s in (list_surfaces() or []):
            for r in (s.get("routes") or []):
                # Strip templated params for matching
                out.add(r)
        return out
    except Exception:
        return set()


def _safe_load_heal_findings():
    """Return {url_path: [findings]} from brain healer."""
    out: dict = {}
    try:
        from flask import current_app
        with current_app.test_client() as tc:
            r = tc.get("/api/v1/heal/findings")
            if r.status_code == 200:
                payload = r.get_json() or {}
                for f in (payload.get("findings") or []):
                    u = f.get("url") or ""
                    # Extract path from full URL
                    if u.startswith("http"):
                        from urllib.parse import urlparse
                        u = urlparse(u).path or "/"
                    out.setdefault(u, []).append(f)
    except Exception:
        pass
    return out


def _score(entry: dict, result: dict, is_surface: bool,
           findings: list, has_max_age: bool) -> tuple[int, str]:
    """Compute 0-100 integrity score + verdict for one page."""
    score = 0
    # 30 pts: page is probed by sentinel
    score += 30  # always true if it's in the manifest
    # 20 pts: it's currently healthy
    if result and result.get("healthy"):
        score += 20
    # 15 pts: brain knows about it (surface registered)
    if is_surface:
        score += 15
    # 15 pts: has staleness gate set
    if has_max_age:
        score += 15
    # 20 pts: no active heal findings against it
    if not findings:
        score += 20

    # Verdict
    if not result or not result.get("healthy"):
        verdict = "broken"
    elif findings:
        verdict = "stale"
    elif not is_surface:
        verdict = "orphan"   # works but brain doesn't track engagement
    else:
        verdict = "alive"

    return score, verdict


@page_integrity_bp.route("/api/v1/sentinel/page-integrity", methods=["GET"])
def page_integrity():
    """Per-URL brain-integration + freshness + health report.

    For each sentinel-manifest entry, returns a 0-100 integrity score
    answering "is this page dynamic, agentic, learning, evolving — or
    is it orphaned/stale/broken?". The site-wide mean is the headline
    number.
    """
    manifest = _safe_load_manifest()
    results = _safe_load_sentinel_results()
    surfaces = _safe_load_brain_surfaces()
    findings = _safe_load_heal_findings()

    weak_only = request.args.get("weak") == "1"

    pages = []
    for entry in manifest:
        path = entry.get("path") or ""
        if not path:
            continue
        result = results.get(path) or {}
        is_surface = path in surfaces
        has_max_age = entry.get("max_age_days") is not None
        page_findings = findings.get(path) or []

        score, verdict = _score(entry, result, is_surface,
                                 page_findings, has_max_age)
        page_row = {
            "path": path,
            "label": entry.get("label") or path,
            "category": entry.get("category") or "normal",
            "score": score,
            "verdict": verdict,
            "currently_healthy": bool(result.get("healthy")),
            "is_brain_surface": is_surface,
            "has_max_age_gate": has_max_age,
            "findings_count": len(page_findings),
            "last_status": result.get("status_code"),
            "last_bytes": result.get("bytes"),
            "last_reason": result.get("reason"),
        }
        if not weak_only or score < 60:
            pages.append(page_row)

    pages.sort(key=lambda p: (p["score"], p["path"]))

    # Site-wide rollup
    if pages:
        mean_score = round(sum(p["score"] for p in pages) / len(pages), 1)
    else:
        mean_score = 0.0
    from collections import Counter
    verdict_counts = Counter(p["verdict"] for p in pages)

    site_verdict = (
        "alive"  if mean_score >= 80 else
        "weak"   if mean_score >= 60 else
        "patchy" if mean_score >= 40 else
        "broken"
    )

    return jsonify(
        site_score=mean_score,
        site_verdict=site_verdict,
        pages_total=len(pages),
        verdict_breakdown=dict(verdict_counts),
        pages=pages,
        legend={
            "alive":  "healthy + brain-tracked + no findings",
            "orphan": "healthy but no surface_brain registration",
            "stale":  "healthy but has active heal findings",
            "broken": "currently failing sentinel scan",
        },
        scoring={
            "in_manifest":      30,
            "currently_healthy": 20,
            "brain_surface":    15,
            "max_age_gate":     15,
            "no_findings":      20,
        },
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        purpose=(
            "Per-URL integrity report. For every page in the sentinel "
            "manifest, score whether it is being probed, tracked by brain, "
            "and free of healer findings. Site-wide mean answers the "
            "'is every page alive and evolving?' question with one number."
        ),
    ), 200
