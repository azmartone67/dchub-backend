"""
database.py — Standalone PostgreSQL connection pool.

This module owns the connection pool and all low-level DB helpers.
It intentionally has NO imports from main.py so that any route module
can import get_db (or the pool primitives) without triggering a circular
import during Flask startup.

Resolution order for the connection URL:
  1. DATABASE_URL   — standard PaaS convention (Railway sets this)
  2. NEON_DATABASE_URL — explicit Neon override
  3. Falls back to a no-op (returns None) so the app still boots when
     no DB is configured (e.g. during CI or local dev without Postgres).

Connection pool:
  - Simple thread-safe pool backed by a queue.Queue.
  - Default size: PG_POOL_SIZE env var (default 5).
  - get_pg_connection()   — blocking acquire (with retries)
  - try_get_pg_connection() — non-blocking acquire (returns None if busy)
  - return_pg_connection()  — return a connection to the pool
  - _record_circuit_failure() — increment the circuit-breaker counter
"""

from __future__ import annotations

import os
import re
import time
import queue
import logging
import threading

logger = logging.getLogger(__name__)

# ── URL resolution ───────────────────────────────────────────────────

def _resolve_pg_url() -> str:
    """Return the PostgreSQL connection URL, normalised for psycopg2."""
    url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL")
        or ""
    ).strip()
    if not url:
        return ""
    # Strip leading 'psql ' shell prefix that sometimes appears in env vars
    url = re.sub(r"^psql\s+", "", url).strip("'\"")
    # Remove channel_binding parameter (not supported by all drivers)
    url = re.sub(r"[&?]channel_binding=[^&]*", "", url)
    # Normalise scheme: psycopg2 accepts postgresql:// or postgres://
    if url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://"):]
    return url


PG_URL: str = _resolve_pg_url()
PG_POOL_SIZE: int = int(os.environ.get("PG_POOL_SIZE", "5"))

# ── Connection pool ──────────────────────────────────────────────────

_pool: "queue.Queue[object]" = queue.Queue(maxsize=PG_POOL_SIZE)
_pool_lock = threading.Lock()
_pool_initialised = False

# Circuit-breaker: consecutive connectivity failures
_circuit_failures = 0
_CIRCUIT_OPEN_THRESHOLD = 5
_circuit_open_until: float = 0.0


def _make_raw_connection():
    """Open a new psycopg2 connection. Returns None if unavailable."""
    if not PG_URL:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(PG_URL)
        conn.autocommit = False
        return conn
    except ImportError:
        try:
            import psycopg2cffi as psycopg2  # type: ignore
            conn = psycopg2.connect(PG_URL)
            conn.autocommit = False
            return conn
        except ImportError:
            logger.warning("[database] No psycopg2 driver available")
            return None
    except Exception as e:
        logger.error(f"[database] PostgreSQL connection failed: {e}")
        return None


def _init_pool() -> None:
    """Fill the pool with fresh connections (called once at startup)."""
    global _pool_initialised
    with _pool_lock:
        if _pool_initialised:
            return
        _pool_initialised = True
    for _ in range(PG_POOL_SIZE):
        conn = _make_raw_connection()
        if conn is not None:
            try:
                _pool.put_nowait(conn)
            except queue.Full:
                try:
                    conn.close()
                except Exception:
                    pass


def _ensure_pool() -> None:
    if not _pool_initialised:
        _init_pool()


# ── Public pool API ──────────────────────────────────────────────────

def get_pg_connection(retries: int = 3, timeout: float = 5.0):
    """Acquire a raw psycopg2 connection from the pool (blocking).

    Retries *retries* times with a short sleep between attempts.
    Returns None if the pool is empty after all retries or if the DB
    is unavailable.
    """
    _ensure_pool()
    global _circuit_open_until
    if time.time() < _circuit_open_until:
        logger.warning("[database] Circuit open — skipping DB call")
        return None
    for attempt in range(max(1, retries)):
        try:
            conn = _pool.get(block=True, timeout=timeout)
            # Validate the connection is still alive
            try:
                conn.cursor().execute("SELECT 1")
            except Exception:
                # Connection is dead — replace it
                try:
                    conn.close()
                except Exception:
                    pass
                conn = _make_raw_connection()
                if conn is None:
                    continue
            return conn
        except queue.Empty:
            if attempt < retries - 1:
                time.sleep(0.2 * (attempt + 1))
            else:
                logger.warning("[database] Pool exhausted after %d retries", retries)
                # Fall back to a fresh connection outside the pool
                return _make_raw_connection()
    return _make_raw_connection()


def try_get_pg_connection():
    """Non-blocking pool acquire. Returns None immediately if pool is empty."""
    _ensure_pool()
    if time.time() < _circuit_open_until:
        return None
    try:
        conn = _pool.get_nowait()
        try:
            conn.cursor().execute("SELECT 1")
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return _make_raw_connection()
        return conn
    except queue.Empty:
        return None


def return_pg_connection(conn) -> None:
    """Return a connection to the pool. Rolls back any open transaction."""
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        _pool.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        except Exception:
            pass


def _record_circuit_failure() -> None:
    """Increment the circuit-breaker counter. Opens the circuit after
    _CIRCUIT_OPEN_THRESHOLD consecutive failures (30-second cooldown)."""
    global _circuit_failures, _circuit_open_until
    _circuit_failures += 1
    if _circuit_failures >= _CIRCUIT_OPEN_THRESHOLD:
        _circuit_open_until = time.time() + 30
        _circuit_failures = 0
        logger.warning(
            "[database] Circuit opened — DB calls paused for 30s "
            "after %d consecutive failures", _CIRCUIT_OPEN_THRESHOLD
        )


# ── High-level get_db helper ─────────────────────────────────────────
# This is the function that route modules should import.  It wraps the
# raw psycopg2 connection in db_utils.PGConnectionWrapper so callers
# get the SQLite-compatible cursor shim automatically.

def get_db(db_path=None, timeout: int = 120):
    """Return a PGConnectionWrapper (or None on failure).

    Importing this from database.py instead of main.py breaks the
    circular-import chain that was preventing the app from booting.
    """
    try:
        from db_utils import PGConnectionWrapper
        conn = get_pg_connection(retries=2)
        if conn is None:
            return None
        try:
            c = conn.cursor()
            c.execute("SET statement_timeout = 15000")
            conn.commit()
            c.close()
        except Exception:
            pass
        return PGConnectionWrapper(conn, return_func=return_pg_connection)
    except Exception as e:
        logger.error(f"[database] get_db failed: {e}")
        return None
