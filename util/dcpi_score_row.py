"""dcpi_score_row.py — the ONE definition of "write a scored DCPI market row".

r-provenance-writer (2026-08-08).

WHY THIS MODULE EXISTS
----------------------
util/dcpi_method.py commits publicly, at /api/v1/dcpi/methodology, to
"method_version stamped on every score row and daily snapshot". On
2026-08-08 that claim was false for 8 published markets — laurel, lenoir,
luckey, maiden, modesto, monroe, salem and west-chester, inserted 08:51 UTC
carrying constraint_score, excess_power_score and verdict but
method_version, signal_tier and data_basis_json all NULL. They were the 8
genuinely-new markets 2.1.1 describes ("only 8 are new to the table"), so no
prior row existed and the gap-filling writer inserted them fresh.

The writer was routes/dcpi_freshness_watchdog.py::recompute_missing_markets.
It runs the SAME scorer as the daily recompute — gather_metrics_for_market,
compute_constraint_score, compute_excess_power_score, derive_verdict — so
the provenance triple was sitting in its `metrics` dict, fully computed. It
simply never listed those three columns in its hand-written INSERT.

That is the whole bug, and it is a hand-copy bug. There were six INSERT
sites for this one table, each with its own hand-maintained column list:

    routes/dcpi.py::recompute              full scorer, stamped all three
    routes/dcpi_freshness_watchdog.py:367  full scorer, stamped none
    routes/dcpi_freshness_watchdog.py:517  full scorer, stamped none  ← the 8
    routes/dcpi.py::lite_recompute         lite formula, stamped none
    main.py::_v216_dcpi_lite_recompute     lite formula, stamped none
    scripts/bulk_dcpi_score.py             lite formula, stamped none

Only the first one was ever updated when data_basis_json (r65), signal_tier
(r-ws3-signal-tier) and method_version (r-ws3-methodology) were added. The
other five were not, because nothing connected them. Same lesson as
util/iso_taxonomy.py, util/status_taxonomy.py, util/deals.py and
util/capacity_pipeline.py: a column list is not the hard part, keeping ONE
of it is. Same lesson as r-ws3-methodology's own hand-copied weights, and as
_SQL_FOOTPRINT_DEDUP's "no third definition" note.

WHAT THIS FIXES BEYOND THE STAMP
--------------------------------
Consolidating the three full-scorer writers onto one statement also closes
two silent drifts that had accumulated in the two watchdog copies:

  * `iso_type` was written only by the daily recompute. The watchdog copies
    left it NULL, so a market first created by the gap-filler had no ISO
    taxonomy label until the next daily sweep.
  * The watchdog UPDATE bound `latitude=%s` with no COALESCE. That is
    verbatim the regression routes/dcpi_freshness_watchdog.py's own
    r-market-resolve-guard sentinel exists to catch ("a future writer
    without COALESCE ... would silently NULL them again → 198/317
    regression"). Force-recomputing a dynamic market, which carries
    (slug, name, state, iso, None, None), wiped its backfilled centroid.

Both are fixed for every caller by construction here, not by three people
remembering.

THE SQL IS GENERATED, NOT TYPED
-------------------------------
_COLUMNS is the single ordered list; the UPDATE and the INSERT are both
built from it. The previous code hand-wrote two parallel column lists and a
positional VALUES string, which needed
tests/test_dcpi_signal_tier.py::test_recompute_insert_placeholder_count_matches_vals
purely to assert the %s count still matched — a miscount was otherwise
swallowed by the per-market try/except. Generating both statements from one
list removes that failure mode instead of testing for it.

No %-formatting anywhere in here: these strings carry %s placeholders for
psycopg2, and mixing Python % into such a string is how a literal % reaches
the driver (see the psycopg2 percent trap).
"""

from __future__ import annotations

import json

# ─────────────────────────────────────────────────────────────────────────
# The row
# ─────────────────────────────────────────────────────────────────────────

# The three columns that answer "what produced this number?". NULL in any of
# them means "unknown", never "low" and never "default" — readers surface an
# unrecorded signal_tier as unknown-confidence (see _signal_tier_basis in
# routes/dcpi.py). Publishing a score with these NULL is the defect this
# module exists to make unrepresentable.
PROVENANCE_COLUMNS = ("data_basis_json", "signal_tier", "method_version")

# Ordered. The UPDATE, the INSERT and the value tuple are all derived from
# this, so they cannot disagree about position or arity.
_COLUMNS = (
    "market_name", "state", "iso", "iso_type",
    "latitude", "longitude",
    "constraint_score", "excess_power_score", "time_to_power_months",
    "queue_capacity_mw", "queue_wait_months", "reserve_margin_pct",
    "gen_additions_12mo_mw", "curtailment_pct", "stranded_capacity_mw",
    "emergency_count_30d",
    "top_risks_json", "top_opportunities_json", "verdict",
) + PROVENANCE_COLUMNS

# Columns an incoming NULL must never overwrite. _load_markets_dynamic emits
# (slug, name, state, iso, None, None) for the ~200 dynamic markets, so a
# plain assignment wipes a backfilled centroid on every recompute. A real
# value still wins; only None defers to what is already stored.
_COALESCE_COLUMNS = frozenset({"latitude", "longitude"})


def _assignment(col: str) -> str:
    if col in _COALESCE_COLUMNS:
        return f"{col}=COALESCE(%s, {col})"
    return f"{col}=%s"


UPDATE_SQL = (
    "UPDATE market_power_scores SET\n    "
    + ",\n    ".join(_assignment(c) for c in _COLUMNS)
    + ",\n    computed_at=NOW()\n"
    + "WHERE market_slug=%s"
)


def _insert_sql(published: bool) -> str:
    cols = ", ".join(_COLUMNS)
    placeholders = ", ".join("%s" for _ in _COLUMNS)
    return (
        f"INSERT INTO market_power_scores (\n    {cols},\n"
        f"    market_slug, published, computed_at\n)\n"
        f"VALUES ({placeholders}, %s, {'TRUE' if published else 'FALSE'}, NOW())"
    )


def scored_row_values(market, metrics, c_score, e_score, ttp,
                      verdict, risks, opps) -> tuple:
    """Build the positional value tuple for one scored market.

    `market` is the canonical (slug, name, state, iso, lat, lon) tuple.
    `metrics` is the dict gather_metrics_for_market returned — the provenance
    triple is read FROM IT, never from the module constant, so a row can
    never claim a version the scorer did not actually run. That distinction
    is r-ws3-methodology's and it is preserved deliberately.
    """
    _slug, name, state, iso, lat, lon = market

    # Derived, never stored independently, so iso and iso_type cannot drift
    # apart (r-iso-taxonomy). Imported here rather than at module import so
    # this module stays importable by tests with no DB and no route deps.
    from util.iso_taxonomy import iso_type_of as _iso_type_of

    return (
        name, state, iso, _iso_type_of(iso) or None,
        lat, lon,
        c_score, e_score, ttp,
        metrics.get("queue_capacity_mw"), metrics.get("queue_wait_months"),
        metrics.get("reserve_margin_pct"),
        metrics.get("gen_additions_12mo_mw"), metrics.get("curtailment_pct"),
        metrics.get("stranded_capacity_mw"),
        metrics.get("emergency_count_30d") or 0,
        json.dumps(risks), json.dumps(opps), verdict,
        # json.dumps(None) → "null" if data_basis is somehow absent.
        json.dumps(metrics.get("data_basis")),
        metrics.get("signal_tier") or None,
        metrics.get("method_version") or None,
    )


class UnprovenancedScore(ValueError):
    """Raised when a caller tries to write a score it cannot attribute.

    Not a defensive nicety. gather_metrics_for_market sets
    metrics["method_version"] = DCPI_METHOD_VERSION unconditionally, so a
    missing version means the caller did not run the real scorer — and a
    published row whose method is unknown is precisely the thing
    /api/v1/dcpi/methodology promises does not exist.

    Checked on every write, not only on the ones that pass publish=True. An
    UPDATE lands on whatever `published` the row already carries, so the
    publish flag says nothing about whether the result is publicly served —
    force_recompute_market inserts unpublished but updates rows the index is
    serving right now.

    Every caller already wraps its per-market work in try/except and counts
    errors, so raising surfaces the market in error_notes / failed_sample
    instead of silently writing an unattributable score.
    """


def update_scored_market(cur, market, metrics, c_score, e_score, ttp,
                         verdict, risks, opps) -> int:
    """Refresh an existing row in place. Returns the number of rows matched.

    Separate from the upsert because one caller — the stale-rescore loop in
    routes/dcpi_freshness_watchdog.py — is deliberately UPDATE-only: it
    reconstructs its market tuples FROM the DB rows it just selected, so a
    zero rowcount means the row disappeared underneath it, not that a new
    market needs creating.

    Rewriting a row's scores without rewriting its provenance is the subtler
    half of this module's reason to exist. The freshness watchdog re-scores
    stale markets under today's method and used to leave method_version at
    whatever the last daily sweep stamped — so a row could advertise 2.1.0
    while carrying numbers 2.2.0 produced. That is worse than the NULL case:
    NULL says "unknown", a stale version says "audited", and the audit does
    not reproduce.
    """
    values = scored_row_values(market, metrics, c_score, e_score, ttp,
                               verdict, risks, opps)
    slug = market[0]

    if not values[_COLUMNS.index("method_version")]:
        raise UnprovenancedScore(
            f"refusing to write a score for {slug} with no method_version: "
            "metrics did not come from gather_metrics_for_market"
        )

    cur.execute(UPDATE_SQL, values + (slug,))
    return cur.rowcount


def upsert_scored_market(cur, market, metrics, c_score, e_score, ttp,
                         verdict, risks, opps, publish: bool):
    """UPDATE-or-INSERT one fully-scored market. Returns "updated"|"inserted".

    UPDATE-or-INSERT rather than ON CONFLICT: Phase SS (2026-05-14) found the
    upsert raising UniqueViolation on market_power_scores_slug_key, which can
    only happen if the live table holds duplicate market_slug rows — i.e. the
    arbiter constraint is not actually enforceable. That killed the recompute
    on every market and froze DCPI 3 days stale. This form depends on no
    constraint: it refreshes every row matching the slug and only INSERTs when
    none exist, so it cannot raise UniqueViolation.

    `publish` is explicit at every call site because it is the difference
    between a row the public /dcpi index serves and one it cannot see; the
    column DEFAULTs to false, so an omitted published= silently hides a
    market (r58's 16 international markets sat at 0 visible for exactly this
    reason).
    """
    if update_scored_market(cur, market, metrics, c_score, e_score, ttp,
                            verdict, risks, opps):
        return "updated"
    values = scored_row_values(market, metrics, c_score, e_score, ttp,
                               verdict, risks, opps)
    cur.execute(_insert_sql(publish), values + (market[0],))
    return "inserted"


# ─────────────────────────────────────────────────────────────────────────
# The lite path
# ─────────────────────────────────────────────────────────────────────────

# The three lite writers (routes/dcpi.py::lite_recompute,
# main.py::_v216_dcpi_lite_recompute, scripts/bulk_dcpi_score.py) do NOT run
# the scorer above. They compute a two-input approximation — facility
# pipeline ratio plus state $/kWh — that shares no weight, ceiling or band
# with the published method. Stamping DCPI_METHOD_VERSION on their output
# would be a straight falsehood, so they do not get to use the writer above.
#
# But leaving them alone is worse than it looks. All three upsert ON CONFLICT
# (market_slug) DO UPDATE and overwrite constraint_score, excess_power_score
# and verdict. Run one today and it rewrites the scores of a row the full
# method produced while LEAVING that row's method_version in place — the row
# then claims 2.2.0 while carrying numbers 2.2.0 never computed. A stale
# stamp is worse than a NULL one: NULL says "unknown", a wrong version says
# "audited, and here is the recipe", and the recipe does not reproduce.
#
# So the lite paths simply may not touch a row the full method owns. This
# predicate is the guard, in one place, for all three.
LITE_MAY_NOT_CLOBBER_FULL = "market_power_scores.method_version IS NULL"
