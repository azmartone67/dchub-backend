"""
extractor_brain.py — Autonomous Intelligence Layer.

Records every extraction outcome, detects anomalies on grid metrics,
scores source quality, generates daily insights, and answers natural-language
queries about platform state.

Endpoints:
  POST /api/v1/intelligence/observe     internal — record an extraction outcome
  GET  /api/v1/intelligence/insights    daily-rolled summary (with optional ?date=YYYY-MM-DD)
  GET  /api/v1/intelligence/anomalies   recent anomalies
  GET  /api/v1/intelligence/quality     per-source 7d success rate
  GET  /api/v1/intelligence/dashboard   HTML dashboard
  GET  /api/v1/intelligence/ask?q=...   natural-language router
  GET  /api/v1/intelligence/health      health
"""

import json
import os
import re
import hmac
import statistics
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta, date
from typing import Any, Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs): pass


extractor_brain_bp = Blueprint("extractor_brain", __name__, url_prefix="/api/v1/intelligence")
SOURCE_ID = "extractor-brain"


def _dsn(): return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS extraction_intelligence (
    id              BIGSERIAL PRIMARY KEY,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_id       TEXT NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'partial', 'anomaly', 'idle', 'error')),
    rows_inserted   INTEGER,
    duration_ms     INTEGER,
    error           TEXT,
    anomaly_score   REAL DEFAULT 0,
    observations    JSONB,
    proposed_fix    TEXT
);

CREATE INDEX IF NOT EXISTS ix_ext_intel_source_obs ON extraction_intelligence (source_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_ext_intel_anomaly ON extraction_intelligence (anomaly_score DESC) WHERE anomaly_score > 0.5;
CREATE INDEX IF NOT EXISTS ix_ext_intel_observed ON extraction_intelligence (observed_at DESC);

CREATE TABLE IF NOT EXISTS daily_insights (
    id                  BIGSERIAL PRIMARY KEY,
    insight_date        DATE NOT NULL UNIQUE,
    summary             TEXT,
    top_changes         JSONB,
    top_anomalies       JSONB,
    sources_summary     JSONB,
    metric_summary      JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_daily_insights_date ON daily_insights (insight_date DESC);
"""

# 2026-06-14: the canonical outcome CHECK above only widens NEW tables. The
# live extraction_intelligence table was created long ago with the narrow
# ('success','failure','partial','anomaly') constraint — so the 9
# autonomous-brain feeds went SILENTLY DEAD on 2026-05-31 when commit b6991791
# started emitting honest 'idle' (wrote-nothing) / 'error' outcomes: every
# 0-row cycle's first INSERT raised CheckViolation, aborted the txn, and the
# recorder's bare except swallowed it (logger.debug). This idempotent ALTER
# drops the stale narrow constraint and re-adds the widened one so the existing
# live table accepts 'idle'/'error' too. NOT VALID-free: the only pre-existing
# rows are 'success'/'partial', both still allowed, so re-validation passes.
_WIDEN_OUTCOME_SQL = """
DO $$
BEGIN
    ALTER TABLE extraction_intelligence DROP CONSTRAINT IF EXISTS extraction_intelligence_outcome_check;
    ALTER TABLE extraction_intelligence ADD CONSTRAINT extraction_intelligence_outcome_check
        CHECK (outcome IN ('success', 'failure', 'partial', 'anomaly', 'idle', 'error'));
EXCEPTION WHEN others THEN
    -- never let a telemetry-schema migration crash startup
    NULL;
END $$;
"""

# Canonical set of accepted outcomes — single source of truth shared by the
# HTTP /observe validator, the in-process record_extraction helper, and the
# DB CHECK constraint above. Keep these three in lockstep.
VALID_OUTCOMES = ("success", "failure", "partial", "anomaly", "idle", "error")


def _ensure_tables():
    if getattr(_ensure_tables, "_done", False): return
    with _conn() as c, c.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        cur.execute(_WIDEN_OUTCOME_SQL)
        c.commit()
    _ensure_tables._done = True


# ---------------------------------------------------------------------------
# Anomaly detection — 3-sigma rule on rolling 24h baseline per (iso, metric)
# ---------------------------------------------------------------------------

def _detect_grid_anomaly(iso, metric_name, current_value):
    """Compute anomaly_score (0-1) for a new metric value vs rolling 24h.
    Returns dict: {is_anomaly, anomaly_score, baseline_mean, baseline_stddev, sigmas}."""
    if current_value is None:
        return {"is_anomaly": False, "anomaly_score": 0, "reason": "null value"}

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT metric_value FROM grid_data
                   WHERE iso = %s AND metric_name = %s
                     AND timestamp > NOW() - INTERVAL '24 hours'
                     AND timestamp < NOW() - INTERVAL '5 minutes'
                   ORDER BY timestamp DESC LIMIT 100""",
                (iso, metric_name),
            )
            historic = [float(r[0]) for r in cur.fetchall() if r[0] is not None]
    except Exception:
        return {"is_anomaly": False, "anomaly_score": 0, "reason": "db error"}

    if len(historic) < 5:
        return {"is_anomaly": False, "anomaly_score": 0, "reason": "insufficient history"}

    try:
        mean = statistics.mean(historic)
        stddev = statistics.stdev(historic) if len(historic) > 1 else 0
        if stddev == 0:
            return {"is_anomaly": False, "anomaly_score": 0, "reason": "zero variance"}
        sigmas = abs(float(current_value) - mean) / stddev
        anomaly_score = min(sigmas / 5.0, 1.0)
        return {
            "is_anomaly": sigmas > 3.0,
            "anomaly_score": round(anomaly_score, 3),
            "baseline_mean": round(mean, 2),
            "baseline_stddev": round(stddev, 2),
            "sigmas": round(sigmas, 2),
            "current_value": float(current_value),
        }
    except Exception as e:
        return {"is_anomaly": False, "anomaly_score": 0, "reason": str(e)}


# ---------------------------------------------------------------------------
# scan_grid_anomalies — proactive sweep over ALL (iso, metric) pairs
# ---------------------------------------------------------------------------
#
# The 3-sigma detector above (_detect_grid_anomaly) only fired inside
# POST /observe when a caller passed observations.grid_metrics — which nothing
# ever did, so 0/40k+ extraction_intelligence rows were ever scored.
#
# scan_grid_anomalies() closes that gap: it pulls the recent grid_data window,
# groups by (iso, metric_name), and for every pair with >=5 samples in 24h it
# treats the most-recent sample as "current" and the prior 24h as the baseline,
# reusing the same mean/std/sigma math as the live detector. Pairs whose latest
# value lands beyond |sigma| > 3 get a single 'anomaly' row written under
# source_id='grid-anomaly-scan'.
#
# Designed for the data-pulse cron (runs right after the ISO pull lands the
# freshest samples). Read-only unless dry_run=False.

GRID_ANOMALY_SOURCE_ID = "grid-anomaly-scan"


def scan_grid_anomalies(min_samples: int = 5, sigma_threshold: float = 10.0,
                        window_hours: int = 24, dry_run: bool = False) -> dict:
    """Sweep grid_data for per-(iso,metric) outliers and record anomalies.

    For each (iso, metric_name) with >= ``min_samples`` samples in the last
    ``window_hours``: baseline = all-but-latest samples, current = latest
    sample. When ``abs(sigmas) > sigma_threshold`` an extraction_intelligence
    row is INSERTed (source_id='grid-anomaly-scan', outcome='anomaly',
    observations={detected_anomalies:[...]}, anomaly_score=sigmas).

    Returns a summary dict: pairs scanned, flagged count, inserted count, and
    up to 25 example anomalies. ``dry_run=True`` computes everything but writes
    nothing — used by the verification probe. Never raises on a per-pair error.
    """
    summary = {
        "source_id": GRID_ANOMALY_SOURCE_ID,
        "window_hours": window_hours,
        "min_samples": min_samples,
        "sigma_threshold": sigma_threshold,
        "dry_run": bool(dry_run),
        "pairs_scanned": 0,
        "pairs_eligible": 0,
        "flagged": 0,
        "inserted": 0,
        "anomalies": [],
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }

    if not dry_run:
        try:
            _ensure_tables()
        except Exception:
            pass

    # Pull the full recent window once, grouped client-side by (iso, metric).
    # metadata holds nothing we need; metric_value + timestamp are enough.
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT iso, metric_name, metric_value, unit, timestamp
                   FROM grid_data
                   WHERE timestamp > NOW() - %s::interval
                     AND metric_value IS NOT NULL
                   ORDER BY iso, metric_name, timestamp DESC""",
                (f"{int(window_hours)} hours",),
            )
            rows = cur.fetchall()
    except Exception as e:
        summary["error"] = f"db error: {e}"
        return summary

    # Group rows by (iso, metric_name); rows are already newest-first.
    grouped: dict = {}
    for iso, metric_name, value, unit, ts in rows:
        if iso is None or metric_name is None or value is None:
            continue
        grouped.setdefault((iso, metric_name), {"unit": unit, "samples": []})
        grouped[(iso, metric_name)]["samples"].append((float(value), ts))

    summary["pairs_scanned"] = len(grouped)
    flagged = []

    for (iso, metric_name), info in grouped.items():
        samples = info["samples"]  # newest-first
        if len(samples) < min_samples:
            continue
        summary["pairs_eligible"] += 1

        current_value, current_ts = samples[0]
        baseline = [v for v, _ in samples[1:]]
        if len(baseline) < (min_samples - 1):
            continue
        try:
            mean = statistics.mean(baseline)
            stddev = statistics.stdev(baseline) if len(baseline) > 1 else 0
            if stddev == 0:
                continue
            sigmas = abs(current_value - mean) / stddev
        except Exception:
            continue

        if sigmas <= sigma_threshold:
            continue

        anomaly = {
            "iso": iso,
            "metric": metric_name,
            "unit": info["unit"],
            "current_value": round(current_value, 4),
            "baseline_mean": round(mean, 4),
            "baseline_stddev": round(stddev, 4),
            "sigmas": round(sigmas, 2),
            "sample_count": len(samples),
            "observed_at": current_ts.isoformat() if current_ts else None,
        }
        flagged.append(anomaly)

    summary["flagged"] = len(flagged)
    # Sort by sigma desc so the worst outliers lead the examples.
    flagged.sort(key=lambda a: a["sigmas"], reverse=True)
    summary["anomalies"] = flagged[:25]

    if dry_run or not flagged:
        return summary

    # Write one row per flagged (iso, metric) anomaly. anomaly_score = sigmas
    # (matches the task spec — NOT the 0-1 normalized score used elsewhere; the
    # /anomalies threshold of 0.5 is trivially cleared by any >3-sigma value).
    inserted = 0
    skipped_dup = 0
    try:
        with _conn() as c, c.cursor() as cur:
            for a in flagged:
                # r86g: dedup persistent anomalies. The scan runs every 15 min
                # and re-treats the latest sample as 'current', so a multi-hour
                # grid event would re-insert each run. Skip if this exact
                # (iso, metric, sample-timestamp) anomaly is already recorded.
                try:
                    cur.execute(
                        """SELECT 1 FROM extraction_intelligence
                            WHERE source_id = %s
                              AND observed_at > NOW() - INTERVAL '26 hours'
                              AND observations #>> '{detected_anomalies,0,iso}' = %s
                              AND observations #>> '{detected_anomalies,0,metric}' = %s
                              AND observations #>> '{detected_anomalies,0,observed_at}' = %s
                            LIMIT 1""",
                        (GRID_ANOMALY_SOURCE_ID, str(a.get("iso")),
                         str(a.get("metric")), str(a.get("observed_at"))))
                    if cur.fetchone():
                        skipped_dup += 1
                        continue
                except Exception:
                    pass  # dedup is best-effort; never block the insert
                # r86g: REAL per-row SAVEPOINT — the old c.rollback() rolled back
                # the WHOLE batch on one bad row (and over-counted). Isolate each.
                try:
                    cur.execute("SAVEPOINT a_ins")
                    cur.execute(
                        """INSERT INTO extraction_intelligence
                              (source_id, outcome, anomaly_score, observations)
                           VALUES (%s, 'anomaly', %s, %s) ON CONFLICT DO NOTHING""",
                        (GRID_ANOMALY_SOURCE_ID, float(a["sigmas"]),
                         json.dumps({"detected_anomalies": [a]})),
                    )
                    cur.execute("RELEASE SAVEPOINT a_ins")
                    inserted += 1
                except Exception:
                    try: cur.execute("ROLLBACK TO SAVEPOINT a_ins")
                    except Exception: pass
            c.commit()
    except Exception as e:
        summary["error"] = f"insert error: {e}"
    summary["skipped_duplicate"] = skipped_dup
    summary["inserted"] = inserted
    return summary


# ---------------------------------------------------------------------------
# Source quality scoring — rolling 7d success rate per source
# ---------------------------------------------------------------------------

def _compute_source_quality():
    """Returns dict: source_id → {success_rate, total_runs, ...}."""
    _ensure_tables()
    out = {}
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT source_id,
                      COUNT(*) AS total_runs,
                      SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS successes,
                      SUM(CASE WHEN outcome IN ('error','failure','execution_failed','exception')
                               THEN 1 ELSE 0 END) AS failures,
                      SUM(rows_inserted) AS total_rows,
                      AVG(duration_ms) AS avg_duration_ms,
                      MAX(observed_at) AS last_observed
               FROM extraction_intelligence
               WHERE observed_at > NOW() - INTERVAL '7 days'
               GROUP BY source_id"""
        )
        for src_id, total, successes, failures, rows, avg_ms, last_obs in cur.fetchall():
            total_i = max(int(total or 1), 1)
            successes_i = int(successes or 0)
            failures_i = int(failures or 0)
            # r86g HEALTH FIX: idle / anomaly / partial are NORMAL outcomes, not
            # failures — a resumed feed that found 0 new rows writes 'idle', the
            # grid anomaly scan writes 'anomaly', a short batch writes 'partial'.
            # Scoring health off success/total alone marked ~10 perfectly healthy
            # feeds "failing 0%" (the false alarm on the autonomous-intelligence
            # dashboard) because they legitimately never emit a literal 'success'.
            # Judge health by the real FAILURE rate; keep success_rate for display.
            ok_count = int(total or 0) - failures_i
            ok_rate = float(ok_count) / total_i
            success_rate = float(successes_i) / total_i
            out[src_id] = {
                "source_id": src_id,
                "total_runs_7d": int(total or 0),
                "successes_7d": successes_i,
                "failures_7d": failures_i,
                "success_rate": round(success_rate, 3),
                "ok_rate": round(ok_rate, 3),
                "total_rows_7d": int(rows or 0),
                "avg_duration_ms": int(avg_ms or 0),
                "last_observed_at": last_obs.isoformat() if last_obs else None,
                "health": (
                    "good" if ok_rate >= 0.9 else
                    "degraded" if ok_rate >= 0.5 else
                    "failing"
                ),
            }
        # r86f SURVIVORSHIP FIX: the 7d GROUP BY above only sees sources that
        # emitted IN the last 7d, so a feed that STOPPED dropped out entirely and
        # rendered as green-by-omission instead of failing. That's how 9
        # autonomous-brain-* feeds (last seen 2026-05-31) silently vanished and a
        # ~75% source outage read as "3 good / 0 failing". Surface any source
        # seen in the last 30d but silent >24h as health="failing" (stale) so the
        # dashboard + brain actually catch the outage.
        cur.execute(
            """SELECT source_id, MAX(observed_at) AS last_obs,
                      EXTRACT(EPOCH FROM (NOW() - MAX(observed_at))) / 3600.0 AS stale_hours
                 FROM extraction_intelligence
                WHERE observed_at > NOW() - INTERVAL '30 days'
                GROUP BY source_id
               HAVING MAX(observed_at) < NOW() - INTERVAL '24 hours'"""
        )
        # r88 (2026-06-15): RETIRED legacy ledger sources — their data still flows
        # via OTHER active loaders, so the r86f stale-surfacing above was raising a
        # false "failing — need attention" on feeds that were intentionally removed
        # from the active extraction list (autonomous_brain.py:1162-1168). Excluding
        # them keeps r86f catching REAL silent outages without crying wolf on these.
        _RETIRED_SOURCES = {
            # power-plants: live data flows via osm_overpass_loader (power_plants table)
            "autonomous-brain-power-plants",
            # substations: live data flows via the active infra-sync
            # (autonomous_brain substation_infrastructure / infra .sync())
            "autonomous-brain-substations",
        }
        for src_id, last_obs, stale_hours in cur.fetchall():
            if src_id in out:
                continue  # had recent activity in the 7d window
            if src_id in _RETIRED_SOURCES:
                continue  # intentionally retired — data covered by other loaders
            out[src_id] = {
                "source_id": src_id,
                "total_runs_7d": 0,
                "successes_7d": 0,
                "failures_7d": 0,
                "success_rate": 0.0,
                "ok_rate": 0.0,
                "total_rows_7d": 0,
                "avg_duration_ms": 0,
                "last_observed_at": last_obs.isoformat() if last_obs else None,
                "stale": True,
                "stale_hours": round(float(stale_hours or 0), 1),
                "health": "failing",
            }
    return out


# ---------------------------------------------------------------------------
# Auto-recovery — suggest fixes for failing sources
# ---------------------------------------------------------------------------

def _suggest_fix_for_source(source_id):
    """Look at recent failures and propose a fix."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT error, COUNT(*) FROM extraction_intelligence
               WHERE source_id = %s AND outcome = 'failure'
                 AND observed_at > NOW() - INTERVAL '7 days'
                 AND error IS NOT NULL
               GROUP BY error ORDER BY COUNT(*) DESC LIMIT 5""",
            (source_id,),
        )
        errors = [(e, c) for e, c in cur.fetchall()]

    if not errors:
        return None

    top_error, count = errors[0]
    suggestions = []
    if "404" in (top_error or ""):
        suggestions.append("URL likely moved. Check source's documentation for current endpoint.")
    if "Connection reset" in (top_error or "") or "URLError" in (top_error or ""):
        suggestions.append("Server may be rate-limiting or blocking. Try different User-Agent or rotate IPs.")
    if "timeout" in (top_error or "").lower():
        suggestions.append("Source is slow. Increase request timeout or use async fetcher.")
    if "JSON" in (top_error or "") or "decode" in (top_error or "").lower():
        suggestions.append("Response format changed. Check source's actual response and update parser.")
    if "column" in (top_error or "").lower() and "does not exist" in (top_error or "").lower():
        suggestions.append("Database schema mismatch. Check INSERT/SELECT against actual table columns.")

    return {
        "top_error": top_error,
        "occurrence_count": count,
        "all_recent_errors": [{"error": e, "count": c} for e, c in errors],
        "suggestions": suggestions or ["Manual investigation needed — review extraction_runs."],
    }


# ---------------------------------------------------------------------------
# Daily insight generation — deterministic summary from data
# ---------------------------------------------------------------------------

def _generate_daily_insight(target_date=None):
    """Generate or refresh insight for a given date (defaults to today UTC)."""
    _ensure_tables()
    if target_date is None:
        target_date = date.today()
    elif isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    with _conn() as c, c.cursor() as cur:
        # Source activity that day
        cur.execute(
            """SELECT source_id, outcome, COUNT(*), SUM(rows_inserted)
               FROM extraction_intelligence
               WHERE DATE(observed_at) = %s
               GROUP BY source_id, outcome""",
            (target_date,),
        )
        source_rows = cur.fetchall()

        # Anomalies that day
        cur.execute(
            """SELECT source_id, anomaly_score, observations, observed_at
               FROM extraction_intelligence
               WHERE DATE(observed_at) = %s AND anomaly_score >= 10
               ORDER BY anomaly_score DESC LIMIT 10""",
            (target_date,),
        )
        anomaly_rows = cur.fetchall()

        # Grid metric snapshot (start vs end of day)
        cur.execute(
            """SELECT iso, metric_name,
                      MIN(metric_value) AS day_min,
                      MAX(metric_value) AS day_max,
                      AVG(metric_value) AS day_avg,
                      COUNT(*) AS observations
               FROM grid_data
               WHERE DATE(timestamp) = %s
               GROUP BY iso, metric_name
               ORDER BY iso, metric_name""",
            (target_date,),
        )
        metric_rows = cur.fetchall()

        # SEC filings that day
        try:
            cur.execute(
                """SELECT form_type, COUNT(*) FROM sec_filings_v2
                   WHERE filing_date = %s
                   GROUP BY form_type""",
                (target_date,),
            )
            sec_rows = cur.fetchall()
        except Exception:
            sec_rows = []

    # Build summary
    sources_summary = {}
    for src, outcome, count, rows in source_rows:
        sources_summary.setdefault(src, {"runs": 0, "rows": 0, "successes": 0, "failures": 0})
        sources_summary[src]["runs"] += count
        sources_summary[src]["rows"] += int(rows or 0)
        if outcome == "success":
            sources_summary[src]["successes"] += count
        elif outcome == "failure":
            sources_summary[src]["failures"] += count

    top_anomalies = [
        {
            "source_id": src,
            "anomaly_score": round(float(score or 0), 3),
            "observations": obs if isinstance(obs, dict) else None,
            "observed_at": ts.isoformat() if ts else None,
        }
        for src, score, obs, ts in anomaly_rows
    ]

    metric_summary = {}
    for iso, metric_name, dmin, dmax, davg, count in metric_rows:
        if iso not in metric_summary:
            metric_summary[iso] = []
        metric_summary[iso].append({
            "metric": metric_name,
            "min": float(dmin) if dmin is not None else None,
            "max": float(dmax) if dmax is not None else None,
            "avg": float(davg) if davg is not None else None,
            "observations": int(count or 0),
        })

    sec_summary = {form: int(count or 0) for form, count in sec_rows} if sec_rows else {}

    # Build prose summary
    parts = []
    total_runs = sum(s["runs"] for s in sources_summary.values())
    total_rows = sum(s["rows"] for s in sources_summary.values())
    parts.append(f"On {target_date.isoformat()}: {total_runs} extractions across {len(sources_summary)} sources, {total_rows:,} new rows ingested.")

    if top_anomalies:
        parts.append(f"Detected {len(top_anomalies)} anomalies (highest score: {top_anomalies[0]['anomaly_score']:.2f}).")

    if metric_summary:
        iso_list = list(metric_summary.keys())
        parts.append(f"Grid data covered {len(iso_list)} ISOs: {', '.join(iso_list)}.")

    if sec_summary:
        sec_total = sum(sec_summary.values())
        parts.append(f"SEC filings: {sec_total} total ({', '.join(f'{k}={v}' for k, v in sec_summary.items())}).")

    failing_sources = [s for s, info in sources_summary.items() if info["failures"] > info["successes"]]
    if failing_sources:
        parts.append(f"Sources needing attention: {', '.join(failing_sources[:5])}.")

    summary = " ".join(parts)

    # Top changes — sources that actually CHANGED data today (rows > 0).
    # 2026-06-15: exclude 0-row sources. The autonomous-brain-* article
    # extractors run every brain cycle but legitimately ingest 0 rows most
    # cycles (bulk infra data is sourced from HIFLD/EIA via the GH-runner, not
    # article mentions), so including them made idle feeds masquerade as "Top
    # Changes". A source with 0 rows ingested did not change anything.
    top_changes = sorted(
        ((s, info) for s, info in sources_summary.items() if info["rows"] > 0),
        key=lambda kv: kv[1]["rows"],
        reverse=True,
    )[:5]
    top_changes = [{"source_id": s, **info} for s, info in top_changes]

    # Persist
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO daily_insights
                  (insight_date, summary, top_changes, top_anomalies, sources_summary, metric_summary)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (insight_date) DO UPDATE SET
                  summary           = EXCLUDED.summary,
                  top_changes       = EXCLUDED.top_changes,
                  top_anomalies     = EXCLUDED.top_anomalies,
                  sources_summary   = EXCLUDED.sources_summary,
                  metric_summary    = EXCLUDED.metric_summary""",
            (
                target_date,
                summary,
                json.dumps(top_changes),
                json.dumps(top_anomalies),
                json.dumps(sources_summary),
                json.dumps({**metric_summary, "_sec_filings": sec_summary} if sec_summary else metric_summary),
            ),
        )
        c.commit()

    return {
        "insight_date": target_date.isoformat(),
        "summary": summary,
        "top_changes": top_changes,
        "top_anomalies": top_anomalies,
        "sources_summary": sources_summary,
        "metric_summary": metric_summary,
        "sec_summary": sec_summary,
    }


# ---------------------------------------------------------------------------
# POST /observe — internal endpoint that records every extraction outcome
# ---------------------------------------------------------------------------

@extractor_brain_bp.route("/observe", methods=["POST"])
def observe():
    _ensure_tables()
    p = request.get_json(silent=True) or {}

    source_id = p.get("source_id")
    if not source_id:
        return jsonify(error="source_id required"), 400

    outcome = p.get("outcome", "success")
    if outcome not in VALID_OUTCOMES:
        return jsonify(error="invalid outcome"), 400

    rows_inserted = p.get("rows_inserted")
    duration_ms = p.get("duration_ms")
    error_text = p.get("error")
    observations = p.get("observations") or {}
    anomaly_score = float(p.get("anomaly_score", 0))

    # If observations include grid metrics, run anomaly detection
    detected_anomalies = []
    if isinstance(observations, dict) and observations.get("grid_metrics"):
        for m in observations["grid_metrics"]:
            iso = m.get("iso")
            metric_name = m.get("metric")
            value = m.get("value")
            if iso and metric_name and value is not None:
                anomaly = _detect_grid_anomaly(iso, metric_name, value)
                if anomaly.get("is_anomaly"):
                    detected_anomalies.append({**m, **anomaly})

    if detected_anomalies:
        # Use highest score
        anomaly_score = max(a["anomaly_score"] for a in detected_anomalies)
        observations["detected_anomalies"] = detected_anomalies

    # Compute proposed_fix if outcome is failure
    proposed_fix = None
    if outcome == "failure":
        suggestion = _suggest_fix_for_source(source_id)
        if suggestion:
            proposed_fix = json.dumps(suggestion)

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO extraction_intelligence
                  (source_id, outcome, rows_inserted, duration_ms, error,
                   anomaly_score, observations, proposed_fix)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
               RETURNING id""",
            (source_id, outcome, rows_inserted, duration_ms, error_text,
             anomaly_score, json.dumps(observations), proposed_fix),
        )
        run_id = cur.fetchone()[0]
        c.commit()

    return jsonify(
        observation_id=run_id,
        anomaly_score=anomaly_score,
        anomalies_detected=len(detected_anomalies),
        proposed_fix_attached=bool(proposed_fix),
    ), 200


# ---------------------------------------------------------------------------
# record_extraction — in-process ledger write for ingestion jobs
# ---------------------------------------------------------------------------

def record_extraction(source_id: str, outcome: str = "success",
                      rows_inserted=None, duration_ms=None,
                      error=None, observations=None) -> None:
    """Same row POST /observe records, minus the HTTP hop.

    For in-process jobs (deal ingestion, gas refresh, ISO pulls): calling the
    worker's own HTTP API from inside the process is the synchronous-self-call
    pattern that exhausts the small replica pool — write the ledger row
    directly instead. Never raises: telemetry must not break an ingestion job.
    """
    try:
        if outcome not in VALID_OUTCOMES:
            outcome = "partial"
        _ensure_tables()
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO extraction_intelligence
                      (source_id, outcome, rows_inserted, duration_ms, error,
                       anomaly_score, observations, proposed_fix)
                   VALUES (%s, %s, %s, %s, %s, 0, %s, NULL) ON CONFLICT DO NOTHING""",
                (source_id, outcome, rows_inserted, duration_ms,
                 (str(error)[:500] if error else None),
                 json.dumps(observations or {})),
            )
            c.commit()
    except Exception:
        note_swallowed_write("extraction_intelligence", where="extractor_brain.record_extraction")
        pass


# ---------------------------------------------------------------------------
# GET /insights — daily-rolled summary
# ---------------------------------------------------------------------------

# AUTO-REPAIR: duplicate route '/insights' also in ai_orchestrator.py:959 — review and remove one
@extractor_brain_bp.route("/insights", methods=["GET"])
def get_insights():
    _ensure_tables()
    target = request.args.get("date")

    # Force-regenerate today's insight to get latest data
    if not target or target == date.today().isoformat():
        return jsonify(_generate_daily_insight()), 200

    # Otherwise read from daily_insights table
    try:
        d = date.fromisoformat(target)
    except ValueError:
        return jsonify(error="date must be YYYY-MM-DD"), 400

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT insight_date, summary, top_changes, top_anomalies,
                      sources_summary, metric_summary, created_at
               FROM daily_insights WHERE insight_date = %s""",
            (d,),
        )
        row = cur.fetchone()

    if row is None:
        # Generate on the fly
        return jsonify(_generate_daily_insight(d)), 200

    return jsonify({
        "insight_date": row[0].isoformat(),
        "summary": row[1],
        "top_changes": row[2],
        "top_anomalies": row[3],
        "sources_summary": row[4],
        "metric_summary": row[5],
        "created_at": row[6].isoformat() if row[6] else None,
    }), 200


# ---------------------------------------------------------------------------
# GET /anomalies — recent anomalies
# ---------------------------------------------------------------------------
# AUTO-REPAIR: duplicate route '/anomalies' also in ai_orchestrator.py:967 — review and remove one

@extractor_brain_bp.route("/anomalies", methods=["GET"])
def get_anomalies():
    _ensure_tables()
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
    except ValueError:
        return jsonify(error="limit must be int"), 400

    threshold = float(request.args.get("min_score", 0.5))
    hours = int(request.args.get("hours", 24))

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT id, observed_at, source_id, anomaly_score, observations
               FROM extraction_intelligence
               WHERE anomaly_score >= %s
                 AND observed_at > NOW() - %s::interval
               ORDER BY anomaly_score DESC, observed_at DESC
               LIMIT %s""",
            (threshold, f"{hours} hours", limit),
        )
        rows = cur.fetchall()

    anomalies = [{
        "id": r[0],
        "observed_at": r[1].isoformat() if r[1] else None,
        "source_id": r[2],
        "anomaly_score": float(r[3] or 0),
        "observations": r[4],
    } for r in rows]

    return jsonify(count=len(anomalies), anomalies=anomalies), 200


# ---------------------------------------------------------------------------
# GET /quality — per-source 7d success rate
# ---------------------------------------------------------------------------

@extractor_brain_bp.route("/quality", methods=["GET"])
def get_quality():
    quality = _compute_source_quality()
    sorted_sources = sorted(
        quality.values(),
        key=lambda x: (x["success_rate"], x["total_runs_7d"]),
    )
    return jsonify(
        count=len(sorted_sources),
        sources=sorted_sources,
        summary={
            "good_count": sum(1 for s in sorted_sources if s["health"] == "good"),
            "degraded_count": sum(1 for s in sorted_sources if s["health"] == "degraded"),
            "failing_count": sum(1 for s in sorted_sources if s["health"] == "failing"),
        },
    ), 200


# ---------------------------------------------------------------------------
# GET /ask?q=... — natural language router
# ---------------------------------------------------------------------------

@extractor_brain_bp.route("/ask", methods=["GET", "POST"])
def ask():
    q = (request.args.get("q") or
         (request.get_json(silent=True) or {}).get("q") or
         (request.get_json(silent=True) or {}).get("query") or "").lower().strip()

    if not q:
        return jsonify(
            error="provide ?q=... query",
            examples=[
                "what's happening today",
                "show anomalies",
                "ercot status",
                "recent 8-k filings",
                "source quality",
                "platform health",
            ],
        ), 400

    # Pattern-matched routing
    response = {"query": q, "interpretations": []}

    # Today / what's happening
    if any(k in q for k in ["today", "happening", "summary", "overview", "what's new"]):
        insight = _generate_daily_insight()
        response["interpretations"].append({
            "intent": "daily_insight",
            "data": {
                "summary": insight["summary"],
                "top_changes": insight["top_changes"][:3],
                "anomalies_count": len(insight["top_anomalies"]),
            },
        })

    # Anomalies
    if any(k in q for k in ["anomal", "weird", "unusual", "outlier", "spike", "drop"]):
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT source_id, anomaly_score, observations, observed_at
                   FROM extraction_intelligence
                   WHERE anomaly_score > 0.5 AND observed_at > NOW() - INTERVAL '24 hours'
                   ORDER BY anomaly_score DESC LIMIT 5"""
            )
            rows = cur.fetchall()
        response["interpretations"].append({
            "intent": "recent_anomalies",
            "data": [
                {"source_id": r[0], "score": float(r[1] or 0), "observed_at": r[3].isoformat() if r[3] else None}
                for r in rows
            ],
        })

    # ISO-specific
    iso_keywords = {"ercot": "ERCOT", "caiso": "CAISO", "nyiso": "NYISO",
                    "miso": "MISO", "spp": "SPP", "isone": "ISONE", "pjm": "PJM",
                    "texas": "ERCOT", "california": "CAISO", "new york": "NYISO"}
    matched_iso = None
    for kw, iso_label in iso_keywords.items():
        if kw in q:
            matched_iso = iso_label
            break

    if matched_iso:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT metric_name, metric_value, unit, timestamp
                   FROM grid_data WHERE iso = %s
                   ORDER BY timestamp DESC LIMIT 20""",
                (matched_iso,),
            )
            rows = cur.fetchall()
        latest_metrics = {}
        for n, v, u, ts in rows:
            if n not in latest_metrics:
                latest_metrics[n] = {"metric": n, "value": float(v) if v else None, "unit": u}
        response["interpretations"].append({
            "intent": f"iso_status_{matched_iso}",
            "data": {"iso": matched_iso, "metrics": list(latest_metrics.values())},
        })

    # SEC filings
    if any(k in q for k in ["8-k", "8k", "10-k", "10k", "10-q", "10q", "filing", "sec ", "edgar"]):
        with _conn() as c, c.cursor() as cur:
            try:
                cur.execute(
                    """SELECT ticker, company_name, form_type, filing_date, primary_doc_url
                       FROM sec_filings_v2
                       ORDER BY filing_date DESC, accepted_at DESC NULLS LAST
                       LIMIT 10"""
                )
                rows = cur.fetchall()
            except Exception:
                rows = []
        response["interpretations"].append({
            "intent": "recent_filings",
            "data": [
                {"ticker": r[0], "company": r[1], "form": r[2],
                 "date": r[3].isoformat() if r[3] else None, "url": r[4]}
                for r in rows
            ],
        })

    # Source quality / health
    if any(k in q for k in ["quality", "health", "fail", "broken", "stale", "dead"]):
        quality = _compute_source_quality()
        failing = [s for s in quality.values() if s["health"] != "good"]
        response["interpretations"].append({
            "intent": "source_quality",
            "data": {
                "total_sources": len(quality),
                "failing": failing[:10],
                "summary": f"{len(failing)} of {len(quality)} sources need attention",
            },
        })

    # Deals / M&A
    if any(k in q for k in ["deal", "m&a", "merger", "acquisition", "transaction"]):
        with _conn() as c, c.cursor() as cur:
            try:
                cur.execute(
                    """SELECT id, buyer, seller, value, type, date FROM deals
                       WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                       ORDER BY date DESC LIMIT 10"""
                )
                rows = cur.fetchall()
            except Exception:
                rows = []
        response["interpretations"].append({
            "intent": "recent_deals",
            "data": [
                {"id": r[0], "buyer": r[1], "seller": r[2],
                 "value_millions": float(r[3]) if r[3] else None,
                 "type": r[4], "date": r[5]}
                for r in rows
            ],
        })

    # Default: if nothing matched, return platform overview
    if not response["interpretations"]:
        insight = _generate_daily_insight()
        quality = _compute_source_quality()
        response["interpretations"].append({
            "intent": "platform_overview",
            "data": {
                "summary": insight["summary"],
                "active_sources": len(quality),
                "anomalies_24h": len(insight["top_anomalies"]),
            },
        })

    return jsonify(response), 200


# ---------------------------------------------------------------------------
# GET /dashboard — HTML view
# AUTO-REPAIR: duplicate route '/dashboard' also in main.py:24898 — review and remove one
# ---------------------------------------------------------------------------

@extractor_brain_bp.route("/dashboard", methods=["GET"])
def dashboard():
    _ensure_tables()
    insight = _generate_daily_insight()
    quality = _compute_source_quality()

    # Build HTML
    html = ['<!doctype html><html><head><meta charset="utf-8">',
            '<title>DC Hub — Autonomous Intelligence</title>',
            '<style>',
            'body{font-family:system-ui,sans-serif;max-width:1400px;margin:20px auto;padding:0 20px;color:#222;background:#fafafa}',
            'h1{margin:0 0 5px}',
            '.muted{color:#888;font-size:13px}',
            '.summary{background:white;padding:18px;border-radius:8px;margin:16px 0;border-left:4px solid #0a6b22;font-size:15px;line-height:1.6}',
            '.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin:16px 0}',
            '.card{background:white;padding:16px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}',
            '.card h3{margin:0 0 8px;font-size:13px;color:#666;text-transform:uppercase;letter-spacing:0.5px}',
            '.kpi{font-size:28px;font-weight:600}',
            '.bar{display:flex;height:24px;border-radius:4px;overflow:hidden;margin-top:8px}',
            '.bar .good{background:#0a6b22}',
            '.bar .degraded{background:#c89800}',
            '.bar .failing{background:#d23}',
            'table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}',
            'th,td{padding:6px 8px;border-bottom:1px solid #eee;text-align:left}',
            'th{background:#f5f5f5;font-weight:600}',
            '.score-high{color:#d23;font-weight:600}',
            '.score-med{color:#c89800}',
            '.score-low{color:#666}',
            '</style></head><body>',
            '<h1>🧠 DC Hub — Autonomous Intelligence</h1>',
            '<div class="muted">Real-time platform self-awareness · brain layer observes every extraction · ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC') + '</div>',
            '<div class="summary">📊 <b>' + insight['summary'] + '</b></div>',
            '<div class="row">',
            f'<div class="card"><h3>Sources Active (7d)</h3><div class="kpi">{len(quality)}</div></div>',
            f'<div class="card"><h3>Top Changes Today</h3><div class="kpi">{len(insight["top_changes"])}</div></div>',
            f'<div class="card"><h3>Anomalies (24h)</h3><div class="kpi">{len(insight["top_anomalies"])}</div></div>',
            # r47.34 (2026-05-26): when daily_insights hasn't aggregated
            # today's grid metrics yet (early morning before the rollup
            # cron, or transient gap), avoid showing a misleading "0 ISOs
            # Reporting" headline. Surface the live `/api/v1/grid/totals`
            # signal alongside so the user knows the underlying ingestion
            # is fine — only the daily aggregator is behind.
            (f'<div class="card"><h3>ISOs Reporting</h3>'
              f'<div class="kpi">{len(insight.get("metric_summary", {}))}</div></div>'
             if insight.get("metric_summary")
             else '<div class="card"><h3>ISOs Reporting</h3>'
                   '<div class="kpi" style="color:#999">…</div>'
                   '<div class="muted" style="font-size:.75rem">'
                   'Daily rollup not yet aggregated. '
                   '<a href="/api/v1/grid/totals" style="color:#6366f1">'
                   'Live ISO data</a> &middot; ingest sources active above.'
                   '</div></div>'),
            '</div>']

    # Source quality breakdown
    good = sum(1 for s in quality.values() if s["health"] == "good")
    degraded = sum(1 for s in quality.values() if s["health"] == "degraded")
    failing = sum(1 for s in quality.values() if s["health"] == "failing")
    total = max(len(quality), 1)

    html.append('<h2>Source Health (7-day rolling)</h2>')
    html.append('<div class="bar">')
    html.append(f'<div class="good" style="width:{good*100//total}%"></div>')
    html.append(f'<div class="degraded" style="width:{degraded*100//total}%"></div>')
    html.append(f'<div class="failing" style="width:{failing*100//total}%"></div>')
    html.append('</div>')
    html.append(f'<div class="muted">{good} good · {degraded} degraded · {failing} failing</div>')

    # Recent anomalies
    if insight['top_anomalies']:
        html.append('<h2>Recent Anomalies</h2><table>')
        html.append('<tr><th>Source</th><th>ISO</th><th>Metric</th><th>Score (σ)</th><th>Observed</th></tr>')
        for a in insight['top_anomalies'][:10]:
            score = a.get('anomaly_score', 0)
            # grid-anomaly-scan writes anomaly_score = sigmas (NOT the 0-1 scale),
            # so band on sigma magnitude, and surface the (iso, metric) that fired
            # so distinct per-fuel anomalies don't render as identical rows.
            cls = 'score-high' if score >= 20 else 'score-med' if score >= 10 else 'score-low'
            obs = a.get('observations') or {}
            det = (obs.get('detected_anomalies') or [{}])[0] if isinstance(obs, dict) else {}
            iso = det.get('iso') or '—'
            metric = det.get('metric') or '—'
            html.append(f'<tr><td>{a["source_id"]}</td><td>{iso}</td><td>{metric}</td><td class="{cls}">{score:.2f}</td><td>{a.get("observed_at", "?")}</td></tr>')
        html.append('</table>')

    # Top changes
    if insight['top_changes']:
        html.append('<h2>Top Changes Today</h2><table>')
        html.append('<tr><th>Source</th><th>Runs</th><th>Rows Ingested</th><th>Successes</th><th>Failures</th></tr>')
        for ch in insight['top_changes']:
            html.append(f'<tr><td>{ch["source_id"]}</td><td>{ch["runs"]}</td><td>{ch["rows"]:,}</td><td>{ch["successes"]}</td><td>{ch["failures"]}</td></tr>')
        html.append('</table>')

    # Failing sources
    failing_list = [s for s in quality.values() if s["health"] == "failing"]
    if failing_list:
        html.append('<h2>⚠️ Sources Failing — Need Attention</h2><table>')
        html.append('<tr><th>Source</th><th>OK Rate</th><th>Failures (7d)</th><th>Runs (7d)</th><th>Last Observed</th></tr>')
        for s in failing_list[:10]:
            stale_tag = ' <span style="color:#f59e0b">(stale)</span>' if s.get("stale") else ''
            html.append(f'<tr><td><b>{s["source_id"]}</b>{stale_tag}</td><td>{s.get("ok_rate", 0)*100:.0f}%</td><td>{s.get("failures_7d", 0)}</td><td>{s["total_runs_7d"]}</td><td>{s.get("last_observed_at", "?")}</td></tr>')
        html.append('</table>')

    html.append('<div class="muted" style="margin-top:32px">DC Hub Autonomous Intelligence · ')
    html.append('<a href="/api/v1/intelligence/insights">JSON insights</a> · ')
    html.append('<a href="/api/v1/intelligence/anomalies">anomalies</a> · ')
    html.append('<a href="/api/v1/intelligence/quality">quality</a> · ')
    html.append('<a href="/api/v1/intelligence/ask?q=what is happening today">ask</a>')
    html.append('</div>')
    html.append('</body></html>')

    return "".join(html), 200, {"Content-Type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------------------
# POST /scan-grid-anomalies — admin/cron-triggered proactive anomaly sweep
# ---------------------------------------------------------------------------

def _admin_authorized() -> bool:
    """Accept X-Admin-Key header, ?admin_key=, or Bearer token matching
    DCHUB_ADMIN_KEY / DCHUB_ADMIN_SECRET / DCHUB_INTERNAL_KEY. The data-pulse
    cron sends `Authorization: Bearer <DCHUB_ADMIN_SECRET>` (same as the source
    heartbeat step), so accept that shape too."""
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or bearer or "")
    if not provided:
        return False
    expected = {v for v in (
        os.environ.get("DCHUB_ADMIN_KEY"),
        os.environ.get("DCHUB_ADMIN_SECRET"),
        os.environ.get("DCHUB_INTERNAL_KEY"),
        # SECURITY (2026-07-31): the hardcoded 'dchub-admin-secret-2026' literal
        # was removed here and from routes/sources.py + admin_ai_deals.py (route-
        # security audit critical). The data-pulse cron now sends
        # `Bearer ${DCHUB_ADMIN_KEY}` (see .github/workflows/data-pulse.yml),
        # which is set in prod and accepted above — so the scan still runs.
    ) if v}
    return any(hmac.compare_digest(provided, e) for e in expected)


@extractor_brain_bp.route("/scan-grid-anomalies", methods=["POST", "GET"])
def scan_grid_anomalies_endpoint():
    """Run the proactive grid anomaly sweep. Admin-gated for writes.

    Query/body params:
      dry_run=1          compute only, no inserts (default for GET; allowed anon)
      sigma=<float>      sigma threshold (default 3.0)
      min_samples=<int>  minimum samples/24h per pair (default 5)
      window_hours=<int> lookback window (default 24)

    A real (write) run requires admin auth. Dry runs are open so the cron's
    pre-check and the dashboard can preview without a key.
    """
    p = request.get_json(silent=True) or {}

    def _arg(name, default):
        return request.args.get(name, p.get(name, default))

    # GET defaults to dry_run; POST defaults to a real write run.
    dr_default = "1" if request.method == "GET" else "0"
    dry_run = str(_arg("dry_run", dr_default)).lower() in ("1", "true", "yes")

    if not dry_run and not _admin_authorized():
        return jsonify(error="admin auth required for write run; pass dry_run=1 to preview"), 401

    try:
        sigma = float(_arg("sigma", 10.0))
        min_samples = int(_arg("min_samples", 5))
        window_hours = int(_arg("window_hours", 24))
    except (TypeError, ValueError):
        return jsonify(error="sigma/min_samples/window_hours must be numeric"), 400

    result = scan_grid_anomalies(
        min_samples=min_samples,
        sigma_threshold=sigma,
        window_hours=window_hours,
        dry_run=dry_run,
    )
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# AUTO-REPAIR: duplicate route '/health' also in diag_app.py:44 — review and remove one
# GET /health
# ---------------------------------------------------------------------------

@extractor_brain_bp.route("/health", methods=["GET"])
def health():
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*), MAX(observed_at), COUNT(DISTINCT source_id)
               FROM extraction_intelligence WHERE observed_at > NOW() - INTERVAL '24 hours'"""
        )
        observations_24h, latest, distinct_sources = cur.fetchone()

        cur.execute("SELECT COUNT(*) FROM daily_insights")
        insights_count = cur.fetchone()[0]

    return jsonify(
        status="ok",
        source_id=SOURCE_ID,
        observations_24h=int(observations_24h or 0),
        latest_observation=latest.isoformat() if latest else None,
        distinct_sources_24h=int(distinct_sources or 0),
        daily_insights_total=int(insights_count or 0),
    ), 200
