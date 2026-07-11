"""Parcel-boundary lookup — pillar: parcel GIS sourcing (pilot 2026-07-11).

Serves the parcel_boundaries PostGIS table (free county/state open-data
layers; pilot = Loudoun County VA, 132K polygons from the county's public
Parcels_LatLong FeatureServer). An agent gives a point, gets back the parcel
that contains it — parcel id, acreage, county attrs, and (optionally) the
polygon itself in the exact GeoJSON shape analyze_parcel accepts, so the
existing analyze_parcel -> analyze_site rail lights up on hosted data
instead of requiring the caller to bring a polygon.

Scope honesty (proven 2026-07-06, do not re-litigate): ISO queue survivors
carry NO parcel identity and are mostly county-centroid geocoded, so queue
rows can NEVER auto-join to these polygons. This surface serves agents that
HAVE a location (a real site, an address geocode, a map click).

License note: sources are public county/state open-data layers; we serve
derived metrics + per-lookup geometry, never bulk export (limit 1 parcel per
call; no bbox/scan endpoint) — consistent with the open-data postures and
deliberately useless for bulk extraction.
"""
import os

from flask import Blueprint, jsonify, request

import psycopg2
import psycopg2.extras

parcels_bp = Blueprint("parcels", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL", "")


def _conn():
    return psycopg2.connect(_dsn())


@parcels_bp.route("/api/v1/parcels/lookup", methods=["GET"])
def parcels_lookup():
    """Point -> containing parcel. ?lat=&lng=[&include_geometry=1]"""
    try:
        lat = float(request.args.get("lat", ""))
        lng = float(request.args.get("lng", ""))
    except (TypeError, ValueError):
        return jsonify({
            "error": "lat and lng are required, e.g. /api/v1/parcels/lookup?lat=39.0437&lng=-77.4875",
        }), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "lat/lng out of range"}), 400
    want_geom = request.args.get("include_geometry", "") in ("1", "true", "yes")

    try:
        conn = _conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT source, state, county, county_fips, parcel_id,
                      acres_gis, acres_legal, attrs,
                      ST_AsGeoJSON(geom, 6)::json AS geometry
               FROM parcel_boundaries
               WHERE geom && ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                 AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               LIMIT 1""",
            (lng, lat, lng, lat))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        return jsonify({"error": "parcel lookup unavailable",
                        "detail": str(e)[:200]}), 503

    if not row:
        return jsonify({
            "found": False,
            "note": ("No hosted parcel contains this point. Coverage is "
                     "rolling out by market — see /api/v1/parcels/coverage. "
                     "If you HAVE a boundary, POST it to /api/v1/analyze-parcel."),
            "coverage_url": "/api/v1/parcels/coverage",
        })

    out = {
        "found": True,
        "parcel_id": row["parcel_id"],
        "state": row["state"],
        "county": row["county"],
        "county_fips": row["county_fips"],
        "acres_gis": row["acres_gis"],
        "acres_legal": row["acres_legal"],
        "attrs": row["attrs"],
        "source": row["source"],
        "data_basis": ("county/state public GIS parcel layer, hosted by "
                       "DC Hub; acreage as published by the source"),
        # deterministic rail: the exact next calls, pre-populated
        "site_evaluation_handoff": {
            "analyze_parcel": {"geometry": ("<included below>" if want_geom
                                            else "re-request with include_geometry=1")},
            "analyze_site": {"lat": lat, "lon": lng},
            "get_water_risk": {"lat": lat, "lng": lng},
        },
    }
    if want_geom:
        out["geometry"] = row["geometry"]
    return jsonify(out)


@parcels_bp.route("/api/v1/parcels/coverage", methods=["GET"])
def parcels_coverage():
    """Which markets have hosted parcel boundaries, and how many."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT state, county, source, COUNT(*) AS parcels,
                      MAX(ingested_at) AS last_ingest
               FROM parcel_boundaries
               GROUP BY state, county, source
               ORDER BY parcels DESC""")
        rows = [{"state": s, "county": c, "source": src,
                 "parcels": int(n), "last_ingest": (ts.isoformat() if ts else None)}
                for (s, c, src, n, ts) in cur.fetchall()]
        conn.close()
    except Exception as e:
        return jsonify({"error": "coverage unavailable", "detail": str(e)[:200]}), 503
    return jsonify({
        "markets": rows,
        "total_parcels": sum(r["parcels"] for r in rows),
        "note": ("Free county/state open-data parcel layers hosted for "
                 "point->parcel lookup (/api/v1/parcels/lookup). Rollout "
                 "order follows data-center market priority."),
    })
