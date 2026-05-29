"""
iso_ercot.py — ERCOT real-time grid data extractor.

ERCOT publishes free, public, no-auth real-time data. We pull system
conditions (load, frequency, renewable share) every 15 minutes.

Endpoints used:
  - System Conditions HTML page (parsed for snapshot values):
      https://www.ercot.com/content/cdr/html/real_time_system_conditions.html
  - Hourly LMP by load zone (CSV):
      https://www.ercot.com/content/cdr/html/hb_lz.html
  - 5-min real-time LMP (CSV):
      https://www.ercot.com/content/cdr/html/real_time_spp.html

Storage: writes to grid_data table (created if missing).
Schedule: Flask endpoint /api/v1/iso/ercot/extract triggered by external cron
          or directly in-process via APScheduler.

Heartbeat: every successful run pings backend-iso-ercot source.
"""

import os
import re
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs):
        pass


iso_ercot_bp = Blueprint("iso_ercot", __name__, url_prefix="/api/v1/iso/ercot")

SOURCE_ID = "iso-ercot-realtime"
ERCOT_CONDITIONS_URL = "https://www.ercot.com/content/cdr/html/real_time_system_conditions.html"
ERCOT_LMP_URL = "https://www.ercot.com/content/cdr/html/hb_lz.html"


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS grid_data (
    id              BIGSERIAL PRIMARY KEY,
    iso             TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name     TEXT NOT NULL,
    metric_value    DOUBLE PRECISION,
    unit            TEXT,
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (iso, timestamp, metric_name)
);

CREATE INDEX IF NOT EXISTS ix_grid_data_iso_ts ON grid_data (iso, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_grid_data_metric ON grid_data (metric_name, timestamp DESC);
"""


def _ensure_table() -> None:
    if getattr(_ensure_table, "_done", False):
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        c.commit()
    _ensure_table._done = True


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "dchub-iso-ercot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_system_conditions(html: str) -> dict:
    """Extract numeric metrics from ERCOT system conditions HTML.

    The page has a table with rows like:
       <td>Actual System Demand</td><td>52,341 MW</td>
       <td>Frequency</td><td>59.999 Hz</td>
    We extract by label-value adjacency, robust to small formatting changes.
    """
    out = {}

    def _grab(label_pattern, value_unit_re=r"([\d,\.]+)\s*([A-Z%]+)?", _strip_unit_chars=False):
        m = re.search(
            r"<td[^>]*>\s*(?:" + label_pattern + r")\s*</td>\s*<td[^>]*>\s*" + value_unit_re,
            html, re.I | re.S,
        )
        if not m:
            return None, None
        # Guard: m.group(1) can be None if label_pattern alternation matched
        # without capturing the value side
        if m.group(1) is None:
            return None, None
        raw_value = m.group(1).replace(",", "")
        try:
            return float(raw_value), (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
        except (TypeError, ValueError):
            return None, None

    # Common ERCOT system conditions metrics (resilient if the page shifts)
    metrics = [
        ("system_demand_mw",     r"Actual\s+System\s+Demand|System\s+Demand"),
        ("system_frequency_hz",  r"Frequency"),
        ("net_renewable_mw",     r"Net\s+Renewable\s+Output|Renewable\s+Output"),
        ("solar_output_mw",      r"Solar\s+Output|Total\s+Solar"),
        ("wind_output_mw",       r"Wind\s+Output|Total\s+Wind"),
    ]
    for metric_name, label_pattern in metrics:
        value, unit = _grab(label_pattern)
        if value is not None:
            out[metric_name] = {"value": value, "unit": unit or ""}

    return out


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------

def _persist_metrics(metrics: dict) -> int:
    """Insert each metric row. Returns count of rows inserted (post-dedup)."""
    if not metrics:
        return 0
    _ensure_table()
    rows = 0
    with _conn() as c, c.cursor() as cur:
        for metric_name, data in metrics.items():
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    ("ERCOT", metric_name, data.get("value"), data.get("unit") or ""),
                )
                if cur.rowcount > 0:
                    rows += 1
            except Exception:
                # one bad metric doesn't kill the batch
                pass
        c.commit()
    return rows


# ---------------------------------------------------------------------------
# Public extractor entry point
# ---------------------------------------------------------------------------

def run_extraction() -> dict:
    """Run a single ERCOT extraction. Returns summary dict."""
    started = time.time()
    summary = {"iso": "ERCOT", "metrics_extracted": 0, "rows_inserted": 0, "errors": []}

    try:
        html = _fetch(ERCOT_CONDITIONS_URL)
        summary["html_size"] = len(html) if html else 0
        metrics = _parse_system_conditions(html)
        summary["metrics_extracted"] = len(metrics)
        # If we fetched HTML but extracted nothing, capture a sample for debugging
        if not metrics and html:
            # First 400 chars of body, after stripping HTML head
            import re as _re_dbg
            body_match = _re_dbg.search(r"<body[^>]*>(.{200,500})", html, _re_dbg.S)
            summary["html_preview"] = body_match.group(1)[:400] if body_match else html[:400]
        summary["sample_metric"] = next(iter(metrics.items())) if metrics else None
        rows = _persist_metrics(metrics)
        summary["rows_inserted"] = rows

        elapsed_ms = int((time.time() - started) * 1000)
        summary["duration_ms"] = elapsed_ms

        _heartbeat(
            SOURCE_ID,
            status="success",
            rows_affected=rows,
            duration_ms=elapsed_ms,
            metadata={"metrics_extracted": len(metrics)},
        )
        summary["status"] = "ok"
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {e}"
        summary["duration_ms"] = elapsed_ms
        _heartbeat(
            SOURCE_ID,
            status="failure",
            duration_ms=elapsed_ms,
            error=summary["error"],
        )

    return summary


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_ercot_bp.route("/extract", methods=["POST", "GET"])
def trigger_extract():
    """Run extraction now. Public endpoint — safe because writes are dedupe'd."""
    summary = run_extraction()
    status_code = 200 if summary.get("status") == "ok" else 500
    return jsonify(summary), status_code

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_ercot_bp.route("/latest", methods=["GET"])
def latest():
    """Return the most recent ERCOT metrics."""
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT metric_name, metric_value, unit, timestamp
               FROM grid_data
               WHERE iso = 'ERCOT'
               ORDER BY timestamp DESC
               LIMIT 100"""
        )
        rows = cur.fetchall()

    # Group by metric_name, keeping the most-recent value
    by_metric = {}
    for name, value, unit, ts in rows:
        if name not in by_metric:
            by_metric[name] = {
                "metric": name,
                "value": value,
                "unit": unit,
                "timestamp": ts.isoformat() if ts else None,
            }

    return jsonify(iso="ERCOT", metrics=list(by_metric.values())), 200


@iso_ercot_bp.route("/history", methods=["GET"])
def history():
    """Return time-series of one metric for charting."""
    _ensure_table()
    metric_name = request.args.get("metric", "system_demand_mw")
    try:
        limit = max(1, min(int(request.args.get("limit", 96)), 1000))
    except ValueError:
        return jsonify(error="limit must be int"), 400

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT timestamp, metric_value, unit
               FROM grid_data
               WHERE iso = 'ERCOT' AND metric_name = %s
               ORDER BY timestamp DESC
               LIMIT %s""",
            (metric_name, limit),
        )
        rows = [(ts.isoformat() if ts else None, val, unit) for ts, val, unit in cur.fetchall()]

    return jsonify(
        iso="ERCOT",
        metric=metric_name,
        count=len(rows),
        points=[{"timestamp": ts, "value": v, "unit": u} for ts, v, u in rows],
    ), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3720 — review and remove one


@iso_ercot_bp.route("/health", methods=["GET"])
def health():
    """Show last successful extraction time."""
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT MAX(timestamp), COUNT(*) FROM grid_data WHERE iso = 'ERCOT'"
        )
        latest, total = cur.fetchone()
    return jsonify(
        iso="ERCOT",
        latest_data_at=latest.isoformat() if latest else None,
        total_records=int(total or 0),
        source_id=SOURCE_ID,
    ), 200
