"""
DC Hub — reVeal Cell endpoint (v2)
/api/v1/reveal-cell

Change in v2:
  - Extracted core computation into compute_reveal_cell() as a pure function
    that can be imported and called directly (no Flask context, no HTTP).
  - Removed HTTP sub-endpoint calls that deadlocked under single-worker gunicorn.
    Sub-endpoint variables (queue, permitting, water, fiber) are still listed
    in missing_from_this_cell for transparency. If you want them populated,
    deploy those endpoints separately; this module doesn't self-call.

Purpose
-------
Accepts a 5 km (configurable) cell reference and returns a single
reVeal-shaped feature row: all the variables DC Hub can compute directly
from its grid + renewable + tax tables, plus an explicit list of which
slide-25 limitations this cell covers.

Designed to be drop-in callable from reveal-cell-bulk for fast multi-cell
queries.
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

def _safe_import_helpers():
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
        logger.warning("reveal_cell _safe_import_helpers: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Core computation — pure function, importable from other modules
# ---------------------------------------------------------------------------

def compute_reveal_cell(lat, lon, cell_size_km=5, state=None, capacity_mw=100):
    """Compute the reveal-cell payload for a single cell.

    Pure function — no Flask request context, no HTTP. Can be called directly
    from reveal-cell-bulk or any other module that needs per-cell features.

    Returns a dict matching the /api/v1/reveal-cell response shape.
    """
    lat = float(lat)
    lon = float(lon)
    cell_size_km = float(cell_size_km)
    capacity_mw = float(capacity_mw)
    state = state.upper() if state else None

    helpers = _safe_import_helpers()
    features = {}
    provenance = {}
    missing = []

    # --- 1. Transmission hosting (slide-25 #1) -----------------------------
    if helpers:
        radius_km = max(cell_size_km * 2.5, 20)
        try:
            subs = helpers["_query_substations"](lat, lon, state, radius_km=radius_km)
        except Exception as exc:
            logger.debug("compute_reveal_cell substations error at (%s,%s): %s", lat, lon, exc)
            subs = []
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

    # --- 2. Renewables + geothermal (slide-25 #4 partial) ------------------
    if helpers:
        ghi = helpers["SOLAR_GHI"].get(state, 4.5) if state else 4.5
        wind_cls = helpers["WIND_CLASS"].get(state, 3) if state else 3
        features["solar_score"] = helpers["_solar_score"](ghi)
        features["wind_score"] = helpers["_wind_score"](wind_cls)
        try:
            geo_zones = helpers["_nearest_geothermal"](lat, lon, 500)
        except Exception:
            geo_zones = []
        features["geothermal_score"] = geo_zones[0]["effective_score"] if geo_zones else 0
        provenance["renewables"] = "NREL state resource atlas + DC Hub geothermal zones"
    else:
        missing.extend(["solar_score", "wind_score", "geothermal_score"])

    # --- 3. Tax incentives -------------------------------------------------
    if helpers and state:
        features["tax_incentive_score"] = helpers["TAX_INCENTIVE_SCORES"].get(state, 70)
        provenance["tax"] = "DC Hub state-level lookup (+ IRA overlay)"
    else:
        missing.append("tax_incentive_score")

    # --- 4. Optional sub-endpoint features — NOT computed here -------------
    # These come from separate endpoints (interconnection-queue, air-permitting,
    # water-risk, fiber-intel). Calling them from here causes gunicorn
    # worker deadlocks under load. If they exist on this server, they can
    # be called separately and merged client-side; we report them as missing
    # here so the bulk caller knows the cell-level story is incomplete.
    for k in ("queue_posture_months", "air_permit_score", "water_risk_score",
              "fiber_proximity_km"):
        if k not in features:
            missing.append(k.replace("_score", "").replace("_months", ""))

    # --- 5. Composite suitability (0-1) ------------------------------------
    pieces = []
    weights = []

    def _push(key, weight, normaliser=None):
        v = features.get(key)
        if v is None:
            return
        if normaliser:
            n = normaliser(v)
        else:
            n = max(0.0, min(1.0, v / 100.0))
        pieces.append(n)
        weights.append(weight)

    if "transmission_hosting_mw" in features:
        pieces.append(max(0.0, min(1.0, features["transmission_hosting_mw"] / 2000.0)))
        weights.append(0.35)
    _push("tax_incentive_score", 0.20)
    _push("solar_score", 0.15)
    _push("wind_score", 0.15)
    _push("geothermal_score", 0.05)

    if pieces and sum(weights) > 0:
        composite = round(sum(p * w for p, w in zip(pieces, weights)) / sum(weights), 3)
    else:
        composite = None

    # --- 6. Coverage (slide-25 limitations this cell closes) ---------------
    covers = {
        "transmission_hosting": "transmission_hosting_mw" in features,
        "demand_queue_time":    False,  # not computed in direct mode
        "zoning_permitting":    False,  # not computed in direct mode
        "capacity_scaling":     all(k in features for k in ("solar_score", "wind_score", "geothermal_score")),
    }

    # --- 7. Confidence based on features-present ---------------------------
    expected_core = ["transmission_hosting_mw", "solar_score", "wind_score",
                     "geothermal_score", "tax_incentive_score"]
    present = sum(1 for k in expected_core if k in features)
    confidence = round(present / len(expected_core), 2)

    return {
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
        "suitability_composite": composite,
        "confidence": confidence,
        "capacity_mw_analyzed": capacity_mw,
        "data_provenance": provenance,
        "source": "DC Hub reveal-cell  \u00B7  direct-compute core feature row",
    }


# ---------------------------------------------------------------------------
# Flask view — thin wrapper over compute_reveal_cell
# ---------------------------------------------------------------------------

@reveal_bp.route("/api/v1/reveal-cell")
def reveal_cell():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        cell_size_km = float(request.args.get("cell_size_km", 5))
        state = request.args.get("state")
        capacity_mw = float(request.args.get("capacity_mw", 100))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": f"lat and lon required as floats: {e}"}), 400

    result = compute_reveal_cell(lat, lon, cell_size_km, state, capacity_mw)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_reveal_cell_routes(app):
    app.register_blueprint(reveal_bp)
    logger.info("\U0001F6F0  reVeal Cell route registered:")
    logger.info("  GET /api/v1/reveal-cell?lat=..&lon=..&cell_size_km=5&state=..")
    print("\U0001F6F0  reVeal Cell: Registered (1 route, importable compute_reveal_cell)")
    print("  GET /api/v1/reveal-cell?lat=..&lon=..&cell_size_km=5&state=..")
