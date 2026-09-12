#!/usr/bin/env python3
"""Build the covering index that takes surface_telemetry off a sequential scan.

★ THE MEASUREMENT (production, 21.75-day window, stats reset 2026-08-21)

The most expensive statement in the database — 146,256s, 13.41% of ALL database
time, 1,307,924 calls — reads `surface_telemetry` (15.78M rows, 4,682 MB) with a
Parallel Seq Scan:

    SELECT COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL $1) AS e24, ...
           COUNT(DISTINCT anon_id) FILTER (...) AS u24, ...
    FROM surface_telemetry WHERE surface_id = $5 AND ts >= NOW() - INTERVAL $6

`ix_surface_telemetry_surface_ts` is `(surface_id, ts DESC)` and matches that
predicate exactly. The scan happens because the SELECT also needs `anon_id`,
which the index does not carry, so an index-only scan is impossible and the
planner reads the heap instead. Isolated on the live table, same predicate and
window, one column apart:

    COUNT(*)                  over 7d ->   486 ms   Parallel Index Only Scan
    COUNT(DISTINCT anon_id)   over 7d ->  2034 ms   Parallel Seq Scan

Four query shapes over this table account for roughly 30% of all database time.
They share that predicate, and all of them select `anon_id`.

★ WHY A NEW INDEX AND NOT AN ALTERED ONE. INCLUDE cannot be added to an index
in place; this is a create-then-replace. The new index has the same leading
columns, so it can serve everything the old one serves — but this script does
NOT drop the old one. `ix_surface_telemetry_surface_ts` has 6.4M recorded scans,
and dropping a live index in the same operation that adds its replacement
leaves no way to tell which change caused a regression. Drop it in a FOLLOW-UP,
once pg_stat_user_indexes shows the new index taking the traffic; the command is
printed at the end.

★ COST, STATED PLAINLY. The existing index is 1,075 MB. `anon_id` averages 17
bytes over 15.78M rows, so the covering index lands near 1.3 GB, and both exist
until the follow-up drop. That is real storage and real write amplification on a
table ingesting ~258,000 rows/day. It buys ~30% of database time.

★ WHY THE VERIFICATION IS NOT `pg_indexes`. CREATE INDEX CONCURRENTLY can fail
and leave the index in place but INVALID — present, named, never used by the
planner. `pg_indexes` does not filter on validity (confirmed against its own
view definition: no `indisvalid`), so both existing scripts in this repo
(`add_performance_indexes.py`, `tools/add_brain_rag_hnsw_index.py`) would report
a failed build as a success. Worse, they issue CREATE INDEX CONCURRENTLY IF NOT
EXISTS, which SKIPS a leftover invalid index — so every later re-run also
reports success and the index stays dead forever. This script reads
`pg_index.indisvalid`, drops a leftover invalid index before rebuilding, and
finally checks the thing that actually matters: that the planner switched off
the sequential scan.

Usage:
    python3 scripts/add_surface_telemetry_covering_index.py            # dry run
    python3 scripts/add_surface_telemetry_covering_index.py --apply    # build it

Exit: 0 done or nothing to do · 1 the build did not take · 2 cannot run.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

TABLE = "surface_telemetry"
INDEX = "ix_surface_telemetry_surface_ts_incl_anon"
SUPERSEDED = "ix_surface_telemetry_surface_ts"
DDL = (f"CREATE INDEX CONCURRENTLY {INDEX} "
       f"ON {TABLE} (surface_id, ts DESC) INCLUDE (anon_id)")

# The representative shape of the four expensive queries — used to prove the
# plan actually changed, rather than that an index object appeared.
PROBE = """
SELECT COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '24 hours') AS e24,
       COUNT(DISTINCT anon_id) FILTER (WHERE ts >= NOW() - INTERVAL '7 days'
                                        AND anon_id IS NOT NULL) AS u7
FROM surface_telemetry WHERE surface_id = %s AND ts >= NOW() - INTERVAL '7 days'
"""


def _dsn() -> str:
    url = ((os.environ.get("DATABASE_URL") or "").strip()
           or (os.environ.get("NEON_DATABASE_URL") or "").strip())
    if not url:
        raise SystemExit("no DATABASE_URL/NEON_DATABASE_URL — refusing to "
                         "pretend the index was built")
    return url


def index_state(cur, name):
    """(exists, valid, ready) straight from pg_index.

    NOT pg_indexes: that view has no `indisvalid`, so it reports an index left
    behind by a failed CONCURRENTLY build exactly as it reports a working one.
    """
    cur.execute("""SELECT ix.indisvalid, ix.indisready
                   FROM pg_index ix
                   JOIN pg_class i ON i.oid = ix.indexrelid
                   JOIN pg_namespace n ON n.oid = i.relnamespace
                   WHERE n.nspname = 'public' AND i.relname = %s""", (name,))
    row = cur.fetchone()
    return (False, False, False) if row is None else (True, row[0], row[1])


def plan_of(cur, sid):
    cur.execute("EXPLAIN " + PROBE, (sid,))
    return "\n".join(r[0] for r in cur.fetchall())


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually build the index (default: report only)")
    args = ap.parse_args(argv[1:])

    import psycopg2
    conn = psycopg2.connect(_dsn(), connect_timeout=20)
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT to_regclass(%s)", (f"public.{TABLE}",))
    if not cur.fetchone()[0]:
        print(f"{TABLE} does not exist — nothing to do")
        return 2
    cur.execute(f"SELECT count(*) FROM {TABLE}")
    rows = cur.fetchone()[0]
    cur.execute("SELECT pg_size_pretty(pg_total_relation_size(%s))", (TABLE,))
    print(f"{TABLE}: {rows:,} rows, {cur.fetchone()[0]}")

    exists, valid, ready = index_state(cur, INDEX)
    if exists and valid:
        print(f"{INDEX} already exists and is VALID — nothing to do")
        return 0
    if exists and not valid:
        # ★ The trap. A leftover invalid index keeps its name, so
        # CREATE INDEX CONCURRENTLY IF NOT EXISTS would skip the rebuild and
        # every run from here on would report success over a dead index.
        print(f"{INDEX} exists but is INVALID (indisready={ready}) — a previous "
              f"build failed. It must be DROPPED before rebuilding; leaving it "
              f"makes IF NOT EXISTS skip forever.")
        if not args.apply:
            print(f"  would run: DROP INDEX CONCURRENTLY {INDEX}")
        else:
            cur.execute(f"DROP INDEX CONCURRENTLY {INDEX}")
            print(f"  dropped {INDEX}")

    cur.execute("SELECT surface_id FROM %s GROUP BY 1 ORDER BY count(*) DESC "
                "LIMIT 1" % TABLE)
    row = cur.fetchone()
    sid = row[0] if row else None

    if sid:
        before = plan_of(cur, sid)
        print(f"\nplan BEFORE (surface_id={sid!r}):")
        print("  " + before.splitlines()[0].strip())
        print(f"  seq scan present: {'Seq Scan' in before}")

    if not args.apply:
        print(f"\nDRY RUN — nothing was changed. Would run:\n  {DDL}")
        print("Re-run with --apply to build it.")
        return 0

    # A multi-GB CONCURRENTLY build must not be killed by a default
    # statement_timeout; lock_timeout stays bounded so it fails fast rather
    # than queueing behind a long transaction.
    cur.execute("SET statement_timeout = 0")
    cur.execute("SET lock_timeout = '30s'")
    print(f"\nbuilding (this reads the whole table twice; minutes, not seconds):\n  {DDL}")
    t0 = time.time()
    try:
        cur.execute(DDL)
    except Exception as exc:
        print(f"BUILD FAILED after {time.time()-t0:.0f}s: {exc}")
        e, v, _ = index_state(cur, INDEX)
        if e and not v:
            print(f"  {INDEX} was left INVALID — re-run this script, which will "
                  f"drop and rebuild it. Do NOT switch to IF NOT EXISTS.")
        return 1
    print(f"  built in {time.time()-t0:.0f}s")

    # ★ Read the schema BACK, and read the column that means "usable".
    exists, valid, ready = index_state(cur, INDEX)
    print(f"\nread-back: exists={exists} indisvalid={valid} indisready={ready}")
    if not (exists and valid):
        print("the index is not valid — the build reported success and did not "
              "land. This is why the check is pg_index, not pg_indexes.")
        return 1

    # ★ And check the OUTCOME, not the artifact. An index that exists but that
    # the planner ignores has fixed nothing.
    if sid:
        cur.execute("ANALYZE " + TABLE)
        after = plan_of(cur, sid)
        print(f"\nplan AFTER:\n  " + after.splitlines()[0].strip())
        if "Seq Scan" in after:
            print("  ⚠ the planner STILL chooses a sequential scan — the index "
                  "is valid but is not being used. Do not report this as fixed.")
            return 1
        print("  ✅ sequential scan is gone")

    cur.execute("SELECT pg_size_pretty(pg_relation_size(%s))", (INDEX,))
    print(f"\n{INDEX}: {cur.fetchone()[0]}")
    print(f"\n{SUPERSEDED} is now redundant — same leading columns. Drop it in a "
          f"FOLLOW-UP, after pg_stat_user_indexes shows this index taking the "
          f"traffic:\n  DROP INDEX CONCURRENTLY {SUPERSEDED};")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
