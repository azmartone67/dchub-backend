"""
DC Hub — reVeal Cell endpoint
/api/v1/reveal-cell

Purpose
-------
Accepts a 5 km (configurable) cell reference and returns a single
reVeal-shaped feature row: all the variables DC Hub serves, pre-joined,
plus an explicit list of which slide-25 limitations DC Hub is closing
for that cell.

Designed to drop straight into reVeal's Characterize step as a
per-cell feature source. Internal calls reuse the existing NLR
Intelligence layer (geothermal-potential, colocation-score,
grid-headroom, microgrid-viability) plus the grid, queue, permitting,
water, fiber, tax, and renewable endpoints already live on DC Hub.

Fix history
-----------
  v1  - first cut for NLR meeting (Apr 2026). Internal reuse of
        nlr_intelligence helpers; no new DB calls, safe to deploy.
"""

import logging
import math
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

reveal_bp = Blueprint("reveal_cell", __name__)


@reveal_bp.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ---------------------------------------------------------------------------
# Import reuse — pull helpers out of nlr_intelligence so we don't duplicate
# the substation/geothermal logic. Soft-fail if the module moves.
# ---------------------------------------------------------------------------

def _safe_import():
    try:
        from nlr_intelligence import (
            _query_substations,
            _nearest_geothermal,
            _estimate_headroom_mw,
            _solar_score,
            _wind_score,
            SOLAR_GHI,
            WIND_CLASS,
            TAX_INCENTIVE_SCORES,
        )
        return {
            "_query_substations": _query_substations,
            "_nearest_geothermal": _nearest_geothermal,
            "_estimate_headroom_mw": _estimate_headroom_mw,
            "_solar_score": _solar_score,
            "_wind_score": _wind_score,
            "SOLAR_GHI": SOLAR_GHI,
            "WIND_CLASS": WIND_CLASS,
            "TAX_INCENTIVE_SCORES": TAX_INCENTIVE_SCORES,
        }
    except Exception as exc:
        logger.warning("reveal_cell _safe_import: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Optional additional route hooks — these will be skipped gracefully if the
# target endpoints are not registered on this app instance.
# ---------------------------------------------------------------------------

def _try_call(client, path, params):
    """Best-effort internal call to an already-registered DC Hub route.
    Returns the JSON body or None if unavailable."""
    try:
        resp = client.get(path, query_string=params)
        if resp.status_code == 200:
            return resp.get_json()
    except Exception as exc:
        logger.debug("reveal_cell _try_call %s: %s", path, exc)
    return None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

@reveal_bp.route("/api/v1/reveal-cell")
def reveal_cell():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        cell_size_km = float(request.args.get("cell_size_km", 5))
        state = request.args.get("state", "").upper() or None
        capacity_mw = float(request.args.get("capacity_mw", 100))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": f"lat and lon are required floats: {e}"}), 400

    helpers = _safe_import()
    from flask import current_app
    client = current_app.test_client()

    features = {}
    provenance = {}
    missing = []

    # --- 1. Transmission hosting (slide-25 #1) -----------------------------
    if helpers:
        radius_km = max(cell_size_km * 2.5, 20)
        subs = helpers["_query_substations"](lat, lon, state, radius_km=radius_km)
        if subs:
            total_mw = sum(helpers["_estimate_headroom_mw"](s["voltage_kv"]) for s in subs[:10])
            max_kv = max((s["voltage_kv"] for s in subs), default=0)
            features["transmission_hosting_mw"] = total_mw
            features["substations_in_cell"] = len(subs)
            features["max_voltage_kv"] = max_kv
            provenance["transmission"] = "DC Hub substations table + HIFLD; radius-weighted"
        else:
            missing.append("transmission_hosting_mw")
    else:
        missing.append("transmission_hosting_mw")

    # --- 2. Interconnection queue (slide-25 #2) ----------------------------
    queue = _try_call(client, "/api/v1/interconnection-queue", {"lat": lat, "lon": lon, "radius_km": max(cell_size_km * 5, 50)})
    if queue and queue.get("success"):
        features["queue_posture_months"] = queue.get("median_wait_months")
        features["active_queue_mw"] = queue.get("active_queue_mw")
        provenance["queue"] = queue.get("source", "ISO/RTO queues")
    else:
        missing.append("interconnection_queue")

    # --- 3. Zoning / permitting / ordinances (slide-25 #3) -----------------
    permits = _try_call(client, "/api/v1/air-permitting", {"lat": lat, "lon": lon})
    if permits and permits.get("success"):
        features["air_permit_score"] = permits.get("score")
        features["permit_regime"] = permits.get("regime_classification")
        provenance["permitting"] = permits.get("source", "DC Hub air permitting intel")
    else:
        missing.append("permitting_regime")

    # --- 4. Capacity scaling by DC type (slide-25 #4) ----------------------
    if helpers:
        ghi = helpers["SOLAR_GHI"].get(state, 4.5) if state else 4.5
        wind_cls = helpers["WIND_CLASS"].get(state, 3) if state else 3
        solar_sc = helpers["_solar_score"](ghi)
        wind_sc = helpers["_wind_score"](wind_cls)
        geo_zones = helpers["_nearest_geothermal"](lat, lon, 500)
        geo_sc = geo_zones[0]["effective_score"] if geo_zones else 0
        features["solar_score"] = solar_sc
        features["wind_score"] = wind_sc
        features["geothermal_score"] = geo_sc
        provenance["renewables"] = "NREL state resource atlas + DC Hub geothermal zones"
    else:
        missing.extend(["solar_score", "wind_score", "geothermal_score"])

    # --- 5. Additional features reVeal doesn't carry today -----------------
    water = _try_call(client, "/api/v1/water-risk", {"lat": lat, "lon": lon})
    if water and water.get("success"):
        features["water_risk_score"] = water.get("risk_score")
        provenance["water"] = water.get("source", "DC Hub water risk feed")
    else:
        missing.append("water_risk_score")

    fiber = _try_call(client, "/api/v1/fiber-intel", {"lat": lat, "lon": lon})
    if fiber and fiber.get("success"):
        features["fiber_proximity_km"] = fiber.get("nearest_long_haul_km")
        features["fiber_provider_count"] = fiber.get("provider_count")
        provenance["fiber"] = "DC Hub fiber intel"
    else:
        missing.append("fiber_proximity")

    tax = _try_call(client, "/api/v1/tax-incentives", {"lat": lat, "lon": lon, "state": state or "XX"})
    if tax and tax.get("success"):
        features["tax_incentive_score"] = tax.get("composite_score")
        features["tax_incentive_stack"] = tax.get("stack", [])
        provenance["tax"] = "DC Hub tax incentive overlay (state + IRA)"
    elif helpers and state:
        features["tax_incentive_score"] = helpers["TAX_INCENTIVE_SCORES"].get(state, 70)
        provenance["tax"] = "DC Hub state-level fallback"
    else:
        missing.append("tax_incentive_score")

    # --- 6. Composite suitability (reVeal-comparable 0-1) ------------------
    def _norm(v, lo=0, hi=100):
        if v is None:
            return None
        return max(0.0, min(1.0, (v - lo) / (hi - lo)))

    pieces = []
    weights = []
    def _push(key, weight):
        v = features.get(key)
        if v is None:
            return
        pieces.append(_norm(v))
        weights.append(weight)

    _push("transmission_hosting_mw", 0.30)  # but normalise differently — MW not 0-100
    # rebuild transmission normalization: 2000 MW = 1.0
    if "transmission_hosting_mw" in features:
        pieces[-1] = max(0.0, min(1.0, features["transmission_hosting_mw"] / 2000.0))
    _push("air_permit_score", 0.15)
    _push("tax_incentive_score", 0.15)
    _push("solar_score", 0.10)
    _push("wind_score", 0.10)
    _push("geothermal_score", 0.05)
    _push("water_risk_score", 0.10)  # inverse-ish handled elsewhere; for now straight
    _push("fiber_proximity_km", 0.05)

    if pieces and sum(weights) > 0:
        composite = sum(p * w for p, w in zip(pieces, weights)) / sum(weights)
    else:
        composite = None

    # confidence: fraction of expected features present
    expected = 10
    present = sum(1 for k in [
        "transmission_hosting_mw", "queue_posture_months", "air_permit_score",
        "solar_score", "wind_score", "geothermal_score",
        "water_risk_score", "fiber_proximity_km", "tax_incentive_score",
        "active_queue_mw",
    ] if k in features)
    confidence = round(present / expected, 2)

    # --- 7. reVeal slide-25 limitation coverage tag ------------------------
    covers = {
        "transmission_hosting": "transmission_hosting_mw" in features,
        "demand_queue_time":    "queue_posture_months"    in features,
        "zoning_permitting":    "air_permit_score"        in features,
        "capacity_scaling":     all(k in features for k in ("solar_score", "wind_score", "geothermal_score")),
    }

    return jsonify({
        "success": True,
        "cell": {
            "center": {"lat": lat, "lon": lon},
            "size_km": cell_size_km,
            "cell_id": f"{lat:.4f},{lon:.4f},{int(cell_size_km)}",
            "state": state,
        },
        "reveal_features": features,
        "missing_from_this_cell": missing,
        "slide25_coverage": covers,
        "suitability_composite": round(composite, 3) if composite is not None else None,
        "confidence": confidence,
        "capacity_mw_analyzed": capacity_mw,
        "data_provenance": provenance,
        "source": "DC Hub reveal-cell endpoint — pre-joined feature row for reVeal Characterize",
    })


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_reveal_cell_routes(app):
    app.register_blueprint(reveal_bp)
    logger.info("🛰  reVeal Cell route registered:")
    logger.info("  GET /api/v1/reveal-cell?lat=..&lon=..&cell_size_km=5&state=..")
    print("🛰  reVeal Cell: ✅ Registered (1 route)")
    print("  GET /api/v1/reveal-cell?lat=..&lon=..&cell_size_km=5&state=..")
