#!/usr/bin/env python3
"""
backfill_facility_states.py — fill the MISSING `state` on US discovered_facilities
rows from their coordinates, so the map / search / DCGI / /daily all see them.

WHY k-NN (nearest stated facility) instead of bounding boxes:
  The on-the-fly /daily fallback (routes/dcgi._lat_lng_to_state) uses state
  bounding boxes, which mis-assign in the dense DC corridor (Ashburn VA -> MD).
  k-NN inherits the state of the geographically NEAREST facility that already
  has one. Because data centers cluster tightly (Ashburn has hundreds of VA
  rows), a blank Ashburn row's nearest neighbour is a VA row -> correct VA.
  No GeoJSON / PostGIS needed; accurate for this clustered dataset.

SAFETY:
  * DRY-RUN by default. Pass --apply to write.
  * Fills ONLY blank states; the UPDATE re-checks the blank predicate, so it can
    never overwrite an existing state (idempotent, re-runnable).
  * Refuses to apply if the assignment count exceeds --max (runaway guard).
  * Single transaction; rolls back on any error.

Usage:
  python backfill_facility_states.py            # dry-run: report what it WOULD do
  python backfill_facility_states.py --apply     # write (fill blanks only)
"""
import os
import sys
import math

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL not set")

import psycopg2  # noqa: E402

APPLY = "--apply" in sys.argv
try:
    MAX = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 2000
except Exception:
    MAX = 2000

_US_ABBRS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'DC', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO',
    'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA',
    'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
}

_US_COUNTRY = "UPPER(TRIM(COALESCE(country,''))) IN ('US','USA','UNITED STATES')"
_VALID_COORD = ("latitude IS NOT NULL AND longitude IS NOT NULL "
                "AND latitude <> 0 AND longitude <> 0")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()

        # 1) reference set: US rows that already HAVE a clean state + coords
        cur.execute(f"""
            SELECT latitude, longitude, UPPER(TRIM(state))
            FROM discovered_facilities
            WHERE {_US_COUNTRY}
              AND state IS NOT NULL AND TRIM(state) <> ''
              AND {_VALID_COORD}
        """)
        ref = [(float(la), float(lo), st) for (la, lo, st) in cur.fetchall()
               if st in _US_ABBRS]
        print(f"reference (stated US rows w/ coords): {len(ref)}")
        if not ref:
            raise SystemExit("no reference rows — aborting (cannot k-NN)")

        # 2) targets: US rows MISSING a state but WITH coords
        cur.execute(f"""
            SELECT id, latitude, longitude, name
            FROM discovered_facilities
            WHERE {_US_COUNTRY}
              AND (state IS NULL OR TRIM(state) = '')
              AND {_VALID_COORD}
        """)
        targets = cur.fetchall()
        print(f"targets (blank-state US rows w/ coords): {len(targets)}")

        # 3) nearest-neighbour assignment
        assignments = []  # (id, state, name)
        for _id, la, lo, name in targets:
            la, lo = float(la), float(lo)
            best_st, best_d = None, None
            for rla, rlo, rst in ref:
                d = (la - rla) ** 2 + (lo - rlo) ** 2  # squared euclid; fine for nearest
                if best_d is None or d < best_d:
                    best_d, best_st = d, rst
            if best_st:
                # great-circle sanity: skip if nearest neighbour is absurdly far (>~3 deg)
                if best_d <= 9.0:
                    assignments.append((_id, best_st, name))

        # per-state tally
        tally = {}
        for _id, st, _n in assignments:
            tally[st] = tally.get(st, 0) + 1
        top = sorted(tally.items(), key=lambda kv: -kv[1])

        print(f"\nwould assign: {len(assignments)} rows across {len(tally)} states")
        print("top states:", ", ".join(f"{s}={n}" for s, n in top[:12]))
        print("sample assignments:")
        for _id, st, name in assignments[:10]:
            print(f"  id={_id} -> {st}   {str(name)[:40]!r}")

        if not APPLY:
            print("\nDRY-RUN — no rows written. Re-run with --apply to persist.")
            return

        if len(assignments) > MAX:
            raise SystemExit(f"REFUSING to apply: {len(assignments)} > --max {MAX} "
                             f"(runaway guard). Raise --max if this is expected.")

        # 4) apply — fill ONLY blanks (the AND-blank predicate makes it idempotent)
        n = 0
        for _id, st, _name in assignments:
            cur.execute("""
                UPDATE discovered_facilities
                SET state = %s
                WHERE id = %s AND (state IS NULL OR TRIM(state) = '')
            """, (st, _id))
            n += cur.rowcount
        conn.commit()
        print(f"\nAPPLIED — {n} rows updated (state filled from nearest neighbour).")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
