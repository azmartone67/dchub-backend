"""
DC Hub New Layer API Routes — add to your Replit FastAPI app
============================================================
Add these routes to your existing main.py / app.py.

All endpoints serve GeoJSON-compatible JSON for the Leaflet map layers.

Dependencies: fastapi, psycopg2-binary, python-dotenv
"""

from fastapi import APIRouter, Query
from typing import Optional
import psycopg2, psycopg2.extras, os, json

router = APIRouter(prefix="/api/v1")

def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


# ─── SUBMARINE CABLES ────────────────────────────────────────────────────────
# Uses pre-loaded subsea_cables table (691 cables from TeleGeography)

@router.get("/submarine-cables")
def get_submarine_cables(
    planned: Optional[bool] = None,
    limit: int = Query(default=700, le=1000)
):
    """All submarine cable routes as GeoJSON features (for Leaflet polylines)."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["geometry_geojson IS NOT NULL"]
            params = []
            if planned is not None:
                conditions.append("is_planned = %s")
                params.append(planned)
            where = "WHERE " + " AND ".join(conditions)
            params.append(limit)
            cur.execute(f"""
                SELECT cable_id, name, owners, color,
                       rfs_year, length_km, is_planned,
                       geometry_geojson, landing_points_json
                FROM subsea_cables {where}
                ORDER BY length_km DESC NULLS LAST
                LIMIT %s
            """, params)
            cables = cur.fetchall()
            # Parse geometry_geojson from text → dict so it's valid JSON in response
            result = []
            for c in cables:
                row = dict(c)
                if row.get("geometry_geojson"):
                    try:
                        row["geometry_geojson"] = json.loads(row["geometry_geojson"])
                    except Exception:
                        row["geometry_geojson"] = None
                result.append(row)
        return {
            "cables": result,
            "count": len(result),
            "source": "TeleGeography / DC Hub"
        }
    finally:
        conn.close()


@router.get("/cable-landing-points")
def get_cable_landing_points(
    country: Optional[str] = None,
    limit: int = Query(default=2000, le=5000)
):
    """Submarine cable landing point locations (from subsea_landing_points)."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["latitude IS NOT NULL", "longitude IS NOT NULL"]
            params = []
            if country:
                conditions.append("country ILIKE %s")
                params.append(f"%{country}%")
            where = "WHERE " + " AND ".join(conditions)
            params.append(limit)
            cur.execute(f"""
                SELECT id, point_id, name, country, country_code,
                       latitude, longitude, cable_ids, cable_count, is_major_hub
                FROM subsea_landing_points {where}
                ORDER BY country, name
                LIMIT %s
            """, params)
            points = cur.fetchall()
            result = [dict(p) for p in points]
        return {
            "landing_points": result,
            "count": len(result),
            "source": "TeleGeography / DC Hub"
        }
    finally:
        conn.close()


# ─── GAS MIDSTREAM ───────────────────────────────────────────────────────────

@router.get("/gas-processing-plants")
def get_gas_processing_plants(
    state: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius: Optional[float] = None,  # miles
    limit: int = Query(default=500, le=2000)
):
    """Natural gas processing plants (EIA data)."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["lat IS NOT NULL", "lng IS NOT NULL"]
            params = []

            if state:
                conditions.append("state = %s")
                params.append(state.upper())

            if lat and lng and radius:
                # Bounding box pre-filter (miles → degrees approx)
                lat_d = radius / 69.0
                lng_d = radius / (69.0 * abs(float(f"{1:.4f}".format(1))) )
                conditions.append(
                    "lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
                )
                params += [lat - lat_d, lat + lat_d, lng - radius/53.0, lng + radius/53.0]

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            params.append(limit)

            cur.execute(f"""
                SELECT eia_id, plant_name, operator, state, county,
                       capacity_mmcfd, status, lat, lng
                FROM gas_processing_plants {where}
                ORDER BY capacity_mmcfd DESC NULLS LAST
                LIMIT %s
            """, params)
            plants = cur.fetchall()

        return {
            "plants": [dict(p) for p in plants],
            "count": len(plants),
            "source": "EIA Energy Atlas / DC Hub"
        }
    finally:
        conn.close()


@router.get("/gas-compressor-stations")
def get_gas_compressor_stations(
    state: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius: Optional[float] = None,
    limit: int = Query(default=500, le=2000)
):
    """Natural gas compressor stations (HIFLD data)."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["lat IS NOT NULL", "lng IS NOT NULL"]
            params = []

            if state:
                conditions.append("state = %s")
                params.append(state.upper())

            if lat and lng and radius:
                lat_d = radius / 69.0
                lng_d = radius / 53.0
                conditions.append(
                    "lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
                )
                params += [lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d]

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            params.append(limit)

            cur.execute(f"""
                SELECT hifld_id, station_name, operator, county, state, lat, lng
                FROM gas_compressor_stations {where}
                ORDER BY state, station_name
                LIMIT %s
            """, params)
            stations = cur.fetchall()

        return {
            "stations": [dict(s) for s in stations],
            "count": len(stations),
            "source": "HIFLD / DC Hub"
        }
    finally:
        conn.close()


# ─── INTERCONNECT QUEUE ──────────────────────────────────────────────────────

@router.get("/interconnect-queue")
def get_interconnect_queue(
    iso: Optional[str] = None,
    state: Optional[str] = None,
    fuel_type: Optional[str] = None,
    status: Optional[str] = "active",
    min_mw: Optional[float] = None,
    limit: int = Query(default=1000, le=5000)
):
    """Interconnection queue projects (LBNL Queued Up data)."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["lat IS NOT NULL", "lng IS NOT NULL"]
            params = []

            if status:
                conditions.append("queue_status = %s")
                params.append(status)
            if iso:
                conditions.append("iso = %s")
                params.append(iso.upper())
            if state:
                conditions.append("state = %s")
                params.append(state.upper())
            if fuel_type:
                conditions.append("fuel_type ILIKE %s")
                params.append(f"%{fuel_type}%")
            if min_mw:
                conditions.append("capacity_mw >= %s")
                params.append(min_mw)

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            params.append(limit)

            cur.execute(f"""
                SELECT queue_id, project_name, iso, state, county,
                       fuel_type, capacity_mw, queue_status,
                       queue_date, poi_name, lat, lng
                FROM interconnect_queue {where}
                ORDER BY capacity_mw DESC NULLS LAST
                LIMIT %s
            """, params)
            projects = cur.fetchall()

            # Also return summary stats
            cur.execute("""
                SELECT iso, COUNT(*) as count, SUM(capacity_mw) as total_mw
                FROM interconnect_queue
                WHERE queue_status = 'active'
                GROUP BY iso ORDER BY total_mw DESC NULLS LAST
            """)
            by_iso = cur.fetchall()

        return {
            "projects": [dict(p) for p in projects],
            "count": len(projects),
            "summary_by_iso": [dict(r) for r in by_iso],
            "source": "LBNL Queued Up / DC Hub"
        }
    finally:
        conn.close()


@router.get("/interconnect-queue/summary")
def get_queue_summary():
    """Queue summary by state — for choropleth / heat map layer."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT state, iso, project_count, total_mw,
                       active_mw, solar_mw, wind_mw, storage_mw
                FROM v_queue_by_state
                ORDER BY total_mw DESC
            """)
            rows = cur.fetchall()
        return {
            "states": [dict(r) for r in rows],
            "source": "LBNL Queued Up / DC Hub"
        }
    finally:
        conn.close()
