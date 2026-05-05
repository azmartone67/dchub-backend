"""
DC Hub — Submarine Cable Ingestion (TeleGeography)
═══════════════════════════════════════════════════
Fetches submarine cable routes and landing points from TeleGeography's
free public API and stores them in PostgreSQL.

Sources:
  - https://www.submarinecablemap.com/api/v3/cable/cable-geo.json  (cable routes)
  - https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json  (landing points)

Run: POST /api/jobs/subsea-sync  (admin/internal key required)
Schedule: Weekly via crawler_scheduler.py

v1.0 — March 2026
"""

import json
import logging
import os
from datetime import datetime

import requests

logger = logging.getLogger('dchub-subsea')

# ─────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────
CABLE_GEO_URL = "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json"
LANDING_POINT_URL = "https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json"

# Backup/alternative URLs (TeleGeography also serves via GitHub)
CABLE_GEO_BACKUP = "https://raw.githubusercontent.com/telegeography/www.submarinecablemap.com/master/web/public/api/v3/cable/cable-geo.json"
LANDING_POINT_BACKUP = "https://raw.githubusercontent.com/telegeography/www.submarinecablemap.com/master/web/public/api/v3/landing-point/landing-point-geo.json"


# ─────────────────────────────────────────────────────────────
# TABLE CREATION
# ─────────────────────────────────────────────────────────────
def init_subsea_tables(get_db):
    """Create subsea cable tables in PostgreSQL."""
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS subsea_cables (
                id SERIAL PRIMARY KEY,
                cable_id TEXT UNIQUE,
                name TEXT NOT NULL,
                color TEXT,
                owners TEXT,
                url TEXT,
                length_km REAL,
                rfs_year INTEGER,
                rfs_date TEXT,
                is_planned BOOLEAN DEFAULT FALSE,
                geometry_geojson TEXT,
                landing_points_json TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS subsea_landing_points (
                id SERIAL PRIMARY KEY,
                point_id TEXT UNIQUE,
                name TEXT NOT NULL,
                country TEXT,
                country_code TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                cable_ids TEXT,
                cable_count INTEGER DEFAULT 0,
                is_major_hub BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes for fast spatial/text queries
        c.execute("CREATE INDEX IF NOT EXISTS idx_subsea_cables_name ON subsea_cables(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_subsea_cables_rfs ON subsea_cables(rfs_year)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_subsea_lp_country ON subsea_landing_points(country_code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_subsea_lp_coords ON subsea_landing_points(latitude, longitude)")

        conn.commit()
        logger.info("✅ Subsea cable tables initialized")
    except Exception as e:
        logger.warning(f"Subsea tables init: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────
def _fetch_json(url, backup_url=None, timeout=60):
    """Fetch JSON from URL with fallback."""
    headers = {
        'User-Agent': 'DCHub-Intelligence/1.0 (dchub.cloud; data-center-research)',
        'Accept': 'application/json',
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Primary fetch failed ({url}): {e}")
        if backup_url:
            try:
                resp = requests.get(backup_url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                logger.info(f"✅ Backup fetch succeeded: {backup_url}")
                return resp.json()
            except Exception as e2:
                logger.error(f"Backup fetch also failed: {e2}")
        return None


def _parse_cable_length(length_str):
    """Parse cable length from various formats like '12,000 km' or '12000'."""
    if not length_str:
        return None
    try:
        cleaned = str(length_str).replace(',', '').replace('km', '').replace('mi', '').strip()
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _extract_rfs_year(rfs_str):
    """Extract year from RFS date string like '2024', '2024 Q2', 'Expected 2025'."""
    if not rfs_str:
        return None
    try:
        import re
        match = re.search(r'(\d{4})', str(rfs_str))
        return int(match.group(1)) if match else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# CABLE INGESTION
# ─────────────────────────────────────────────────────────────
def ingest_cables(get_db):
    """Fetch and store submarine cable routes."""
    data = _fetch_json(CABLE_GEO_URL, CABLE_GEO_BACKUP)
    if not data:
        return {'success': False, 'error': 'Failed to fetch cable data'}

    # TeleGeography returns GeoJSON FeatureCollection
    features = data.get('features', [])
    if not features and isinstance(data, list):
        features = data  # Some versions return array directly

    conn = None
    inserted = 0
    updated = 0
    errors = 0

    try:
        conn = get_db()
        c = conn.cursor()

        for feat in features:
            try:
                props = feat.get('properties', {})
                geom = feat.get('geometry', {})

                cable_id = str(props.get('id', props.get('cable_id', '')))
                if not cable_id:
                    continue

                name = props.get('name', props.get('cable_name', ''))
                color = props.get('color', '')
                url = props.get('url', '')

                # Owners can be string or list
                owners_raw = props.get('owners', props.get('owner', ''))
                if isinstance(owners_raw, list):
                    owners = ', '.join(str(o.get('name', o) if isinstance(o, dict) else o) for o in owners_raw)
                else:
                    owners = str(owners_raw) if owners_raw else ''

                length_km = _parse_cable_length(props.get('length', props.get('length_km', '')))
                rfs_str = props.get('rfs', props.get('rfs_date', ''))
                rfs_year = _extract_rfs_year(rfs_str)
                is_planned = bool(props.get('is_planned', False)) or (rfs_year and rfs_year > datetime.utcnow().year)

                # Landing points from properties
                lps = props.get('landing_points', [])
                landing_json = json.dumps(lps) if lps else None

                # Store geometry as GeoJSON string
                geometry_str = json.dumps(geom) if geom else None

                # Upsert
                c.execute("""
                    INSERT INTO subsea_cables
                        (cable_id, name, color, owners, url, length_km, rfs_year, rfs_date,
                         is_planned, geometry_geojson, landing_points_json, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (cable_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        owners = EXCLUDED.owners,
                        length_km = EXCLUDED.length_km,
                        rfs_year = EXCLUDED.rfs_year,
                        rfs_date = EXCLUDED.rfs_date,
                        is_planned = EXCLUDED.is_planned,
                        geometry_geojson = EXCLUDED.geometry_geojson,
                        landing_points_json = EXCLUDED.landing_points_json,
                        updated_at = NOW()
                """, (cable_id, name, color, owners, url, length_km, rfs_year,
                      str(rfs_str), is_planned, geometry_str, landing_json))

                if c.rowcount > 0:
                    # Check if it was insert or update
                    inserted += 1  # Simplified — counts both

            except Exception as e:
                errors += 1
                if errors < 5:
                    logger.warning(f"Cable ingestion error: {e}")

        conn.commit()
        logger.info(f"✅ Subsea cables: {inserted} upserted, {errors} errors from {len(features)} features")

    except Exception as e:
        logger.error(f"Cable ingestion failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return {
        'success': True,
        'source': 'TeleGeography',
        'total_features': len(features),
        'upserted': inserted,
        'errors': errors,
    }


# ─────────────────────────────────────────────────────────────
# LANDING POINT INGESTION
# ─────────────────────────────────────────────────────────────
def ingest_landing_points(get_db):
    """Fetch and store submarine cable landing points."""
    data = _fetch_json(LANDING_POINT_URL, LANDING_POINT_BACKUP)
    if not data:
        return {'success': False, 'error': 'Failed to fetch landing point data'}

    features = data.get('features', [])
    if not features and isinstance(data, list):
        features = data

    conn = None
    inserted = 0
    errors = 0

    try:
        conn = get_db()
        c = conn.cursor()

        for feat in features:
            try:
                props = feat.get('properties', {})
                geom = feat.get('geometry', {})

                point_id = str(props.get('id', props.get('point_id', '')))
                if not point_id:
                    continue

                name = props.get('name', '')
                country = props.get('country', '')
                country_code = props.get('country_code', props.get('iso2', ''))

                # Extract coordinates from geometry
                coords = geom.get('coordinates', [])
                longitude = float(coords[0]) if len(coords) >= 2 else None
                latitude = float(coords[1]) if len(coords) >= 2 else None

                # Cable connections
                cables = props.get('cables', props.get('cable_ids', []))
                if isinstance(cables, list):
                    cable_ids = json.dumps(cables)
                    cable_count = len(cables)
                else:
                    cable_ids = str(cables)
                    cable_count = 0

                # Major hub = 10+ cable connections
                is_major = cable_count >= 10

                c.execute("""
                    INSERT INTO subsea_landing_points
                        (point_id, name, country, country_code, latitude, longitude,
                         cable_ids, cable_count, is_major_hub, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (point_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        country = EXCLUDED.country,
                        latitude = EXCLUDED.latitude,
                        longitude = EXCLUDED.longitude,
                        cable_ids = EXCLUDED.cable_ids,
                        cable_count = EXCLUDED.cable_count,
                        is_major_hub = EXCLUDED.is_major_hub,
                        updated_at = NOW()
                """, (point_id, name, country, country_code, latitude, longitude,
                      cable_ids, cable_count, is_major))

                inserted += 1

            except Exception as e:
                errors += 1
                if errors < 5:
                    logger.warning(f"Landing point error: {e}")

        conn.commit()
        logger.info(f"✅ Landing points: {inserted} upserted, {errors} errors from {len(features)} features")

    except Exception as e:
        logger.error(f"Landing point ingestion failed: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return {
        'success': True,
        'source': 'TeleGeography',
        'total_features': len(features),
        'upserted': inserted,
        'errors': errors,
    }


# ─────────────────────────────────────────────────────────────
# MAIN SYNC FUNCTION
# ─────────────────────────────────────────────────────────────
def run_subsea_sync(get_db):
    """Full sync: cables + landing points."""
    results = {
        'source': 'TeleGeography Submarine Cable Map',
        'timestamp': datetime.utcnow().isoformat(),
    }

    # Init tables first
    init_subsea_tables(get_db)

    # Cables
    cable_result = ingest_cables(get_db)
    results['cables'] = cable_result

    # Landing points
    lp_result = ingest_landing_points(get_db)
    results['landing_points'] = lp_result

    results['success'] = cable_result.get('success', False) and lp_result.get('success', False)
    results['total_new'] = (cable_result.get('upserted', 0) + lp_result.get('upserted', 0))

    logger.info(f"🌊 Subsea sync complete: {results['total_new']} total records")
    return results


# ─────────────────────────────────────────────────────────────
# API ENDPOINTS (register with Flask app)
# ─────────────────────────────────────────────────────────────
def register_subsea_routes(app, get_db):
    """Register subsea cable API routes with the Flask app."""
    from flask import jsonify, request

    @app.route('/api/v1/subsea/cables', methods=['GET'])
    def subsea_cables_api():
        """Get submarine cable routes. Optional filters: country, year, planned."""
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            query = "SELECT cable_id, name, owners, length_km, rfs_year, is_planned, geometry_geojson FROM subsea_cables WHERE 1=1"
            params = []

            # Filter by owner/country in cable name
            search = request.args.get('search', '')
            if search:
                query += " AND (LOWER(name) LIKE %s OR LOWER(owners) LIKE %s)"
                params.extend([f'%{search.lower()}%', f'%{search.lower()}%'])

            year = request.args.get('year', type=int)
            if year:
                query += " AND rfs_year = %s"
                params.append(year)

            planned = request.args.get('planned')
            if planned == 'true':
                query += " AND is_planned = TRUE"
            elif planned == 'false':
                query += " AND is_planned = FALSE"

            include_geometry = request.args.get('geometry', 'false').lower() == 'true'

            query += " ORDER BY rfs_year DESC NULLS LAST, name"
            limit = min(request.args.get('limit', 100, type=int), 500)
            query += f" LIMIT {limit}"

            c.execute(query, params)
            cables = []
            for row in c.fetchall():
                cable = {
                    'cable_id': row[0], 'name': row[1], 'owners': row[2],
                    'length_km': row[3], 'rfs_year': row[4], 'is_planned': row[5],
                }
                if include_geometry and row[6]:
                    try:
                        cable['geometry'] = json.loads(row[6])
                    except Exception:
                        pass
                cables.append(cable)

            # Total count
            c.execute("SELECT COUNT(*) FROM subsea_cables")
            total = c.fetchone()[0]

            return jsonify({
                'success': True,
                'cables': cables,
                'total': total,
                'returned': len(cables),
                'source': 'TeleGeography via DC Hub Intelligence'
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/v1/subsea/landing-points', methods=['GET'])
    def subsea_landing_points_api():
        """Get submarine cable landing points. Optional: country, major_hubs_only."""
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            query = "SELECT point_id, name, country, country_code, latitude, longitude, cable_count, is_major_hub FROM subsea_landing_points WHERE 1=1"
            params = []

            country = request.args.get('country', '')
            if country:
                query += " AND (LOWER(country) LIKE %s OR LOWER(country_code) = %s)"
                params.extend([f'%{country.lower()}%', country.lower()])

            if request.args.get('major_only', 'false').lower() == 'true':
                query += " AND is_major_hub = TRUE"

            query += " ORDER BY cable_count DESC, name"
            limit = min(request.args.get('limit', 200, type=int), 1000)
            query += f" LIMIT {limit}"

            c.execute(query, params)
            points = []
            for row in c.fetchall():
                points.append({
                    'point_id': row[0], 'name': row[1], 'country': row[2],
                    'country_code': row[3], 'lat': row[4], 'lng': row[5],
                    'cable_count': row[6], 'is_major_hub': row[7],
                })

            c.execute("SELECT COUNT(*) FROM subsea_landing_points")
            total = c.fetchone()[0]

            return jsonify({
                'success': True,
                'landing_points': points,
                'total': total,
                'returned': len(points),
                'source': 'TeleGeography via DC Hub Intelligence'
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/v1/subsea/nearby', methods=['GET'])
    def subsea_nearby_api():
        """Find landing points near a lat/lng coordinate. For site selection connectivity scoring."""
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius_km = min(request.args.get('radius_km', 100, type=float), 500)

        if lat is None or lng is None:
            return jsonify({'error': 'lat and lng required'}), 400

        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            # Haversine approximation in SQL (1 degree ≈ 111km)
            deg_radius = radius_km / 111.0
            c.execute("""
                SELECT point_id, name, country, country_code, latitude, longitude,
                       cable_count, is_major_hub,
                       (6371 * acos(
                           cos(radians(%s)) * cos(radians(latitude)) *
                           cos(radians(longitude) - radians(%s)) +
                           sin(radians(%s)) * sin(radians(latitude))
                       )) AS distance_km
                FROM subsea_landing_points
                WHERE latitude BETWEEN %s AND %s
                  AND longitude BETWEEN %s AND %s
                ORDER BY distance_km
                LIMIT 20
            """, (lat, lng, lat,
                  lat - deg_radius, lat + deg_radius,
                  lng - deg_radius, lng + deg_radius))

            points = []
            for row in c.fetchall():
                points.append({
                    'point_id': row[0], 'name': row[1], 'country': row[2],
                    'country_code': row[3], 'lat': row[4], 'lng': row[5],
                    'cable_count': row[6], 'is_major_hub': row[7],
                    'distance_km': round(row[8], 1) if row[8] else None,
                })

            return jsonify({
                'success': True,
                'query': {'lat': lat, 'lng': lng, 'radius_km': radius_km},
                'landing_points': points,
                'total_nearby': len(points),
                'connectivity_note': f"{'Excellent' if any(p['cable_count'] >= 10 for p in points) else 'Good' if points else 'Limited'} subsea connectivity within {radius_km}km"
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    logger.info("🌊 Subsea cable routes registered: /api/v1/subsea/cables, /api/v1/subsea/landing-points, /api/v1/subsea/nearby")
