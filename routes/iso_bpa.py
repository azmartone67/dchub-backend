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
    parse_eia_v2_fuel_mix, scrub_url,
    persist_metrics, latest_for_iso, health_for_iso,
)

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*a, **k): pass


iso_bpa_bp = Blueprint("iso_bpa", __name__, url_prefix="/api/v1/iso/bpa")
SOURCE_ID = "iso-bpa-realtime"


def _bpa_urls():
    """Phase QQ+11 (2026-05-13): reordered AGAIN. Live orchestrator
    probe (post-QQ+9) shows transmission.bpa.gov takes ~6s/URL from
    Railway's us-west2 region — even the small .aspx variant. Two
    consecutive BPA URLs eat the 12s budget. Meanwhile api.eia.gov v2
    responds in ~800ms (verified via TVA + ISONE on the same Railway
    region). Now that EIA_API_KEY is set in env, leading with EIA v2
    gives BPA the same first-attempt success TVA/ISONE get.

    Fallbacks remain transmission.bpa.gov in case EIA upstream goes
    down — but the primary path is now EIA.
    """
    import os
    eia_key = os.environ.get("EIA_API_KEY", "")
    return [
        # PRIMARY: api.eia.gov v2 BPAT region (fast from Railway, ~800ms)
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?api_key={eia_key}&frequency=hourly&data[0]=value&facets[respondent][]=BPAT&sort[0][column]=period&sort[0][direction]=desc&length=12",
        # Fallback 1: small .aspx variant (~13KB)
        "https://transmission.bpa.gov/business/operations/Wind/baltwg.aspx?format=txt",
        # Fallback 2: larger raw text
        "https://transmission.bpa.gov/business/operations/wind/baltwg.txt",
    ]


def run_extraction():
    started = time.time()
    summary = {"iso": "BPA", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_bpa_urls(), ua="dchub-iso-bpa/1.0")
        # Phase QQ+10: scrub api_key from echoed URL
        summary["fetched_url"] = scrub_url(url)
        summary["html_size"] = len(text)
        # Phase QQ+10: EIA v2 parser when URL is api.eia.gov v2
        metrics = {}
        if "api.eia.gov/v2/" in url:
            metrics = parse_eia_v2_fuel_mix(text, prefix="fuel_")
        if not metrics:
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


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_bpa_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_bpa_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="BPA", metrics=latest_for_iso("BPA")), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3855 — review and remove one


@iso_bpa_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("BPA", SOURCE_ID)), 200
