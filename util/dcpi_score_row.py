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


# ─────────────────────────────────────────────────────────────────────────
# The publish gate
# ─────────────────────────────────────────────────────────────────────────
#
# r-publish-gate (2026-08-08).
#
# Writing a row and SERVING it are different decisions, and until now only
# the first one had a single owner. `published` was decided by two statements
# in dchub_self_heal.py::fix_enforce_publish_gate — one for curated rows, one
# for lite-pro rows — and r-twin-unpublish's "do not re-publish a retired
# twin" rule was hand-added to the first and not the second.
#
# That is the whole defect, and the live table fingerprinted it exactly.
# Seven twins were retired on 2026-07-28. Six carry tier_required='free' and
# were caught by the curated statement's exclusion: measured 2026-08-08 they
# are all still published=false, and dcpi_daily_snapshots shows them dropping
# out of the index on 07-28 and never returning. The seventh, `washington`,
# carries tier_required='lite-pro' — the one branch with no exclusion — so
# the DCPI recompute unpublished it and the next self-heal cycle put it
# straight back. It flapped: present in the published snapshot on 07-29,
# absent 07-30 → 08-05, present 08-06 and 08-07, absent 08-08. Same fix, same
# day, seven rows; the only variable that predicted the outcome was which of
# the two statements owned the row.
#
# So the fix is not a third copy of the rule. It is one predicate, and ONE
# statement that assigns `published`. Same shape as LITE_MAY_NOT_CLOBBER_FULL
# above and for the same reason: a column list is not the hard part, keeping
# ONE of it is.
#
# THE THREE RULES, and why each is here rather than folded into the others:
#
#   1. TWIN — never publish a redundant twin while its canonical is
#      published. Load-bearing. It is the only rule that still holds if
#      something re-scores the twin: routes/dcpi_freshness_watchdog.py's
#      recompute_stale_markets selects on `computed_at < NOW() - 7 days`
#      with no published filter and no twin filter, and says in its own
#      comment that it deliberately does not require the slug to be in
#      MARKETS — so it can make a retired twin fresh AND stamped, at which
#      point rules 2 and 3 both go quiet.
#      Mirrors r-twin-unpublish's "canonical must exist" guard: if the
#      canonical is absent or itself unpublished the twin is allowed
#      through, because retiring it would leave the market with nowhere to
#      redirect.
#
#   2. FRESHNESS — never publish a row far behind the rest of the index. The
#      gate's quality components (iso filled, EIA price, facility count,
#      constraint > 0, excess > 0) are all things a 19-day-old row still
#      satisfies perfectly; `washington` scored 100/100 while frozen.
#      Measured RELATIVE to the index, not to NOW(): if the whole pipeline
#      stalls, every row's computed_at moves together and nothing is
#      unpublished — which is the behaviour you want, since a stalled
#      pipeline must not also blank the index.
#      The reference is the MEDIAN, not MAX, and that is deliberate. Against
#      MAX, one row with a future computed_at drags every other row past the
#      threshold and the gate unpublishes the entire index — a single bad
#      timestamp escalating into a total outage. The median is unmoved by
#      an outlier at either end. Measured 2026-08-08: median 14:42 vs max
#      14:49, seven minutes apart across 330 rows.
#
#   3. PROVENANCE — never publish a row whose method_version is NULL.
#      /api/v1/dcpi/methodology commits to "method_version stamped on every
#      score row"; r-provenance-writer made that true of every WRITE path,
#      but a statement that only flips `published` binds no score, so it sat
#      outside that census by construction. This is the same commitment,
#      enforced at the serving decision.
#      A row the full scorer never reaches is also a row that will go stale,
#      so rule 3 mostly catches rule 2's cases earlier rather than catching
#      different ones. It is not redundant though: it fires immediately,
#      where rule 2 has to wait out the window.
#
# BLAST RADIUS, measured on the live table before shipping (2026-08-08
# 15:39 UTC): of 324 published rows, the gate refuses exactly one —
# `washington` — and all three rules independently agree on it. Every other
# published row carries method_version 2.2.0 and a computed_at inside 15
# minutes of the median. Rule 3's full reach across the table is the seven
# twins and nothing else: table-wide, method_version IS NULL and "is a
# retired twin" select the same seven rows.

#: How far behind the index a row may fall and still be served. The live
#: distribution is bimodal with nothing in the middle — 323 rows inside one
#: day of the median, 7 rows beyond fourteen — so any threshold from 1 to 14
#: days selects the same set. 7 days takes the middle of that empty gap.
PUBLISH_STALE_AFTER = "7 days"

#: One predicate, three rules, evaluated per row of market_power_scores.
#:
#: Written against the bare table name rather than an alias so it can be
#: dropped into any UPDATE on market_power_scores, exactly like
#: LITE_MAY_NOT_CLOBBER_FULL. Binds two parameters — see may_publish_params.
#:
#: The interval is substituted with .replace() and not an f-string: this
#: string carries %s placeholders for psycopg2, and putting Python
#: %-formatting anywhere near such a string is how a literal % reaches the
#: driver (see the psycopg2 percent trap, and the same note at the top of
#: this module).
#: Read-side counterpart to MAY_PUBLISH: which rows a PUBLIC surface may count,
#: list or roll up as a scored market.
#:
#: MAY_PUBLISH decides what earns the flag; this decides who believes it. They
#: sit together so that changing one puts the other in front of you — the two
#: drifted apart once already, and the retired alias-twins r-twin-unpublish had
#: correctly unpublished went on being counted, listed and averaged by every
#: surface that queried this table without asking.
#:
#: `published = true`, NOT `COALESCE(published, true) = true`. The column is
#: nullable and its schema DEFAULT is false, so coalescing a NULL to TRUE
#: inverts the table's own stated default and would serve a row nobody ever
#: published. This spelling is also character-identical to the predicate /dcpi
#: and /api/v1/dcpi/scores use, which is the whole point: the surfaces that
#: import this exist to AGREE with those two, and agreement is not something a
#: second, independently-worded predicate can promise.
#:
#: Written against the bare table name, like MAY_PUBLISH, so it drops into any
#: query over market_power_scores.
#:
#: ★ A WHERE clause that already contains an OR must be parenthesised before
#: this is ANDed onto it — AND binds tighter than OR, so appending it to
#: `WHERE a = %s OR b = %s` silently filters only the second branch. See the
#: region query in routes/agent_index.py.
PUBLISHED_ONLY = "market_power_scores.published = true"


MAY_PUBLISH = """(
        market_power_scores.method_version IS NOT NULL
    AND market_power_scores.computed_at IS NOT NULL
    AND market_power_scores.computed_at >= (
            SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY computed_at)
              FROM market_power_scores
        ) - INTERVAL '{stale_after}'
    AND NOT EXISTS (
            SELECT 1
              FROM unnest(%s::text[], %s::text[]) AS twin(slug, canonical)
              JOIN market_power_scores canon
                ON canon.market_slug = twin.canonical
             WHERE twin.slug = market_power_scores.market_slug
               AND COALESCE(canon.published, true) = true
        )
)""".replace("{stale_after}", PUBLISH_STALE_AFTER)
# ★ That COALESCE is deliberate, and it is NOT the inversion fixed in
# canonical_stats.py on 2026-09-05. Do not "make it consistent" with
# PUBLISHED_ONLY — the two ask opposite questions.
#
# PUBLISHED_ONLY asks "may I show this row?", so assuming an unknown flag means
# yes would publish something nobody released. Permissive is wrong there.
#
# Here the predicate sits inside a NOT EXISTS that BLOCKS a twin from
# publishing while its canonical is live. Assuming an unknown canonical is live
# therefore BLOCKS the twin — which is the conservative answer, because the
# failure it prevents is both rows being published as separate markets at once.
# Rewriting this to `canon.published = true` would let a twin publish beside a
# canonical whose state is unknown: the exact duplication r-twin-unpublish
# exists to end.
#
# Same words, opposite direction, because this one is read under a negation.


def may_publish_params():
    """The two parallel arrays MAY_PUBLISH's placeholders bind: twin slugs
    and their canonical targets, zipped by unnest.

    Derived from util.market_aliases every call, never listed here. A second
    copy of that table is the bug class fixed in util/iso_taxonomy.py, and
    r-twin-unpublish already moved the table out of routes/dcpi.py precisely
    so a caller could read it without paying for that module's import.

    A twin with no canonical target is dropped rather than passed with an
    empty string, which would make the join match nothing anyway — but
    silently, and a silent no-op is what let the original retirement gap sit
    unnoticed for nine days. tests/test_dcpi_twin_retirement.py asserts the
    set has no such entries, so dropping one here is unreachable in practice
    and defensive only.
    """
    from util.market_aliases import DCPI_METRO_ALIASES, REDUNDANT_TWIN_SLUGS

    pairs = sorted(
        (twin, DCPI_METRO_ALIASES[twin])
        for twin in REDUNDANT_TWIN_SLUGS
        if DCPI_METRO_ALIASES.get(twin)
    )
    return [t for t, _ in pairs], [c for _, c in pairs]
