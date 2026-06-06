"""Global infrastructure proxies — fills the non-US gaps on the land-power map.

Sources (all CORS-or-CDN, backend-reachable, free w/ attribution; QA'd 2026-06-06):
  - WRI Global Power Plant Database (CC-BY-4.0): 34,936 plants, 167 countries,
    fuel-typed (solar/wind/hydro/nuclear/geothermal/gas/coal/oil). Single biggest
    global power + renewables gap-closer.
  - Global Energy Monitor Gas + Oil Infrastructure Trackers (CC-BY-4.0, via the
    GreenInfo public mirror): ~3,067 gas pipelines + LNG terminals + ~1,018 oil
    pipelines, 150+ countries, with route geometry.

Each endpoint fetches the upstream CSV server-side, converts to GeoJSON, caches
24h (these datasets change rarely), and serves with CORS so the browser can
render directly. Serves stale on a transient upstream error.
"""
import csv
import io
import json
import logging
import time
import urllib.request

from flask import Blueprint, Response, jsonify, request

log = logging.getLogger("global_infra")
global_infra_bp = Blueprint("global_infra", __name__)

_WRI = ("https://raw.githubusercontent.com/wri/global-power-plant-database/"
        "master/output_database/global_power_plant_database.csv")
_GEM_GAS = ("https://greeninfo-network.github.io/global-gas-infrastructure-tracker/"
            "data/data.csv")
_GEM_OIL = ("https://greeninfo-network.github.io/global-oil-infrastructure-tracker/"
            "static/data/data.csv")

_TTL = 86400  # 24h
_cache = {}   # key -> {"data": <json str>, "ts": float}


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "dchub-map/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def _resp(body: str, state: str):
    return Response(body, mimetype="application/json", headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
        "X-Cache": state,
    })


def _cached(key: str, builder):
    now = time.time()
    c = _cache.get(key)
    if c and (now - c["ts"]) < _TTL:
        return _resp(c["data"], "hit")
    try:
        body = builder()
    except Exception as e:
        if c:
            return _resp(c["data"], "stale")
        return jsonify({"ok": False, "error": f"source fetch failed: {str(e)[:160]}"}), 502
    _cache[key] = {"data": body, "ts": now}
    return _resp(body, "miss")


def _f(v):
    try:
        x = float(v)
        return None if x != x else x  # reject NaN (x != x is True only for NaN)
    except (TypeError, ValueError):
        return None


# ── WRI Global Power Plants → GeoJSON points ──────────────────────────
def _build_wri():
    txt = _fetch_text(_WRI)
    rows = csv.DictReader(io.StringIO(txt))
    feats = []
    for r in rows:
        lat, lng = _f(r.get("latitude")), _f(r.get("longitude"))
        if lat is None or lng is None:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "name": r.get("name") or "",
                "capacity_mw": _f(r.get("capacity_mw")) or 0,
                "fuel": (r.get("primary_fuel") or "").strip(),
                "country": r.get("country_long") or r.get("country") or "",
                "year": r.get("commissioning_year") or "",
            },
        })
    return json.dumps({
        "type": "FeatureCollection", "features": feats,
        "count": len(feats),
        "source": "WRI Global Power Plant Database (CC-BY-4.0)",
    })


@global_infra_bp.route("/api/v1/infrastructure/global-power-plants", methods=["GET"])
def global_power_plants():
    return _cached("wri", _build_wri)


# ── GEM gas/oil trackers → GeoJSON (lines + LNG points) ───────────────
def _gem_features(txt, kind):
    rows = csv.DictReader(io.StringIO(txt))
    feats = []
    for r in rows:
        typ = (r.get("type") or "").strip()
        status = (r.get("status") or "").strip()
        props = {
            "name": r.get("project") or r.get("unit") or "",
            "type": typ,
            "operator": r.get("parent") or "",
            "status": status,
            "countries": r.get("countries") or "",
            "capacity": r.get("capacity") or "",
            "kind": kind,
        }
        route = (r.get("route") or "").strip()
        geom = (r.get("geom") or "").strip()
        if geom == "line" and route and route.lower() != "nan":
            coords = []
            for pt in route.split(":"):
                parts = pt.split(",")
                if len(parts) >= 2:
                    a, b = _f(parts[0]), _f(parts[1])
                    if a is not None and b is not None:
                        coords.append([b, a])  # [lng, lat]
            if len(coords) >= 2:
                feats.append({"type": "Feature",
                              "geometry": {"type": "LineString", "coordinates": coords},
                              "properties": props})
        else:
            lat, lng = _f(r.get("lat")), _f(r.get("lng"))
            if lat is not None and lng is not None:
                feats.append({"type": "Feature",
                              "geometry": {"type": "Point", "coordinates": [lng, lat]},
                              "properties": props})
    return feats


def _build_gem():
    feats = _gem_features(_fetch_text(_GEM_GAS), "gas")
    try:
        feats += _gem_features(_fetch_text(_GEM_OIL), "oil")
    except Exception:
        pass  # gas alone is still a win
    return json.dumps({
        "type": "FeatureCollection", "features": feats,
        "count": len(feats),
        "source": "Global Energy Monitor Gas + Oil Infrastructure Trackers (CC-BY-4.0)",
    })


@global_infra_bp.route("/api/v1/infrastructure/global-gas", methods=["GET"])
def global_gas():
    return _cached("gem", _build_gem)


# ── PeeringDB Global IXPs → GeoJSON points (geocoded via ix→ixfac→fac) ──
_PDB = "https://www.peeringdb.com/api"


def _pdb(path):
    req = urllib.request.Request(_PDB + path,
                                 headers={"User-Agent": "dchub-map/1.0",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()).get("data", [])


def _build_ixps():
    # /api/ix has city/country but NO lat/lng — geocode each IXP via the
    # facility it's hosted in (ixfac → fac). Slim field-sets keep the anon
    # rate-limit happy; cached 24h so this 3-call join runs once a day.
    facs = {f["id"]: f for f in _pdb("/fac?fields=id,latitude,longitude")}
    time.sleep(0.6)
    ix_to_fac = {}
    for xf in _pdb("/ixfac?fields=ix_id,fac_id"):
        ix_to_fac.setdefault(xf.get("ix_id"), xf.get("fac_id"))
    time.sleep(0.6)
    feats = []
    for x in _pdb("/ix?fields=id,name,city,country,net_count"):
        f = facs.get(ix_to_fac.get(x.get("id")))
        if not f:
            continue
        lat, lng = _f(f.get("latitude")), _f(f.get("longitude"))
        if lat is None or lng is None or (lat == 0 and lng == 0):
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "name": x.get("name") or "",
                "city": x.get("city") or "",
                "country": x.get("country") or "",
                "networks": x.get("net_count") or 0,
            },
        })
    return json.dumps({
        "type": "FeatureCollection", "features": feats, "count": len(feats),
        "source": "PeeringDB",
    })


@global_infra_bp.route("/api/v1/infrastructure/global-ixps", methods=["GET"])
def global_ixps():
    return _cached("ixps", _build_ixps)


# ── GDACS multi-hazard proxy (browser can't fetch gdacs.org directly) ──
@global_infra_bp.route("/api/v1/infrastructure/global-hazards", methods=["GET"])
def global_hazards():
    return _cached("gdacs", lambda: _fetch_text(
        "https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP"))


def register_global_infra(app):
    try:
        app.register_blueprint(global_infra_bp)
    except Exception as e:
        log.warning(f"global_infra registration: {e}")
