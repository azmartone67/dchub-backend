#!/usr/bin/env python3
"""Convert placeholder power_mw = 0 to NULL in discovered_facilities and facilities.

WHY
---
Several loaders wrote 0 to mean "this source did not report capacity". A zero
that means "unknown" is not a harmless placeholder: it is counted as data by
every COUNT(power_mw), and it drags every AVG and SUM toward zero. Measured
2026-08-31 on live:

    discovered_facilities (is_duplicate=0)      facilities
      power_mw > 0    6,652  (33%)                dchub_pipeline      0 zeros
      power_mw = 0    7,002  (35%)                OpenStreetMap   5,805 zeros (86%)
      power_mw NULL   6,365  (32%)                PeeringDB       5,599 zeros (94%)

A plain COUNT(power_mw) on discovered_facilities reports 68% coverage. The real
figure is 33%. That gap is entirely these zeros, and it is the number the
facility-page and thin-content work depends on.

WHY 0 IS SAFE TO REINTERPRET
----------------------------
Because no source ever writes a small real value. Every source that produces
real capacity has a MINIMUM non-zero reading of 3.0-4.0 MW — nothing has ever
written a value between 0 and 3. And the sources with zeros divide cleanly:

    100% zeros, zero real values   PeeringDB, provider_directory,
                                   providerwebsites, global_directory,
                                   curated_global, datacenterhawk
    mostly zeros                   openstreetmap (4,693 of 5,258)
    mostly real                    peeringdb (5,277 real, 89 zeros)

A 0 MW data center does not exist. This is a sentinel, not a measurement.

WHAT THIS DOES NOT DO
---------------------
It does not invent capacity. Absence stays absence — it just becomes HONEST
absence, which the schema can already express and which every downstream
COUNT/AVG/SUM already handles correctly. Coverage does not improve; the
REPORTED coverage stops being wrong.

The loaders that produced these were fixed in the same change
(discovery_engine_v3's INSERT branch, its DDL default, BaxtelSource's sentinel,
and osm_crawler's record dict), so this is a one-time repair rather than a
recurring sweep.

USAGE
-----
    python3 scripts/repair_power_mw_placeholder_zeros.py            # DRY RUN
    python3 scripts/repair_power_mw_placeholder_zeros.py --apply

Idempotent: a second --apply run updates 0 rows.
"""

from __future__ import annotations

import argparse
import os
import sys

TABLES = ("discovered_facilities", "facilities")

# Nothing has ever written a real value below this. A row at or under it that is
# exactly 0 is a sentinel; anything above is a measurement and is left alone.
# Deliberately an EQUALITY on 0 rather than a `< 3` range: reinterpreting a
# 1.5 MW reading as unknown would be inventing absence, which is the same class
# of error in the other direction.
SENTINEL = 0.0


def _dsn() -> str:
    dsn = (os.environ.get("NEON_DATABASE_URL")
           or os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("FATAL: NEON_DATABASE_URL / DATABASE_URL unset", file=sys.stderr)
        raise SystemExit(2)
    return dsn


def _survey(cur, table: str) -> dict:
    cur.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE power_mw > 0),
               COUNT(*) FILTER (WHERE power_mw = 0),
               COUNT(*) FILTER (WHERE power_mw IS NULL),
               COUNT(*),
               MIN(power_mw) FILTER (WHERE power_mw > 0)
          FROM {table}
        """)
    real, zero, nul, total, min_real = cur.fetchone()
    return {"real": real, "zero": zero, "null": nul, "total": total,
            "min_real": min_real}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Default is a dry run.")
    args = ap.parse_args()

    import psycopg2
    conn = psycopg2.connect(_dsn(), connect_timeout=15)
    conn.autocommit = False
    rc = 0
    try:
        with conn.cursor() as cur:
            for table in TABLES:
                try:
                    before = _survey(cur, table)
                except Exception as e:
                    print(f"{table}: SKIP ({type(e).__name__}: {str(e)[:80]})")
                    conn.rollback()
                    continue

                print(f"\n{table}")
                print(f"  before: real={before['real']:,}  "
                      f"zero={before['zero']:,}  null={before['null']:,}  "
                      f"total={before['total']:,}")
                if before["min_real"] is not None:
                    print(f"  smallest real reading: {before['min_real']} MW")
                    # The safety argument, asserted rather than assumed.
                    if float(before["min_real"]) <= SENTINEL:
                        print("  ABORT: a real reading is <= the sentinel — "
                              "0 may be meaningful here. Not touching this table.")
                        rc = 1
                        continue

                if not before["zero"]:
                    print("  nothing to repair")
                    continue

                if not args.apply:
                    print(f"  DRY RUN — would set {before['zero']:,} rows to NULL")
                    continue

                cur.execute(
                    f"UPDATE {table} SET power_mw = NULL WHERE power_mw = 0")
                changed = cur.rowcount
                after = _survey(cur, table)
                print(f"  updated {changed:,} rows")
                print(f"  after:  real={after['real']:,}  "
                      f"zero={after['zero']:,}  null={after['null']:,}")
                # Real readings must be untouched — this only reinterprets 0.
                if after["real"] != before["real"]:
                    print("  ABORT: real-value count changed — rolling back")
                    conn.rollback()
                    return 1
                if after["zero"] != 0:
                    print("  ABORT: zeros remain after the update — rolling back")
                    conn.rollback()
                    return 1

            if args.apply:
                conn.commit()
                print("\ncommitted")
            else:
                conn.rollback()
                print("\ndry run — nothing written. Re-run with --apply.")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
