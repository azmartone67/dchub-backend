"""ISO New England real-time grid extractor."""
import time
from datetime import datetime, timezone
from flask import Blueprint, jsonify
from routes._iso_common import (
    fetch_first_working, parse_json_numeric, parse_csv_numeric_columns,
    persist_metrics, latest_for_iso, health_for_iso,
)

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*a, **k): pass


iso_isone_bp = Blueprint("iso_isone", __name__, url_prefix="/api/v1/iso/isone")
SOURCE_ID = "iso-isone-realtime"


def _today():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _yesterday():
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")


def _isone_urls():
    today = _today()
    yest = _yesterday()
    return [
        f"https://www.iso-ne.com/transform/csv/genfuelmix?start={today}",
        f"https://www.iso-ne.com/transform/csv/genfuelmix?start={yest}",
        "https://www.iso-ne.com/static-assets/documents/2024/01/genfuelmix.csv",
        "https://www.iso-ne.com/transform/csv/sysload?start=" + today,
    ]


def run_extraction():
    started = time.time()
    summary = {"iso": "ISONE", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_isone_urls(), ua="dchub-iso-isone/1.0")
        summary["fetched_url"] = url
        summary["html_size"] = len(text)
        metrics = parse_csv_numeric_columns(text, prefix="fuel_")
        if not metrics:
            metrics = parse_json_numeric(text)
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["html_preview"] = text[:400]
        rows = persist_metrics("ISONE", metrics)
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


@iso_isone_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)


@iso_isone_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="ISONE", metrics=latest_for_iso("ISONE")), 200


@iso_isone_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("ISONE", SOURCE_ID)), 200
