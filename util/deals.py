"""deals.py — canonical quarantine guard for `deals`.

Single source of truth for "is this M&A row publishable?". Sibling of
util/capacity_pipeline.py, created for the same reason and by the same
lesson: a one-line predicate is not the hard part, keeping ONE of it is.

THE HAND-COPY THAT util/capacity_pipeline.py WAS SUPPOSED TO END
----------------------------------------------------------------
capacity_pipeline.py's own docstring names three copies of this predicate
as the motivating example, then #2071 added two more in
routes/agent_index.py because consolidating was out of that PR's scope.
Counted 2026-08-01, before this module existed, there were seven files
carrying a hand-written copy in two different spellings:

    routes/graph_spine_master_shell.py   _LIVE_DEALS  + 3 inline
    routes/infra_growth.py               _live (function-local)
    routes/deals_routes.py               3 inline
    routes/agent_index.py                DEALS_OK (function-local) + 1 inline
    canonical_stats.py                   1 inline
    routes/brain_rag.py                  1 inline (aliased)
    routes/agreement_master_shell.py     1 inline (inverted)

Two of those — `_live` and #2071's `DEALS_OK` — were function-LOCAL, the
exact shape that made the 2026-07-27 "every capacity_pipeline read is
guarded" claim false for fourteen served reads: nothing could import
them, so nothing could check anything against them, so nothing noticed
the reads that had no guard at all. Import from here; do not re-inline.
tests/test_deals_guard.py censuses guarded vs unguarded `FROM deals` per
file and fails the build when that census moves.

WHAT THE GUARD IS WORTH
-----------------------
Measured 2026-08-01:

    deals, raw                                          4,711 rows
    deals, WHERE DEALS_OK                               1,843 rows   (2.6x)

    data_flag = 'quarantine_duplicate'                  2,823
    data_flag IS NULL                                   1,836
    data_flag = 'quarantine_unit_garbage'                  34
    data_flag = 'quarantine_seed_placeholder'              11
    data_flag = 'cumulative_capex'                          7

Published canon for deal counts is "1,400+". The unguarded 4,711 is the
stale "4,000+" — a number the site stopped claiming precisely because it
counts the same transaction up to 945 times.

★ THE PREFIX TEST, NOT capacity_pipeline's STRICT `= ''`
capacity_pipeline uses `COALESCE(data_flag,'') = ''` and fails CLOSED: a
new flag vocabulary drops out of published figures instead of leaking
through. `deals` CANNOT use that form. It carries `cumulative_capex`
(7 rows live), a legitimate non-quarantine annotation, and the strict
form would silently drop those seven publishable rows. So `deals` tests
the `quarantine_` PREFIX and everything else is publishable.

The two modules disagreeing here is deliberate and each is correct for
its table. Do not "harmonise" them.

★ NO LITERAL `%` — DELIBERATE
The predicate is `<>`-based, never `LIKE 'quarantine_%'`. Several callers
concatenate or interpolate this string into a query that ALSO passes a
params tuple; a literal `%` would make psycopg2 attempt substitution on
it and 500 the route. `LEFT(data_flag,11)` is how the prefix test is
expressed without a wildcard. Keep it %-free —
tests/test_deals_guard.py asserts it.

★ EQUIVALENT TO BOTH SPELLINGS IT REPLACES
The seven files above used two forms. They are the same predicate:

    COALESCE(LEFT(data_flag,11),'') <> 'quarantine_'          (this one)
    (data_flag IS NULL OR LEFT(data_flag,11) <> 'quarantine_')

    data_flag                 LEFT(...,11)     DEALS_OK
    NULL                      NULL -> ''       true   (publishable)
    ''                        ''               true
    'cumulative_capex'        'cumulative_'    true   ← the 7 rows
    'quarantine_duplicate'    'quarantine_'    false
    'quarantine_unit_garbage' 'quarantine_'    false

This module ships the COALESCE form to mirror CP_OK's shape, because it
names the column ONCE — which is what makes `deals_ok("d")` readable —
and because it contains no OR, so it cannot change meaning if a caller
concatenates it next to an AND without parentheses.

★ THE GUARD IS NOT A ROW FILTER FOR *EVERY* READ
Quarantined rows are KEPT on purpose. An admin dedup console reporting
rows-before/rows-after, an integrity sweep comparing row counts against
sentinel_row_baselines, and an ingest job confirming its writes landed
must all see the whole table — guarding those would make them lie. Use
this guard on figures we PUBLISH. tests/test_deals_guard.py carries the
list of reads that are unguarded on purpose, each with its reason.
"""

from __future__ import annotations

__all__ = ["DEALS_OK", "deals_ok", "DEALS_QUARANTINED", "deals_quarantined"]

#: The flag prefix. Eleven characters — which is where the 11 comes from.
_PREFIX = "quarantine_"


def _left(alias: str) -> str:
    prefix = f"{alias}." if alias else ""
    return f"COALESCE(LEFT({prefix}data_flag,{len(_PREFIX)}),'')"


#: Canonical predicate: TRUE for rows we may publish. Bare form, for an
#: unaliased `FROM deals`.
DEALS_OK = f"{_left('')} <> '{_PREFIX}'"

#: The complement. Exists so the `11` and the `quarantine_` literal live in
#: exactly one place: routes/agreement_master_shell.py and
#: routes/graph_spine_master_shell.py both report a quarantined-row COUNT
#: next to a published one, and a hand-written inverse is how the two halves
#: of a "N of M rows are suppressed" line drift apart.
DEALS_QUARANTINED = f"{_left('')} = '{_PREFIX}'"


def deals_ok(alias: str = "") -> str:
    """Return the publishable-row guard, optionally qualified by an alias.

    >>> deals_ok()
    "COALESCE(LEFT(data_flag,11),'') <> 'quarantine_'"
    >>> deals_ok("d")
    "COALESCE(LEFT(d.data_flag,11),'') <> 'quarantine_'"

    Use the alias form in any query that joins another table carrying its
    own `data_flag` — `capacity_pipeline` does, and so does the brain
    corpus view. Unqualified, Postgres either resolves the column
    ambiguously or errors, and an errored read inside a try/except
    silently becomes a zero rather than a 500.
    """
    return f"{_left(alias)} <> '{_PREFIX}'"


def deals_quarantined(alias: str = "") -> str:
    """Return the complement of :func:`deals_ok` — TRUE for suppressed rows.

    >>> deals_quarantined("d")
    "COALESCE(LEFT(d.data_flag,11),'') = 'quarantine_'"
    """
    return f"{_left(alias)} = '{_PREFIX}'"
