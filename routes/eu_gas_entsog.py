"""
eu_gas_entsog.py — EU gas transmission flows (ENTSOG, LIVE, tokenless).

#60 follow-on (2026-06-02). The owner asked for EU gas coverage; this is the
HONEST version.

WHAT THIS IS — a per-country gas TRANSMISSION-ACTIVITY context signal built from
ENTSOG's live operational data: total physical throughput (entry+exit across a
country's transmission-system-operator points) + net flow (entry-exit, a rough
net-supply direction). Real, live, no token (ENTSOG Transparency is public).

WHAT THIS IS NOT — it is deliberately NOT a "DC Gas Index" and is NOT presented
as a peer to the US DCGI. The US DCGI scores pipeline DENSITY for siting
gas-fired data centers (a US/Texas behind-the-meter phenomenon). EU data centers
are grid-powered, and this measures cross-network gas FLOW, not siting density —
a different thing. So we surface it as energy-transition / gas-dependency
CONTEXT, clearly labeled, never blended into DCGI. (Avoids the false-comparability
trap — same discipline as the DCPI ISO-clone + the HI gas false-gap.)

DATA: GET https://transparency.entsog.eu/api/v1/operationalData
      ?indicator=Physical Flow&operatorKey=<TSO>&periodType=day
  value is kWh/d → /1e6 = GWh/d. We dynamically discover the transmission
  operators from /operators (key contains '-TSO-') for the major gas countries,
  so no operator keys are hardcoded. LIVE-only: nothing fabricated; a country
  with no live points simply isn't reported.
"""
import os
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify

try:
    import requests as _rq
except Exception:
    _rq = None

eu_gas_entsog_bp = Blueprint("eu_gas_entsog", __name__, url_prefix="/api/v1/gas/eu")

_ENTSOG = "https://transparency.entsog.eu/api/v1"
_TARGET = {"DE", "FR", "NL", "IT", "ES", "BE", "AT", "PL", "CZ", "SK"}
_CNAME = {
    "DE": "Germany", "FR": "France", "NL": "Netherlands", "IT": "Italy",
    "ES": "Spain", "BE": "Belgium", "AT": "Austria", "PL": "Poland",
    "CZ": "Czechia", "SK": "Slovakia",
}

_OPS_CACHE = {"data": None, "ts": 0.0}
_SNAP_CACHE = {"data": None, "ts": 0.0}
_OPS_TTL = 86400      # operator list is ~static — refresh daily
_SNAP_TTL = 900       # flows — 15 min (ENTSOG itself lags ~1-2 days)


def _tso_operators():
    """Transmission operators (operatorKey contains '-TSO-') in the target gas
    countries. Cached daily. [] on failure (LIVE-only)."""
    if _rq is None:
        return []
    now = time.time()
    if _OPS_CACHE["data"] is not None and (now - _OPS_CACHE["ts"]) < _OPS_TTL:
        return _OPS_CACHE["data"]
    try:
        r = _rq.get(f"{_ENTSOG}/operators", params={"limit": 600},
                    headers={"Accept": "application/json"}, timeout=15)
        if not r.ok:
            return _OPS_CACHE["data"] or []
        ops = (r.json() or {}).get("operators") or []
    except Exception:
        return _OPS_CACHE["data"] or []
    tso = [{"key": o.get("operatorKey"), "country": o.get("operatorCountryKey"),
            "label": o.get("operatorLabel")}
           for o in ops
           if "-TSO-" in (o.get("operatorKey") or "")
           and o.get("operatorCountryKey") in _TARGET]
    _OPS_CACHE["data"] = tso
    _OPS_CACHE["ts"] = now
    return tso


def _tso_flow(opkey):
    """Latest available day's Physical Flow for one TSO → (entry_gwh, exit_gwh)
    or None. Sums across the TSO's points for the most-recent date that has
    live values."""
    if _rq is None:
        return None
    now = datetime.datetime.utcnow()
    frm = (now - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
    to = now.strftime("%Y-%m-%d")
    try:
        r = _rq.get(f"{_ENTSOG}/operationalData", params={
            "indicator": "Physical Flow", "operatorKey": opkey,
            "from": frm, "to": to, "periodType": "day", "limit": 600,
        }, headers={"Accept": "application/json"}, timeout=18)
        if not r.ok:
            return None
        recs = (r.json() or {}).get("operationalData") or []
    except Exception:
        return None
    # keep only live rows (non-empty value, not flagged NA)
    live = []
    for x in recs:
        v = x.get("value")
        if v in ("", None) or x.get("isNA") == 1:
            continue
        try:
            fv = float(v)
        except Exception:
            continue
        live.append((x.get("periodFrom") or "", x.get("directionKey"), fv))
    if not live:
        return None
    latest = max(p for p, _, _ in live)           # most recent date with data
    entry = sum(v for p, d, v in live if p == latest and d == "entry")
    exit_ = sum(v for p, d, v in live if p == latest and d == "exit")
    return (entry / 1e6, exit_ / 1e6, latest[:10])  # kWh/d → GWh/d


def _live_snapshot():
    """Per-country gas transmission throughput + net flow, aggregated across
    each country's TSOs. None if ENTSOG is unreachable / no live data."""
    now = time.time()
    if _SNAP_CACHE["data"] is not None and (now - _SNAP_CACHE["ts"]) < _SNAP_TTL:
        return _SNAP_CACHE["data"]
    tsos = _tso_operators()
    if not tsos:
        return None
    by_country = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(_tso_flow, t["key"]): t for t in tsos}
        for fut in as_completed(futs, timeout=40):
            t = futs[fut]
            try:
                res = fut.result(timeout=20)
            except Exception:
                res = None
            if not res:
                continue
            entry_gwh, exit_gwh, asof = res
            c = t["country"]
            slot = by_country.setdefault(c, {"entry": 0.0, "exit": 0.0,
                                             "tsos": 0, "asof": asof})
            slot["entry"] += entry_gwh
            slot["exit"] += exit_gwh
            slot["tsos"] += 1
            if asof > slot["asof"]:
                slot["asof"] = asof
    if not by_country:
        return None
    countries = {}
    for c, s in by_country.items():
        throughput = round(s["entry"] + s["exit"], 1)
        countries[c] = {
            "country": c, "name": _CNAME.get(c, c),
            "throughput_gwh_per_day": throughput,
            "net_flow_gwh_per_day": round(s["entry"] - s["exit"], 1),
            "entry_gwh_per_day": round(s["entry"], 1),
            "exit_gwh_per_day": round(s["exit"], 1),
            "tso_count": s["tsos"], "as_of": s["asof"],
        }
    result = {
        "countries": countries,
        "total_throughput_gwh_per_day": round(sum(v["throughput_gwh_per_day"] for v in countries.values()), 1),
        "active_countries": len(countries),
    }
    _SNAP_CACHE["data"] = result
    _SNAP_CACHE["ts"] = now
    return result


_DISCLAIMER = ("EU gas TRANSMISSION-ACTIVITY context (physical throughput + net "
               "flow per country, live via ENTSOG). This is gas-dependency / "
               "energy-transition context — NOT a data-center siting score and "
               "NOT comparable to the US DCGI (which measures pipeline density "
               "for siting gas-fired DCs). EU data centers are grid-powered.")


# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_nordpool_intl.py:206 — review and remove one
@eu_gas_entsog_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    snap = _live_snapshot()
    if snap is None:
        return jsonify({"source": "ENTSOG", "error": "entsog_live_unavailable"}), 503
    return jsonify({
        "source": "ENTSOG Transparency (operational data, tokenless)",
        "live": True,
        "metric": "gas_transmission_flow",
        "unit": "GWh/day",
        "disclaimer": _DISCLAIMER,
        **snap,
    }), 200


@eu_gas_entsog_bp.route("/operators", methods=["GET"])
def http_operators():
    tsos = _tso_operators()
    return jsonify({"source": "ENTSOG", "tso_count": len(tsos),
                    "countries": sorted(_TARGET),
                    "operators": tsos}), 200

# AUTO-REPAIR: duplicate route '/health' also in main.py:3980 — review and remove one

@eu_gas_entsog_bp.route("/health", methods=["GET"])
def http_health():
    tsos = _tso_operators()
    return jsonify({"source": "entsog_transparency_tokenless",
                    "tso_operators_discovered": len(tsos),
                    "live_feed_ok": _live_snapshot() is not None}), 200
