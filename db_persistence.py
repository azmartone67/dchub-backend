import os
import sqlite3
import logging
import time
import threading
import json

logger = logging.getLogger(__name__)

DB_PATH = "dc_nexus.db"

def _get_pg_url():
    import re
    neon = os.environ.get('NEON_DATABASE_URL', '')
    if neon:
        neon = neon.strip()
        neon = re.sub(r"^psql\s+", "", neon)
        neon = neon.strip("'\"")
        neon = re.sub(r'[&?]channel_binding=[^&]*', '', neon)
        return neon
    return os.environ.get("NEON_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")

PG_URL = _get_pg_url()

_pg_available = False
_pg_pool = None

CRITICAL_TABLES = {
    'users': {
        'pk': 'id',
        'columns': [
            ('id', 'TEXT PRIMARY KEY'),
            ('email', 'TEXT UNIQUE NOT NULL'),
            ('password_hash', 'TEXT NOT NULL'),
            ('name', 'TEXT'),
            ('company', 'TEXT'),
            ('role', 'TEXT DEFAULT \'free\''),
            ('plan', 'TEXT DEFAULT \'free\''),
            ('api_calls_today', 'INTEGER DEFAULT 0'),
            ('api_calls_total', 'INTEGER DEFAULT 0'),
            ('saved_searches', 'TEXT'),
            ('saved_markets', 'TEXT'),
            ('preferences', 'TEXT'),
            ('created_at', 'TEXT'),
            ('last_login', 'TEXT'),
            ('reset_token', 'TEXT'),
            ('reset_expires', 'TEXT'),
            ('stripe_customer_id', 'TEXT'),
            ('subscription_status', 'TEXT'),
        ]
    },
    'api_keys': {
        'pk': 'id',
        'columns': [
            ('id', 'SERIAL PRIMARY KEY'),
            ('user_id', 'TEXT NOT NULL'),
            ('key_hash', 'TEXT NOT NULL UNIQUE'),
            ('key_prefix', 'TEXT NOT NULL'),
            ('name', 'TEXT'),
            ('permissions', 'TEXT DEFAULT \'[]\''),
            ('rate_limit_tier', 'TEXT DEFAULT \'free\''),
            ('is_active', 'INTEGER DEFAULT 1'),
            ('last_used_at', 'TEXT'),
            ('created_at', 'TEXT'),
            ('expires_at', 'TEXT'),
            ('usage_count', 'INTEGER DEFAULT 0'),
            ('plan', 'TEXT DEFAULT \'free\''),
            ('last_reset_date', 'TEXT'),
            ('calls_today', 'INTEGER DEFAULT 0'),
            ('calls_total', 'INTEGER DEFAULT 0'),
            ('last_used', 'TEXT'),
        ]
    },
    'deals': {
        'pk': 'id',
        'columns': [
            ('id', 'TEXT PRIMARY KEY'),
            ('date', 'TEXT'),
            ('year', 'INTEGER'),
            ('buyer', 'TEXT'),
            ('seller', 'TEXT'),
            ('value', 'REAL'),
            ('mw', 'REAL'),
            ('type', 'TEXT'),
            ('region', 'TEXT'),
            ('market', 'TEXT'),
            ('source_url', 'TEXT'),
            ('created_at', 'TEXT'),
            ('verified', 'INTEGER DEFAULT 0'),
            ('status', 'TEXT'),
            ('notes', 'TEXT'),
        ]
    },
    'ecosystem_companies': {
        'pk': 'id',
        'columns': [
            ('id', 'TEXT PRIMARY KEY'),
            ('name', 'TEXT'),
            ('description', 'TEXT'),
            ('category', 'TEXT'),
            ('subcategory', 'TEXT'),
            ('website', 'TEXT'),
            ('logo_url', 'TEXT'),
            ('headquarters', 'TEXT'),
            ('markets', 'TEXT'),
            ('services', 'TEXT'),
            ('contact_email', 'TEXT'),
            ('linkedin_url', 'TEXT'),
            ('twitter_url', 'TEXT'),
            ('founded_year', 'INTEGER'),
            ('employee_count', 'TEXT'),
            ('facility_count', 'INTEGER'),
            ('total_mw', 'REAL'),
            ('verified', 'INTEGER DEFAULT 0'),
            ('featured', 'INTEGER DEFAULT 0'),
            ('ai_enriched', 'INTEGER DEFAULT 0'),
            ('ai_summary', 'TEXT'),
            ('ai_keywords', 'TEXT'),
            ('submitted_by', 'TEXT'),
            ('submitted_at', 'TEXT'),
            ('approved_at', 'TEXT'),
            ('updated_at', 'TEXT'),
            ('status', 'TEXT'),
        ]
    },
    'capacity_pipeline': {
        'pk': 'id',
        'columns': [
            ('id', 'INTEGER PRIMARY KEY'),
            ('operator', 'TEXT'),
            ('market', 'TEXT'),
            ('region', 'TEXT'),
            ('capacity_mw', 'REAL'),
            ('phase', 'TEXT'),
            ('status', 'TEXT'),
            ('announcement_date', 'TEXT'),
            ('completion_date', 'TEXT'),
            ('source', 'TEXT'),
            ('source_url', 'TEXT'),
            ('notes', 'TEXT'),
            ('created_at', 'TEXT'),
            ('confidence_score', 'INTEGER'),
            ('confidence_label', 'TEXT'),
        ]
    },
}

AI_TRACKING_TABLES = {
    'ai_cumulative': {
        'pk': 'platform',
        'db_path': 'ai_tracking.db',
        'columns': [
            ('platform', 'TEXT PRIMARY KEY'),
            ('total_requests', 'INTEGER DEFAULT 0'),
            ('first_seen', 'TEXT'),
            ('last_seen', 'TEXT'),
        ]
    },
    'ai_daily_stats': {
        'pk': 'date_platform',
        'db_path': 'ai_tracking.db',
        'columns': [
            ('date', 'TEXT'),
            ('platform', 'TEXT'),
            ('request_count', 'INTEGER DEFAULT 0'),
        ]
    },
}


def _get_pg():
    global _pg_pool, _pg_available
    if not PG_URL:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(PG_URL)
        conn.autocommit = False
        return conn
    except ImportError:
        try:
            import psycopg2cffi as psycopg2
            conn = psycopg2.connect(PG_URL)
            conn.autocommit = False
            return conn
        except ImportError:
            logger.warning("No psycopg2 driver available")
            return None
    except Exception as e:
        logger.error(f"PostgreSQL connection failed: {e}")
        return None


def _sqlite_to_pg_type(col_def):
    col_def = col_def.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY')
    col_def = col_def.replace('REAL', 'DOUBLE PRECISION')
    return col_def


def init_pg_tables():
    global _pg_available
    conn = _get_pg()
    if not conn:
        logger.warning("PostgreSQL not available - persistence disabled")
        _pg_available = False
        return False

    try:
        cur = conn.cursor()
        all_tables = dict(CRITICAL_TABLES)
        for table_name, table_def in all_tables.items():
            cols = []
            for col_name, col_type in table_def['columns']:
                pg_type = _sqlite_to_pg_type(col_type)
                cols.append(f"{col_name} {pg_type}")

            create_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(cols)})"
            try:
                cur.execute(create_sql)
                conn.commit()
            except Exception as te:
                conn.rollback()
                logger.debug(f"Table {table_name} already exists or creation skipped: {te}")

        for table_name, table_def in AI_TRACKING_TABLES.items():
            cols = []
            for col_name, col_type in table_def['columns']:
                pg_type = _sqlite_to_pg_type(col_type)
                cols.append(f"{col_name} {pg_type}")
            create_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(cols)})"
            if table_name == 'ai_daily_stats':
                create_sql = f"""CREATE TABLE IF NOT EXISTS {table_name} (
                    date TEXT, platform TEXT, request_count INTEGER DEFAULT 0,
                    PRIMARY KEY (date, platform))"""
            try:
                cur.execute(create_sql)
                conn.commit()
            except Exception as te:
                conn.rollback()
                logger.debug(f"Table {table_name} already exists or creation skipped: {te}")

        _pg_available = True
        logger.info("PostgreSQL persistence tables initialized")
        return True
    except Exception as e:
        logger.error(f"Failed to init PG tables: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        _pg_available = False
        return False
    finally:
        conn.close()


def sync_sqlite_to_pg(table_name=None):
    if not _pg_available:
        return False

    tables = [table_name] if table_name else list(CRITICAL_TABLES.keys())
    pg_conn = _get_pg()
    if not pg_conn:
        return False

    try:
        sq_conn = sqlite3.connect(DB_PATH, timeout=60)
        sq_conn.execute("PRAGMA busy_timeout = 60000")
        sq_conn.execute("PRAGMA journal_mode = WAL")
        pg_cur = pg_conn.cursor()

        for tname in tables:
            tdef = CRITICAL_TABLES.get(tname)
            if not tdef:
                continue

            col_names = [c[0] for c in tdef['columns']]
            pk = tdef['pk']

            rows = sq_conn.execute(f"SELECT {', '.join(col_names)} FROM {tname}").fetchall()
            if not rows:
                continue

            for row in rows:
                row_dict = dict(zip(col_names, row))

                non_pk_cols = [c for c in col_names if c != pk]

                if pk == 'id' and tname == 'api_keys':
                    pg_cur.execute(
                        f"SELECT 1 FROM {tname} WHERE key_hash = %s",
                        (row_dict.get('key_hash'),)
                    )
                    exists = pg_cur.fetchone()
                    if exists:
                        set_clause = ', '.join([f"{c} = %s" for c in non_pk_cols if c != 'key_hash'])
                        vals = [row_dict[c] for c in non_pk_cols if c != 'key_hash']
                        vals.append(row_dict['key_hash'])
                        pg_cur.execute(
                            f"UPDATE {tname} SET {set_clause} WHERE key_hash = %s",
                            vals
                        )
                    else:
                        insert_cols = [c for c in col_names if c != 'id']
                        placeholders = ', '.join(['%s'] * len(insert_cols))
                        vals = [row_dict[c] for c in insert_cols]
                        pg_cur.execute(
                            f"INSERT INTO {tname} ({', '.join(insert_cols)}) VALUES ({placeholders})",
                            vals
                        )
                else:
                    placeholders = ', '.join(['%s'] * len(col_names))
                    set_clause = ', '.join([f"{c} = EXCLUDED.{c}" for c in non_pk_cols])
                    pg_cur.execute(
                        f"INSERT INTO {tname} ({', '.join(col_names)}) VALUES ({placeholders}) "
                        f"ON CONFLICT ({pk}) DO UPDATE SET {set_clause}",
                        [row_dict[c] for c in col_names]
                    )

        pg_conn.commit()
        logger.info(f"Synced {len(tables)} table(s) to PostgreSQL: {', '.join(tables)}")
        return True
    except Exception as e:
        logger.error(f"SQLite->PG sync failed: {e}")
        pg_conn.rollback()
        return False
    finally:
        sq_conn.close()
        pg_conn.close()


def restore_pg_to_sqlite(max_retries=3, tables_to_restore=None):
    if not _pg_available:
        return False

    tables_map = {k: v for k, v in CRITICAL_TABLES.items() if tables_to_restore is None or k in tables_to_restore}

    for attempt in range(max_retries):
        pg_conn = _get_pg()
        if not pg_conn:
            return False

        sq_conn = None
        try:
            sq_conn = sqlite3.connect(DB_PATH, timeout=60)
            sq_conn.execute("PRAGMA busy_timeout = 60000")
            sq_conn.execute("PRAGMA journal_mode = WAL")
            pg_cur = pg_conn.cursor()

            restored_counts = {}

            for tname, tdef in tables_map.items():
                col_names = [c[0] for c in tdef['columns']]
                pk = tdef['pk']

                try:
                    pg_cur.execute(f"SELECT COUNT(*) FROM {tname}")
                    pg_count = pg_cur.fetchone()[0]
                except Exception:
                    pg_conn.rollback()
                    continue

                if pg_count == 0:
                    continue

                select_cols = [c for c in col_names if c != 'id' or tname != 'api_keys']
                pg_cur.execute(f"SELECT {', '.join(select_cols)} FROM {tname}")
                rows = pg_cur.fetchall()

                for row in rows:
                    row_dict = dict(zip(select_cols, row))

                    if tname == 'api_keys':
                        existing = sq_conn.execute(
                            f"SELECT 1 FROM {tname} WHERE key_hash = ?",
                            (row_dict.get('key_hash'),)
                        ).fetchone()
                        if existing:
                            continue
                        insert_cols = [c for c in select_cols]
                        placeholders = ', '.join(['?'] * len(insert_cols))
                        sq_conn.execute(
                            f"INSERT OR IGNORE INTO {tname} ({', '.join(insert_cols)}) VALUES ({placeholders})",
                            [row_dict[c] for c in insert_cols]
                        )
                    else:
                        insert_cols = select_cols
                        placeholders = ', '.join(['?'] * len(insert_cols))
                        sq_conn.execute(
                            f"INSERT OR REPLACE INTO {tname} ({', '.join(insert_cols)}) VALUES ({placeholders})",
                            [row_dict[c] for c in insert_cols]
                        )

                sq_conn.commit()
                restored_counts[tname] = len(rows)
                logger.info(f"  {tname}: restored {len(rows)} rows from PostgreSQL")

            if restored_counts:
                logger.info(f"Restored from PostgreSQL: {restored_counts}")
            else:
                logger.info("No restoration needed - SQLite already up to date")

            return True
        except Exception as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                logger.warning(f"PG->SQLite restore attempt {attempt+1} got lock, retrying in {2*(attempt+1)}s...")
                time.sleep(2 * (attempt + 1))
                continue
            logger.error(f"PG->SQLite restore failed: {e}")
            return False
        finally:
            if sq_conn:
                try:
                    sq_conn.close()
                except Exception:
                    pass
            try:
                pg_conn.close()
            except Exception:
                pass

    return False


_sync_lock = threading.Lock()
_last_sync = 0
SYNC_INTERVAL = 180


def sync_ai_tracking_to_pg():
    if not _pg_available:
        return False
    ai_db = 'ai_tracking.db'
    if not os.path.exists(ai_db):
        return False
    pg_conn = _get_pg()
    if not pg_conn:
        return False
    try:
        sq_conn = sqlite3.connect(ai_db, timeout=30)
        pg_cur = pg_conn.cursor()
        rows = sq_conn.execute("SELECT platform, total_requests, first_seen, last_seen FROM ai_cumulative").fetchall()
        for r in rows:
            pg_cur.execute(
                """INSERT INTO ai_cumulative (platform, total_requests, first_seen, last_seen)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (platform) DO UPDATE SET
                     total_requests = GREATEST(ai_cumulative.total_requests, EXCLUDED.total_requests),
                     last_seen = GREATEST(ai_cumulative.last_seen, EXCLUDED.last_seen)""",
                r)
        rows = sq_conn.execute("SELECT date, platform, request_count FROM ai_daily_stats").fetchall()
        for r in rows:
            pg_cur.execute(
                """INSERT INTO ai_daily_stats (date, platform, request_count)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (date, platform) DO UPDATE SET
                     request_count = GREATEST(ai_daily_stats.request_count, EXCLUDED.request_count)""",
                r)
        pg_conn.commit()
        logger.info(f"AI tracking synced to PostgreSQL")
        return True
    except Exception as e:
        logger.error(f"AI tracking sync failed: {e}")
        try:
            pg_conn.rollback()
        except Exception:
            pass
        return False
    finally:
        sq_conn.close()
        pg_conn.close()


def restore_ai_tracking_from_pg():
    if not _pg_available:
        return False
    pg_conn = _get_pg()
    if not pg_conn:
        return False
    try:
        ai_db = 'ai_tracking.db'
        sq_conn = sqlite3.connect(ai_db, timeout=30)
        sq_conn.execute("CREATE TABLE IF NOT EXISTS ai_cumulative (platform TEXT PRIMARY KEY, total_requests INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT)")
        sq_conn.execute("CREATE TABLE IF NOT EXISTS ai_daily_stats (date TEXT, platform TEXT, request_count INTEGER DEFAULT 0, PRIMARY KEY (date, platform))")
        pg_cur = pg_conn.cursor()
        try:
            pg_cur.execute("SELECT COUNT(*) FROM ai_cumulative")
            if pg_cur.fetchone()[0] > 0:
                pg_cur.execute("SELECT platform, total_requests, first_seen, last_seen FROM ai_cumulative")
                for r in pg_cur.fetchall():
                    sq_conn.execute("INSERT OR REPLACE INTO ai_cumulative (platform, total_requests, first_seen, last_seen) VALUES (?,?,?,?)", r)
        except Exception:
            pg_conn.rollback()
        try:
            pg_cur.execute("SELECT COUNT(*) FROM ai_daily_stats")
            if pg_cur.fetchone()[0] > 0:
                pg_cur.execute("SELECT date, platform, request_count FROM ai_daily_stats")
                for r in pg_cur.fetchall():
                    sq_conn.execute("INSERT OR REPLACE INTO ai_daily_stats (date, platform, request_count) VALUES (?,?,?)", r)
        except Exception:
            pg_conn.rollback()
        sq_conn.commit()
        logger.info("AI tracking restored from PostgreSQL")
        return True
    except Exception as e:
        logger.error(f"AI tracking restore failed: {e}")
        return False
    finally:
        try:
            sq_conn.close()
        except Exception:
            pass
        pg_conn.close()


def periodic_sync():
    global _last_sync
    if not _pg_available:
        return

    with _sync_lock:
        now = time.time()
        if now - _last_sync < SYNC_INTERVAL:
            return
        _last_sync = now

    try:
        restore_pg_to_sqlite(tables_to_restore=['users', 'api_keys'])
    except Exception as e:
        logger.error(f"Periodic PG->SQLite auth sync failed: {e}")

    for tbl in ['users', 'deals', 'ecosystem_companies', 'capacity_pipeline']:
        try:
            sync_sqlite_to_pg(table_name=tbl)
        except Exception as e:
            logger.error(f"Periodic SQLite->PG {tbl} sync failed: {e}")

    try:
        sync_ai_tracking_to_pg()
    except Exception as e:
        logger.error(f"Periodic AI tracking sync failed: {e}")


def sync_on_write(table_name):
    if not _pg_available:
        return
    try:
        threading.Thread(
            target=sync_sqlite_to_pg,
            args=(table_name,),
            daemon=True
        ).start()
    except Exception as e:
        logger.error(f"Write-triggered sync failed: {e}")


import fcntl


def _deferred_data_restore():
    time.sleep(10)
    try:
        logger.info("Starting deferred data restore from PostgreSQL...")
        restore_pg_to_sqlite(tables_to_restore=['deals', 'ecosystem_companies', 'capacity_pipeline'])
        restore_ai_tracking_from_pg()
        logger.info("Deferred data restore complete")
    except Exception as e:
        logger.error(f"Deferred data restore failed: {e}")


def _ensure_missing_users():
    MISSING_USERS = [
        {
            'id': 'be235c52648431f5a3599d1d',
            'email': 'freetest@dchub.cloud',
            'password_hash': 'b82de2b092c51cd320ddf1d4f589bc33:8ed2fd31f475a3c8d3e8a9398f301d6c2940f2f827d093bc2dda66a5b4c2d603',
            'name': 'free test',
            'company': 'Free Test Account',
            'role': 'free',
            'plan': 'free',
            'created_at': '2026-02-14T10:30:27.366872',
        },
    ]
    pg_conn = _get_pg()
    if not pg_conn:
        return
    try:
        cur = pg_conn.cursor()
        for u in MISSING_USERS:
            cur.execute("SELECT 1 FROM users WHERE id = %s", (u['id'],))
            if not cur.fetchone():
                cur.execute(
                    """INSERT INTO users (id, email, password_hash, name, company, role, plan, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                    (u['id'], u['email'], u['password_hash'], u['name'], u['company'], u['role'], u['plan'], u['created_at']))
                logger.info(f"Inserted missing user {u['email']} into PostgreSQL")
        pg_conn.commit()
    except Exception as e:
        logger.error(f"Missing user insert failed: {e}")
        try:
            pg_conn.rollback()
        except Exception:
            pass
    finally:
        pg_conn.close()


def startup_restore_and_sync():
    logger.info("=== Database Persistence Layer Starting ===")

    if not PG_URL:
        logger.warning("DATABASE_URL not set - persistence disabled")
        return False

    if not init_pg_tables():
        return False

    lock_file = '/tmp/dchub_persistence_restore.lock'
    try:
        fd = open(lock_file, 'w')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _ensure_missing_users()
        restore_pg_to_sqlite(tables_to_restore=['users', 'api_keys'])
        for tbl in ['users', 'deals', 'ecosystem_companies', 'capacity_pipeline']:
            sync_sqlite_to_pg(table_name=tbl)
        sync_ai_tracking_to_pg()
        threading.Thread(target=_deferred_data_restore, daemon=True).start()
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    except (IOError, OSError):
        logger.info("Another worker handling restore/sync - skipping")

    logger.info("=== Database Persistence Layer Ready ===")
    return True
