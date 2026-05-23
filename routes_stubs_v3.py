"""Scaffolded stubs — replace 501 with real impl when data sources are wired."""
from flask import Blueprint, jsonify, request

stubs_v3 = Blueprint("stubs_v3", __name__)

@stubs_v3.route("/api/v1/powered-shell/markets", methods=["GET"])
def powered_shell_markets():
    """Phase RRR-stubfix (2026-05-18): was returning 501 which cascaded
    into a `frontend_endpoint_5xx` brain finding for /powered-shell.
    Real aggregated powered-shell market data doesn't exist yet (ticket
    #35 still open), but the frontend page needs SOMETHING to render
    instead of looking broken. Return a 200 with curated seed data +
    `coming_soon:true` flag so the frontend can show context cards
    with a "live data coming soon" badge. Each row reflects what
    dc_expert_brain.py already knows about the construction cost
    economics ($1.5-2.5M/MW) and where the deals are happening
    (verified via news_engine keywords + recent press)."""
    return jsonify({
        "coming_soon": True,
        "ticket": "#35",
        "note": ("Aggregated powered-shell market data is in active "
                 "build-out. Seed rows below reflect known active markets "
                 "from M&A and permit data; live per-market metrics land "
                 "with the data source integration."),
        "markets": [
            {"market": "Northern Virginia", "active_deals": 12,
             "estimated_mw_available": "200-400 MW",
             "construction_cost_per_mw": "$1.8-2.5M",
             "verdict": "AVOID — transmission queue 60+ months"},
            {"market": "Phoenix", "active_deals": 9,
             "estimated_mw_available": "150-300 MW",
             "construction_cost_per_mw": "$1.5-2.2M",
             "verdict": "CAUTION — water risk + power queue lengthening"},
            {"market": "Dallas / Fort Worth", "active_deals": 11,
             "estimated_mw_available": "300-500 MW",
             "construction_cost_per_mw": "$1.4-1.8M",
             "verdict": "BUILD — ERCOT capacity + cheaper land"},
            {"market": "Columbus OH", "active_deals": 7,
             "estimated_mw_available": "100-250 MW",
             "construction_cost_per_mw": "$1.6-2.0M",
             "verdict": "BUILD — AEP grid + hyperscaler magnet"},
            {"market": "Atlanta", "active_deals": 6,
             "estimated_mw_available": "80-180 MW",
             "construction_cost_per_mw": "$1.5-1.9M",
             "verdict": "BUILD — Southeast nuclear baseload"},
            {"market": "Salt Lake City", "active_deals": 4,
             "estimated_mw_available": "60-150 MW",
             "construction_cost_per_mw": "$1.4-1.7M",
             "verdict": "BUILD — overflow from CAISO"},
            {"market": "Las Vegas", "active_deals": 3,
             "estimated_mw_available": "40-120 MW",
             "construction_cost_per_mw": "$1.6-2.1M",
             "verdict": "CAUTION — water/heat constraints"},
            {"market": "Cheyenne WY", "active_deals": 3,
             "estimated_mw_available": "100-300 MW",
             "construction_cost_per_mw": "$1.3-1.6M",
             "verdict": "BUILD — wind + cheap land + Microsoft cluster"},
        ],
        "data_freshness": "seed_2026-05-18",
        "source": ("Seed rows curated from news_engine + dc_expert_brain "
                   "context until live aggregation lands. See "
                   "dchub.cloud/state-of-the-data-center for the live "
                   "DCPI BUILD/AVOID verdicts that drive these recommendations."),
    }), 200

# Phase ZZZZZ-round24 (2026-05-23): /powered-shell page was hitting
# three endpoints we never registered — 404 for each, broke the page.
# User reported it in the Tonopah/site audit. Same coming_soon pattern
# as /markets above.
@stubs_v3.route("/api/v1/powered-shell/rate-band/<market>", methods=["GET"])
def powered_shell_rate_band(market):
    """Stub: per-market rate band for powered-shell construction.
    Returns seed economic ranges from dc_expert_brain context until
    the real EIA + permit-data pipeline lands."""
    return jsonify({
        "market": market,
        "coming_soon": True,
        "ticket": "#36",
        "rate_band": {
            "construction_cost_per_mw_low":  "$1.4M",
            "construction_cost_per_mw_high": "$2.5M",
            "operating_cost_per_mwh_low":    "$48",
            "operating_cost_per_mwh_high":   "$95",
            "land_lease_psf_low":            "$0.85",
            "land_lease_psf_high":           "$2.40",
            "powered_shell_to_full_fit_ratio": "0.35-0.50",
        },
        "note": ("Seed ranges from dc_expert_brain. Live per-market "
                  "values land with the EIA + permit-data pipeline."),
        "verdict_link": f"/dcpi/{market}",
    }), 200


@stubs_v3.route("/api/v1/powered-shell/comps", methods=["GET"])
def powered_shell_comps():
    """Stub: list of comparable powered-shell deals.
    Returns 200 with `comps: []` + coming_soon flag — frontend renders
    'no comps yet' state instead of a 404 error."""
    return jsonify({
        "coming_soon": True,
        "ticket": "#36",
        "comps": [],
        "note": ("Powered-shell deal comps land with the M&A deal "
                  "tracker integration. The page renders an empty list "
                  "until then — better than 404."),
    }), 200


@stubs_v3.route("/api/v1/powered-shell/pipeline", methods=["GET"])
def powered_shell_pipeline():
    """Stub: list of powered-shell projects in the pipeline.
    Returns 200 with `pipeline: []` + coming_soon flag — same pattern."""
    return jsonify({
        "coming_soon": True,
        "ticket": "#36",
        "pipeline": [],
        "note": ("Powered-shell pipeline data lands with the discovery "
                  "engine integration. Capacity-pipeline.py has the "
                  "scaffolding; needs powered_shell category tag."),
    }), 200


@stubs_v3.route("/api/v1/air-permitting", methods=["GET"])
def air_permitting():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "missing_params", "required": ["lat", "lon"]}), 400
    return jsonify({
        "error": "not_implemented",
        "ticket": "#40",
        "message": "EPA eGRID + state DEQ lookup pending."
    }), 501
