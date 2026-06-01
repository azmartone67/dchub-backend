"""Phase HH (2026-05-13) — TVA (Tennessee Valley Authority) extractor.

Vertically-integrated federal utility, NOT an ISO/RTO — but covers
~33 GW peak across TN + parts of AL/MS/KY/GA/VA/NC. Major data-center
build-out: Google, Meta, Microsoft sites in TN; Nashville is a
top-15 US DC market.

TVA itself publishes limited public real-time data, so the primary
data source here is EIA's Form 930 (EBA) which tracks balancing-
authority hourly fuel-mix for TVA. EIA EBA = free, no auth, JSON.
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


iso_tva_bp = Blueprint("iso_tva", __name__, url_prefix="/api/v1/iso/tva")
SOURCE_ID = "iso-tva-realtime"


def _tva_urls():
    """Phase QQ+9 (2026-05-13): refreshed URL list. Probed each candidate
    live:
      www.eia.gov/electricity/data/eia930/...  → 301/503 (legacy mirror dead)
      www.tva.com/api/Power/UpdateInsightsData → 403 (now blocked)
      www.tva.com/api/cdn/totalcurrentpower... → 403 (blocked)
      api.eia.gov/v2/...?api_key=...            → 200 JSON when EIA_API_KEY env set

    The api.eia.gov v2 endpoint is the canonical source for TVA fuel mix
    via the EBA region 'TVA'. Requires a free API key from eia.gov; set
    EIA_API_KEY env var on Railway. Without the key the endpoint returns
    403 and we fall through, which is fine — the orchestrator just
    reports 0 rows for TVA.
    """
    import os
    eia_key = os.environ.get("EIA_API_KEY", "")
    return [
        # EIA v2 — primary (api_key required; otherwise 403 → next URL)
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?api_key={eia_key}&frequency=hourly&data[0]=value&facets[respondent][]=TVA&sort[0][column]=period&sort[0][direction]=desc&length=12",
        # EIA v2 region-data variant (different aggregation)
        f"https://api.eia.gov/v2/electricity/rto/region-data/data/?api_key={eia_key}&frequency=hourly&data[0]=value&facets[respondent][]=TVA&sort[0][column]=period&sort[0][direction]=desc&length=24",
        # Legacy EIA mirror — keeps returning 301/503 in 2026 but harmless to try last
        "https://www.eia.gov/electricity/data/eia930/api/region/TVA/fuel-type-data",
    ]


def run_extraction():
    started = time.time()
    summary = {"iso": "TVA", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_tva_urls(), ua="dchub-iso-tva/1.0")
        # Phase QQ+10: scrub api_key from echoed URL before storing
        summary["fetched_url"] = scrub_url(url)
        summary["html_size"] = len(text)
        # Phase QQ+10: try the EIA v2 fuel-mix parser FIRST when the URL
        # is an api.eia.gov v2 endpoint. The generic parse_json_numeric
        # walks the whole tree and emits zeros for EIA v2's nested
        # `response.data[]` shape.
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
        rows = persist_metrics("TVA", metrics)
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
@iso_tva_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_tva_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="TVA", metrics=latest_for_iso("TVA")), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3871 — review and remove one


@iso_tva_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("TVA", SOURCE_ID)), 200
