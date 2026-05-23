import hashlib
import time
import re
import math
import threading
from datetime import datetime
from collections import defaultdict
from db_utils import get_db

TRUSTED_SOURCES = {'peeringdb', 'openstreetmap'}

BATCH_SIZE = 50
BATCH_DELAY = 2
MAX_PER_RUN = 100

_cache_lock = threading.Lock()
_cached_name_index = None
_cached_geo_index = None
_cache_built_at = None
_CACHE_TTL = 1800


def _get_db():
    return get_db()


def init_approval_table():
    conn = _get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS discovery_approvals (
            id SERIAL PRIMARY KEY,
            discovered_facility_id INTEGER,
            source TEXT,
            action TEXT,
            match_reason TEXT,
            promoted_facility_id TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def normalize_name(name):
    if not name:
        return ""
    name = name.lower().strip()
    for suffix in ['llc', 'inc', 'ltd', 'corp', 'data center', 'datacenter', 'dc', 'colocation', 'colo']:
        name = name.replace(suffix, '')
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def build_name_index(facilities):
    name_set = {}
    for f in facilities:
        key = (normalize_name(f['name']), (f.get('country') or '').lower().strip())
        if key[0]:
            name_set[key] = f['id']
    return name_set


def build_geo_index(facilities):
    geo_index = defaultdict(list)
    for f in facilities:
        if f.get('latitude') and f.get('longitude'):
            try:
                key = (round(float(f['latitude']), 2), round(float(f['longitude']), 2))
                geo_index[key].append(f['id'])
            except (ValueError, TypeError):
                pass
    return geo_index


def _load_facilities_from_db():
    # psycopg2 cursor.execute() returns None; chaining .fetchall() on it
    # raised "NoneType has no attribute fetchall" in JOB auto-approve.
    # Split into two statements + guarantee conn.close on every exit.
    conn = _get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, name, city, country, latitude, longitude
            FROM facilities
        """)
        existing = c.fetchall()
    finally:
        try: conn.close()
        except Exception: pass
    return [dict(r) for r in existing]


def rebuild_cache():
    global _cached_name_index, _cached_geo_index, _cache_built_at
    facilities = _load_facilities_from_db()
    name_idx = build_name_index(facilities)
    geo_idx = build_geo_index(facilities)
    with _cache_lock:
        _cached_name_index = name_idx
        _cached_geo_index = geo_idx
        _cache_built_at = time.time()
    count = len(facilities)
    print(f"   Facility index cached: {count} facilities, {len(name_idx)} names, {len(geo_idx)} geo cells")
    return count


def get_cached_indexes():
    global _cached_name_index, _cached_geo_index, _cache_built_at
    with _cache_lock:
        if _cached_name_index is None or _cache_built_at is None:
            pass
        elif (time.time() - _cache_built_at) < _CACHE_TTL:
            return dict(_cached_name_index), defaultdict(list, {k: list(v) for k, v in _cached_geo_index.items()})
    rebuild_cache()
    with _cache_lock:
        return dict(_cached_name_index), defaultdict(list, {k: list(v) for k, v in _cached_geo_index.items()})


def update_cache_incremental(name, country, facility_id, lat, lon):
    global _cached_name_index, _cached_geo_index
    with _cache_lock:
        if _cached_name_index is None:
            return
        norm = normalize_name(name)
        ctry = (country or '').lower().strip()
        if norm:
            _cached_name_index[(norm, ctry)] = facility_id
        if lat and lon:
            try:
                gk = (round(float(lat), 2), round(float(lon), 2))
                _cached_geo_index[gk].append(facility_id)
            except (ValueError, TypeError):
                pass


def check_geo_duplicate(lat, lon, geo_index):
    if not lat or not lon:
        return False, None
    try:
        lat_r, lon_r = round(float(lat), 2), round(float(lon), 2)
    except (ValueError, TypeError):
        return False, None
    for dlat in [-0.01, 0, 0.01]:
        for dlon in [-0.01, 0, 0.01]:
            key = (round(lat_r + dlat, 2), round(lon_r + dlon, 2))
            if key in geo_index:
                return True, geo_index[key][0]
    return False, None


def _generate_facility_id(name, city, country):
    raw = f"{name}|{city}|{country}".lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_cache_stats():
    with _cache_lock:
        if _cached_name_index is None:
            return {'cached': False, 'names': 0, 'geo_cells': 0, 'age_seconds': 0}
        age = int(time.time() - _cache_built_at) if _cache_built_at else 0
        return {
            'cached': True,
            'names': len(_cached_name_index),
            'geo_cells': len(_cached_geo_index),
            'age_seconds': age
        }


def run_auto_approval(max_records=None, test_mode=False):
    if max_records is None:
        max_records = MAX_PER_RUN
    max_records = min(max_records, MAX_PER_RUN)

    init_approval_table()

    # psycopg2: cursor.execute() returns None — chaining .fetchall() /
    # .fetchone() on it raised "NoneType has no attribute" in prod
    # (JOB auto-approve log). Also wrap the whole job body in try/finally
    # so a slow batch doesn't leak the conn for 74s+ (forced-reclaim).
    conn = _get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, source, source_id, name, provider, market, city, state, country,
                   address, latitude, longitude, power_mw, sqft, status, facility_type,
                   source_url, confidence_score
            FROM discovered_facilities
            WHERE merged_at IS NULL AND is_duplicate = 0
            ORDER BY
                CASE WHEN source IN ('peeringdb','openstreetmap') THEN 0 ELSE 1 END,
                id ASC
            LIMIT %s
        """, (max_records,))
        pending = c.fetchall()

        if not pending:
            return {
                'status': 'no_pending',
                'approved': 0,
                'duplicate_skipped': 0,
                'flagged_review': 0,
                'errors': 0,
                'total_processed': 0
            }

        name_index, geo_index = get_cached_indexes()

        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM facilities")
        count_before = c.fetchone()[0]

        stats = {
            'approved': 0,
            'duplicate_skipped': 0,
            'flagged_review': 0,
            'errors': 0,
            'total_processed': 0,
            'batches': 0,
            'count_before': count_before
        }

        pending_list = [dict(r) for r in pending]
        batches = [pending_list[i:i+BATCH_SIZE] for i in range(0, len(pending_list), BATCH_SIZE)]

        for batch_idx, batch in enumerate(batches):
            stats['batches'] += 1
            try:
                _process_batch(conn, batch, stats, name_index, geo_index)
            except Exception as e:
                print(f"   Batch {batch_idx+1} error: {e}")
                stats['errors'] += len(batch)

            if batch_idx < len(batches) - 1:
                time.sleep(BATCH_DELAY)

        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM facilities")
        count_after = c.fetchone()[0]
        stats['count_after'] = count_after
        stats['net_new'] = count_after - count_before
        stats['status'] = 'complete'
    finally:
        try: conn.close()
        except Exception: pass

    cache_info = get_cache_stats()
    stats['cache_age_seconds'] = cache_info['age_seconds']
    stats['cache_names'] = cache_info['names']

    print(f"\n   Auto-Approval Summary:")
    print(f"   Approved & inserted: {stats['approved']}")
    print(f"   Duplicate skipped: {stats['duplicate_skipped']}")
    print(f"   Flagged for review: {stats['flagged_review']}")
    print(f"   Errors: {stats['errors']}")
    print(f"   Batches: {stats['batches']}")
    print(f"   Facilities: {count_before} -> {count_after} (+{count_after - count_before})")
    print(f"   Cache: {cache_info['names']} names, age {cache_info['age_seconds']}s")

    return stats


def _process_batch(conn, batch, stats, name_index, geo_index):
    for disc in batch:
        stats['total_processed'] += 1
        try:
            disc_name_norm = normalize_name(disc['name'])
            disc_country = (disc.get('country') or '').lower().strip()
            name_key = (disc_name_norm, disc_country)

            if disc_name_norm and name_key in name_index:
                match_id = name_index[name_key]
                c = conn.cursor()
                c.execute("""
                    INSERT INTO discovery_approvals
                    (discovered_facility_id, source, action, match_reason, promoted_facility_id)
                    VALUES (%s, %s, 'duplicate_skipped', 'name_match', %s)
                """, (disc['id'], disc['source'], match_id))
                c = conn.cursor()
                c.execute("""
                    UPDATE discovered_facilities SET is_duplicate = 1 WHERE id = %s
                """, (disc['id'],))
                stats['duplicate_skipped'] += 1
                continue

            source = (disc['source'] or '').lower()

            geo_match, geo_match_id = check_geo_duplicate(
                disc.get('latitude'), disc.get('longitude'), geo_index
            )
            if geo_match and source not in TRUSTED_SOURCES:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO discovery_approvals
                    (discovered_facility_id, source, action, match_reason, promoted_facility_id)
                    VALUES (%s, %s, 'flagged_review', 'geo_match', %s)
                """, (disc['id'], disc['source'], geo_match_id))
                stats['flagged_review'] += 1
                continue

            if source not in TRUSTED_SOURCES:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO discovery_approvals
                    (discovered_facility_id, source, action, match_reason)
                    VALUES (%s, %s, 'flagged_review', 'untrusted_source')
                """, (disc['id'], disc['source']))
                stats['flagged_review'] += 1
                continue

            facility_id = _generate_facility_id(
                disc['name'],
                disc.get('city') or '',
                disc.get('country') or 'US'
            )

            try:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO facilities
                    (id, name, provider, city, state, country, latitude, longitude,
                     power_mw, sqft, status, source, source_id, source_url,
                     confidence, first_seen, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    facility_id,
                    disc['name'],
                    disc.get('provider'),
                    disc.get('city'),
                    disc.get('state'),
                    disc.get('country') or 'US',
                    disc.get('latitude'),
                    disc.get('longitude'),
                    disc.get('power_mw'),
                    disc.get('sqft'),
                    disc.get('status') or 'active',
                    disc['source'],
                    disc.get('source_id'),
                    disc.get('source_url'),
                    disc.get('confidence_score') or 0.5,
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat()
                ))

                c = conn.cursor()
                c.execute("""
                    INSERT INTO discovery_approvals
                    (discovered_facility_id, source, action, match_reason, promoted_facility_id)
                    VALUES (%s, %s, 'approved', NULL, %s)
                """, (disc['id'], disc['source'], facility_id))

                c = conn.cursor()
                c.execute("""
                    UPDATE discovered_facilities
                    SET merged_at = %s, merged_facility_id = %s
                    WHERE id = %s
                """, (datetime.utcnow().isoformat(), facility_id, disc['id']))

                update_cache_incremental(
                    disc['name'], disc.get('country'),
                    facility_id,
                    disc.get('latitude'), disc.get('longitude')
                )

                new_name_key = (disc_name_norm, disc_country)
                if disc_name_norm:
                    name_index[new_name_key] = facility_id
                if disc.get('latitude') and disc.get('longitude'):
                    try:
                        gk = (round(float(disc['latitude']), 2), round(float(disc['longitude']), 2))
                        geo_index[gk].append(facility_id)
                    except (ValueError, TypeError):
                        pass

                stats['approved'] += 1

            except Exception:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO discovery_approvals
                    (discovered_facility_id, source, action, match_reason)
                    VALUES (%s, %s, 'duplicate_skipped', 'unique_constraint')
                """, (disc['id'], disc['source']))
                c = conn.cursor()
                c.execute("""
                    UPDATE discovered_facilities SET is_duplicate = 1 WHERE id = %s
                """, (disc['id'],))
                stats['duplicate_skipped'] += 1

        except Exception as e:
            print(f"   Record {disc['id']} error: {e}")
            stats['errors'] += 1

    conn.commit()


if __name__ == '__main__':
    print("Running auto-approval test batch (10 records)...")
    result = run_auto_approval(max_records=10, test_mode=True)
    print(f"\nResult: {result}")
