"""
iso_caiso.py — California ISO real-time grid data extractor.
Pattern matches iso_ercot.py. Writes to shared grid_data table.

CAISO publishes fuel mix at:
  https://www.caiso.com/outlook/SP/fuelsource.csv
  (CSV with current generation by fuel type — solar, wind, gas, nuclear, etc.)

Plus current load at:
  https://www.caiso.com/outlook/SP/current_demand.csv (best effort — may differ)
"""

import os
import re
import csv
import io
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2 as _pg
from flask import Blueprint, jsonify, request

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs): pass


iso_caiso_bp = Blueprint("iso_caiso", __name__, url_prefix="/api/v1/iso/caiso")
SOURCE_ID = "iso-caiso-realtime"

CAISO_FUEL_URL = "https://www.caiso.com/outlook/SP/fuelsource.csv"
CAISO_DEMAND_URL = "https://www.caiso.com/outlook/SP/demand.csv"


def _dsn(): return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "dchub-iso-caiso/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_caiso_fuel_csv(csv_text):
    """CAISO fuelsource.csv: time + columns per fuel type. We take the most recent row."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return {}

    last = rows[-1]  # most recent row
    metrics = {}
    fuel_columns = ["Solar", "Wind", "Geothermal", "Biomass", "Biogas", "Small hydro",
                    "Coal", "Nuclear", "Natural gas", "Large hydro", "Batteries", "Imports", "Other"]
    for col in fuel_columns:
        for variant in (col, col.lower(), col.replace(" ", "_").lower(), col.upper()):
            if variant in last:
                try:
                    val = float(last[variant])
                    metric_name = "fuel_" + col.lower().replace(" ", "_") + "_mw"
                    metrics[metric_name] = {"value": val, "unit": "MW"}
                    break
                except (TypeError, ValueError):
                    pass
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
        csv_text = _fetch(CAISO_FUEL_URL)
        summary["html_size"] = len(csv_text)
        metrics = _parse_caiso_fuel_csv(csv_text)
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
            by_metric[n] = {"metric": n, "value": v, "unit": u, "timestamp": ts.isoformat() if ts else None}
    return jsonify(iso="CAISO", metrics=list(by_metric.values())), 200


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
