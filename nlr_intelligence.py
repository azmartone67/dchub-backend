"""
NLR Intelligence Layer — DC Hub
Routes: geothermal-potential, colocation-score, grid-headroom, microgrid-viability

Fix history:
  v2  - use get_pg_connection from __main__ (not db_utils)
      - query `substations` table with lat/lng/state columns
        (was hifld_electric_substations with latitude/longitude)
"""

import math
import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

nlr_bp = Blueprint("nlr_intelligence", __name__)


# ---------------------------------------------------------------------------
# DB helper — pulls the live Neon connection from main.py
# ---------------------------------------------------------------------------

def _get_db_safe():
    """Return a live PostgreSQL connection or None (never raises)."""
    try:
        import __main__
        if hasattr(__main__, "get_pg_connection"):
            return __main__.get_pg_connection()
    except Exception as exc:
        logger.debug("NLR _get_db_safe: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Static lookup data
# ---------------------------------------------------------------------------

# NLR / NREL geothermal resource zones (lat, lon, name, type, base_score, notes)
GEOTHERMAL_ZONES = [
    (39.74, -105.17, "Colorado Front Range",        "egs",         62, "Moderate EGS, NLR/NREL research zone"),
    (44.50, -110.50, "Yellowstone / Snake River",   "hydrothermal",95, "High-enthalpy hydrothermal corridor"),
    (38.80, -117.00, "Great Basin Nevada",           "hydrothermal",88, "Largest US geothermal district"),
    (40.50, -122.50, "Northern California Geysers",  "hydrothermal",91, "Active commercial production"),
    (35.00, -106.50, "New Mexico Rio Grande Rift",   "egs",         70, "EGS research zone — NLR FORGE adjacent"),
    (36.50, -117.50, "Salton Sea / Imperial Valley", "hydrothermal",93, "Critical minerals + geothermal hub"),
    (46.00, -122.00, "Cascades Washington / Oregon", "hydrothermal",78, "Volcanic hydrothermal potential"),
    (39.50, -119.00, "Reno / Fallon Nevada",         "hydrothermal",84, "Ormat operating plants nearby"),
    (37.00, -114.00, "Utah Basin and Range",         "egs",         72, "FORGE EGS demonstration site"),
    (64.00, -153.00, "Alaska Interior",              "hydrothermal",80, "In-powerplant DC concept — NLR ARIES"),
]

# State solar / wind lookup (approximate resource quality)
SOLAR_GHI = {   # kWh/m²/day
    "AZ": 6.5, "NM": 6.3, "NV": 6.2, "CO": 5.5, "TX": 5.8, "CA": 5.9,
    "UT": 5.6, "WY": 5.2, "MT": 4.9, "ID": 4.8, "OR": 4.2, "WA": 3.8,
    "FL": 5.6, "GA": 5.2, "SC": 5.1, "NC": 5.0, "VA": 4.8, "MD": 4.6,
    "PA": 4.3, "NY": 4.1, "MA": 4.2, "CT": 4.1, "NJ": 4.4, "DE": 4.4,
    "OH": 4.0, "IN": 4.2, "IL": 4.4, "MI": 3.9, "WI": 4.1, "MN": 4.4,
    "IA": 4.6, "MO": 4.7, "KS": 5.2, "NE": 5.1, "SD": 4.9, "ND": 4.7,
    "OK": 5.4, "AR": 5.0, "LA": 5.3, "MS": 5.2, "AL": 5.1, "TN": 4.8,
    "KY": 4.5, "WV": 4.2, "AK": 3.2, "HI": 5.8,
}
WIND_CLASS = {  # NREL wind class 1-7
    "TX": 7, "KS": 7, "OK": 7, "NE": 6, "SD": 7, "ND": 7, "WY": 6,
    "CO": 6, "MN": 6, "IA": 6, "MT": 5, "NM": 5, "ID": 4, "OR": 5,
    "WA": 5, "CA": 4, "NV": 3, "AZ": 3, "UT": 4, "AK": 6,
    "FL": 3, "GA": 2, "VA": 3, "NC": 3, "SC": 2, "NY": 4, "MA": 4,
    "ME": 5, "NH": 4, "VT": 4, "PA": 3, "OH": 3, "MI": 4, "WI": 4,
    "IL": 4, "IN": 3, "MO": 4, "AR": 3, "LA": 3, "MS": 2, "AL": 2,
    "TN": 2, "KY": 2, "WV": 2, "MD": 3, "NJ": 4, "DE": 3, "CT": 3,
    "RI": 4, "HI": 5,
}

# IRA / state tax incentive proxy scores (0-100)
TAX_INCENTIVE_SCORES = {
    "TX": 90, "WY": 88, "NV": 82, "UT": 85, "CO": 80, "NM": 83,
    "ID": 78, "MT": 77, "KS": 86, "OK": 87, "ND": 84, "SD": 83,
    "NE": 81, "IA": 80, "MN": 79, "IN": 75, "OH": 73, "PA": 72,
    "VA": 74, "NC": 76, "GA": 78, "FL": 77, "SC": 75, "AL": 73,
    "TN": 74, "KY": 72, "WV": 71, "CA": 65, "OR": 68, "WA": 67,
    "AZ": 76, "MI": 70, "WI": 71, "IL": 69, "MO": 74, "AR": 75,
    "LA": 76, "MS": 74, "NY": 64, "MA": 66, "CT": 65, "NJ": 64,
    "MD": 67, "DE": 68, "HI": 70, "AK": 72,
}


# ---------------------------------------------------------------------------
# Helper math
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _nearest_geothermal(lat, lon, radius_km=500):
    results = []
    for zlat, zlon, name, ztype, score, notes in GEOTHERMAL_ZONES:
        d = _haversine_km(lat, lon, zlat, zlon)
        if d <= radius_km:
            decay = max(0.0, 1.0 - d / radius_km * 0.5)
            results.append({
                "name": name,
                "type": ztype,
                "distance_km": round(d, 1),
                "base_potential": score,
                "effective_score": round(score * decay),
                "notes": notes,
            })
    results.sort(key=lambda x: x["distance_km"])
    return results


def _solar_score(ghi):
    if ghi >= 6.0: return 95
    if ghi >= 5.5: return 85
    if ghi >= 5.0: return 75
    if ghi >= 4.5: return 65
    if ghi >= 4.0: return 52
    return 40


def _wind_score(wind_class):
    return min(95, max(10, wind_class * 13))


def _geo_classification(score):
    if score >= 85: return "Very High — commercial geothermal viable"
    if score >= 70: return "High — EGS or hydrothermal development feasible"
    if score >= 50: return "Moderate — EGS research / exploration warranted"
    if score >= 30: return "Low — marginal resource, deep EGS only"
    return "Minimal — not recommended for geothermal"


def _colocation_rating(score):
    if score >= 80: return "Excellent — strong NLR co-location candidate"
    if score >= 65: return "Good — viable co-location with standard PPA"
    if score >= 50: return "Moderate — viable with right PPA structure"
    if score >= 35: return "Fair — marginal; consider alternative markets"
    return "Poor — low renewable density, high grid dependency"


def _microgrid_viability(score):
    if score >= 80: return "High — standalone microgrid viable"
    if score >= 65: return "Moderate-High — grid-tied microgrid with storage viable"
    if score >= 50: return "Moderate — grid-tied microgrid with renewables viable"
    if score >= 35: return "Low-Moderate — supplemental renewables only"
    return "Low — grid-dependent; limited microgrid potential"


# ---------------------------------------------------------------------------
# Substation DB query (uses correct `substations` table)
# ---------------------------------------------------------------------------

def _query_substations(lat, lon, state, radius_km=80):
    """
    Query the `substations` table (Neon PG) for nearby substations.
    Columns used: name, voltage_kv, capacity_mva, lat, lng, state
    Returns list of dicts sorted by distance.
    """
    conn = _get_db_safe()
    if conn is None:
        return []

    lat_range  = radius_km / 111.0
    lon_range  = radius_km / (111.0 * abs(math.cos(math.radians(lat))) + 0.001)

    sql = """
        SELECT
            name,
            voltage_kv,
            capacity_mva,
            lat,
            lng,
            state
        FROM substations
        WHERE lat  BETWEEN %(lat_min)s  AND %(lat_max)s
          AND lng  BETWEEN %(lon_min)s  AND %(lon_max)s
        ORDER BY ABS(lat - %(lat)s) + ABS(lng - %(lon)s)
        LIMIT 50
    """
    params = {
        "lat": lat, "lon": lon,
        "lat_min": lat - lat_range, "lat_max": lat + lat_range,
        "lon_min": lon - lon_range, "lon_max": lon + lon_range,
    }

    results = []
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        for row in rows:
            name, voltage_kv, capacity_mva, slat, slon, sstate = row
            dist = _haversine_km(lat, lon, float(slat or 0), float(slon or 0))
            if dist <= radius_km:
                results.append({
                    "name": name,
                    "voltage_kv": float(voltage_kv or 0),
                    "capacity_mva": float(capacity_mva or 0),
                    "distance_km": round(dist, 1),
                    "state": sstate,
                })
        results.sort(key=lambda x: x["distance_km"])
    except Exception as exc:
        logger.warning("NLR substations query error: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# Route 1 — /api/v1/geothermal-potential
# ---------------------------------------------------------------------------

@nlr_bp.route("/api/v1/geothermal-potential")
def geothermal_potential():
    try:
        lat   = float(request.args.get("lat",   39.7405))
        lon   = float(request.args.get("lon",  -105.1686))
        state = request.args.get("state", "CO").upper()
        radius_km = float(request.args.get("radius_km", 500))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": str(e)}), 400

    zones = _nearest_geothermal(lat, lon, radius_km)
    geo_score = zones[0]["effective_score"] if zones else 0
    nearest_name = zones[0]["name"]  if zones else "None"
    nearest_km   = zones[0]["distance_km"] if zones else None

    # Pull nearby geothermal plants from DB (power_plants table)
    nearby_plants = []
    conn = _get_db_safe()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT name, capacity_mw, lat, lng
                FROM power_plants
                WHERE fuel_type ILIKE '%geothermal%'
                  AND lat  BETWEEN %s AND %s
                  AND lng  BETWEEN %s AND %s
                LIMIT 10
            """, (lat - 3, lat + 3, lon - 3, lon + 3))
            for row in cur.fetchall():
                pname, cap, plat, plon = row
                d = _haversine_km(lat, lon, float(plat or 0), float(plon or 0))
                nearby_plants.append({"name": pname, "capacity_mw": float(cap or 0), "distance_km": round(d, 1)})
            nearby_plants.sort(key=lambda x: x["distance_km"])
            cur.close()
            conn.close()
        except Exception as exc:
            logger.debug("NLR geothermal plants query: %s", exc)
            try: conn.rollback()
            except Exception: pass
            try: conn.close()
            except Exception: pass

    aries_compat = geo_score >= 50
    return jsonify({
        "success": True,
        "location": {"lat": lat, "lon": lon, "state": state},
        "geothermal_potential": {
            "geothermal_score": geo_score,
            "classification": _geo_classification(geo_score),
            "nearest_zone": nearest_name,
            "nearest_zone_km": nearest_km,
            "nearby_zones": zones,
        },
        "nearby_geothermal_plants": nearby_plants,
        "nlr_relevance": {
            "research_zone": geo_score >= 40,
            "aries_compatible": aries_compat,
            "commercial_viable": geo_score >= 75,
            "note": "NLR/NREL EGS research zones mapped. See nlr.gov/geothermal for full dataset.",
        },
        "source": "DC Hub + USGS EGS Atlas + EIA-860",
    })


# ---------------------------------------------------------------------------
# Route 2 — /api/v1/colocation-score
# ---------------------------------------------------------------------------

@nlr_bp.route("/api/v1/colocation-score")
def colocation_score():
    try:
        lat         = float(request.args.get("lat",   39.7405))
        lon         = float(request.args.get("lon",  -105.1686))
        state       = request.args.get("state", "CO").upper()
        capacity_mw = float(request.args.get("capacity_mw", 100))
        radius_km   = float(request.args.get("radius_km", 100))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Renewable scores
    ghi        = SOLAR_GHI.get(state, 4.5)
    wind_cls   = WIND_CLASS.get(state, 3)
    solar_sc   = _solar_score(ghi)
    wind_sc    = _wind_score(wind_cls)
    geo_zones  = _nearest_geothermal(lat, lon, 500)
    geo_sc     = geo_zones[0]["effective_score"] if geo_zones else 0
    renew_sc   = round((solar_sc * 0.4 + wind_sc * 0.35 + min(geo_sc, 30) * 0.25))
    tax_sc     = TAX_INCENTIVE_SCORES.get(state, 70)

    # Substations
    subs = _query_substations(lat, lon, state, radius_km)
    sub_count  = len(subs)
    max_kv     = max((s["voltage_kv"] for s in subs), default=0)

    # Grid access score (0-100) based on substation count + voltage
    if sub_count == 0:
        grid_sc = 15
    elif sub_count < 3:
        grid_sc = 35 + min(max_kv / 10, 25)
    elif sub_count < 10:
        grid_sc = 55 + min(max_kv / 10, 20)
    else:
        grid_sc = 75 + min(max_kv / 20, 20)
    grid_sc = round(min(grid_sc, 100))

    # Composite (weighted)
    score = round(renew_sc * 0.40 + grid_sc * 0.25 + tax_sc * 0.20 + geo_sc * 0.15)

    # NLR flag: high renewable + good grid + geo research zone
    nlr_flag = (score >= 70 and geo_sc >= 40 and grid_sc >= 50)

    carbon_reduction = round((solar_sc + wind_sc) / 2 * 0.9, 1)
    ppa_discount = round((renew_sc - 50) * 0.8, 1) if renew_sc > 50 else 0

    return jsonify({
        "success": True,
        "location": {"lat": lat, "lon": lon, "state": state},
        "colocation_score": score,
        "rating": _colocation_rating(score),
        "component_scores": {
            "renewable_potential": renew_sc,
            "grid_access": grid_sc,
            "tax_incentives": tax_sc,
            "geothermal_bonus": geo_sc,
        },
        "renewable_breakdown": {
            "solar_ghi_kwh_m2_day": ghi,
            "solar_score": solar_sc,
            "wind_class": wind_cls,
            "wind_score": wind_sc,
            "geothermal_score": geo_sc,
        },
        "infrastructure": {
            "substations_within_100km": sub_count,
            "max_voltage_kv": max_kv,
        },
        "economics": {
            "capacity_mw_analyzed": capacity_mw,
            "estimated_ppa_discount_pct": ppa_discount,
            "carbon_reduction_potential_pct": carbon_reduction,
        },
        "nlr_colocation_flag": nlr_flag,
        "source": "DC Hub NLR Intelligence Layer",
    })


# ---------------------------------------------------------------------------
# Route 3 — /api/v1/grid-headroom
# ---------------------------------------------------------------------------

def _estimate_headroom_mw(voltage_kv):
    """Rough available headroom estimate based on voltage class."""
    if voltage_kv >= 500: return 800
    if voltage_kv >= 345: return 500
    if voltage_kv >= 230: return 300
    if voltage_kv >= 138: return 150
    if voltage_kv >= 115: return 100
    if voltage_kv >= 69:  return 50
    return 20


@nlr_bp.route("/api/v1/grid-headroom")
def grid_headroom():
    try:
        lat       = float(request.args.get("lat",   39.7405))
        lon       = float(request.args.get("lon",  -105.1686))
        state     = request.args.get("state", "CO").upper()
        radius_km = float(request.args.get("radius_km", 80))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": str(e)}), 400

    subs = _query_substations(lat, lon, state, radius_km)

    top_subs = []
    total_mw = 0
    nearest  = None

    for s in subs[:10]:
        headroom = _estimate_headroom_mw(s["voltage_kv"])
        total_mw += headroom
        entry = {
            "name":        s["name"],
            "voltage_kv":  s["voltage_kv"],
            "distance_km": s["distance_km"],
            "estimated_available_mw": headroom,
        }
        top_subs.append(entry)
        if nearest is None:
            nearest = entry

    if total_mw == 0:
        rating = "Unknown — no substations found in radius"
    elif total_mw >= 2000:
        rating = "Abundant — >2 GW estimated available"
    elif total_mw >= 1000:
        rating = "Strong — 1–2 GW estimated available"
    elif total_mw >= 500:
        rating = "Adequate — 500 MW–1 GW estimated available"
    elif total_mw >= 200:
        rating = "Moderate — 200–500 MW estimated available"
    else:
        rating = "Constrained — <200 MW estimated available"

    return jsonify({
        "success": True,
        "location": {"lat": lat, "lon": lon, "state": state},
        "grid_headroom": {
            "substations_analyzed": len(subs),
            "radius_km": radius_km,
            "total_estimated_available_mw": total_mw,
            "rating": rating,
        },
        "nearest_substation": nearest,
        "top_substations": top_subs,
        "source": "DC Hub + HIFLD Substation Database",
        "caveat": (
            "Capacity estimates based on voltage class. "
            "Actual interconnection queue and utility availability "
            "require direct utility confirmation."
        ),
    })


# ---------------------------------------------------------------------------
# Route 4 — /api/v1/microgrid-viability
# ---------------------------------------------------------------------------

@nlr_bp.route("/api/v1/microgrid-viability")
def microgrid_viability():
    try:
        lat         = float(request.args.get("lat",   39.7405))
        lon         = float(request.args.get("lon",  -105.1686))
        state       = request.args.get("state", "CO").upper()
        capacity_mw = float(request.args.get("capacity_mw", 50))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": str(e)}), 400

    ghi       = SOLAR_GHI.get(state, 4.5)
    wind_cls  = WIND_CLASS.get(state, 3)
    solar_sc  = _solar_score(ghi)
    wind_sc   = _wind_score(wind_cls)
    geo_zones = _nearest_geothermal(lat, lon, 500)
    geo_sc    = geo_zones[0]["effective_score"] if geo_zones else 0

    # Storage suitability: temperature extremes help or hurt battery cycles
    # Proxy: higher solar = warmer climate = better storage
    storage_sc = min(95, 45 + round(ghi * 8))

    # Composite microgrid score
    mg_score = round(solar_sc * 0.30 + wind_sc * 0.25 + geo_sc * 0.20 + storage_sc * 0.25)

    # Recommended configuration
    config = [
        f"Solar PV: {round(capacity_mw * 1.5)} MW (1.5× DC load ratio)",
        f"Wind: {round(capacity_mw * 0.8)} MW",
    ]
    if geo_sc >= 50:
        config.append(f"Geothermal baseload: {round(capacity_mw * 0.4)} MW (24/7 firm)")
    config.append(f"Battery storage: {round(capacity_mw * 2)} MWh (2-hour buffer)")

    # ARIES alignment flags
    aries_flags = {
        "islanding_candidate":   mg_score >= 65,
        "high_renewable_fraction": (solar_sc + wind_sc) / 2 >= 65,
        "geothermal_baseload":   geo_sc >= 50,
        "storage_integration":   storage_sc >= 60,
        "dc_powerplant_concept": geo_sc >= 80 and mg_score >= 75,
    }

    return jsonify({
        "success": True,
        "location": {"lat": lat, "lon": lon, "state": state},
        "capacity_mw_analyzed": capacity_mw,
        "microgrid_score": mg_score,
        "viability": _microgrid_viability(mg_score),
        "component_scores": {
            "solar":             solar_sc,
            "wind":              wind_sc,
            "geothermal":        geo_sc,
            "storage_suitability": storage_sc,
        },
        "recommended_configuration": config,
        "aries_platform_flags": aries_flags,
        "nlr_research_alignment": {
            "relevant_programs": [
                "ARIES (Advanced Research on Integrated Energy Systems)",
                "Geothermal Technologies Office",
                "Solar Energy Research Institute",
            ],
            "dc_powerplant_in_microgrid": aries_flags["dc_powerplant_concept"],
            "reference": "nlr.gov/news — In Alaska, a Data Center Inside a Power Plant, Inside a Microgrid",
        },
        "source": "DC Hub NLR Intelligence Layer + EIA + USGS",
    })


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_nlr_routes(app):
    app.register_blueprint(nlr_bp)
    logger.info("🌋 NLR Intelligence routes registered:")
    logger.info("  GET /api/v1/geothermal-potential")
    logger.info("  GET /api/v1/colocation-score")
    logger.info("  GET /api/v1/grid-headroom")
    logger.info("  GET /api/v1/microgrid-viability")
    print("🌋 NLR Intelligence: ✅ Registered (4 routes)")
    print("  GET /api/v1/geothermal-potential")
    print("  GET /api/v1/colocation-score")
    print("  GET /api/v1/grid-headroom")
    print("  GET /api/v1/microgrid-viability")
