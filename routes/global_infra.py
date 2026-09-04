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

★ 2026-08-17 — COLD CACHE WAS A GUARANTEED 503 AT THE EDGE. See the cache
section below: the 24h cache was an in-process dict, so every deploy and every
replica started cold, and a cold build (7-9s) cannot fit the CF worker's budget
for these GETs. Now: Redis L2 + serve-stale-while-revalidate + a per-process
boot warm, so no user request ever waits on an upstream fetch.
"""
import csv
import io
import json
import logging
import os
import random
import threading
import time
import urllib.request

import requests
import zlib

from flask import Blueprint, Response, jsonify, request

log = logging.getLogger("global_infra")
global_infra_bp = Blueprint("global_infra", __name__)

_WRI = ("https://raw.githubusercontent.com/wri/global-power-plant-database/"
        "master/output_database/global_power_plant_database.csv")
_GEM_GAS = ("https://greeninfo-network.github.io/global-gas-infrastructure-tracker/"
            "data/data.csv")
_GEM_OIL = ("https://greeninfo-network.github.io/global-oil-infrastructure-tracker/"
            "static/data/data.csv")

# ── cache: L1 in-process → L2 Redis → build ───────────────────────────
#
# ★ 2026-08-17 — MEASURED, not assumed. The old cache was `_cache`, a plain
# in-process dict, and its `stale` fallback can never help a COLD process
# because a cold process has no stale copy. What that cost, at the origin:
#
#   global-hazards (GDACS passthrough)   COLD 7.3-8.4s   WARM 0.85-1.0s
#   global-power-plants (12MB WRI CSV)   COLD ~9s        WARM 1.8s
#   global-gas (2 GEM CSVs + parse)      COLD ~5s        WARM 1.3s
#   global-ixps (PeeringDB 3-call join)  COLD ~5s        WARM —
#
# ★ THE EDGE BUDGET FOR THESE IS 5s, NOT 15s. `/api/v1/infrastructure/` is NOT
# in the CF worker's SLOW_PATH_PREFIXES, and for a GET the first attempt is
# `Math.min(isSlowPath ? 15000 : 5000, timeoutMs)` — so the ROUTE_TIMEOUTS entry
# `'/api/v1/infrastructure': 15_000` only ever raises the RETRY. Attempt 1 is
# capped at 5s; the retry then walks Railway→Render→KV, finds nothing for these
# paths, and returns the worker's 503 envelope. EVERY cold build above exceeds
# 5s, so a cold process was a guaranteed 503 for the first caller.
#
# ★ AND COLD IS NOT RARE. The service runs `--workers 1` × **2 replicas**, so
# each dataset had to be built twice, independently. Proven live 2026-08-17:
# 8 sequential requests to global-hazards returned hit,hit,MISS(5.2s),hit,hit,
# hit,hit,hit — request 3 landed on the other replica. On top of that, main is
# push-to-deploy and moves several times an hour (five deploys between 09:56 and
# 10:24 UTC that morning alone), and gunicorn recycles each worker every ~1000
# requests. The 24h TTL was never the thing driving cold starts — deploys were.
#
# The fix has three parts, in the order they matter:
#   1. L2 in Redis (REDIS_URL, live in prod), zlib'd. A freshly booted process
#      or a second replica hydrates from the shared copy in ms, so a deploy no
#      longer costs an upstream fetch at all.
#   2. Serve-stale-while-revalidate. An expired-but-present payload is returned
#      instantly and refreshed in a background single-flight thread, so the 24h
#      TTL lapsing is never paid on a user's request either.
#   3. A per-process boot warm (see the bottom of this file) for the one case
#      1 and 2 cannot cover: nothing cached anywhere yet.
# Mirrors the shape already used by routes/mcp_leadership_engine.py
# (_RESP_TTL / _STALE_MAX / _refresh_async / warm) — the Redis layer is the
# addition, and it earns its place here because unlike the engines' per-process
# DB scores, these four payloads are byte-identical across every replica.
_TTL = 86400            # 24h — refresh target (these datasets change rarely)
_STALE_MAX = 7 * 86400  # serve-stale ceiling: past this, block and rebuild rather
                        # than serve week-old hazards through a long outage
_RKEY = "dchub:ginfra:"

_cache = {}        # key -> {"data": <json str>, "ts": float}   (L1, per process)
_REFRESH = {}      # key -> Lock, single-flight for background revalidation
_BUILD = {}        # key -> Lock, single-flight for the blocking cold build
_LOCKS_GUARD = threading.Lock()
_redis = {"client": None, "tried": False}


def _lock(registry: dict, key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return registry.setdefault(key, threading.Lock())


def _rds():
    """Binary-safe Redis client, or None. Deliberately NOT redis_cache's shared
    client: that one is `decode_responses=True` (str only) and json-encodes on
    write, which would escape a 7MB JSON string into a JSON string. These blobs
    are zlib bytes. Connect once per process; any failure degrades to L1."""
    if _redis["tried"]:
        return _redis["client"]
    _redis["tried"] = True
    url = os.environ.get("REDIS_URL")
    if not url:
        log.info("global_infra: REDIS_URL unset — in-process cache only")
        return None
    try:
        import redis as _redis_lib
        c = _redis_lib.from_url(url, socket_connect_timeout=3, socket_timeout=5,
                                retry_on_timeout=True)
        c.ping()
        _redis["client"] = c
        log.info("global_infra: redis L2 connected")
    except Exception as e:
        log.info(f"global_infra: redis unavailable ({str(e)[:100]}) — in-process cache only")
    return _redis["client"]


def _blob_dump(body: str, ts: float) -> bytes:
    return f"{int(ts)}\n".encode() + zlib.compress(body.encode("utf-8"), 6)


def _blob_load(raw: bytes):
    head, sep, comp = raw.partition(b"\n")
    if not sep:
        raise ValueError("malformed blob")
    return zlib.decompress(comp).decode("utf-8"), float(head)


def _load(key: str):
    """Newest usable entry as (body, ts, source), or None.

    A FRESH L1 short-circuits before Redis, so the hot path stays a dict read;
    only a stale-or-missing L1 pays a round trip. When both exist the newer
    wins — otherwise a replica whose L1 just expired would rebuild a dataset
    another replica has already refreshed into Redis."""
    c = _cache.get(key)
    l1 = (c["data"], c["ts"]) if c else None
    if l1 and (time.time() - l1[1]) < _TTL:
        return l1[0], l1[1], "l1"
    r = _rds()
    if r is not None:
        try:
            raw = r.get(_RKEY + key)
        except Exception as e:
            log.debug(f"global_infra: redis get {key}: {str(e)[:100]}")
            raw = None
        if raw:
            try:
                body, ts = _blob_load(raw)
                if not l1 or ts > l1[1]:
                    _cache[key] = {"data": body, "ts": ts}
                    return body, ts, "redis"
            except Exception as e:
                log.warning(f"global_infra: unreadable redis blob {key}: {str(e)[:100]}")
    return (l1[0], l1[1], "l1") if l1 else None


def _store(key: str, body: str, ts: float = None):
    ts = time.time() if ts is None else ts
    _cache[key] = {"data": body, "ts": ts}
    r = _rds()
    if r is not None:
        try:
            r.setex(_RKEY + key, int(_STALE_MAX), _blob_dump(body, ts))
        except Exception as e:
            log.debug(f"global_infra: redis set {key}: {str(e)[:100]}")


def _refresh_async(key: str, builder):
    """Kick one background rebuild; deduped per key, never blocks the caller.
    A failed refresh leaves the last good copy in place on purpose."""
    lk = _lock(_REFRESH, key)
    if not lk.acquire(blocking=False):
        return False

    def _run():
        try:
            _store(key, builder())
        except Exception as e:
            log.warning(f"global_infra: background refresh {key} failed: {str(e)[:160]}")
        finally:
            lk.release()

    threading.Thread(target=_run, name=f"ginfra-refresh-{key}", daemon=True).start()
    return True


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "dchub-map/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def _require_feature_collection(body: str, label: str) -> str:
    """Raise unless `body` really is a GeoJSON FeatureCollection.

    ★ 2026-08-17 — for the PASSTHROUGH proxies (the ones that forward an
    upstream body instead of building GeoJSON here), a 200 with the wrong SHAPE
    is the dangerous case: `_cached` would store it for 24h and every client
    would render an error document. GDACS returned exactly that class of
    response — HTTP 400 with `{"message":"Eventtype is required."}` — after it
    began requiring a parameter we were not sending.

    Raising here instead of returning means `_cached` keeps serving the last
    GOOD payload (its `stale` branch) rather than poisoning the cache with an
    error body. Fail closed on shape, degrade gracefully on availability.
    """
    try:
        doc = json.loads(body)
    except Exception:
        raise ValueError(f"{label}: upstream returned non-JSON")
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection" \
            or not isinstance(doc.get("features"), list):
        detail = ""
        if isinstance(doc, dict):
            detail = str(doc.get("message") or doc.get("error") or doc.get("type") or "")[:120]
        raise ValueError(f"{label}: upstream is not a FeatureCollection ({detail or 'unknown shape'})")
    return body


def _resp(body: str, state: str):
    return Response(body, mimetype="application/json", headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
        "X-Cache": state,
    })


def _cached(key: str, builder):
    """Answer from cache if at all possible; only ever block when NOTHING is
    cached anywhere. X-Cache tells you which lane answered — `hit` (this
    process), `shared` (hydrated from Redis, i.e. a fresh replica that would
    previously have been a cold 503), `stale` (served instantly, refreshing
    behind you), `miss` (the blocking build the boot warm exists to prevent)."""
    now = time.time()
    ent = _load(key)
    if ent and (now - ent[1]) < _TTL:
        return _resp(ent[0], "hit" if ent[2] == "l1" else "shared")
    if ent and (now - ent[1]) < _STALE_MAX:
        _refresh_async(key, builder)
        return _resp(ent[0], "stale")

    # Nothing usable anywhere. Single-flight the build so a burst of map loads
    # on a cold process fires ONE upstream fetch, not one per request thread
    # (2 replicas × 8 gunicorn threads made that a real stampede).
    with _lock(_BUILD, key):
        ent = _load(key)  # another thread may have won the race while we waited
        if ent and (time.time() - ent[1]) < _TTL:
            return _resp(ent[0], "hit" if ent[2] == "l1" else "shared")
        try:
            body = builder()
        except Exception as e:
            if ent:  # older than _STALE_MAX, but far better than a 502
                return _resp(ent[0], "stale")
            return jsonify({"ok": False, "error": f"source fetch failed: {str(e)[:160]}"}), 502
        _store(key, body)
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
    headers = {"User-Agent": "dchub-map/1.0", "Accept": "application/json"}
    # Anon PeeringDB rate-limits the multi-call join (HTTP 429). A free API key
    # (set PEERINGDB_API_KEY on Railway) raises the limit so IXPs load reliably.
    key = os.environ.get("PEERINGDB_API_KEY")
    if key:
        headers["Authorization"] = "Api-Key " + key
    req = urllib.request.Request(_PDB + path, headers=headers)
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
#
# ★ 2026-08-17 — `eventtype` IS REQUIRED NOW. GDACS started rejecting the bare
# MAP call: `GET .../geteventlist/MAP` → 400 {"message":"Eventtype is
# required."}. That surfaced as a 502 here, a 503 at the edge, and — because the
# map's layer called r.json() without checking the status — an "Invalid GeoJSON
# object" throw from inside Leaflet on the flagship page. Measured 2026-08-17:
#
#   geteventlist/MAP                              → 400 Eventtype is required
#   geteventlist/MAP?eventtypes=EQ;TC;...         → 400 (plural spelling rejected)
#   geteventlist/MAP?eventlist=EQ;TC;...          → 400
#   geteventlist/MAP?eventtype=EQ,TC,FL,DR,WF,VO  → 200 FeatureCollection ✓
#
# So it is singular `eventtype` with a COMMA-separated list. The six codes are
# exactly the ones the client's HAZ icon map renders (EQ/TC/FL/VO/DR/WF); asking
# for more would return events the map cannot label.
#
# ★ 2026-09-04 — GDACS TIGHTENED AGAIN, AND THE COMMA LIST IS NOW REJECTED.
# The layer had been dark on the live map (503 at the edge, "Backend unreachable
# and no cached data available"). Measured 2026-09-04 against gdacs.org:
#
#   geteventlist/MAP?eventtype=EQ,TC,FL,DR,WF,VO  → 400 "Please specify only 1 eventtype."
#   geteventlist/MAP?eventtype=EQ                 → 200 FeatureCollection ✓
#
# Every single code still answers 200 on its own, so the parameter did not go
# away — it stopped accepting a list. One call per code, merged here, is the
# only shape that survives both the 2026-08-17 rule (eventtype is required) and
# this one (exactly one per call).
#
# PARTIAL IS PUBLISHED, NOT HIDDEN. A per-code fan-out can lose one code while
# five answer. Dropping wildfires silently for the 24h TTL would be the failure
# this file's `_require_feature_collection` docstring exists to prevent, so the
# merged body carries `eventtypes_ok` / `eventtypes_failed` and callers can see
# which hazard classes the payload actually covers. Only a TOTAL failure raises
# — that is the case where `_cached` should keep serving the last good payload
# rather than cache a hazard map that covers nothing.
_GDACS_EVENTTYPES = ("EQ", "TC", "FL", "DR", "WF", "VO")
_GDACS_ONE = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP"
              "?eventtype={}")


def _build_gdacs():
    features, ok, failed = [], [], {}
    for code in _GDACS_EVENTTYPES:
        try:
            body = _require_feature_collection(
                _fetch_text(_GDACS_ONE.format(code)), f"gdacs:{code}")
            features.extend(json.loads(body).get("features") or [])
            ok.append(code)
        except Exception as e:
            failed[code] = f"{type(e).__name__}: {str(e)[:120]}"

    if not ok:
        # Nothing answered — let `_cached` fall back to the last good payload
        # instead of caching an empty hazard map for a day.
        raise ValueError(
            "gdacs: no eventtype answered ("
            + "; ".join(f"{k} {v}" for k, v in failed.items())[:400] + ")")

    return json.dumps({
        "type": "FeatureCollection",
        "features": features,
        "count": len(features),
        "source": "GDACS",
        "eventtypes_ok": ok,
        "eventtypes_failed": failed,
    })


@global_infra_bp.route("/api/v1/infrastructure/global-hazards", methods=["GET"])
def global_hazards():
    return _cached("gdacs", _build_gdacs)


# ── boot warm — the one case Redis + serve-stale cannot cover ─────────
#
# Redis makes a fresh process warm on arrival and serve-stale keeps the TTL off
# the request path, so this loop exists for the remaining gap: nothing cached
# ANYWHERE (first deploy after this ships, a Redis flush/eviction, a new key).
# It also keeps the shared copy refreshed so `stale` stays a rarity rather than
# the steady state.
#
# Per-process rather than cron-driven, for the reason routes/engine_prewarm.py
# documents at length: the cron heartbeat's dominant caller runs on the WORKER
# role and dispatches over loopback, so its fires warm the worker, not the web
# replicas users actually hit. A process warming ITSELF is the only topology-
# proof shape. Redis makes the second replica's pass cheap — it finds the first
# one's payload fresh and just hydrates L1.
_WARMABLE = {}  # key -> builder; populated below, after the builders exist
_SELFWARM = {"started": False}
_SELFWARM_BOOT_DELAY = 30.0    # let blueprints finish registering first
_SELFWARM_JITTER = 60.0        # stagger the two replicas off each other
_SELFWARM_INTERVAL = 6 * 3600  # 4 ticks per _TTL — refreshes well before expiry


def warm(force: bool = False) -> dict:
    """Build any dataset that is missing or past _TTL, in-process, off the
    request path. Honors the cache like `_cached` does, so a fresh entry —
    including one another replica just wrote to Redis — is a cheap no-op."""
    out = {}
    for key, builder in _WARMABLE.items():
        ent = None if force else _load(key)
        if ent and (time.time() - ent[1]) < _TTL:
            out[key] = {"warmed": False, "reason": "fresh",
                        "source": ent[2], "age_s": round(time.time() - ent[1], 1)}
            continue
        t0 = time.time()
        try:
            _store(key, builder())
            out[key] = {"warmed": True, "took_s": round(time.time() - t0, 2)}
        except Exception as e:
            out[key] = {"warmed": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return out


def _selfwarm_loop():
    time.sleep(_SELFWARM_BOOT_DELAY + random.uniform(0, _SELFWARM_JITTER))
    while True:
        try:
            if os.environ.get("GLOBAL_INFRA_PREWARM_DISABLE") != "1":
                log.info(f"global_infra: warm tick {warm()}")
        except Exception as e:
            log.warning(f"global_infra: warm tick failed: {str(e)[:160]}")
        time.sleep(_SELFWARM_INTERVAL)


def _start_selfwarm_thread() -> bool:
    """Railway-only, once per process. Render is the read-only failover — every
    upstream fetch there is wasted CPU — and local/dev/test runs must never grow
    a background thread that reaches the network."""
    if not (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID")):
        return False
    if os.environ.get("GLOBAL_INFRA_PREWARM_DISABLE") == "1":
        return False
    if _SELFWARM["started"]:
        return False
    _SELFWARM["started"] = True
    threading.Thread(target=_selfwarm_loop, name="ginfra-selfwarm", daemon=True).start()
    return True


@global_infra_bp.record_once
def _boot_selfwarm(state):
    # daemon thread that sleeps first — cannot delay boot or the /api/health check
    try:
        if _start_selfwarm_thread():
            print("[global_infra] per-process warm loop started "
                  f"(every {int(_SELFWARM_INTERVAL)}s)", flush=True)
    except Exception as e:
        print(f"[global_infra] warm thread skipped: {e}", flush=True)


# Same builders the routes use — a warm that drifted from the served path would
# populate a key nothing reads. Ordered cheapest-first (gdacs ~7s → wri ~9s) so
# a slow dataset never delays the others.
_WARMABLE.update({
    "gdacs": _build_gdacs,
    "gem": _build_gem,
    "ixps": _build_ixps,
    "wri": _build_wri,
})


# ── OSM transmission lines (the non-US grid the map could not draw) ───
#
# ★ 2026-09-04 — WHY THIS EXISTS
#
# Every transmission layer on the Land & Power map was US-only, and silently so.
# Measured that day against the HIFLD ArcGIS service the map itself calls
# (Electric_Power_Transmission_Lines/FeatureServer/0), returnCountOnly:
#
#   bbox -77.8,38.7,-77.1,39.3   (N. Virginia)  -> count 177
#   bbox  8.5,49.8,10.1,50.9     (Hesse, DE)    -> count 0
#   bbox  5.8,47.2,15.1,55.1     (all Germany, no voltage filter) -> count 0
#
# So a user evaluating a German site saw an empty map under a badge reading
# "300k". The dataset has no European coverage at all; nothing in our own DB
# could fix that, because transmission_lines_geocoded_snapshot IS HIFLD.
#
# OpenStreetMap does have it, and densely. Measured the same day via Overpass:
#
#   way["power"="line"]["voltage"] over Germany  -> 65,460 ways
#   way["power"="line"] near Birstein            -> a 380 kV TenneT TSO line,
#                                                   6 cables, 111 geometry points
#
# 65k voltage-tagged ways for Germany alone is the same order as the entire US
# HIFLD set (~95k), so this is a real layer, not a token gesture.
#
# ★ LICENCE — ODbL, AND IT PROPAGATES
# OSM is ODbL-licensed. Every response carries the attribution and licence in
# `_licence` / `attribution`, and the map must render it. ODbL is share-alike:
# a Derivative Database has to be offered under ODbL too. That is a product/
# legal decision, not a code one — this endpoint keeps the obligation VISIBLE in
# the payload rather than laundering it into an unattributed layer.
#
# ★ WHY A SERVER-SIDE PROXY AND NOT A BROWSER CALL
# The frontend's own OSM/Overpass loaders were stubbed out on 2026-06-12 for
# "Overpass 429 spam" (see the note at js/land-power-app.js ~7603) and never
# replaced. Calling Overpass per-pan from every visitor's browser is what earned
# those 429s. Going through `_cached` means one upstream fetch per bbox per TTL
# across all users, single-flighted, with serve-stale on top — the same
# machinery the GDACS and PeeringDB proxies above use.
_OSM_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Overpass bills by area. A whole-continent bbox times out upstream and returns
# a 429/504 rather than data, so the request is refused HERE with an actionable
# message instead of being turned into a slow failure.
_OSM_MAX_AREA_DEG2 = 6.0
_OSM_TIMEOUT = 120


def _osm_query(min_lat, min_lng, max_lat, max_lng, min_kv):
    # voltage is tagged in VOLTS in OSM (380 kV -> "380000").
    return (
        f"[out:json][timeout:{_OSM_TIMEOUT}];"
        f'way["power"="line"]["voltage"]'
        f"({min_lat},{min_lng},{max_lat},{max_lng});"
        f"out geom;"
    )


def _osm_ints(raw):
    """Every integer in a free-text OSM value, in order.

    OSM voltage/cables/circuits are strings, and a tower carrying two circuits
    at different voltages is tagged with a SEMICOLON list: '380000;110000'.
    Requiring digits-only in the Overpass filter silently dropped exactly those
    — measured 2026-09-04: 63,793 digits-only vs 65,460 with any voltage tag
    over Germany, i.e. 1,667 lines missing, and they are the multi-circuit
    towers rather than a random sample."""
    if raw is None:
        return []
    out, digits = [], ""
    for ch in str(raw):
        if ch.isdigit():
            digits += ch
        else:
            # A space inside a number ('380 000') is a thousands separator, not
            # a separator between two values; only a real delimiter ends one.
            if digits and ch not in " \u00a0":
                out.append(int(digits)); digits = ""
            elif digits and ch in " \u00a0":
                continue
    if digits:
        out.append(int(digits))
    return out


def _osm_max_volts(raw):
    """Highest voltage on the way — what the line is rated at."""
    vals = _osm_ints(raw)
    return max(vals) if vals else None


def _osm_first_int(raw):
    """First integer (cables / circuits are single-valued in practice)."""
    vals = _osm_ints(raw)
    return vals[0] if vals else None


def _build_osm_transmission(min_lat, min_lng, max_lat, max_lng, min_kv):
    body, last_err = None, None
    for url in _OSM_MIRRORS:
        try:
            r = requests.post(
                url,
                data={"data": _osm_query(min_lat, min_lng, max_lat, max_lng, min_kv)},
                headers={"User-Agent": "dchub-map/1.0 (+https://dchub.cloud)"},
                timeout=_OSM_TIMEOUT + 20,
            )
            if r.status_code == 200:
                body = r.text
                break
            last_err = f"{url.split('/')[2]} HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{url.split('/')[2]} {type(e).__name__}"
    if body is None:
        raise ValueError(f"overpass unavailable ({last_err})")

    doc = json.loads(body)
    feats = []
    for el in doc.get("elements") or []:
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        if len(geom) < 2:
            continue
        tags = el.get("tags") or {}
        volts = _osm_max_volts(tags.get("voltage"))
        if volts is None:
            continue
        kv = round(volts / 1000.0, 1)
        if min_kv and kv < min_kv:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[p["lon"], p["lat"]] for p in geom],
            },
            "properties": {
                "osm_id": el.get("id"),
                "voltage_kv": kv,
                "operator": tags.get("operator") or "",
                "cables": _osm_first_int(tags.get("cables")),
                "circuits": _osm_first_int(tags.get("circuits")),
                "frequency": tags.get("frequency") or "",
                "source": "OpenStreetMap",
            },
        })

    feats.sort(key=lambda f: f["properties"]["voltage_kv"], reverse=True)
    return json.dumps({
        "type": "FeatureCollection",
        "features": feats,
        "count": len(feats),
        "source": "OpenStreetMap (Overpass)",
        # ODbL requires attribution to travel WITH the data.
        "attribution": "© OpenStreetMap contributors",
        "_licence": "ODbL 1.0 — https://opendatacommons.org/licenses/odbl/",
    })


@global_infra_bp.route("/api/v1/infrastructure/osm-transmission", methods=["GET"])
def osm_transmission():
    """Transmission lines from OpenStreetMap for a viewport bbox.

    Complements the HIFLD layers rather than replacing them: HIFLD is the
    authority inside the US, OSM is the only open source with real geometry
    outside it.

    Query: bbox=minLng,minLat,maxLng,maxLat  (GeoJSON axis order)
           min_kv=<float, default 110>
    """
    raw = (request.args.get("bbox") or "").strip()
    parts = raw.split(",")
    if len(parts) != 4:
        return jsonify({"ok": False,
                        "error": "bbox=minLng,minLat,maxLng,maxLat required"}), 400
    try:
        min_lng, min_lat, max_lng, max_lat = (float(p) for p in parts)
    except ValueError:
        return jsonify({"ok": False, "error": "bbox values must be numbers"}), 400

    if not (-180 <= min_lng < max_lng <= 180 and -90 <= min_lat < max_lat <= 90):
        return jsonify({"ok": False,
                        "error": "bbox out of range or corners transposed"}), 400

    area = (max_lng - min_lng) * (max_lat - min_lat)
    if area > _OSM_MAX_AREA_DEG2:
        # Refuse loudly rather than hand Overpass a query it will 429/504 on.
        return jsonify({
            "ok": False,
            "error": (f"bbox area {area:.1f} deg² exceeds the "
                      f"{_OSM_MAX_AREA_DEG2} deg² cap — zoom in"),
            "max_area_deg2": _OSM_MAX_AREA_DEG2,
        }), 400

    try:
        min_kv = float(request.args.get("min_kv", 110))
    except ValueError:
        min_kv = 110.0

    # Round the bbox into the cache key so ordinary panning reuses one upstream
    # fetch instead of minting a key per pixel of movement.
    k = (f"osm_tx:{min_lat:.1f},{min_lng:.1f},{max_lat:.1f},{max_lng:.1f}"
         f":{min_kv:g}")
    return _cached(k, lambda: _build_osm_transmission(
        min_lat, min_lng, max_lat, max_lng, min_kv))


def register_global_infra(app):
    try:
        app.register_blueprint(global_infra_bp)
    except Exception as e:
        log.warning(f"global_infra registration: {e}")
