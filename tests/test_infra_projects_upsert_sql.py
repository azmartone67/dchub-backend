"""The gas/transmission PROJECT upsert and the board's "+N new", executed
against a real Postgres (2026-09-22).

tests/test_infra_projects_ingest.py pins keys, parsers and the statement text
with no database. Only a database shows what the product actually publishes:

  release 1  (initial load)   3 projects → inserted 3, all in_initial_load,
                              and the board counts +0 new — NOT +3
  release 2                   A changes status, B unchanged, C dropped, D new
                              → inserted 1, delisted 1, status_changes 1;
                              A keeps its first_seen_at and records prev_status;
                              the board counts +1 new
  release 3  (truncated)      1 project while 3 are listed → 409, table as it was
  release 4                   C reappears → listed again, still not "new"

and routes/infra_growth._summary — the code /api/v1/whats-new runs — reading
that table next to a count history that WOULD have said "+4" had it been
differenced. Nothing is copied: the DDL, the upsert and the board are the
shipped modules' own. Everything lives in a private schema this test drops.

Set INFRA_PROJECTS_SQL_DSN to run it. CI passes the db-parity service DSN and
then asserts this file did not skip.
"""
import datetime as dt
import os
import sys

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("INFRA_PROJECTS_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="INFRA_PROJECTS_SQL_DSN not set — no Postgres to run against")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCHEMA = "infra_projects_t"


@pytest.fixture
def conn():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    c.close()
    c = psycopg2.connect(DSN, options=f"-c search_path={SCHEMA}")
    yield c
    c.close()
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    c.close()


def _gas(name, status):
    return {"project_name": name, "status": status, "operator": "Op",
            "capacity_mmcfd": 100, "source_release": "2026-08-04"}


def _release(ipi, conn, raws, allow_shrink=False):
    rows, _ = ipi.normalize_gas_rows(raws)
    return ipi._write_in(conn, ipi.GAS_TABLE, ipi._GAS_DDL, ipi._GAS_FIELDS,
                         ipi.GAS_SOURCE, rows, allow_shrink)


def _rows(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT project_key, status, prev_status, status_changed_at, "
                    "first_seen_at, last_seen_at, in_initial_load, in_latest_release "
                    "FROM gas_pipeline_projects ORDER BY project_key")
        cols = [d[0] for d in cur.description]
        out = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    conn.commit()
    return out


def test_first_seen_survives_releases_and_the_board_never_counts_the_initial_load(conn):
    from routes import infra_projects_ingest as ipi
    from routes import infra_growth as ig

    # ── release 1: the initial load ─────────────────────────────────────
    st, body = _release(ipi, conn, [_gas("A", "Applied"), _gas("B", "Approved"),
                                    _gas("C", "Announced")])
    assert (st, body["inserted"], body["initial_load"]) == (200, 3, True)
    r1 = _rows(conn)
    assert all(r["in_initial_load"] and r["in_latest_release"] for r in r1.values())
    with conn.cursor() as cur:
        assert ig._first_seen_added(cur, "gas_pipeline_projects",
                                    "gas_pipeline_projects") == (0, 0), (
            "the initial load was counted as new")
    conn.commit()

    # ── release 2: a status change, a drop, an addition ─────────────────
    st, body = _release(ipi, conn, [_gas("A", "Construction"), _gas("B", "Approved"),
                                    _gas("D", "Announced")])
    assert st == 200
    assert (body["inserted"], body["updated"], body["delisted"],
            body["status_changes"], body["initial_load"]) == (1, 2, 1, 1, False)
    r2 = _rows(conn)
    assert r2["a"]["first_seen_at"] == r1["a"]["first_seen_at"], "first_seen_at was rewritten"
    assert r2["a"]["in_initial_load"] is True, "in_initial_load was rewritten"
    assert (r2["a"]["status"], r2["a"]["prev_status"]) == ("Construction", "Applied")
    assert r2["a"]["status_changed_at"] == r2["a"]["last_seen_at"] > r1["a"]["last_seen_at"]
    assert r2["b"]["status_changed_at"] is None and r2["b"]["prev_status"] is None
    assert r2["c"]["in_latest_release"] is False, "a dropped project must be kept, delisted"
    assert r2["d"]["in_initial_load"] is False and r2["d"]["in_latest_release"] is True
    with conn.cursor() as cur:
        assert ig._first_seen_added(cur, "gas_pipeline_projects",
                                    "gas_pipeline_projects") == (1, 1)
    conn.commit()

    # ── release 3: truncated — refused, nothing moves ───────────────────
    st, body = _release(ipi, conn, [_gas("A", "Cancelled")])
    assert st == 409 and body["ok"] is False and "shrink floor" in body["error"]
    assert _rows(conn) == r2, "a refused release changed the table"

    # ── release 4: C comes back — listed again, and still not new ───────
    st, body = _release(ipi, conn, [_gas("A", "Construction"), _gas("B", "Approved"),
                                    _gas("C", "Announced"), _gas("D", "Announced")])
    assert (st, body["inserted"], body["delisted"]) == (200, 0, 0)
    r4 = _rows(conn)
    assert r4["c"]["in_latest_release"] is True
    assert r4["c"]["first_seen_at"] == r1["c"]["first_seen_at"]

    # ── the board: count history says +4, first-seen says +1 ───────────
    today = dt.date.today()
    with conn.cursor() as cur:
        ig._ensure(cur)
        # The table "existed empty" two days ago and holds 4 rows today — the
        # exact history under which a COUNT(*) delta publishes the backfill.
        cur.executemany(
            "INSERT INTO infra_growth_snapshot (snapshot_date, layer, count) "
            "VALUES (%s, 'gas_pipeline_projects', %s)",
            [(today - dt.timedelta(days=2), 0), (today, 4)])
        conn.commit()
        layers, _flat = ig._summary(cur)
    conn.commit()
    rec = next(l for l in layers if l["layer"] == "gas_pipeline_projects")
    assert rec["count"] == 4
    assert (rec["delta_window"], rec["window_days"], rec["delta_1d"], rec["delta_7d"]) == (
        1, 7, 1, 1), f"board published {rec['delta_window']} new, want 1: {rec}"
    assert rec["growth_basis"] == "first_seen_column"
    assert rec["status"] == "growing" and "initial load is never counted" in rec["status_reason"]
    assert rec["freshness_column"] == "last_seen_at" and rec["ingest_age_days"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
