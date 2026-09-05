#!/usr/bin/env python3
"""
repair_thin_twin_canonical.py — point every empty facility page's canonical at
its populated twin, and stop populated pages pointing at empty ones
=============================================================================
Data QA 2026-08-14.

THE POPULATION
--------------
533 live rows in `discovered_facilities` are the same physical facility as
another live row: identical LOWER(TRIM(name)) + LOWER(TRIM(city)), one with
power_mw > 0 and one without, both COALESCE(is_duplicate,0)=0. Measured
2026-08-14 against production:

    PeeringDB       1,049 thin rows    444 match a populated row
    openstreetmap   5,082 thin rows     75 match
    peeringdb          89 thin rows     14 match
                                       ---
                                       533

They are the same building, not a coincidence of naming:

  - EVERY one of the 533 matches EXACTLY ONE populated row. Zero ambiguity,
    so there is no "which twin wins" question to get wrong.
  - Of the 499 pairs where both rows carry coordinates, 496 are under 1 km
    apart and the other 3 under 5 km. None is further. The 34 remaining pairs
    have coordinates on only one side.
  - 532/533 agree on country, 520/533 on state.
  - Only 12/533 agree on PROVIDER — which is the root cause. The existing
    dedup keys on provider|name, and the two ingests spell the operator
    differently ("Equinix Inc" vs "Equinix", "QTS Realty" vs "QTS"), so the
    key never collides and both rows survive.

★ THE 533 ARE NOT THE SEO PROBLEM. 524 OF THEM ARE ALREADY HANDLED.
------------------------------------------------------------------
524 of the 533 already carry duplicate_of_id pointing at exactly the populated
twin. routes/facility_profile_page.py::_canonical_twin_url therefore already
serves rel=canonical at the twin's URL, and _build_sitemap_sections already
drops them via _noncanon_slugs (r-selfcanon, 2026-08-01). Verified live
2026-08-14 as Googlebot, cache-busted:

    /facilities/equinix-inc-equinix-da6-dallas-b32f6cd6   200
        canonical -> /facilities/equinix-equinix-da6-dallas-462cea2f
    /facilities/qts-realty-trust-inc-qts-richmond-ric1-43a9597d   200
        canonical -> /facilities/qts-qts-richmond-ric1-f363022b
    /facilities/cogent-cogent-toulouse-f3e8a1a5   200
        canonical -> /facilities/cogent-communications-inc-cogent-toulouse-…

Those are "Alternate page with proper canonical" in GSC, not "Duplicate without
user-selected canonical". Only NINE of the 533 misbehave, and this script fixes
exactly those nine plus a second defect the measurement turned up.

DEFECT A — nine rows that never consolidate (9 rows)
----------------------------------------------------
    4 carry no duplicate_of_id at all           -> self-canonical
    3 point at a row that is itself is_duplicate=1; _canonical_twin_url
      requires a live target, returns None      -> self-canonical
    2 point at another THIN row instead of the populated twin

Verified live 2026-08-14 — the self-canonical ones, as predicted:

    /facilities/qts-realty-qts-sacramento-3794986a   200
        canonical -> itself
    /facilities/switch-switch-las-vegas-8-1630bb5d   200
        canonical -> itself

DEFECT B — 24 BACKWARDS canonicals (24 rows)
--------------------------------------------
The measurement for A turned up the inverse, and it is the more damaging one:
24 live rows WITH capacity carry a duplicate_of_id resolving to a live row
WITHOUT capacity, so the page that can rank hands its canonical to the page
that cannot — and the empty target is exactly what PR #2655's capacity gate
removed from the sitemap. We submit the good page and tell Google the real one
is a URL we deliberately withheld.

    291   Switch Las Vegas 8   130 MW -> 19502   0 MW
    468   AirTrunk SYD2        120 MW -> 10907   0 MW
    473   AirTrunk TOK1         80 MW -> 10910   0 MW
    471   AirTrunk SGP2         70 MW -> 11035   0 MW
    526   QTS Sacramento        55 MW ->  8430   0 MW
    …24 rows, 130 MW down to 3 MW

★ B IS ALSO WHY A CANNOT BE SHIPPED ALONE. Five of B's targets ARE thin rows in
A (19502, 8430, 7925, and 317/526 as A's targets). Setting A's pointer
thin->populated while B still holds populated->thin makes a two-row canonical
CYCLE, which Google reads as no canonical at all — strictly worse than the
self-canonical it replaces. There are zero live-to-live cycles today; A alone
would create five.

  ★ It is ATOMICITY that matters here, not order. The two id sets are disjoint
    by construction — A only ever writes rows with power_mw = 0, B only rows
    with power_mw > 0 — and both SELECTs run before either UPDATE, so applying
    them in either order lands the same final state. What must not happen is A
    committing without B. Hence one transaction, and a cycle re-check inside it.
    (I first wrote this comment claiming B had to run FIRST; the fixture in
    tests/ disproved it, which is why that test runs both orders.)

WHAT THIS DOES NOT DO
---------------------
★ IT DOES NOT SET is_duplicate. That was proposed and it is the change this
repo already made and reverted. From facility_profile_page.py, 2026-07-28:

    "consolidate on duplicate_of_id ALONE, not on is_duplicate. Setting
     is_duplicate=1 is a VISIBILITY flag — it drops the row from every
     is_duplicate-filtered count and from the sitemap, which is exactly how
     9,318 facilities went missing and why repair_dedup_keeper_election.py had
     to elect keepers on 2026-07-27. I reproduced that bug at small scale: 57
     of 58 slugs I flagged were left with no keeper, and it was reverted."

Measured cost of doing it anyway, 2026-08-14: canonical_stats.facilities_verified
is COUNT(DISTINCT canonical_slug) WHERE COALESCE(is_duplicate,0)=0, and 527 of
the 533 hold a canonical_slug distinct from their twin's. Flagging them takes
that number 17,864 -> 17,337 — through the "17,500+" the MCP server instructions
and the registry listings advertise. A duplicate_of_id repair moves it by zero,
because facilities_verified does not read duplicate_of_id.

★ IT DOES NOT COPY power_mw ACROSS. That would re-admit the empty row to the
sitemap as a second URL for the same building — the duplicate problem, made
worse, by the change that looks like it fixes it.

SAFETY
------
  - Dry-run by default. Requires --apply to write.
  - Writes every (id, old value) pair to a rollback file BEFORE mutating.
  - Single transaction; A and B land together or not at all.
  - Refuses to write a self-pointer or a two-row cycle, and re-checks for
    cycles after the UPDATE, inside the transaction, rolling back if any
    appeared.
  - Uses DATABASE_URL (writes go to the primary, not the read replica).

USAGE
-----
    python3 repair_thin_twin_canonical.py                  # dry run
    python3 repair_thin_twin_canonical.py --apply          # write
    python3 repair_thin_twin_canonical.py --rollback FILE  # undo
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from utc_clock import utc_now

# ─────────────────────────────────────────────────────────────────────────────
# ★ EVERY query below carries COALESCE(is_duplicate,0)=0 ON BOTH SIDES. A
#   flagged row is neither a candidate nor a valid target: _canonical_twin_url
#   requires a live target and returns None otherwise, which is precisely how
#   three of defect A's rows ended up self-canonical in the first place. A
#   repair that pointed at a flagged row would reproduce the bug it is fixing.
# ─────────────────────────────────────────────────────────────────────────────

# DEFECT B, selected first: a live row WITH capacity whose duplicate_of_id
# resolves to a live row WITHOUT capacity. Direction is the whole point — the
# comparison is on power_mw, not on id order or ingest date.
BACKWARDS_SQL = """
SELECT d.id, d.power_mw, d.canonical_slug,
       t.id, COALESCE(t.power_mw, 0), t.canonical_slug
  FROM discovered_facilities d
  JOIN discovered_facilities t ON t.id = d.duplicate_of_id
 WHERE COALESCE(d.is_duplicate, 0) = 0
   AND COALESCE(t.is_duplicate, 0) = 0
   AND t.canonical_slug IS NOT NULL AND t.canonical_slug <> ''
   AND d.canonical_slug IS NOT NULL AND d.canonical_slug <> ''
   AND t.canonical_slug <> d.canonical_slug
   AND d.power_mw > 0
   AND COALESCE(t.power_mw, 0) = 0
 ORDER BY d.power_mw DESC, d.id
"""

# DEFECT A: a live thin row with exactly one live populated name+city twin,
# whose duplicate_of_id is not already that twin.
#
# ★ The COUNT(*) = 1 is load-bearing, not decoration. It is true of all 533
#   today, but a future ingest that lands two populated rows for one name+city
#   would otherwise make the winner depend on scan order — the same
#   nondeterminism test_sitemap_thin_gate.py documents for Equinix PA2/PA3.
#   Ambiguous rows are reported and skipped, never guessed.
#
# ★ THE NORMALISED KEYS ARE MATERIALISED IN `live` ON PURPOSE. Joining on
#   LOWER(TRIM(name)) directly reads better and is 300x slower: no index covers
#   the expression, so the planner nested-loops 17.9k x 17.9k and re-evaluates
#   the functions per probe. Timed 2026-08-14: >30s and still running as a
#   direct expression join, 0.9s in this form. The brief that opened this work
#   reported a 400s timeout on the same query — that is the same cause, not a
#   big table.
THIN_TWIN_SQL = """
WITH live AS (
    SELECT id, power_mw, canonical_slug, duplicate_of_id,
           LOWER(TRIM(name)) AS k_name,
           LOWER(TRIM(city)) AS k_city
      FROM discovered_facilities
     WHERE COALESCE(is_duplicate, 0) = 0
       AND name IS NOT NULL AND TRIM(name) <> ''
       AND city IS NOT NULL AND TRIM(city) <> ''
       AND canonical_slug IS NOT NULL AND canonical_slug <> ''
),
-- The `p.canonical_slug <> t.canonical_slug` below drops 6 of the 533: twins
-- that already share ONE canonical_slug (e.g. 17466/411 Equinix AM3). They emit
-- a single URL between them, so there is no duplicate URL to consolidate and a
-- pointer would only add a self-referential canonical. 527 remain.
thin AS (SELECT * FROM live WHERE COALESCE(power_mw, 0) = 0),
pop  AS (SELECT * FROM live WHERE power_mw > 0),
pairs AS (
    SELECT t.id             AS thin_id,
           t.canonical_slug AS thin_slug,
           t.duplicate_of_id AS thin_dup_of,
           MIN(p.id)        AS twin_id,
           COUNT(*)         AS n_twins
      FROM thin t
      JOIN pop p ON p.k_name = t.k_name
                AND p.k_city = t.k_city
                AND p.canonical_slug <> t.canonical_slug
     GROUP BY t.id, t.canonical_slug, t.duplicate_of_id
)
SELECT pairs.thin_id, pairs.thin_slug, pairs.thin_dup_of,
       pairs.twin_id, p.canonical_slug, p.power_mw, pairs.n_twins
  FROM pairs
  JOIN discovered_facilities p ON p.id = pairs.twin_id
 ORDER BY pairs.thin_id
"""

# Post-UPDATE assertion, run inside the transaction. A single hop is all
# _canonical_twin_url does, so a two-row cycle is the failure that matters:
# both pages would name each other and neither would consolidate.
#
# ★ LIVE ON BOTH SIDES, and the result is compared BEFORE vs AFTER rather than
#   asserted to be zero. Live-to-live cycles are 0 today, but the table already
#   holds one mutual pair across the flag boundary (17370 <-> 7579, measured
#   2026-08-14; 7579 is is_duplicate=1, so _canonical_twin_url rejects it and
#   17370 self-canonicals — inert, and not this script's defect). A
#   zero-assertion would abort every run on that pair; a delta assertion fails
#   only on a cycle this run actually introduced.
CYCLE_CHECK_SQL = """
SELECT a.id, b.id
  FROM discovered_facilities a
  JOIN discovered_facilities b ON b.id = a.duplicate_of_id
 WHERE COALESCE(a.is_duplicate, 0) = 0
   AND COALESCE(b.is_duplicate, 0) = 0
   AND (b.duplicate_of_id = a.id OR a.duplicate_of_id = a.id)
 LIMIT 20
"""


def plan(pair_rows):
    """Split THIN_TWIN_SQL's rows into (repair, already_correct, ambiguous).

    ★ A PURE FUNCTION ON PURPOSE. This is the decision the script actually
    makes, and it is the one worth guarding: which rows get written. Left
    inline in main() the only way to test it is to re-implement it in the test,
    which proves the test agrees with itself. Verified 2026-08-14 — a mutation
    that dropped the ambiguity filter from an inline version of this survived
    the guard green.
    """
    repair, already, ambiguous = [], [], []
    for r in pair_rows:
        thin_id, _slug, old_target, twin_id, _twin_slug, _mw, n_twins = r
        if n_twins != 1:
            ambiguous.append(r)          # a winner would be scan order, not evidence
        elif old_target == twin_id:
            already.append(r)
        else:
            repair.append(r)
    return repair, already, ambiguous


def _conn(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn, sslmode="require", connect_timeout=10)


def _counts(cur) -> dict:
    """The numbers a reviewer will want to see move — and the one that must not.

    facilities_verified mirrors canonical_stats.py exactly. It is here to prove
    the repair leaves the advertised count alone, which is the whole reason this
    is a duplicate_of_id change and not an is_duplicate change.
    """
    out = {}
    cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                "WHERE COALESCE(is_duplicate,0)=0")
    out["live_rows"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities "
                "WHERE COALESCE(is_duplicate,0)=0 AND canonical_slug IS NOT NULL")
    out["facilities_verified"] = cur.fetchone()[0]
    cur.execute(BACKWARDS_SQL)
    out["backwards_canonicals"] = len(cur.fetchall())
    cur.execute(CYCLE_CHECK_SQL)
    out["pointer_cycles"] = len(cur.fetchall())
    return out


def rollback(dsn: str, path: str) -> int:
    """Restore every duplicate_of_id this script changed, to its prior value."""
    payload = json.load(open(path))
    changes = payload["changes"]           # [[id, old_duplicate_of_id], ...]
    if not changes:
        print("rollback file lists no changes; nothing to do")
        return 0
    conn = _conn(dsn)
    try:
        with conn, conn.cursor() as cur:
            n = 0
            for row_id, old in changes:
                cur.execute("UPDATE discovered_facilities SET duplicate_of_id = %s "
                            "WHERE id = %s", (old, row_id))
                n += cur.rowcount
        print(f"rolled back duplicate_of_id on {n} rows")
        return n
    finally:
        conn.close()


def main() -> int:
    if "--rollback" in sys.argv:
        dsn = os.environ.get("DATABASE_URL") or ""
        if not dsn:
            print("DATABASE_URL not set", file=sys.stderr)
            return 2
        rollback(dsn, sys.argv[sys.argv.index("--rollback") + 1])
        return 0

    apply = "--apply" in sys.argv
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
                print(f"  {k:24} {v:,}")

            # ── DEFECT B ────────────────────────────────────────────────────
            cur.execute(BACKWARDS_SQL)
            back = cur.fetchall()
            print(f"\nB. {len(back):,} populated rows canonicalise to an EMPTY twin.")
            print("   Clearing duplicate_of_id so each keeps its own canonical.")
            for r in back[:10]:
                print(f"     id={r[0]:<8} {r[1]:>7} MW  ->  id={r[3]:<8} {r[4]:>5} MW"
                      f"   {(r[5] or '')[:46]}")
            if len(back) > 10:
                print(f"     … and {len(back) - 10:,} more")

            # ── DEFECT A ────────────────────────────────────────────────────
            cur.execute(THIN_TWIN_SQL)
            todo, already, ambiguous = plan(cur.fetchall())

            print(f"\nA. {len(todo) + len(already):,} thin rows have exactly one "
                  f"populated name+city twin at a DIFFERENT canonical_slug.")
            print(f"   {len(already):,} already point at it — untouched.")
            print(f"   {len(todo):,} do not, and are repaired:")
            for r in todo:
                was = r[2] if r[2] is not None else "none"
                print(f"     id={r[0]:<9} dup_of {str(was):<8} -> {r[3]:<8}"
                      f"  {(r[4] or '')[:44]}  ({r[5]} MW)")
            if ambiguous:
                print(f"   ★ {len(ambiguous):,} SKIPPED as ambiguous (more than one "
                      f"populated twin — a winner would be scan order, not evidence):")
                for r in ambiguous[:10]:
                    print(f"     id={r[0]} has {r[6]} populated twins")

            # ── The cycle that a naive fix would create ─────────────────────
            back_ids = {r[0] for r in back}
            would_cycle = [r for r in todo if r[3] in back_ids]
            if would_cycle:
                print(f"\n★ {len(would_cycle)} of A's targets are in B. Clearing B "
                      f"in the SAME transaction is what stops these becoming "
                      f"canonical cycles — A alone would create them:")
                for r in would_cycle:
                    print(f"     {r[0]} -> {r[3]}, and {r[3]} currently points back")

            changes = ([[r[0], r[3]] for r in back]        # B: id -> old target
                       + [[r[0], r[2]] for r in todo])     # A: id -> old target
            if not changes:
                print("\nNothing to repair.")
                return 0

            if not apply:
                print(f"\nDRY RUN — nothing written. {len(changes):,} rows would "
                      f"change. Re-run with --apply to write.")
                return 0

            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            rb = os.path.expanduser(
                f"~/Downloads/thin_twin_canonical_rollback_{stamp}.json")
            json.dump({"generated_at_utc": stamp,
                       "note": "undo: python3 repair_thin_twin_canonical.py "
                               "--rollback <this file>",
                       "before": before,
                       "changes": changes},
                      open(rb, "w"), indent=1)
            print(f"\nrollback file written: {rb}")

            # ★ ONE TRANSACTION. A committed without B leaves 5 canonical
            # cycles; the order between them is immaterial (disjoint id sets).
            cur.execute("UPDATE discovered_facilities SET duplicate_of_id = NULL "
                        "WHERE id = ANY(%s)", ([r[0] for r in back],))
            print(f"B: cleared {cur.rowcount:,} backwards pointers")
            for r in todo:
                cur.execute("UPDATE discovered_facilities SET duplicate_of_id = %s "
                            "WHERE id = %s", (r[3], r[0]))
            print(f"A: repointed {len(todo):,} thin rows at their populated twin")

            cur.execute(CYCLE_CHECK_SQL)
            cycles = cur.fetchall()
            if len(cycles) > before["pointer_cycles"]:
                conn.rollback()
                print(f"\n✗ ABORTED: the write created pointer cycle(s) — "
                      f"{before['pointer_cycles']} before, {len(cycles)} after, "
                      f"e.g. {cycles[:3]}. Rolled back, nothing changed.",
                      file=sys.stderr)
                return 1

            after = _counts(cur)
            if after["facilities_verified"] != before["facilities_verified"]:
                conn.rollback()
                print(f"\n✗ ABORTED: facilities_verified moved "
                      f"{before['facilities_verified']:,} -> "
                      f"{after['facilities_verified']:,}. A duplicate_of_id repair "
                      f"must not move the advertised count. Rolled back.",
                      file=sys.stderr)
                return 1

            conn.commit()
            print("\nAFTER:")
            for k, v in after.items():
                print(f"  {k:24} {v:,}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
