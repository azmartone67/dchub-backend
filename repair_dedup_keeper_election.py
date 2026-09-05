#!/usr/bin/env python3
"""
repair_dedup_keeper_election.py — restore facilities suppressed with no keeper
=============================================================================
Data QA 2026-07-27.

THE BUG
-------
`discovered_facilities` dedup flags rows `is_duplicate=1` but never guarantees a
SURVIVOR. Grouping by `canonical_slug` (the building identity):

    14,686  distinct facilities (COUNT(DISTINCT canonical_slug))
     5,368  have at least one row with is_duplicate=0   <- the only ones visible
     9,318  have EVERY row flagged is_duplicate=1       <- invisible entirely

So any is_duplicate-based count reports ~5,7xx and silently omits 9,318 real
facilities — including Meta Hyperion, STACK Dona Ana (Stargate), CoreWeave
Project Horizon, Microsoft Wisconsin AI Campus and AWS Mississippi, at
confidence 0.85-0.95. This is why "verified" collapsed to ~5.6k while the raw
pile stayed ~22.7k: the facilities did not vanish and were not removed as
duplicates — the pipeline suppressed whole clusters without electing a keeper.

Evidence that the flags are not trustworthy on their own:
  - 9,358 rows are flagged is_duplicate=1 with NO duplicate_of_id AND NO
    dedup_method — flagged by a legacy path that recorded nothing. Of those,
    3,715 have no merge evidence of any kind, and 2,197 have no name+city+country
    twin ANYWHERE in the table, so the flag cannot be justified at all.
  - 1,673 rows are the inverse contradiction: is_duplicate=0 while carrying a
    duplicate_of_id. Sampling shows the KEEPER holds the pointer to the twin it
    absorbed, so `duplicate_of_id IS NULL` (the filter behind the published
    13,395) wrongly drops legitimate keepers.

WHAT THIS DOES
--------------
For each canonical_slug group with no keeper, elects exactly ONE best row and
sets is_duplicate=0. It is purely additive: it never merges, deletes, or
re-flags anything, and it only touches groups that currently have no survivor.

Election order (deterministic, best evidence first):
    confidence_score DESC, has power_mw, has lat/lon, has city, has provider,
    then lowest id as a stable tiebreak.

SAFETY
------
  - Dry-run by default. Requires --apply to write.
  - Writes the exact id list to a rollback file BEFORE mutating, so the change
    reverts with a single UPDATE ... WHERE id IN (...).
  - Single transaction.
  - Uses DATABASE_URL (writes must go to the primary, not the read replica).

USAGE
-----
    python3 repair_dedup_keeper_election.py                 # dry run
    python3 repair_dedup_keeper_election.py --apply         # write
    python3 repair_dedup_keeper_election.py --rollback FILE  # undo

NOTE ON PUBLISHED NUMBERS
-------------------------
This does NOT move the number on /by-the-numbers: that binds total_facilities,
which derives from duplicate_of_id and is untouched here. It DOES move
`facilities_verified` / `facilities_with_keeper` (~5,737 -> ~14,964) and
therefore canonical_stats.facilities_verified_phrase() ("5,700+" -> "14,900+").
Confirm that phrase change is wanted before applying.
"""
from __future__ import annotations

import os
import sys
import json
import datetime
from utc_clock import utc_now

# Rows suppressed BECAUSE THEY ARE NOT FACILITIES are not candidates for
# election. Without this, the 34 scraped page titles that
# routes/facility_scrape_quality.py suppresses ('Equinix Smart Hands®',
# 'APAC', 'See our EMEA facilities') are each ALONE in their canonical_slug
# group — measured 34/34 on 2026-08-08 — so every one becomes a keeperless
# group the moment it is flagged, and the next run of this script elects the
# junk row as its own keeper and un-suppresses all of it. Election is for
# groups whose rows lost a DEDUP contest, not for rows that should never have
# existed. BOTH CTEs must filter: `nokeeper` decides which groups qualify,
# `ranked` decides which row wins, and a leak in either revives the row.
NOT_A_FACILITY_METHODS = ("pw_page_furniture",)

ELECTION_SQL = """
WITH eligible AS (
    SELECT *
      FROM discovered_facilities
     WHERE COALESCE(dedup_method, '') <> ALL(%(not_a_facility)s)
),
nokeeper AS (
    SELECT canonical_slug
      FROM eligible
     WHERE canonical_slug IS NOT NULL
     GROUP BY canonical_slug
    HAVING MIN(is_duplicate) = 1
),
ranked AS (
    SELECT d.id,
           d.canonical_slug,
           d.name,
           d.source,
           d.confidence_score,
           ROW_NUMBER() OVER (
               PARTITION BY d.canonical_slug
               ORDER BY COALESCE(d.confidence_score, 0) DESC,
                        (d.power_mw IS NOT NULL) DESC,
                        (d.latitude IS NOT NULL AND d.longitude IS NOT NULL) DESC,
                        (COALESCE(d.city, '') <> '') DESC,
                        (COALESCE(d.provider, '') <> '') DESC,
                        d.id ASC
           ) AS rn
      FROM eligible d
      JOIN nokeeper n ON n.canonical_slug = d.canonical_slug
)
SELECT id, canonical_slug, name, source, confidence_score
  FROM ranked
 WHERE rn = 1
 ORDER BY id
"""


def _conn(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn, sslmode="require", connect_timeout=10)


def _counts(cur) -> dict:
    out = {}
    cur.execute("SELECT COUNT(*) FROM discovered_facilities")
    out["records"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities "
                "WHERE canonical_slug IS NOT NULL")
    out["distinct_facilities"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                "WHERE COALESCE(is_duplicate,0)=0")
    out["rows_with_keeper_flag"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM ("
                "  SELECT canonical_slug FROM discovered_facilities"
                "   WHERE canonical_slug IS NOT NULL"
                "   GROUP BY canonical_slug HAVING MIN(is_duplicate)=1) z")
    out["facilities_with_no_keeper"] = cur.fetchone()[0]
    return out


def rollback(dsn: str, path: str) -> int:
    ids = json.load(open(path))["elected_ids"]
    if not ids:
        print("rollback file lists no ids; nothing to do")
        return 0
    conn = _conn(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE discovered_facilities SET is_duplicate=1 "
                        "WHERE id = ANY(%s)", (ids,))
            n = cur.rowcount
        print(f"rolled back {n} rows to is_duplicate=1")
        return n
    finally:
        conn.close()


def main() -> int:
    apply = "--apply" in sys.argv
    if "--rollback" in sys.argv:
        dsn = os.environ.get("DATABASE_URL") or ""
        if not dsn:
            print("DATABASE_URL not set", file=sys.stderr)
            return 2
        return 0 if rollback(dsn, sys.argv[sys.argv.index("--rollback") + 1]) >= 0 else 1

    dsn = os.environ.get("DATABASE_URL") or ""
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    conn = _conn(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout='300s'")
            before = _counts(cur)
            print("BEFORE:")
            for k, v in before.items():
                print(f"  {k:28} {v:,}")

            cur.execute(ELECTION_SQL,
                        {"not_a_facility": list(NOT_A_FACILITY_METHODS)})
            rows = cur.fetchall()
            ids = [r[0] for r in rows]
            print(f"\nWould elect {len(ids):,} keepers "
                  f"(one per canonical_slug group that currently has none).")

            conf = [float(r[4] or 0) for r in rows]
            print(f"  confidence >= 0.80 : {sum(1 for c in conf if c >= 0.80):,}")
            print(f"  confidence 0       : {sum(1 for c in conf if c == 0):,}")
            print("\n  sample winners:")
            for r in rows[:8]:
                print(f"    id={r[0]:<9} conf={r[4] or 0:<5} {(r[2] or '')[:44]:<44} [{r[3]}]")

            print(f"\nPROJECTED rows_with_keeper_flag: "
                  f"{before['rows_with_keeper_flag']:,} -> "
                  f"{before['rows_with_keeper_flag'] + len(ids):,}")

            if not apply:
                print("\nDRY RUN — nothing written. Re-run with --apply to write.")
                return 0

            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            rb = os.path.expanduser(
                f"~/Downloads/dedup_keeper_election_rollback_{stamp}.json")
            json.dump({"generated_at_utc": stamp,
                       "note": "undo: python3 repair_dedup_keeper_election.py "
                               "--rollback <this file>",
                       "before": before,
                       "elected_ids": ids},
                      open(rb, "w"), indent=1)
            print(f"\nrollback file written: {rb}")

            cur.execute("UPDATE discovered_facilities SET is_duplicate=0 "
                        "WHERE id = ANY(%s)", (ids,))
            print(f"UPDATE affected {cur.rowcount:,} rows")
            after = _counts(cur)
            conn.commit()
            print("\nAFTER:")
            for k, v in after.items():
                print(f"  {k:28} {v:,}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
