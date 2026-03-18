"""
DC Hub Facility Auto-Approval Pipeline v2.0
=============================================
Moves discovered_facilities → facilities table with dedup logic.
PostgreSQL/Neon compatible. Called by dchub-scheduler.py via /api/jobs/auto-approve.

Flow:
  1. Query discovered_facilities where status = 'pending'
  2. For each, check if duplicate exists in facilities (by name+city or lat/lng proximity)
  3. If net-new → INSERT into facilities, mark discovered as 'approved'
  4. If duplicate → mark discovered as 'duplicate', optionally enrich existing record
  5. If bad data → mark discovered as 'rejected' with reason

Dedup strategy:
  - Exact match: normalized name + city
  - Geo proximity: within 0.5km (~0.005 degrees) AND same provider
  - Fuzzy name: Levenshtein-like via trigram similarity (if pg_trgm available)
"""

import os
import re
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger('facility_auto_approve')


# =============================================================================
# NAME NORMALIZATION
# =============================================================================

def normalize_name(name):
    """Normalize facility name for dedup comparison."""
    if not name:
        return ''
    name = name.lower().strip()
    # Remove common suffixes
    for suffix in ['data center', 'datacenter', 'dc', 'facility', 'campus',
                   'building', 'bldg', 'site', 'colo', 'colocation',
                   'co-location', 'llc', 'inc', 'corp', 'ltd']:
        name = re.sub(rf'\b{re.escape(suffix)}\b', '', name)
    # Remove punctuation and extra whitespace
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def generate_facility_id(provider, city, state, country):
    """Generate a text ID matching the facilities table format."""
    parts = []
    if provider:
        parts.append(re.sub(r'[^a-z0-9]', '', provider.lower())[:20])
    if city:
        parts.append(re.sub(r'[^a-z0-9]', '', city.lower())[:15])
    if state:
        parts.append(state.lower()[:5])
    if country:
        parts.append(country.lower()[:3])
    base = '-'.join(parts) if parts else 'discovered'
    # Add short UUID suffix for uniqueness
    suffix = uuid.uuid4().hex[:8]
    return f"{base}-{suffix}"


# =============================================================================
# DEDUP CHECK
# =============================================================================

def check_duplicate(cur, name, city, state, latitude, longitude, provider):
    """
    Check if a discovered facility is a duplicate of an existing one.
    Returns: (is_duplicate: bool, existing_id: str|None, match_type: str|None)
    """
    norm_name = normalize_name(name)
    norm_city = (city or '').lower().strip()

    # Strategy 1: Exact normalized name + city match
    if norm_name and norm_city:
        cur.execute("""
            SELECT id, name, city FROM facilities
            WHERE LOWER(TRIM(name)) ILIKE %s
              AND LOWER(TRIM(COALESCE(city, ''))) = %s
            LIMIT 1
        """, (f'%{norm_name}%', norm_city))
        row = cur.fetchone()
        if row:
            return True, row[0], 'exact_name_city'

    # Strategy 2: Geo proximity (within ~500m) AND same provider
    if latitude and longitude and provider:
        cur.execute("""
            SELECT id, name, provider FROM facilities
            WHERE ABS(CAST(latitude AS FLOAT) - %s) < 0.005
              AND ABS(CAST(longitude AS FLOAT) - %s) < 0.005
              AND LOWER(COALESCE(provider, '')) = LOWER(%s)
            LIMIT 1
        """, (float(latitude), float(longitude), provider))
        row = cur.fetchone()
        if row:
            return True, row[0], 'geo_provider'

    # Strategy 3: Tight geo proximity (within ~100m) regardless of provider
    if latitude and longitude:
        cur.execute("""
            SELECT id, name, provider FROM facilities
            WHERE ABS(CAST(latitude AS FLOAT) - %s) < 0.001
              AND ABS(CAST(longitude AS FLOAT) - %s) < 0.001
            LIMIT 1
        """, (float(latitude), float(longitude)))
        row = cur.fetchone()
        if row:
            return True, row[0], 'geo_tight'

    return False, None, None


# =============================================================================
# VALIDATION
# =============================================================================

def validate_facility(row):
    """
    Validate a discovered facility has minimum required data.
    Returns: (is_valid: bool, rejection_reason: str|None)
    """
    name = row.get('name', '') or ''
    lat = row.get('latitude')
    lng = row.get('longitude')

    if not name or len(name.strip()) < 3:
        return False, 'name_too_short'

    if name.lower() in ['test', 'unknown', 'none', 'n/a', 'null']:
        return False, 'invalid_name'

    # Must have either location (city/state) or coordinates
    city = row.get('city', '') or ''
    country = row.get('country', '') or ''
    has_location = bool(city.strip()) or bool(country.strip())
    has_coords = lat is not None and lng is not None

    if not has_location and not has_coords:
        return False, 'no_location'

    # Basic coord sanity check
    if has_coords:
        try:
            flat, flng = float(lat), float(lng)
            if flat == 0 and flng == 0:
                return False, 'null_island'
            if abs(flat) > 90 or abs(flng) > 180:
                return False, 'coords_out_of_range'
        except (ValueError, TypeError):
            return False, 'invalid_coords'

    return True, None


# =============================================================================
# ENRICHMENT (optional — fill gaps in existing records)
# =============================================================================

def enrich_existing(cur, existing_id, discovered_row):
    """
    If we found a duplicate, check if the discovered record has data
    that the existing record is missing (e.g., coordinates, connectivity).
    """
    updates = []
    params = []

    cur.execute("""
        SELECT latitude, longitude, connectivity, source_url
        FROM facilities WHERE id = %s
    """, (existing_id,))
    existing = cur.fetchone()
    if not existing:
        return 0

    ex_lat, ex_lng, ex_conn, ex_source_url = existing

    # Fill in missing coordinates
    d_lat = discovered_row.get('latitude')
    d_lng = discovered_row.get('longitude')
    if (not ex_lat or not ex_lng) and d_lat and d_lng:
        updates.append("latitude = %s")
        params.append(str(d_lat))
        updates.append("longitude = %s")
        params.append(str(d_lng))

    # Fill in missing connectivity
    d_conn = discovered_row.get('connectivity')
    if not ex_conn and d_conn:
        updates.append("connectivity = %s")
        params.append(d_conn)

    if updates:
        params.append(existing_id)
        cur.execute(
            f"UPDATE facilities SET {', '.join(updates)} WHERE id = %s",
            params
        )
        return 1
    return 0


# =============================================================================
# MAIN APPROVAL PIPELINE
# =============================================================================

def run_auto_approve(conn, batch_size=100, dry_run=False):
    """
    Process pending discovered_facilities and move approved ones to facilities.

    Args:
        conn: psycopg2 connection (Neon)
        batch_size: max facilities to process per run
        dry_run: if True, don't commit changes

    Returns:
        dict with counts: approved, duplicates, rejected, enriched, errors
    """
    results = {
        'approved': 0,
        'duplicates': 0,
        'rejected': 0,
        'enriched': 0,
        'errors': 0,
        'batch_size': batch_size,
        'timestamp': datetime.now(timezone.utc).isoformat()
    }

    cur = conn.cursor()

    try:
        # Check if discovered_facilities table exists
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'discovered_facilities'
            )
        """)
        if not cur.fetchone()[0]:
            results['error'] = 'discovered_facilities table does not exist'
            return results

        # Auto-add columns if missing (safe migration)
        for col, col_type in [('status', 'TEXT'), ('notes', 'TEXT')]:
            try:
                cur.execute("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'discovered_facilities' AND column_name = %s
                """, (col,))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE discovered_facilities ADD COLUMN {col} {col_type}")
                    conn.commit()
                    logger.info(f"Added missing column '{col}' to discovered_facilities")
            except Exception as e:
                logger.warning(f"Column check/add for '{col}': {e}")
                conn.rollback()

        # Auto-add columns if missing (safe migration)
        for col, col_type in [('status', 'TEXT'), ('notes', 'TEXT')]:
            try:
                cur.execute("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'discovered_facilities' AND column_name = %s
                """, (col,))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE discovered_facilities ADD COLUMN {col} {col_type}")
                    conn.commit()
            except Exception:
                conn.rollback()

        # Auto-add columns if missing (safe migration)
        for col, col_type in [('status', 'TEXT'), ('notes', 'TEXT')]:
            try:
                cur.execute("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'discovered_facilities' AND column_name = %s
                """, (col,))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE discovered_facilities ADD COLUMN {col} {col_type}")
                    conn.commit()
                    logger.info(f"Added missing column '{col}' to discovered_facilities")
            except Exception as e:
                logger.warning(f"Column check/add for '{col}': {e}")
                conn.rollback()

        # Get pending discoveries
        cur.execute("""
            SELECT * FROM discovered_facilities
            WHERE COALESCE(status, 'pending') = 'pending'
            ORDER BY discovered_at ASC NULLS LAST
            LIMIT %s
        """, (batch_size,))

        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        results['pending_count'] = len(rows)

        if not rows:
            results['message'] = 'No pending discoveries to process'
            return results

        for row in rows:
            try:
                disc_id = row.get('id')
                name = row.get('name', '')
                city = row.get('city', '')
                state = row.get('state', '')
                country = row.get('country', '')
                latitude = row.get('latitude')
                longitude = row.get('longitude')
                provider = row.get('provider') or row.get('operator', '')
                source = row.get('source', 'discovery')

                # Step 1: Validate
                is_valid, rejection_reason = validate_facility(row)
                if not is_valid:
                    if not dry_run:
                        cur.execute("""
                            UPDATE discovered_facilities
                            SET status = 'rejected', notes = %s
                            WHERE id = %s
                        """, (rejection_reason, disc_id))
                    results['rejected'] += 1
                    continue

                # Step 2: Dedup check
                is_dup, existing_id, match_type = check_duplicate(
                    cur, name, city, state, latitude, longitude, provider
                )

                if is_dup:
                    if not dry_run:
                        # Mark as duplicate
                        cur.execute("""
                            UPDATE discovered_facilities
                            SET status = 'duplicate',
                                notes = %s
                            WHERE id = %s
                        """, (f'matches {existing_id} via {match_type}', disc_id))
                        # Try to enrich existing record
                        enriched = enrich_existing(cur, existing_id, row)
                        results['enriched'] += enriched
                    results['duplicates'] += 1
                    continue

                # Step 3: Insert into facilities
                new_id = generate_facility_id(provider, city, state, country)
                confidence = row.get('confidence_score') or row.get('confidence', 0.6)

                if not dry_run:
                    cur.execute("""
                        INSERT INTO facilities (
                            id, name, provider, address, city, state, country,
                            region, latitude, longitude, status, source,
                            source_url, confidence_score, connectivity
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        ON CONFLICT (id) DO NOTHING
                    """, (
                        new_id,
                        name,
                        provider,
                        row.get('address', ''),
                        city,
                        state,
                        country or 'US',
                        row.get('region', ''),
                        str(latitude) if latitude else None,
                        str(longitude) if longitude else None,
                        row.get('facility_status', 'active'),
                        source,
                        row.get('source_url', ''),
                        float(confidence) if confidence else 0.6,
                        row.get('connectivity', '')
                    ))

                    # Mark discovered as approved
                    cur.execute("""
                        UPDATE discovered_facilities
                        SET status = 'approved',
                            notes = %s
                        WHERE id = %s
                    """, (f'inserted as {new_id}', disc_id))

                results['approved'] += 1
                # Queue US facilities for permit enrichment
                if row.get('country') in ('US', 'United States', None) and new_id:
                    try:
                        cur.execute('''
                            INSERT INTO permit_enrichment_queue (facility_id)
                            VALUES (%s) ON CONFLICT (facility_id) DO NOTHING
                        ''', (str(new_id),))
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"Error processing discovered facility {row.get('id')}: {e}")
                results['errors'] += 1
                continue

        if not dry_run:
            conn.commit()
        else:
            conn.rollback()
            results['dry_run'] = True

    except Exception as e:
        conn.rollback()
        logger.error(f"Auto-approve pipeline error: {e}")
        results['error'] = str(e)
    finally:
        cur.close()

    logger.info(
        f"Auto-approve complete: {results['approved']} approved, "
        f"{results['duplicates']} duplicates, {results['rejected']} rejected, "
        f"{results['enriched']} enriched, {results['errors']} errors"
    )
    return results


# =============================================================================
# FLASK ROUTE REGISTRATION
# =============================================================================

def register_auto_approve_routes(app):
    """
    Register auto-approve routes. Call from main.py:
        from facility_auto_approve import register_auto_approve_routes
        register_auto_approve_routes(app)
    """
    from flask import jsonify, request

    def _get_neon_conn():
        """Get a Neon PostgreSQL connection."""
        import psycopg2
        db_url = os.environ.get('DATABASE_URL') or os.environ.get('NEON_DATABASE_URL')
        if not db_url:
            raise RuntimeError("No DATABASE_URL configured")
        return psycopg2.connect(db_url)

    def _check_admin_auth():
        """Verify admin/internal auth."""
        internal_key = request.headers.get('X-Internal-Key', '')
        admin_key = request.headers.get('X-Admin-Key', '')
        expected_admin = os.environ.get('DCHUB_ADMIN_KEY', '')
        if internal_key == 'dchub-internal-2024':
            return True
        if expected_admin and admin_key == expected_admin:
            return True
        return False

    @app.route('/api/jobs/auto-approve', methods=['POST'])
    def job_auto_approve():
        """Scheduler-triggered auto-approve endpoint."""
        if not _check_admin_auth():
            return jsonify({'error': 'Unauthorized'}), 401

        batch_size = request.args.get('batch_size', 100, type=int)
        dry_run = request.args.get('dry_run', 'false').lower() == 'true'

        try:
            conn = _get_neon_conn()
            results = run_auto_approve(conn, batch_size=batch_size, dry_run=dry_run)
            conn.close()
            return jsonify({'success': True, **results})
        except Exception as e:
            logger.error(f"Auto-approve job failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/admin/auto-approve/status', methods=['GET'])
    def auto_approve_status():
        """Check discovered_facilities pipeline status."""
        if not _check_admin_auth():
            return jsonify({'error': 'Unauthorized'}), 401

        try:
            conn = _get_neon_conn()
            cur = conn.cursor()

            # Check if table exists
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'discovered_facilities'
                )
            """)
            table_exists = cur.fetchone()[0]

            if not table_exists:
                cur.close()
                conn.close()
                return jsonify({
                    'success': True,
                    'table_exists': False,
                    'message': 'discovered_facilities table not found — run discovery first'
                })

            # Get counts by status
            cur.execute("""
                SELECT COALESCE(status, 'pending') as status, COUNT(*)
                FROM discovered_facilities
                GROUP BY COALESCE(status, 'pending')
                ORDER BY COUNT(*) DESC
            """)
            status_counts = {row[0]: row[1] for row in cur.fetchall()}

            # Total facilities in main table
            cur.execute("SELECT COUNT(*) FROM facilities")
            total_facilities = cur.fetchone()[0]

            # Recent approvals
            try:
                cur.execute("""
                    SELECT name, city, state, notes
                    FROM discovered_facilities
                    WHERE status = 'approved'
                    ORDER BY discovered_at DESC NULLS LAST
                    LIMIT 10
                """)
                cols = [d[0] for d in cur.description]
                recent_approvals = [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception:
                conn.rollback()
                cur = conn.cursor()
                cur.execute("""
                    SELECT name, city, state
                    FROM discovered_facilities
                    WHERE status = 'approved'
                    ORDER BY discovered_at DESC NULLS LAST
                    LIMIT 10
                """)
                cols = [d[0] for d in cur.description]
                recent_approvals = [dict(zip(cols, r)) for r in cur.fetchall()]

            cur.close()
            conn.close()

            return jsonify({
                'success': True,
                'table_exists': True,
                'pipeline_status': status_counts,
                'total_pending': status_counts.get('pending', 0),
                'total_approved': status_counts.get('approved', 0),
                'total_duplicates': status_counts.get('duplicate', 0),
                'total_rejected': status_counts.get('rejected', 0),
                'main_facilities_count': total_facilities,
                'recent_approvals': recent_approvals
            })

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/admin/auto-approve/dry-run', methods=['POST'])
    def auto_approve_dry_run():
        """Preview what would happen without committing."""
        if not _check_admin_auth():
            return jsonify({'error': 'Unauthorized'}), 401

        try:
            conn = _get_neon_conn()
            results = run_auto_approve(conn, batch_size=25, dry_run=True)
            conn.close()
            return jsonify({'success': True, **results})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    logger.info("✅ Facility Auto-Approve Pipeline v2.0 registered")
    return True
