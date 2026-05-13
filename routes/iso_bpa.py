"""Phase HH (2026-05-13) — BPA (Bonneville Power Administration) extractor.

Federal Power Marketing Administration covering Pacific Northwest
(OR / WA / ID / W. MT). ~12 GW typical load, 80%+ hydro. Major
data-center corridor: Wenatchee, Quincy, Hermiston (Microsoft, Meta,
Amazon, Google all have hyperscale sites here — cheap hydro power).

BPA publishes balancing-authority data via public text files (their
own URL is more reliable than the EIA mirror, but EIA serves as
fallback).
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


iso_bpa_bp = Blueprint("iso_bpa", __name__, url_prefix="/api/v1/iso/bpa")
SOURCE_ID = "iso-bpa-realtime"


def _bpa_urls():
    return [
        # BPA's own real-time balancing authority text file
        "https://transmission.bpa.gov/business/operations/wind/baltwg.txt",
        # BPA generation by fuel type (current day)
        "https://transmission.bpa.gov/business/operations/Wind/baltwg.aspx?format=txt",
        # EIA EBA — BPAT region (Bonneville Power's BA code)
        "https://www.eia.gov/electricity/data/eia930/api/region/BPAT/fuel-type-data",
        # Alt EIA v2 API path
        "https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?frequency=hourly&data[0]=value&facets[respondent][]=BPAT&sort[0][column]=period&sort[0][direction]=desc&offset=0&length=12",
    ]


def run_extraction():
    started = time.time()
    summary = {"iso": "BPA", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_bpa_urls(), ua="dchub-iso-bpa/1.0")
        summary["fetched_url"] = url
        summary["html_size"] = len(text)
        # BPA's txt file is tab-separated with a header section; CSV parser
        # handles either layout via the prefix.
        metrics = parse_csv_numeric_columns(text, prefix="fuel_")
        if not metrics:
            metrics = parse_json_numeric(text)
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["html_preview"] = text[:400]
        rows = persist_metrics("BPA", metrics)
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


@iso_bpa_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)


@iso_bpa_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="BPA", metrics=latest_for_iso("BPA")), 200


@iso_bpa_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("BPA", SOURCE_ID)), 200
