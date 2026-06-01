"""MISO real-time grid extractor.

2026-05-31 FIX (#100, ISO coverage expansion): repointed to the authenticated
EIA-930 balancing-authority feed, mirroring the proven iso_pjm.py / iso_bpa.py
fix and the 43 utility BAs in eia_utility_bas.py.

ROOT CAUSE of MISO persisting 0 rows in grid_data: the old URL list led with
api.misoenergy.org/MISORTWDDataBroker (the public "RTWD Data Broker"). That
endpoint now returns HTTP 200 with body {"error": "no data", "See":
".../rtdataapis"} — MISO retired/gated that public real-time feed. The numeric
parser found no usable numbers → 0 metrics → 0 rows. The misoenergy.org
fallbacks 301-redirect (urllib doesn't follow redirects by default), so the
loop counted them all as failures. There was NO EIA fallback, so the extractor
had no working path at all.

FIX: lead with api.eia.gov/v2 fuel-type-data, respondent=MISO (EIA's region
code for the Midcontinent ISO is literally "MISO"). EIA-930 is the Hourly
Electric Grid Monitor — the SAME authed feed that powers our working PJM/BPA
extractors. EIA_API_KEY is already set in Railway env. The misoenergy.org URLs
are kept only as last-resort fallbacks in case MISO ever re-opens that feed.
"""
import os
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


iso_miso_bp = Blueprint("iso_miso", __name__, url_prefix="/api/v1/iso/miso")
SOURCE_ID = "iso-miso-realtime"


def _miso_urls():
    """Ordered URL list — first working response wins.

    PRIMARY is the authenticated EIA v2 endpoint (respondent=MISO), the same
    path/parser that fixed PJM and BPA (~800ms from Railway). The two
    api.misoenergy.org Data Broker URLs are demoted to last-resort fallbacks:
    as of 2026-05-31 they return {"error": "no data"} (the public RTWD feed was
    retired), but we keep them so MISO auto-recovers if EIA upstream is down AND
    MISO ever re-opens the broker."""
    eia_key = os.environ.get("EIA_API_KEY", "")
    return [
        # PRIMARY: api.eia.gov v2 MISO region (authenticated; parsed by
        # parse_eia_v2_fuel_mix in run_extraction, same as PJM/BPA).
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?api_key={eia_key}"
        f"&frequency=hourly&data[0]=value&facets[respondent][]=MISO"
        f"&sort[0][column]=period&sort[0][direction]=desc&length=12",
        # Fallback 1/2: MISO public Data Broker (currently "no data" — kept in
        # case MISO restores the public feed).
        "https://api.misoenergy.org/MISORTWDDataBroker/DataBrokerServices.asmx?messageType=getfuelmix&returnType=json",
        "https://api.misoenergy.org/MISORTWDDataBroker/DataBrokerServices.asmx?messageType=getrealtimegenmix&returnType=json",
    ]


def run_extraction():
    started = time.time()
    summary = {"iso": "MISO", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_miso_urls(), ua="dchub-iso-miso/1.0")
        # scrub_url hides the embedded EIA api_key from the echoed /extract response
        summary["fetched_url"] = scrub_url(url)
        summary["html_size"] = len(text)
        # EIA v2 parser first when the winning URL is api.eia.gov/v2 (same path
        # PJM/BPA use), then generic JSON, then CSV for the legacy broker shape.
        metrics = {}
        if "api.eia.gov/v2/" in url:
            metrics = parse_eia_v2_fuel_mix(text, prefix="fuel_")
        if not metrics:
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
# AUTO-REPAIR: duplicate route '/health' also in main.py:3871 — review and remove one


@iso_miso_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("MISO", SOURCE_ID)), 200
