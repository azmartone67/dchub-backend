#!/usr/bin/env python3
"""
repair_one_url_many_rows.py — consolidate rows that already share one URL
=========================================================================
Data QA 2026-09-12 (r-one-url-many-rows).

THE BUG
-------
`discovered_facilities` holds groups of rows wearing ONE byte-identical
`canonical_slug` with NO consolidation of any kind: no `duplicate_of_id` on any
member, the `is_duplicate` flag unset, one page serving 200 at that slug.

MEASURED LIVE 2026-09-12, ids 12901642-12901646, "South Reach Networks Fort
Pierce", all five wearing `south-reach-networks-fort-pierce-d6d47cf4`:
  · /api/v1/facilities filters `duplicate_of_id IS NULL` and returned all five;
  · the IndexNow delta filters `COALESCE(is_duplicate,0)=0` and returned all
    five;
  → the free search preview is 5 results and ALL FIVE were that one building.
    search_facilities is the #1 agent tool. This is a product defect.

Globally, walking the public delta preview: 22,573 rows resolve to 20,504
distinct URLs. ~696 of the 2,069 redundant rows collapse inside a single
500-row window — identical slug, consecutive ids — concentrated in ids
12,816,692-12,902,079 (900 rows, 264 facilities, 4-5 copies each). Ids above
~12.95M are clean, so the tap has stopped; this repairs what it left.

WHY NO EXISTING LANE FIXES IT
-----------------------------
Every other lane looks for ONE facility published at SEVERAL URLs. This is the
inverse — one URL backed by several rows:
  repair_dedup_keeper_election  elects a keeper for groups with NONE; these have
                                five, so it sees nothing to do.
  facility_dedup_v3             wants an anonymous-provider twin; all members
                                share one real provider.
  facility_dedup_v4             keys on duplicate published URLs; this is ONE.
  _twin_redirect_target case B  needs slug_rows == 1; here it is 5.

WHAT THIS DOES
--------------
Per group, elects the best row and sets `duplicate_of_id = <keeper id>` on the
others. POINTER ONLY — it never sets `is_duplicate`, never deletes, never
re-slugs. Suppression deletes a page; a canonical merges it, and there is only
one page here to keep (see reference_dchub_dedup_suppression_truth: "consolidate
on duplicate_of_id ALONE").

★ THE PAGE DOES NOT MOVE. A pointer only redirects under
facility_profile_page._twin_redirect_target case B, which requires the keeper's
slug to be worn by exactly ONE row. Every member of these groups wears it, so
the condition is false and /facilities/<slug> keeps answering 200 at the same
URL, whichever member the page's DISTINCT ON picks. Proved against a real
Postgres in tests/test_one_url_many_rows_repair_sql.py, which runs this module's
own SQL and then asks the real served_slugs where the slug lands.

★★ IT MOVES A PUBLISHED NUMBER. Every count on `duplicate_of_id IS NULL` — the
public listing, `facilities_verified` — drops by the number of rows pointed
(~700 expected). That is the correction, not a side effect, but confirm it is
wanted before applying.

ONLY UNTOUCHED GROUPS
---------------------
A group qualifies only when NO member carries a `duplicate_of_id` at all. A
group where something already pointed somewhere has been decided by another
lane or a human, and this script leaves it alone.

SAFETY
------
  - Dry-run by default. Requires --apply to write.
  - Writes the exact (id -> keeper) list to a rollback file BEFORE mutating; the
    change reverts with one UPDATE ... SET duplicate_of_id = NULL.
  - Only ever writes rows whose duplicate_of_id IS NULL, so the rollback is
    exact: NULL is where they came from.
  - Single transaction. --max caps the blast radius.
  - Uses DATABASE_URL (writes go to the primary, not the read replica).

USAGE
-----
    python3 repair_one_url_many_rows.py                    # dry run
    python3 repair_one_url_many_rows.py --apply            # write
    python3 repair_one_url_many_rows.py --apply --max 100  # capped
    python3 repair_one_url_many_rows.py --rollback FILE     # undo
"""
from __future__ import annotations

import json
import os
import sys

from utc_clock import utc_now

# Groups where NOTHING has been consolidated yet: more than one row on the slug,
# no member carrying a pointer, and at least one member still live so the elected
# keeper cannot itself be a suppressed row.
GROUPS_CTE = """
groups AS (
    SELECT canonical_slug
      FROM discovered_facilities
     WHERE canonical_slug IS NOT NULL AND canonical_slug <> ''
     GROUP BY canonical_slug
    HAVING COUNT(*) > 1
       AND COUNT(duplicate_of_id) = 0
       AND MIN(COALESCE(is_duplicate, 0)) = 0
)
"""

# Election order, best evidence first. `is_duplicate = 0` leads: pointing live
# rows at a SUPPRESSED keeper would drop the facility out of both the pointer
# basis and the flag basis at once, and it would vanish from counts entirely.
ELECTION_SQL = f"""
WITH {GROUPS_CTE},
ranked AS (
    SELECT d.id, d.canonical_slug, d.name, d.provider, d.source,
           d.confidence_score,
           ROW_NUMBER() OVER (
               PARTITION BY d.canonical_slug
               ORDER BY (COALESCE(d.is_duplicate, 0) = 0) DESC,
                        COALESCE(d.confidence_score, 0) DESC,
                        (d.power_mw IS NOT NULL) DESC,
                        (d.latitude IS NOT NULL AND d.longitude IS NOT NULL) DESC,
                        (COALESCE(d.city, '') <> '') DESC,
                        (COALESCE(d.provider, '') <> '') DESC,
                        d.id ASC
           ) AS rn
      FROM discovered_facilities d
      JOIN groups g ON g.canonical_slug = d.canonical_slug
)
SELECT r.id, r.canonical_slug, r.name, r.source, k.id AS keeper_id
  FROM ranked r
  JOIN (SELECT canonical_slug, id FROM ranked WHERE rn = 1) k
    ON k.canonical_slug = r.canonical_slug
 WHERE r.rn > 1
 ORDER BY r.canonical_slug, r.id
"""

COUNTS_SQL = {
    "records": "SELECT COUNT(*) FROM discovered_facilities",
    "distinct_slugs": "SELECT COUNT(DISTINCT canonical_slug) FROM "
                      "discovered_facilities WHERE canonical_slug IS NOT NULL",
    "rows_no_pointer": "SELECT COUNT(*) FROM discovered_facilities "
                       "WHERE duplicate_of_id IS NULL",
    "unconsolidated_groups": f"SELECT COUNT(*) FROM (WITH {GROUPS_CTE} "
                             "SELECT canonical_slug FROM groups) z",
}


def _conn(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn, sslmode="require", connect_timeout=10)


def _counts(cur) -> dict:
    out = {}
    for key, sql in COUNTS_SQL.items():
        cur.execute(sql)
        out[key] = cur.fetchone()[0]
    return out


def rollback(dsn: str, path: str) -> int:
    ids = [int(p["id"]) for p in json.load(open(path))["pointed"]]
    if not ids:
        print("rollback file lists no ids; nothing to do")
        return 0
    conn = _conn(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE discovered_facilities SET duplicate_of_id = NULL "
                        "WHERE id = ANY(%s)", (ids,))
            n = cur.rowcount
        print(f"rolled back {n} rows to duplicate_of_id = NULL")
        return n
    finally:
        conn.close()


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    cap = None
    if "--max" in argv:
        cap = int(argv[argv.index("--max") + 1])

    dsn = os.environ.get("DATABASE_URL") or ""
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    if "--rollback" in argv:
        return 0 if rollback(dsn, argv[argv.index("--rollback") + 1]) >= 0 else 1

    conn = _conn(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout='300s'")
            before = _counts(cur)
            print("BEFORE:")
            for k, v in before.items():
                print(f"  {k:24} {v:,}")

            cur.execute(ELECTION_SQL)
            rows = cur.fetchall()
            if cap is not None:
                rows = rows[:cap]
            pointed = [{"id": int(r[0]), "keeper_id": int(r[4]),
                        "canonical_slug": r[1]} for r in rows]
            groups = len({r[1] for r in rows})
            print(f"\nWould point {len(pointed):,} rows at {groups:,} keepers "
                  "(one keeper per slug; pointer only, no flag).")
            print("\n  sample:")
            for r in rows[:8]:
                print(f"    id={r[0]:<10} -> keeper {r[4]:<10} "
                      f"{(r[2] or '')[:38]:<38} [{r[3]}]")

            print(f"\nPROJECTED rows_no_pointer (the PUBLISHED dedup basis): "
                  f"{before['rows_no_pointer']:,} -> "
                  f"{before['rows_no_pointer'] - len(pointed):,}")

            if not apply:
                print("\nDRY RUN — nothing written. Re-run with --apply to write.")
                return 0

            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            rb = os.path.expanduser(
                f"~/Downloads/one_url_many_rows_rollback_{stamp}.json")
            json.dump({"generated_at_utc": stamp,
                       "note": "undo: python3 repair_one_url_many_rows.py "
                               "--rollback <this file>",
                       "before": before, "pointed": pointed},
                      open(rb, "w"), indent=1)
            print(f"\nrollback file written: {rb}")

            for p in pointed:
                cur.execute("UPDATE discovered_facilities SET duplicate_of_id = %s "
                            " WHERE id = %s AND duplicate_of_id IS NULL",
                            (p["keeper_id"], p["id"]))
            after = _counts(cur)
            conn.commit()
            print("\nAFTER:")
            for k, v in after.items():
                print(f"  {k:24} {v:,}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
