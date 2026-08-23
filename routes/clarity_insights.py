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
# Named for what it actually gates: a count of CLICKS, not of sessions. The
# old env name is still honoured so a set value on Railway keeps working.
_MIN_DEAD_CLICKS = int(os.environ.get("CLARITY_DEADCLICK_MIN_CLICKS")
                       or os.environ.get("CLARITY_DEADCLICK_MIN_SESSIONS")
                       or "4")
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
    """[{url, dead_clicks, dead_sessions, rage_clicks, traffic_sessions,
    dead_share_pct, dead_clicks_per_session}] from Clarity's metric list.

    Defensive: unknown shapes yield []. Never fabricates — a URL with no
    traffic row, or no session count to put over it, reports None rather than
    a number. `dead_share_pct` is sessions/sessions and is bounded by 100;
    `dead_clicks_per_session` is the unbounded rate and is where a value
    above 1 legitimately belongs."""
    by_metric = {}
    if isinstance(payload, list):
        for m in payload:
            if isinstance(m, dict) and m.get("metricName"):
                by_metric[m["metricName"]] = m.get("information") or []

    def _rows(name, value_key, strict=False):
        """Sum `value_key` per URL across one metric's rows.

        ★ `strict` exists because the lenient fallback chain below is a liar
        when you ask for a key the payload does not carry: asking for
        sessionsCount and silently receiving subTotal returns a CLICK count
        wearing a session name, which is exactly the bug this function grew.
        Session reads MUST pass strict=True so an absent field stays absent.
        """
        out = {}
        for r in by_metric.get(name, []):
            if not isinstance(r, dict):
                continue
            url = r.get("URL") or r.get("Url") or r.get("url")
            if not url:
                continue
            raw = r.get(value_key)
            if raw is None and not strict:
                raw = r.get("subTotal") or r.get("sessionsCount")
            if raw is None:
                continue
            try:
                out[url] = out.get(url, 0) + int(float(raw))
            except Exception:
                continue
        return out

    # subTotal is a COUNT OF CLICKS. totalSessionCount is a COUNT OF SESSIONS.
    # Dividing the first by the second is not a share of anything — it is a
    # per-session rate — and publishing it as `dead_share_pct` is what put
    # "1000.0" on a percentage field in production.
    dead_clicks = _rows("DeadClickCount", "subTotal")
    rage_clicks = _rows("RageClickCount", "subTotal")
    dead_sessions = _rows("DeadClickCount", "sessionsCount", strict=True)
    traffic = _rows("Traffic", "totalSessionCount")

    spots = []
    for url, clicks in dead_clicks.items():
        t = traffic.get(url)
        ds = dead_sessions.get(url)
        # A share is sessions-over-sessions and cannot exceed 100. It is
        # reported ONLY when Clarity actually gave us a session count for the
        # numerator; otherwise it stays null. Never fabricate a percentage.
        share = round(100.0 * ds / t, 1) if (ds is not None and t) else None
        # The rate is always derivable and CAN legitimately exceed 1.0 — one
        # frustrated visitor clicking a dead control eight times is 8.0.
        per_session = round(float(clicks) / t, 2) if t else None
        spots.append({"url": url,
                      "dead_clicks": clicks,
                      "dead_sessions": ds,
                      "rage_clicks": rage_clicks.get(url, 0),
                      "traffic_sessions": t,
                      "dead_share_pct": share,
                      "dead_clicks_per_session": per_session})
    spots.sort(key=lambda s: -s["dead_clicks"])
    return spots


def _worth_filing(s):
    if s["dead_clicks"] < _MIN_DEAD_CLICKS:
        return False
    # With a REAL sessions-based share, also require it to be meaningful.
    # Before this was fixed the share was clicks/sessions, so it cleared an
    # 8% floor for essentially every URL and the threshold gated nothing.
    if s["dead_share_pct"] is not None and s["dead_share_pct"] < _MIN_DEAD_SHARE_PCT:
        return False
    return True


def _file_findings(spots):
    """File via the canonical writer (episode semantics): an open same-URL
    finding absorbs the re-observation without moving seen_count; a resolved
    one reopens as a new episode. The old hand-rolled UPDATE's guards are
    gone on purpose — its resolved_at IS NULL check is superseded by the
    writer's reopen logic, and its created_at > 7d window made every sweep
    file a DUPLICATE row for any hotspot older than a week (no
    UNIQUE(issue,url) exists to stop it)."""
    filed = refreshed = 0
    try:
        # psycopg2 direct (water_aqueduct_ingest._conn pattern) — the pooled
        # helper lives in main.py, which route modules must never import.
        import psycopg2 as _pg
        from routes.brain_findings_writer import upsert_brain_finding
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return 0, 0
        conn = _pg.connect(url, connect_timeout=8)
        try:
            with conn.cursor() as cur:
                for s in spots:
                    if not _worth_filing(s):
                        continue
                    detail = json.dumps({
                        "detector": "clarity_dead_clicks",
                        "window_days": 3,
                        "dead_clicks": s["dead_clicks"],
                        "dead_sessions": s["dead_sessions"],
                        "rage_clicks": s["rage_clicks"],
                        "traffic_sessions": s["traffic_sessions"],
                        "dead_share_pct": s["dead_share_pct"],
                        "evidence": "Clarity Data Export project-live-insights",
                        "hint": ("element looks interactive but doesn't respond"
                                 " — check pointer-affordance without a handler,"
                                 " slow INP, or a gate that swallows the click"),
                    })
                    outcome = upsert_brain_finding(
                        cur, issue="ux_dead_clicks", url=s["url"],
                        count=s["dead_clicks"], detail=detail,
                        detector="clarity_insights")
                    if outcome == "inserted":
                        filed += 1
                    elif outcome == "updated":
                        refreshed += 1
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
        "thresholds": {"min_dead_clicks": _MIN_DEAD_CLICKS,
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
