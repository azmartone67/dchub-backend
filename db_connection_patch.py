"""
Global SQLite Connection Patch
Monkey-patches sqlite3.connect so EVERY connection in the process
automatically gets WAL mode and high busy_timeout.

Key insight: threading.Lock does NOT work across gunicorn worker processes
(each worker is a separate OS process with its own memory). Instead we rely on:
1. WAL mode - allows concurrent readers with one writer
2. High busy_timeout - SQLite retries internally when locked (no Python retry needed)
3. Short-lived connections - open late, close early

Import this module ONCE at the top of main.py before any other imports.
"""
import sqlite3
import time
import logging
import random

logger = logging.getLogger(__name__)

_original_connect = sqlite3.connect

MAX_COMMIT_RETRIES = 5
BASE_RETRY_DELAY = 0.1


class PatchedConnection:
    def __init__(self, conn, db_path):
        self._conn = conn
        self._db_path = db_path

    def commit(self):
        for attempt in range(MAX_COMMIT_RETRIES):
            try:
                self._conn.commit()
                return
            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < MAX_COMMIT_RETRIES - 1:
                    wait = BASE_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 0.1)
                    logger.warning(f"DB locked on commit (attempt {attempt+1}/{MAX_COMMIT_RETRIES}), retrying in {wait:.2f}s: {self._db_path}")
                    time.sleep(wait)
                else:
                    logger.error(f"Commit failed after {attempt + 1} attempts on {self._db_path}: {e}")
                    raise

    def execute(self, sql, params=None):
        if params is not None:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def executemany(self, sql, params):
        return self._conn.executemany(sql, params)

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def cursor(self):
        return self._conn.cursor()

    def close(self):
        return self._conn.close()

    def rollback(self):
        return self._conn.rollback()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    @property
    def total_changes(self):
        return self._conn.total_changes

    @property
    def isolation_level(self):
        return self._conn.isolation_level

    @isolation_level.setter
    def isolation_level(self, value):
        self._conn.isolation_level = value

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)


def patched_connect(database, *args, **kwargs):
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 120

    conn = _original_connect(database, *args, **kwargs)

    db_str = str(database)
    if db_str.endswith('.db') or 'nexus' in db_str.lower():
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=120000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
        except Exception:
            pass

    return PatchedConnection(conn, db_str)


def install_patch():
    sqlite3.connect = patched_connect
    logger.info("🔒 SQLite connection patch installed (WAL + busy_timeout=120s for all connections)")


install_patch()
