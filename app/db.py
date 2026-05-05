"""SQLite (local dev) + Postgres (Neon, prod) engine + session helpers.

Resolution order for the connection URL:
  1. DATABASE_URL  — typical PaaS convention (Railway/Render/Fly all set this).
  2. DCHUB_DATABASE_URL — explicit override.
  3. SQLite at DCHUB_DB_PATH or /tmp/dchub.db.

Neon's connection strings normally look like:
  postgresql://user:pass@ep-xxx.aws.neon.tech/dchub?sslmode=require
psycopg v3 is the driver. We rewrite postgres:// → postgresql+psycopg:// so
SQLAlchemy picks the right dialect automatically.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlmodel import SQLModel, Session, create_engine


def _resolve_db_url() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("DCHUB_DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://") and "+psycopg" not in url:
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    db_path = os.getenv("DCHUB_DB_PATH", "/tmp/dchub.db")
    return f"sqlite:///{db_path}"


DB_URL = _resolve_db_url()
IS_SQLITE = DB_URL.startswith("sqlite")

_connect_args = {"check_same_thread": False} if IS_SQLITE else {}
engine = create_engine(DB_URL, echo=False, connect_args=_connect_args)


def init_db() -> None:
    """Create all SQLModel tables. Safe to call multiple times."""
    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on exception, always close.

    Use for write paths. The bytecode for this matches your original line 45.
    """
    s = Session(engine)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@contextmanager
def get_session() -> Iterator[Session]:
    """Read-style session helper using Session as a context manager.

    Matches your original line 58 — simpler shape, no auto-commit.
    """
    with Session(engine) as s:
        yield s
