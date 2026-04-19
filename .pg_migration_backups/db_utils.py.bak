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
    "datetime('now', '-1 day')": "(NOW() - INTERVAL '1 day')",
    "datetime('now', '-24 hours')": "(NOW() - INTERVAL '24 hours')",
    "datetime('now', '-1 hour')": "(NOW() - INTERVAL '1 hour')",
    "datetime('now', '-6 hours')": "(NOW() - INTERVAL '6 hours')",
    "datetime('now', '-12 hours')": "(NOW() - INTERVAL '12 hours')",
    "datetime('now', '-48 hours')": "(NOW() - INTERVAL '48 hours')",
    "datetime('now', '-90 days')": "(NOW() - INTERVAL '90 days')",
    "datetime('now', '-365 days')": "(NOW() - INTERVAL '365 days')",
    "datetime('now')": "NOW()",
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
    out = re.sub(r"\bdatetime\s*\(\s*'now'\s*,\s*'([^']+)'\s*\)", r"(NOW() - INTERVAL '\1')", out, flags=re.IGNORECASE)
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
        if self._cur.description:
            keys = [d[0] for d in self._cur.description]
            return PGRowProxy(row, keys)
        return row

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
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

    def cursor(self):
        return PGCursorWrapper(self._conn.cursor())

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


def _get_pg_connection():
    try:
        from main import get_pg_connection, return_pg_connection
        conn = get_pg_connection(retries=2)
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


def safe_write(db_path, sql, params=None, retries=5, delay=0.5):
    for attempt in range(retries):
        try:
            conn = _get_pg_connection()
            try:
                if params:
                    conn.execute(sql, params)
                else:
                    conn.execute(sql)
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
                conn.executemany(sql, rows)
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
