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
    """Phase QQ+9 (2026-05-13): reordered URLs to put the fast .aspx
    (~13KB) endpoint first, .txt (~78KB) second. Previously the
    orchestrator was hitting total_budget_exceeded on BPA at 11.5s/12s
    — the first two URLs are both 78KB .txt files that take ~6s each
    over Railway's outbound network. The .aspx variant is the same
    data in a smaller format; serving it first means BPA completes in
    ~3s on a fresh connection.

    Also injects the EIA API key when EIA_API_KEY env var is set so the
    api.eia.gov v2 fallback can actually return data (it returns 403 to
    keyless callers).
    """
    import os
    eia_key = os.environ.get("EIA_API_KEY", "")
    return [
        # FAST first — same data, smaller payload (~13KB vs 78KB)
        "https://transmission.bpa.gov/business/operations/Wind/baltwg.aspx?format=txt",
        # Larger raw text fallback
        "https://transmission.bpa.gov/business/operations/wind/baltwg.txt",
        # EIA v2 API with key when configured (otherwise 403 → fall through)
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?api_key={eia_key}&frequency=hourly&data[0]=value&facets[respondent][]=BPAT&sort[0][column]=period&sort[0][direction]=desc&length=12",
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
