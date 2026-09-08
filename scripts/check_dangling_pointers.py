#!/usr/bin/env python3
"""check_dangling_pointers.py — does any consolidation pointer name a row the
site will not serve?

A `duplicate_of_id` / `discovered_twin_id` is only worth writing if the row it
names is one a reader can reach. Both consumers refuse a target that is
suppressed:

    routes/facility_profile_page._canonical_twin_row  -> COALESCE(is_duplicate,0)=0
    routes/facility_profile_page._twin_pointer_url    -> COALESCE(d.is_duplicate,0)=0
    main._build_sitemap_sections (_noncanon_slugs)    -> COALESCE(t.is_duplicate,0)=0

so a pointer at a suppressed row is a DEAD pointer: the page falls back to a
self-canonical and the row stays in the sitemap, while the alternate is
excluded from every `duplicate_of_id IS NULL` count and the target from every
`is_duplicate = 0` one. The facility is then counted ZERO times and
consolidated not at all — invisible in both directions, and silent.

    DATABASE_URL=... python3 scripts/check_dangling_pointers.py
    ... --json          machine-readable
    ... --max-rows 0    fail above this many dead pointers

Exit 0 clean · 1 over budget · 2 could not measure.

★ "Could not measure" is NEVER exit 0. This guard's PASS state is a count of
  zero, which is also what a broken query, an empty table and a typo'd column
  return. A scan whose clean result and whose failure result are the same
  number needs a FLOOR, or it is a green light wired to nothing. The three
  MIN_* constants below are that floor: each is a denominator this check joins
  through, and if one has collapsed the scan is not clean, it is blind.

MEASURED 2026-09-08, live, after the r-dangling repair:

    live rows carrying a duplicate_of_id        2,020    <- MIN_POINTERS floor
    suppressed rows                             7,683    <- MIN_SUPPRESSED floor
    legacy rows carrying a twin pointer           466    <- MIN_TWINS floor

    dangling: pointer -> suppressed row              0   <- the budget's basis
    dangling: pointer -> missing row                 0
    dangling: pointer -> target with no slug         0
    dangling: legacy twin -> suppressed keeper       0

★ THE BUDGET IS 0, WITH NO HEADROOM, and that is deliberate. Before the repair
  this same query returned 1,350 (1,283 whose target led nowhere, 67 whose
  target pointed onward). There is no such thing as an acceptable number of
  dead pointers — one is a facility that is consolidated nowhere and counted
  nowhere — so any headroom here would be headroom for the exact defect this
  exists to catch.

★ It covers FOUR shapes, not one. The repair found the suppressed-target shape;
  the other three are the same class reached differently, and a guard that
  pinned only the shape that happened to bite would go green while a pointer at
  a deleted row did the identical damage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# ── floors: the denominators this check joins THROUGH ────────────────
# Each is well below the live value and far above zero. A scan that sees
# fewer than these is not looking at the production corpus.
MIN_POINTERS = 1000      # live 2,020 — rows carrying a duplicate_of_id
MIN_SUPPRESSED = 4000    # live 7,683 — rows carrying is_duplicate = 1
MIN_TWINS = 200          # live   466 — legacy rows carrying a twin pointer

_COUNTS = {
    "live_rows_with_a_pointer": """
        SELECT count(*) FROM discovered_facilities
         WHERE COALESCE(is_duplicate,0)=0 AND duplicate_of_id IS NOT NULL""",
    "suppressed_rows": """
        SELECT count(*) FROM discovered_facilities
         WHERE COALESCE(is_duplicate,0)=1""",
    "legacy_rows_with_a_twin": """
        SELECT count(*) FROM facilities WHERE discovered_twin_id IS NOT NULL""",
}

_DANGLING = {
    # the shape the 2026-09-08 repair found: 1,350 rows
    "pointer_to_suppressed_row": """
        SELECT count(*) FROM discovered_facilities a
          JOIN discovered_facilities b ON b.id = a.duplicate_of_id
         WHERE COALESCE(a.is_duplicate,0)=0 AND COALESCE(b.is_duplicate,0)=1""",
    "pointer_to_missing_row": """
        SELECT count(*) FROM discovered_facilities a
         WHERE COALESCE(a.is_duplicate,0)=0 AND a.duplicate_of_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM discovered_facilities b
                            WHERE b.id = a.duplicate_of_id)""",
    "pointer_to_row_without_a_slug": """
        SELECT count(*) FROM discovered_facilities a
          JOIN discovered_facilities b ON b.id = a.duplicate_of_id
         WHERE COALESCE(a.is_duplicate,0)=0 AND COALESCE(b.is_duplicate,0)=0
           AND (b.canonical_slug IS NULL OR b.canonical_slug = '')""",
    "legacy_twin_to_suppressed_keeper": """
        SELECT count(*) FROM facilities f
          JOIN discovered_facilities d ON d.id = f.discovered_twin_id
         WHERE COALESCE(d.is_duplicate,0)=1""",
}


def _connect():
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return None, "DATABASE_URL is not set"
    try:
        import psycopg2
        return psycopg2.connect(dsn, sslmode="require", connect_timeout=20), None
    except Exception as e:                       # noqa: BLE001
        return None, f"connect failed: {str(e)[:160]}"


def measure(cur):
    out = {}
    for name, sql in list(_COUNTS.items()) + list(_DANGLING.items()):
        cur.execute(sql)
        out[name] = int(cur.fetchone()[0] or 0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-rows", type=int, default=0,
                    help="fail above this many dead pointers (default 0)")
    args = ap.parse_args()

    conn, err = _connect()
    if conn is None:
        # ★ exit 2, never 0 — see the module docstring.
        print(f"COULD NOT MEASURE: {err}", file=sys.stderr)
        return 2
    try:
        with conn.cursor() as cur:
            m = measure(cur)
    except Exception as e:                       # noqa: BLE001
        print(f"COULD NOT MEASURE: query failed: {str(e)[:200]}", file=sys.stderr)
        return 2
    finally:
        try: conn.close()
        except Exception: pass

    floors = [("live_rows_with_a_pointer", MIN_POINTERS),
              ("suppressed_rows", MIN_SUPPRESSED),
              ("legacy_rows_with_a_twin", MIN_TWINS)]
    blind = [f"{k}={m[k]} < floor {fl}" for k, fl in floors if m[k] < fl]

    total = sum(m[k] for k in _DANGLING)
    report = {"dangling_total": total, "max_rows": args.max_rows,
              "by_shape": {k: m[k] for k in _DANGLING},
              "denominators": {k: m[k] for k in _COUNTS},
              "blind": blind}

    if blind:
        report["result"] = "could_not_measure"
        print(json.dumps(report, indent=2) if args.json else
              "COULD NOT MEASURE: " + "; ".join(blind), file=sys.stderr)
        return 2

    report["result"] = "over_budget" if total > args.max_rows else "clean"
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"dangling pointers: {total} (budget {args.max_rows})")
        for k in _DANGLING:
            print(f"  {k:34} {m[k]}")
        for k, fl in floors:
            print(f"  [floor] {k:27} {m[k]} >= {fl}")
    return 1 if total > args.max_rows else 0


if __name__ == "__main__":
    sys.exit(main())
