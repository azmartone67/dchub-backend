#!/usr/bin/env python3
"""
repair_fiber_routes_hifld_transmission.py — remove power lines stored as fiber
==============================================================================
MEASURED 2026-09-13 on production (BEGIN READ ONLY ... ROLLBACK)

  fiber_routes held 9,695 rows with route_type = 'transmission'. All of them:
    source = 'hifld', source_id = 'hifld_tl_<HIFLD line id>...'
    name   = '<utility> <kV>kV Line - <market>'
    coordinates, end_lat and the bbox columns NULL
    start_lat/start_lng = one of the 20 DC_MARKETS centroids
    provider = 102 distinct values, 101 of which own no other row

  They are HIFLD Electric_Power_Transmission_Lines features. The fiber lane
  (FiberRouteDiscovery._sync_hifld_transmission_lines, removed in the same
  change) wrote them between 2026-03-30 and 2026-08-14. Nothing in the rows or
  in the writer says a line carries fiber. Transmission lines are published
  from the transmission_lines table, not from these rows.

  Every COUNT(*) FROM fiber_routes surface counted them (67,836 raw, 58,141
  without them). /api/v1/fiber/sources listed the utilities as carriers.
  Start-point proximity readers counted them around the 20 market centroids,
  e.g. the site report's nearby fiber providers.

WHAT THIS DOES
  Deletes exactly those rows, so every reader of the table stops counting them
  at once. REMOVABLE_SQL selects them. is_removable() re-checks each fetched
  row in Python, and the run stops before writing anything if one fails. The
  full rows go to a rollback JSON before the DELETE.

SAFETY
  Dry run by default, in a read-only transaction. --apply writes the rollback
  file first, deletes by id in one transaction, and rolls back unless the DELETE
  removed exactly the rows it selected. --rollback FILE re-inserts them.
  HUMAN-RUN: the rollback file is written to ~/Downloads.

  Deploy the change that removes the writer FIRST. The lane inserted with
  ON CONFLICT DO NOTHING against (source, upstream_uid). With these rows gone
  and the lane still running, it would re-insert every line it can reach
  within one market rotation.

USAGE
    python3 repair_fiber_routes_hifld_transmission.py            # dry run
    python3 repair_fiber_routes_hifld_transmission.py --apply
    python3 repair_fiber_routes_hifld_transmission.py --rollback FILE
"""
import datetime
import json
import os
import sys

# substr(), not LIKE: there is no '%' for psycopg2 to substitute, and the same
# text runs on SQLite, which is how the test holds it against is_removable().
REMOVABLE_SQL = ("route_type = 'transmission' AND source = 'hifld' "
                 "AND substr(source_id, 1, 9) = 'hifld_tl_'")


def is_removable(row: dict) -> bool:
    return (row.get("route_type") == "transmission"
            and row.get("source") == "hifld"
            and str(row.get("source_id") or "").startswith("hifld_tl_"))


def _conn(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn, sslmode="require", connect_timeout=10)


def _select(cur, lock: bool = False) -> list:
    cur.execute("SELECT * FROM fiber_routes WHERE " + REMOVABLE_SQL
                + " ORDER BY id" + (" FOR UPDATE" if lock else ""))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _refuse(rows: list) -> bool:
    bad = [r.get("id") for r in rows if not is_removable(r)]
    if bad:
        print(f"REFUSING: {len(bad)} selected rows fail is_removable(), e.g. id {bad[0]}")
    return bool(bad)


def dry_run(conn) -> int:
    with conn.cursor() as cur:
        # Transaction-scoped. Never conn.set_session(readonly=True): DATABASE_URL
        # is Neon's -pooler endpoint, where a session-level read-only can stick to
        # the shared server connection and fail the next client's writes (25006).
        # See tests/test_readonly_session_never_on_a_pooled_dsn.py.
        cur.execute("SET TRANSACTION READ ONLY")
        rows = _select(cur)
    conn.rollback()
    providers = {r.get("provider") for r in rows}
    markets = {r.get("start_location") for r in rows}
    print(f"would delete {len(rows):,} rows "
          f"({len(providers)} providers, {len(markets)} markets)")
    if _refuse(rows):
        return 3
    print("DRY RUN — nothing written. Re-run with --apply.")
    return 0


def apply(conn, rollback_dir: str) -> int:
    with conn.cursor() as cur:
        rows = _select(cur, lock=True)
        if _refuse(rows):
            conn.rollback()
            return 3
        if not rows:
            conn.rollback()
            print("nothing to delete")
            return 0
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(rollback_dir,
                            f"fiber_routes_hifld_transmission_rollback_{stamp}.json")
        with open(path, "w") as fh:
            json.dump({"generated_at_utc": stamp, "table": "fiber_routes",
                       "note": "undo: python3 repair_fiber_routes_hifld_transmission.py "
                               "--rollback <this file>",
                       "rows": rows}, fh, default=str)
        print(f"rollback file written: {path}")
        ids = [r["id"] for r in rows]
        cur.execute("DELETE FROM fiber_routes WHERE id = ANY(%s) AND " + REMOVABLE_SQL,
                    (ids,))
        if cur.rowcount != len(ids):
            conn.rollback()
            print(f"ROLLED BACK: the DELETE removed {cur.rowcount} rows, "
                  f"{len(ids)} were selected")
            return 4
    conn.commit()
    print(f"deleted {len(ids):,} rows")
    return 0


def rollback(conn, path: str) -> int:
    with open(path) as fh:
        rows = json.load(fh)["rows"]
    restored = 0
    with conn.cursor() as cur:
        for row in rows:
            cols = list(row)
            if not all(c.isidentifier() for c in cols):
                raise ValueError(f"unexpected column name in {path}: {cols}")
            sql = ("INSERT INTO fiber_routes ({cols}) VALUES ({vals}) ON CONFLICT DO NOTHING"
                   .format(cols=", ".join(cols), vals=", ".join(["%s"] * len(cols))))
            cur.execute(sql, [row[c] for c in cols])
            restored += cur.rowcount
    conn.commit()
    print(f"restored {restored:,} of {len(rows):,} rows")
    return 0 if restored == len(rows) else 5


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dsn = os.environ.get("DATABASE_URL") or ""
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    conn = _conn(dsn)
    try:
        if "--rollback" in argv:
            return rollback(conn, argv[argv.index("--rollback") + 1])
        if "--apply" in argv:
            return apply(conn, os.path.expanduser("~/Downloads"))
        return dry_run(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
