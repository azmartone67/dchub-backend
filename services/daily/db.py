"""Neon Postgres helpers.

Schema (see neon-schema.sql):
    snapshots(date PK, payload JSONB, generated_at TIMESTAMPTZ)
    renders(date, theme, size, r2_key, bytes, PRIMARY KEY(date, theme, size))

Env:
    DATABASE_URL   postgres://...@...neon.tech/dchub_daily
"""
from __future__ import annotations

import json
import os
import datetime
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@contextmanager
def conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    c = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def upsert_snapshot(date: datetime.date, payload: dict) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO daily.snapshots(date, payload, generated_at)
               VALUES (%s, %s, now() ON CONFLICT DO NOTHING)
               ON CONFLICT(date) DO UPDATE
                 SET payload = EXCLUDED.payload,
                     generated_at = EXCLUDED.generated_at""",
            (date, json.dumps(payload)),
        )


def get_snapshot(date: datetime.date | None = None) -> dict | None:
    q = ("SELECT payload FROM daily.snapshots WHERE date = %s" if date else
         "SELECT payload FROM daily.snapshots ORDER BY date DESC LIMIT 1")
    params = (date,) if date else ()
    with conn() as c:
        row = c.execute(q, params).fetchone()
        return row["payload"] if row else None


def upsert_render(date: datetime.date, theme: str, size: str,
                  r2_key: str, nbytes: int) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO daily.renders(date, theme, size, r2_key, bytes, generated_at)
               VALUES (%s, %s, %s, %s, %s, now() ON CONFLICT DO NOTHING)
               ON CONFLICT(date, theme, size) DO UPDATE
                 SET r2_key = EXCLUDED.r2_key,
                     bytes = EXCLUDED.bytes,
                     generated_at = EXCLUDED.generated_at""",
            (date, theme, size, r2_key, nbytes),
        )


def get_renders(date: datetime.date) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT theme, size, r2_key, bytes FROM daily.renders WHERE date = %s",
            (date,),
        ).fetchall()
        return list(rows)
