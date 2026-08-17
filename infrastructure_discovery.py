"""
Infrastructure Discovery Module
Tracks fiber routes, commercial real estate, construction permits, and substations
All reads/writes go to PostgreSQL via db_utils.
"""

import requests
import json
import logging
from datetime import datetime, timedelta
from threading import Thread
import time
import os
import hashlib
from db_utils import get_db
from util.hifld_layers import layer_url

# phase57_landing — daily landing URL helper for LinkedIn rich-card preview
def _phase30c_landing_url(d=None):
    """Return canonical /api/v1/social/posts/<date> URL for LinkedIn OG card."""
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"


logger = logging.getLogger(__name__)

DB_PATH = 'dc_nexus.db'


def _safe_write(sql, params=None, retries=3):
    """Write to PostgreSQL via db_utils. Rolls back on error to keep pool clean.

    Phase FF+10-pipeline (2026-05-19): added fast-fail for the
    "no unique or exclusion constraint" error class. Without this
    short-circuit, every `INSERT ... ON CONFLICT(<col>) DO NOTHING`
    statement that hits a table missing the matching constraint
    retried 3× with 1s + 2s sleeps — turning a single bad statement
    into 3-4s of wasted DB connection time. The autonomous-brain
    inline loop calls into here every 5 min × N pipelines, which
    exhausted the Neon pool and pegged gunicorn workers (watchdog
    eventually killed the container on self_response failures).
    """
    for attempt in range(retries):
        conn = None
        try:
            conn = get_db()
            cursor = conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            conn.commit()
            return cursor.rowcount
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            # Phase FF+10-pipeline fast-fail: schema bugs (missing
            # constraint, undefined column) can NEVER be fixed by a
            # retry — bail immediately so the caller can degrade
            # gracefully instead of holding a connection 3-4 seconds.
            _msg = str(e).lower()
            # r33-Q+substations (2026-05-21): the "no unique or exclusion
            # constraint matching the ON CONFLICT specification" error
            # was firing for substations even though psql-shell tests
            # showed the constraint IS there. Root cause never fully
            # nailed (suspected: pool connection's catalog cache differs
            # from a fresh psql connection's view of the constraint).
            # ONE-TIME RECOVERY: when this error fires, retry the same
            # SQL with the explicit conflict target stripped down to
            # bare `ON CONFLICT DO NOTHING`. That form lets PG choose
            # ANY unique constraint as the arbiter — works as long as
            # at least one UNIQUE constraint exists on the table.
            if "no unique or exclusion constraint" in _msg:
                import re as _re
                # Strip ON CONFLICT(col) → ON CONFLICT
                _fallback_sql = _re.sub(
                    r"ON\s+CONFLICT\s*\([^)]+\)",
                    "ON CONFLICT",
                    sql,
                    flags=_re.IGNORECASE,
                )
                if _fallback_sql != sql:
                    try:
                        conn2 = get_db()
                        cur2 = conn2.cursor()
                        if params:
                            cur2.execute(_fallback_sql, params)
                        else:
                            cur2.execute(_fallback_sql)
                        conn2.commit()
                        rc = cur2.rowcount
                        try: conn2.close()
                        except Exception: pass
                        # Log once at INFO level so we know fallback fired
                        logger.info(
                            "Infrastructure write recovered via bare "
                            "ON CONFLICT DO NOTHING fallback (rowcount=%s).",
                            rc,
                        )
                        return rc
                    except Exception as e2:
                        logger.warning(
                            "Fallback ON CONFLICT DO NOTHING also failed: %s",
                            str(e2)[:200],
                        )
                logger.warning(f"Infrastructure write SCHEMA-fail (no retry): {e}")
                return 0
            if ("does not exist" in _msg
                    or "undefined column" in _msg
                    or "syntax error" in _msg):
                logger.warning(f"Infrastructure write SCHEMA-fail (no retry): {e}")
                return 0
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
            else:
                logger.warning(f"Infrastructure write failed after {retries} attempts: {e}")
                return 0
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    return 0


_UID_SENTINELS = frozenset(
    ('', '0', '-1', 'NONE', 'NULL', 'UNKNOWN', 'NOT AVAILABLE', 'N/A', 'NA'))


def _route_uid(route):
    """The SOURCE's own identifier for this physical asset, or None.

    SH52-054. This is the whole identity contract in one place, so read the
    three rules before adding a caller:

      1. IT COMES FROM UPSTREAM, NOT FROM US. A value we computed from where we
         were standing when we found the asset (a market centroid, a search
         radius, a loop index) is not identity — it re-identifies the crawl,
         not the line. That is the exact defect this replaces.
      2. IT IS STABLE ACROSS RUNS AND ACROSS PROCESSES. Never `hash()` — CPython
         salts str hashing per process unless PYTHONHASHSEED is pinned, and it
         is not pinned anywhere in this repo. A per-process id makes every
         restart mint new assets.
      3. IT IS NEVER AN ArcGIS OBJECTID. OBJECTID is the row number of one
         particular export; re-export in a different order and it names a
         different asset. That mistake is not hypothetical here — it is what
         `substations.hifld_objectid` holds for 78,356 of 79,686 rows, and it
         is why SH52-056's bulk refresh is blocked.

    Returning None is a legitimate answer meaning UNIDENTIFIED. It is far
    better than a fabricated id: NULL keeps the row out of the partial unique
    index and leaves the older name/source_id arbitration in charge, whereas a
    fabricated id asserts distinctness that was never established.
    """
    uid = route.get('uid')
    if uid is None:
        return None
    uid = str(uid).strip()
    if uid.upper() in _UID_SENTINELS:
        return None
    return uid[:120]


def init_infrastructure_tables():
    """Initialize tables for infrastructure data — PostgreSQL compatible"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fiber_routes (
            id SERIAL PRIMARY KEY,
            name TEXT,
            provider TEXT,
            route_type TEXT,
            start_location TEXT,
            end_location TEXT,
            start_lat REAL,
            start_lng REAL,
            end_lat REAL,
            end_lng REAL,
            distance_miles REAL,
            fiber_count INTEGER,
            lit_capacity_gbps REAL,
            status TEXT DEFAULT 'active',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    ''')

    # SH52-054 identity column. Kept HERE as well as in
    # migrations/2026-08-12_fiber_route_upstream_uid.sql because _save_route
    # names this column in its INSERT: if the migration has not been applied on
    # some environment, every fiber write would fail with UndefinedColumn and
    # the lane would go silently to zero. Both statements are idempotent.
    #
    # The index is PARTIAL (WHERE upstream_uid IS NOT NULL) for two reasons that
    # are not stylistic:
    #   · 55k+ bulk-carrier rows have no upstream uid and never will — a full
    #     index would make them all collide on NULL... it would not (NULLs are
    #     distinct in a btree), but it would carry 55k dead entries for nothing.
    #   · 84 discovery rows are ALREADY duplicate twins of one upstream line
    #     (measured 2026-08-12: 1,826 rows over 1,742 distinct HIFLD ids). Those
    #     rows are published and FROZEN. The migration deliberately leaves the
    #     twin's upstream_uid NULL so the unique index can be built at all
    #     without deleting a live row.
    for _ddl in (
        "ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS upstream_uid TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS fiber_routes_upstream_uid_uniq "
        "ON fiber_routes (source, upstream_uid) WHERE upstream_uid IS NOT NULL",
    ):
        try:
            cursor.execute(_ddl)
            conn.commit()
        except Exception as _e:
            # Never let an identity-index failure take down table init — but do
            # not swallow it silently either. A zero-row lane with no log line
            # is how SH52-054 stayed invisible for five months.
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(f"fiber_routes identity DDL skipped: {_e}")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dc_properties (
            id SERIAL PRIMARY KEY,
            name TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            country TEXT DEFAULT 'US',
            lat REAL,
            lng REAL,
            property_type TEXT,
            square_feet INTEGER,
            power_capacity_mw REAL,
            asking_price REAL,
            price_per_sqft REAL,
            cap_rate REAL,
            zoning TEXT,
            utility_provider TEXT,
            fiber_providers TEXT,
            listing_url TEXT,
            broker TEXT,
            status TEXT DEFAULT 'available',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS construction_permits (
            id SERIAL PRIMARY KEY,
            permit_number TEXT,
            project_name TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            country TEXT DEFAULT 'US',
            lat REAL,
            lng REAL,
            permit_type TEXT,
            project_type TEXT,
            square_feet INTEGER,
            estimated_power_mw REAL,
            estimated_cost REAL,
            contractor TEXT,
            owner TEXT,
            issue_date DATE,
            expiration_date DATE,
            status TEXT DEFAULT 'active',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS substations (
            id SERIAL PRIMARY KEY,
            name TEXT,
            operator TEXT,
            substation_type TEXT,
            voltage_kv REAL,
            capacity_mva REAL,
            lat REAL,
            lng REAL,
            city TEXT,
            state TEXT,
            country TEXT DEFAULT 'US',
            connected_transmission TEXT,
            status TEXT DEFAULT 'active',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS gas_pipelines (
            id SERIAL PRIMARY KEY,
            name TEXT,
            operator TEXT,
            pipeline_type TEXT,
            diameter_inches REAL,
            capacity_mcf REAL,
            status TEXT DEFAULT 'active',
            lat REAL,
            lng REAL,
            city TEXT,
            state TEXT,
            country TEXT DEFAULT 'US',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    ''')

    # Phase FF+10-pipeline (2026-05-19) — backfill the UNIQUE constraint
    # on gas_pipelines.source_id for tables created before the DDL above
    # included `UNIQUE`. Without this, every `INSERT ... ON CONFLICT
    # (source_id)` returned "no unique or exclusion constraint matching"
    # and the autonomous-brain loop spammed retries until the Neon pool
    # collapsed (root cause of the 15:38 watchdog kill). Partial index
    # (WHERE source_id IS NOT NULL) so NULL rows don't block creation,
    # and wrapped in try/except so existing duplicates don't crash init.
    try:
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS gas_pipelines_source_id_uniq
              ON gas_pipelines (source_id)
            WHERE source_id IS NOT NULL
        """)
        logger.info("[infra] gas_pipelines source_id UNIQUE index ensured")
    except Exception as _idx_err:
        logger.warning(
            f"[infra] gas_pipelines UNIQUE index NOT created "
            f"(likely dupe source_ids exist): {str(_idx_err)[:200]}. "
            f"Falling back to fast-fail-on-conflict in _safe_write."
        )
        try: conn.rollback()
        except Exception: pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS linkedin_weekly_posts (
            id SERIAL PRIMARY KEY,
            week_start DATE,
            week_end DATE,
            content TEXT,
            stats_snapshot TEXT,
            posted_at TIMESTAMPTZ,
            post_id TEXT,
            engagement TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    ''')

    conn.commit()
    conn.close()
    logger.info("✅ Infrastructure tables initialized")


DC_MARKETS = [
    {"name": "Northern Virginia", "lat": 39.0438, "lng": -77.4874, "state": "VA"},
    {"name": "Dallas-Fort Worth", "lat": 32.7767, "lng": -96.7970, "state": "TX"},
    {"name": "Phoenix", "lat": 33.4484, "lng": -112.0740, "state": "AZ"},
    {"name": "Chicago", "lat": 41.8781, "lng": -87.6298, "state": "IL"},
    {"name": "Atlanta", "lat": 33.7490, "lng": -84.3880, "state": "GA"},
    {"name": "Silicon Valley", "lat": 37.3861, "lng": -122.0839, "state": "CA"},
    {"name": "Los Angeles", "lat": 34.0522, "lng": -118.2437, "state": "CA"},
    {"name": "New York Metro", "lat": 40.7128, "lng": -74.0060, "state": "NJ"},
    {"name": "Portland", "lat": 45.5152, "lng": -122.6784, "state": "OR"},
    {"name": "Seattle", "lat": 47.6062, "lng": -122.3321, "state": "WA"},
    {"name": "Salt Lake City", "lat": 40.7608, "lng": -111.8910, "state": "UT"},
    {"name": "Columbus", "lat": 39.9612, "lng": -82.9988, "state": "OH"},
    {"name": "Richmond", "lat": 37.5407, "lng": -77.4360, "state": "VA"},
    {"name": "San Antonio", "lat": 29.4241, "lng": -98.4936, "state": "TX"},
    {"name": "Reno", "lat": 39.5296, "lng": -119.8138, "state": "NV"},
    {"name": "Des Moines", "lat": 41.5868, "lng": -93.6250, "state": "IA"},
    {"name": "Kansas City", "lat": 39.0997, "lng": -94.5786, "state": "MO"},
    {"name": "Minneapolis", "lat": 44.9778, "lng": -93.2650, "state": "MN"},
    {"name": "Denver", "lat": 39.7392, "lng": -104.9903, "state": "CO"},
    {"name": "Houston", "lat": 29.7604, "lng": -95.3698, "state": "TX"},
]

HIFLD_APIS = {
    # Electric_Substations: DEAD as of 2024 — HIFLD moved to hash-based service names
    # "substations": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0",
    #
    # ★★★ SH52-057 (2026-08-12) — transmission_lines USED TO BE HARDCODED HERE
    # to services1/Hp6G80Pky0om7QvQ, and that was the WRONG LAYER. Measured
    # live the same day: that service carries 52,244 features; the maintained
    # one carries 89,744. land_power_crawler.py had already established this
    # and defends it with a 70,000-row floor written specifically to reject the
    # 52,244 layer — so the two modules in this repo disagreed about which
    # population is real, and this lane was reading the one the other module
    # deliberately throws away.
    #
    # It now resolves from util/hifld_layers.py, the single definition both
    # modules read, so the disagreement cannot come back by editing one file.
    #
    # Safe to repoint because the two layers share ONE id space: 1,706 of the
    # 1,742 upstream_uids already held by fiber_routes resolve on the new layer
    # (97.9%), so the SH52-054 identity keeps matching and ON CONFLICT DO
    # NOTHING keeps discarding lines we already hold. This is a coverage
    # change, not a re-insert. See util/hifld_layers.py for the 36 that do not
    # resolve and why they are left alone.
    "transmission_lines": layer_url('hifld-transmission'),
    # Power_Plants: DEAD as of 2024 — HIFLD moved to hash-based service names; use EIA + market coords instead
    # "power_plants": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0",
}

EIA_PIPELINE_APIS = {
    "natural_gas": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0",
    "crude_oil": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Crude_Oil_Trunk_Pipelines_1/FeatureServer/0",
    "hgl": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Hydrocarbon_Gas_Liquids_Pipelines_1/FeatureServer/0",
    "petroleum": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Petroleum_Products_Pipelines_1/FeatureServer/0",
    "gulf_pipelines": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Oil_And_Natural_Gas_Pipelines_Gulf_2024Q4/FeatureServer/0",
}


def _query_hifld_nearby(api_url, lat, lng, radius_m=80000, max_records=500, return_geometry=True):
    """Query HIFLD ArcGIS API for features near a point"""
    try:
        params = {
            'where': '1=1',
            'geometry': f'{lng},{lat}',
            'geometryType': 'esriGeometryPoint',
            'inSR': '4326',
            'spatialRel': 'esriSpatialRelIntersects',
            'distance': radius_m,
            'units': 'esriSRUnit_Meter',
            'outFields': '*',
            'returnGeometry': 'true' if return_geometry else 'false',
            'outSR': '4326',
            'resultRecordCount': max_records,
            'f': 'json'
        }
        response = requests.get(f"{api_url}/query", params=params, timeout=20)
        if response.ok:
            data = response.json()
            features = data.get('features', [])
            # ★ SH52-057 — the server has ALWAYS told us when it truncated and
            # this function has always thrown that away. ArcGIS sets
            # exceededTransferLimit=true when more features matched than it
            # returned, so a lane pinned at its own resultRecordCount looked
            # exactly like a lane that had collected everything there was.
            # Measured 2026-08-12 at the old max_records=100: 19 of the 20
            # DC_MARKETS came back with this flag set, and nothing logged it.
            # A ceiling you cannot see is a ceiling nobody raises.
            if data.get('exceededTransferLimit'):
                logger.warning(
                    f"   ⚠️ HIFLD truncated at {len(features)} features "
                    f"(resultRecordCount={max_records}) — more matched than "
                    f"were returned; raise max_records or tighten the radius")
            return features
    except Exception as e:
        logger.warning(f"HIFLD query failed for {api_url}: {e}")
    return []


def _query_hifld_paginated(api_url, where='1=1', max_total=2000, batch_size=1000):
    """Query HIFLD ArcGIS API with pagination for bulk pulls"""
    all_features = []
    offset = 0
    try:
        while offset < max_total:
            params = {
                'where': where,
                'outFields': '*',
                'returnGeometry': 'true',
                'outSR': '4326',
                'resultRecordCount': batch_size,
                'resultOffset': offset,
                'f': 'json'
            }
            response = requests.get(f"{api_url}/query", params=params, timeout=45)
            if not response.ok:
                break
            data = response.json()
            features = data.get('features', [])
            if not features:
                break
            all_features.extend(features)
            offset += len(features)
            if len(features) < batch_size:
                break
            time.sleep(0.5)
    except Exception as e:
        logger.warning(f"HIFLD paginated query failed: {e}")
    return all_features


# ── HIFLD sentinel scrubbing (2026-08-15) ─────────────────────────────────────
# HIFLD does not use NULL for "we don't know". It ships MAGIC NUMBERS and MAGIC
# STRINGS, and both travelled straight into public display names:
#
#     "NOT AVAILABLE -999999kV Line - Columbus [6faf59c85f0e]"
#
# 718 rows in fiber_routes carry that. `attrs.get('VOLTAGE', 0) or 0` LOOKS like
# a null-guard and is not one: -999999 is truthy, so `or 0` never fires. It only
# catches a voltage that is already 0 — a guard written for the one sentinel
# HIFLD does not use. Same shape on the owner side: the `or 'Unknown'` fallback
# only catches empty string, never the literal 'NOT AVAILABLE' that is actually
# in the data.
#
# ★Display-name-only TODAY — _save_route persists no voltage column, so the
# blast radius is cosmetic (the note SH52-054 above is about the same synthesized
# name, for a different reason). Do not read that as "safe to leave": the same
# attrs feed any numeric column added later, and a -999999 kV sorts to the top of
# every "lowest voltage" query anyone ever writes. Scrub at the source.
_HIFLD_NULL_STRINGS = frozenset({'', 'not available', 'n/a', 'na', 'none',
                                 'null', 'unknown', 'not applicable',
                                 'no data', '-999999'})


def hifld_voltage(raw):
    """A usable kV, or None. HIFLD's sentinels are -999999/-999998 and 0; real
    transmission voltage is never <= 0, so the whole family collapses to None."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def hifld_owner(owner, operator=None):
    """The first field naming an actual party, else 'Unknown'. Compared
    case-insensitively — the live data carries 'NOT AVAILABLE' in caps."""
    for cand in (owner, operator):
        t = str(cand or '').strip()
        if t and t.lower() not in _HIFLD_NULL_STRINGS:
            return t
    return 'Unknown'


def hifld_line_name(owner, voltage, market):
    """Display name that OMITS what is unknown instead of printing a sentinel.
    'Unknown 345kV Line - Columbus' is honest about the owner; 'NOT AVAILABLE
    -999999kV Line - Columbus' is two database artifacts wearing a name."""
    kv = f" {voltage:g}kV" if voltage is not None else ""
    return f"{owner}{kv} Line - {market}"


class FiberRouteDiscovery:
    """Discover fiber routes from HIFLD transmission lines, PeeringDB, and OSM"""

    PEERINGDB_API = "https://www.peeringdb.com/api"
    OVERPASS_API = "https://overpass-api.de/api/interpreter"

    # 2 markets are queried per run and the window advances by 2, so a full
    # sweep of DC_MARKETS takes len(DC_MARKETS)/2 runs.
    MARKETS_PER_RUN = 2

    def __init__(self, market_index=None):
        self.new_routes = 0
        # ★ THIS USED TO BE A HARD `= 0`, WHICH KILLED THE ROTATION ENTIRELY.
        # `_market_index` is per-INSTANCE, and every caller constructs a fresh
        # FiberRouteDiscovery() per run (main.py job_fiber_sync does), so the
        # window reset to 0 on every single invocation: the loader re-queried
        # DC_MARKETS[0:2] — Northern Virginia and Dallas-Fort Worth — forever,
        # and the other 18 of 20 markets were NEVER reached. Those two markets
        # were fully ingested long ago, so every row hit ON CONFLICT DO NOTHING
        # and the job reported "0 new" while looking perfectly healthy.
        # Measured 2026-08-07: fiber_routes had 0 new rows in 7d despite
        # fiber_sync running 4x/day.
        #
        # The window is now derived from a MONOTONIC slot instead of instance
        # state, so it advances across runs, across processes and across the
        # multiple Railway workers that each hold their own memory.
        self._market_index = (self._default_market_index()
                              if market_index is None else market_index)

    @classmethod
    def _default_market_index(cls):
        """Rotation slot from a monotonic ordinal — never a per-instance counter.

        Uses date.toordinal() (which keeps increasing across year boundaries)
        rather than day-of-year: `doy % N` silently repeats one slot and skips
        another every Jan 1, because 365 % N is rarely 0. Four runs/day are
        given distinct slots via hour//6 so a full sweep takes ~2.5 days.
        """
        now = datetime.utcnow()
        slots = max(1, len(DC_MARKETS) // cls.MARKETS_PER_RUN)
        slot = (now.date().toordinal() * 4 + now.hour // 6) % slots
        return slot * cls.MARKETS_PER_RUN

    def sync(self):
        logger.info("🔌 Syncing fiber routes...")
        self.new_routes = 0
        self._sync_hifld_transmission_lines()
        self._sync_peeringdb_exchanges()
        self._sync_osm_fiber_cables()
        self._sync_from_learned_apis()
        logger.info(f"   ✅ Fiber routes: {self.new_routes} new")
        return self.new_routes

    # ★★★ SH52-057 (2026-08-12) — THE LANE'S HARD CEILING, AND WHY IT IS THIS
    # NUMBER. This sweep is bounded by DC_MARKETS × HIFLD_MAX_RECORDS, and
    # nothing else: 20 markets × 100 records meant the lane could never hold
    # more than 2,000 distinct transmission lines, EVER. Measured on the live
    # Neon table 2026-08-12 it held 1,826 rows over 1,742 distinct upstream
    # ids — 87% of its own ceiling, so it was within weeks of flat-lining for
    # a reason no dashboard would have named.
    #
    # What 100 was actually costing, measured per market against the
    # (correct) 89,744-feature layer at the same 50 km radius:
    #
    #     19 of 20 markets returned exceededTransferLimit=true at 100
    #     lines reachable at 50 km, summed over all 20 markets:  9,573
    #     largest single market (Dallas-Fort Worth):               792
    #
    # 1000 clears the largest market with ~25% headroom, so every market is
    # collected COMPLETE in one request and the truncation flag goes quiet —
    # which is the point: the new ceiling is the population itself, not an
    # arbitrary constant, and the warning above now means something when it
    # fires. It is deliberately NOT the server's own maxRecordCount (2,000):
    # this is sized to the measured need, leaving the server's limit as
    # headroom rather than as the target.
    #
    # ★ THIS IS NOT A BACKFILL AND MUST NOT BECOME ONE. The lane still sweeps
    # MARKETS_PER_RUN=2 markets per run on the existing rotation, so the work
    # per run rises from ~200 upsert attempts to at most 2,000 — bounded,
    # idempotent (ON CONFLICT DO NOTHING against the upstream_uid index), and
    # spread over the full rotation rather than landing at once. Do not "just
    # loop over DC_MARKETS" to catch up faster: a runaway INSERT loop in this
    # module is why infra_sync was disabled in the scheduler (v3.7), and it
    # only came back pool-gated at 1×/day as infra_sync_safe.
    HIFLD_MAX_RECORDS = 1000

    def _sync_hifld_transmission_lines(self):
        markets = DC_MARKETS[self._market_index:self._market_index + self.MARKETS_PER_RUN]
        self._market_index = (self._market_index + self.MARKETS_PER_RUN) % len(DC_MARKETS)

        for market in markets:
            try:
                features = _query_hifld_nearby(
                    HIFLD_APIS['transmission_lines'],
                    market['lat'], market['lng'],
                    radius_m=50000, max_records=self.HIFLD_MAX_RECORDS,
                    return_geometry=False
                )
                for feat in features:
                    attrs = feat.get('attributes', {})
                    voltage = hifld_voltage(attrs.get('VOLTAGE'))
                    owner = hifld_owner(attrs.get('OWNER'), attrs.get('OPERATOR'))
                    # ★ SH52-054 — `or attrs.get('OBJECTID')` USED TO BE HERE
                    # AND IS DELIBERATELY GONE. OBJECTID is the row number of
                    # one ArcGIS export, not an asset id; keying on it is the
                    # fault that destroyed substation identity (SH52-056).
                    # Re-verified 2026-08-12 against the CANONICAL layer this
                    # lane now reads (SH52-057 repointed it): ID is populated
                    # on 89,744 of 89,744 features, 0 null, 0 empty and 0
                    # 'NOT AVAILABLE'/'UNKNOWN' sentinel — so this fallback
                    # never fired anyway, and removing it costs no coverage
                    # while removing the landmine. (The same check passed on
                    # the superseded 52,244-feature layer; the property held
                    # across the swap, it was not assumed.)
                    #
                    # When ID really is absent the fallback is the PHYSICAL
                    # identity of a transmission line: the ordered pair of
                    # substations it terminates on plus its voltage. SUB_1/SUB_2
                    # are populated on 100/100 and 99/100 of a live NoVA sample
                    # with 76 distinct SUB_1 values over 100 lines, so the
                    # composite discriminates where owner+voltage (14 distinct
                    # keys over those same 100 lines) does not.
                    line_id = str(attrs.get('ID', '') or '').strip()
                    if not line_id:
                        sub1 = str(attrs.get('SUB_1', '') or '').strip()
                        sub2 = str(attrs.get('SUB_2', '') or '').strip()
                        if sub1 and sub2:
                            line_id = f"{sub1}~{sub2}~{voltage}"
                    route = {
                        "name": hifld_line_name(owner, voltage, market['name'])[:200],
                        "provider": str(owner)[:100],
                        "type": "transmission",
                        "start": market['name'],
                        "end": market['name'],
                        # NB: this is the MARKET centroid, not the line's
                        # geometry (return_geometry=False above). It is stored
                        # as a locator and must never re-enter the identity —
                        # see _save_route.
                        "start_lat": market['lat'],
                        "start_lng": market['lng'],
                        "voltage_kv": voltage,
                        "uid": line_id,
                        "source_id": f"hifld_tl_{line_id}"
                    }
                    self._save_route(route, source='hifld')
                logger.info(f"   📡 HIFLD transmission {market['name']}: {len(features)} lines found")
                time.sleep(1)
            except Exception as e:
                logger.warning(f"   ⚠️ HIFLD transmission failed for {market['name']}: {e}")

    def _sync_peeringdb_exchanges(self):
        try:
            response = requests.get(f"{self.PEERINGDB_API}/ix?limit=100", timeout=15)
            if response.ok:
                data = response.json().get('data', [])
                for ix in data[:100]:
                    self._save_fiber_endpoint(ix)
                logger.info(f"   📡 PeeringDB IXs: {len(data[:100])} processed")
        except Exception as e:
            logger.warning(f"   ⚠️ PeeringDB IX sync failed: {e}")

    def _sync_osm_fiber_cables(self):
        markets = DC_MARKETS[self._market_index:self._market_index + 3]
        for market in markets:
            try:
                query = f"""
                [out:json][timeout:25];
                (
                  way["communication"="line"](around:50000,{market['lat']},{market['lng']});
                  way["utility"="telecom"](around:50000,{market['lat']},{market['lng']});
                  way["man_made"="pipeline"]["substance"="telecommunication"](around:50000,{market['lat']},{market['lng']});
                );
                out center 50;
                """
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                elements = []
                if response.ok:
                    data = response.json()
                    elements = data.get('elements', [])
                    for element in elements:
                        tags = element.get('tags', {})
                        center = element.get('center', {})
                        route = {
                            "name": tags.get('name', f"Telecom line near {market['name']}")[:200],
                            "provider": tags.get('operator', tags.get('owner', 'Unknown'))[:100],
                            "type": "fiber",
                            "start": market['name'],
                            "end": market['name'],
                            "start_lat": center.get('lat', 0),
                            "start_lng": center.get('lon', 0),
                            # OSM element ids are stable and upstream-assigned.
                            # The Overpass query above selects way[...] only, so
                            # a bare id cannot collide with a node of the same
                            # number; it is also the value the migration derives
                            # from existing osm_fiber_* source_ids, so held rows
                            # and new writes land on the same key.
                            "uid": str(element.get('id') or ''),
                            "source_id": f"osm_fiber_{element.get('id', 0)}"
                        }
                        self._save_route(route, source='osm')
                logger.info(f"   📡 OSM fiber {market['name']}: {len(elements)} cables found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM fiber failed for {market['name']}: {e}")

    def _sync_from_learned_apis(self):
        try:
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT name, location, metadata FROM learned_infrastructure
                    WHERE category = 'fiber'
                    ORDER BY id DESC LIMIT 200
                """)
                for row in cursor.fetchall():
                    try:
                        meta = json.loads(row['metadata']) if row['metadata'] else {}
                        # ★★★ SH52-054 — THIS LINE WAS A DUPLICATE GENERATOR.
                        # It read:
                        #     f"learned_fiber_{hash(row['name']) % 10**8}"
                        # CPython salts str hashing per process and
                        # PYTHONHASHSEED is not set anywhere in this repo
                        # (grep: only a comment in routes/brain_v2_layer5.py),
                        # so the SAME learned row minted a NEW source_id on
                        # every worker restart. Before 2026-08-10 the
                        # synthesized name collapsed those onto one
                        # (name, provider) key, which accidentally contained
                        # the damage; the fingerprint fix removed that
                        # containment and the lane started leaking.
                        #
                        # Measured on the live table 2026-08-12 — 13 rows, and
                        # every one added since 08-10 is a fresh identity for
                        # the same upstream row:
                        #     'Unknown [901a7c409094]' learned_fiber_69486095_...
                        #     'Unknown [22712c4f84ff]' learned_fiber_73392280_...
                        #     'Unknown [d9a588d0c476]' learned_fiber_34064995_...
                        #     ... ~5/day, unbounded.
                        #
                        # md5 of the same input is the same digest in every
                        # process, forever. Truncated to 16 hex chars: 64 bits
                        # over a lane that reads at most 200 rows, so collision
                        # risk is not the failure mode here — nondeterminism was.
                        _lname = row['name'] or ''
                        _luid = hashlib.md5(
                            f"learned_fiber|{_lname}".encode('utf-8')).hexdigest()[:16]
                        route = {
                            "name": row['name'][:200] if row['name'] else 'Unknown',
                            "provider": meta.get('OWNER', meta.get('OPERATOR', 'Discovered')),
                            "type": meta.get('TYPE', 'fiber'),
                            "start": row['location'][:100] if row['location'] else '',
                            "end": '',
                            "uid": _luid,
                            "source_id": f"learned_fiber_{_luid}"
                        }
                        self._save_route(route, source='auto_discovery')
                    except Exception:
                        pass
            finally:
                try: conn.close()
                except Exception: pass
        except Exception as e:
            logger.warning(f"   ⚠️ Learned API fiber sync failed: {e}")

    def _save_fiber_endpoint(self, ix):
        # FIX: INSERT OR IGNORE → ON CONFLICT DO NOTHING, ? → %s
        # PeeringDB's numeric IX id is upstream-assigned and stable; it is also
        # what the migration derives from the existing peeringdb_ix_* source_ids,
        # so the 2,288 held rows and any new write share one key space.
        _ix_uid = _route_uid({'uid': ix.get('id')})
        rowcount = _safe_write('''
            INSERT INTO fiber_routes
            (name, provider, route_type, start_location, source, source_id,
             upstream_uid)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        ''', (
            ix.get('name', 'Unknown IX'),
            'Internet Exchange', 'IX',
            ix.get('city', 'Unknown'),
            'peeringdb',
            f"peeringdb_ix_{ix.get('id')}",
            _ix_uid
        ))
        if rowcount and rowcount > 0:
            self.new_routes += 1

    def _save_route(self, route, source='discovery'):
        try:
            base_name = (route.get('name') or 'route')[:200]
            provider = (route.get('provider') or 'Unknown')[:100]
            # SH52-054 (2026-08-10): fiber_routes carries a live UNIQUE(name,
            # provider). The discovery path synthesizes `name` from
            # owner/voltage/market (e.g. "Dominion 500kV Line - Northern
            # Virginia"), so MANY distinct physical segments share one
            # (name, provider) key and the ON CONFLICT DO NOTHING below discards
            # all but the first. Measured live 2026-08-10: terrestrial discovery
            # held ~154 rows (hifld 109 / discovery 28 / osm 16 / auto 1) against
            # 55k of bulk carrier data — the discovery lane was structurally
            # capped, not quiet. Even the provided source_id does not save it: the
            # HIFLD caller sets source_id="hifld_tl_{id}" and `id` is often empty,
            # so those collapse on source_id too.
            #
            # Fingerprint each PHYSICAL segment from its geometry + whatever id the
            # source gave, and fold that into BOTH keys so distinct lines keep
            # distinct (name, provider) and source_id. The fingerprint is stable
            # across re-runs (same segment -> same keys -> correct dedup, no dup
            # explosion); genuinely indistinguishable rows (no id, no geometry,
            # same name) still collapse, which is correct.
            #
            # ★★★ 2026-08-12 — THE PARAGRAPH ABOVE IS HALF TRUE, AND THE HALF
            # THAT IS FALSE IS NOW MEASURED. The fix above DID un-cap the lane:
            # hifld went 109 rows -> 1,826 in three days. But it over-splits,
            # because `geo` is NOT the segment's geometry.
            # _sync_hifld_transmission_lines calls the HIFLD layer with
            # return_geometry=False and then fills start_lat/start_lng with
            # market['lat']/market['lng'] — the MARKET CENTROID, identical for
            # every line in that market, and different for the same line seen
            # from a neighbouring market. DC_MARKETS are swept at a 50 km
            # radius and those radii overlap, so one physical line reached from
            # two markets produced two `seg` values, two source_ids, two names
            # and TWO ROWS.
            #
            # Measured live on the Neon table 2026-08-12:
            #     1,826 hifld rows  /  1,742 distinct upstream HIFLD line ids
            #     -> 84 physical lines already held twice (4.8%), climbing daily.
            #
            # That is an identity derived from OUR CRAWL GEOMETRY instead of
            # from the asset. Two upstream records are the same physical line
            # when the SOURCE says so, and this source does say so: HIFLD's
            # `ID` field is populated on 89,744 of 89,744 features with zero
            # empties and zero 'NOT AVAILABLE'/'UNKNOWN' sentinels (verified
            # against the live FeatureServer 2026-08-12, on the canonical layer
            # SH52-057 repointed this lane onto; it held on the superseded
            # 52,244-feature layer too) — the claim above that "`id` is often
            # empty" was never true of either layer.
            #
            # So identity is now taken from a SOURCE-INTRINSIC uid the caller
            # supplies, and nothing the caller merely happened to be standing
            # near. `upstream_uid` is written to its own column and arbitrated
            # by a partial unique index on (source, upstream_uid); the bare
            # ON CONFLICT DO NOTHING below covers EVERY unique index on the
            # table, this one included, so the market-B sighting of a line we
            # already hold is discarded with no SQL change here.
            #
            # name and source_id keep their existing shape ON PURPOSE. Changing
            # them would give all 1,826 held rows new keys and re-insert every
            # one of them — a duplicate wave dressed as a fix. Published
            # identities stay exactly as they are; only the arbiter is new.
            raw_sid = str(route.get('source_id') or '')
            geo = "|".join('' if route.get(k) is None else str(route.get(k))
                           for k in ('start_lat', 'start_lng', 'end_lat', 'end_lng'))
            seg = hashlib.md5(
                f"{provider}|{raw_sid}|{geo}".encode('utf-8')).hexdigest()[:16]
            source_id = ((raw_sid + '_' + seg) if raw_sid else seg)[:100]
            name = (base_name if seg in base_name
                    else f"{base_name[:180]} [{seg[:12]}]")[:200]
            # The uid is the upstream's own identifier for the asset. Absent
            # (a source that genuinely cannot identify its rows) it stays NULL:
            # the partial index skips NULLs, so such a row falls back to the
            # pre-existing name/source_id arbitration rather than claiming an
            # identity it does not have. NULL here means UNIDENTIFIED, never
            # "unique" — do not substitute a random or per-process value to
            # "fill it in"; that is exactly the defect fixed in
            # _sync_from_learned_apis below.
            upstream_uid = _route_uid(route)
            # FIX: INSERT OR IGNORE → ON CONFLICT DO NOTHING, ? → %s
            rowcount = _safe_write('''
                INSERT INTO fiber_routes
                (name, provider, route_type, start_location, end_location,
                 start_lat, start_lng, end_lat, end_lng, source, source_id,
                 upstream_uid)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            ''', (
                name,
                provider,
                route.get('type', 'terrestrial'),
                route.get('start', ''),
                route.get('end', ''),
                route.get('start_lat'),
                route.get('start_lng'),
                route.get('end_lat'),
                route.get('end_lng'),
                source,
                source_id,
                upstream_uid
            ))
            if rowcount and rowcount > 0:
                self.new_routes += 1
        except Exception as e:
            logger.warning(f"Error saving route: {e}")


class DCPropertyDiscovery:
    """Discover data center properties from OSM, news, and learned APIs"""

    OVERPASS_API = "https://overpass-api.de/api/interpreter"

    def __init__(self):
        self.new_properties = 0
        self._market_index = 0

    def sync(self):
        logger.info("🏢 Syncing DC properties...")
        self.new_properties = 0
        self._sync_osm_properties()
        self._sync_from_news()
        self._sync_from_learned_apis()
        logger.info(f"   ✅ DC properties: {self.new_properties} new")
        return self.new_properties

    def _sync_osm_properties(self):
        markets = DC_MARKETS[self._market_index:self._market_index + 3]
        self._market_index = (self._market_index + 3) % len(DC_MARKETS)

        for market in markets:
            try:
                query = f"""
                [out:json][timeout:25];
                (
                  node["building"="data_centre"](around:80000,{market['lat']},{market['lng']});
                  way["building"="data_centre"](around:80000,{market['lat']},{market['lng']});
                  node["telecom"="data_center"](around:80000,{market['lat']},{market['lng']});
                  way["telecom"="data_center"](around:80000,{market['lat']},{market['lng']});
                  way["building"="industrial"]["operator"~"data|cloud|hosting|colo",i](around:80000,{market['lat']},{market['lng']});
                );
                out center 100;
                """
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                if response.ok:
                    elements = response.json().get('elements', [])
                    for el in elements:
                        tags = el.get('tags', {})
                        center = el.get('center', {})
                        lat = el.get('lat') or center.get('lat', 0)
                        lng = el.get('lon') or center.get('lon', 0)
                        prop = {
                            "name": tags.get('name', f"DC Property near {market['name']}")[:200],
                            "city": market['name'],
                            "state": market['state'],
                            "type": "data_center",
                            "status": "active",
                            "lat": lat,
                            "lng": lng,
                            "source_id": f"osm_prop_{el.get('id', 0)}"
                        }
                        self._save_property(prop, source='osm')
                    logger.info(f"   🏢 OSM properties {market['name']}: {len(elements)} found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM property sync failed for {market['name']}: {e}")

    def _sync_from_news(self):
        conn = get_db()
        try:
            cursor = conn.cursor()
            keywords = ['for sale', 'listing', 'available', 'seeking buyer', 'on the market',
                        'acquisition', 'campus', 'new facility', 'expansion']
            for keyword in keywords:
                try:
                    # FIX: datetime('now') → NOW() - INTERVAL, ? → %s
                    cursor.execute('''
                        SELECT title, summary, companies, locations FROM announcements
                        WHERE (title ILIKE %s OR summary ILIKE %s)
                        AND discovered_at::timestamptz > NOW() - INTERVAL '30 days'
                        LIMIT 20
                    ''', (f'%{keyword}%', f'%{keyword}%'))

                    for row in cursor.fetchall():
                        title = (row['title'] or '').lower()
                        summary = (row['summary'] or '').lower()
                        if 'data center' in title or 'data center' in summary or 'datacenter' in title:
                            prop = {
                                "name": row['title'][:200] if row['title'] else 'Unknown',
                                "city": row['locations'].split(',')[0].strip() if row['locations'] else 'Unknown',
                                "state": "",
                                "type": "listing",
                                "status": "available"
                            }
                            self._save_property(prop, source='news')
                except Exception:
                    pass
        finally:
            try: conn.close()
            except Exception: pass

    def _sync_from_learned_apis(self):
        try:
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT name, location, metadata FROM learned_infrastructure
                    WHERE category IN ('other', 'environmental')
                    AND (name ILIKE '%data center%' OR name ILIKE '%datacenter%' OR name ILIKE '%colocation%')
                    ORDER BY id DESC LIMIT 100
                """)
                for row in cursor.fetchall():
                    try:
                        meta = json.loads(row['metadata']) if row['metadata'] else {}
                        prop = {
                            "name": row['name'][:200] if row['name'] else 'Unknown',
                            "city": row['location'].split(',')[0].strip() if row['location'] else '',
                            "state": row['location'].split(',')[-1].strip() if row['location'] else '',
                            "type": "discovered",
                            "status": "active",
                            "source_id": f"learned_prop_{hash(row['name']) % 10**8}"
                        }
                        self._save_property(prop, source='auto_discovery')
                    except Exception:
                        pass
            finally:
                try: conn.close()
                except Exception: pass
        except Exception as e:
            logger.warning(f"   ⚠️ Learned API property sync failed: {e}")

    def _save_property(self, prop, source='discovery'):
        try:
            source_id = prop.get('source_id', f"{prop['name']}_{prop['city']}".replace(" ", "_").lower()[:100])
            # FIX: INSERT OR IGNORE → ON CONFLICT DO NOTHING, ? → %s
            rowcount = _safe_write('''
                INSERT INTO dc_properties
                (name, city, state, lat, lng, property_type, square_feet, power_capacity_mw, status, source, source_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(source_id) DO NOTHING
            ''', (
                prop['name'][:200],
                prop.get('city', '')[:100],
                prop.get('state', ''),
                prop.get('lat'),
                prop.get('lng'),
                prop.get('type', 'facility'),
                prop.get('sqft', 0),
                prop.get('power_mw', 0),
                prop.get('status', 'available'),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_properties += 1
        except Exception as e:
            logger.warning(f"Error saving property: {e}")


class ConstructionPermitDiscovery:
    """Discover construction permits from HIFLD power plants, OSM, and news"""

    OVERPASS_API = "https://overpass-api.de/api/interpreter"

    def __init__(self):
        self.new_permits = 0
        self._market_index = 0

    def sync(self):
        logger.info("🏗️ Syncing construction permits...")
        self.new_permits = 0
        self._sync_hifld_power_plants()
        self._sync_osm_construction()
        self._sync_from_news()
        logger.info(f"   ✅ Construction permits: {self.new_permits} new")
        return self.new_permits

    def _sync_hifld_power_plants(self):
        # HIFLD Power_Plants FeatureServer URL is dead (moved to hash-based names 2024).
        # Skip network call to avoid ArcGIS 400/499 errors and downstream DB transaction aborts.
        logger.info("   ℹ️ HIFLD Power Plants URL deprecated — skipping (avoids ArcGIS 400 errors)")
        self._market_index = (self._market_index + 4) % len(DC_MARKETS)

    def _sync_osm_construction(self):
        markets = DC_MARKETS[self._market_index:self._market_index + 3]

        for market in markets:
            try:
                query = f"""
                [out:json][timeout:25];
                (
                  node["building"="construction"]["name"~"data|server|cloud|colo",i](around:80000,{market['lat']},{market['lng']});
                  way["building"="construction"]["name"~"data|server|cloud|colo",i](around:80000,{market['lat']},{market['lng']});
                  way["landuse"="construction"]["name"~"data|server|cloud|colo",i](around:80000,{market['lat']},{market['lng']});
                  node["construction"="yes"]["building"~"industrial|commercial"](around:80000,{market['lat']},{market['lng']});
                );
                out center 50;
                """
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                if response.ok:
                    elements = response.json().get('elements', [])
                    for el in elements:
                        tags = el.get('tags', {})
                        center = el.get('center', {})
                        permit = {
                            "name": tags.get('name', f"Construction near {market['name']}")[:200],
                            "city": market['name'],
                            "state": market['state'],
                            "owner": tags.get('operator', tags.get('developer', 'Unknown')),
                            "status": "under_construction",
                            "lat": el.get('lat') or center.get('lat', 0),
                            "lng": el.get('lon') or center.get('lon', 0),
                            "source_id": f"osm_constr_{el.get('id', 0)}"
                        }
                        self._save_permit(permit, source='osm')
                    logger.info(f"   🏗️ OSM construction {market['name']}: {len(elements)} found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM construction sync failed for {market['name']}: {e}")

    def _sync_from_news(self):
        conn = get_db()
        try:
            cursor = conn.cursor()
            keywords = ['construction', 'groundbreaking', 'breaking ground', 'new campus',
                        'expansion', 'building permit', 'megawatt', 'hyperscale',
                        'development', 'approved', 'planning commission', 'zoning']
            for keyword in keywords:
                try:
                    # FIX: datetime('now') → NOW() - INTERVAL, LIKE → ILIKE, ? → %s
                    cursor.execute('''
                        SELECT title, summary, companies, locations FROM announcements
                        WHERE (title ILIKE %s OR summary ILIKE %s)
                        AND discovered_at::timestamptz > NOW() - INTERVAL '30 days'
                        LIMIT 20
                    ''', (f'%{keyword}%', f'%{keyword}%'))

                    for row in cursor.fetchall():
                        title = (row['title'] or '').lower()
                        summary = (row['summary'] or '').lower()
                        if 'data center' in title or 'data center' in summary or 'datacenter' in title or 'hyperscale' in title:
                            permit = {
                                "name": row['title'][:200] if row['title'] else 'Unknown Project',
                                "city": row['locations'].split(',')[0].strip() if row['locations'] else 'Unknown',
                                "state": "",
                                "owner": row['companies'].split(',')[0].strip() if row['companies'] else 'Unknown',
                                "status": "announced"
                            }
                            self._save_permit(permit, source='news')
                except Exception:
                    pass
        finally:
            try: conn.close()
            except Exception: pass

    def _save_permit(self, permit, source='discovery'):
        try:
            source_id = permit.get('source_id', f"{permit['name']}_{permit['city']}".replace(" ", "_").lower()[:100])
            # FIX: INSERT OR IGNORE → ON CONFLICT DO NOTHING, ? → %s
            rowcount = _safe_write('''
                INSERT INTO construction_permits
                (project_name, city, state, square_feet, estimated_power_mw, owner, status, lat, lng, source, source_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(source_id) DO NOTHING
            ''', (
                permit['name'][:200],
                permit.get('city', '')[:100],
                permit.get('state', ''),
                permit.get('sqft', 0),
                permit.get('power_mw', 0),
                permit.get('owner', ''),
                permit.get('status', 'announced'),
                permit.get('lat'),
                permit.get('lng'),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_permits += 1
        except Exception as e:
            logger.warning(f"Error saving permit: {e}")


class SubstationDiscovery:
    """Discover substations from HIFLD, OSM, and learned APIs"""

    OVERPASS_API = "https://overpass-api.de/api/interpreter"

    def __init__(self):
        self.new_substations = 0
        self._market_index = 0

    def sync(self):
        logger.info("⚡ Syncing substations...")
        self.new_substations = 0
        self._sync_hifld_substations()
        self._sync_osm_substations()
        self._sync_from_learned_apis()
        logger.info(f"   ✅ Substations: {self.new_substations} new")
        return self.new_substations

    def _sync_hifld_substations(self):
        # HIFLD Electric_Substations FeatureServer URL is dead (moved to hash-based names 2024).
        # Fall through to OSM-based substation sync only.
        logger.info("   ℹ️ HIFLD Electric Substations URL deprecated — using OSM only")
        self._market_index = (self._market_index + 3) % len(DC_MARKETS)
        return

    def _sync_hifld_substations_DISABLED(self):
        markets = DC_MARKETS[self._market_index:self._market_index + 3]
        self._market_index = (self._market_index + 3) % len(DC_MARKETS)

        for market in markets:
            try:
                features = _query_hifld_nearby(
                    HIFLD_APIS.get('substations', ''),
                    market['lat'], market['lng'],
                    radius_m=50000, max_records=200
                )
                for feat in features:
                    attrs = feat.get('attributes', {})
                    geom = feat.get('geometry', {})
                    name = attrs.get('NAME', attrs.get('SUBSTATION', 'Unknown'))
                    operator = attrs.get('OWNER', attrs.get('OPERATOR', attrs.get('UTILITY', '')))
                    voltage = attrs.get('MAX_VOLT', attrs.get('MIN_VOLT', 0)) or 0
                    if voltage and voltage > 1000:
                        voltage = voltage / 1000
                    capacity = attrs.get('MAX_LOAD', attrs.get('CAPACITY', 0)) or 0
                    sub_id = attrs.get('OBJECTID', attrs.get('ID', ''))
                    state = attrs.get('STATE', attrs.get('STUSPS', market['state']))
                    city = attrs.get('CITY', attrs.get('COUNTY', market['name']))

                    sub = {
                        "name": str(name)[:200],
                        "operator": str(operator)[:100] if operator else 'Unknown',
                        "voltage_kv": voltage,
                        "capacity_mva": capacity,
                        "city": str(city)[:100] if city else market['name'],
                        "state": str(state)[:10] if state else market['state'],
                        "lat": geom.get('y', geom.get('lat', 0)),
                        "lng": geom.get('x', geom.get('lon', 0)),
                        "source_id": f"hifld_sub_{sub_id}"
                    }
                    self._save_substation(sub, source='hifld')

                logger.info(f"   ⚡ HIFLD substations {market['name']}: {len(features)} found")
                time.sleep(1)
            except Exception as e:
                logger.warning(f"   ⚠️ HIFLD substations failed for {market['name']}: {e}")

    def _sync_osm_substations(self):
        start = self._market_index
        markets = DC_MARKETS[start:start + 4]

        for market in markets:
            try:
                query = f"""
                [out:json][timeout:25];
                (
                  node["power"="substation"](around:80000,{market['lat']},{market['lng']});
                  way["power"="substation"](around:80000,{market['lat']},{market['lng']});
                  node["power"="plant"](around:80000,{market['lat']},{market['lng']});
                );
                out center 100;
                """
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                if response.ok:
                    data = response.json()
                    for element in data.get('elements', []):
                        tags = element.get('tags', {})
                        lat = element.get('lat') or element.get('center', {}).get('lat')
                        lng = element.get('lon') or element.get('center', {}).get('lon')
                        if lat and lng:
                            sub = {
                                "name": tags.get('name', f"Substation near {market['name']}")[:200],
                                "operator": tags.get('operator', 'Unknown')[:100],
                                "voltage_kv": self._parse_voltage(tags.get('voltage', '')),
                                "city": market['name'],
                                "state": market['state'],
                                "lat": lat,
                                "lng": lng,
                                "source_id": f"osm_sub_{element.get('id', 0)}"
                            }
                            self._save_substation(sub, source='osm')
                    logger.info(f"   ⚡ OSM substations {market['name']}: {len(data.get('elements', []))} found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM substation sync failed for {market['name']}: {e}")

    def _sync_from_learned_apis(self):
        try:
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT name, location, metadata FROM learned_infrastructure
                    WHERE category = 'power'
                    ORDER BY id DESC LIMIT 200
                """)
                for row in cursor.fetchall():
                    try:
                        meta = json.loads(row['metadata']) if row['metadata'] else {}
                        voltage = meta.get('MAX_VOLT', meta.get('VOLTAGE', meta.get('KV', 0))) or 0
                        if voltage and voltage > 1000:
                            voltage = voltage / 1000
                        sub = {
                            "name": str(row['name'])[:200] if row['name'] else 'Unknown',
                            "operator": str(meta.get('OWNER', meta.get('OPERATOR', 'Discovered')))[:100],
                            "voltage_kv": voltage,
                            "capacity_mva": meta.get('CAPACITY', 0) or 0,
                            "city": row['location'].split(',')[0].strip() if row['location'] else '',
                            "state": row['location'].split(',')[-1].strip() if row['location'] and ',' in row['location'] else '',
                            "lat": meta.get('LATITUDE', meta.get('LAT', meta.get('Y'))),
                            "lng": meta.get('LONGITUDE', meta.get('LON', meta.get('X'))),
                            "source_id": f"learned_sub_{hash(str(row['name'])) % 10**8}"
                        }
                        self._save_substation(sub, source='auto_discovery')
                    except Exception:
                        pass
            finally:
                try: conn.close()
                except Exception: pass
        except Exception as e:
            logger.warning(f"   ⚠️ Learned API substation sync failed: {e}")

    def _parse_voltage(self, voltage_str):
        try:
            if not voltage_str:
                return 0
            voltage_str = str(voltage_str).replace('kV', '').replace('V', '').strip()
            if ';' in voltage_str:
                voltage_str = voltage_str.split(';')[0]
            voltage = float(voltage_str)
            if voltage > 1000:
                voltage = voltage / 1000
            return voltage
        except:
            return 0

    def _save_substation(self, sub, source='discovery'):
        try:
            # Filter out telecom/distribution substations at ingestion
            # Only save utility-grade substations (>69kV) or unknown voltage (0/None)
            voltage = sub.get('voltage_kv', 0) or 0
            if 0 < voltage <= 69:
                return  # Skip telecom/distribution-grade substations

            # Phase ZZZZ-substation-fix (2026-05-18): .get('lat', 0) returns
            # None when key EXISTS with None value (the default only kicks
            # in when key is missing). Coerce both to floats explicitly so
            # the :.4f format never gets None.
            lat_val = sub.get('lat')
            lng_val = sub.get('lng')
            lat_safe = float(lat_val) if lat_val is not None else 0.0
            lng_safe = float(lng_val) if lng_val is not None else 0.0
            source_id = sub.get('source_id', f"{sub['name']}_{lat_safe:.4f}_{lng_safe:.4f}".replace(" ", "_").lower()[:100])
            # FIX: INSERT OR IGNORE → ON CONFLICT DO NOTHING, ? → %s
            rowcount = _safe_write('''
                INSERT INTO substations
                (name, operator, voltage_kv, capacity_mva, city, state, lat, lng, source, source_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            ''', (
                sub['name'][:200],
                sub.get('operator', '')[:100],
                sub.get('voltage_kv', 0),
                sub.get('capacity_mva', 0),
                sub.get('city', '')[:100],
                sub.get('state', ''),
                sub.get('lat'),
                sub.get('lng'),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_substations += 1
        except Exception as e:
            logger.warning(f"Error saving substation: {e}")


class GasPipelineDiscovery:
    """Discover gas pipelines from EIA ArcGIS and learned APIs"""

    def __init__(self):
        self.new_pipelines = 0
        self._market_index = 0

    def sync(self):
        logger.info("🔥 Syncing gas pipelines...")
        self.new_pipelines = 0
        self._sync_eia_gas_pipelines()
        self._sync_eia_gulf_pipelines()
        self._sync_from_learned_apis()
        logger.info(f"   ✅ Gas pipelines: {self.new_pipelines} new")
        return self.new_pipelines

    def _sync_eia_gas_pipelines(self):
        if not hasattr(GasPipelineDiscovery, '_eia_fid_offset'):
            GasPipelineDiscovery._eia_fid_offset = 0

        batch_size = 2000
        fid_start = GasPipelineDiscovery._eia_fid_offset
        fid_end = fid_start + batch_size

        logger.info(f"   🔥 EIA gas pipelines: FID {fid_start}-{fid_end}...")
        before = self.new_pipelines

        try:
            features = _query_hifld_paginated(
                EIA_PIPELINE_APIS['natural_gas'],
                where=f'FID>{fid_start} AND FID<={fid_end}',
                max_total=batch_size,
                batch_size=1000
            )

            if not features:
                logger.info(f"   🔥 EIA gas pipelines: reached end at FID {fid_start}, resetting to 0")
                GasPipelineDiscovery._eia_fid_offset = 0
            else:
                GasPipelineDiscovery._eia_fid_offset = fid_end

                for feat in features:
                    attrs = feat.get('attributes', {})
                    geom = feat.get('geometry', {})
                    operator = attrs.get('Operator', 'Unknown')
                    typepipe = attrs.get('TYPEPIPE', 'Interstate')
                    status = attrs.get('Status', 'Operating')
                    fid = attrs.get('FID', '')

                    if str(status).lower() not in ('operating', 'active', 'in service'):
                        continue

                    lat = lng = None
                    if geom:
                        if 'paths' in geom and geom['paths']:
                            path = geom['paths'][0]
                            mid = path[len(path) // 2] if path else None
                            if mid and len(mid) >= 2:
                                lng, lat = mid[0], mid[1]
                        elif 'x' in geom and 'y' in geom:
                            lng, lat = geom['x'], geom['y']

                    if not lat or not lng:
                        continue

                    city = self._nearest_market(lat, lng)
                    state = self._lat_lng_to_state(lat, lng)
                    pipe_type = 'interstate' if 'Interstate' in str(typepipe) else 'intrastate'

                    pipeline = {
                        "name": f"{operator} ({typepipe})"[:200],
                        "operator": str(operator)[:100],
                        "pipeline_type": pipe_type,
                        "status": 'active',
                        "city": city,
                        "state": state,
                        "lat": lat,
                        "lng": lng,
                        "source_id": f"eia_gas_{fid}"
                    }
                    self._save_pipeline(pipeline, source='eia')

            logger.info(f"   🔥 EIA gas pipelines: {len(features)} fetched (FID {fid_start}-{fid_end}), {self.new_pipelines - before} new")
        except Exception as e:
            logger.warning(f"   ⚠️ EIA gas pipelines failed: {e}")

    def _sync_eia_gulf_pipelines(self):
        logger.info("   🔥 EIA Gulf pipelines: pulling...")
        before = self.new_pipelines

        try:
            features = _query_hifld_paginated(
                EIA_PIPELINE_APIS['gulf_pipelines'],
                where='1=1',
                max_total=5000,
                batch_size=1000
            )

            for feat in features:
                attrs = feat.get('attributes', {})
                geom = feat.get('geometry', {})
                operator = attrs.get('Operator', attrs.get('OPER_NM', 'Unknown'))
                name = attrs.get('SYS_NM', attrs.get('Name', operator))
                fid = attrs.get('FID', attrs.get('OBJECTID', ''))

                lat = lng = None
                if geom:
                    if 'paths' in geom and geom['paths']:
                        path = geom['paths'][0]
                        mid = path[len(path) // 2] if path else None
                        if mid and len(mid) >= 2:
                            lng, lat = mid[0], mid[1]
                    elif 'x' in geom and 'y' in geom:
                        lng, lat = geom['x'], geom['y']

                if not lat or not lng:
                    continue

                pipeline = {
                    "name": f"{name} (Gulf)"[:200],
                    "operator": str(operator)[:100],
                    "pipeline_type": "offshore",
                    "status": "active",
                    "city": "Gulf of Mexico",
                    "state": "GOM",
                    "lat": lat,
                    "lng": lng,
                    "source_id": f"eia_gulf_{fid}"
                }
                self._save_pipeline(pipeline, source='eia_gulf')

            logger.info(f"   🔥 EIA Gulf pipelines: {len(features)} fetched, {self.new_pipelines - before} new")
        except Exception as e:
            logger.warning(f"   ⚠️ EIA Gulf pipelines failed: {e}")

    def _lat_lng_to_state(self, lat, lng):
        for m in DC_MARKETS:
            from math import radians, sin, cos, sqrt, atan2
            dlat = radians(m['lat'] - lat)
            dlng = radians(m['lng'] - lng)
            a = sin(dlat/2)**2 + cos(radians(lat)) * cos(radians(m['lat'])) * sin(dlng/2)**2
            d = 2 * atan2(sqrt(a), sqrt(1 - a)) * 6371
            if d < 150:
                return m['state']
        return ''

    def _nearest_market(self, lat, lng):
        from math import radians, sin, cos, sqrt, atan2
        best = 'Unknown'
        best_dist = float('inf')
        for m in DC_MARKETS:
            dlat = radians(m['lat'] - lat)
            dlng = radians(m['lng'] - lng)
            a = sin(dlat/2)**2 + cos(radians(lat)) * cos(radians(m['lat'])) * sin(dlng/2)**2
            d = 2 * atan2(sqrt(a), sqrt(1 - a)) * 6371
            if d < best_dist:
                best_dist = d
                best = m['name']
        return best if best_dist < 500 else ''

    def _sync_from_learned_apis(self):
        try:
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT name, location, metadata FROM learned_infrastructure
                    WHERE category = 'gas'
                    ORDER BY id DESC LIMIT 200
                """)
                for row in cursor.fetchall():
                    try:
                        meta = json.loads(row['metadata']) if row['metadata'] else {}
                        pipeline = {
                            "name": str(row['name'])[:200] if row['name'] else 'Unknown',
                            "operator": str(meta.get('Operator', meta.get('OPERATOR', 'Discovered')))[:100],
                            "pipeline_type": meta.get('Typepipe', meta.get('TYPE', 'discovered')),
                            "diameter_inches": meta.get('Diameter', 0) or 0,
                            "city": row['location'].split(',')[0].strip() if row['location'] else '',
                            "state": row['location'].split(',')[-1].strip() if row['location'] and ',' in row['location'] else '',
                            "lat": meta.get('LATITUDE', meta.get('LAT', meta.get('Y'))),
                            "lng": meta.get('LONGITUDE', meta.get('LON', meta.get('X'))),
                            "source_id": f"learned_gas_{hash(str(row['name'])) % 10**8}"
                        }
                        self._save_pipeline(pipeline, source='auto_discovery')
                    except Exception:
                        pass
            finally:
                try: conn.close()
                except Exception: pass
        except Exception as e:
            logger.warning(f"   ⚠️ Learned API gas sync failed: {e}")

    def _save_pipeline(self, pipeline, source='discovery'):
        try:
            source_id = pipeline.get('source_id', f"{pipeline['name']}".replace(" ", "_").lower()[:100])
            # FIX r34 (2026-05-22): ON CONFLICT target was (source_id) but the
            # actual unique index is gas_pipelines_name_operator_uniq on
            # (name, operator). Different pulls produced different source_ids
            # for the same Texas Intrastate Pipeline row → ON CONFLICT didn't
            # match → duplicate raised → retry loop hammered the DB → warning
            # spam filled Railway logs (same shape as the 2026-05-21 incident).
            # Target the constraint by name so the conflict is handled cleanly
            # regardless of which columns the unique index actually covers.
            rowcount = _safe_write('''
                INSERT INTO gas_pipelines
                (name, operator, pipeline_type, diameter_inches, capacity_mcf, status,
                 lat, lng, city, state, source, source_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name, operator) DO NOTHING
            ''', (
                pipeline['name'][:200],
                pipeline.get('operator', '')[:100],
                pipeline.get('pipeline_type', 'interstate'),
                pipeline.get('diameter_inches', 0),
                pipeline.get('capacity_mcf', 0),
                pipeline.get('status', 'active'),
                pipeline.get('lat'),
                pipeline.get('lng'),
                pipeline.get('city', '')[:100],
                pipeline.get('state', ''),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_pipelines += 1
        except Exception as e:
            logger.warning(f"Error saving pipeline: {e}")


class WeeklyLinkedInSummary:
    """Generate and post weekly market digest to LinkedIn"""

    def __init__(self):
        # Resolved lazily in post_to_linkedin() — binding the token at
        # construction pinned whatever the env var held at that moment, and
        # that env var goes stale/revoked silently. See routes/li_token.py.
        self.linkedin_token = None

    def generate_weekly_digest(self):
        conn = get_db()
        try:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) as cnt FROM facilities")
            new_facilities = cursor.fetchone()['cnt']

            # FIX: datetime('now') → NOW() - INTERVAL, ? → %s
            cursor.execute("SELECT COUNT(*) as cnt FROM announcements WHERE discovered_at::timestamptz > NOW() - INTERVAL '7 days'")
            new_news = cursor.fetchone()['cnt']

            cursor.execute("SELECT COUNT(*) as cnt FROM construction_permits")
            new_permits = cursor.fetchone()['cnt']

            cursor.execute("SELECT SUM(estimated_power_mw) as total FROM construction_permits WHERE status IN ('approved', 'under_construction')")
            pipeline_mw = cursor.fetchone()['total'] or 0

            # FIX: datetime('now') → NOW() - INTERVAL
            cursor.execute('''
                SELECT title, companies FROM announcements
                WHERE discovered_at::timestamptz > NOW() - INTERVAL '7 days'
                ORDER BY discovered_at DESC LIMIT 5
            ''')
            top_news = cursor.fetchall()

        finally:
            try: conn.close()
            except Exception: pass

        digest = f"""📊 DC Hub Weekly Market Intelligence

This week in data center infrastructure:

📈 Key Metrics:
• {new_facilities:,} new facilities tracked
• {new_news:,} industry news articles
• {new_permits} new construction permits
• {pipeline_mw:,.0f} MW in development pipeline

🔥 Top Headlines:
"""
        for i, news in enumerate(top_news[:3], 1):
            digest += f"{i}. {news['title'][:80]}...\n"

        digest += f"""
🌍 Powered by DC Hub - tracking 10,000+ data centers worldwide

#DataCenter #Infrastructure #CloudComputing #DigitalInfrastructure #MarketIntelligence

📡 Real-time data at dchub.cloud"""

        return digest

    def post_to_linkedin(self, content):
        if not self.linkedin_token:
            from routes.li_token import li_access_token
            self.linkedin_token = li_access_token()
        if not self.linkedin_token:
            logger.warning("⚠️ LinkedIn token not configured")
            return None

        try:
            headers = {
                'Authorization': f'Bearer {self.linkedin_token}',
                'Content-Type': 'application/json',
                'X-Restli-Protocol-Version': '2.0.0'
            }

            response = requests.get(
                'https://api.linkedin.com/v2/userinfo',
                headers={'Authorization': f'Bearer {self.linkedin_token}'},
                timeout=10
            )

            if not response.ok:
                logger.error(f"Failed to get LinkedIn user: {response.status_code}")
                return None

            user_info = response.json()
            user_id = user_info.get('sub')

            post_data = {
                "author": f"urn:li:person:{user_id}",
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": content},
                        "shareMediaCategory": "NONE"
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
            }

            post_response = requests.post(
                'https://api.linkedin.com/v2/ugcPosts',
                headers=headers,
                json=post_data,
                timeout=30
            )

            if post_response.ok:
                logger.info("✅ Weekly LinkedIn digest posted")
                return post_response.json()
            else:
                logger.error(f"LinkedIn post failed: {post_response.status_code} - {post_response.text}")
                return None

        except Exception as e:
            logger.error(f"LinkedIn post error: {e}")
            return None

    def save_weekly_post(self, content, post_id=None):
        conn = get_db()
        try:
            cursor = conn.cursor()

            today = datetime.now()
            week_start = today - timedelta(days=today.weekday())
            week_end = week_start + timedelta(days=6)

            # FIX: ? → %s
            cursor.execute('''
                INSERT INTO linkedin_weekly_posts (week_start, week_end, content, posted_at, post_id)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            ''', (week_start.date(), week_end.date(), content, datetime.now(), post_id))

            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass

    def run_weekly_post(self):
        content = self.generate_weekly_digest()
        result = self.post_to_linkedin(content)
        post_id = result.get('id') if result else None
        self.save_weekly_post(content, post_id)
        return content


class InfrastructureDiscoveryEngine:
    """Main engine that runs all infrastructure discovery"""

    def __init__(self):
        init_infrastructure_tables()
        self.fiber = FiberRouteDiscovery()
        self.properties = DCPropertyDiscovery()
        self.permits = ConstructionPermitDiscovery()
        self.substations = SubstationDiscovery()
        self.gas = GasPipelineDiscovery()
        self.linkedin = WeeklyLinkedInSummary()
        self._scheduler_running = False

    def run_full_sync(self):
        logger.info("=" * 60)
        logger.info("🔄 INFRASTRUCTURE DISCOVERY SYNC")
        logger.info("=" * 60)

        start_time = datetime.now()

        fiber_new = self.fiber.sync()
        properties_new = self.properties.sync()
        permits_new = self.permits.sync()
        substations_new = self.substations.sync()
        gas_new = self.gas.sync()

        elapsed = (datetime.now() - start_time).total_seconds()
        total_new = fiber_new + properties_new + permits_new + substations_new + gas_new

        logger.info("=" * 60)
        logger.info(f"✅ INFRASTRUCTURE SYNC COMPLETE in {elapsed:.1f}s — {total_new} total new records")
        logger.info(f"   🔌 Fiber routes: {fiber_new} new")
        logger.info(f"   🏢 Properties: {properties_new} new")
        logger.info(f"   🏗️ Permits: {permits_new} new")
        logger.info(f"   ⚡ Substations: {substations_new} new")
        logger.info(f"   🔥 Gas pipelines: {gas_new} new")
        logger.info("=" * 60)

        return {
            "fiber_routes": fiber_new,
            "properties": properties_new,
            "permits": permits_new,
            "substations": substations_new,
            "gas_pipelines": gas_new,
            "total_new": total_new,
            "elapsed_seconds": elapsed
        }

    def get_status(self):
        conn = get_db()
        try:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) as cnt FROM fiber_routes")
            fiber_count = cursor.fetchone()['cnt']

            cursor.execute("SELECT COUNT(*) as cnt FROM dc_properties")
            properties_count = cursor.fetchone()['cnt']

            cursor.execute("SELECT COUNT(*) as cnt FROM construction_permits")
            permits_count = cursor.fetchone()['cnt']

            cursor.execute("SELECT COUNT(*) as cnt FROM substations")
            substations_count = cursor.fetchone()['cnt']

            try:
                cursor.execute("SELECT COUNT(*) as cnt FROM gas_pipelines")
                gas_count = cursor.fetchone()['cnt']
            except:
                gas_count = 0

            cursor.execute("SELECT COUNT(*) as cnt FROM linkedin_weekly_posts")
            weekly_posts = cursor.fetchone()['cnt']

        finally:
            try: conn.close()
            except Exception: pass

        return {
            "fiber_routes": fiber_count,
            "dc_properties": properties_count,
            "construction_permits": permits_count,
            "substations": substations_count,
            "gas_pipelines": gas_count,
            "weekly_linkedin_posts": weekly_posts,
            "scheduler_running": self._scheduler_running
        }

    def start_scheduler(self, interval_hours=6):
        if self._scheduler_running:
            return

        self._scheduler_running = True

        def scheduler_loop():
            time.sleep(120)
            while self._scheduler_running:
                try:
                    self.run_full_sync()
                except Exception as e:
                    logger.error(f"Infrastructure sync error: {e}")
                time.sleep(interval_hours * 3600)

        thread = Thread(target=scheduler_loop, daemon=True)
        thread.start()
        logger.info(f"🔄 Infrastructure Discovery Scheduler started (every {interval_hours} hours)")


def register_infrastructure_routes(app, start_scheduler=True):
    """Register Flask routes for infrastructure API"""
    from flask import Blueprint, jsonify, request

    bp = Blueprint('infrastructure', __name__)
    engine = InfrastructureDiscoveryEngine()

    if start_scheduler:
        engine.start_scheduler(interval_hours=6)

    @bp.route('/api/infrastructure/status')
    def infrastructure_status():
        return jsonify({"success": True, "data": engine.get_status()})

    @bp.route('/api/infrastructure/sync', methods=['POST'])
    def infrastructure_sync():
        # r66 security: triggers an EXPENSIVE full sync — was open, so anyone
        # could hammer it (DoS / worker-pool exhaustion → site flapping). The
        # real sync already runs via the in-process scheduler (crawler_scheduler
        # 'infrastructure_sync', every 6h); this HTTP trigger now requires an
        # internal or admin key. Fail closed.
        _ok = False
        try:
            from internal_auth import is_valid_internal_key
            _ok = is_valid_internal_key(request.headers.get('X-Internal-Key', ''))
        except Exception:
            _ok = False
        if not _ok:
            import os
            import hmac as _hmac
            _admin = os.environ.get('DCHUB_ADMIN_KEY', '') or ''
            _hdr = request.headers.get('X-Admin-Key', '') or ''
            _ok = bool(_admin) and _hmac.compare_digest(_hdr, _admin)
        if not _ok:
            return jsonify({"success": False, "error": "forbidden",
                            "message": "infrastructure sync requires an internal or admin key"}), 403
        result = engine.run_full_sync()
        return jsonify({"success": True, "data": result})

    @bp.route('/api/infrastructure/fiber-routes')
    def get_fiber_routes():
        conn = get_db()
        try:
            cursor = conn.cursor()
            # total real routes (exclude the brain's geometry-less news rows). RealDictCursor-
            # safe: fetchone() may be a dict OR a tuple depending on cursor_factory.
            # #1: only RENDERABLE interconnects (coordinates present) — ~half the fiber_routes
            # rows have no geometry (IX/facility entries) and just bloat the 4MB payload + a
            # misleading n/total. Filter to geometry-present so count==total==what the map draws.
            _where = "source IS DISTINCT FROM 'news_extraction' AND coordinates IS NOT NULL"
            _params = []
            # 2026-07-18: optional viewport filter bbox=minLng,minLat,maxLng,maxLat.
            # The Metro Links map layer viewport-drives — instead of a fixed 8000-row
            # slice of the full ~51k set, it loads the interconnects whose START point
            # is in the current view, so panning surfaces every route. Absent/malformed
            # bbox → unchanged legacy behavior (first 8000 by created_at).
            _bbox = request.args.get('bbox')
            if _bbox:
                try:
                    _mnx, _mny, _mxx, _mxy = [float(v) for v in _bbox.split(',')]
                    _where += " AND start_lng BETWEEN %s AND %s AND start_lat BETWEEN %s AND %s"
                    _params = [min(_mnx, _mxx), max(_mnx, _mxx), min(_mny, _mxy), max(_mny, _mxy)]
                except Exception:
                    _where = "source IS DISTINCT FROM 'news_extraction' AND coordinates IS NOT NULL"
                    _params = []
            total = 0
            try:
                cursor.execute("SELECT COUNT(*) AS total FROM fiber_routes WHERE " + _where, _params)
                _r = cursor.fetchone()
                total = (_r.get('total') if hasattr(_r, 'get') else _r[0]) if _r else 0
            except Exception:
                total = 0
            # cap 100 -> 8000 so the Metro Links map layer shows the FULL real set, not a sample.
            cursor.execute("SELECT * FROM fiber_routes WHERE " + _where + " ORDER BY created_at DESC LIMIT 8000", _params)
            routes = [dict(row) for row in cursor.fetchall()]
        finally:
            try: conn.close()
            except Exception: pass
        return jsonify({"success": True, "data": routes, "count": len(routes), "total": total})

    @bp.route('/api/infrastructure/properties')
    def get_properties():
        conn = get_db()
        try:
            cursor = conn.cursor()
            status = request.args.get('status', 'available')
            # FIX: ? → %s
            cursor.execute("SELECT * FROM dc_properties WHERE status = %s ORDER BY created_at DESC LIMIT 100", (status,))
            properties = [dict(row) for row in cursor.fetchall()]
        finally:
            try: conn.close()
            except Exception: pass
        return jsonify({"success": True, "data": properties, "count": len(properties)})

    @bp.route('/api/infrastructure/permits')
    def get_permits():
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM construction_permits ORDER BY created_at DESC LIMIT 100")
            permits = [dict(row) for row in cursor.fetchall()]
        finally:
            try: conn.close()
            except Exception: pass
        return jsonify({"success": True, "data": permits, "count": len(permits)})

    @bp.route('/api/infrastructure/substations')
    def get_substations():
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM substations WHERE voltage_kv > 69 OR voltage_kv IS NULL OR voltage_kv = 0 ORDER BY voltage_kv DESC LIMIT 100")
            substations = [dict(row) for row in cursor.fetchall()]
        finally:
            try: conn.close()
            except Exception: pass
        return jsonify({"success": True, "data": substations, "count": len(substations)})

    @bp.route('/api/infrastructure/gas-pipelines')
    def get_gas_pipelines():
        conn = get_db()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT * FROM gas_pipelines ORDER BY created_at DESC LIMIT 200")
                pipelines = [dict(row) for row in cursor.fetchall()]
            except:
                pipelines = []
        finally:
            try: conn.close()
            except Exception: pass
        return jsonify({"success": True, "data": pipelines, "count": len(pipelines)})

    @bp.route('/api/infrastructure/weekly-digest')
    def get_weekly_digest():
        content = engine.linkedin.generate_weekly_digest()
        return jsonify({"success": True, "content": content})

    @bp.route('/api/infrastructure/weekly-digest/post', methods=['POST'])
    def post_weekly_digest():
        from internal_auth import require_internal_or_admin
        if not require_internal_or_admin(request):
            return jsonify({"success": False, "error": "unauthorized"}), 401
        content = engine.linkedin.generate_weekly_digest()
        result = engine.linkedin.post_to_linkedin(content)
        engine.linkedin.save_weekly_post(content, result.get('id') if result else None)
        return jsonify({"success": result is not None, "content": content, "posted": result is not None})

    app.register_blueprint(bp)

    logger.info("🏗️ Infrastructure Discovery API registered:")
    logger.info("   GET  /api/infrastructure/status")
    logger.info("   POST /api/infrastructure/sync")
    logger.info("   GET  /api/infrastructure/fiber-routes")
    logger.info("   GET  /api/infrastructure/properties")
    logger.info("   GET  /api/infrastructure/permits")
    logger.info("   GET  /api/infrastructure/substations")
    logger.info("   GET  /api/infrastructure/gas-pipelines")
    logger.info("   GET  /api/infrastructure/weekly-digest")
    logger.info("   POST /api/infrastructure/weekly-digest/post")

    return engine