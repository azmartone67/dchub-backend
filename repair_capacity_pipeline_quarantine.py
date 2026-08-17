#!/usr/bin/env python3
"""
repair_capacity_pipeline_quarantine.py — flag contaminated pipeline rows
=======================================================================
Pipeline-GW audit 2026-07-27. Mirrors the `deals` table's `data_flag` pattern.

THE PROBLEM
-----------
`SUM(capacity_mw) FROM capacity_pipeline` = 2,580.5 GW, published as 2,514 GW.
It is contaminated four ways:

  1. UTILITY AGGREGATES — AEP 63,000 MW, Dominion 48,000, PPL 25,200 are
     utilities' data-centre *interconnection-request queues*, summed as if each
     were one building. Plus impossible singles: Google Nevada 150,000 MW,
     NextEra/Dominion 130,000 MW.
  2. NON-PIPELINE STATUS — 'operational', 'acquisition', 'cancelled', 'lease'
     rows (~339 GW) inside a table named pipeline.
  3. DUPLICATES — 285 groups on (operator, market, capacity_mw); 487 redundant
     rows double-counting 761.0 GW.
  4. UNPARSED ROWS — 428 rows whose operator is blank/'Unknown'/'None'
     (546.0 GW), written by the dchub_pipeline extractor when entity extraction
     failed.

Strict cleanup lands at 486.4 GW — the published figure was 5.2x that.

WHAT THIS DOES
--------------
Adds a `data_flag` column and STAMPS the contaminated rows. It does NOT delete:
the utility interconnection-request figures are real and valuable, they are just
a *different metric* than project pipeline and need their own surface. Flag
precedence (first match wins), so each row carries one reason:

    quarantine_aggregate     capacity_mw >= 5000   (utility queue / unit error)
    quarantine_not_pipeline  status in operational/acquisition/cancelled/lease
    quarantine_unparsed      operator blank / Unknown / None
    quarantine_duplicate     redundant row within (operator, market, capacity_mw)

Consumers then sum `WHERE COALESCE(data_flag,'') = ''` for a defensible figure,
exactly like `deals`.

Since 2026-08-17 the extractor stamps arms 1–3 at insert time
(extractor_cron.insert_capacity, arms imported from util.capacity_pipeline),
so new extractor rows no longer wait for this script. This script remains the
periodic full recompute — the only place duplicates are numbered and stale
flags are re-derived (including UN-stamping rows whose data changed). It is
HUMAN-RUN by design: --apply resets every flag before restamping and writes
its rollback file to ~/Downloads, both of which assume an operator at the
keyboard. Do not register it as an unattended job as-is.

SAFETY
------
Dry-run by default; --apply to write. Writes an id->flag rollback JSON before
mutating. Single transaction. --rollback restores prior values (NULL for rows
that had none). Idempotent: re-running re-computes the same flags.

USAGE
    python3 repair_capacity_pipeline_quarantine.py            # dry run
    python3 repair_capacity_pipeline_quarantine.py --apply
    python3 repair_capacity_pipeline_quarantine.py --rollback FILE
"""
from __future__ import annotations

import os
import sys
import json
import datetime

from util.capacity_pipeline import cp_classify_arms

# Ordered: first match wins, so every row gets exactly one reason. Arms 1–3
# are the canonical taxonomy IMPORTED from util.capacity_pipeline — the same
# arms extractor_cron.insert_capacity stamps at insert time since 2026-08-17,
# so the two cannot drift. Only the duplicate arm lives here: it needs a
# window over the whole table, and the writer prevents duplicates with its
# twin probe instead (PR #2782). The imported arms test containment with
# strpos(lower(..)) rather than the original ILIKE '%unknown%' — identical
# for a fixed pattern, and %-free because the writer interpolates the arms
# into a parameterized statement.
CLASSIFY_SQL = """
WITH dup AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY lower(COALESCE(operator,'')),
                            lower(COALESCE(market,'')),
                            COALESCE(capacity_mw, -1)
               ORDER BY id
           ) AS rn
      FROM capacity_pipeline
)
SELECT c.id,
       CASE
         """ + cp_classify_arms("c") + """
         WHEN dup.rn > 1 THEN 'quarantine_duplicate'
         ELSE NULL
       END AS flag,
       c.capacity_mw
  FROM capacity_pipeline c
  JOIN dup ON dup.id = c.id
"""


def _conn(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn, sslmode="require", connect_timeout=10)


def _ensure_column(cur) -> None:
    cur.execute("ALTER TABLE capacity_pipeline "
                "ADD COLUMN IF NOT EXISTS data_flag VARCHAR(40)")


def rollback(dsn: str, path: str) -> int:
    doc = json.load(open(path))
    prior = doc["prior"]          # list of [id, flag_or_null]
    conn = _conn(dsn)
    try:
        with conn, conn.cursor() as cur:
            _ensure_column(cur)
            n = 0
            for pid, flag in prior:
                cur.execute("UPDATE capacity_pipeline SET data_flag=%s WHERE id=%s",
                            (flag, pid))
                n += cur.rowcount
        print(f"restored data_flag on {n} rows")
        return n
    finally:
        conn.close()


def main() -> int:
    dsn = os.environ.get("DATABASE_URL") or ""
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    if "--rollback" in sys.argv:
        rollback(dsn, sys.argv[sys.argv.index("--rollback") + 1])
        return 0

    apply = "--apply" in sys.argv
    conn = _conn(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout='300s'")
            if apply:
                _ensure_column(cur)
            else:
                # Dry run must not alter schema; classification needs no column.
                pass

            cur.execute(CLASSIFY_SQL)
            rows = cur.fetchall()

            buckets: dict = {}
            for _id, flag, mw in rows:
                k = flag or "CLEAN"
                b = buckets.setdefault(k, [0, 0.0])
                b[0] += 1
                b[1] += float(mw or 0)

            total_mw = sum(v[1] for v in buckets.values())
            print(f"{'bucket':28} {'rows':>6} {'GW':>9}   {'% of raw':>8}")
            print("-" * 58)
            for k in ("quarantine_aggregate", "quarantine_not_pipeline",
                      "quarantine_unparsed", "quarantine_duplicate", "CLEAN"):
                if k in buckets:
                    n, mw = buckets[k]
                    print(f"{k:28} {n:6,} {mw/1000:9.1f}   "
                          f"{100*mw/total_mw if total_mw else 0:7.1f}%")
            print("-" * 58)
            clean = buckets.get("CLEAN", [0, 0.0])
            print(f"{'RAW TOTAL':28} {len(rows):6,} {total_mw/1000:9.1f}")
            print(f"{'PUBLISHABLE (unflagged)':28} {clean[0]:6,} {clean[1]/1000:9.1f}")

            if not apply:
                print("\nDRY RUN — nothing written. Re-run with --apply.")
                return 0

            cur.execute("SELECT id, data_flag FROM capacity_pipeline")
            prior = [[r[0], r[1]] for r in cur.fetchall()]
            stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
            rb = os.path.expanduser(
                f"~/Downloads/capacity_pipeline_quarantine_rollback_{stamp}.json")
            json.dump({"generated_at_utc": stamp,
                       "note": "undo: python3 repair_capacity_pipeline_quarantine.py "
                               "--rollback <this file>",
                       "prior": prior}, open(rb, "w"))
            print(f"\nrollback file written: {rb}")

            # Reset then stamp, so the pass is idempotent.
            cur.execute("UPDATE capacity_pipeline SET data_flag = NULL")
            by_flag: dict = {}
            for _id, flag, _mw in rows:
                if flag:
                    by_flag.setdefault(flag, []).append(_id)
            for flag, ids in by_flag.items():
                cur.execute("UPDATE capacity_pipeline SET data_flag=%s "
                            "WHERE id = ANY(%s)", (flag, ids))
                print(f"  stamped {cur.rowcount:5,} rows -> {flag}")
            conn.commit()

            cur.execute("SELECT COALESCE(SUM(capacity_mw),0) FROM capacity_pipeline "
                        "WHERE COALESCE(data_flag,'')=''")
            print(f"\nAFTER — publishable SUM: "
                  f"{float(cur.fetchone()[0] or 0)/1000:,.1f} GW")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
