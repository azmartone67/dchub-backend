import os
import time
import logging
import random
import re

logger = logging.getLogger(__name__)

DB_PATH = 'dc_nexus.db'

PG_READ_ENABLED = True

SKIP_DDL = os.environ.get('SKIP_DDL', '1') == '1'

_DDL_PREFIXES = ('CREATE TABLE', 'CREATE INDEX', 'ALTER TABLE', 'CREATE UNIQUE INDEX')

def _is_ddl(sql):
    if not SKIP_DDL:
        return False
    stripped = sql.strip().upper()
    return any(stripped.startswith(p) for p in _DDL_PREFIXES)

SQLITE_TO_PG_FUNC = {
    "datetime('now', '-7 days')": "(NOW() - INTERVAL '7 days')",
    "datetime('now', '-30 days')": "(NOW() - INTERVAL '30 days')",
    "NOW() - INTERVAL '1 days'": "(NOW() - INTERVAL '1 day')",
    "datetime('now', '-24 hours')": "(NOW() - INTERVAL '24 hours')",
    "NOW() - INTERVAL '1 hours'": "(NOW() - INTERVAL '1 hour')",
    "datetime('now', '-6 hours')": "(NOW() - INTERVAL '6 hours')",
    "datetime('now', '-12 hours')": "(NOW() - INTERVAL '12 hours')",
    "datetime('now', '-48 hours')": "(NOW() - INTERVAL '48 hours')",
    "datetime('now', '-90 days')": "(NOW() - INTERVAL '90 days')",
    "datetime('now', '-365 days')": "(NOW() - INTERVAL '365 days')",
    "NOW()": "NOW()",
}


def _translate_sql(sql):
    out = sql.strip()
    if out.upper().startswith('PRAGMA'):
        m = re.match(r"PRAGMA\s+table_info\s*\(\s*\[?(\w+)\]?\s*\)", out, re.IGNORECASE)
        if m:
            tbl = m.group(1)
            return (f"SELECT ordinal_position AS cid, column_name AS name, "
                    f"data_type AS type, CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END AS notnull, "
                    f"column_default AS dflt_value, 0 AS pk "
                    f"FROM information_schema.columns WHERE table_name='{tbl}' "
                    f"ORDER BY ordinal_position"), 0
        return 'SELECT 1 WHERE false', 0
    for old, new in SQLITE_TO_PG_FUNC.items():
        out = out.replace(old, new)
    out = re.sub(r'\bLIKE\b', 'ILIKE', out)
    out = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b', 'SERIAL PRIMARY KEY', out, flags=re.IGNORECASE)
    out = re.sub(r'\bAUTOINCREMENT\b', '', out, flags=re.IGNORECASE)
    _has_or_ignore = bool(re.search(r'\bINSERT\s+OR\s+IGNORE\b', out, flags=re.IGNORECASE))
    out = re.sub(r'\bINSERT\s+OR\s+IGNORE\b', 'INSERT', out, flags=re.IGNORECASE)
    out = re.sub(r'\bINSERT\s+OR\s+REPLACE\b', 'INSERT', out, flags=re.IGNORECASE)
    n = 0
    result = []
    i = 0
    while i < len(out):
        ch = out[i]
        if ch == '?':
            n += 1
            result.append('%s')
        elif ch == '%':
            if i + 1 < len(out) and out[i + 1] == 's':
                result.append('%s')
                i += 2
                n += 1
                continue
            result.append('%%')
        else:
            result.append(ch)
        i += 1
    out = ''.join(result)
    _ts_cmp = r'(>=|<=|!=|<>|>|<)'
    for col in ('first_seen', 'discovered_at', 'published_date', 'last_success',
                'last_failure', 'last_tested', 'detected_at', 'created_at',
                'updated_at', 'last_seen', 'timestamp', 'scheduled_for',
                'last_handshake', 'last_health', 'last_ping', 'last_checked'):
        out = re.sub(r"\b" + col + r"\s*" + _ts_cmp, col + r"::timestamptz \1", out)
    out = re.sub(r'\bBOOLEAN\s+DEFAULT\s+1\b', 'BOOLEAN DEFAULT TRUE', out, flags=re.IGNORECASE)
    out = re.sub(r'\bBOOLEAN\s+DEFAULT\s+0\b', 'BOOLEAN DEFAULT FALSE', out, flags=re.IGNORECASE)
    out = re.sub(r'\bDATETIME\b(?!\s*\()', 'TIMESTAMP', out, flags=re.IGNORECASE)
    # ADD the modifier, never subtract it. SQLite's datetime('now', M) applies
    # M with its own sign, so '-3 days' already means "3 days ago". Subtracting
    # it — NOW() - INTERVAL '-3 days' — lands 3 days in the FUTURE, and the
    # query then succeeds and returns almost nothing. That is why this went
    # unnoticed: the eight spellings in SQLITE_TO_PG_FUNC above are rewritten
    # sign-stripped and correct, so only intervals absent from that dict
    # inverted, and they inverted silently. Measured 2026-08-17 against the
    # replica: '-3 days' over `announcements` returned 1 row where the correct
    # window returns 201, and '-14 days' returned 1 where the answer is 2,225.
    out = re.sub(r"\bdatetime\s*\(\s*'now'\s*,\s*'([^']+)'\s*\)", r"(NOW() + INTERVAL '\1')", out, flags=re.IGNORECASE)
    out = re.sub(r"\bdatetime\s*\(\s*'now'\s*\)", "NOW()", out, flags=re.IGNORECASE)
    if _has_or_ignore:
        stripped = out.rstrip().rstrip(';')
        if 'ON CONFLICT' not in stripped.upper():
            out = stripped + ' ON CONFLICT DO NOTHING'
    return out, n


class PGRowProxy:
    __slots__ = ('_data', '_keys')
    def __init__(self, data, keys):
        self._data = data
        self._keys = keys
    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[key]
        if isinstance(key, str):
            try:
                idx = self._keys.index(key)
                return self._data[idx]
            except ValueError:
                raise KeyError(key)
        raise TypeError(f"Invalid key type: {type(key)}")
    def keys(self):
        return list(self._keys)
    def values(self):
        return list(self._data)
    def items(self):
        return list(zip(self._keys, self._data))
    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default
    def __iter__(self):
        return iter(self._data)
    def __len__(self):
        return len(self._data)
    def __contains__(self, key):
        return key in self._keys


def _is_connectivity_error(e):
    err_str = str(e).lower()
    connectivity_patterns = [
        'connection refused', 'connection reset', 'connection timed out',
        'server closed the connection', 'could not connect', 'broken pipe',
        'network is unreachable', 'no route to host', 'connection terminated',
        'ssl connection has been closed',
        'remaining connection slots are reserved', 'too many connections',
        'the database system is shutting down', 'the database system is starting up',
        # r-breaker-neon (2026-08-21, incident 02:00-02:16Z): Neon's OWN refusal
        # dialect. Every pattern above is generic-libpq/vanilla-Postgres phrasing;
        # NONE of them matched the three strings Neon actually returned while it
        # was refusing connections for 16 minutes, so _record_circuit_failure()
        # was never called, the breaker never opened (/api/health/db reported
        # circuit_trips:0 from a worker booted 00:53, i.e. one that lived through
        # the whole outage), and all 32 gthreads x 2 replicas kept opening fresh
        # connects into a limiter whose complaint was literally "too many ongoing
        # connection attempts". Requests stacked to 965s. The misses were near:
        #   Neon "Couldn't connect to compute node"      vs 'could not connect'  (contraction)
        #   Neon "Too many database CONNECTION ATTEMPTS" vs 'too many connections' (3 words inserted)
        #   libpq "timeout expired"                      vs 'connection timed out' (different phrasing)
        # Only strings OBSERVED in the 02:00 logs are added — no guessed variants.
        # Every caller of this fn does exactly one thing with a True (record a
        # circuit failure), so the blast radius is "the breaker can now see Neon".
        "couldn't connect",
        'failed to acquire permit',
        'too many database connection attempts',
        'timeout expired',
    ]
    return any(p in err_str for p in connectivity_patterns)


class PGCursorWrapper:
    def __init__(self, pg_cursor):
        self._cur = pg_cursor
        self._description = None
        self._lastrowid = None

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._lastrowid

    def execute(self, sql, params=None):
        if _is_ddl(sql):
            return self

        translated, param_count = _translate_sql(sql)
        if params:
            if isinstance(params, (list, tuple)):
                pg_params = tuple(params)
            else:
                pg_params = (params,)
        else:
            pg_params = None

        is_insert = translated.lstrip().upper().startswith('INSERT')
        has_returning = 'RETURNING' in translated.upper() if is_insert else False

        try:
            self._cur.execute(translated, pg_params)
            self._description = self._cur.description
            if is_insert and not has_returning:
                try:
                    self._cur.execute("SELECT lastval()")
                    row = self._cur.fetchone()
                    self._lastrowid = row[0] if row else None
                except Exception:
                    self._lastrowid = None
        except Exception as e:
            logger.warning(f"PG query failed, sql snippet: {translated[:120]}... error: {e}")
            try:
                self._cur.connection.rollback()
            except Exception:
                pass
            if _is_connectivity_error(e):
                try:
                    from main import _record_circuit_failure
                    _record_circuit_failure()
                except Exception:
                    pass
            raise

    def executemany(self, sql, rows):
        translated, _ = _translate_sql(sql)
        try:
            for row in rows:
                if isinstance(row, (list, tuple)):
                    pg_params = tuple(row)
                else:
                    pg_params = (row,)
                self._cur.execute(translated, pg_params)
        except Exception as e:
            logger.warning(f"PG executemany failed, sql snippet: {translated[:120]}... error: {e}")
            try:
                self._cur.connection.rollback()
            except Exception:
                pass
            if _is_connectivity_error(e):
                try:
                    from main import _record_circuit_failure
                    _record_circuit_failure()
                except Exception:
                    pass
            raise

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        # If cursor_factory already returned dict-like row, pass through (fixes cursor_factory=RealDictCursor)
        if hasattr(row, 'keys') or isinstance(row, dict):
            return row
        if self._cur.description:
            keys = [d[0] for d in self._cur.description]
            return PGRowProxy(row, keys)
        return row

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        # If cursor_factory already returned dict-like rows, pass through (fixes cursor_factory=RealDictCursor)
        if rows and (hasattr(rows[0], 'keys') or isinstance(rows[0], dict)):
            return rows
        if self._cur.description:
            keys = [d[0] for d in self._cur.description]
            return [PGRowProxy(r, keys) for r in rows]
        return rows

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class PGConnectionWrapper:
    def __init__(self, pg_conn, return_func=None):
        self._conn = pg_conn
        self._return_func = return_func

    def cursor(self, *args, **kwargs):
        return PGCursorWrapper(self._conn.cursor(*args, **kwargs))

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql, rows):
        cur = self.cursor()
        cur.executemany(sql, rows)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        # 2026-06-12: idempotent. A second close() used to rollback + putconn a
        # connection ALREADY returned to the pool — racing whichever thread had
        # checked it out next (silently aborting its in-flight transaction, or
        # putconn raising and the fallback hard-closing a pooled connection).
        # Exactly-once semantics enforced here makes `with safe_db() as conn:`
        # blocks that also call conn.close() internally safe by construction.
        if getattr(self, "_lp_closed", False):
            return
        self._lp_closed = True
        try:
            self._conn.rollback()
        except Exception:
            pass
        if self._return_func:
            try:
                self._return_func(self._conn)
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
        else:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        self.close()
        return False

    def executescript(self, script):
        statements = [s.strip() for s in script.split(';') if s.strip()]
        cur = self.cursor()
        for stmt in statements:
            try:
                cur.execute(stmt)
            except Exception as e:
                logger.warning(f"executescript statement failed: {e}")
        self.commit()
        return cur

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, val):
        pass


def cap_lock_wait(conn, ms=3000):
    """Cap how long a connection WAITS for a lock (Postgres SET lock_timeout) so
    boot-time schema migrations FAIL FAST under contention — e.g. the still-serving
    old replica holds table locks during a deploy — instead of hanging until
    statement_timeout (~15s) per ALTER and blowing the 5-min healthcheck window (the
    2026-07-01 failed deploy). Session-level + isolated to THIS raw migration
    connection (NOT the pooled runtime path), so runtime/brain/bulk-load lock
    behavior is unchanged. Idempotent, never raises. Returns conn."""
    try:
        c = conn.cursor()
        c.execute(f"SET lock_timeout = '{int(ms)}ms'")
        try:
            conn.commit()
        except Exception:
            pass
        c.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return conn


def _get_pg_connection():
    try:
        from main import get_pg_connection, return_pg_connection
        conn = get_pg_connection(retries=2)
        try:
            c = conn.cursor()
            c.execute("SET statement_timeout = 15000")
            conn.commit()
            c.close()
        except Exception:
            pass
        return PGConnectionWrapper(conn, return_func=lambda c: return_pg_connection(c))
    except Exception as e:
        logger.error(f"PG connection failed: {e}")
        raise


def try_get_db():
    """Non-blocking: returns a connection or None if pool is busy. For non-critical logging."""
    try:
        from main import try_get_pg_connection, return_pg_connection
        conn = try_get_pg_connection()
        if conn is None:
            return None
        return PGConnectionWrapper(conn, return_func=lambda c: return_pg_connection(c))
    except Exception:
        return None


def get_db(db_path=None, timeout=120):
    return _get_pg_connection()


def get_read_db(db_path=None):
    return _get_pg_connection()


def get_bg_db():
    return _get_pg_connection()


# Phase FF+7-fix4 (2026-05-19) — context manager that guarantees
# conn.close() in finally, regardless of exception path. Use this for
# every new daemon-thread / background-task DB call going forward:
#
#   from db_utils import safe_db
#   with safe_db() as conn:
#       c = conn.cursor()
#       c.execute(...)
#       conn.commit()
#   # conn is guaranteed closed here, even if the block raised
#
# This prevents the Neon pool exhaustion class of outage that took
# Railway down for 30 minutes on 2026-05-19. The brain detector
# check_unsafe_db_conn_pattern (routes/brain_consistency_radar.py)
# flags files that have many `conn = get_db()` calls but few finally
# blocks — adopt this helper to make those flags clear.
from contextlib import contextmanager as _cm

@_cm
def safe_db():
    """Context manager that guarantees conn.close() on any exit path.
    Use for daemon-thread / background-task DB operations.
    Yields the connection; the caller manages cursors and commits.
    """
    conn = _get_pg_connection()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


@_cm
def safe_db_cursor():
    """Same guarantees as safe_db() but yields a cursor instead.
    Convenient when you don't need direct access to the connection.
    Auto-commits on clean exit.

      with safe_db_cursor() as cur:
          cur.execute("...")
          # commit on clean exit; rollback + close on exception
    """
    conn = _get_pg_connection()
    cur = None
    try:
        cur = conn.cursor()
        yield cur
        try: conn.commit()
        except Exception: pass
    except Exception as _e:
        # r80b: log before re-raising. Many callers wrap this in their own
        # try/except:pass (fire-and-forget writes), which swallowed the
        # error — exactly how the ai_citations column-mismatch hid for 2
        # weeks. Logging HERE surfaces a dead write in the Railway logs even
        # when the caller stays silent. Re-raise preserves caller control flow.
        try:
            logger.error("safe_db_cursor write failed: %r", _e)
        except Exception:
            pass
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        if cur is not None:
            try: cur.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass


@_cm
def ddl_cursor():
    """The ONE blessed way to run DDL. A DIRECT psycopg2 cursor — never pooled.

    ★ WHY THIS EXISTS. Everything else in this module hands back a
    PGCursorWrapper, whose execute() returns early for CREATE TABLE / CREATE
    INDEX / ALTER TABLE whenever SKIP_DDL is set — and it defaults to '1' (line
    13) and is absent from prod config. No raise, no log, no table. That hid
    mcp_sessions for three months (#2196), and scripts/check_ddl_through_pool.py
    froze 59 more functions with the same defect.

    ★ AND WHY IT IS HERE RATHER THAN IN EACH MODULE. Twenty-five modules
    already work around the trap, every one of them by hand-rolling its own
    psycopg2.connect — because there was no blessed alternative to reach for.
    A trap with no marked path around it gets walked into. This is the marked
    path:

        from db_utils import ddl_cursor
        with ddl_cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS ...")

    Autocommit, so a failed statement cannot leave an aborted transaction
    behind. Its own connection, so it never occupies a pool slot and a boot-time
    schema create cannot contend with request traffic. Raises if there is no
    DATABASE_URL — a silent no-op is precisely the bug being fixed here, and a
    caller that wants best-effort should catch, visibly, at the call site.
    """
    import psycopg2
    url = (os.environ.get('DATABASE_URL')
           or os.environ.get('NEON_DATABASE_URL') or '').strip()
    if not url:
        raise RuntimeError(
            'ddl_cursor: no DATABASE_URL/NEON_DATABASE_URL — refusing to '
            'pretend the DDL ran')
    conn = psycopg2.connect(url, connect_timeout=10)
    conn.autocommit = True
    cur = None
    try:
        cur = conn.cursor()
        yield cur
    finally:
        if cur is not None:
            try: cur.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass


def safe_write(db_path, sql, params=None, retries=5, delay=0.5):
    for attempt in range(retries):
        try:
            conn = _get_pg_connection()
            try:
                if params:
                    c = conn.cursor()
                    c.execute(sql, params)
                else:
                    c = conn.cursor()
                    c.execute(sql)
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1) + random.uniform(0, 0.3))
            else:
                logger.error(f"DB write failed after {retries} attempts: {e}")
                return False
    return False


def safe_executemany(db_path, sql, rows, retries=5, delay=0.5):
    for attempt in range(retries):
        try:
            conn = _get_pg_connection()
            try:
                c = conn.cursor()
                c.executemany(sql, rows)
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1) + random.uniform(0, 0.3))
            else:
                logger.error(f"DB executemany failed after {retries} attempts: {e}")
                return False
    return False


def safe_write_returning(db_path, sql, params=None, retries=5, delay=0.5):
    for attempt in range(retries):
        try:
            conn = _get_pg_connection()
            try:
                cursor = conn.cursor()
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1) + random.uniform(0, 0.3))
            else:
                logger.error(f"DB write failed after {retries} attempts: {e}")
                return 0
    return 0


def safe_transaction(db_path, operations, retries=5, delay=0.5):
    for attempt in range(retries):
        try:
            conn = _get_pg_connection()
            try:
                cursor = conn.cursor()
                for sql, params in operations:
                    if params:
                        cursor.execute(sql, params)
                    else:
                        cursor.execute(sql)
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1) + random.uniform(0, 0.3))
            else:
                logger.error(f"DB transaction failed after {retries} attempts: {e}")
                return False
    return False
