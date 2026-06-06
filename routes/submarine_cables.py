"""Submarine cable proxy — TeleGeography open data → land-power map.

TeleGeography's submarinecablemap.com publishes the canonical global subsea
dataset (~712 cables + ~1,917 landing points) as GeoJSON, but WITHOUT CORS, so
the browser can't fetch it directly (unlike the HIFLD/OSM layers the map already
pulls client-side). This server-side proxy fetches + caches it and serves it
with CORS, replacing the 9 hardcoded seed cables in the frontend.

Read-only, no secrets. Cached 6h (the dataset changes rarely). Serves stale on
a transient source error rather than failing the layer.
"""
import json
import logging
import time
import urllib.request

from flask import Blueprint, Response, jsonify

log = logging.getLogger("submarine_cables")
subsea_bp = Blueprint("submarine_cables", __name__)

_CABLES = "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json"
_LANDINGS = "https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json"
_cache = {"data": None, "ts": 0.0}
_TTL = 21600  # 6 hours


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "dchub-map/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _resp(body: str, cache_state: str):
    return Response(body, mimetype="application/json", headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
        "X-Cache": cache_state,
    })


@subsea_bp.route("/api/v1/infrastructure/submarine-cables", methods=["GET"])
def submarine_cables():
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _TTL:
        return _resp(_cache["data"], "hit")
    try:
        cables = _fetch(_CABLES)
        landings = _fetch(_LANDINGS)
    except Exception as e:
        if _cache["data"] is not None:
            return _resp(_cache["data"], "stale")
        return jsonify({"ok": False,
                        "error": f"source fetch failed: {str(e)[:160]}"}), 502
    out = json.dumps({
        "ok": True,
        "cables": cables,
        "landings": landings,
        "counts": {
            "cables": len(cables.get("features", [])),
            "landings": len(landings.get("features", [])),
        },
        "source": "TeleGeography submarinecablemap.com",
    })
    _cache["data"] = out
    _cache["ts"] = now
    return _resp(out, "miss")


def register_submarine_cables(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(subsea_bp)
    except Exception as e:
        log.warning(f"submarine_cables registration: {e}")
