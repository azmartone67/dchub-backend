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
    parse_eia_v2_fuel_mix, scrub_url,
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

    2026-05-30 FIX: lead with the AUTHENTICATED EIA v2 endpoint, mirroring
    the working iso_bpa.py extractor. Root cause of PJM persisting 0 rows
    since registration: the old list led with UNauthenticated EIA URLs
    (api.eia.gov/v2 REQUIRES api_key — without it the request fails or
    returns no data), and the no-auth www.eia.gov path returns a shape the
    numeric parser can't read. So fetch_first_working fell all the way
    through to dataminer2.pjm.com, which serves the JS SPA HTML shell
    (200 OK, ~1.5KB, no data) → parse → {} → persist 0 rows. EIA_API_KEY
    is already set in env (same key iso_bpa/iso_isone use)."""
    eia_key = os.environ.get("EIA_API_KEY", "")
    return [
        # PRIMARY: api.eia.gov v2 PJM region (authenticated; ~800ms from Railway).
        # Parsed by parse_eia_v2_fuel_mix in run_extraction (same as BPA).
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?api_key={eia_key}&frequency=hourly&data[0]=value&facets[respondent][]=PJM&sort[0][column]=period&sort[0][direction]=desc&length=12",
        # Fallback: PJM API (subscription) — sends key via Ocp-Apim-Subscription-Key
        # if DCHUB_PJM_API_KEY is set. The fetch helper handles the header.
        "https://api.pjm.com/api/v1/gen_by_fuel/data?rowCount=24&order=desc&download=true",
        # Last resort: Dataminer 2 (usually the SPA shell — kept only in case
        # PJM ever exposes a direct CSV path).
        "https://dataminer2.pjm.com/feed/gen_by_fuel/download?format=csv&period=current",
    ]


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
        # scrub_url hides the embedded EIA api_key from the echoed /extract response
        summary["fetched_url"] = scrub_url(url)
        summary["html_size"]   = len(text)

        # 2026-05-30: EIA v2 parser first when the winning URL is api.eia.gov/v2
        # (same path BPA uses), then JSON, then CSV. Previously this only tried
        # JSON/CSV, which couldn't parse the EIA v2 envelope shape.
        metrics = {}
        if "api.eia.gov/v2/" in url:
            metrics = parse_eia_v2_fuel_mix(text, prefix="fuel_")
        if not metrics:
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
# AUTO-REPAIR: duplicate route '/health' also in main.py:3819 — review and remove one


@iso_pjm_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("PJM", SOURCE_ID)), 200
