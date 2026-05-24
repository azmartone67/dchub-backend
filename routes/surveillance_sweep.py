"""
surveillance_sweep.py — Phase r29b (2026-05-24).

Unified surveillance rollup. Answers the operator's question:
"is everything green right now, and if not, what's wrong?"

Previous r29 attempt (full version with data-drift, table creation,
direct psycopg2 calls) caused a Railway boot issue and was reverted.
This v2 is intentionally minimal: composes EXISTING endpoints via
Flask's test_client (no new DB schema, no module-level imports of
heavy modules, no table creation). Same pattern brain_v2_layer4
already uses successfully for /heal/findings.

What it composes (read-only):
  - /api/v1/sentinel/findings   — page health (site_sentinel)
  - /api/v1/backup/status        — Neon backup + feed freshness
  - /api/v1/brain/status          — brain layer-4 verdict
  - /api/v1/media/press-health   — press cadence
  - /api/v1/heartbeat/inventory  — stale surfaces count
  - /api/health                   — pool / memory / uptime

Severity rollup: critical > high > medium > none → red / amber / green.
"""
from __future__ import annotations

import datetime
import time

from flask import Blueprint, jsonify, current_app


surveillance_bp = Blueprint("surveillance_sweep", __name__)


def _call(tc, path, timeout_note="ok"):
    """Internal Flask call. Returns (dict, http_code). Never raises."""
    try:
        r = tc.get(path)
        if r.status_code == 200:
            try:
                return r.get_json() or {}, 200
            except Exception:
                return {"_non_json": True}, 200
        return {"_status": r.status_code}, r.status_code
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:100]}"}, 0


@surveillance_bp.route("/api/v1/sentinel/sweep", methods=["GET"])
def sentinel_sweep():
    """Unified surveillance payload — composes existing endpoints.

    Polled by surveillance-sweep.yml every 15 min. Severity ≠ green
    emits ::warning:: in GHA logs so problems surface without operator
    digging.
    """
    t0 = time.time()
    checks: dict = {}
    actions: list = []

    with current_app.test_client() as tc:
        # Page health (site_sentinel)
        body, _ = _call(tc, "/api/v1/sentinel/findings")
        page_count = int(body.get("count") or 0)
        checks["pages"] = {
            "ok": page_count == 0,
            "unhealthy_count": page_count,
            "findings": (body.get("findings") or [])[:5],
        }
        if page_count > 0:
            actions.append({
                "category": "pages",
                "priority": "medium",
                "issue": f"{page_count} unhealthy pages",
            })

        # Data freshness — SLA breaches per domain. /api/v1/backup/status
        # doesn't exist as a REST route (it's an MCP tool); /api/v1/freshness
        # is the canonical Flask endpoint and exposes richer per-domain
        # SLA data including which domains have breached.
        body, _ = _call(tc, "/api/v1/freshness")
        breaches = body.get("sla_breaches") or []
        checks["freshness"] = {
            "ok": len(breaches) == 0,
            "sla_breached_domains": breaches,
            "breach_count": len(breaches),
            "dcpi_age_minutes": (body.get("dcpi") or {}).get("age_minutes"),
            "dcpi_published_markets": (body.get("dcpi") or {}).get("published_markets"),
        }
        if len(breaches) >= 3:
            actions.append({
                "category": "data_freshness",
                "priority": "high",
                "issue": f"{len(breaches)} domain(s) breaching SLA",
                "detail": ", ".join(breaches[:6]),
            })
        elif len(breaches) > 0:
            actions.append({
                "category": "data_freshness",
                "priority": "medium",
                "issue": f"{len(breaches)} domain(s) breaching SLA",
                "detail": ", ".join(breaches[:6]),
            })

        # Brain v2 verdict
        body, _ = _call(tc, "/api/v1/brain/status")
        brain_ok = (body.get("verdict") or "").startswith("healthy")
        checks["brain"] = {
            "ok": brain_ok,
            "verdict": body.get("verdict"),
            "learning_log_count": body.get("learning_log_count"),
            "proposed_fixes": body.get("proposed_fixes_count"),
            "minutes_since_run": body.get("minutes_since_last_run"),
        }
        if not brain_ok and body.get("verdict"):
            actions.append({
                "category": "brain",
                "priority": "medium",
                "issue": f"brain verdict: {body.get('verdict')}",
            })

        # Media chain
        body, _ = _call(tc, "/api/v1/media/press-health")
        media_verdict = body.get("verdict") or "unknown"
        checks["media"] = {
            "ok": media_verdict in ("healthy", "weak"),
            "verdict": media_verdict,
            "days_since_last_press": body.get("days_since_last_press"),
            "press_releases_30d":    body.get("press_releases_30d"),
            "source_of_truth_score": body.get("source_of_truth_score"),
        }
        if media_verdict == "silent":
            actions.append({
                "category": "media",
                "priority": "medium",
                "issue": "press output silent (>7 days)",
            })

        # Heartbeat (stale-surface backlog)
        body, _ = _call(tc, "/api/v1/heartbeat/inventory")
        stale = int(body.get("stale") or 0)
        fresh = int(body.get("fresh") or 0)
        checks["heartbeat"] = {
            "ok": stale < (fresh / 4) if fresh else False,
            "fresh": fresh,
            "stale": stale,
            "stale_ratio": round(stale / max(fresh + stale, 1), 3),
        }
        if checks["heartbeat"]["stale_ratio"] > 0.30:
            actions.append({
                "category": "heartbeat",
                "priority": "medium",
                "issue": f"stale-surface ratio {checks['heartbeat']['stale_ratio'] * 100:.0f}%",
            })

        # Core health (pool / memory / uptime / version)
        body, _ = _call(tc, "/api/health")
        pool = body.get("pool") or {}
        pool_ok = pool.get("status") == "healthy"
        checks["health"] = {
            "ok": pool_ok,
            "version": body.get("version"),
            "uptime_seconds": body.get("uptime_seconds"),
            "memory_rss_mb": body.get("memory_rss_mb"),
            "pool_status": pool.get("status"),
            "pool_utilization_pct": pool.get("utilization_pct"),
        }
        if not pool_ok:
            actions.append({
                "category": "infrastructure",
                "priority": "high",
                "issue": f"pool {pool.get('status')}",
                "detail": f"util={pool.get('utilization_pct')}% ",
            })
        if (body.get("memory_rss_mb") or 0) > 800:
            actions.append({
                "category": "infrastructure",
                "priority": "high",
                "issue": "memory above threshold",
                "detail": f"{body.get('memory_rss_mb')}mb",
            })

    # Severity rollup
    has = lambda p: any(a.get("priority") == p for a in actions)
    severity = (
        "red"   if has("critical") or has("high") else
        "amber" if has("medium") else
        "green"
    )

    return jsonify({
        "ok": severity == "green",
        "severity": severity,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "elapsed_ms": int((time.time() - t0) * 1000),
        "checks": checks,
        "actions": actions,
        "actions_count": len(actions),
        "purpose": (
            "Surveillance rollup — composes /sentinel/findings, "
            "/backup/status, /brain/status, /media/press-health, "
            "/heartbeat/inventory, /api/health. Polled by "
            "surveillance-sweep.yml every 15 min. Severity != green "
            "emits ::warning:: in GHA logs."
        ),
    }), 200
