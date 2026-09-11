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
USER_AGENT = 'DCHub-Intelligence/1.0 (+https://dchub.cloud; data-center-research; contact: hello@dchub.cloud)'

# Politeness (added 2026-07-29, before this ingest was put back on a weekly
# schedule). It fetches two bulk JSON documents rather than crawling pages, but
# it had no robots.txt check and no inter-request spacing at all.
#
# Measured 2026-07-29 with the UA below:
#   https://www.submarinecablemap.com/robots.txt  → HTTP 200, 24 bytes:
#       "User-agent: *" / "Disallow:"  — an empty Disallow allows everything.
#   https://raw.githubusercontent.com/robots.txt  → HTTP 404, i.e. no
#       robots.txt, so nothing is disallowed on the backup mirrors either.
# Both sources are therefore permitted today. The check is enforced at fetch
# time rather than trusted from this note: if either policy tightens, the fetch
# is abandoned with a logged reason instead of proceeding.
_MIN_REQUEST_SPACING_S = 2.0
_last_request_at = {}   # host -> monotonic timestamp of our last GET
_robots_cache = {}      # host -> urllib.robotparser.RobotFileParser | None


def _robots_allows(url):
    """True if robots.txt permits USER_AGENT to fetch `url`.

    One robots.txt fetch per host, cached for the life of the process. If
    robots.txt cannot be retrieved we proceed — an unreachable or absent
    robots.txt is the standard "no restrictions stated" case. An explicit
    Disallow is honoured and the fetch is abandoned.
    """
    import time as _time
    from urllib.parse import urlsplit
    from urllib.robotparser import RobotFileParser

    parts = urlsplit(url)
    host = parts.netloc
    if host not in _robots_cache:
        rp = RobotFileParser()
        robots_url = f"{parts.scheme}://{host}/robots.txt"
        try:
            resp = requests.get(robots_url, headers={'User-Agent': USER_AGENT},
                                timeout=15)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                _robots_cache[host] = rp
            else:
                # 404/410 = no robots.txt = nothing disallowed.
                logger.info(f"robots.txt for {host}: HTTP {resp.status_code} — "
                            f"treating as no restrictions")
                _robots_cache[host] = None
        except Exception as e:
            logger.warning(f"robots.txt fetch failed for {host} ({e}) — "
                           f"treating as no restrictions")
            _robots_cache[host] = None
        _last_request_at[host] = _time.monotonic()

    rp = _robots_cache.get(host)
    if rp is None:
        return True
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    allowed = rp.can_fetch(USER_AGENT, path)
    if not allowed:
        logger.error(f"robots.txt DISALLOWS {url} for our UA — not fetching")
    return allowed


def _throttle(url):
    """Sleep so we never issue two requests to one host inside the spacing."""
    import time as _time
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc
    last = _last_request_at.get(host)
    if last is not None:
        wait = _MIN_REQUEST_SPACING_S - (_time.monotonic() - last)
        if wait > 0:
            _time.sleep(wait)
    _last_request_at[host] = _time.monotonic()


def _fetch_json(url, backup_url=None, timeout=60):
    """Fetch JSON from URL with fallback.

    Respects robots.txt and enforces >= _MIN_REQUEST_SPACING_S between requests
    to the same host.
    """
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'application/json',
    }

    def _get(u):
        if not _robots_allows(u):
            raise PermissionError('robots.txt disallows ' + u)
        _throttle(u)
        resp = requests.get(u, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    try:
        return _get(url)
    except Exception as e:
        logger.warning(f"Primary fetch failed ({url}): {e}")
        if backup_url:
            try:
                data = _get(backup_url)
                logger.info(f"✅ Backup fetch succeeded: {backup_url}")
                return data
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
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
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
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
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
def _field_coverage(cur, table, columns):
    """Which of `columns` are served on every row without ever being populated.

    ★ WHY A RESPONSE BLOCK AND NOT A ROW-SHAPE CHANGE. The obvious fix is to
    emit null instead of '' / 0 per row, but the map frontend and the MCP
    fiber tools read these keys positionally and an unannounced type change
    would break them silently — trading a disclosed gap for an outage. This
    states the gap alongside the rows and leaves the shape alone; converting
    the rows themselves is a follow-up with its own consumer sweep.

    Never raises: a disclosure block that can 500 the route it documents is
    worse than no disclosure.
    """
    try:
        from util.db_honesty import column_population, POPULATED, UNKNOWN
    except Exception:
        return None

    unpopulated, confirmed = [], []
    for column, kind in columns:
        try:
            verdict, detail = column_population(cur, table, column, kind)
        except Exception:
            continue
        if verdict == POPULATED:
            # ★ Recorded POSITIVELY, not inferred from "absent in
            # unpopulated[]". UNKNOWN (the probe itself failed) also leaves
            # the column out of unpopulated[], and a caller that reads
            # absence as proof-of-data would resume grading on the strength
            # of a failed probe — fail-open, which is the bug this block
            # exists to close.
            confirmed.append(column)
            continue
        if verdict == UNKNOWN:
            continue
        unpopulated.append({
            'column': column,
            'verdict': verdict,
            'rows': detail.get('rows'),
            'non_null': detail.get('non_null'),
            'distinct_values': detail.get('distinct_values'),
        })

    if not unpopulated:
        return {'unpopulated': [], 'confirmed_populated': confirmed, 'note': (
            'Every column confirmed here carries data; values are as '
            'measured. Columns in neither list could not be checked.')}

    names = ', '.join(u['column'] for u in unpopulated)
    return {
        'unpopulated': unpopulated,
        'confirmed_populated': confirmed,
        'note': (
            f'NOT MEASURED — {names}: the ingest writes these columns but the '
            f'upstream TeleGeography feed does not supply the properties they '
            f'derive from, so every row carries the default. Treat the served '
            f'values as unknown, not as findings, and note that filters over '
            f'these columns return nothing for the same reason.'),
    }


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
                'source': 'TeleGeography via DC Hub Intelligence',
                # ★ 2026-08-08 — four of the six columns above have never been
                # populated. `cable-geo.json` is a GEOMETRY feed: its feature
                # properties are only {color, coordinates, feature_id, id,
                # name}. The ingest reads owners/rfs/length/is_planned off it,
                # gets nothing every time, and writes the defaults. So
                # `?planned=true` AND `?planned=false` both return 0 rows out
                # of 699 — is_planned is NULL, and NULL matches neither.
                # Disclosed here rather than silently served as null metadata.
                'field_coverage': _field_coverage(c, 'subsea_cables', (
                    ('is_planned', 'flag'),
                    ('owners', 'text'),
                    ('length_km', 'flag'),
                    ('rfs_year', 'flag'),
                )),
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
                'source': 'TeleGeography via DC Hub Intelligence',
                # ★ 2026-08-08 — three of the eight columns above are served
                # on every row without ever having been populated: `country`
                # is '' on all 1,927 rows, `cable_count` is 0 on all of them
                # (this route ORDERs BY cable_count DESC, so the top row
                # reading 0 proves the whole table does), and `is_major_hub`
                # is therefore FALSE everywhere. An empty string and a zero
                # are not neutral placeholders — they read as measurements,
                # and `?country=` / `?major_only=true` silently return
                # nothing rather than saying the filter has nothing to bite
                # on. Measured live rather than hard-coded, so it corrects
                # itself the moment the ingest starts populating them.
                'field_coverage': _field_coverage(c, 'subsea_landing_points', (
                    ('country', 'text'),
                    ('cable_count', 'flag'),
                    ('is_major_hub', 'flag'),
                )),
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
                    'distance_km': round(row[8] or 0, 1) if row[8] else None,
                })

            # ★ 2026-08-08 — THIS GRADE WAS NOT A MEASUREMENT.
            # It read "Excellent" when any nearby landing point had
            # cable_count >= 10, else "Good" if any point at all, else
            # "Limited". `cable_count` is 0 on all 1,927 rows (never
            # populated — see _field_coverage), so the >= 10 branch could
            # never fire and every site with a landing point in radius was
            # graded "Good" on the strength of nothing. This feeds site
            # selection, where "Good subsea connectivity" is exactly the kind
            # of sentence that gets pasted into a memo.
            #
            # Grade only what we can count: how many landing points are in
            # range. That IS measured — the coordinates are populated.
            coverage = _field_coverage(c, 'subsea_landing_points',
                                       (('cable_count', 'flag'),))
            # Grade only on POSITIVE confirmation that cable_count carries
            # data — never on the absence of a complaint.
            cable_counts_live = bool(
                coverage and 'cable_count' in coverage['confirmed_populated'])

            if cable_counts_live:
                grade = ('Excellent'
                         if any((p['cable_count'] or 0) >= 10 for p in points)
                         else 'Good' if points else 'Limited')
                note = (f"{grade} subsea connectivity within {radius_km}km "
                        f"({len(points)} landing points, "
                        f"{sum(p['cable_count'] or 0 for p in points)} cables)")
            else:
                grade = None
                note = (f"{len(points)} subsea landing point(s) within "
                        f"{radius_km}km. NOT GRADED: cable counts per landing "
                        f"point were never populated, so the number of cables "
                        f"reachable from here is unknown — proximity alone "
                        f"does not establish connectivity quality.")

            return jsonify({
                'success': True,
                'query': {'lat': lat, 'lng': lng, 'radius_km': radius_km},
                'landing_points': points,
                'total_nearby': len(points),
                # null, not a cheerful default: a consumer can branch on null,
                # it cannot detect a grade asserted from an empty column.
                'connectivity_grade': grade,
                'connectivity_note': note,
                'field_coverage': coverage,
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

# === phase 92: source-registry heartbeat (auto-fires on clean module exit) ===
# Non-invasive: never crashes the script if the registry is unreachable.
# Source ID: backend-subsea-cable
_phase92_heartbeat_registered = True
try:
    import atexit as _phase92_atexit
    from dchub_heartbeat import heartbeat as _phase92_heartbeat
    def _phase92_emit():
        try:
            _phase92_heartbeat("backend-subsea-cable", status="success",
                              metadata={"trigger": "atexit"})
        except Exception:
            pass
    _phase92_atexit.register(_phase92_emit)
except Exception:
    pass  # heartbeat module unavailable; extractor continues normally
