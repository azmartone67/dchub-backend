"""
iso_nyiso.py — New York ISO real-time grid data extractor.

NYISO publishes daily aggregated CSV at:
  http://mis.nyiso.com/public/csv/realtime/YYYYMMDDrealtime_zone.csv
  (zonal real-time pricing + flow)

Plus current load:
  http://mis.nyiso.com/public/csv/pal/YYYYMMDDpal.csv
  (palindrome — actual load)
"""

import os
import csv
import io
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import psycopg2 as _pg
from flask import Blueprint, jsonify, request

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs): pass


iso_nyiso_bp = Blueprint("iso_nyiso", __name__, url_prefix="/api/v1/iso/nyiso")
SOURCE_ID = "iso-nyiso-realtime"

def _today_str():
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def _yesterday_str():
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")

NYISO_PAL_URL_TEMPLATE = "http://mis.nyiso.com/public/csv/pal/{}pal.csv"


def _dsn(): return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "dchub-iso-nyiso/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_nyiso_pal_csv(csv_text):
    """NYISO PAL (Palindrome) CSV: per-zone load, multiple rows per timestamp.
    We aggregate by zone and take the most recent timestamp."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows: return {}

    # Find latest timestamp
    latest_ts = None
    for r in rows:
        ts = r.get("Time Stamp") or r.get("Timestamp") or r.get("time_stamp")
        if ts:
            latest_ts = ts  # last wins (assuming sorted)

    metrics = {}
    if latest_ts:
        for r in rows:
            r_ts = r.get("Time Stamp") or r.get("Timestamp") or r.get("time_stamp")
            if r_ts != latest_ts: continue
            zone = r.get("Name") or r.get("Zone") or r.get("zone")
            try:
                load_mw = float(r.get("Load") or r.get("load") or r.get("Integrated Load") or 0)
            except (TypeError, ValueError):
                continue
            if zone and load_mw > 0:
                clean_zone = zone.replace(" ", "_").lower()[:30]
                metrics[f"zone_{clean_zone}_mw"] = {"value": load_mw, "unit": "MW"}

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
                    ("NYISO", name, data["value"], data.get("unit", "")),
                )
                if cur.rowcount > 0: rows += 1
            except Exception: pass
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {"iso": "NYISO", "metrics_extracted": 0, "rows_inserted": 0, "errors": []}

    # Try today's CSV first, then yesterday's (NYISO can lag near midnight UTC)
    csv_text = None
    last_err = None
    for date_str in (_today_str(), _yesterday_str()):
        url = NYISO_PAL_URL_TEMPLATE.format(date_str)
        try:
            csv_text = _fetch(url)
            summary["fetched_url"] = url
            break
        except Exception as e:
            last_err = e

    try:
        if csv_text is None:
            raise RuntimeError(f"Both today and yesterday CSV unreachable: {last_err}")
        summary["html_size"] = len(csv_text)
        metrics = _parse_nyiso_pal_csv(csv_text)
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["html_preview"] = csv_text[:400]
        rows = _persist_metrics(metrics)
        summary["rows_inserted"] = rows
        elapsed_ms = int((time.time() - started) * 1000)
        summary["duration_ms"] = elapsed_ms
        _heartbeat(SOURCE_ID, status="success", rows_affected=rows, duration_ms=elapsed_ms,
                   metadata={"metrics_extracted": len(metrics)})
        summary["status"] = "ok"
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {e}"
        summary["duration_ms"] = elapsed_ms
        _heartbeat(SOURCE_ID, status="failure", duration_ms=elapsed_ms, error=summary["error"])
    return summary


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_nyiso_bp.route("/extract", methods=["POST", "GET"])
def trigger_extract():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_nyiso_bp.route("/latest", methods=["GET"])
def latest():
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT metric_name, metric_value, unit, timestamp
               FROM grid_data WHERE iso = 'NYISO'
               ORDER BY timestamp DESC LIMIT 100"""
        )
        rows = cur.fetchall()
    by_metric = {}
    for n, v, u, ts in rows:
        if n not in by_metric:
            by_metric[n] = {"metric": n, "value": v, "unit": u, "timestamp": ts.isoformat() if ts else None}
    return jsonify(iso="NYISO", metrics=list(by_metric.values())), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3819 — review and remove one


@iso_nyiso_bp.route("/health", methods=["GET"])
def health():
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT MAX(timestamp), COUNT(*) FROM grid_data WHERE iso = 'NYISO'")
        latest, total = cur.fetchone()
    return jsonify(
        iso="NYISO",
        latest_data_at=latest.isoformat() if latest else None,
        total_records=int(total or 0),
        source_id=SOURCE_ID,
    ), 200
