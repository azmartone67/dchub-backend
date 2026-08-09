"""ONE definition of the platform-wide facility count.

Why this module exists (2026-08-09)
-----------------------------------
`/api/agents/intelligence-index` published `data_summary.facilities = 20132`
to AI agents. That figure reconciled with NOTHING the platform publishes:

    canon phrase   /api/v1/canon/phrases     "17,200+"
    distinct       stats/canonical           17,294   <- the citeable fleet
    records        stats/canonical           25,024   <- raw source rows
    the endpoint                             20,132   <- ???

20,132 was `SELECT COUNT(*) FROM facilities` — the LEGACY publication table,
a DIFFERENT TABLE from the canonical `discovered_facilities` fleet, counted
raw: every lifecycle status, no de-duplication, no keeper election. Measured
2026-08-09: /api/v1/stats `by_status` (computed over that same legacy table)
sums to exactly 20,132, and `_facility_count_notes.legacy_facilities_table`
reads 20,132. It is a real number about a real table; it is simply not an
answer to "how many data centers does DC Hub track", and it must never be
published under the bare name `facilities`.

The genuine distinction this module preserves
---------------------------------------------
Distinct facilities (17k) vs source records (25k) is REAL and must not be
flattened — `discovered_facilities` holds several rows per building from the
March 2026 backfill. Both are publishable; each must state which it is.

Suppression flags are NOT a counting basis. `is_duplicate` / `duplicate_of_id`
govern VISIBILITY, not population: the dedup pipeline flags rows without
electing a keeper, so 9,318 of the distinct facilities have no is_duplicate=0
row at all and vanish from any is_duplicate-based count — Meta Hyperion,
Stargate Abilene, CoreWeave Project Horizon and Microsoft Wisconsin among
them. COUNT(DISTINCT canonical_slug) is unambiguous by construction and is
what /api/v1/stats/canonical already serves as `facilities_distinct`.

Usage — the count is READ, never baked in. There is no numeric fallback in
this module on purpose: an unmeasurable count returns None so the caller
publishes null + a reason, rather than a stale constant that looks live.
"""

from __future__ import annotations

# ── the canonical definition (identical to /api/v1/stats/canonical) ──────────
CANON_SQL = ("SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities "
             "WHERE canonical_slug IS NOT NULL")
RECORDS_SQL = "SELECT COUNT(*) FROM discovered_facilities"

# The legacy table. Kept here ONLY so its basis string is stated wherever the
# number still appears. Do not introduce new consumers.
LEGACY_SQL = "SELECT COUNT(*) FROM facilities"

CANON_BASIS = (
    "COUNT(DISTINCT canonical_slug) over discovered_facilities WHERE "
    "canonical_slug IS NOT NULL — distinct BUILDINGS (one physical facility "
    "counted once, however many sources reported it). This is the citeable "
    "figure and is the same query behind facilities_distinct on "
    "/api/v1/stats/canonical.")

RECORDS_BASIS = (
    "COUNT(*) over discovered_facilities — raw SOURCE RECORDS, ~1.4x the "
    "building count because several sources may each contribute a row for the "
    "same site. Not a facility count.")

LEGACY_BASIS = (
    "COUNT(*) over the legacy `facilities` publication table — rows, all "
    "lifecycle statuses, no de-duplication. A DIFFERENT TABLE from the "
    "canonical discovered_facilities fleet; never publish it as 'facilities'.")


def canonical_facility_count(cur):
    """Distinct-building count via `cur`, or None if it cannot be measured.

    Returns None — never a fallback integer. A count that could not be read is
    unknown, and an unknown count must reach the wire as null beside a stated
    reason. Substituting any constant here would recreate the exact defect this
    module was written to remove.
    """
    try:
        cur.execute(CANON_SQL)
        n = int((cur.fetchone() or [0])[0] or 0)
        return n if n > 0 else None
    except Exception:
        return None


def facility_records_count(cur):
    """Raw source-record count via `cur`, or None if it cannot be measured."""
    try:
        cur.execute(RECORDS_SQL)
        n = int((cur.fetchone() or [0])[0] or 0)
        return n if n > 0 else None
    except Exception:
        return None
