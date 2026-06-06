"""Minimal /api/epa/facilities shim (2026-06-06).

The land-power map's api-layers.js calls `/api/epa/facilities?lat&lng&radius`
and expects `{success, data:{facilities_in_radius, facilities_in_state,
total_in_state}}`. That route never existed → repeated 404s in the browser
console (and category Select-All retried it, multiplying the noise).

This returns the expected shape with a valid payload so the EPA layer degrades
cleanly (empty) instead of erroring. It never returns 4xx/5xx — the whole point
is to stop the console noise on our most-important surface.

TODO (follow-up): back this with real EPA FRS / ECHO facility data. That data is
CORS-blocked from the browser, so it must be proxied/cached server-side here.
"""
import logging

from flask import Blueprint, jsonify, request

log = logging.getLogger("epa_facilities")
epa_facilities_bp = Blueprint("epa_facilities", __name__)


@epa_facilities_bp.route("/api/epa/facilities", methods=["GET"])
def epa_facilities():
    """Return EPA facilities in the api-layers.js shape. Always 200."""
    try:
        lat = request.args.get("lat", type=float)
        lng = request.args.get("lng", type=float)
        radius = request.args.get("radius", 50, type=float) or 50
    except Exception:
        lat = lng = None
        radius = 50
    return jsonify({
        "success": True,
        "data": {
            "facilities_in_radius": [],
            "facilities_in_state": [],
            "total_in_state": 0,
        },
        "query": {"lat": lat, "lng": lng, "radius": radius},
        "source": "EPA",
        "note": "EPA facility dataset not yet loaded; layer intentionally empty.",
    }), 200


def register_epa_facilities(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(epa_facilities_bp)
    except Exception as e:  # already registered / name clash
        log.warning(f"epa_facilities blueprint registration: {e}")
