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

2026-05-31 FIX: lead with api.eia.gov/v2 fuel-type-data, respondent=MISO.
EIA-930 is the Hourly Electric Grid Monitor — the SAME authed feed that powers
our PJM/BPA extractors. EIA_API_KEY is already set in Railway env.

★ 2026-09-07 SUPERSEDED — MISO RE-OPENED THE FEED, at a new host.
The RTWD Data Broker was not gated, it was MOVED: on 2025-12-12 MISO
republished every real-time feed as JSON at public-api.misoenergy.org, and the
retired host says so in its own error body ("See": ".../rtdataapis"). Nobody
followed the pointer, so this module stayed on the EIA fallback and MISO sat
27.5h stale in grid_data while MISO itself served the current 5-minute
interval — measured 2026-09-07, /api/FuelMix "Interval 02:40 EST" read at
07:47Z, SEVEN MINUTES old.

The public API is now PRIMARY and EIA-930 is the fallback. This is not a new
integration: iso_grid_adapters.fetch_miso has been reading the same endpoint
into grid_telemetry every 20 minutes all along — it was simply absent from the
path that feeds grid_data, the table the brain's detector and 15 other readers
actually use. See [tests/test_iso_miso_public_api.py] for the shape contract.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify
from routes._iso_common import (
    fetch_first_working, parse_json_numeric, parse_csv_numeric_columns,
    parse_eia_v2_fuel_mix, scrub_url,
    persist_metrics, parse_eia_v2_latest_period, latest_for_iso, health_for_iso,
    scrub_secrets,
)
# ws2 (2026-07-29): one shared EIA-930 URL builder. See routes/eia930.py.
from routes.eia930 import eia930_url

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*a, **k): pass


iso_miso_bp = Blueprint("iso_miso", __name__, url_prefix="/api/v1/iso/miso")
SOURCE_ID = "iso-miso-realtime"


#: MISO's public real-time API. MISO moved here 2025-12-12 ("these data feeds
#: are available in JSON format only... the underlying URLs have also changed
#: for all links"); the retired MISORTWDDataBroker host names this page in its
#: own error body. Keyless. MISO asks for no more than one request per minute —
#: the orchestrator's cadence is far below that.
MISO_PUBLIC_FUELMIX = "https://public-api.misoenergy.org/api/FuelMix"

#: MISO CATEGORY -> the EIA-930 fuel code the EIA path already writes, so
#: grid_data keeps ONE metric vocabulary for MISO across both sources. MISO
#: publishes no oil/water split, so fuel_oil/fuel_wat simply stop appearing
#: rather than being invented as zeros.
_MISO_FUEL_CODE = {
    "coal": "col",
    "natural gas": "ng",
    "nuclear": "nuc",
    "wind": "wnd",
    "solar": "sun",
    "other": "oth",
}

#: Deliberately NOT in the fuel_ namespace. "Battery Storage" goes NEGATIVE
#: while charging (observed -75 MW) and "Imports" is interchange, not
#: generation — either one inside fuel_* would corrupt any consumer that sums
#: or sign-checks that prefix (routes/state_of_power.py globs `fuel_%`;
#: routes/iso_jp_denkiyoho.py sign-checks `fuel_`).
_MISO_NON_FUEL = {
    "battery storage": "battery_storage_mw",
    "imports":         "net_imports_mw",
}


def _miso_urls():
    """Ordered URL list — first working response wins.

    ★ 2026-09-07 — PRIMARY is MISO's OWN public API again.

    From 2026-05-31 this led with EIA-930 because MISO retired the public RTWD
    Data Broker and the extractor had no working path. That was right at the
    time and wrong now: EIA-930 publishes ~26-28h behind real time, so MISO sat
    27.5h stale in grid_data while MISO itself served the current interval.
    Probed live 2026-09-07: /api/FuelMix returned "Interval 02:40 EST" at
    07:47Z — SEVEN MINUTES old, against EIA's newest MISO period of
    2026-09-06T04 (27.5h).

    This is not a new integration: iso_grid_adapters.fetch_miso already reads
    this exact endpoint and has been landing 8-minute-old MISO rows into
    grid_telemetry every 20 minutes. It was only ever missing from the path
    that feeds grid_data — the table the detector and 15 other readers use.

    EIA-930 stays as fallback 1: still authenticated, still correct, just a day
    late — the right answer when MISO's own API is down. The Data Broker URLs
    stay last in case MISO ever restores them.
    """
    return [
        # PRIMARY: MISO public real-time API (keyless, 5-min intervals).
        MISO_PUBLIC_FUELMIX,
        # Fallback 1: api.eia.gov v2 MISO region (authenticated, ~27h lag).
        # ws2 (2026-07-29): built by routes/eia930.eia930_url; byte-identical.
        eia930_url("MISO"),
        # Fallback 2/3: MISO public Data Broker (retired 2026-05-31, returns
        # {"error": "no data"} — kept in case MISO restores the public feed).
        "https://api.misoenergy.org/MISORTWDDataBroker/DataBrokerServices.asmx?messageType=getfuelmix&returnType=json",
        "https://api.misoenergy.org/MISORTWDDataBroker/DataBrokerServices.asmx?messageType=getrealtimegenmix&returnType=json",
    ]


def parse_miso_public_fuelmix(json_text, prefix="fuel_"):
    """Parse MISO's public /api/FuelMix into the EIA-930 metric vocabulary.

    Shape:
      {"RefId": "07-Sep-2026 - Interval 02:40 EST",
       "TotalMW": 69972,
       "Fuel": {"Type": [{"INTERVALEST": "2026-09-07 2:40:00 AM",
                          "CATEGORY": "Coal", "ACT": "20588", ...}, ...]}}

    ★ ACT arrives as a STRING ("20588"), and can be negative ("-75") — coercion
      is load-bearing or downstream compares megawatts lexically.
    ★ An UNKNOWN category is dropped, never coerced into fuel_oth: silently
      folding a new MISO category into "other" would overstate it and hide the
      fact that MISO added one.
    Returns {} on any shape it does not recognise, so the caller falls through
    to the next URL rather than persisting a half-parse.
    """
    try:
        d = json.loads(json_text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}
    types = ((d.get("Fuel") or {}).get("Type")) if isinstance(d.get("Fuel"), dict) else None
    if not isinstance(types, list) or not types:
        return {}
    out = {}
    for t in types:
        if not isinstance(t, dict):
            continue
        cat = (t.get("CATEGORY") or "").strip().lower()
        if not cat:
            continue
        try:
            mw = float(t.get("ACT"))
        except (TypeError, ValueError):
            continue
        code = _MISO_FUEL_CODE.get(cat)
        if code:
            out[f"{prefix}{code}"] = mw
        elif cat in _MISO_NON_FUEL:
            out[_MISO_NON_FUEL[cat]] = mw
    return out


def parse_miso_interval(json_text):
    """The upstream observation time from /api/FuelMix, as UTC.

    ★ MISO stamps these EST YEAR-ROUND — the field is literally INTERVALEST and
      the 2026-09-07 sample read "02:40 EST" in September, when Eastern local
      time is EDT. So this is a FIXED UTC-5, not US/Eastern; using a DST-aware
      zone would misplace every summer row by an hour. Verified against the
      wall clock at fetch time: 02:40 EST -> 07:40Z, observed 07:47Z (7 min).

    Returns None if the stamp is missing or unparseable — persist_metrics then
    takes the insert clock, which is honest rather than invented.
    """
    try:
        d = json.loads(json_text)
        types = ((d.get("Fuel") or {}).get("Type")) or []
        raw = (types[0] or {}).get("INTERVALEST")
        if not raw:
            return None
        naive = datetime.strptime(str(raw).strip(), "%Y-%m-%d %I:%M:%S %p")
        return naive.replace(tzinfo=timezone(timedelta(hours=-5)))
    except Exception:  # noqa: BLE001 — a bad stamp must not fail the fetch
        return None


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
        if url.startswith(MISO_PUBLIC_FUELMIX):
            metrics = parse_miso_public_fuelmix(text, prefix="fuel_")
        if not metrics and "api.eia.gov/v2/" in url:
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
        # D4 (2026-09-02): the EIA observation hour is the row timestamp, so a
        # repeated reading dedups instead of being re-stamped "now". Non-EIA
        # fallbacks carry no stamp we parse -> None (logged, insert clock).
        if url.startswith(MISO_PUBLIC_FUELMIX):
            observed_at = parse_miso_interval(text)
        elif "api.eia.gov/v2/" in url:
            observed_at = parse_eia_v2_latest_period(text)
        else:
            observed_at = None
        rows = persist_metrics("MISO", metrics, observed_at=observed_at)
        summary["rows_inserted"] = rows
        elapsed = int((time.time() - started) * 1000)
        summary["duration_ms"] = elapsed
        _heartbeat(SOURCE_ID, status="success", rows_affected=rows, duration_ms=elapsed,
                   metadata={"metrics_extracted": len(metrics), "url": url})
        summary["status"] = "ok"
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        summary["status"] = "error"
        summary["error"] = scrub_secrets(f"{type(e).__name__}: {e}")
        summary["duration_ms"] = elapsed
        _heartbeat(SOURCE_ID, status="failure", duration_ms=elapsed, error=summary["error"])
    return summary


@iso_miso_bp.route("/extract", methods=["POST", "GET"])
def trigger():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)


@iso_miso_bp.route("/latest", methods=["GET"])
def latest():
    return jsonify(iso="MISO", metrics=latest_for_iso("MISO")), 200


@iso_miso_bp.route("/health", methods=["GET"])
def health():
    return jsonify(health_for_iso("MISO", SOURCE_ID)), 200
