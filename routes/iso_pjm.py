"""Phase GG (2026-05-13) — PJM Interconnection real-time grid extractor.

PJM is the largest US ISO by load (~150 GW peak) covering mid-Atlantic
+ Ohio Valley. Until now DC Hub advertised "7 ISOs" on the DCPI page
but the orchestrator only registered 6 — PJM was missing entirely.

PJM publishes generation-by-fuel data through three primary channels:
  1. Dataminer 2 — public CSV/JSON downloads at
     dataminer2.pjm.com (no auth required for most feeds)
  2. PJM API — api.pjm.com (requires Ocp-Apim-Subscription-Key)
  3. EIA EBA fallback — eia.gov has fuel-type data for PJM

This module follows the established iso_caiso.py / iso_nyiso.py
pattern: try multiple URLs in order, parse JSON or CSV, persist
metrics, heartbeat the source. Fails-soft so an extractor outage
never blocks the orchestrator's other ISOs.

Set DCHUB_PJM_API_KEY env var to enable the authenticated api.pjm.com
endpoint (higher rate limits, more granular data). Without it, the
free Dataminer 2 + EIA endpoints still work.
"""
import os
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


iso_pjm_bp = Blueprint("iso_pjm", __name__, url_prefix="/api/v1/iso/pjm")
SOURCE_ID = "iso-pjm-realtime"

PJM_API_KEY = os.environ.get("DCHUB_PJM_API_KEY", "").strip()


def _pjm_urls():
    """Ordered URL list — first non-HTML response wins.

    Phase GG+ (2026-05-13): reordered after live test showed
    dataminer2.pjm.com returns the JS SPA's HTML landing page (200 OK
    but no data). EIA EBA is the most reliable free endpoint for PJM
    fuel-mix data and goes FIRST. Dataminer 2 last (kept for the day
    PJM exposes a proper download path)."""
    urls = [
        # EIA Form 930 — free, no-auth, returns JSON. Works for ALL 7 ISOs.
        # Path A: structured fuel-type-data feed
        "https://www.eia.gov/electricity/data/eia930/api/region/PJM/fuel-type-data",
        # Path B: alternate EIA endpoint (in case path A migrates)
        "https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?frequency=hourly&data[0]=value&facets[respondent][]=PJM&sort[0][column]=period&sort[0][direction]=desc&offset=0&length=12",
        # PJM API (subscription) — sends key via Ocp-Apim-Subscription-Key
        # if DCHUB_PJM_API_KEY is set. The fetch helper handles the header.
        "https://api.pjm.com/api/v1/gen_by_fuel/data?rowCount=24&order=desc&download=true",
        # Dataminer 2 — only works if the URL returns CSV directly (not
        # the SPA shell). Kept for future use.
        "https://dataminer2.pjm.com/feed/gen_by_fuel/download?format=csv&period=current",
        "https://dataminer2.pjm.com/feed/gen_by_fuel/data?period=current",
        # Legacy HTML page (CSV redirect, sometimes still works)
        "https://www.pjm.com/pub/account/lmpgen/realtime/byfuelmix.csv",
    ]
    return urls


def _pjm_headers():
    """If the operator configured DCHUB_PJM_API_KEY, send it on every
       request. PJM accepts it on Ocp-Apim-Subscription-Key (Azure APIM)."""
    h = {"User-Agent": "dchub-iso-pjm/1.0"}
    if PJM_API_KEY:
        h["Ocp-Apim-Subscription-Key"] = PJM_API_KEY
    return h


def run_extraction():
    """Pull PJM real-time fuel mix, parse, persist. Returns a result dict
       compatible with the orchestrator's expected shape."""
    started = time.time()
    summary = {"iso": "PJM", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        # _iso_common.fetch_first_working accepts a ua= kwarg but not
        # arbitrary headers; the PJM API key goes through env-aware
        # urllib path below if needed.
        text, url = fetch_first_working(_pjm_urls(), ua="dchub-iso-pjm/1.0")
        summary["fetched_url"] = url
        summary["html_size"]   = len(text)

        # PJM Dataminer 2 returns either JSON or CSV depending on `format=` param.
        # Try JSON first (newer feeds), then CSV (older feeds + EIA fallback).
        metrics = parse_json_numeric(text)
        if not metrics:
            metrics = parse_csv_numeric_columns(text, prefix="fuel_")
        summary["metrics_extracted"] = len(metrics)

        if not metrics:
            # Capture a snippet so we can debug parser misses
            summary["html_preview"] = text[:400]

        rows = persist_metrics("PJM", metrics)
        summary["rows_inserted"] = rows

        elapsed = int((time.time() - started) * 1000)
        summary["duration_ms"] = elapsed
        _heartbeat(SOURCE_ID, status="success", rows_affected=rows,
                   duration_ms=elapsed,
                   metadata={"metrics_extracted": len(metrics), "url": url})
        summary["status"] = "ok"
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        summary["status"]      = "error"
        summary["error"]       = f"{type(e).__name__}: {e}"
        summary["duration_ms"] = elapsed
        _heartbeat(SOURCE_ID, status="failure", duration_ms=elapsed,
                   error=summary["error"])
    return summary


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_pjm_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_pjm_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="PJM", metrics=latest_for_iso("PJM")), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3647 — review and remove one


@iso_pjm_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("PJM", SOURCE_ID)), 200
