"""
eia_utility_bas.py — 2026-05-30.
================================

Non-ISO **utility / balancing-authority** coverage. The 10 ISOs/RTOs we
already track (ERCOT, CAISO, PJM, MISO, SPP, NYISO, ISO-NE, IESO, AESO, BPA,
TVA) only cover the organized-market ~60% of the US. The rest — Arizona,
Florida, the Southeast, much of the Mountain West — is run by
vertically-integrated utilities with NO LMP market, so DC Hub showed
"Non-ISO · LMP: Varies" for major data-center markets like Phoenix (APS/SRP)
and Florida (FPL).

EIA-930 (the Hourly Electric Grid Monitor, the same feed that powers our
working PJM/BPA extractors) publishes hourly generation-by-fuel for ~60
balancing authorities — INCLUDING these utilities. So we cover them with the
exact same proven pattern (api.eia.gov/v2 fuel-type-data, per-respondent),
just driven from a registry instead of one file per ISO.

This is the "become the energy resource for the industry" build: BA-level
coverage no competitor offers (they stop at the 7 ISOs).

Routes:
  GET  /api/v1/utility/list                  — the BA registry (public)
  GET  /api/v1/utility/<code>/latest         — latest fuel-mix for one BA
  GET  /api/v1/utility/<code>/health         — extractor health for one BA
  POST /api/v1/utility/extract               — run ALL BAs now (admin/cron)
  POST /api/v1/utility/<code>/extract        — run one BA

run_extraction() (no args) runs ALL BAs and returns an aggregate summary, so
the orchestrator (routes/iso_orchestrator.py) can register this as a single
("eia_utility_bas", "UTILITY_BAS") entry alongside the ISOs.
"""
import os
import time
from flask import Blueprint, jsonify, request

from routes._iso_common import (
    fetch_first_working, parse_eia_v2_fuel_mix, parse_json_numeric,
    persist_metrics, latest_for_iso, health_for_iso, scrub_url,
)

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*a, **k): pass


eia_utility_bas_bp = Blueprint("eia_utility_bas", __name__)


# Registry — code (our tag) → EIA-930 respondent + display metadata.
# `code` doubles as the persist_metrics ISO tag + the /utility/<code> slug.
# eia == the EIA-930 balancing-authority respondent abbreviation.
_BAS = [
    # ── The markets the user called out (non-ISO, were "Non-ISO/Varies") ──
    {"code": "APS",  "eia": "AZPS", "name": "Arizona Public Service",      "region": "Arizona (Phoenix)",      "type": "IOU"},
    {"code": "SRP",  "eia": "SRP",  "name": "Salt River Project",          "region": "Arizona (Phoenix/Tempe)","type": "public"},
    {"code": "FPL",  "eia": "FPL",  "name": "Florida Power & Light",       "region": "Florida (South/East)",   "type": "IOU"},
    # ── Major non-ISO IOUs (big DC-build territory) ──
    {"code": "FPC",  "eia": "FPC",  "name": "Duke Energy Florida",         "region": "Florida (Central)",      "type": "IOU"},
    {"code": "SOCO", "eia": "SOCO", "name": "Southern Company",            "region": "GA/AL/MS",               "type": "IOU"},
    {"code": "DUK",  "eia": "DUK",  "name": "Duke Energy Carolinas",       "region": "NC/SC",                  "type": "IOU"},
    {"code": "SCEG", "eia": "SCEG", "name": "Dominion Energy South Carolina","region": "South Carolina",       "type": "IOU"},
    {"code": "PACE", "eia": "PACE", "name": "PacifiCorp East",             "region": "UT/WY/ID",               "type": "IOU"},
    {"code": "PACW", "eia": "PACW", "name": "PacifiCorp West",             "region": "OR/WA/CA-N",             "type": "IOU"},
    {"code": "PSCO", "eia": "PSCO", "name": "Xcel Energy Colorado",        "region": "Colorado (Denver)",      "type": "IOU"},
    {"code": "NEVP", "eia": "NEVP", "name": "NV Energy",                   "region": "Nevada (Las Vegas/Reno)","type": "IOU"},
    {"code": "IPCO", "eia": "IPCO", "name": "Idaho Power",                 "region": "Idaho",                  "type": "IOU"},
    {"code": "PNM",  "eia": "PNM",  "name": "Public Service New Mexico",   "region": "New Mexico",             "type": "IOU"},
    {"code": "TEC",  "eia": "TEC",  "name": "Tampa Electric",              "region": "Florida (Tampa)",        "type": "IOU"},
    # ── Generation & transmission co-ops (the user asked for co-op power) ──
    {"code": "AECI", "eia": "AECI", "name": "Associated Electric Cooperative","region": "Missouri co-op",      "type": "co-op"},
    {"code": "SEC",  "eia": "SEC",  "name": "Seminole Electric Cooperative","region": "Florida co-op",         "type": "co-op"},
    # ── 2026-05-30 NATIONAL SWEEP — the user asked to "add them all". Every
    # remaining major non-ISO EIA-930 balancing authority, prioritized by
    # data-center relevance. Pacific NW PUDs (Quincy/Wenatchee) are THE
    # hyperscaler cluster; WAPA federal PMAs + Southeast munis fill the rest.
    # Any code that returns 0 rows post-deploy gets pruned (verify loop). ──
    # Pacific Northwest (Quincy/Wenatchee/Portland DC clusters)
    {"code": "PGE",  "eia": "PGE",  "name": "Portland General Electric",   "region": "Oregon (Portland)",       "type": "IOU"},
    {"code": "PSEI", "eia": "PSEI", "name": "Puget Sound Energy",          "region": "Washington (Seattle E)",  "type": "IOU"},
    {"code": "SCL",  "eia": "SCL",  "name": "Seattle City Light",          "region": "Washington (Seattle)",    "type": "public"},
    {"code": "TPWR", "eia": "TPWR", "name": "Tacoma Power",                "region": "Washington (Tacoma)",     "type": "public"},
    {"code": "AVA",  "eia": "AVA",  "name": "Avista",                      "region": "WA/ID (Spokane)",         "type": "IOU"},
    {"code": "CHPD", "eia": "CHPD", "name": "Chelan County PUD",           "region": "Washington (Wenatchee)",  "type": "public"},
    {"code": "DOPD", "eia": "DOPD", "name": "Douglas County PUD",          "region": "Washington (E Wenatchee)","type": "public"},
    {"code": "GCPD", "eia": "GCPD", "name": "Grant County PUD",            "region": "Washington (Quincy DCs)", "type": "public"},
    {"code": "NWMT", "eia": "NWMT", "name": "NorthWestern Energy",         "region": "Montana",                 "type": "IOU"},
    # California (non-CAISO islands)
    {"code": "LDWP", "eia": "LDWP", "name": "LA Dept of Water & Power",    "region": "California (Los Angeles)","type": "public"},
    {"code": "BANC", "eia": "BANC", "name": "Balancing Auth N. California (SMUD)","region": "California (Sacramento)","type": "public"},
    {"code": "IID",  "eia": "IID",  "name": "Imperial Irrigation District","region": "California (Imperial)",   "type": "public"},
    {"code": "TIDC", "eia": "TIDC", "name": "Turlock Irrigation District", "region": "California (Turlock)",     "type": "public"},
    # Desert Southwest
    {"code": "EPE",  "eia": "EPE",  "name": "El Paso Electric",            "region": "TX/NM (El Paso)",         "type": "IOU"},
    {"code": "TEPC", "eia": "TEPC", "name": "Tucson Electric Power",       "region": "Arizona (Tucson)",        "type": "IOU"},
    # WAPA federal power marketing administrations
    {"code": "WACM", "eia": "WACM", "name": "WAPA Rocky Mountain Region",  "region": "CO/WY/NE (federal)",      "type": "federal"},
    {"code": "WALC", "eia": "WALC", "name": "WAPA Desert Southwest",       "region": "AZ/NM/CA (federal)",      "type": "federal"},
    {"code": "WAUW", "eia": "WAUW", "name": "WAPA Upper Great Plains West", "region": "MT/ND/SD (federal)",     "type": "federal"},
    # Southeast (Carolinas, Florida munis, Gulf co-op)
    {"code": "CPLE", "eia": "CPLE", "name": "Duke Energy Progress East",   "region": "NC/SC",                   "type": "IOU"},
    {"code": "CPLW", "eia": "CPLW", "name": "Duke Energy Progress West",   "region": "Western NC",              "type": "IOU"},
    {"code": "SC",   "eia": "SC",   "name": "Santee Cooper",               "region": "South Carolina (public)", "type": "public"},
    {"code": "JEA",  "eia": "JEA",  "name": "JEA",                         "region": "Florida (Jacksonville)",  "type": "public"},
    {"code": "TAL",  "eia": "TAL",  "name": "City of Tallahassee",         "region": "Florida (Tallahassee)",   "type": "public"},
    {"code": "GVL",  "eia": "GVL",  "name": "Gainesville Regional Utilities","region": "Florida (Gainesville)", "type": "public"},
    {"code": "AEC",  "eia": "AEC",  "name": "PowerSouth Energy Cooperative","region": "AL/FL co-op",            "type": "co-op"},
    # Kentucky / Mid-continent federal
    {"code": "LGEE", "eia": "LGEE", "name": "Louisville Gas & Electric / KU","region": "Kentucky",              "type": "IOU"},
    {"code": "SPA",  "eia": "SPA",  "name": "Southwestern Power Admin",    "region": "AR/OK/MO (federal)",      "type": "federal"},
]

_BY_CODE = {b["code"]: b for b in _BAS}
SOURCE_PREFIX = "eia-ba"


def _eia_urls(eia_respondent: str):
    """EIA-930 v2 fuel-type-data for one balancing authority — same authed
    endpoint + parser that fixed PJM/BPA, just a different respondent."""
    key = os.environ.get("EIA_API_KEY", "")
    return [
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/?api_key={key}"
        f"&frequency=hourly&data[0]=value&facets[respondent][]={eia_respondent}"
        f"&sort[0][column]=period&sort[0][direction]=desc&length=12",
    ]


def extract_one(ba: dict) -> dict:
    """Pull one BA's latest hourly fuel mix, parse, persist under its code."""
    started = time.time()
    code, eia = ba["code"], ba["eia"]
    summary = {"code": code, "eia": eia, "name": ba["name"],
               "metrics_extracted": 0, "rows_inserted": 0}
    try:
        text, url = fetch_first_working(_eia_urls(eia), ua="dchub-eia-ba/1.0")
        summary["fetched_url"] = scrub_url(url)  # hide api_key in echoed url
        metrics = {}
        if "api.eia.gov/v2/" in url:
            metrics = parse_eia_v2_fuel_mix(text, prefix="fuel_")
        if not metrics:
            metrics = parse_json_numeric(text)
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["preview"] = (text or "")[:240]
        rows = persist_metrics(code, metrics)
        summary["rows_inserted"] = rows
        summary["status"] = "ok"
        _heartbeat(f"{SOURCE_PREFIX}-{code.lower()}", status="success",
                   rows_affected=rows, duration_ms=int((time.time()-started)*1000),
                   metadata={"eia_respondent": eia, "metrics": len(metrics)})
    except Exception as e:
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        _heartbeat(f"{SOURCE_PREFIX}-{code.lower()}", status="failure",
                   duration_ms=int((time.time()-started)*1000), error=summary["error"])
    summary["duration_ms"] = int((time.time() - started) * 1000)
    return summary


def run_extraction() -> dict:
    """Orchestrator entry — extract EVERY registered BA. Parallel (I/O-bound
    EIA calls) so all BAs finish in a few seconds and fit the orchestrator's
    per-slot timeout even at 40+ BAs. Fail-soft per BA so one EIA hiccup never
    blocks the rest."""
    from concurrent.futures import ThreadPoolExecutor
    started = time.time()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(extract_one, _BAS))
    ok = sum(1 for r in results if r.get("status") == "ok" and r.get("rows_inserted", 0) > 0)
    return {
        "iso": "UTILITY_BAS",
        "total_bas": len(_BAS),
        "bas_with_data": ok,
        "rows_inserted": sum(r.get("rows_inserted", 0) for r in results),
        "duration_ms": int((time.time() - started) * 1000),
        "status": "ok" if ok else "partial",
        "per_ba": results,
    }


# ── Routes ───────────────────────────────────────────────────────────
@eia_utility_bas_bp.route("/api/v1/utility/list", methods=["GET"])
def utility_list():
    return jsonify({
        "ok": True,
        "count": len(_BAS),
        "note": ("Non-ISO balancing authorities (utility/co-op territory) "
                 "tracked via EIA-930. Covers the markets organized-market "
                 "ISOs don't — Arizona (APS/SRP), Florida (FPL), Southeast, "
                 "Mountain West."),
        "balancing_authorities": [
            {"code": b["code"], "name": b["name"], "region": b["region"],
             "type": b["type"], "eia_respondent": b["eia"],
             "latest": f"/api/v1/utility/{b['code']}/latest"}
            for b in _BAS
        ],
    }), 200


@eia_utility_bas_bp.route("/api/v1/utility/<code>/latest", methods=["GET"])
def utility_latest(code):
    code = code.upper()
    if code not in _BY_CODE:
        return jsonify({"error": "unknown balancing authority", "code": code,
                        "see": "/api/v1/utility/list"}), 404
    b = _BY_CODE[code]
    return jsonify({"code": code, "name": b["name"], "region": b["region"],
                    "type": b["type"], "metrics": latest_for_iso(code)}), 200


@eia_utility_bas_bp.route("/api/v1/utility/<code>/health", methods=["GET"])
def utility_health(code):
    code = code.upper()
    if code not in _BY_CODE:
        return jsonify({"error": "unknown balancing authority", "code": code}), 404
    return jsonify(health_for_iso(code, f"{SOURCE_PREFIX}-{code.lower()}")), 200


@eia_utility_bas_bp.route("/api/v1/utility/<code>/extract", methods=["POST", "GET"])
def utility_extract_one(code):
    code = code.upper()
    if code not in _BY_CODE:
        return jsonify({"error": "unknown balancing authority", "code": code}), 404
    s = extract_one(_BY_CODE[code])
    return jsonify(s), (200 if s.get("status") == "ok" else 502)


@eia_utility_bas_bp.route("/api/v1/utility/extract", methods=["POST"])
def utility_extract_all():
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 207)


def register_eia_utility_bas(app):
    app.register_blueprint(eia_utility_bas_bp)
