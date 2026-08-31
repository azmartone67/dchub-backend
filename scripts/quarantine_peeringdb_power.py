#!/usr/bin/env python3
"""Quarantine the synthesized peeringdb power_mw values.

WHY
---
5,855 facilities sourced `peeringdb` carry a power_mw the upstream does not
publish. Verified against the live PeeringDB API on 2026-08-31 — a fac object
has no power, capacity, MW or kW field anywhere:

    address1, address2, aka, available_voltage_services, campus_id,
    carrier_count, city, clli, country, created, diverse_serving_substations,
    floor, id, ix_count, latitude, logo, longitude, name, name_long, net_count,
    notes, npanxx, org_id, org_name, property, region_continent, rencode,
    sales_email, sales_phone, social_media, state, status, status_dashboard,
    suite, tech_email, tech_phone, updated, website, zipcode

The values are degenerate in exactly the way synthesized data is: 22 distinct
values across 5,855 rows, with 4,973 of them (85%) sitting at exactly 3.0 MW.
Genuinely sourced power looks nothing like that — operator_website has 46
distinct values with a 9% mode share, datacentermap 29 distinct at 11%.

All 5,855 were written on a single day, 2026-03-18, by a one-off backfill. No
peeringdb row created since 2026-07-23 has carried power, so the writer is
already inactive and this quarantine holds rather than needing a recurring sweep.

WHAT IT COSTS, STATED PLAINLY
-----------------------------
Published power coverage on live facilities drops from 33.2% to 6.9%. That is
not a regression — 6.9% is what we can actually source. The 33.2% was mostly
this.

It also removes a claim from ~4,445 public facility pages that currently read
"It carries a REPORTED power capacity of 3.0 MW", and from the meta description
Google shows in the SERP. Nobody reported it.

WHY NULL RATHER THAN DELETE
---------------------------
NULL is what the schema already uses for "unknown", every downstream COUNT/AVG/
SUM handles it correctly, and the facility page renders absent power cleanly
(the `if power` check is falsy). The row, its identity and its provenance are
untouched — only the unsourceable number goes.

THE SAFETY GUARD
----------------
This refuses to run unless the distribution still looks synthetic. If someone
later ingests REAL peeringdb power, the mode share collapses and this script
aborts rather than wiping it. That check is the reason this is safe to keep in
the repo rather than being a one-time hand-run UPDATE.

USAGE
-----
    python3 scripts/quarantine_peeringdb_power.py            # DRY RUN
    python3 scripts/quarantine_peeringdb_power.py --apply

Idempotent: a second --apply run finds 0 rows and exits clean.
"""

from __future__ import annotations

import argparse
import os
import sys

TABLE = "discovered_facilities"
SOURCE_MATCH = "peeringdb"          # matched case-insensitively

# A genuine multi-source power column spreads out. These thresholds describe
# the synthesized block as measured (22 distinct / 85% mode) with enough slack
# that ordinary drift will not trip them — but real data will.
MAX_DISTINCT_VALUES = 40
MIN_MODE_SHARE_PCT = 50.0

NOTE = ("power_mw quarantined 2026-08-31: value was not sourced — the PeeringDB "
        "API publishes no power/capacity field (verified live). 22 distinct "
        "values across 5,855 rows, 85% at exactly 3.0 MW, all written "
        "2026-03-18.")


def _dsn() -> str:
    dsn = (os.environ.get("NEON_DATABASE_URL")
           or os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("FATAL: NEON_DATABASE_URL / DATABASE_URL unset", file=sys.stderr)
        raise SystemExit(2)
    return dsn


def _survey(cur) -> dict:
    cur.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE power_mw IS NOT NULL),
               COUNT(DISTINCT power_mw) FILTER (WHERE power_mw IS NOT NULL)
          FROM {TABLE}
         WHERE LOWER(source) = %s
        """, (SOURCE_MATCH,))
    n, distinct = cur.fetchone()
    mode_pct = 0.0
    if n:
        cur.execute(
            f"""
            SELECT MAX(c) FROM (
              SELECT COUNT(*) c FROM {TABLE}
               WHERE LOWER(source) = %s AND power_mw IS NOT NULL
               GROUP BY power_mw) x
            """, (SOURCE_MATCH,))
        top = cur.fetchone()[0] or 0
        mode_pct = 100.0 * top / n
    return {"n": n or 0, "distinct": distinct or 0, "mode_pct": mode_pct}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Default is a dry run.")
    args = ap.parse_args()

    import psycopg2
    conn = psycopg2.connect(_dsn(), connect_timeout=15)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            before = _survey(cur)
            print(f"{TABLE} source~'{SOURCE_MATCH}':")
            print(f"  rows with power_mw : {before['n']:,}")
            print(f"  distinct values    : {before['distinct']}")
            print(f"  mode share         : {before['mode_pct']:.0f}%")

            if not before["n"]:
                print("\nnothing to quarantine — already clean")
                return 0

            # ── the guard ───────────────────────────────────────────
            if before["distinct"] > MAX_DISTINCT_VALUES:
                print(f"\nABORT: {before['distinct']} distinct values exceeds "
                      f"{MAX_DISTINCT_VALUES}. This no longer looks synthesized "
                      f"— someone may have ingested real power. Not touching it.")
                return 1
            if before["mode_pct"] < MIN_MODE_SHARE_PCT:
                print(f"\nABORT: mode share {before['mode_pct']:.0f}% is below "
                      f"{MIN_MODE_SHARE_PCT:.0f}%. The distribution looks real. "
                      f"Not touching it.")
                return 1

            if not args.apply:
                print(f"\nDRY RUN — would NULL {before['n']:,} power_mw values "
                      f"and stamp the reason into notes.")
                print("Re-run with --apply.")
                return 0

            cur.execute(
                f"""
                UPDATE {TABLE}
                   SET power_mw = NULL,
                       notes = CASE
                                 WHEN COALESCE(notes, '') = '' THEN %s
                                 ELSE notes || ' | ' || %s
                               END
                 WHERE LOWER(source) = %s AND power_mw IS NOT NULL
                """, (NOTE, NOTE, SOURCE_MATCH))
            changed = cur.rowcount
            after = _survey(cur)
            print(f"\n  updated {changed:,} rows")
            print(f"  remaining with power_mw: {after['n']:,}")
            if after["n"] != 0:
                print("  ABORT: values remain after the update — rolling back")
                conn.rollback()
                return 1
            conn.commit()
            print("  committed")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
