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
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from internal_auth import is_valid_internal_key

from routes._iso_common import (
    persist_metrics, latest_for_iso, health_for_iso,
)
# ws2 (2026-07-29): the EIA-930 URL builder + fetch/parse now live in ONE
# module instead of a per-extractor copy. See routes/eia930.py.
from routes.eia930 import eia930_url, fetch_eia930_ba

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
    endpoint + parser that fixed PJM/BPA, just a different respondent.

    ws2 (2026-07-29): the query string moved to routes/eia930.eia930_url, which
    builds the byte-identical URL for all five EIA-930 extractors. Kept as a
    thin shim so anything holding a URL list keeps working."""
    return [eia930_url(eia_respondent)]


def extract_one(ba: dict) -> dict:
    """Pull one BA's latest hourly fuel mix, parse, persist under its code."""
    started = time.time()
    code, eia = ba["code"], ba["eia"]
    summary = {"code": code, "eia": eia, "name": ba["name"],
               "metrics_extracted": 0, "rows_inserted": 0}
    try:
        res = fetch_eia930_ba(eia, ua="dchub-eia-ba/1.0", timeout=4, total_budget=5)
        summary["fetched_url"] = res.get("fetched_url")  # scrubbed by the adapter
        # The EIA observation hour. grid_data does NOT store it — that table's
        # timestamp column defaults to NOW() (routes/iso_ercot.py:67), i.e. the
        # insert clock — so this is the only honest freshness basis we carry.
        summary["eia_period"] = res.get("period")
        summary["eia_cached"] = res.get("cached")
        summary["eia_cache_age_s"] = res.get("cache_age_s")
        if res.get("truncated_risk"):
            summary["truncated_risk"] = True
        if res.get("status") != "ok":
            # The adapter never raises. Re-raise so a failed BA takes exactly
            # the same path (status=error + failure heartbeat) it took when
            # fetch_first_working raised — otherwise a dead fetch would
            # heartbeat SUCCESS with 0 rows.
            raise RuntimeError(f"eia930/{res.get('status')}: "
                               + (res.get("error") or "no detail"))
        metrics = res.get("metrics") or {}
        # 2026-07-29: the old parse_json_numeric fallback is GONE. It only ran
        # when the EIA v2 parser found nothing, and against an EIA v2 envelope
        # it walks the response tree and emits junk keys like
        # `response_data_0_value_mw` — garbage persisted under a real BA code.
        # 0 metrics is now reported as 0, with a reason.
        summary["metrics_extracted"] = len(metrics)
        if not metrics:
            summary["preview"] = res.get("preview")
            summary["no_metrics_reason"] = res.get("no_metrics_reason")
        # D4 (2026-09-02): write the EIA observation hour as the row timestamp.
        # The comment above ("grid_data does NOT store it") described the old
        # insert-clock write; a frozen BA (AEC, 2021-09-01T05) now dedups to
        # ZERO new rows per tick instead of 96 fabricated-fresh ones a day.
        rows = persist_metrics(code, metrics, observed_at=res.get("period"))
        summary["rows_inserted"] = rows
        summary["status"] = "ok"
        _heartbeat(f"{SOURCE_PREFIX}-{code.lower()}", status="success",
                   rows_affected=rows, duration_ms=int((time.time()-started)*1000),
                   metadata={"eia_respondent": eia, "metrics": len(metrics),
                             "eia_period": res.get("period"),
                             "cached": res.get("cached")})
    except Exception as e:
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        _heartbeat(f"{SOURCE_PREFIX}-{code.lower()}", status="failure",
                   duration_ms=int((time.time()-started)*1000), error=summary["error"])
    summary["duration_ms"] = int((time.time() - started) * 1000)
    return summary


# EIA-930 is hourly. A frozen period is ordinary for an hour or two (the feed
# publishes with a lag) and abnormal well beyond that. Past this, a stall stops
# being a "wait" and is reported as the failure it is.
_UPSTREAM_STALL_H = 6.0


def _period_age_hours(period, now=None):
    """Hours since an EIA observation period, or None if unparseable.

    None is UNMEASURED, never 0 — a period we cannot read must not present as
    perfectly fresh, and must not silently become a 'wait' either.
    """
    if not period:
        return None
    txt = str(period).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H", "%Y-%m-%d %H", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = (datetime.fromisoformat(txt) if fmt is None
                  else datetime.strptime(txt, fmt))
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc)) - dt).total_seconds() / 3600.0
    return None


def run_extraction() -> dict:
    """Orchestrator entry — extract EVERY registered BA. Parallel (I/O-bound
    EIA calls) so all BAs finish in a few seconds and fit the orchestrator's
    per-slot timeout even at 40+ BAs. Fail-soft per BA so one EIA hiccup never
    blocks the rest."""
    from concurrent.futures import ThreadPoolExecutor
    started = time.time()
    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(extract_one, _BAS))
    # Two readings of "how many BAs worked", both reported rather than one
    # picked: a BA can extract cleanly and still persist 0 rows (EIA publishes
    # nothing for it, or every fuel was exactly 0). Collapsing those into one
    # number is how a coverage claim drifts from the data.
    ok = sum(1 for r in results if r.get("status") == "ok" and r.get("rows_inserted", 0) > 0)
    ran_ok = sum(1 for r in results if r.get("status") == "ok")
    live = sum(1 for r in results if r.get("eia_cached") is False)
    cached = sum(1 for r in results if r.get("eia_cached") is True)

    # ★★★ 2026-09-03 — "partial" COULD NOT SAY WHICH KIND OF BROKEN.
    #
    #   `status = "ok" if ok else "partial"` collapsed two opposite situations
    #   into one word, because `ok` counts BAs that persisted a ROW:
    #
    #     · every fetch worked and EIA simply has not published a newer hour
    #       -> 0 rows (D4 dedups an unchanged period, by design)  -> "partial"
    #     · every fetch FAILED                                     -> "partial"
    #
    #   Measured this morning: data-pulse printed
    #   `failed_isos=UTILITY_BAS(partial)` on every tick for 25 hours while the
    #   newest stored observation stood at 2026-09-02T06:00Z across 45 BAs, and
    #   nothing anywhere could say whether EIA had stopped publishing or we had
    #   stopped reading. The orchestrator gained the vocabulary for exactly this
    #   distinction on 2026-09-03 (iso_orchestrator.AWAITING_UPSTREAM); this
    #   extractor never spoke it.
    #
    #   ★ THE STATUS KEY IS LOAD-BEARING AND THE ORDER IS A TRAP.
    #     classify_result tests `if st:` -> failed BEFORE it looks at
    #     `awaiting_upstream`, so ANY non-empty status that is not "ok" or
    #     "no_new_data" is a failure and the wait list is never read. To be
    #     classified as waiting, an extractor must leave `status` UNSET.
    #
    #   ★ AND A LONG WAIT IS NOT A WAIT. EIA-930 is hourly; a frozen period is
    #     benign for an hour or two and abnormal after that. Past
    #     _UPSTREAM_STALL_H the feed stops calling itself "waiting" and goes
    #     back to being a failure that names the frozen hour — otherwise this
    #     change would convert a real 25h outage into a permanently reassuring
    #     "awaiting upstream", which is the exact shape of the bug it fixes.
    errored = [r for r in results if r.get("status") == "error"]
    periods = sorted({str(r.get("eia_period")) for r in results
                      if r.get("status") == "ok" and r.get("eia_period")})
    newest = periods[-1] if periods else None
    stall_h = _period_age_hours(newest)
    out_status, waiting, note = None, [], None

    if errored:
        # A real error outranks a wait — an extractor must not launder a
        # failure by also being late.
        out_status = "partial"
        note = ("%d of %d BA fetches FAILED: %s"
                % (len(errored), len(_BAS),
                   "; ".join("%s=%s" % (r.get("code"), str(r.get("error"))[:60])
                             for r in errored[:4])))
    elif ok:
        out_status = "ok"
    elif stall_h is not None and stall_h > _UPSTREAM_STALL_H:
        out_status = "partial"
        # ★ The first 60 characters are all data-pulse prints (it truncates
        #   the reason), so the WHICH-kind-of-broken has to lead.
        note = ("EIA published nothing newer than %s (%.1fh ago) — all %d BAs "
                "fetched cleanly, so this is an UPSTREAM stall, not a read "
                "failure; past the %.0fh tolerance it is reported as a failure "
                "rather than a wait"
                % (newest, stall_h, len(_BAS), _UPSTREAM_STALL_H))
    else:
        # status deliberately UNSET so classify_result reaches the wait list.
        waiting = ["%s (all %d BAs fetched cleanly; newest EIA hour %s%s)"
                   % ("EIA-930", len(_BAS), newest or "unknown",
                      "" if stall_h is None else ", %.1fh ago" % stall_h)]
        note = "no newer EIA hour published yet — nothing to insert"

    return {
        "iso": "UTILITY_BAS",
        "total_bas": len(_BAS),
        "bas_with_data": ok,            # extracted AND persisted >= 1 row
        "bas_extracted_ok": ran_ok,     # extractor ran clean (may be 0 rows)
        "eia_calls_live": live,
        "eia_calls_from_cache": cached,
        "eia_cache_note": ("EIA-930 is hourly, data-pulse runs every 15 min; the "
                           "cache is PER PROCESS (routes/eia930.py) so this counts "
                           "one replica of two, not the fleet"),
        "rows_inserted": sum(r.get("rows_inserted", 0) for r in results),
        "duration_ms": int((time.time() - started) * 1000),
        # Only set when there is something to say. An UNSET status is what lets
        # classify_result reach `awaiting_upstream` — see the block above.
        **({"status": out_status} if out_status else {}),
        # ★ classify_result derives its reason from `error` (then errors[0]),
        #   NEVER from `note` — so a diagnosis parked in `note` alone reaches
        #   nobody. This is why the log said only "UTILITY_BAS(partial)" for 25
        #   hours: the extractor had no `error` to report and the status word
        #   was the whole message.
        **({"error": note} if (out_status and note) else {}),
        **({"awaiting_upstream": waiting} if waiting else {}),
        **({"note": note} if note else {}),
        "newest_eia_period": newest,
        "upstream_stall_hours": (None if stall_h is None else round(stall_h, 2)),
        "bas_failed": len(errored),
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
    if not is_valid_internal_key(request.headers.get("X-Internal-Key") or request.headers.get("X-Admin-Key")):
        return jsonify({"error": "unauthorized"}), 401
    code = code.upper()
    if code not in _BY_CODE:
        return jsonify({"error": "unknown balancing authority", "code": code}), 404
    s = extract_one(_BY_CODE[code])
    return jsonify(s), (200 if s.get("status") == "ok" else 502)


@eia_utility_bas_bp.route("/api/v1/utility/extract", methods=["POST"])
def utility_extract_all():
    if not is_valid_internal_key(request.headers.get("X-Internal-Key") or request.headers.get("X-Admin-Key")):
        return jsonify({"error": "unauthorized"}), 401
    s = run_extraction()
    return jsonify(s), (200 if s.get("status") == "ok" else 207)


def register_eia_utility_bas(app):
    app.register_blueprint(eia_utility_bas_bp)
