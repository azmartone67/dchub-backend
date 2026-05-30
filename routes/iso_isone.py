"""ISO New England real-time grid extractor."""
import time
from datetime import datetime, timezone
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


iso_isone_bp = Blueprint("iso_isone", __name__, url_prefix="/api/v1/iso/isone")
SOURCE_ID = "iso-isone-realtime"


def _today():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _yesterday():
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")


def _isone_urls():
    """Phase QQ+9 (2026-05-13): re-probed every URL live. Findings:
      webservices.iso-ne.com/api/v1.1/genfuelmix/current.json → 401
          (endpoint EXISTS but requires HTTP Basic auth; ISO-NE
          requires free registration to get credentials)
      www.iso-ne.com/ws/wsclient                              → 500
      www.iso-ne.com/transform/csv/genfuelmix                 → 403
      www.iso-ne.com/static-assets/.../*.json                 → 404
      www.eia.gov/electricity/data/eia930/...                 → 301/503
      api.eia.gov/v2/...?api_key=$EIA_API_KEY                 → 200 JSON ✓

    Primary is api.eia.gov v2 (needs EIA_API_KEY env var). If the user
    also sets ISONE_USERNAME + ISONE_PASSWORD env vars (free reg on
    iso-ne.com), we hit webservices.iso-ne.com FIRST with Basic auth —
    that gives 5-min granular data vs EIA's hourly. Without either,
    ISONE returns 0 metrics gracefully.
    """
    import os
    today = _today()
    yest  = _yesterday()
    eia_key = os.environ.get("EIA_API_KEY", "")
    isone_user = os.environ.get("ISONE_USERNAME", "")
    isone_pass = os.environ.get("ISONE_PASSWORD", "")
    urls = []
    if isone_user and isone_pass:
        from urllib.parse import quote as _q
        urls.append(
            f"https://{_q(isone_user)}:{_q(isone_pass)}@webservices.iso-ne.com"
            "/api/v1.1/genfuelmix/current.json"
        )
    # Primary public fallback — EIA v2 with API key
    urls.append(
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?api_key={eia_key}&frequency=hourly&data[0]=value&facets[respondent][]=ISNE&sort[0][column]=period&sort[0][direction]=desc&length=12"
    )
    urls.append(
        f"https://api.eia.gov/v2/electricity/rto/region-data/data/?api_key={eia_key}&frequency=hourly&data[0]=value&facets[respondent][]=ISNE&sort[0][column]=period&sort[0][direction]=desc&length=24"
    )
    # Legacy iso-ne.com paths kept LAST (all 404/403/500 in 2026 but
    # harmless to try in case they come back).
    urls.extend([
        f"https://www.iso-ne.com/transform/csv/genfuelmix?start={today}",
        f"https://www.iso-ne.com/transform/csv/genfuelmix?start={yest}",
    ])
    return urls


def run_extraction():
    started = time.time()
    summary = {"iso": "ISONE", "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_isone_urls(), ua="dchub-iso-isone/1.0")
        # Phase QQ+10: scrub embedded api_key + Basic-auth userinfo
        summary["fetched_url"] = scrub_url(url)
        summary["html_size"] = len(text)
        # Phase QQ+10: try EIA v2 parser FIRST for api.eia.gov URLs
        metrics = {}
        if "api.eia.gov/v2/" in url:
            metrics = parse_eia_v2_fuel_mix(text, prefix="fuel_")
        if not metrics:
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


# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@iso_isone_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)

# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one

@iso_isone_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="ISONE", metrics=latest_for_iso("ISONE")), 200
# AUTO-REPAIR: duplicate route '/health' also in main.py:3746 — review and remove one


@iso_isone_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("ISONE", SOURCE_ID)), 200
