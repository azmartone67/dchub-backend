"""Scaffolded stubs — replace 501 with real impl when data sources are wired."""
from flask import Blueprint, jsonify, request

stubs_v3 = Blueprint("stubs_v3", __name__)

@stubs_v3.route("/api/v1/powered-shell/markets", methods=["GET"])
def powered_shell_markets():
    return jsonify({
        "error": "not_implemented",
        "ticket": "#35",
        "message": "Endpoint scaffolded; data source integration pending."
    }), 501

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
