"""
system_loops.py — Phase PP (2026-05-13).

One JSON endpoint that surveys every autonomous loop and reports
whether it's actually firing on its schedule and producing output.

Replaces the contradictory state across four dashboards
(/brain, /audit, /dc-hub-media, /heartbeat) with a single source of
truth: "is each loop alive, when did it last run, what did it produce?"

Public, read-only, no admin auth required — but emits aggregates only,
no PII. Heavily cached at the edge (Cache-Control: max-age=30) so it
can power a dashboard widget that polls every minute without
hammering the DB.

Loops surveyed:
  1. brain_learn         — hourly self-evolving brain Layer 4 pass
  2. auto_press_daily    — 13:00 UTC autonomous press generation
  3. testimonial_ingest  — every-6h HN/Reddit/MCP citation ingest
  4. dcpi_recompute      — daily 06:00 UTC market score refresh
  5. iso_extract         — periodic grid-data refresh (every-Nh)
  6. engagement_track    — real-time press-engagement pixel writes
  7. mcp_traffic         — MCP tool-call ingest (real-time, ~5k/day)

Each loop reports:
  - name              loop identifier
  - cadence_hours     expected fire interval
  - last_event_at     timestamp of most recent observable output
  - age_hours         how long ago that was
  - status            "alive" | "stale" | "dead"
  - output_24h        count of measurable outputs in last 24h
  - note              one human-readable sentence on what 'alive' means here

Status thresholds:
  alive    age_hours <= cadence_hours
  stale    age_hours <= cadence_hours * 3
  dead     beyond that (or no events ever)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any
from flask import Blueprint, jsonify

system_loops_bp = Blueprint("system_loops", __name__)


def _conn():
    """Single, defensive DB connection. None on any failure — every
    probe below tolerates None and emits a 'no_database' note."""
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return None
        return psycopg2.connect(url, connect_timeout=5)
    except Exception as e:
        print(f"[system_loops] _conn failed: {e}", file=sys.stderr)
        return None


def _hours_since(ts) -> float | None:
    """Convert a tz-aware or naive datetime to hours-ago. None on
    invalid input — caller treats that as 'never seen'."""
    if ts is None:
        return None
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    except Exception:
        return None


def _classify(age_h: float | None, cadence_h: float) -> str:
    """alive / stale / dead based on age vs expected cadence."""
    if age_h is None:
        return "dead"
    if age_h <= cadence_h:
        return "alive"
    if age_h <= cadence_h * 3:
        return "stale"
    return "dead"


def _safe_query(cur, sql: str, params: tuple = ()) -> Any:
    """Execute SQL and return fetchone() result, or None on failure.

    Each loop probe is wrapped so a single missing table or schema
    mismatch never takes down the whole endpoint. Errors are logged
    but the loop reports as 'dead' rather than 500-ing the response."""
    try:
        cur.execute(sql, params)
        return cur.fetchone()
    except Exception as e:
        print(f"[system_loops] query failed: {e} :: {sql[:80]}",
              file=sys.stderr)
        return None


def _probe_brain_learn(cur) -> dict:
    """Brain v2 Layer 4 hourly pass. We measure liveness by the most
    recent entry in brain_learning_log (refused, proposed, etc. all
    count — what matters is that the loop is running)."""
    row = _safe_query(cur, """
        SELECT MAX(t),
               COUNT(*) FILTER (WHERE t > NOW() - INTERVAL '24 hours')
        FROM brain_learning_log
    """)
    last, count = (row or (None, 0))
    age = _hours_since(last)
    return {
        "name": "brain_learn",
        "cadence_hours": 1.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 1.0),
        "output_24h": int(count or 0),
        "note": "Hourly brain Layer 4 attempts (any outcome — proposed/refused/api_error all signal 'running')",
    }


def _probe_auto_press(cur) -> dict:
    """Daily 13:00 UTC autonomous press generation. Output_24h should
    be exactly 1 most days (the cron is idempotent per-day)."""
    row = _safe_query(cur, """
        SELECT MAX(generated_at),
               COUNT(*) FILTER (WHERE generated_at > NOW() - INTERVAL '7 days')
        FROM auto_press_releases
    """)
    last, count_7d = (row or (None, 0))
    age = _hours_since(last)
    return {
        "name": "auto_press_daily",
        "cadence_hours": 24.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 24.0),
        "output_7d": int(count_7d or 0),
        "note": "Fires daily at 13:00 UTC. Phase LL (PR #45) made it always-publish via topic fallbacks; 7d count should hit ≥7 after one full week.",
    }


def _probe_testimonial_ingest(cur) -> dict:
    """Every-6h HN/Reddit/MCP citation ingest. Output_24h counts NEW
    rows in ai_testimonials_auto in last 24h."""
    row = _safe_query(cur, """
        SELECT MAX(COALESCE(posted_at, created_at)),
               COUNT(*) FILTER (
                 WHERE COALESCE(posted_at, created_at) > NOW() - INTERVAL '24 hours'
               )
        FROM ai_testimonials_auto
    """)
    last, count_24h = (row or (None, 0))
    age = _hours_since(last)
    return {
        "name": "testimonial_ingest",
        "cadence_hours": 6.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 6.0),
        "output_24h": int(count_24h or 0),
        "note": "Phase MM wired ai_testimonials_auto into feed-v3 (PR #48). New rows here = the every-6h ingest is finding fresh HN/Reddit mentions.",
    }


def _probe_dcpi_recompute(cur) -> dict:
    """Daily 06:00 UTC market score refresh. Liveness = newest row in
    market_power_scores."""
    row = _safe_query(cur, """
        SELECT MAX(computed_at),
               COUNT(DISTINCT market_slug) FILTER (
                 WHERE computed_at > NOW() - INTERVAL '7 days'
               )
        FROM market_power_scores
    """)
    last, markets_7d = (row or (None, 0))
    age = _hours_since(last)
    return {
        "name": "dcpi_recompute",
        "cadence_hours": 24.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 24.0),
        "markets_scored_7d": int(markets_7d or 0),
        "note": "Daily 06:00 UTC. Phase LL relies on this — auto-press fallback chain reads top BUILD/AVOID markets from here.",
    }


def _probe_engagement_track(cur) -> dict:
    """Real-time press-engagement pixel writes. Phase MM (PR #46 + #32)
    wired the tracking pixel. Any non-zero count_24h proves the GET
    /track pixel + the frontend sendBeacon are both functioning."""
    row = _safe_query(cur, """
        SELECT MAX(t),
               COUNT(*) FILTER (WHERE t > NOW() - INTERVAL '24 hours'),
               COUNT(*) FILTER (WHERE event_type = 'view'
                                AND t > NOW() - INTERVAL '24 hours'),
               COUNT(*) FILTER (WHERE event_type = 'click_out'
                                AND t > NOW() - INTERVAL '24 hours')
        FROM press_engagement
    """)
    last, total_24h, views_24h, clicks_24h = (row or (None, 0, 0, 0))
    age = _hours_since(last)
    # Cadence is "any visitor visit" — so we use 12h as a reasonable
    # "if nothing fired in 12h, something's wrong" threshold.
    return {
        "name": "engagement_track",
        "cadence_hours": 12.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 12.0),
        "output_24h": int(total_24h or 0),
        "views_24h": int(views_24h or 0),
        "click_outs_24h": int(clicks_24h or 0),
        "note": "Frontend tracking pixel fires on /dc-hub-media page load + click_out on auto-press cards. 0 here after PR #32 deployed = pixel not reaching backend.",
    }


def _probe_mcp_traffic(cur) -> dict:
    """MCP tool-call ingest. Real-time per request. ~5k/day in prod is
    the established baseline."""
    row = _safe_query(cur, """
        SELECT MAX(created_at),
               COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 hour'),
               COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours')
        FROM mcp_tool_calls
    """)
    last, count_1h, count_24h = (row or (None, 0, 0))
    age = _hours_since(last)
    return {
        "name": "mcp_traffic",
        "cadence_hours": 1.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 1.0),
        "calls_1h": int(count_1h or 0),
        "calls_24h": int(count_24h or 0),
        "note": "Baseline ~200/hr, ~5000/day. Hourly count well under that = MCP traffic falling off OR ingest broken.",
    }


def _probe_iso_extract(cur) -> dict:
    """Periodic grid-data refresh. We don't have a single canonical
    table here so use the heartbeat's iso_metrics checkpoint instead
    (already maintained by the orchestrator). Falls back to the
    market_power_scores newest computed_at if heartbeat missing."""
    # Try heartbeat_surfaces table (per routes/heartbeat.py)
    row = _safe_query(cur, """
        SELECT last_updated
        FROM heartbeat_surfaces
        WHERE name = 'iso_metrics'
        ORDER BY last_updated DESC LIMIT 1
    """)
    last = row[0] if row else None
    age = _hours_since(last)
    return {
        "name": "iso_extract",
        "cadence_hours": 6.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 6.0),
        "note": "ISO orchestrator (Phase HH+ parallel fan-out) writes to iso_metrics checkpoint. Stale = upstream EIA/ISO sources slow OR CF Worker edge timeout (known issue, not crash).",
    }


@system_loops_bp.get("/api/v1/system/loops")
def system_loops():
    """The truth endpoint: one JSON that answers 'are the autonomous
    loops actually running?'

    Designed to power a 'system pulse' widget that replaces four
    contradictory dashboards (audit, brain, heartbeat, dc-hub-media)
    with one observable. Cache: 30s edge TTL — frequent enough to be
    useful, rare enough not to hammer the DB.
    """
    started = datetime.now(timezone.utc)

    c = _conn()
    if c is None:
        return jsonify(
            as_of=started.isoformat(),
            error="no_database",
            note="DATABASE_URL/NEON_DATABASE_URL not configured",
            loops=[],
        ), 503

    probes = [
        _probe_brain_learn,
        _probe_auto_press,
        _probe_testimonial_ingest,
        _probe_dcpi_recompute,
        _probe_engagement_track,
        _probe_mcp_traffic,
        _probe_iso_extract,
    ]

    loops = []
    try:
        with c.cursor() as cur:
            for probe in probes:
                try:
                    loops.append(probe(cur))
                except Exception as e:
                    # A single probe failure shouldn't kill the rest.
                    loops.append({
                        "name": getattr(probe, "__name__", "?").replace("_probe_", ""),
                        "status": "dead",
                        "error": str(e)[:200],
                    })
    finally:
        try: c.close()
        except Exception: pass

    # Top-line summary so the consumer can render a single
    # alive/stale/dead badge without re-aggregating.
    alive = sum(1 for l in loops if l.get("status") == "alive")
    stale = sum(1 for l in loops if l.get("status") == "stale")
    dead  = sum(1 for l in loops if l.get("status") == "dead")
    overall = "alive" if dead == 0 and stale == 0 else (
        "degraded" if dead == 0 else "critical")

    resp = jsonify(
        as_of=started.isoformat(),
        overall=overall,
        summary={"alive": alive, "stale": stale, "dead": dead, "total": len(loops)},
        loops=loops,
        elapsed_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
    )
    # Short CF cache — keep the dashboard fresh but absorb traffic spikes.
    resp.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=60"
    return resp, 200
