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


def mw_coverage_note(reporting, total) -> str:
    """'9 of 91 report MW', or '' when the coverage is unknown.

    '' whenever either half is missing, so a painter that cannot measure
    coverage renders exactly what it renders today rather than a fabricated
    "0 of 0". Escape-free by construction: the output is two integers.

    ★ ONE COPY, SIX PAINTERS. #4710 introduced this for /markets/<slug> (the
    cached brief and market_short_html's SEO shell). Measured 2026-09-18, four
    more surfaces published the same bare SUM(power_mw) with no denominator:
    routes/market_brief.py, routes/operator_brief.py, routes/operators.py and
    routes/hyperscaler_brief.py. It lives here — beside the capacity_basis
    vocabulary that already defines what a MW total MEANS — because a second
    copy of this string would drift from the first.

    WHY THE DENOMINATOR IS NOT COSMETIC: power_mw is NULL for 91.8% of
    `facilities` and 94.9% of `discovered_facilities` (measured 2026-09-18),
    and every fleet aggregate COALESCEs that NULL to 0. A market whose rows
    mostly do not record capacity still yields a confident-looking SUM. Austin
    reads 107 MW; 4 of its 91 facilities reported anything at all.
    """
    try:
        _rep, _tot = int(reporting), int(total)
    except (TypeError, ValueError):
        return ""
    if _tot <= 0:
        return ""
    return f"{_rep:,} of {_tot:,} report MW"


# ── Which sources may set power_mw at all ────────────────────────────────
#
# A source belongs here when it identifies that a data centre EXISTS without
# publishing its capacity. Measured 2026-09-18 against the live fleet:
#
#   openstreetmap  688 discovered_facilities rows carry a power_mw, and ALL
#                  688 have raw_data IS NULL and sqft IS NULL — there is no
#                  source record behind any of them. The values cluster on
#                  four constants (5.0 x263, 50.0 x169, 14.0 x78, 18.0 x63)
#                  spread across unrelated operators — atNorth, QTS, Verizon
#                  and Universite Claude Bernard all read exactly 5.0 — which
#                  is a flat default, not a measurement. All were written in
#                  one closed window (2026-01-17 .. 2026-03-02); every OSM row
#                  since reads NULL. routes/osm_crawler.py already says so in
#                  its INSERT: "OSM tells us a data centre EXISTS; it does not
#                  tell us its capacity."
#
# This is deliberately NARROW. A source is listed only once it has been
# measured to carry no capacity basis — not because it merely looks thin.
# PeeringDB and the provider directories publish no power_mw today, but they
# are absent here because nothing in them has been shown to FABRICATE one.
NON_CAPACITY_SOURCES = frozenset({"openstreetmap"})


def source_publishes_capacity(source) -> bool:
    """False when `source` identifies facilities but publishes no capacity.

    The single predicate for "may this row's power_mw be trusted, copied or
    backfilled". It exists so the ingest writer and any future backfill cannot
    disagree about which values are real: a copier that trusts a source the
    writer refuses re-introduces exactly what the writer was fixed to stop.

    Unknown/empty sources return True — this refuses only what has been
    measured to fabricate, and a blanket denial would silently drop the
    curated feeds (seed, datacentermap, operator_website) that are the only
    real capacity the fleet has.
    """
    return str(source or "").strip().lower() not in NON_CAPACITY_SOURCES
