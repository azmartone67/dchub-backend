"""
CSP Violation Report endpoint for DC Hub (Flask blueprint).

WIRE-UP in main.py — two-line patch:

    # near the other blueprint imports (top of file):
    from csp_report import csp_report_bp

    # near the other app.register_blueprint(...) calls:
    app.register_blueprint(csp_report_bp)

This endpoint is referenced by the `report-uri /api/csp-report` directive in
the Content-Security-Policy header set in dchub-frontend/_headers. Without
this endpoint, every browser-side CSP violation 404s into the void — which
is exactly how the 2026-04-22 /map + /land-power-map outage went undetected
until a user reported it.

Browsers POST either:
  - legacy `report-uri` format: `{"csp-report": {...}}`
  - newer Reporting API `report-to` format: array of report objects
We accept both.

Exposes two endpoints:
  - POST /api/csp-report        — receive violation reports (returns 204)
  - GET  /api/csp-report/stats  — last-hour rolling summary (for monitoring)
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger("csp-report")

csp_report_bp = Blueprint("csp_report", __name__)

# ---------------------------------------------------------------------------
# In-process state (simple, no external deps)
# ---------------------------------------------------------------------------

# Dedupe window: identical (directive, blocked, document) tuples within this
# window are logged only once so one broken page can't flood logs.
_DEDUPE_WINDOW_SECONDS = 60
_dedupe_seen: dict[tuple[str, str, str], float] = {}

# Rolling 1h counter used by /api/csp-report/stats.
_RECENT_WINDOW_SECONDS = 3600
_recent_events: deque[tuple[float, str, str, str]] = deque(maxlen=5000)


def _now() -> float:
    return time.time()


def _prune_dedupe() -> None:
    cutoff = _now() - _DEDUPE_WINDOW_SECONDS
    stale = [k for k, ts in _dedupe_seen.items() if ts < cutoff]
    for k in stale:
        _dedupe_seen.pop(k, None)


def _normalize_reports(payload):
    """Normalize legacy (report-uri) and Reporting-API (report-to) payloads
    into a common shape. Returns a list of dicts."""
    out = []

    # Legacy: {"csp-report": {...}}
    if isinstance(payload, dict) and "csp-report" in payload:
        r = payload.get("csp-report") or {}
        out.append({
            "document_uri":        r.get("document-uri", ""),
            "violated_directive":  r.get("violated-directive", ""),
            "effective_directive": r.get("effective-directive", ""),
            "blocked_uri":         r.get("blocked-uri", ""),
            "source_file":         r.get("source-file", ""),
            "line_number":         r.get("line-number"),
            "column_number":       r.get("column-number"),
            "status_code":         r.get("status-code"),
            "script_sample":       r.get("script-sample", ""),
            "format":              "report-uri",
        })
        return out

    # Reporting API: list of report objects
    if isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            body = entry.get("body") or {}
            out.append({
                "document_uri":        body.get("documentURL", ""),
                "violated_directive":  body.get("effectiveDirective", ""),
                "effective_directive": body.get("effectiveDirective", ""),
                "blocked_uri":         body.get("blockedURL", ""),
                "source_file":         body.get("sourceFile", ""),
                "line_number":         body.get("lineNumber"),
                "column_number":       body.get("columnNumber"),
                "status_code":         body.get("statusCode"),
                "script_sample":       body.get("sample", ""),
                "format":              "report-to",
            })
        return out

    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@csp_report_bp.route("/api/csp-report", methods=["POST"])
def csp_report():
    # Browsers send various content-types (application/csp-report,
    # application/reports+json, application/json). Parse defensively.
    raw = request.get_data(cache=False) or b""
    if not raw:
        return Response(status=204)

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("csp-report: malformed body: %s", e)
        return Response(status=204)

    reports = _normalize_reports(payload)
    if not reports:
        return Response(status=204)

    user_agent = request.headers.get("User-Agent", "")
    client_ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    )

    _prune_dedupe()
    now = _now()

    for r in reports:
        key = (
            r.get("effective_directive") or r.get("violated_directive") or "",
            r.get("blocked_uri", ""),
            r.get("document_uri", ""),
        )
        if key in _dedupe_seen and (now - _dedupe_seen[key]) < _DEDUPE_WINDOW_SECONDS:
            continue
        _dedupe_seen[key] = now
        _recent_events.append((now, key[0], key[1], key[2]))

        # Structured log line — WARNING level, logger name "csp-report".
        # Wire an alert on repeated CSP_VIOLATION lines to page on policy regressions.
        logger.warning(
            "CSP_VIOLATION directive=%s blocked=%s document=%s source=%s:%s ua=%s ip=%s",
            key[0] or "-",
            key[1] or "-",
            key[2] or "-",
            r.get("source_file") or "-",
            r.get("line_number") or "-",
            (user_agent or "")[:200],
            client_ip,
        )

    return Response(status=204)


# ---------------------------------------------------------------------------
# Helpers consumed by brain_consistency_radar (Phase QQQ, 2026-05-17)
# ---------------------------------------------------------------------------

def recent_blocked_uris(window_seconds: int = 86400, top_n: int = 10) -> list[dict]:
    """Return the top-N most-blocked URIs in the last `window_seconds`.

    Brain `check_csp_violation_reports()` calls this each scan; if a
    blocked URI shows up repeatedly we want a finding because that's a
    CSP allowlist gap actively breaking real users.

    NOTE: in-process state only. If Railway recycles the worker the
    counts reset. Good enough — the brain runs every 6h and persistent
    storage would require a migration; keeping this simple keeps it
    deployable today.
    """
    cutoff = _now() - window_seconds
    by_blocked: dict[tuple[str, str], int] = defaultdict(int)
    for ts, directive, blocked, document in _recent_events:
        if ts < cutoff:
            continue
        if not blocked or blocked == "-":
            continue
        # Strip protocol so we group cdn.x.com http vs https together
        clean = blocked.split("://", 1)[-1].split("?", 1)[0].split("/", 1)[0]
        by_blocked[(clean, directive or "-")] += 1
    ranked = sorted(by_blocked.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"blocked_uri": k[0], "directive": k[1], "count": v}
        for k, v in ranked[:top_n]
    ]


@csp_report_bp.route("/api/csp-report/stats", methods=["GET"])
def csp_report_stats():
    """Summary of recent CSP violations for at-a-glance health."""
    cutoff = _now() - _RECENT_WINDOW_SECONDS
    recent = [e for e in _recent_events if e[0] >= cutoff]

    by_directive: dict[str, int] = defaultdict(int)
    by_blocked: dict[str, int]   = defaultdict(int)
    by_document: dict[str, int]  = defaultdict(int)
    for _, directive, blocked, document in recent:
        by_directive[directive or "-"] += 1
        by_blocked[blocked or "-"]     += 1
        by_document[document or "-"]   += 1

    def _top(d, n=10):
        return [
            {"key": k, "count": v}
            for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n]
        ]

    return jsonify({
        "window_seconds":    _RECENT_WINDOW_SECONDS,
        "total_events":      len(recent),
        "by_directive":      _top(by_directive),
        "by_blocked_uri":    _top(by_blocked),
        "by_document_uri":   _top(by_document),
    })
