"""
Flask Integration - Power Plant Enrichment Routes
==================================================
Drop-in Flask blueprint for your Replit backend.
Provides endpoints to trigger enrichment and check status.

Usage in your main app.py:
    from power_plant_enrichment.routes import enrichment_bp
    app.register_blueprint(enrichment_bp, url_prefix="/api/enrichment")
"""

import logging
import threading
from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request

from .eia_860m import EIA860MIngester
from .nccs_featureserver import NCCSFeatureServerClient
from .matcher import PlantMatcher

logger = logging.getLogger(__name__)

enrichment_bp = Blueprint("enrichment", __name__)

# In-memory job status tracking
_jobs = {}


def require_admin_key(f):
    """Simple API key auth for admin endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
        import os
        expected = os.environ.get("ADMIN_API_KEY", "")
        if not expected or key != expected:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# AUTO-REPAIR: duplicate route '/trigger' also in routes.py:44 — review and remove one
@enrichment_bp.route("/trigger", methods=["POST"])
@require_admin_key
def trigger_enrichment():
    """
    POST /api/enrichment/trigger
    Body (optional):
        {
            "sources": ["eia_860m", "nccs"],  // default: both
            "eia_year": 2025,
            "eia_month": 10,
            "state_filter": "VA",             // optional state filter
            "dry_run": true                   // preview without DB writes
        }
    """
    params = request.get_json(silent=True) or {}
    sources = params.get("sources", ["eia_860m", "nccs"])
    dry_run = params.get("dry_run", True)

    job_id = f"enrich_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    _jobs[job_id] = {
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "params": params,
        "progress": "Starting...",
    }

    # Run in background thread
    thread = threading.Thread(
        target=_run_enrichment,
        args=(job_id, sources, params, dry_run),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "started",
        "check_status": f"/api/enrichment/status/{job_id}",
    }), 202

# AUTO-REPAIR: duplicate route '/status/<job_id>' also in routes.py:85 — review and remove one

@enrichment_bp.route("/status/<job_id>", methods=["GET"])
@require_admin_key
def check_status(job_id):
    """GET /api/enrichment/status/<job_id>"""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)
# AUTO-REPAIR: duplicate route '/nccs/query' also in routes.py:95 — review and remove one


@enrichment_bp.route("/nccs/query", methods=["GET"])
@require_admin_key
def nccs_query():
    """
    GET /api/enrichment/nccs/query?state=VA&min_mw=100&fuel=NG
    Quick proxy to NCCS FeatureServer for ad-hoc queries.
    """
    client = NCCSFeatureServerClient()

    state = request.args.get("state")
    min_mw = request.args.get("min_mw", type=float)
    fuel = request.args.get("fuel")
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    radius = request.args.get("radius_miles", 50, type=float)
    limit = request.args.get("limit", 100, type=int)

    try:
        if lat and lng:
            plants = client.query_by_proximity(
                lat=lat, lng=lng,
                radius_miles=radius,
                state_filter=state,
                min_capacity_mw=min_mw,
                fuel_type=fuel,
                max_records=limit,
            )
        else:
            plants = client.query_power_plants(
                state_filter=state,
                min_capacity_mw=min_mw,
                fuel_type=fuel,
                max_records=limit,
            )

        return jsonify({
            "count": len(plants),
            "plants": plants,
        })

    except Exception as e:
        logger.error(f"NCCS query failed: {e}")
# AUTO-REPAIR: duplicate route '/nccs/layers' also in routes.py:140 — review and remove one
        return jsonify({"error": str(e)}), 500


@enrichment_bp.route("/nccs/layers", methods=["GET"])
@require_admin_key
def nccs_layers():
    """GET /api/enrichment/nccs/layers — Show available NCCS FeatureServer layers."""
    client = NCCSFeatureServerClient()
    try:
        info = client.get_service_info()
        layers = [
            {"id": l.get("id"), "name": l.get("name")}
            for l in info.get("layers", [])
        ]
        return jsonify({"layers": layers})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _run_enrichment(job_id: str, sources: list, params: dict, dry_run: bool):
    """Background enrichment pipeline."""
    try:
        eia_plants = []
        nccs_plants = []

        # --- Phase 1: Ingest from sources ---

        if "eia_860m" in sources:
            _jobs[job_id]["progress"] = "Downloading EIA plant data..."
            ingester = EIA860MIngester()
            try:
                use_annual = params.get("use_annual", True)
                eia_plants = ingester.ingest(
                    year=params.get("eia_year"),
                    month=params.get("eia_month"),
                    use_annual=use_annual,
                )
                _jobs[job_id]["eia_count"] = len(eia_plants)
            except Exception as e:
                logger.error(f"EIA ingestion failed: {e}")
                _jobs[job_id]["eia_error"] = str(e)

        if "nccs" in sources:
            _jobs[job_id]["progress"] = "Querying NASA NCCS FeatureServer..."
            client = NCCSFeatureServerClient()
            try:
                nccs_plants = client.query_power_plants(
                    state_filter=params.get("state_filter"),
                )
                _jobs[job_id]["nccs_count"] = len(nccs_plants)
            except Exception as e:
                logger.error(f"NCCS query failed: {e}")
                _jobs[job_id]["nccs_error"] = str(e)

        if not eia_plants and not nccs_plants:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = "No data from any source"
            return

        # --- Phase 2: Merge sources ---

        _jobs[job_id]["progress"] = "Merging data sources..."
        matcher = PlantMatcher()
        merged = matcher.merge_sources(eia_plants, nccs_plants)
        _jobs[job_id]["merged_count"] = len(merged)

        # --- Phase 3: Match against DC Hub DB ---

        _jobs[job_id]["progress"] = "Matching against DC Hub database..."

        # TODO: Replace with actual DB query
        # existing_plants = db.session.query(PowerPlant).all()
        existing_plants = []  # Placeholder

        results = matcher.match_against_dchub(merged, existing_plants)

        # --- Phase 4: Apply updates (if not dry run) ---

        if not dry_run and results["updates"]:
            _jobs[job_id]["progress"] = "Applying database updates..."
            # TODO: Implement actual DB writes
            # for update in results["updates"]:
            #     plant = db.session.query(PowerPlant).get(update["existing_id"])
            #     for field, value in update["updates"].items():
            #         setattr(plant, field, value)
            # db.session.commit()
            pass

        # --- Done ---

        _jobs[job_id].update({
            "status": "completed",
            "completed_at": datetime.utcnow().isoformat(),
            "progress": "Done",
            "dry_run": dry_run,
            "results": {
                "total_enriched": len(merged),
                "matched_existing": len(results["updates"]),
                "new_plants": len(results["new"]),
                "conflicts": len(results["conflicts"]),
                "stats": matcher.get_stats(),
            },
            # Include preview of first few updates/new plants
            "preview": {
                "updates": results["updates"][:10],
                "new_plants": [
                    {k: v for k, v in p.items() if not k.startswith("_")}
                    for p in results["new"][:10]
                ],
                "conflicts": results["conflicts"][:5],
            },
        })

    except Exception as e:
        logger.exception(f"Enrichment job {job_id} failed")
        _jobs[job_id].update({
            "status": "failed",
            "error": str(e),
            "completed_at": datetime.utcnow().isoformat(),
        })
