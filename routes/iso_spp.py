"""SPP (Southwest Power Pool) real-time grid extractor."""
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


iso_spp_bp = Blueprint("iso_spp", __name__, url_prefix="/api/v1/iso/spp")
SOURCE_ID = "iso-spp-realtime"

# Phase QQ+9 (2026-05-13): probed SPP's public endpoints live. The old
# portal.spp.org/file-browser-api/... paths now 404 (SPP rotated). The
# new marketplace.spp.org/chart-api/gen-mix/asChart returns 200 with
# real JSON ~4KB per probe. portal.spp.org PublicAPI paths all serve
# the React SPA shell (609 bytes of HTML, not data).
def _spp_urls():
    import os
    eia_key = os.environ.get("EIA_API_KEY", "")
    return [
        # Marketplace chart-api — CONFIRMED working 2026-05-13 (200, ~4KB JSON)
        "https://marketplace.spp.org/chart-api/gen-mix/asChart",
        "https://marketplace.spp.org/chart-api/gen-mix/asChart?type=json",
        # Same chart-api with the fuel-mix-rtbm slug as alt
        "https://marketplace.spp.org/chart-api/fuel-mix-rtbm-genmix/asChart",
        # EIA v2 API with key (works when EIA_API_KEY is set on Railway)
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?api_key={eia_key}&frequency=hourly&data[0]=value&facets[respondent][]=SWPP&sort[0][column]=period&sort[0][direction]=desc&length=12",
    ]

SPP_URLS = _spp_urls()  # kept for back-compat; call _spp_urls() directly to pick up env changes


def run_extraction():
    started = time.time()
    summary = {"iso": "SPP", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_spp_urls(), ua="dchub-iso-spp/1.0")
        # Phase QQ+10: scrub api_key from echoed URL
        summary["fetched_url"] = scrub_url(url)
        summary["html_size"] = len(text)
        # Phase QQ+10: EIA v2 parser when URL is api.eia.gov v2
        metrics = {}
        if "api.eia.gov/v2/" in url:
            metrics = parse_eia_v2_fuel_mix(text, prefix="fuel_")
        if not metrics:
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


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_spp_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_spp_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="SPP", metrics=latest_for_iso("SPP")), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3528 — review and remove one


@iso_spp_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("SPP", SOURCE_ID)), 200
