"""Submarine cable proxy — TeleGeography open data → land-power map.

TeleGeography's submarinecablemap.com publishes the canonical global subsea
dataset as GeoJSON, but WITHOUT CORS, so the browser can't fetch it directly
(unlike the HIFLD/OSM layers the map already pulls client-side). This
server-side proxy fetches + caches it and serves it with CORS, replacing the 9
hardcoded seed cables in the frontend.

Read-only, no secrets. Cached 6h (the dataset changes rarely). Serves stale on
a transient source error rather than failing the layer.

★ 2026-07-29 — `counts.cables` WAS A ROUTE-SEGMENT COUNT PUBLISHED AS A CABLE
  COUNT, on a keyless public endpoint. It was `len(features)` = 717. Measured
  on the live upstream payload the same day: 717 features, but only 696 distinct
  properties.id and 696 distinct properties.name; 20 ids carry MORE THAN ONE
  feature (echo has 3; rising-8, apricot, topaz, connected-coast, medloop,
  medusa-submarine-cable-system and tanjung-pandan-sungai-kakap have 2 each);
  every geometry is a MultiLineString. A cable that lands in three places
  arrives as three features. This is the same class of error as the documented
  hosting_capacity "rows = GIS vertices" trap: the row count is a geometry
  artifact, not a population.
  `counts.cables` is now the DISTINCT-cable count and `counts.cable_features`
  carries the segment count, so the number that was being published is still
  observable under a name that says what it is. `counts.landings` was already
  correct — 1,918 features = 1,918 distinct ids, verified the same way.
  The docstring's own "~712 cables + ~1,917 landing points" was also wrong and
  is deleted rather than corrected: this module proxies live upstream data, so
  it should not carry a hardcoded population at all. Read counts_basis instead.
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


def _distinct_feature_ids(fc: dict) -> int:
    """Count DISTINCT feature ids in a GeoJSON FeatureCollection.

    len(features) is a SEGMENT count: TeleGeography ships one MultiLineString
    feature per route leg, so a multi-landing cable appears several times under
    one id. Falls back to `name` when a feature carries no id, and counts an
    unidentifiable feature as its own cable — that can only over-count, never
    silently merge two distinct cables into one.
    """
    seen = set()
    unidentified = 0
    for f in (fc.get("features") or []):
        props = f.get("properties") or {}
        key = props.get("id") or props.get("name")
        if key:
            seen.add(str(key))
        else:
            unidentified += 1
    return len(seen) + unidentified


def build_counts(cables: dict, landings: dict) -> dict:
    """Build the published `counts` + `counts_basis` for one upstream payload.

    PURE: no network, no Flask, no module state — so
    tests/test_subsea_wrong_table_and_segment_counts.py drives the real shipped
    function against a synthetic FeatureCollection instead of asserting on
    source text. Split out of the route for exactly that reason.
    """
    return {
        "counts": {
            # ★ DISTINCT cables, not features. See the module docstring.
            "cables": _distinct_feature_ids(cables),
            "cable_features": len(cables.get("features") or []),
            "landings": _distinct_feature_ids(landings),
            "landing_features": len(landings.get("features") or []),
        },
        "counts_basis": {
            "cables": "distinct properties.id across the cable "
                      "FeatureCollection — one per cable SYSTEM.",
            "cable_features": "raw GeoJSON feature count — one per route "
                              "SEGMENT. Larger than `cables` because "
                              "TeleGeography emits one MultiLineString per "
                              "route leg, so a multi-landing cable repeats "
                              "under one id. Do NOT publish this as a cable "
                              "count.",
            "landings": "distinct properties.id across the landing-point "
                        "FeatureCollection — one per landing point.",
            "landing_features": "raw GeoJSON feature count for landing points. "
                                "Equal to `landings` in every observation so "
                                "far (1,918 == 1,918 on 2026-07-29), but "
                                "published separately so a future divergence "
                                "is visible instead of silent.",
            "unit_note": "counts describe the LIVE UPSTREAM payload in this "
                         "response, not DC Hub's stored snapshot. DC Hub's own "
                         "tables held 691 cables / 1,908 landing points as of "
                         "2026-03-27 — see /api/v1/submarine-cables.",
        },
    }


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
        "source": "TeleGeography submarinecablemap.com",
        **build_counts(cables, landings),
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
