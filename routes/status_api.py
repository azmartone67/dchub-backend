"""
status_api.py — Phase GG (2026-05-15) Bundle 7 item 2: public status endpoint.

Single bundled GET for the public /status page. Pulls from:
  - freshness radar (data domain SLA + brain heartbeat)
  - brain self-assessment (letter grade)
  - recent broadcast history (last 7d)
  - sitemap health (URL count)
  - stats endpoint (totals)

All cached at edge for 60s. Designed to be Verge-style: dark page,
one big letter grade, one big "is brain healthy" indicator, the 12
radar domains with traffic-light status, and a 7-day broadcast trail.
"""
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

status_api_bp = Blueprint("status_api", __name__)


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=6)
    c.autocommit = True
    return c


def _safe_jget(path):
    """In-process Flask test_client fetch, returns dict or None."""
    try:
        from flask import current_app
        with current_app.test_client() as client:
            r = client.get(path)
            if r.status_code == 200:
                return r.get_json() or {}
    except Exception:
        pass
    return None


@status_api_bp.route("/api/v1/status", methods=["GET"])
def public_status():
    """Single-call public status bundle. Used by the /status page."""
    out = {
        "ok": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }

    # Brain self-assessment (cached 5min in brain_meta — fast path)
    sa = _safe_jget("/api/v1/brain/self-assessment") or {}
    out["brain"] = {
        "grade": sa.get("grade"),
        "weighted_score": sa.get("weighted_score"),
        "rationale": sa.get("rationale"),
        "component_scores": sa.get("component_scores"),
        "metrics": sa.get("metrics"),
    }

    # Freshness radar
    rad = _safe_jget("/api/v1/freshness/radar") or {}
    domains = rad.get("domains", [])
    out["freshness"] = {
        "summary": rad.get("summary"),
        "domains": [{
            "domain": d.get("domain"),
            "status": d.get("status"),
            "age_hours": d.get("age_hours"),
            "sla_hours": d.get("sla_hours"),
            "row_count": d.get("row_count"),
            "detail": (d.get("detail") or "")[:160],
        } for d in domains],
    }

    # Stats
    st = _safe_jget("/api/v1/stats") or {}
    data = st.get("data") or st
    out["stats"] = {
        "facilities": data.get("main_facilities") or data.get("total_facilities"),
        "pipeline_projects": data.get("curated_pipeline_count") or data.get("pipeline_count"),
        "users": data.get("total_users") or data.get("users"),
        "countries": data.get("total_countries") or data.get("countries"),
        "total_announcements": data.get("total_announcements"),
        "new_users_7d": data.get("new_users_7d"),
    }

    # Recent broadcasts (admin-gated, so do it with our own DB read here)
    broadcasts_recent = []
    try:
        with _conn() as c, c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT subject, mode, eligible_count, sent_count,
                           triggered_by, sent_at
                      FROM broadcast_log
                     ORDER BY sent_at DESC LIMIT 10""")
                for r in cur.fetchall():
                    broadcasts_recent.append({
                        "subject": r[0],
                        "mode": r[1],
                        "eligible_count": r[2],
                        "sent_count": r[3],
                        "triggered_by": r[4],
                        "sent_at": r[5].isoformat() if r[5] else None,
                    })
            except Exception:
                pass
    except Exception:
        pass
    out["broadcasts_recent"] = broadcasts_recent

    # Sitemap health
    sm = _safe_jget("/api/v1/sitemap/health") or {}
    out["sitemap"] = {
        "static_pages": sm.get("static_pages"),
        "dcpi_markets": sm.get("dcpi_markets"),
        "facilities_with_power": sm.get("facilities_with_power"),
        "recent_news": sm.get("recent_news"),
    }

    # Demo health (for "is the live demo up?" signal)
    demo = _safe_jget("/api/v1/demo/health") or {}
    out["demo"] = {
        "configured": demo.get("configured"),
        "model": demo.get("model"),
        "unique_ips_today": demo.get("unique_ips_today"),
        "total_calls_today": demo.get("total_calls_today"),
    }

    # Headline summary — single sentence the status page can use as hero text
    grade = out["brain"].get("grade") or "?"
    radar_summary = out["freshness"].get("summary") or {}
    breaches = radar_summary.get("breach", 0)
    if breaches:
        out["headline"] = (
            f"Brain grade {grade} · {breaches} domain(s) in breach · "
            "active incident response")
    elif grade in ("A", "B"):
        out["headline"] = (
            f"All systems normal · Brain grade {grade} · "
            "all data domains within SLA")
    else:
        out["headline"] = (
            f"Brain grade {grade} · radar nominal · "
            "see component scores for detail")

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=60, stale-while-revalidate=120"
    return resp, 200
