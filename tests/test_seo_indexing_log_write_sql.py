#!/usr/bin/env python3
"""seo_agent.ping_indexnow's log write, against a REAL Postgres.

Railway logged it twice on 2026-09-11, at 15:50:30Z and 17:36:20Z:

    swallowed DB write #1 table=seo_indexing_log at=seo_agent.ping_indexnow
    err=InvalidColumnReference: there is no unique or exclusion constraint
    matching the ON CONFLICT specification

The INSERT upserted on url, and init_seo_tables gives url no unique constraint,
so Postgres refused every write and note_swallowed_write ate it. The same day
/api/seo/status read indexnow_pings_24h 0 with both of those pings inside its
window. A fake cursor accepts any conflict target; only a database shows this.

Each test builds the tables with the module's own DDL, in a schema of its own
that is dropped afterwards. The submit is stubbed: nothing leaves the process.

Set SEO_INDEXING_LOG_SQL_DSN to run. CI's db-parity job sets it and fails the
job if anything here skipped.

Run:  SEO_INDEXING_LOG_SQL_DSN=postgresql:///seo_log_parity \\
        python3 -m pytest tests/test_seo_indexing_log_write_sql.py -v
"""
import importlib.util
import os
import pathlib
import sys
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("SEO_INDEXING_LOG_SQL_DSN", "").strip()

pytestmark = pytest.mark.skipif(not DSN, reason="SEO_INDEXING_LOG_SQL_DSN is not set")

BING = {"ok": True, "status": 200, "submitted": 3,
        "endpoint": "https://www.bing.com/indexnow"}
# The same URL twice: a re-submission is a second row, not a conflict.
URLS = ["https://dchub.cloud/news/a", "https://dchub.cloud/news/a",
        "https://dchub.cloud/"]


def _load_seo_agent():
    """seo_agent.py executed from disk under a private name, never a sys.modules stub."""
    spec = importlib.util.spec_from_file_location(
        "_seo_indexing_log_sql_seo_agent", ROOT / "seo_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def connect():
    """connect() for connections that live in a fresh schema."""
    schema = "seo_log_" + uuid.uuid4().hex[:10]
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
    try:
        yield lambda: psycopg2.connect(DSN, options=f"-c search_path={schema}")
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.fixture
def seo_agent(connect, monkeypatch):
    sa = _load_seo_agent()
    monkeypatch.setattr(sa, "get_db", connect)
    sa.init_seo_tables()  # the table the live INSERT met
    return sa


def _logged(connect):
    conn = connect()  # a fresh connection sees only what was committed
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url, search_engine, status, response_code "
                        "FROM seo_indexing_log ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def test_every_submitted_url_is_logged(seo_agent, connect, monkeypatch):
    import routes.indexnow as indexnow
    from routes import _swallowed_writes as swallowed
    monkeypatch.setattr(indexnow, "submit_to_indexnow", lambda urls: dict(BING))
    swallowed._reset_for_tests()

    out = seo_agent.ping_indexnow(URLS)

    assert out["success"] is True
    assert not [k for k in swallowed.swallowed_write_counts()
                if k.endswith(":seo_indexing_log")], swallowed.swallowed_write_counts()
    assert _logged(connect) == [(u, BING["endpoint"], "success", 200) for u in URLS]


def test_control_this_table_refuses_an_upsert_on_url(seo_agent, connect):
    """The premise, pinned: the DDL gives url no unique constraint, so a write
    naming url as its conflict target is refused. If the DDL ever gains one,
    this fails, and the append in ping_indexnow deserves a second look."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InvalidColumnReference):
                cur.execute(
                    "INSERT INTO seo_indexing_log (url, search_engine) "
                    "VALUES (%s, %s) ON CONFLICT (url) DO NOTHING",
                    ("https://dchub.cloud/", "control"))
    finally:
        conn.rollback()
        conn.close()
