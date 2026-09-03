"""The shared vocabulary for "how many facilities are in <market>" — and, since
2026-09-03, for "how much capacity is in <market>" (see capacity_basis below).

Four public surfaces answer that question with four different numbers. For
Ashburn on 2026-08-01 they read 130, 141, 179 and 206 — every one of them
correct, none of them interchangeable. Each surface documented itself in its
own prose, so a reader comparing two of them saw a contradiction rather than
two different questions. This module is the one place the terms are defined.

A facility count is fixed by THREE independent axes. Change any one and the
number moves:

  population  WHICH facilities count      tracked / operational / metered
  unit        what ONE of them is         row / distinct_name / distinct_site
  grouping    what "in <market>" means    city / city_state / market_slug

Every axis is orthogonal, which is why "distinct operational facilities by
market slug" (179) and "distinct sites by city" (206) can both be true of
Ashburn at the same instant.

Live reconciliation, 2026-08-01 (Ashburn, all over the #1539 fleet filter):

    141  /radar facility_count          metered · row · city_state
    130  ai-capacity metered_facility_count
                                        metered · distinct_name · market_slug
    179  ai-capacity facility_count     operational · distinct_name · market_slug
    187  ai-capacity tracked_count      tracked · distinct_name · market_slug
    206  by-market count                tracked · distinct_site · city

★ `facility_count` is NOT a stable name across surfaces — /radar publishes the
metered population under it, ai-capacity publishes the operational one. Neither
is wrong; they answer different questions. Read `count_basis`, never the field
name, and never compare two surfaces' counts without comparing their bases
first.

Status classification is NOT redefined here — `util/status_taxonomy.py` owns
which literals mean operational, and it folds case, so these definitions are
stable across the 2026-08-01 lowercase-status backfill.
"""

# ── the three axes ───────────────────────────────────────────────────────────

POPULATIONS = {
    "tracked": (
        "every facility in the fleet, any lifecycle status, whether or not we "
        "hold a capacity figure for it — the widest honest answer"),
    "operational": (
        "tracked, narrowed to facilities that are running, per "
        "util/status_taxonomy.py (case-folded; 'active' counts as operational)"),
    "metered": (
        "tracked, narrowed to facilities carrying power_mw > 0 — the only "
        "population whose count describes the same rows as a MW total beside "
        "it. A facility missing from this count is a DISCLOSURE gap, not a "
        "shut-down facility"),
}

UNITS = {
    "row": (
        "one row of discovered_facilities — over-counts a site held as several "
        "keeper rows"),
    "distinct_name": "one distinct case-folded facility name within the group",
    "distinct_site": (
        "one distinct canonical_slug — the building identity; rows with a NULL "
        "slug are uncounted"),
}

GROUPINGS = {
    "city": "city alone — merges same-named cities in different states",
    "city_state": "(city, state) as stored, so case variants form separate groups",
    "market_slug": (
        "case-folded city resolved to a single market slug, blank state folded "
        "in only when unambiguous"),
}

FLEET_FILTER = "COALESCE(is_duplicate,0)=0"

# ── the MW axis this module was missing ──────────────────────────────────────
# Everything above fixes "how many facilities". Nothing fixed "how much
# capacity", and a market's MW moves on its own axes. Measured live 2026-09-03,
# all for Ashburn / Northern Virginia, all correct, none interchangeable:
#
#     5,793 MW  rank_markets            operational · sum_rows   · city_state
#    11,052 MW  /markets/ashburn page   tracked     · sum_sites  · market_slug
#    12,438 MW  /api/v1/markets "NoVA"  operational · sum_rows   · alias_group
#
# The page's figure is larger because it counts PLANNED capacity the ranking
# excludes, and because it collapses each site to MAX(mw) before summing rather
# than adding every row. Both choices are defensible; publishing all three under
# the bare name `total_mw` is not. An agent that crawls the page and also calls
# the tool sees us contradict ourselves, and a source that contradicts itself
# does not get cited — which is worse than not being found.
#
# `population` and `grouping` are the SAME axes as a count, deliberately: a MW
# total and the count beside it must be able to declare that they describe the
# same rows.
AGGREGATIONS = {
    "sum_rows": (
        "SUM(power_mw) over every qualifying row — the plain total. Adds a "
        "site twice when it is held as several keeper rows"),
    "sum_sites": (
        "collapse each site to one figure first (MAX(mw) per identity), then "
        "sum — immune to multi-row sites, but silently drops a genuinely "
        "separate building that shares an identity key"),
    "sum_metered_rows": (
        "sum_rows narrowed to rows carrying power_mw > 0 — the only aggregation "
        "whose MW describes exactly the rows a `metered` count describes"),
}


def capacity_basis(population: str, aggregation: str, grouping: str,
                   note: str | None = None) -> dict:
    """The `capacity_basis` disclosure a surface publishes beside a MW total.

    Same contract as basis() above — unknown terms raise rather than shipping a
    plausible-looking basis nobody can cross-reference.
    """
    for value, vocab, axis in ((population, POPULATIONS, "population"),
                               (aggregation, AGGREGATIONS, "aggregation"),
                               (grouping, GROUPINGS, "grouping")):
        if value not in vocab:
            raise ValueError(
                f"unknown {axis} {value!r} — util/facility_count_basis.py "
                f"defines {sorted(vocab)}. Add the term there (and say what it "
                f"means) rather than inventing one at the call site.")
    out = {
        "population": population, "population_means": POPULATIONS[population],
        "aggregation": aggregation, "aggregation_means": AGGREGATIONS[aggregation],
        "grouping": grouping, "grouping_means": GROUPINGS[grouping],
        "fleet_filter": FLEET_FILTER,
        "compare_note": (
            "MW totals from different surfaces are comparable only when all "
            "three axes match. A larger number is usually a wider population, "
            "not more capacity. See util/facility_count_basis.py."),
    }
    if note:
        out["note"] = note
    return out


def basis(population: str, unit: str, grouping: str, note: str | None = None) -> dict:
    """Build the `count_basis` disclosure block a surface publishes beside its
    count. Raises on an unknown term — a typo must fail loudly at the call
    site rather than ship a plausible-looking basis nobody can cross-reference.
    """
    for value, vocab, axis in ((population, POPULATIONS, "population"),
                               (unit, UNITS, "unit"),
                               (grouping, GROUPINGS, "grouping")):
        if value not in vocab:
            raise ValueError(
                f"unknown {axis} {value!r} — util/facility_count_basis.py "
                f"defines {sorted(vocab)}. Add the term there (and say what it "
                f"means) rather than inventing one at the call site.")
    out = {
        "population": population, "population_means": POPULATIONS[population],
        "unit": unit, "unit_means": UNITS[unit],
        "grouping": grouping, "grouping_means": GROUPINGS[grouping],
        "fleet_filter": FLEET_FILTER,
        "compare_note": (
            "Counts from different surfaces are comparable only when all three "
            "axes match. See util/facility_count_basis.py."),
    }
    if note:
        out["note"] = note
    return out
