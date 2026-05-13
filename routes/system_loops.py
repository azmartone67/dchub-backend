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
    but the loop reports as 'dead' rather than 500-ing the response.

    Phase QQ (2026-05-13): probes ALSO now record their last SQL
    error via _last_err so the response can surface it per-loop —
    previously a typo or aborted-transaction state silently turned
    every loop into "dead" with no diagnostic. _last_err is a single-
    item dict module-global; the probe sets it via _err() and the
    caller reads it after each probe.
    """
    try:
        cur.execute(sql, params)
        return cur.fetchone()
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        _err(msg)
        print(f"[system_loops] query failed: {msg} :: {sql[:80]}",
              file=sys.stderr)
        # CRITICAL: a failed cur.execute leaves the transaction in
        # ABORTED state. Without rollback, EVERY subsequent query in
        # this connection raises InFailedSqlTransaction and the rest
        # of the probes report dead even though their tables are fine.
        # That's the root cause of why 6 of 7 loops showed dead in
        # the first /api/v1/system/loops response — one query failed,
        # everything after silently rolled.
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None


# Per-probe last error — module global, single-slot. Each probe
# clears it before executing and reads it after, so the loop dict
# can carry an "error" field when the probe couldn't get its data.
_LAST_ERR: dict = {"msg": None}
def _err(msg: str | None):
    _LAST_ERR["msg"] = msg
def _take_err() -> str | None:
    e = _LAST_ERR["msg"]
    _LAST_ERR["msg"] = None
    return e


def _two_query_probe(cur, max_sql: str, count_sql: str, count_params: tuple = ()) -> tuple:
    """Phase QQ (2026-05-13): every probe used to be ONE compound query
    with MAX + multiple FILTER aggregates. If any column was misnamed
    OR the connection's transaction was already aborted, the entire
    query failed silently and the probe reported (None, 0, 0, ...) —
    which classifies as "dead" even when the underlying table is fine.

    Splitting into two separate simple queries (one MAX, one COUNT
    with WHERE) means a single query failure no longer cascades and
    the per-query rollback in _safe_query keeps the connection
    usable for downstream probes."""
    last_row = _safe_query(cur, max_sql)
    last = last_row[0] if last_row else None
    count_row = _safe_query(cur, count_sql, count_params)
    count = int(count_row[0]) if count_row else 0
    return last, count


def _probe_brain_learn(cur) -> dict:
    """Brain v2 Layer 4 hourly pass. We measure liveness by the most
    recent entry in brain_learning_log (refused, proposed, etc. all
    count — what matters is that the loop is running)."""
    _err(None)
    last, count = _two_query_probe(cur,
        "SELECT MAX(t) FROM brain_learning_log",
        "SELECT COUNT(*) FROM brain_learning_log WHERE t > NOW() - INTERVAL '24 hours'")
    age = _hours_since(last)
    return {
        "name": "brain_learn",
        "cadence_hours": 1.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 1.0),
        "output_24h": count,
        "error": _take_err(),
        "note": "Hourly brain Layer 4 attempts (any outcome — proposed/refused/api_error all signal 'running')",
    }


def _probe_auto_press(cur) -> dict:
    """Daily 13:00 UTC autonomous press generation."""
    _err(None)
    last, count_7d = _two_query_probe(cur,
        "SELECT MAX(generated_at) FROM auto_press_releases",
        "SELECT COUNT(*) FROM auto_press_releases WHERE generated_at > NOW() - INTERVAL '7 days'")
    age = _hours_since(last)
    return {
        "name": "auto_press_daily",
        "cadence_hours": 24.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 24.0),
        "output_7d": count_7d,
        "error": _take_err(),
        "note": "Fires daily at 13:00 UTC. Phase LL (PR #45) made it always-publish via topic fallbacks.",
    }


def _probe_testimonial_ingest(cur) -> dict:
    """Every-6h HN/Reddit/MCP citation ingest."""
    _err(None)
    last, count_24h = _two_query_probe(cur,
        "SELECT MAX(COALESCE(posted_at, created_at)) FROM ai_testimonials_auto",
        "SELECT COUNT(*) FROM ai_testimonials_auto WHERE COALESCE(posted_at, created_at) > NOW() - INTERVAL '24 hours'")
    age = _hours_since(last)
    return {
        "name": "testimonial_ingest",
        "cadence_hours": 6.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 6.0),
        "output_24h": count_24h,
        "error": _take_err(),
        "note": "Phase MM wired ai_testimonials_auto into feed-v3. 0 here may mean no fresh HN/Reddit dchub mentions (legit) OR cron not firing.",
    }


def _probe_dcpi_recompute(cur) -> dict:
    """Daily 06:00 UTC market score refresh."""
    _err(None)
    last_row = _safe_query(cur,
        "SELECT MAX(computed_at) FROM market_power_scores")
    last = last_row[0] if last_row else None
    # Second query: distinct markets scored in last 7 days
    count_row = _safe_query(cur,
        "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores WHERE computed_at > NOW() - INTERVAL '7 days'")
    markets_7d = int(count_row[0]) if count_row else 0
    age = _hours_since(last)
    return {
        "name": "dcpi_recompute",
        "cadence_hours": 24.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 24.0),
        "markets_scored_7d": markets_7d,
        "error": _take_err(),
        "note": "Daily 06:00 UTC. Phase LL relies on this — auto-press fallback chain reads top BUILD/AVOID markets from here.",
    }


def _probe_engagement_track(cur) -> dict:
    """Real-time press-engagement pixel writes."""
    _err(None)
    last_row = _safe_query(cur, "SELECT MAX(t) FROM press_engagement")
    last = last_row[0] if last_row else None
    total_row = _safe_query(cur,
        "SELECT COUNT(*) FROM press_engagement WHERE t > NOW() - INTERVAL '24 hours'")
    total_24h = int(total_row[0]) if total_row else 0
    views_row = _safe_query(cur,
        "SELECT COUNT(*) FROM press_engagement WHERE event_type = 'view' AND t > NOW() - INTERVAL '24 hours'")
    views_24h = int(views_row[0]) if views_row else 0
    clicks_row = _safe_query(cur,
        "SELECT COUNT(*) FROM press_engagement WHERE event_type = 'click_out' AND t > NOW() - INTERVAL '24 hours'")
    clicks_24h = int(clicks_row[0]) if clicks_row else 0
    age = _hours_since(last)
    return {
        "name": "engagement_track",
        "cadence_hours": 12.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 12.0),
        "output_24h": total_24h,
        "views_24h": views_24h,
        "click_outs_24h": clicks_24h,
        "error": _take_err(),
        "note": "Frontend tracking pixel fires on /dc-hub-media page load + click_out on auto-press cards.",
    }


def _probe_mcp_traffic(cur) -> dict:
    """MCP tool-call ingest. Real-time per request. ~5k/day baseline."""
    _err(None)
    last_row = _safe_query(cur, "SELECT MAX(created_at) FROM mcp_tool_calls")
    last = last_row[0] if last_row else None
    c1h_row = _safe_query(cur,
        "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at > NOW() - INTERVAL '1 hour'")
    count_1h = int(c1h_row[0]) if c1h_row else 0
    c24h_row = _safe_query(cur,
        "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at > NOW() - INTERVAL '24 hours'")
    count_24h = int(c24h_row[0]) if c24h_row else 0
    age = _hours_since(last)
    return {
        "name": "mcp_traffic",
        "cadence_hours": 1.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 1.0),
        "calls_1h": count_1h,
        "calls_24h": count_24h,
        "error": _take_err(),
        "note": "Baseline ~200/hr, ~5000/day. Hourly count well under that = MCP traffic falling off OR ingest broken.",
    }


def _probe_iso_extract(cur) -> dict:
    """ISO orchestrator liveness. Phase QQ fix: heartbeat is in-memory
    state in routes/heartbeat.py (no `heartbeat_surfaces` table), so
    import the _status() function directly and look up the 'iso_metrics'
    surface — same pattern Phase OO uses in brain_v2_layer4 to read
    heartbeat from inside another route handler."""
    last = None
    try:
        from routes.heartbeat import _status as _hb_status
        rows = _hb_status()
        for r in rows or []:
            if r.get("surface") == "iso_metrics":
                la = r.get("last_updated")
                # _status() may return either a datetime or an ISO string
                # depending on internal representation; handle both.
                if isinstance(la, str):
                    try:
                        last = datetime.fromisoformat(la.replace("Z", "+00:00"))
                    except Exception:
                        last = None
                else:
                    last = la
                break
    except Exception as e:
        _err(f"heartbeat-import: {type(e).__name__}: {str(e)[:120]}")

    age = _hours_since(last)
    return {
        "name": "iso_extract",
        "cadence_hours": 6.0,
        "last_event_at": last.isoformat() if last else None,
        "age_hours": round(age, 2) if age is not None else None,
        "status": _classify(age, 6.0),
        "error": _take_err(),
        "note": "Phase HH+ parallel fan-out writes the iso_metrics heartbeat checkpoint. Stale = upstream EIA/ISO sources slow OR CF Worker edge timeout.",
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
