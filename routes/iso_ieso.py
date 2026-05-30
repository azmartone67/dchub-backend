"""Phase HH (2026-05-13) — IESO (Ontario) real-time grid extractor.

Independent Electricity System Operator for Ontario, Canada. Covers
~25 GW peak across Toronto / Mississauga / Markham / Ottawa — top-10
global data-center market with rapid AI build-out.

IESO publishes hourly generation-by-fuel CSVs at reports.ieso.ca
with no auth required. Fallback to EIA EBA's Canada region data
(less granular but always available).

Follows the established iso_pjm.py pattern.
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


iso_ieso_bp = Blueprint("iso_ieso", __name__, url_prefix="/api/v1/iso/ieso")
SOURCE_ID = "iso-ieso-realtime"


def _ieso_urls():
    """Ordered URL list — first working response wins. IESO publishes
       these CSVs hourly with predictable filenames.

       Phase QQ+9 (2026-05-13): switched http:// → https://. Direct probe
       confirmed every http:// URL returns a 302 redirect to the https://
       version; urllib doesn't follow these by default so the loop counted
       them all as failures and the orchestrator reported IESO dead. The
       https:// hostname returns 200 + 5460 bytes of real CSV in ~620ms.
    """
    return [
        # IESO public reports — hourly generation by fuel type
        "https://reports.ieso.ca/public/GenOutputbyFuelHourly/PUB_GenOutputbyFuelHourly.csv",
        # Day-ahead market summary (alternative)
        "https://reports.ieso.ca/public/RealtimeMktTotals/PUB_RealtimeMktTotals.csv",
        # IESO Dataset 7 — Hourly Generator Output and Capability
        "https://reports.ieso.ca/public/GenOutputCapability/PUB_GenOutputCapability.csv",
        # Adequacy report (rolling 7 days)
        "https://reports.ieso.ca/public/Adequacy/PUB_Adequacy.csv",
    ]


def run_extraction():
    started = time.time()
    summary = {"iso": "IESO", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_ieso_urls(), ua="dchub-iso-ieso/1.0")
        summary["fetched_url"] = url
        summary["html_size"] = len(text)
        # IESO CSVs have header comments before the data table — strip them
        # by finding the first line that starts with a fuel-type column name
        lines = text.splitlines()
        header_idx = 0
        for i, line in enumerate(lines):
            if any(kw in line.upper() for kw in ("FUEL TYPE", "NUCLEAR", "GAS", "HOUR")):
                header_idx = i
                break
        data_text = "\n".join(lines[header_idx:])
        metrics = parse_csv_numeric_columns(data_text, prefix="fuel_")
        if not metrics:
            metrics = parse_json_numeric(text)
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["html_preview"] = text[:400]
        rows = persist_metrics("IESO", metrics)
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
@iso_ieso_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_ieso_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="IESO", metrics=latest_for_iso("IESO")), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3819 — review and remove one


@iso_ieso_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("IESO", SOURCE_ID)), 200
