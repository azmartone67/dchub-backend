"""Clarity UX-signal detector — dead/rage clicks → brain_findings.

r-clarity-deadclicks (2026-07-10): Clarity showed 11.6% of sessions with dead
clicks for weeks and nothing in the flywheel ever saw it — UX breakage was
invisible to the brain. This module pulls Microsoft Clarity's Data Export API
(project-live-insights, last 1-3 days) and files per-URL dead-click / rage-click
hotspots as brain_findings, so the existing triage → autopilot → janitor loop
treats UX breakage like any other detector signal.

HONEST NO-OP without CLARITY_API_TOKEN (generate in Clarity → Settings →
Data Export; it is a JWT, ~500 chars). The export API is rate-limited to
10 requests/project/day — call this tick at most a few times daily.

Surfaces:
  GET  /api/v1/admin/clarity/dead-clicks        — read-only hotspot report
  POST /api/v1/admin/clarity/dead-clicks-tick   — report + file brain_findings

Both admin-key gated (fail-closed), same pattern as mcp_registry_watch.
"""

import json
import logging
import os

import requests

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

clarity_insights_bp = Blueprint("clarity_insights", __name__)

_EXPORT_URL = ("https://www.clarity.ms/export-data/api/v1/project-live-insights"
               "?numOfDays=3&dimension1=URL")

# File a finding only when a URL is a real hotspot, not one stray misclick.
_MIN_DEAD_SESSIONS = int(os.environ.get("CLARITY_DEADCLICK_MIN_SESSIONS", "4"))
_MIN_DEAD_SHARE_PCT = float(os.environ.get("CLARITY_DEADCLICK_MIN_SHARE", "8.0"))


def _admin_ok():
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    if not admin_key:
        return jsonify({"error": "admin_endpoint_unconfigured"}), 503
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if provided != admin_key:
        return jsonify({"error": "unauthorized"}), 401
    return None


def _token():
    return (os.environ.get("CLARITY_API_TOKEN") or "").strip()


def _fetch_insights(token):
    resp = requests.get(_EXPORT_URL, timeout=30, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": "dchub-clarity-detector/1.0",
    })
    resp.raise_for_status()
    return resp.json()


def _hotspots(payload):
    """[{url, dead_sessions, rage_sessions, traffic_sessions, dead_share_pct}]
    from Clarity's metric list. Defensive: unknown shapes yield []. Never
    fabricates — URLs missing a traffic row report dead_share_pct=None."""
    by_metric = {}
    if isinstance(payload, list):
        for m in payload:
            if isinstance(m, dict) and m.get("metricName"):
                by_metric[m["metricName"]] = m.get("information") or []

    def _rows(name, value_key):
        out = {}
        for r in by_metric.get(name, []):
            if not isinstance(r, dict):
                continue
            url = r.get("URL") or r.get("Url") or r.get("url")
            if not url:
                continue
            try:
                out[url] = out.get(url, 0) + int(
                    float(r.get(value_key) or r.get("subTotal")
                          or r.get("sessionsCount") or 0))
            except Exception:
                continue
        return out

    dead = _rows("DeadClickCount", "subTotal")
    rage = _rows("RageClickCount", "subTotal")
    traffic = _rows("Traffic", "totalSessionCount")

    spots = []
    for url, d in dead.items():
        t = traffic.get(url)
        share = round(100.0 * d / t, 1) if t else None
        spots.append({"url": url, "dead_sessions": d,
                      "rage_sessions": rage.get(url, 0),
                      "traffic_sessions": t, "dead_share_pct": share})
    spots.sort(key=lambda s: -s["dead_sessions"])
    return spots


def _worth_filing(s):
    if s["dead_sessions"] < _MIN_DEAD_SESSIONS:
        return False
    # With traffic data, also require a meaningful share; without it, the
    # absolute floor alone decides (never fabricate a share).
    if s["dead_share_pct"] is not None and s["dead_share_pct"] < _MIN_DEAD_SHARE_PCT:
        return False
    return True


def _file_findings(spots):
    """Upsert-style: refresh an open same-URL finding seen in the last 7d
    instead of re-emitting (the stateless-detector re-emission trap)."""
    filed = refreshed = 0
    try:
        from db_utils import get_pg_connection
        conn = get_pg_connection()
        try:
            with conn.cursor() as cur:
                for s in spots:
                    if not _worth_filing(s):
                        continue
                    detail = json.dumps({
                        "detector": "clarity_dead_clicks",
                        "window_days": 3,
                        "dead_sessions": s["dead_sessions"],
                        "rage_sessions": s["rage_sessions"],
                        "traffic_sessions": s["traffic_sessions"],
                        "dead_share_pct": s["dead_share_pct"],
                        "evidence": "Clarity Data Export project-live-insights",
                        "hint": ("element looks interactive but doesn't respond"
                                 " — check pointer-affordance without a handler,"
                                 " slow INP, or a gate that swallows the click"),
                    })
                    cur.execute("SAVEPOINT cl_sp")
                    try:
                        cur.execute(
                            "UPDATE brain_findings SET count = %s, detail = %s,"
                            " last_seen = NOW(), seen_count = COALESCE(seen_count,1) + 1"
                            " WHERE issue = 'ux_dead_clicks' AND url = %s"
                            " AND resolved_at IS NULL"
                            " AND created_at > NOW() - INTERVAL '7 days'",
                            (s["dead_sessions"], detail, s["url"]))
                        if cur.rowcount:
                            refreshed += 1
                        else:
                            cur.execute(
                                "INSERT INTO brain_findings"
                                " (issue, url, count, detail, detector)"
                                " VALUES ('ux_dead_clicks', %s, %s, %s,"
                                " 'clarity_dead_clicks')"
                                # dedup = the UPDATE-first path above; the
                                # only unique index is the pkey, so this
                                # arbiter-less form never masks real rows
                                " ON CONFLICT DO NOTHING",
                                (s["url"], s["dead_sessions"], detail))
                            filed += 1
                        cur.execute("RELEASE SAVEPOINT cl_sp")
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT cl_sp")
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("[clarity] findings write failed: %s", str(e)[:120])
    return filed, refreshed


def _report(file_them):
    gate = _admin_ok()
    if gate:
        return gate
    token = _token()
    if not token:
        return jsonify({
            "ok": True, "available": False,
            "skipped": ("CLARITY_API_TOKEN not set — honest no-op. Generate in"
                        " Clarity → Settings → Data Export → API tokens, then"
                        " set it on the dchub-backend Railway service."),
        })
    try:
        payload = _fetch_insights(token)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502
    spots = _hotspots(payload)
    out = {
        "ok": True, "available": True, "window_days": 3,
        "hotspots": spots[:15],
        "thresholds": {"min_dead_sessions": _MIN_DEAD_SESSIONS,
                       "min_dead_share_pct": _MIN_DEAD_SHARE_PCT},
    }
    if file_them:
        filed, refreshed = _file_findings(spots)
        out["findings_filed"] = filed
        out["findings_refreshed"] = refreshed
    return jsonify(out)


@clarity_insights_bp.route("/api/v1/admin/clarity/dead-clicks", methods=["GET"])
def clarity_dead_clicks():
    return _report(file_them=False)


@clarity_insights_bp.route("/api/v1/admin/clarity/dead-clicks-tick",
                           methods=["POST"])
def clarity_dead_clicks_tick():
    return _report(file_them=True)
