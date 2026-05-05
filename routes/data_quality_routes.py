from internal_auth import is_valid_internal_key
"""
DC Hub Data Quality Routes Blueprint
======================================
Phase 4: Data Quality Display

Provides:
  - Confidence score calculation based on field completeness
  - /api/v1/data-quality — global completeness metrics
  - /api/v1/data-quality/facility/<id> — per-facility quality breakdown
  - /api/v1/data-quality/recalculate — trigger confidence score recalculation
  - min_confidence filter support for facility search

Confidence score formula (0.0 — 1.0):
  Each field contributes a weighted score based on presence and quality.
  Weights reflect data utility for site selection and market intelligence.

Actual facilities table columns (Neon):
  id, name, provider, address, city, state, country, region, latitude, longitude,
  power_mw, sqft, status, tier, certifications, connectivity, source, source_url,
  source_id, confidence, first_seen, last_updated, raw_data, confidence_score

Usage in main.py:
    from routes.data_quality_routes import data_quality_bp, init_data_quality_routes
    app.register_blueprint(data_quality_bp)
    init_data_quality_routes(get_pg_connection_fn, return_pg_connection_fn)

Dependencies: Flask, psycopg2 (via main.py connection pool)
"""

import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger('data_quality')

data_quality_bp = Blueprint('data_quality', __name__)

# ---------------------------------------------------------------------------
# Late-binding DB connection (injected from main.py)
# ---------------------------------------------------------------------------
_get_pg_connection = None
_return_pg_connection = None


def init_data_quality_routes(get_pg_fn, return_pg_fn):
    global _get_pg_connection, _return_pg_connection
    _get_pg_connection = get_pg_fn
    _return_pg_connection = return_pg_fn
    logger.info("Data quality routes initialized")


def _get_conn():
    if _get_pg_connection is None:
        raise RuntimeError("data_quality_routes not initialized — call init_data_quality_routes()")
    return _get_pg_connection()


def _return_conn(conn):
    if _return_pg_connection and conn:
        try:
            _return_pg_connection(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Confidence Score Weights — matched to actual facilities table columns
# ---------------------------------------------------------------------------
# Weights sum to 1.0. Each field scores 0 or 1 (present/absent),
# multiplied by its weight. Some fields get partial credit for quality.

FIELD_WEIGHTS = {
    'name':           0.12,   # Facility name
    'provider':       0.15,   # Operator/provider — critical for market intel
    'latitude':       0.10,   # Geolocation
    'longitude':      0.10,   # Geolocation
    'city':           0.08,   # Location granularity
    'state':          0.05,   # Location granularity
    'country':        0.05,   # Location granularity
    'address':        0.10,   # Full street address — rare but valuable
    'power_mw':       0.15,   # Power capacity — most valuable for site selection
    'status':         0.05,   # Operational status
    'source':         0.05,   # Data source identifier
}
# Total: 1.00


def calculate_confidence(row_dict):
    """
    Calculate confidence score (0.0 — 1.0) for a facility record.

    Args:
        row_dict: dict with facility column names as keys

    Returns:
        float: confidence score between 0.0 and 1.0
    """
    score = 0.0
    for field, weight in FIELD_WEIGHTS.items():
        val = row_dict.get(field)
        if val is not None and val != '' and val != 0:
            if field == 'provider' and str(val) in ('Unknown', 'unknown', 'N/A', ''):
                score += weight * 0.25
            elif field == 'name' and ('unknown' in str(val).lower() or 'untitled' in str(val).lower()):
                score += weight * 0.25
            elif field in ('latitude', 'longitude') and val == 0:
                pass  # 0,0 coordinates are not valid
            else:
                score += weight
    return round(score, 4)


# ---------------------------------------------------------------------------
# SQL: Bulk recalculate confidence scores
# ---------------------------------------------------------------------------

RECALCULATE_SQL = """
UPDATE facilities SET confidence_score = (
    (CASE WHEN name IS NOT NULL AND name != '' THEN 0.12 ELSE 0 END) +
    (CASE WHEN provider IS NOT NULL AND provider != '' AND provider NOT IN ('Unknown', 'unknown', 'N/A')
          THEN 0.15
          WHEN provider IN ('Unknown', 'unknown', 'N/A') THEN 0.04
          ELSE 0 END) +
    (CASE WHEN latitude IS NOT NULL AND latitude != 0 THEN 0.10 ELSE 0 END) +
    (CASE WHEN longitude IS NOT NULL AND longitude != 0 THEN 0.10 ELSE 0 END) +
    (CASE WHEN city IS NOT NULL AND city != '' THEN 0.08 ELSE 0 END) +
    (CASE WHEN state IS NOT NULL AND state != '' THEN 0.05 ELSE 0 END) +
    (CASE WHEN country IS NOT NULL AND country != '' THEN 0.05 ELSE 0 END) +
    (CASE WHEN address IS NOT NULL AND address != '' THEN 0.10 ELSE 0 END) +
    (CASE WHEN power_mw IS NOT NULL AND power_mw > 0 THEN 0.15 ELSE 0 END) +
    (CASE WHEN status IS NOT NULL AND status != '' THEN 0.05 ELSE 0 END) +
    (CASE WHEN source IS NOT NULL AND source != '' THEN 0.05 ELSE 0 END)
);
"""

COMPLETENESS_SQL = """
SELECT
    COUNT(*) AS total,
    COUNT(NULLIF(name, '')) AS has_name,
    COUNT(NULLIF(provider, '')) AS has_provider,
    COUNT(CASE WHEN latitude IS NOT NULL AND latitude != 0 THEN 1 END) AS has_coords,
    COUNT(NULLIF(city, '')) AS has_city,
    COUNT(NULLIF(state, '')) AS has_state,
    COUNT(NULLIF(country, '')) AS has_country,
    COUNT(NULLIF(address, '')) AS has_address,
    COUNT(CASE WHEN power_mw IS NOT NULL AND power_mw > 0 THEN 1 END) AS has_power,
    COUNT(CASE WHEN sqft IS NOT NULL AND sqft > 0 THEN 1 END) AS has_sqft,
    COUNT(NULLIF(status, '')) AS has_status,
    COUNT(NULLIF(source_url, '')) AS has_source_url,
    COUNT(NULLIF(source, '')) AS has_source,
    COUNT(NULLIF(region, '')) AS has_region,
    COUNT(CASE WHEN tier IS NOT NULL AND tier > 0 THEN 1 END) AS has_tier,
    COUNT(NULLIF(connectivity, '')) AS has_connectivity,
    COUNT(NULLIF(certifications, '')) AS has_certifications,
    ROUND(AVG(confidence_score)::numeric, 4) AS avg_confidence,
    ROUND(MIN(confidence_score)::numeric, 4) AS min_confidence,
    ROUND(MAX(confidence_score)::numeric, 4) AS max_confidence,
    COUNT(CASE WHEN confidence_score >= 0.8 THEN 1 END) AS high_confidence,
    COUNT(CASE WHEN confidence_score >= 0.5 AND confidence_score < 0.8 THEN 1 END) AS medium_confidence,
    COUNT(CASE WHEN confidence_score < 0.5 THEN 1 END) AS low_confidence
FROM facilities;
"""

CONFIDENCE_DISTRIBUTION_SQL = """
SELECT
    CASE
        WHEN confidence_score >= 0.9 THEN '0.9-1.0'
        WHEN confidence_score >= 0.8 THEN '0.8-0.9'
        WHEN confidence_score >= 0.7 THEN '0.7-0.8'
        WHEN confidence_score >= 0.6 THEN '0.6-0.7'
        WHEN confidence_score >= 0.5 THEN '0.5-0.6'
        WHEN confidence_score >= 0.4 THEN '0.4-0.5'
        WHEN confidence_score >= 0.3 THEN '0.3-0.4'
        ELSE '0.0-0.3'
    END AS bucket,
    COUNT(*) AS count
FROM facilities
GROUP BY bucket
ORDER BY bucket DESC;
"""

SOURCE_QUALITY_SQL = """
SELECT
    source,
    COUNT(*) AS total,
    ROUND(AVG(confidence_score)::numeric, 4) AS avg_confidence,
    COUNT(CASE WHEN power_mw IS NOT NULL AND power_mw > 0 THEN 1 END) AS has_power,
    COUNT(NULLIF(address, '')) AS has_address,
    COUNT(CASE WHEN latitude IS NOT NULL AND latitude != 0 THEN 1 END) AS has_coords
FROM facilities
GROUP BY source
ORDER BY total DESC;
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@data_quality_bp.route('/api/v1/data-quality', methods=['GET'])
def data_quality_overview():
    """
    Global data quality metrics — completeness by field, confidence distribution,
    quality by source.
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()

        # Field completeness
        cur.execute(COMPLETENESS_SQL)
        row = cur.fetchone()
        cols = [desc[0] for desc in cur.description]
        stats = dict(zip(cols, row))

        total = stats['total']

        field_completeness = {
            'name':          _pct(total, stats['has_name']),
            'provider':      _pct(total, stats['has_provider']),
            'coordinates':   _pct(total, stats['has_coords']),
            'city':          _pct(total, stats['has_city']),
            'state':         _pct(total, stats['has_state']),
            'country':       _pct(total, stats['has_country']),
            'address':       _pct(total, stats['has_address']),
            'power_mw':      _pct(total, stats['has_power']),
            'sqft':          _pct(total, stats['has_sqft']),
            'status':        _pct(total, stats['has_status']),
            'source_url':    _pct(total, stats['has_source_url']),
            'source':        _pct(total, stats['has_source']),
            'region':        _pct(total, stats['has_region']),
            'tier':          _pct(total, stats['has_tier']),
            'connectivity':  _pct(total, stats['has_connectivity']),
            'certifications': _pct(total, stats['has_certifications']),
        }

        # Confidence distribution
        cur.execute(CONFIDENCE_DISTRIBUTION_SQL)
        distribution = {}
        for bucket_row in cur.fetchall():
            distribution[bucket_row[0]] = bucket_row[1]

        # Quality by source
        cur.execute(SOURCE_QUALITY_SQL)
        source_cols = [desc[0] for desc in cur.description]
        sources = []
        for src_row in cur.fetchall():
            src = dict(zip(source_cols, src_row))
            src['avg_confidence'] = float(src['avg_confidence']) if src['avg_confidence'] else 0
            sources.append(src)

        cur.close()

        return jsonify({
            'success': True,
            'data': {
                'total_facilities': total,
                'confidence': {
                    'average': float(stats['avg_confidence']) if stats['avg_confidence'] else 0,
                    'min': float(stats['min_confidence']) if stats['min_confidence'] else 0,
                    'max': float(stats['max_confidence']) if stats['max_confidence'] else 0,
                    'high_count': stats['high_confidence'],
                    'medium_count': stats['medium_confidence'],
                    'low_count': stats['low_confidence'],
                },
                'confidence_distribution': distribution,
                'field_completeness': field_completeness,
                'quality_by_source': sources,
                'badge_thresholds': {
                    'high': 0.8,
                    'medium': 0.5,
                    'low': 0.0,
                    'description': 'high = 0.8+, medium = 0.5-0.79, low = below 0.5'
                }
            }
        })

    except Exception as e:
        logger.error(f"Data quality overview error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _return_conn(conn)


@data_quality_bp.route('/api/v1/data-quality/facility/<int:facility_id>', methods=['GET'])
def facility_quality(facility_id):
    """
    Per-facility data quality breakdown — shows which fields are populated,
    confidence score, and badge level.
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT name, provider, latitude, longitude, city, state, country,
                   address, power_mw, sqft, status, source_url, source, region,
                   tier, certifications, connectivity, confidence_score
            FROM facilities WHERE id = %s
        """, (facility_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            return jsonify({'success': False, 'error': 'Facility not found'}), 404

        cols = ['name', 'provider', 'latitude', 'longitude', 'city', 'state',
                'country', 'address', 'power_mw', 'sqft', 'status', 'source_url',
                'source', 'region', 'tier', 'certifications', 'connectivity',
                'confidence_score']
        fac = dict(zip(cols, row))

        # Calculate fresh confidence
        calculated = calculate_confidence(fac)
        stored = float(fac['confidence_score']) if fac['confidence_score'] else 0

        # Build field-by-field breakdown (scored fields only)
        field_status = {}
        for field, weight in FIELD_WEIGHTS.items():
            val = fac.get(field)
            populated = val is not None and val != '' and val != 0
            if field in ('latitude', 'longitude') and val == 0:
                populated = False
            field_status[field] = {
                'populated': populated,
                'weight': weight,
                'contribution': weight if populated else 0,
            }

        # Badge
        if calculated >= 0.8:
            badge = 'high'
        elif calculated >= 0.5:
            badge = 'medium'
        else:
            badge = 'low'

        # Missing fields ranked by impact
        missing = []
        for field, info in sorted(field_status.items(), key=lambda x: -x[1]['weight']):
            if not info['populated']:
                missing.append({'field': field, 'weight': info['weight']})

        # Bonus fields (not scored but useful context)
        bonus_populated = []
        for bf in ('sqft', 'source_url', 'region', 'tier', 'certifications', 'connectivity'):
            val = fac.get(bf)
            if val is not None and val != '' and val != 0:
                bonus_populated.append(bf)

        return jsonify({
            'success': True,
            'data': {
                'facility_id': facility_id,
                'confidence_score': calculated,
                'stored_score': stored,
                'badge': badge,
                'fields': field_status,
                'missing_fields': missing,
                'potential_score': min(1.0, calculated + sum(m['weight'] for m in missing)),
                'bonus_fields_populated': bonus_populated,
            }
        })

    except Exception as e:
        logger.error(f"Facility quality error for {facility_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _return_conn(conn)


@data_quality_bp.route('/api/v1/data-quality/recalculate', methods=['POST'])
def recalculate_confidence():
    """
    Trigger bulk recalculation of confidence scores for all facilities.
    Protected — requires admin auth or internal key.
    """
    internal_key = request.headers.get('X-Internal-Key')
    if not is_valid_internal_key(internal_key):  # centralized
        auth = request.headers.get('Authorization')
        if not auth:
            return jsonify({'error': 'authentication_required'}), 401

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()

        # Before stats
        cur.execute("SELECT ROUND(AVG(confidence_score)::numeric, 4), COUNT(*) FROM facilities")
        before = cur.fetchone()

        # Run recalculation
        cur.execute(RECALCULATE_SQL)
        updated = cur.rowcount
        conn.commit()

        # After stats
        cur.execute("SELECT ROUND(AVG(confidence_score)::numeric, 4), COUNT(*) FROM facilities")
        after = cur.fetchone()

        cur.close()

        logger.info(f"Confidence recalculated: {updated} facilities, avg {before[0]} -> {after[0]}")

        return jsonify({
            'success': True,
            'data': {
                'facilities_updated': updated,
                'before_avg_confidence': float(before[0]) if before[0] else 0,
                'after_avg_confidence': float(after[0]) if after[0] else 0,
            }
        })

    except Exception as e:
        logger.error(f"Confidence recalculation error: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _return_conn(conn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(total, count):
    """Return completeness percentage."""
    if total == 0:
        return 0.0
    return round(count / total * 100, 2)


def get_min_confidence_clause():
    """
    Returns SQL WHERE clause fragment for min_confidence filtering.
    Call from facility search endpoints.

    Usage:
        clause, params = get_min_confidence_clause()
        query += clause
        params_list.extend(params)
    """
    min_conf = request.args.get('min_confidence')
    if min_conf:
        try:
            val = float(min_conf)
            if 0 <= val <= 1:
                return " AND confidence_score >= %s", [val]
        except (ValueError, TypeError):
            pass
    return "", []


# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------
print("   📊 Data quality routes loaded:")
print("      /api/v1/data-quality")
print("      /api/v1/data-quality/facility/<id>")
print("      /api/v1/data-quality/recalculate (POST)")
