"""
mcp_connection_pool.py — Connection Pool Warmup & Health Monitor

Deploy: Copy to Railway project root alongside dchub_mcp_server.py
Import: Add `from mcp_connection_pool import warm_pool, get_healthy_connection` to dchub_mcp_server.py

Problem: MCP process restarts every 10-15 min. First requests after restart
hit cold connections, causing timeouts and auth errors.

Fix: 
- Warm the connection pool on startup (pre-create 3 connections)
- Health-check connections before use
- Auto-reconnect on failure with exponential backoff
"""

import os
import time
import logging
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

logger = logging.getLogger("dchub.mcp_pool")

# Connection config
NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
POOL_MIN = 2
POOL_MAX = 8
CONNECT_TIMEOUT = 5  # seconds
QUERY_TIMEOUT = 10000  # milliseconds (10s) — prevents runaway queries

_pool = None
_pool_created_at = 0
POOL_MAX_AGE = 300  # Recreate pool every 5 min to avoid stale connections


def _create_pool():
    """Create a new connection pool with health-checked connections."""
    global _pool, _pool_created_at
    
    if _pool:
        try:
            _pool.closeall()
        except Exception:
            pass
    
    _pool = pool.ThreadedConnectionPool(
        POOL_MIN,
        POOL_MAX,
        NEON_DATABASE_URL,
        connect_timeout=CONNECT_TIMEOUT,
        options=f"-c statement_timeout={QUERY_TIMEOUT}"
    )
    _pool_created_at = time.time()
    logger.info(f"Connection pool created: min={POOL_MIN}, max={POOL_MAX}, timeout={QUERY_TIMEOUT}ms")
    return _pool


def warm_pool():
    """
    Call on MCP server startup to pre-warm connections.
    This eliminates cold-start latency on the first few requests.
    """
    try:
        p = _create_pool()
        
        # Pull and return connections to warm them
        conns = []
        for i in range(POOL_MIN):
            try:
                conn = p.getconn()
                # Validate with a simple query
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conns.append(conn)
                logger.info(f"Warmed connection {i+1}/{POOL_MIN}")
            except Exception as e:
                logger.warning(f"Failed to warm connection {i+1}: {e}")
        
        # Return all connections to pool
        for conn in conns:
            try:
                p.putconn(conn)
            except Exception:
                pass
        
        logger.info(f"Pool warmup complete: {len(conns)}/{POOL_MIN} connections ready")
        return True
    except Exception as e:
        logger.error(f"Pool warmup failed: {e}")
        return False


def _ensure_pool():
    """Ensure pool exists and isn't too old."""
    global _pool, _pool_created_at
    
    if _pool is None or (time.time() - _pool_created_at > POOL_MAX_AGE):
        _create_pool()
    
    return _pool


@contextmanager
def get_healthy_connection():
    """
    Get a health-checked connection from the pool.
    Auto-reconnects on failure. Always returns connection to pool.
    
    Usage:
        with get_healthy_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ...")
                result = cur.fetchall()
    """
    p = _ensure_pool()
    conn = None
    retries = 2
    
    for attempt in range(retries + 1):
        try:
            conn = p.getconn()
            
            # Health check: is connection still alive?
            if conn.closed:
                logger.warning(f"Got closed connection, attempt {attempt+1}")
                try:
                    p.putconn(conn, close=True)
                except Exception:
                    pass
                conn = None
                continue
            
            # Ping test
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            except Exception:
                logger.warning(f"Connection ping failed, attempt {attempt+1}")
                try:
                    p.putconn(conn, close=True)
                except Exception:
                    pass
                conn = None
                # If pool is bad, recreate it
                if attempt == 1:
                    p = _create_pool()
                continue
            
            # Connection is healthy
            yield conn
            return
            
        except psycopg2.OperationalError as e:
            logger.error(f"Operational error attempt {attempt+1}: {e}")
            if conn:
                try:
                    p.putconn(conn, close=True)
                except Exception:
                    pass
                conn = None
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))  # Backoff
                p = _create_pool()
            else:
                raise
        finally:
            if conn:
                try:
                    # Reset any aborted transaction
                    if conn.info.transaction_status != 0:  # IDLE
                        conn.rollback()
                    p.putconn(conn)
                except Exception:
                    try:
                        p.putconn(conn, close=True)
                    except Exception:
                        pass


def execute_with_timeout(query, params=None, timeout_ms=None):
    """
    Execute a query with explicit timeout. Returns list of dicts.
    
    Usage:
        results = execute_with_timeout(
            "SELECT * FROM eia_retail_rates WHERE state = %s LIMIT 10",
            ("VA",),
            timeout_ms=5000
        )
    """
    with get_healthy_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if timeout_ms:
                cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            cur.execute(query, params)
            if cur.description:
                return cur.fetchall()
            return []


# ============================================================
# MCP Request Timeout Wrapper
# ============================================================

import signal
import functools


class MCPTimeoutError(Exception):
    """Raised when an MCP tool handler exceeds its time budget."""
    pass


def mcp_timeout(seconds=30):
    """
    Decorator to enforce a hard timeout on MCP tool handlers.
    Prevents any single tool from hanging the entire MCP process.
    
    Usage:
        @mcp_timeout(15)
        def handle_get_renewable_energy(params):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            def handler(signum, frame):
                raise MCPTimeoutError(
                    f"MCP tool {func.__name__} timed out after {seconds}s"
                )
            
            old_handler = signal.signal(signal.SIGALRM, handler)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
                signal.alarm(0)  # Cancel alarm
                return result
            except MCPTimeoutError:
                logger.error(f"TIMEOUT: {func.__name__} exceeded {seconds}s")
                return {
                    "error": f"Query timed out after {seconds}s",
                    "suggestion": "Try a more specific query or smaller area",
                    "success": False
                }
            finally:
                signal.signal(signal.SIGALRM, old_handler)
        return wrapper
    return decorator


# ============================================================
# Startup hook
# ============================================================

def init():
    """Call from dchub_mcp_server.py on startup."""
    logger.info("Initializing MCP connection pool...")
    success = warm_pool()
    if success:
        logger.info("MCP connection pool ready")
    else:
        logger.warning("MCP connection pool warmup failed — will retry on first request")
    return success
