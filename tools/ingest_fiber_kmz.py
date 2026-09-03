#!/usr/bin/env python3
"""Ingest surveyed fiber routes from KMZ/KML into fiber_routes.

The white-glove path: a carrier or a customer sends real route geometry, and
this puts it on the map with its polyline intact — the thing that makes a
carrier look real next to FiberLocator.

★ WHY A CLI AND NOT AN UPLOAD ENDPOINT. Admin POSTs through the Cloudflare edge
time out at 15s (ROUTE_TIMEOUTS DEFAULT). A real carrier export is 1-2 MB and
thousands of placemarks — a 1.7MB file measured here yields 737 routes — so an
edge-fronted upload would 503 while the origin kept working, which is the worst
of both. This runs from a shell (local or `railway run`) with no such ceiling.

★ WHY THIS MATTERS. The lane that writes most of fiber_routes
(infrastructure_discovery._save_route) never writes the `coordinates` column at
all — it stores endpoints only. That is why so much of the table is 2-point
straight lines: measured 2026-09-03, Uniti holds 69 routes of which 9 have more
than 2 vertices (max 5 points); 123Net 8 routes, max 5 points. The carriers that
look right on the map — Zayo (max 27,388 vertices), Bluebird (1,053), Peninsula
Fiber Network (1,569) — are precisely the ones loaded from surveyed KMZ.

Usage
-----
  # See what a file holds. DRY RUN IS THE DEFAULT — nothing is written.
  python3 tools/ingest_fiber_kmz.py ~/Downloads/*.kmz

  # Attribute a file whose placemarks carry no Owner field
  python3 tools/ingest_fiber_kmz.py route.kmz --provider "US Signal"

  # Actually write
  DATABASE_URL=... python3 tools/ingest_fiber_kmz.py route.kmz --provider Uniti --write

Idempotency
-----------
Each route is keyed by `upstream_uid` — an md5 of its own geometry (rounded to
~1m), carrier and name — under source='kmz'. Re-running the same file writes
nothing. This matches the identity rule set by
migrations/2026-08-12_fiber_route_upstream_uid.sql: identity comes from the
asset, never from the crawl or the filename that carried it.

Requires the partial unique index from that migration to be present for
ON CONFLICT to arbitrate; --write refuses to run if it is missing rather than
inserting duplicates.

★ THE FINGERPRINT IS FOLDED INTO `name` AND `source_id`, NOT JUST upstream_uid.
fiber_routes carries FIVE unique indexes, measured on production 2026-09-03:

    fiber_routes_name_provider_key      UNIQUE (name, provider)
    fiber_routes_name_provider_unique   UNIQUE (name, provider)   -- a twin
    fiber_routes_source_id_key          UNIQUE (source_id)        -- bare!
    idx_fiber_routes_source_id          UNIQUE (source, source_id)
    fiber_routes_upstream_uid_uniq      UNIQUE (source, upstream_uid) WHERE NOT NULL

`ON CONFLICT (source, upstream_uid)` arbitrates ONLY the last one; a collision
on any of the others RAISES. The first version of this tool stamped source_id =
the file's basename and used the placemark's raw name, so:

  - `UNIQUE (source_id)` alone meant only ONE row per file could ever insert;
  - KML placemarks routinely share a name — Google Earth's "Temporary Places"
    folder is the fallback label for every unnamed placemark in an export — so
    (name, provider) collapsed too.

Both fired against production on the first --write:
`duplicate key value violates unique constraint "fiber_routes_name_provider_key"
DETAIL: Key (name, provider)=(Temporary Places, C3NTRO) already exists.`

This is the SAME defect migrations/2026-08-12 documents in _save_route, which
synthesized `name` from owner/voltage/market and so held ~154 rows against 55k
of real data. The fix there and here is identical: fold the per-route
fingerprint into every key the table arbitrates on, so distinct physical
segments keep distinct keys.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.fiber_kmz import parse_bytes, storage_keys  # noqa: E402

DB_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")

# ★ THE CONFLICT TARGET IS NAMED ON PURPOSE, including the partial index's
# WHERE predicate so Postgres can infer fiber_routes_upstream_uid_uniq.
#
# A BARE `ON CONFLICT DO NOTHING` is legal SQL that simply never fires when no
# matching unique index exists — it does not error, it just inserts. That is
# exactly how fiber_kmz_routes reached 12,296,960 rows / 10 GB over ~70k
# distinct identities by 2026-08-22 (see
# tests/test_kmz_routes_identity_conflict.py). With the target named, a missing
# index RAISES on the first row instead of quietly duplicating the whole file
# on every run. The explicit _has_index() check below is the second line of
# defence, not the only one.
INSERT = """
INSERT INTO fiber_routes
    (name, provider, route_type, coordinates,
     start_lat, start_lng, end_lat, end_lng,
     distance_miles, source, source_id, upstream_uid)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source, upstream_uid) WHERE upstream_uid IS NOT NULL DO NOTHING
"""

# Written separately so the tool works whether or not the 2026-09-03 bbox
# migration has been applied (see _has_bbox_cols).
UPDATE_BBOX = """
UPDATE fiber_routes SET min_lat=%s, max_lat=%s, min_lng=%s, max_lng=%s
 WHERE source='kmz' AND upstream_uid=%s AND min_lat IS NULL
"""


def _expand(paths):
    out = []
    for p in paths:
        hits = glob.glob(os.path.expanduser(p))
        if not hits:
            print(f"  !! no such file: {p}", file=sys.stderr)
        for h in sorted(hits):
            if os.path.isdir(h):
                for ext in ("kmz", "kml", "KMZ", "KML"):
                    out.extend(sorted(glob.glob(os.path.join(h, "**", f"*.{ext}"),
                                                recursive=True)))
            else:
                out.append(h)
    # de-dup while preserving order
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def _has_index(cur):
    cur.execute("""SELECT COUNT(*) FROM pg_indexes
                    WHERE tablename='fiber_routes'
                      AND indexname='fiber_routes_upstream_uid_uniq'""")
    return cur.fetchone()[0] > 0


def _has_bbox_cols(cur):
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                    WHERE table_name='fiber_routes'
                      AND column_name IN ('min_lat','max_lat','min_lng','max_lng')""")
    return cur.fetchone()[0] == 4


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="KMZ/KML files, globs or directories")
    ap.add_argument("--provider", default=None,
                    help="Carrier for placemarks with no Owner/Provider field")
    ap.add_argument("--route-type", default="metro",
                    help="route_type for placemarks that don't declare one (default: metro)")
    ap.add_argument("--source-id", default=None,
                    help="source_id to stamp (default: the file's basename)")
    ap.add_argument("--min-vertices", type=int, default=2,
                    help="Skip routes with fewer vertices than this (default 2). "
                         "Use 3 to ingest only surveyed polylines and drop endpoint pairs.")
    ap.add_argument("--write", action="store_true",
                    help="Actually write. Without this the tool only reports.")
    args = ap.parse_args()

    files = _expand(args.paths)
    if not files:
        print("nothing to do")
        return 1

    all_routes, per_file = [], []
    for f in files:
        try:
            with open(f, "rb") as fh:
                raw = fh.read()
            routes = parse_bytes(raw, default_provider=args.provider,
                                 default_route_type=args.route_type)
        except Exception as e:
            print(f"  !! {os.path.basename(f)}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        routes = [r for r in routes if r["vertices"] >= args.min_vertices]
        origin = args.source_id or os.path.basename(f)
        for r in routes:
            # Keys the table arbitrates on must carry the per-route
            # fingerprint (see the module docstring). `origin` — the file this
            # arrived in — is kept only as a human-readable prefix; it is NOT
            # the identity, so re-exporting the same route under a different
            # filename still dedups on (source, upstream_uid).
            r["display_name"] = r["name"]
            r["name"], r["source_id"] = storage_keys(
                r["name"], r["upstream_uid"], origin)
            r["origin_file"] = origin
        per_file.append((f, routes))
        all_routes.extend(routes)

    print(f"\n{'file':<50} {'routes':>7} {'surveyed':>9} {'max_pts':>8} {'miles':>10}")
    print("-" * 88)
    for f, routes in per_file:
        surveyed = sum(1 for r in routes if r["vertices"] > 2)
        mx = max((r["vertices"] for r in routes), default=0)
        mi = sum(r["distance_miles"] for r in routes)
        print(f"{os.path.basename(f)[:48]:<50} {len(routes):>7} {surveyed:>9} {mx:>8} {mi:>10.1f}")

    prov = Counter(r["provider"] for r in all_routes)
    print(f"\nTOTAL: {len(all_routes)} routes, "
          f"{sum(1 for r in all_routes if r['vertices'] > 2)} with surveyed geometry")
    print("carriers:")
    for p, n in prov.most_common(15):
        print(f"   {n:6d}  {p}")
    if "Unknown" in prov:
        print(f"\n   NOTE: {prov['Unknown']} routes carry no Owner/Provider field. They will "
              f"be stored as 'Unknown' and will NOT appear under a carrier name on the map."
              f"\n   Re-run with --provider \"<Carrier>\" to attribute them.")

    # Duplicate uids WITHIN this batch: the same geometry exported twice.
    uids = Counter(r["upstream_uid"] for r in all_routes)
    dupes = sum(1 for n in uids.values() if n > 1)
    if dupes:
        print(f"\n   {dupes} route(s) appear more than once across these files "
              f"(identical geometry+name+carrier) — they will be stored once.")

    if not args.write:
        print("\nDRY RUN — nothing written. Re-run with --write to ingest.")
        return 0

    if not DB_URL:
        print("\nERROR: set NEON_DATABASE_URL or DATABASE_URL to write.", file=sys.stderr)
        return 2

    import psycopg2
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.autocommit = False
    cur = conn.cursor()

    if not _has_index(cur):
        print("\nERROR: unique index fiber_routes_upstream_uid_uniq is missing, so "
              "ON CONFLICT cannot dedup and this would insert duplicates.\n"
              "Apply migrations/2026-08-12_fiber_route_upstream_uid.sql first.",
              file=sys.stderr)
        conn.close()
        return 3
    bbox_cols = _has_bbox_cols(cur)

    inserted = 0
    seen = set()
    for r in all_routes:
        if r["upstream_uid"] in seen:
            continue
        seen.add(r["upstream_uid"])
        cur.execute(INSERT, (
            r["name"], r["provider"], r["route_type"],
            json.dumps(r["coordinates"]),
            r["start_lat"], r["start_lng"], r["end_lat"], r["end_lng"],
            r["distance_miles"], "kmz", r["source_id"], r["upstream_uid"],
        ))
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
            if bbox_cols:
                cur.execute(UPDATE_BBOX, (r["min_lat"], r["max_lat"],
                                          r["min_lng"], r["max_lng"],
                                          r["upstream_uid"]))
    conn.commit()

    print(f"\n✅ inserted {inserted} new routes "
          f"({len(seen) - inserted} already held, unchanged)")
    if not bbox_cols:
        print("   NOTE: bbox columns absent — apply "
              "migrations/2026-09-03_fiber_routes_bbox_columns.sql, which "
              "backfills them for these rows too.")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
