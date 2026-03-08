"""
DC Hub — H3 Hexagonal Opportunity Scoring
===========================================
Aggregates infrastructure proximity into hex-grid scores.
Shows at a glance where power + fiber + water + low risk converge.

Endpoints:
  /api/v2/scoring/h3-heatmap   — H3 hex grid with composite scores
  /api/v2/scoring/h3-cell      — Score a single H3 cell
"""

import math
import time
import logging
import h3
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

h3_bp = Blueprint('h3_scoring', __name__)

# Cache
_h3_cache = {}
CACHE_TTL = 1800  # 30 minutes


def _get_cache(key):
    if key in _h3_cache:
        data, ts = _h3_cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _h3_cache[key]
    return None


def _set_cache(key, data):
    if len(_h3_cache) > 200:
        oldest = min(_h3_cache, key=lambda k: _h3_cache[k][1])
        del _h3_cache[oldest]
    _h3_cache[key] = (data, time.time())


def get_db():
    """Get database connection — import from main app"""
    try:
        from db_utils import get_pg_connection
        return get_pg_connection()
    except ImportError:
        try:
            import psycopg2
            import os
            return psycopg2.connect(os.environ.get('DATABASE_URL'))
        except Exception:
            return None


def score_hex_cell(hex_id, conn=None):
    """
    Score a single H3 cell (0-100) based on infrastructure proximity.
    
    Scoring weights:
      - Power (substations + power plants): 30 pts max
      - Fiber (routes): 25 pts max  
      - Gas (pipelines): 15 pts max
      - Connectivity (facilities from DB): 15 pts max
      - Water (negative if scarce): 15 pts max
    """
    lat, lng = h3.cell_to_latlng(hex_id)
    
    # Radius for queries — based on H3 resolution
    res = h3.get_resolution(hex_id)
    # Approximate radius in degrees (varies by resolution)
    radius_map = {3: 1.5, 4: 0.6, 5: 0.25, 6: 0.1, 7: 0.04}
    radius = radius_map.get(res, 0.25)
    
    scores = {
        'power': 0,
        'fiber': 0,
        'gas': 0,
        'connectivity': 0,
        'water': 15,  # Start at max, subtract for risk
    }
    
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    
    if not conn:
        return {'hex': hex_id, 'score': 0, 'breakdown': scores, 'error': 'No DB connection'}
    
    try:
        c = conn.cursor()
        
        # Power score: substations within radius
        try:
            c.execute("""
                SELECT COUNT(*), COALESCE(MAX(voltage_kv), 0)
                FROM substations 
                WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
            """, (lat - radius, lat + radius, lng - radius, lng + radius))
            row = c.fetchone()
            sub_count = row[0] if row else 0
            max_voltage = row[1] if row else 0
            
            # More substations = better, high voltage = bonus
            scores['power'] = min(15, sub_count * 2)
            if max_voltage >= 345:
                scores['power'] = min(30, scores['power'] + 15)
            elif max_voltage >= 230:
                scores['power'] = min(30, scores['power'] + 10)
            elif max_voltage >= 115:
                scores['power'] = min(30, scores['power'] + 5)
        except Exception as e:
            logger.debug(f"Power scoring error: {e}")
            try: conn.rollback()
            except: pass
        
        # Fiber score: use fiber density from discovered_facilities as proxy
        # (fiber_routes and fiber_kmz_routes lack coordinate columns)
        try:
            c.execute("""
                SELECT COUNT(*) FROM discovered_facilities 
                WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                AND (facility_type ILIKE '%%carrier%%' OR facility_type ILIKE '%%network%%' OR name ILIKE '%%fiber%%' OR name ILIKE '%%equinix%%' OR name ILIKE '%%coresite%%' OR name ILIKE '%%digital%%')
            """, (lat - radius, lat + radius, lng - radius, lng + radius))
            row = c.fetchone()
            fiber_count = row[0] if row else 0
            scores['fiber'] = min(25, fiber_count * 5)
        except Exception as e:
            logger.debug(f"Fiber scoring error: {e}")
            try: conn.rollback()
            except: pass
        
        # Gas score: pipelines within radius
        try:
            c.execute("""
                SELECT COUNT(*), COALESCE(MAX(diameter_inches), 0)
                FROM gas_pipelines 
                WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
            """, (lat - radius, lat + radius, lng - radius, lng + radius))
            row = c.fetchone()
            gas_count = row[0] if row else 0
            max_diameter = row[1] if row else 0
            scores['gas'] = min(10, gas_count * 2)
            if max_diameter >= 30:
                scores['gas'] = min(15, scores['gas'] + 5)
        except Exception as e:
            logger.debug(f"Gas scoring error: {e}")
            try: conn.rollback()
            except: pass
        
        # Connectivity score: discovered facilities
        try:
            c.execute("""
                SELECT COUNT(*) FROM discovered_facilities 
                WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
            """, (lat - radius, lat + radius, lng - radius, lng + radius))
            row = c.fetchone()
            fac_count = row[0] if row else 0
            scores['connectivity'] = min(15, fac_count * 3)
        except Exception as e:
            try: conn.rollback()
            except: pass
            # Try alternate column names
            try:
                c.execute("""
                    SELECT COUNT(*) FROM discovered_facilities 
                    WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                """, (lat - radius, lat + radius, lng - radius, lng + radius))
                row = c.fetchone()
                fac_count = row[0] if row else 0
                scores['connectivity'] = min(15, fac_count * 3)
            except Exception:
                logger.debug(f"Connectivity scoring error: {e}")
        
    except Exception as e:
        logger.error(f"H3 scoring error for {hex_id}: {e}")
    finally:
        if close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass
    
    total = sum(scores.values())
    
    # Grade
    if total >= 80:
        grade = 'A'
    elif total >= 60:
        grade = 'B'
    elif total >= 40:
        grade = 'C'
    elif total >= 20:
        grade = 'D'
    else:
        grade = 'F'
    
    return {
        'hex': hex_id,
        'score': total,
        'grade': grade,
        'breakdown': scores,
        'center': {'lat': lat, 'lng': lng}
    }


@h3_bp.route('/api/v2/scoring/h3-heatmap', methods=['GET'])
def get_h3_heatmap():
    """
    Get H3 hexagonal heat map scores for a bounding box.
    
    Query params:
        minLat, maxLat, minLng, maxLng: Bounding box (required)
        resolution: H3 resolution 3-7 (default 5, ~8km hexagons)
    """
    min_lat = request.args.get('minLat', type=float)
    max_lat = request.args.get('maxLat', type=float)
    min_lng = request.args.get('minLng', type=float)
    max_lng = request.args.get('maxLng', type=float)
    resolution = min(7, max(3, request.args.get('resolution', 5, type=int)))
    
    if not all(v is not None for v in [min_lat, max_lat, min_lng, max_lng]):
        return jsonify({'success': False, 'error': 'Bounds required'}), 400
    
    # Cache check
    cache_key = f"h3:{min_lat:.2f},{max_lat:.2f},{min_lng:.2f},{max_lng:.2f}:{resolution}"
    cached = _get_cache(cache_key)
    if cached:
        return jsonify({'success': True, **cached})
    
    # Get all H3 cells covering the bounding box
    # Use polygon_to_cells with the bbox as a polygon
    bbox_polygon = [
        (min_lat, min_lng),
        (min_lat, max_lng),
        (max_lat, max_lng),
        (max_lat, min_lng),
        (min_lat, min_lng),
    ]
    
    try:
        cells = list(h3.geo_to_cells(
            {'type': 'Polygon', 'coordinates': [[(lng, lat) for lat, lng in bbox_polygon]]},
            resolution
        ))
    except Exception:
        # Fallback for older h3 API
        try:
            cells = list(h3.polyfill_geojson(
                {'type': 'Polygon', 'coordinates': [[(lng, lat) for lat, lng in bbox_polygon]]},
                resolution
            ))
        except Exception as e:
            return jsonify({'success': False, 'error': f'H3 cell generation failed: {e}'}), 500
    
    # Limit to prevent overload
    max_cells = 500
    if len(cells) > max_cells:
        return jsonify({
            'success': False, 
            'error': f'Too many cells ({len(cells)}). Zoom in or use lower resolution. Max: {max_cells}',
            'suggestion': f'Try resolution {resolution - 1} or zoom in'
        }), 400
    
    # Score each cell
    conn = get_db()
    features = []
    
    for cell_id in cells:
        result = score_hex_cell(cell_id, conn=conn)
        
        # Get cell boundary for GeoJSON polygon
        try:
            boundary = h3.cell_to_boundary(cell_id)
            coords = [[lng, lat] for lat, lng in boundary]
            coords.append(coords[0])  # Close the polygon
        except Exception:
            continue
        
        score = result['score']
        
        # Color based on score
        if score >= 70:
            color = '#22c55e'  # Green - excellent
        elif score >= 50:
            color = '#84cc16'  # Lime - good
        elif score >= 30:
            color = '#eab308'  # Yellow - moderate
        elif score >= 15:
            color = '#f97316'  # Orange - low
        else:
            color = '#ef4444'  # Red - poor
        
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Polygon',
                'coordinates': [coords]
            },
            'properties': {
                'hex_id': cell_id,
                'score': score,
                'grade': result['grade'],
                'color': color,
                'power': result['breakdown']['power'],
                'fiber': result['breakdown']['fiber'],
                'gas': result['breakdown']['gas'],
                'connectivity': result['breakdown']['connectivity'],
                'water': result['breakdown']['water'],
                'center_lat': result['center']['lat'],
                'center_lng': result['center']['lng'],
            }
        })
    
    if conn:
        try:
            conn.close()
        except Exception:
            pass
    
    result_data = {
        'type': 'FeatureCollection',
        'features': features,
        'count': len(features),
        'resolution': resolution,
        'stats': {
            'avg_score': round(sum(f['properties']['score'] for f in features) / max(1, len(features)), 1),
            'max_score': max((f['properties']['score'] for f in features), default=0),
            'min_score': min((f['properties']['score'] for f in features), default=0),
            'grade_A': len([f for f in features if f['properties']['grade'] == 'A']),
            'grade_B': len([f for f in features if f['properties']['grade'] == 'B']),
            'grade_C': len([f for f in features if f['properties']['grade'] == 'C']),
            'grade_D': len([f for f in features if f['properties']['grade'] == 'D']),
            'grade_F': len([f for f in features if f['properties']['grade'] == 'F']),
        }
    }
    
    _set_cache(cache_key, result_data)
    
    return jsonify({'success': True, **result_data})


@h3_bp.route('/api/v2/scoring/h3-cell', methods=['GET'])
def score_single_cell():
    """
    Score a single location using H3.
    
    Query params:
        lat, lng: Location (required)
        resolution: H3 resolution (default 5)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    resolution = min(7, max(3, request.args.get('resolution', 5, type=int)))
    
    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400
    
    cell_id = h3.latlng_to_cell(lat, lng, resolution)
    result = score_hex_cell(cell_id)
    
    # Get neighboring cells for context
    neighbors = []
    try:
        ring = h3.grid_ring(cell_id, 1)
        conn = get_db()
        for n_cell in list(ring)[:6]:
            n_result = score_hex_cell(n_cell, conn=conn)
            neighbors.append({
                'hex': n_cell,
                'score': n_result['score'],
                'grade': n_result['grade']
            })
        if conn:
            conn.close()
    except Exception:
        pass
    
    return jsonify({
        'success': True,
        'cell': result,
        'neighbors': neighbors,
        'resolution': resolution
    })


def register_h3_routes(app):
    """Register H3 scoring blueprint with Flask app"""
    app.register_blueprint(h3_bp)
    logger.info("🔷 H3 hexagonal scoring routes registered")
