"""Phase HH (2026-05-13) — AESO (Alberta) real-time grid extractor.

Alberta Electric System Operator. ~12 GW peak. Major DC build-out
target: Crusoe, Hut 8, gas-flared-power hyperscale, cheap industrial
rates, cold-climate cooling efficiency.

AESO's ETS (Energy Trading System) publishes Current Supply Demand
Report CSV files. Some endpoints rotate session tokens, so the
fallback chain is intentionally broad.
"""
import time
from flask import Blueprint, jsonify
from routes._iso_common import (
    fetch_first_working, parse_json_numeric, parse_csv_numeric_columns,
    persist_metrics, latest_for_iso, health_for_iso,
)

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*a, **k): pass


iso_aeso_bp = Blueprint("iso_aeso", __name__, url_prefix="/api/v1/iso/aeso")
SOURCE_ID = "iso-aeso-realtime"


def _aeso_urls():
    return [
        # AESO ETS — Current Supply Demand report (CSV)
        "http://ets.aeso.ca/ets_web/ip/Market/Reports/CSDReportServlet?contentType=csv",
        "http://ets.aeso.ca/ets_web/ip/Market/Reports/CSDReportServlet",
        # AESO market data feeds (public, no auth)
        "https://api.aeso.ca/report/v1.1/realTimeShiftReportCsv",
        "https://www.aeso.ca/assets/Uploads/data-requests/Current-Supply-Demand.csv",
        # EIA EBA fallback — Canadian region not always covered but try
        "https://www.eia.gov/electricity/data/eia930/api/region/CAN/fuel-type-data",
    ]


def run_extraction():
    started = time.time()
    summary = {"iso": "AESO", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_aeso_urls(), ua="dchub-iso-aeso/1.0")
        summary["fetched_url"] = url
        summary["html_size"] = len(text)
        metrics = parse_csv_numeric_columns(text, prefix="fuel_")
        if not metrics:
            metrics = parse_json_numeric(text)
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["html_preview"] = text[:400]
        rows = persist_metrics("AESO", metrics)
        summary["rows_inserted"] = rows
        elapsed = int((time.time() - started) * 1000)
        summary["duration_ms"] = elapsed
        _heartbeat(SOURCE_ID, status="success", rows_affected=rows,
                   duration_ms=elapsed,
                   metadata={"metrics_extracted": len(metrics), "url": url})
        summary["status"] = "ok"
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {e}"
        summary["duration_ms"] = elapsed
        _heartbeat(SOURCE_ID, status="failure", duration_ms=elapsed,
                   error=summary["error"])
    return summary


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_aeso_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_aeso_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="AESO", metrics=latest_for_iso("AESO")), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3845 — review and remove one


@iso_aeso_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("AESO", SOURCE_ID)), 200
