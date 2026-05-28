"""MISO real-time grid extractor."""
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


iso_miso_bp = Blueprint("iso_miso", __name__, url_prefix="/api/v1/iso/miso")
SOURCE_ID = "iso-miso-realtime"

MISO_URLS = [
    "https://api.misoenergy.org/MISORTWDDataBroker/DataBrokerServices.asmx?messageType=getfuelmix&returnType=json",
    "https://api.misoenergy.org/MISORTWDDataBroker/DataBrokerServices.asmx?messageType=getrealtimegenmix&returnType=json",
    "https://misoenergy.org/markets-and-operations/RTDF/json",
]


def run_extraction():
    started = time.time()
    summary = {"iso": "MISO", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(MISO_URLS, ua="dchub-iso-miso/1.0")
        summary["fetched_url"] = url
        summary["html_size"] = len(text)
        # Try JSON first; fall back to CSV
        metrics = parse_json_numeric(text, key_path="Fuel.Type")
        if not metrics:
            metrics = parse_json_numeric(text)
        if not metrics:
            metrics = parse_csv_numeric_columns(text, prefix="fuel_")
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["html_preview"] = text[:400]
        rows = persist_metrics("MISO", metrics)
        summary["rows_inserted"] = rows
        elapsed = int((time.time() - started) * 1000)
        summary["duration_ms"] = elapsed
        _heartbeat(SOURCE_ID, status="success", rows_affected=rows, duration_ms=elapsed,
                   metadata={"metrics_extracted": len(metrics), "url": url})
        summary["status"] = "ok"
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {e}"
        summary["duration_ms"] = elapsed
        _heartbeat(SOURCE_ID, status="failure", duration_ms=elapsed, error=summary["error"])
    return summary


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_miso_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_miso_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="MISO", metrics=latest_for_iso("MISO")), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3712 — review and remove one


@iso_miso_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("MISO", SOURCE_ID)), 200
