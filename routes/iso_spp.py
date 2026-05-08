"""SPP (Southwest Power Pool) real-time grid extractor."""
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


iso_spp_bp = Blueprint("iso_spp", __name__, url_prefix="/api/v1/iso/spp")
SOURCE_ID = "iso-spp-realtime"

SPP_URLS = [
    "https://marketplace.spp.org/chart-api/fuel-mix-rtbm-genmix/asChart",
    "https://marketplace.spp.org/chart-api/fuel-mix-rtbm-genmix/asChart?type=json",
    "https://marketplace.spp.org/file-browser-api/download/rtbm-fuel-mix",
    "https://www.spp.org/Real-time-Market",
]


def run_extraction():
    started = time.time()
    summary = {"iso": "SPP", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(SPP_URLS, ua="dchub-iso-spp/1.0")
        summary["fetched_url"] = url
        summary["html_size"] = len(text)
        metrics = parse_json_numeric(text)
        if not metrics:
            metrics = parse_csv_numeric_columns(text, prefix="fuel_")
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["html_preview"] = text[:400]
        rows = persist_metrics("SPP", metrics)
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


@iso_spp_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)


@iso_spp_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="SPP", metrics=latest_for_iso("SPP")), 200


@iso_spp_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("SPP", SOURCE_ID)), 200
