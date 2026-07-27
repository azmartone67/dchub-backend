#!/usr/bin/env python3
"""
repair_source_normalization.py — stop re-ingests from duplicating facilities
============================================================================
Data QA 2026-07-27.

THE BUG
-------
`discovered_facilities` has a UNIQUE index on (source, source_id), but the same
upstream record can be written under two different source spellings AND two
different source_id formats, so the key never matches and a second row is born:

    peeringdb   source_id=891       1-Net East  Singapore   (id 2325)
    PeeringDB   source_id=pdb_891   1-Net East  Singapore   (id 12142)

Row counts:  PeeringDB 5,940 | peeringdb 5,856 | OpenStreetMap 6,737 |
             openstreetmap 1,036

This is why the facility count read ~21-22k while only ~15.1k distinct
facilities exist. Nothing was ever deleted — the number was inflated from the
second ingest onward. Different loaders hardcode different `source` literals
(routes/osm_crawler.py writes 'openstreetmap'; another path wrote
'OpenStreetMap'), so fixing one loader cannot hold the invariant.

EVIDENCE THE pdb_ STRIP IS SAFE
-------------------------------
Of 5,856 plain-id `peeringdb` rows and 5,897 `pdb_`-prefixed `PeeringDB` rows,
5,849 numeric ids match across the two spellings; 5,801 of those share the same
name and 5,799 the same name AND city (99.1%). The two id spaces are the same
PeeringDB facility ids.

OSM IS DELIBERATELY NOT ID-NORMALISED
-------------------------------------
OSM ids carry object type: `osm_node_123` and `osm_way_123` are DIFFERENT
objects. Stripping the type would invent false merges. Only the source *case*
is normalised for OSM.

WHAT THIS DOES
--------------
  1. Flags the 5,849 redundant rows: is_duplicate=1 + duplicate_of_id -> keeper.
     Their (source, source_id) is left untouched so nothing violates the unique
     index. The keeper is the row already in normalised form.
  2. Normalises the remaining rows that need it (source case; pdb_ strip where
     it does not collide).
  3. Installs a BEFORE INSERT/UPDATE trigger so no writer — present, future, or
     unknown — can create a case-variant again. With it, a re-ingest writing
     ('PeeringDB','pdb_891') becomes ('peeringdb','891'), hits the unique index,
     and the loaders' ON CONFLICT (source, source_id) DO UPDATE fires as
     intended instead of inserting a twin.

NOT FIXED HERE
--------------
4,952 rows have a NULL source_id. Postgres permits unlimited NULLs in a unique
index, so those rows are entirely unconstrained and can still duplicate. They
come from news/manual paths with no stable upstream id and need a different
key (name+city+provider), which is a separate change.

SAFETY
------
Dry-run by default; --apply to write. Rollback JSON written before mutating.
Single transaction. --rollback restores prior source/source_id/is_duplicate/
duplicate_of_id and drops the trigger.

USAGE
    python3 repair_source_normalization.py             # dry run
    python3 repair_source_normalization.py --apply
    python3 repair_source_normalization.py --rollback FILE
"""
from __future__ import annotations

import os
import sys
import json
import datetime

NORM_SOURCE = "lower(btrim(source))"
NORM_SID = ("CASE WHEN lower(btrim(source))='peeringdb' AND source_id IS NOT NULL "
            "THEN regexp_replace(source_id,'^pdb_','') ELSE source_id END")

# Rows sharing a normalised key. Keeper = the row already closest to normal
# form (no rewrite needed), tie-broken by lowest id for determinism.
COLLISIONS_SQL = f"""
WITH n AS (
    SELECT id, source, source_id,
           {NORM_SOURCE} AS nsource,
           {NORM_SID}    AS nsid,
           (source = {NORM_SOURCE} AND source_id IS NOT DISTINCT FROM {NORM_SID})
                         AS already_normal
      FROM discovered_facilities
     WHERE source_id IS NOT NULL AND btrim(source_id) <> ''
), ranked AS (
    SELECT *, ROW_NUMBER() OVER (
               PARTITION BY nsource, nsid
               ORDER BY already_normal DESC, id ASC) AS rn,
           COUNT(*) OVER (PARTITION BY nsource, nsid) AS grp
      FROM n
)
SELECT id, source, source_id, nsource, nsid, rn, grp,
       FIRST_VALUE(id) OVER (PARTITION BY nsource, nsid
                             ORDER BY already_normal DESC, id ASC) AS keeper_id
  FROM ranked
 WHERE grp > 1
 ORDER BY nsource, nsid, rn
"""

# Rows that need normalising and have no collision.
SAFE_NORMALISE_SQL = f"""
WITH n AS (
    SELECT id, source, source_id,
           {NORM_SOURCE} AS nsource,
           {NORM_SID}    AS nsid
      FROM discovered_facilities
     WHERE source_id IS NOT NULL AND btrim(source_id) <> ''
), g AS (
    SELECT nsource, nsid, COUNT(*) AS grp FROM n GROUP BY 1,2
)
SELECT n.id, n.source, n.source_id, n.nsource, n.nsid
  FROM n JOIN g USING (nsource, nsid)
 WHERE g.grp = 1
   AND (n.source <> n.nsource OR n.source_id IS DISTINCT FROM n.nsid)
"""

TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION df_normalize_source() RETURNS trigger AS $fn$
BEGIN
    -- ★2026-07-27: the (source, source_id) unique index cannot dedup if two
    -- loaders spell the same upstream source differently. Normalise here so the
    -- invariant holds for every writer, including ones added later.
    IF NEW.source IS NOT NULL THEN
        NEW.source := lower(btrim(NEW.source));
    END IF;
    -- PeeringDB only: 'pdb_891' and '891' are the same PeeringDB facility id
    -- (verified: 5,849 id matches, 5,799 also matching name AND city).
    -- OSM is deliberately NOT id-normalised: osm_node_123 and osm_way_123 are
    -- different objects and stripping the type would invent false merges.
    IF NEW.source = 'peeringdb' AND NEW.source_id IS NOT NULL THEN
        NEW.source_id := regexp_replace(NEW.source_id, '^pdb_', '');
    END IF;
    RETURN NEW;
END $fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_df_normalize_source ON discovered_facilities;
CREATE TRIGGER trg_df_normalize_source
    BEFORE INSERT OR UPDATE OF source, source_id ON discovered_facilities
    FOR EACH ROW EXECUTE FUNCTION df_normalize_source();
"""


def _conn(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn, sslmode="require", connect_timeout=10)


def rollback(dsn: str, path: str) -> None:
    doc = json.load(open(path))
    conn = _conn(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DROP TRIGGER IF EXISTS trg_df_normalize_source "
                        "ON discovered_facilities")
            n = 0
            for row in doc["prior"]:
                rid, src, sid, isdup, dupof = row
                cur.execute("UPDATE discovered_facilities SET source=%s, source_id=%s,"
                            " is_duplicate=%s, duplicate_of_id=%s WHERE id=%s",
                            (src, sid, isdup, dupof, rid))
                n += cur.rowcount
            print(f"restored {n} rows; trigger dropped")
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

            cur.execute(COLLISIONS_SQL)
            coll = cur.fetchall()
            redundant = [r for r in coll if r[5] > 1]      # rn > 1
            keepers = {r[7] for r in coll}

            cur.execute(SAFE_NORMALISE_SQL)
            safe = cur.fetchall()

            print(f"{'collision groups':38} {len(keepers):>7,}")
            print(f"{'redundant rows -> flag as duplicate':38} {len(redundant):>7,}")
            print(f"{'non-colliding rows -> normalise in place':38} {len(safe):>7,}")
            by_src: dict = {}
            for r in redundant:
                by_src[r[1]] = by_src.get(r[1], 0) + 1
            print("\n  redundant rows by current source label:")
            for k, v in sorted(by_src.items(), key=lambda kv: -kv[1]):
                print(f"    {k:22} {v:6,}")
            print("\n  sample redundant -> keeper:")
            for r in redundant[:5]:
                print(f"    id={r[0]:<8} {r[1]}/{r[2]:<14} -> keeper id={r[7]}")

            if not apply:
                print("\nDRY RUN — nothing written. Re-run with --apply.")
                return 0

            ids = [r[0] for r in redundant] + [r[0] for r in safe]
            cur.execute("SELECT id, source, source_id, is_duplicate, duplicate_of_id "
                        "FROM discovered_facilities WHERE id = ANY(%s)", (ids,))
            prior = [list(r) for r in cur.fetchall()]
            stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
            rb = os.path.expanduser(
                f"~/Downloads/source_normalization_rollback_{stamp}.json")
            json.dump({"generated_at_utc": stamp,
                       "note": "undo: python3 repair_source_normalization.py "
                               "--rollback <this file>",
                       "prior": prior}, open(rb, "w"))
            print(f"\nrollback file written: {rb}")

            # 1. flag redundant rows (keys left untouched -> no unique violation)
            for r in redundant:
                cur.execute("UPDATE discovered_facilities SET is_duplicate=1,"
                            " duplicate_of_id=%s WHERE id=%s", (r[7], r[0]))
            print(f"  flagged {len(redundant):,} redundant rows")

            # 2. normalise the rest
            for r in safe:
                cur.execute("UPDATE discovered_facilities SET source=%s, source_id=%s"
                            " WHERE id=%s", (r[3], r[4], r[0]))
            print(f"  normalised {len(safe):,} rows")

            # 3. install the guard
            cur.execute(TRIGGER_SQL)
            print("  trigger trg_df_normalize_source installed")
            conn.commit()

            cur.execute("SELECT source, COUNT(*) FROM discovered_facilities "
                        "WHERE lower(source) IN ('peeringdb','openstreetmap') "
                        "GROUP BY 1 ORDER BY 2 DESC")
            print("\nAFTER — source labels:")
            for s, n in cur.fetchall():
                print(f"    {s:22} {n:6,}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
