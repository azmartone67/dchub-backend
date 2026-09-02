#!/usr/bin/env python3
"""Step 2 of migrations/2026-08-12_substation_hifld_id_identity.sql — the
LINK-ONLY pass that populates substations.hifld_id for rows already held.

WHY THIS EXISTS. The March 2026 load stored the ArcGIS **OBJECTID** — the row
number of that particular export — in `hifld_objectid`, not the upstream HIFLD
`ID`. OBJECTID runs 1..79,687 and matched the real ID on 0 of 2,000 sampled
rows, so keyed on the new `hifld_id` column every upstream record reads as new
and a full ingest would insert ~75,000 duplicates. That is why
`land_power_crawler.SUBSTATION_WRITES_BLOCKED` is set, and why the nightly job
fetches 75,328 rows and upserts 0.

This pass writes ONE column. It inserts nothing and deletes nothing.

★ IT SKIPS AMBIGUITY RATHER THAN GUESSING. Rows are matched on coordinate
rounded to 4dp (~11 m), with NAME deliberately excluded — name is half
placeholder upstream, and including it was what made the 07-31 canary
extrapolate ~25,000 duplicates that were not real. Any coordinate key carrying
more than one asset on EITHER side is left alone: those are genuinely co-located
assets and need a human, not a tiebreak rule. Measured 2026-08-12: 110 ambiguous
upstream keys, 125 held.

SAFETY, in the order the migration requires:
  · dry run is the DEFAULT; --apply is required to write
  · --apply demands --expect-links N and refuses if the plan disagrees, so a
    changed upstream cannot silently rewrite a different number of rows
  · writes go in bounded batches and every batch asserts its own rowcount
  · the transaction rolls back on any mismatch

AFTER RUNNING, RE-MEASURE (step 3):
    SELECT COUNT(*) FROM substations WHERE source='HIFLD' AND hifld_id IS NULL;
It must fall to roughly 8,552 + the ambiguous residue. If it does not, STOP —
the pass did not do what it claims. Do NOT clear SUBSTATION_WRITES_BLOCKED until
that number has been read and understood; clearing it is step 4 and is
deliberately not automated here.

Usage:
    python3 tools/substation_hifld_link.py                      # dry run
    python3 tools/substation_hifld_link.py --apply --expect-links 66000
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

# The canonical HIFLD org. services1 is SUPERSEDED (52,244 lines vs 89,744) and
# its Electric_Substations layer 400/404s — see util/hifld_layers.py, which is
# the source of truth for layer URLs.
UPSTREAM = ("https://services5.arcgis.com/HDRa0B57OVrv2E1q/arcgis/rest/services/"
            "Electric_Substations/FeatureServer/0")

PRECISION = 4          # ~11 m. Fixed by the migration; do not tune casually.
BATCH = 5000


def coord_key(lat, lng, precision=PRECISION):
    """The match key. None for an unusable coordinate — never a bare 0,0."""
    if lat is None or lng is None:
        return None
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return None
    if lat == 0 and lng == 0:
        return None
    return (round(lat, precision), round(lng, precision))


def plan_links(upstream, held, precision=PRECISION):
    """Pure. upstream: [(hifld_id, lat, lng)]. held: [(row_id, lat, lng)].

    Returns a dict with the links to write and, separately, every reason a row
    was NOT linked. Nothing here touches a database or a network.
    """
    up_by_key = defaultdict(list)
    for hifld_id, lat, lng in upstream:
        key = coord_key(lat, lng, precision)
        if key is not None and hifld_id not in (None, ""):
            up_by_key[key].append(str(hifld_id))

    held_by_key = defaultdict(list)
    for row_id, lat, lng in held:
        key = coord_key(lat, lng, precision)
        if key is not None:
            held_by_key[key].append(row_id)

    links = []
    ambiguous_upstream = set()
    ambiguous_held = set()
    unmatched_held = []

    for key, row_ids in held_by_key.items():
        if len(row_ids) > 1:
            ambiguous_held.add(key)
            continue
        candidates = up_by_key.get(key)
        if not candidates:
            unmatched_held.append(row_ids[0])
            continue
        if len(set(candidates)) > 1:
            ambiguous_upstream.add(key)
            continue
        links.append((row_ids[0], candidates[0]))

    # A single upstream ID must never be written to two different held rows —
    # the partial unique index would reject it, but catching it here names the
    # cause instead of surfacing an IntegrityError mid-batch.
    seen = defaultdict(list)
    for row_id, hifld_id in links:
        seen[hifld_id].append(row_id)
    collisions = {h: r for h, r in seen.items() if len(r) > 1}
    links = [(r, h) for r, h in links if h not in collisions]

    return {
        "links": links,
        "ambiguous_upstream_keys": len(ambiguous_upstream),
        "ambiguous_held_keys": len(ambiguous_held),
        "unmatched_held": len(unmatched_held),
        "collisions": len(collisions),
        "upstream_keys": len(up_by_key),
        "held_keys": len(held_by_key),
    }


def fetch_upstream(url=UPSTREAM, page=2000):
    """Page the FeatureServer for (ID, lat, lng). Runner-side by convention."""
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "where": "1=1", "outFields": "ID", "returnGeometry": "true",
            "outSR": "4326", "f": "json",
            "resultOffset": offset, "resultRecordCount": page,
            "orderByFields": "ID",          # unordered paging overlaps
        })
        req = urllib.request.Request(f"{url}/query?{q}",
                                     headers={"User-Agent": "dchub-substation-link/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        if "error" in data:
            raise SystemExit(f"upstream error: {data['error']}")
        feats = data.get("features", [])
        for f in feats:
            geom = f.get("geometry") or {}
            out.append((f.get("attributes", {}).get("ID"), geom.get("y"), geom.get("x")))
        if not data.get("exceededTransferLimit") and len(feats) < page:
            break
        if not feats:
            break
        offset += len(feats)
    return out


def _held_rows(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, latitude, longitude
              FROM substations
             WHERE source = 'HIFLD' AND hifld_id IS NULL
        """)
        return cur.fetchall()


def _apply(conn, links, expect):
    if expect is None or len(links) != expect:
        raise SystemExit(
            f"REFUSING TO WRITE: planned {len(links)} links, --expect-links "
            f"{expect}. Re-run the dry run and pass the number you actually saw."
        )
    written = 0
    with conn:
        with conn.cursor() as cur:
            for i in range(0, len(links), BATCH):
                chunk = links[i:i + BATCH]
                cur.executemany(
                    "UPDATE substations SET hifld_id = %s "
                    " WHERE id = %s AND hifld_id IS NULL",
                    [(h, r) for r, h in chunk])
                if cur.rowcount != len(chunk):
                    raise SystemExit(
                        f"BATCH MISMATCH at offset {i}: expected {len(chunk)} "
                        f"rows, wrote {cur.rowcount}. Rolling back.")
                written += cur.rowcount
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--expect-links", type=int, default=None)
    ap.add_argument("--precision", type=int, default=PRECISION)
    args = ap.parse_args(argv)

    print(f"fetching upstream from {UPSTREAM} ...", flush=True)
    upstream = fetch_upstream()
    print(f"  upstream rows: {len(upstream)}")

    import psycopg2
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set")
    conn = psycopg2.connect(dsn)
    held = _held_rows(conn)
    print(f"  held rows needing a link: {len(held)}")

    plan = plan_links(upstream, held, args.precision)
    print(json.dumps({k: v for k, v in plan.items() if k != "links"}, indent=2))
    print(f"  LINKS PLANNED: {len(plan['links'])}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with:")
        print(f"  --apply --expect-links {len(plan['links'])}")
        return 0

    written = _apply(conn, plan["links"], args.expect_links)
    print(f"WROTE {written} links.")
    print("Now re-measure (step 3) before clearing SUBSTATION_WRITES_BLOCKED:")
    print("  SELECT COUNT(*) FROM substations WHERE source='HIFLD' AND hifld_id IS NULL;")
    return 0


if __name__ == "__main__":
    sys.exit(main())
