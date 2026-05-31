"""
iso_caiso.py — California ISO real-time grid data extractor.

CAISO's published URL paths have shifted over the years. We try multiple
known endpoints in sequence and use the first that returns 200.
"""

import os
import csv
import io
import time
import urllib.request
import urllib.error
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, jsonify

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs): pass


iso_caiso_bp = Blueprint("iso_caiso", __name__, url_prefix="/api/v1/iso/caiso")
SOURCE_ID = "iso-caiso-realtime"

# Try in order — first success wins
CAISO_URLS = [
    "https://www.caiso.com/outlook/current/fuelsource.csv",
    "https://www.caiso.com/outlook/SP/fuelsource.csv",
    "https://www.caiso.com/outlook/sp/fuelsource.csv",
    "https://www.caiso.com/outlook/current/fuelmix.csv",
    "https://www.caiso.com/Documents/RealTimeGen.csv",
]


def _dsn(): return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _fetch_first_working(urls, timeout=20):
    """Try each URL; return (text, working_url) or raise last error."""
    last_err = None
    for url in urls:
        req = urllib.request.Request(url, headers={"User-Agent": "dchub-iso-caiso/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return resp.read().decode("utf-8", errors="replace"), url
        except urllib.error.HTTPError as e:
            last_err = f"{url}: HTTP {e.code}"
            continue
        except (urllib.error.URLError, OSError) as e:
            last_err = f"{url}: {type(e).__name__}: {e}"
            continue
    raise RuntimeError(f"all CAISO URLs failed; last_err: {last_err}")


def _parse_caiso_fuel_csv(csv_text):
    """CAISO fuel CSV: time + columns per fuel. Take the most recent row.

    Format varies; we accept whichever columns are present and create
    fuel_<name>_mw metrics for any numeric column we recognize.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows: return {}

    # Take the row with the latest timestamp (last row in chronological CSV)
    last = rows[-1]
    metrics = {}

    # All columns except known time/timestamp columns become potential metrics
    skip_cols = {"time", "timestamp", "Time", "Timestamp", "interval", "Interval",
                 "TIME", "INTERVAL", "datetime", "DateTime"}
    for col, val in last.items():
        if not col or col in skip_cols:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        # Normalize column name to metric_name
        clean = col.strip().lower().replace(" ", "_").replace("-", "_")
        # Drop non-alphanumeric except _
        clean = "".join(ch for ch in clean if ch.isalnum() or ch == "_")
        if clean and num != 0:
            metrics[f"fuel_{clean}_mw"] = {"value": num, "unit": "MW"}

    return metrics


def _persist_metrics(metrics):
    if not metrics: return 0
    rows = 0
    with _conn() as c, c.cursor() as cur:
        for name, data in metrics.items():
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    ("CAISO", name, data["value"], data.get("unit", "")),
                )
                if cur.rowcount > 0: rows += 1
            except Exception: pass
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {"iso": "CAISO", "metrics_extracted": 0, "rows_inserted": 0, "errors": []}
    try:
        csv_text, working_url = _fetch_first_working(CAISO_URLS)
        summary["fetched_url"] = working_url
        summary["html_size"] = len(csv_text)
        metrics = _parse_caiso_fuel_csv(csv_text)
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["html_preview"] = csv_text[:400]
        rows = _persist_metrics(metrics)
        summary["rows_inserted"] = rows
        elapsed_ms = int((time.time() - started) * 1000)
        summary["duration_ms"] = elapsed_ms
        _heartbeat(SOURCE_ID, status="success", rows_affected=rows,
                   duration_ms=elapsed_ms,
                   metadata={"metrics_extracted": len(metrics), "url": working_url})
        summary["status"] = "ok"
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {e}"
        summary["duration_ms"] = elapsed_ms
        _heartbeat(SOURCE_ID, status="failure", duration_ms=elapsed_ms, error=summary["error"])
    return summary


@iso_caiso_bp.route("/extract", methods=["POST", "GET"])
def trigger_extract():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)


@iso_caiso_bp.route("/latest", methods=["GET"])
def latest():
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT metric_name, metric_value, unit, timestamp
               FROM grid_data WHERE iso = 'CAISO'
               ORDER BY timestamp DESC LIMIT 100"""
        )
        rows = cur.fetchall()
    by_metric = {}
    for n, v, u, ts in rows:
        if n not in by_metric:
            by_metric[n] = {"metric": n, "value": v, "unit": u,
                            "timestamp": ts.isoformat() if ts else None}
    return jsonify(iso="CAISO", metrics=list(by_metric.values())), 200


# AUTO-REPAIR: duplicate route '/health' also in main.py:3845 — review and remove one
@iso_caiso_bp.route("/health", methods=["GET"])
def health():
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT MAX(timestamp), COUNT(*) FROM grid_data WHERE iso = 'CAISO'")
        latest, total = cur.fetchone()
    return jsonify(
        iso="CAISO",
        latest_data_at=latest.isoformat() if latest else None,
        total_records=int(total or 0),
        source_id=SOURCE_ID,
    ), 200
