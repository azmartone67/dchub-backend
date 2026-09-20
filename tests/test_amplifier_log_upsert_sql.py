"""_record()'s upsert must actually land against a REAL Postgres.

THE DEFECT (2026-06-07 -> 2026-09-20, every single write): the unique index
this upsert infers is PARTIAL —

    CREATE UNIQUE INDEX multiplatform_amplifier_log_src_tgt
      ON multiplatform_amplifier_log (source_post_id, target_platform)
      WHERE (source_post_id > 0)

— and Postgres will not infer a partial index unless the ON CONFLICT clause
repeats its predicate. `_record` omitted it, so every insert raised

    InvalidColumnReference: there is no unique or exclusion constraint
    matching the ON CONFLICT specification

which `_record`'s own `except Exception` swallowed to stderr. The table held
ZERO rows for three and a half months while the fan-out posted to Bluesky
normally, and the emptiness fed back: _already_amplified() and the sweep's
NOT EXISTS both read an empty log as "never amplified", so the same LinkedIn
post was re-published on every sweep inside the lookback window (measured on
the live Bluesky feed 2026-09-20: the same text at 16:50:05 and again at
20:54:06), and _amplifications_today() read 0, so the daily cap never bound.

This runs the REAL DDL from init_amplifier_tables and the REAL SQL from
_record against a live Postgres, because that is the only place the defect
is visible — it is a planner-inference error, not a string the source can be
grepped for. Skips without a DSN; pre-merge.yml's db-parity job supplies one
and then asserts this file did not skip.
"""
import os
import re
import pathlib

import pytest

psycopg2 = pytest.importorskip("psycopg2")

ROOT = pathlib.Path(__file__).resolve().parents[1]
DSN = (os.environ.get("AMPLIFIER_LOG_SQL_DSN")
       or os.environ.get("CONTINUATION_SQL_DSN")
       or os.environ.get("FIBER_PARITY_DSN") or "")

pytestmark = pytest.mark.skipif(not DSN, reason="no Postgres DSN in env")

TABLE = "multiplatform_amplifier_log"


def _module_source() -> str:
    return (ROOT / "routes" / "multiplatform_amplifier.py").read_text()


def _extract(pattern: str, label: str) -> str:
    """Pull the real SQL out of the module so this test cannot drift into
    testing a copy that no longer matches what ships."""
    m = re.search(pattern, _module_source(), re.S)
    assert m, f"could not locate {label} in routes/multiplatform_amplifier.py"
    return m.group(1)


@pytest.fixture()
def conn():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    yield c
    with c.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    c.close()


def _create_schema(cur):
    """Replay init_amplifier_tables' real DDL, read out of the module."""
    create = _extract(r"(CREATE TABLE IF NOT EXISTS multiplatform_amplifier_log.*?\n\s*\)\s*)\n\s*\"\"\"",
                      "CREATE TABLE")
    uniq = _extract(r"(CREATE UNIQUE INDEX IF NOT EXISTS\s+multiplatform_amplifier_log_src_tgt.*?source_post_id > 0)",
                    "partial unique index")
    cur.execute(create)
    cur.execute(uniq)
    cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                ("multiplatform_amplifier_log_src_tgt",))
    indexdef = cur.fetchone()[0]
    # The whole defect only exists because this index is PARTIAL. If it ever
    # stops being partial the test below would pass for the wrong reason.
    assert "WHERE (source_post_id > 0)" in indexdef, indexdef


def _record_sql() -> str:
    sql = _extract(r"cur\.execute\(\"\"\"\s*(INSERT INTO multiplatform_amplifier_log.*?)\"\"\"",
                   "_record INSERT")
    assert "ON CONFLICT" in sql, sql
    return sql


ROW = (611, "linkedin", "substack", "body text", "https://x/p/y", "posted", "")


def test_record_upsert_lands_a_row(conn):
    """The write that never happened. Fails with InvalidColumnReference when
    the ON CONFLICT drops the partial index's predicate."""
    with conn.cursor() as cur:
        _create_schema(cur)
        cur.execute(_record_sql(), ROW)
        cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        assert cur.fetchone()[0] == 1, "the amplifier log write did not land"


def test_record_upsert_is_idempotent_on_repeat(conn):
    """Second sweep over the same source must UPDATE, not duplicate — this is
    what makes _already_amplified() and the sweep's NOT EXISTS work, and its
    absence is what double-posted to Bluesky."""
    sql = _record_sql()
    with conn.cursor() as cur:
        _create_schema(cur)
        cur.execute(sql, ROW)
        cur.execute(sql, ROW[:5] + ("failed", "boom"))
        cur.execute(f"SELECT COUNT(*), MAX(status), MAX(error) FROM {TABLE}")
        n, status, err = cur.fetchone()
        assert n == 1, f"repeat sweep duplicated the row ({n} rows)"
        assert status == "failed" and err == "boom", (status, err)


def test_adhoc_rows_outside_the_predicate_still_insert(conn):
    """source_post_id = 0 is the ad-hoc path and sits OUTSIDE the partial
    index, so it can never conflict — two ad-hoc posts must both be kept, not
    collapsed and not rejected."""
    sql = _record_sql()
    with conn.cursor() as cur:
        _create_schema(cur)
        cur.execute(sql, (0, "linkedin", "substack", "first", "", "posted", ""))
        cur.execute(sql, (0, "linkedin", "substack", "second", "", "posted", ""))
        cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE source_post_id = 0")
        assert cur.fetchone()[0] == 2, "ad-hoc rows were collapsed or refused"
