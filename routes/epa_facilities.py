"""Minimal /api/epa/facilities shim (2026-06-06).

The land-power map's api-layers.js calls `/api/epa/facilities?lat&lng&radius`
and expects `{success, data:{facilities_in_radius, facilities_in_state,
total_in_state}}`. That route never existed → repeated 404s in the browser
console (and category Select-All retried it, multiplying the noise).

This returns the expected shape with a valid payload so the EPA layer degrades
cleanly (empty) instead of erroring.

TIER GATING (2026-06-06): /api/epa/facilities is listed in LOCKED_GATE_MANIFEST
'pro' in main.py, but the route was returning 200 to anonymous callers because
no @require_plan decorator was attached. The tier-audit canary correctly flagged
it as UNGATED (CRITICAL). Added @require_plan('pro') with a defensive-import
fallback (same pattern as routes/pricing_connectivity_routes.py).

TODO (follow-up): back this with real EPA FRS / ECHO facility data. That data is
CORS-blocked from the browser, so it must be proxied/cached server-side here.
"""
import logging

from flask import Blueprint, jsonify, request

log = logging.getLogger("epa_facilities")

# Defensive import for tier gating (same pattern as
# routes/pricing_connectivity_routes.py). If api_tier_gating fails to load
# (partial deploy / dev env without the module), FAIL CLOSED — return 503
# rather than falling back to an ungated no-op. The endpoint is listed as
# 'pro' in LOCKED_GATE_MANIFEST so any leak is a security regression.
try:
    from api_tier_gating import require_plan
except Exception as _e:  # pragma: no cover
    log.warning(
        "epa_facilities: require_plan unavailable (%s) — endpoint will "
        "fail-closed with 503 until api_tier_gating loads cleanly.", _e
    )
    def require_plan(plan):  # type: ignore[no-redef]
        def _decorator(fn):
            def _wrapped(*a, **kw):
                return jsonify(ok=False, error="gate_not_wired",
                               hint="Tier-gating not loaded."), 503
            _wrapped.__name__ = getattr(fn, "__name__", "_wrapped")
            return _wrapped
        return _decorator

epa_facilities_bp = Blueprint("epa_facilities", __name__)


@epa_facilities_bp.route("/api/epa/facilities", methods=["GET"])
@require_plan('pro')
def epa_facilities():
    """Return EPA facilities in the api-layers.js shape. PRO-tier gated."""
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
