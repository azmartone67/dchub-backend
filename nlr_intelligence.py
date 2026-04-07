"""
nlr_intelligence.py  —  DC Hub NLR/reVeal Partner Intelligence Layer
New endpoints purpose-built to be the ideal data partner for NLR (formerly NREL)
and any reVeal-style renewable + data center co-location siting platform.

Endpoints:
  GET /api/v1/geothermal-potential       — geothermal resource score by lat/lon
  GET /api/v1/colocation-score           — DC + renewable co-location composite
  GET /api/v1/grid-headroom              — estimated available MW at nearby substations
  GET /api/v1/microgrid-viability        — on-site generation & microgrid opportunity
"""

import logging
import math
from flask import Blueprint, request, jsonify

logger = logging.getLogger("nlr-intelligence")

nlr_bp = Blueprint("nlr_intelligence", __name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def _get_db_safe():
    """Try to get DB connection, return None if unavailable."""
    try:
        from db_utils import get_db
        return get_db()
    except Exception:
        return None

def _query_safe(conn, sql, params=()):
    """Run a query and return rows, or [] on failure."""
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception as e:
        logger.warning(f"Query failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return []

# ---------------------------------------------------------------------------
# Geothermal zones (USGS EGS potential + known hydrothermal regions)
# Covers high-value zones relevant to NLR/NREL research areas
# ---------------------------------------------------------------------------
GEOTHERMAL_ZONES = [
    # (lat, lon, name, type, potential_score, notes)
    (43.7, -110.5,  "Yellowstone Caldera",         "hydrothermal",  95, "World-class hydrothermal; NPS restricted"),
    (38.8, -122.8,  "The Geysers CA",               "hydrothermal",  98, "Largest geothermal complex in world, ~750 MW"),
    (38.2, -118.5,  "Long Valley Caldera CA",        "hydrothermal",  88, "Active caldera, significant EGS potential"),
    (44.5, -114.0,  "Snake River Plain ID",          "egs",           82, "High heat flow, EGS target zone"),
    (40.7, -112.1,  "Great Salt Lake Basin UT",      "hydrothermal",  78, "Roosevelt Hot Springs + Cove Fort area"),
    (38.5, -112.5,  "Milford UT",                    "hydrothermal",  85, "Blundell geothermal plant, active"),
    (35.2, -106.6,  "Valles Caldera NM",             "hydrothermal",  80, "High temp gradient, NM Tech research"),
    (39.7, -105.2,  "Colorado Front Range",          "egs",           62, "Moderate EGS, NLR/NREL research zone"),
    (36.1, -115.2,  "Nevada Basin & Range",          "hydrothermal",  88, "Brady, Beowawe, Dixie Valley plants"),
    (42.5, -117.0,  "Nevada-Oregon Border",          "hydrothermal",  84, "Neal Hot Springs + Raft River area"),
    (32.5, -106.5,  "Southern NM Rio Grande Rift",   "egs",           72, "High heat flow rift zone"),
    (46.8, -121.7,  "Cascade Volcanic Arc WA",       "hydrothermal",  70, "Mount Rainier zone, exploration stage"),
    (37.8, -121.2,  "SF Bay Area CA",                "egs",           58, "EGS research, proximity to grid"),
    (34.0, -118.2,  "Los Angeles Basin CA",          "egs",           45, "Shallow EGS potential, urban constraints"),
    (47.6, -122.3,  "Pacific NW Seattle",            "egs",           40, "Low gradient, limited potential"),
    (41.8, -87.6,   "Chicago IL",                    "egs",           25, "Very low geothermal gradient"),
    (40.7, -74.0,   "New York NY",                   "egs",           22, "Low gradient, high cost urban"),
    (30.3, -97.7,   "Austin TX",                     "egs",           55, "ERCOT grid, moderate gradient"),
    (29.8, -95.4,   "Houston TX",                    "hydrothermal",  60, "Gulf Coast geopressured zones"),
    (61.2, -149.9,  "Anchorage AK",                  "hydrothermal",  85, "Chena Hot Springs area, active plants"),
    (20.8, -156.3,  "Hawaii Maui",                   "hydrothermal",  90, "Puna Geothermal Venture, Big Island"),
]

def _geothermal_score(lat, lon):
    """Score geothermal potential 0-100 for a given lat/lon."""
    best_score = 0
    best_zone = None
    best_dist_km = None
    nearby_zones = []

    for z_lat, z_lon, name, gtype, base_score, notes in GEOTHERMAL_ZONES:
        dist = _haversine_km(lat, lon, z_lat, z_lon)
        # Score decays with distance: full score within 50km, zero at 400km
        if dist <= 50:
            decay = 1.0
        elif dist <= 400:
            decay = 1.0 - (dist - 50) / 350
        else:
            decay = 0.0

        effective = round(base_score * decay)
        if decay > 0:
            nearby_zones.append({
                "name": name,
                "type": gtype,
                "distance_km": round(dist, 1),
                "base_potential": base_score,
                "effective_score": effective,
                "notes": notes,
            })
        if effective > best_score:
            best_score = effective
            best_zone = name
            best_dist_km = round(dist, 1)

    nearby_zones.sort(key=lambda z: z["distance_km"])

    if best_score >= 80:
        classification = "Exceptional — commercial geothermal viable"
    elif best_score >= 60:
        classification = "High — EGS or hydrothermal development feasible"
    elif best_score >= 40:
        classification = "Moderate — research-grade EGS potential"
    elif best_score >= 20:
        classification = "Low — deep EGS only, high cost"
    else:
        classification = "Minimal — not economically viable"

    return {
        "geothermal_score": best_score,
        "classification": classification,
        "nearest_zone": best_zone,
        "nearest_zone_km": best_dist_km,
        "nearby_zones": nearby_zones[:5],
    }

# ---------------------------------------------------------------------------
# Solar irradiance lookup (simplified GHI zones by lat band + state)
# ---------------------------------------------------------------------------
SOLAR_GHI_BY_STATE = {
    "AZ": 6.5, "NM": 6.3, "NV": 6.1, "CA": 5.8, "TX": 5.6, "CO": 5.5,
    "UT": 5.4, "KS": 5.2, "OK": 5.1, "FL": 5.0, "HI": 5.9, "GA": 4.9,
    "SC": 4.9, "NC": 4.8, "TN": 4.6, "AR": 4.7, "AL": 4.8, "MS": 4.8,
    "LA": 4.8, "MO": 4.6, "IL": 4.4, "IN": 4.3, "OH": 4.2, "KY": 4.4,
    "VA": 4.6, "MD": 4.5, "DE": 4.4, "NJ": 4.4, "PA": 4.3, "NY": 4.2,
    "CT": 4.3, "RI": 4.3, "MA": 4.2, "VT": 4.0, "NH": 4.0, "ME": 4.0,
    "WV": 4.2, "MI": 4.1, "WI": 4.2, "MN": 4.3, "IA": 4.5, "NE": 5.0,
    "SD": 4.8, "ND": 4.6, "MT": 4.6, "WY": 5.2, "ID": 4.8, "OR": 4.1,
    "WA": 3.7, "AK": 2.5,
}

WIND_CLASS_BY_STATE = {
    "TX": 8, "KS": 8, "ND": 8, "SD": 7, "NE": 7, "WY": 7, "MT": 7,
    "IA": 7, "MN": 6, "OK": 8, "CO": 6, "NM": 6, "NV": 5, "ID": 6,
    "OR": 6, "WA": 6, "CA": 5, "AZ": 4, "UT": 5, "IL": 5, "IN": 5,
    "MI": 5, "OH": 4, "PA": 4, "NY": 5, "ME": 7, "NH": 5, "VT": 5,
    "MA": 6, "FL": 3, "GA": 3, "SC": 3, "NC": 4, "VA": 4, "WV": 4,
    "TN": 3, "AL": 3, "MS": 3, "LA": 4, "AR": 4, "MO": 5, "WI": 5,
    "AK": 7, "HI": 6,
}


# ---------------------------------------------------------------------------
# 1. Geothermal Potential
# ---------------------------------------------------------------------------
@nlr_bp.route("/api/v1/geothermal-potential", methods=["GET"])
def geothermal_potential():
    """
    Score geothermal resource potential for a given location.
    Key differentiator vs Baxtel — purpose-built for NLR research alignment.

    Query params: lat, lon, state
    """
    try:
        lat = float(request.args.get("lat", 0))
        lon = float(request.args.get("lon", 0))
        state = request.args.get("state", "").upper()

        if not lat or not lon:
            return jsonify({"error": "lat and lon required"}), 400

        geo = _geothermal_score(lat, lon)

        # Try to enrich with nearby geothermal plants from DB
        conn = _get_db_safe()
        nearby_plants = []
        if conn:
            rows = _query_safe(conn, """
                SELECT name, capacity_mw, latitude, longitude, state_name
                FROM power_plants
                WHERE energy_source_code IN ('GEO', 'geothermal')
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                LIMIT 200
            """)
            for r in rows:
                if r[2] and r[3]:
                    dist = _haversine_km(lat, lon, float(r[2]), float(r[3]))
                    if dist <= 300:
                        nearby_plants.append({
                            "name": r[0],
                            "capacity_mw": r[1],
                            "distance_km": round(dist, 1),
                            "state": r[4],
                        })
            nearby_plants.sort(key=lambda x: x["distance_km"])
            try:
                conn.close()
            except Exception:
                pass

        return jsonify({
            "success": True,
            "location": {"lat": lat, "lon": lon, "state": state},
            "geothermal_potential": geo,
            "nearby_geothermal_plants": nearby_plants[:5],
            "nlr_relevance": {
                "aries_compatible": True,
                "research_zone": geo["geothermal_score"] >= 50,
                "commercial_viable": geo["geothermal_score"] >= 70,
                "note": "NLR/NREL EGS research zones mapped. See nlr.gov/geothermal for full dataset."
            },
            "source": "DC Hub + USGS EGS Atlas + EIA-860"
        })
    except Exception as e:
        logger.error(f"geothermal_potential error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 2. Co-location Score
# ---------------------------------------------------------------------------
@nlr_bp.route("/api/v1/colocation-score", methods=["GET"])
def colocation_score():
    """
    Composite score for data center + renewable energy co-location opportunity.
    Combines grid access, renewable proximity, incentives, and risk into one
    NLR-friendly siting score.

    Query params: lat, lon, state, capacity_mw (default 100)
    """
    try:
        lat = float(request.args.get("lat", 0))
        lon = float(request.args.get("lon", 0))
        state = request.args.get("state", "").upper()
        capacity_mw = float(request.args.get("capacity_mw", 100))

        if not lat or not lon:
            return jsonify({"error": "lat and lon required"}), 400

        # --- Solar score (0-100)
        ghi = SOLAR_GHI_BY_STATE.get(state, 4.2)
        solar_score = min(100, round((ghi / 7.0) * 100))

        # --- Wind score (0-100)
        wind_class = WIND_CLASS_BY_STATE.get(state, 4)
        wind_score = min(100, round((wind_class / 8.0) * 100))

        # --- Geothermal score
        geo = _geothermal_score(lat, lon)
        geo_score = geo["geothermal_score"]

        # --- Best renewable score (weighted: solar 40%, wind 40%, geo 20%)
        renewable_score = round(solar_score * 0.4 + wind_score * 0.4 + geo_score * 0.2)

        # --- Grid score from DB substations
        conn = _get_db_safe()
        substation_count = 0
        max_voltage = 0
        if conn:
            rows = _query_safe(conn, """
                SELECT COUNT(*), MAX(voltage_kv)
                FROM substations
                WHERE latitude BETWEEN %s AND %s
                AND longitude BETWEEN %s AND %s
            """, (lat - 1.0, lat + 1.0, lon - 1.0, lon + 1.0))
            if rows and rows[0][0]:
                substation_count = rows[0][0]
                max_voltage = float(rows[0][1] or 0)
            try:
                conn.close()
            except Exception:
                pass

        if substation_count >= 10:
            grid_score = 90
        elif substation_count >= 5:
            grid_score = 75
        elif substation_count >= 2:
            grid_score = 55
        elif substation_count >= 1:
            grid_score = 35
        else:
            grid_score = 15

        if max_voltage >= 345:
            grid_score = min(100, grid_score + 10)
        elif max_voltage >= 138:
            grid_score = min(100, grid_score + 5)

        # --- Incentive score (IRA/CHIPS Act zones)
        IRA_BONUS_STATES = {"TX", "NM", "CO", "WY", "MT", "ND", "SD", "NE",
                            "KS", "OK", "IA", "MN", "ID", "NV", "AZ", "UT",
                            "OR", "WA", "ME", "MI", "OH", "PA", "IN", "GA"}
        incentive_score = 80 if state in IRA_BONUS_STATES else 50

        # --- Composite (grid 30%, renewable 35%, incentive 20%, geo 15%)
        composite = round(
            grid_score    * 0.30 +
            renewable_score * 0.35 +
            incentive_score * 0.20 +
            geo_score     * 0.15
        )

        if composite >= 80:
            rating = "Exceptional co-location opportunity"
        elif composite >= 65:
            rating = "Strong co-location opportunity"
        elif composite >= 50:
            rating = "Moderate — viable with right PPA structure"
        elif composite >= 35:
            rating = "Limited — grid or renewable constraints"
        else:
            rating = "Poor — not recommended for co-location"

        # --- Estimated PPA savings
        ppa_discount_pct = round(max(5, min(35, (renewable_score - 40) * 0.5 + 15)), 1)
        carbon_reduction_pct = round(min(95, renewable_score * 0.9), 1)

        return jsonify({
            "success": True,
            "location": {"lat": lat, "lon": lon, "state": state},
            "colocation_score": composite,
            "rating": rating,
            "component_scores": {
                "grid_access": grid_score,
                "renewable_potential": renewable_score,
                "tax_incentives": incentive_score,
                "geothermal_bonus": geo_score,
            },
            "renewable_breakdown": {
                "solar_score": solar_score,
                "solar_ghi_kwh_m2_day": ghi,
                "wind_score": wind_score,
                "wind_class": wind_class,
                "geothermal_score": geo_score,
            },
            "economics": {
                "estimated_ppa_discount_pct": ppa_discount_pct,
                "carbon_reduction_potential_pct": carbon_reduction_pct,
                "capacity_mw_analyzed": capacity_mw,
            },
            "infrastructure": {
                "substations_within_100km": substation_count,
                "max_voltage_kv": max_voltage,
            },
            "nlr_colocation_flag": composite >= 60,
            "source": "DC Hub NLR Intelligence Layer"
        })
    except Exception as e:
        logger.error(f"colocation_score error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 3. Grid Headroom (estimated available MW at nearby substations)
# ---------------------------------------------------------------------------
@nlr_bp.route("/api/v1/grid-headroom", methods=["GET"])
def grid_headroom():
    """
    Estimate available power capacity (MW headroom) at substations near a site.
    Goes beyond Baxtel's simple 'substation nearby' flag to show actual
    usable capacity for a data center build.

    Query params: lat, lon, state, radius_km (default 80)
    """
    try:
        lat = float(request.args.get("lat", 0))
        lon = float(request.args.get("lon", 0))
        state = request.args.get("state", "").upper()
        radius_km = float(request.args.get("radius_km", 80))

        if not lat or not lon:
            return jsonify({"error": "lat and lon required"}), 400

        conn = _get_db_safe()
        substations = []
        if conn:
            deg_margin = radius_km / 111.0
            rows = _query_safe(conn, """
                SELECT name, voltage_kv, latitude, longitude, state_name
                FROM substations
                WHERE latitude BETWEEN %s AND %s
                AND longitude BETWEEN %s AND %s
                AND voltage_kv IS NOT NULL
                ORDER BY voltage_kv DESC
                LIMIT 50
            """, (lat - deg_margin, lat + deg_margin,
                  lon - deg_margin, lon + deg_margin))
            for r in rows:
                if r[2] and r[3]:
                    dist = _haversine_km(lat, lon, float(r[2]), float(r[3]))
                    if dist <= radius_km:
                        vkv = float(r[1] or 0)
                        # Estimate headroom by voltage class
                        # (Higher voltage = larger transformer capacity)
                        if vkv >= 500:
                            est_capacity_mw = 800
                            confidence = "medium"
                        elif vkv >= 345:
                            est_capacity_mw = 400
                            confidence = "medium"
                        elif vkv >= 230:
                            est_capacity_mw = 200
                            confidence = "medium"
                        elif vkv >= 138:
                            est_capacity_mw = 80
                            confidence = "low"
                        else:
                            est_capacity_mw = 20
                            confidence = "low"

                        substations.append({
                            "name": r[0],
                            "voltage_kv": vkv,
                            "distance_km": round(dist, 1),
                            "state": r[4],
                            "estimated_capacity_mw": est_capacity_mw,
                            "confidence": confidence,
                        })
            substations.sort(key=lambda x: x["distance_km"])
            try:
                conn.close()
            except Exception:
                pass

        total_estimated_mw = sum(s["estimated_capacity_mw"] for s in substations[:5])
        nearest = substations[0] if substations else None

        if total_estimated_mw >= 500:
            headroom_rating = "Abundant — supports hyperscale build"
        elif total_estimated_mw >= 200:
            headroom_rating = "Strong — supports 50-200 MW campus"
        elif total_estimated_mw >= 80:
            headroom_rating = "Moderate — supports 10-50 MW facility"
        elif total_estimated_mw > 0:
            headroom_rating = "Limited — edge/small facility only"
        else:
            headroom_rating = "Unknown — no substations found in radius"

        return jsonify({
            "success": True,
            "location": {"lat": lat, "lon": lon, "state": state},
            "grid_headroom": {
                "total_estimated_available_mw": total_estimated_mw,
                "rating": headroom_rating,
                "substations_analyzed": len(substations),
                "radius_km": radius_km,
            },
            "nearest_substation": nearest,
            "top_substations": substations[:8],
            "caveat": "Capacity estimates based on voltage class. Actual interconnection queue and utility availability require direct utility confirmation.",
            "source": "DC Hub + HIFLD Substation Database"
        })
    except Exception as e:
        logger.error(f"grid_headroom error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 4. Microgrid Viability
# ---------------------------------------------------------------------------
@nlr_bp.route("/api/v1/microgrid-viability", methods=["GET"])
def microgrid_viability():
    """
    Score a site's viability for on-site microgrid / distributed generation.
    Directly aligned with NLR's ARIES platform and microgrid research program.
    (See: nlr.gov — 'Data Center Inside a Power Plant, Inside a Microgrid')

    Query params: lat, lon, state, capacity_mw (default 10)
    """
    try:
        lat = float(request.args.get("lat", 0))
        lon = float(request.args.get("lon", 0))
        state = request.args.get("state", "").upper()
        capacity_mw = float(request.args.get("capacity_mw", 10))

        if not lat or not lon:
            return jsonify({"error": "lat and lon required"}), 400

        # Solar
        ghi = SOLAR_GHI_BY_STATE.get(state, 4.2)
        solar_score = min(100, round((ghi / 7.0) * 100))

        # Wind
        wind_class = WIND_CLASS_BY_STATE.get(state, 4)
        wind_score = min(100, round((wind_class / 8.0) * 100))

        # Geothermal
        geo = _geothermal_score(lat, lon)
        geo_score = geo["geothermal_score"]

        # Battery storage suitability (based on solar+wind variability)
        storage_score = min(100, round((solar_score + wind_score) / 2 * 0.85))

        # Overall microgrid score
        mg_score = round(solar_score * 0.35 + wind_score * 0.30 + geo_score * 0.15 + storage_score * 0.20)

        # Recommended configuration
        configs = []
        if solar_score >= 60:
            solar_mw = round(capacity_mw * 1.5, 1)
            configs.append(f"Solar PV: {solar_mw} MW (1.5× DC load ratio)")
        if wind_score >= 60:
            wind_mw = round(capacity_mw * 0.8, 1)
            configs.append(f"Wind: {wind_mw} MW")
        if geo_score >= 50:
            geo_mw = round(min(capacity_mw * 0.5, 20), 1)
            configs.append(f"Geothermal baseload: {geo_mw} MW (24/7 firm)")
        if storage_score >= 50:
            batt_mwh = round(capacity_mw * 2, 1)
            configs.append(f"Battery storage: {batt_mwh} MWh (2-hour buffer)")

        if mg_score >= 75:
            viability = "High — strong candidate for islanded microgrid"
        elif mg_score >= 55:
            viability = "Moderate — grid-tied microgrid with renewables viable"
        elif mg_score >= 35:
            viability = "Limited — supplement grid power with on-site solar only"
        else:
            viability = "Low — traditional grid connection recommended"

        # ARIES alignment flags (NLR's Advanced Research on Integrated Energy Systems)
        aries_flags = {
            "geothermal_baseload": geo_score >= 50,
            "high_renewable_fraction": (solar_score + wind_score) / 2 >= 60,
            "islanding_candidate": mg_score >= 70,
            "storage_integration": storage_score >= 60,
            "dc_powerplant_concept": mg_score >= 75 and geo_score >= 50,
        }

        return jsonify({
            "success": True,
            "location": {"lat": lat, "lon": lon, "state": state},
            "microgrid_score": mg_score,
            "viability": viability,
            "component_scores": {
                "solar": solar_score,
                "wind": wind_score,
                "geothermal": geo_score,
                "storage_suitability": storage_score,
            },
            "recommended_configuration": configs,
            "capacity_mw_analyzed": capacity_mw,
            "aries_platform_flags": aries_flags,
            "nlr_research_alignment": {
                "relevant_programs": [
                    "ARIES (Advanced Research on Integrated Energy Systems)",
                    "Geothermal Technologies Office",
                    "Solar Energy Research Institute",
                ],
                "dc_powerplant_in_microgrid": aries_flags["dc_powerplant_concept"],
                "reference": "nlr.gov/news — In Alaska, a Data Center Inside a Power Plant, Inside a Microgrid"
            },
            "source": "DC Hub NLR Intelligence Layer + EIA + USGS"
        })
    except Exception as e:
        logger.error(f"microgrid_viability error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Registration helper (called from main.py)
# ---------------------------------------------------------------------------
def register_nlr_routes(app):
    app.register_blueprint(nlr_bp)
    logger.info("🌋 NLR Intelligence routes registered:")
    logger.info("   GET /api/v1/geothermal-potential")
    logger.info("   GET /api/v1/colocation-score")
    logger.info("   GET /api/v1/grid-headroom")
    logger.info("   GET /api/v1/microgrid-viability")
