"""
Database connection pool for Neon PostgreSQL.
Uses psycopg2 with a simple connection helper.
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")


@contextmanager
def get_conn():
    """Context manager that yields a DB connection and auto-commits/rollbacks."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_cursor():
    """Context manager that yields a cursor with RealDictCursor."""
    with get_conn() as conn:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()


def run_migration(sql_path: str):
    """Run a SQL migration file against the database."""
    with open(sql_path, "r") as f:
        sql = f.read()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cur.close()
    print(f"Migration applied: {sql_path}")
