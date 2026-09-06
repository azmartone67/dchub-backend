"""The canonical machine-readable representation of one market.

r-entity-json (2026-09-03). An agent that crawls /markets/<slug> had no way to
get the same facts as data: there was no .json twin, no content negotiation, and
the API path is a different shape it cannot guess. Worse, /markets/<slug>.json
actively 301'd — market_short_html normalises with slug_norm.replace(".", ""),
so the request landed on /markets/<slug>json and redirected.

Meanwhile the numbers themselves need a basis to be quotable at all: the same
market reads 5,793 MW (rank_markets), 11,052 MW (its page) and 12,438 MW
(/api/v1/markets) — all correct, all different populations and aggregations.

So the twin is not a serialisation of the page; it is the ENTITY, and it is
valid schema.org JSON-LD in its own right. One builder, so the page's embedded
block and the .json twin cannot drift into two different answers.

Pure: no Flask, no DB, no network. Callers supply the facts.
"""
from __future__ import annotations

SITE = "https://dchub.cloud"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
CITE_AS = "DC Hub, dchub.cloud"


def _capacity_basis_or_none(population: str, aggregation: str, grouping: str):
    """The MW basis, when util/facility_count_basis provides it.

    ★ Its OWN try. capacity_basis lands separately; if this import shared the
    builder's error handling, an older util/ would take the whole entity down.
    An optional enrichment must never break the thing it enriches.
    """
    try:
        from util.facility_count_basis import capacity_basis
        return capacity_basis(population, aggregation, grouping)
    except Exception:
        return None


def _count_basis_or_none(population: str, unit: str, grouping: str):
    """The COUNT basis. Same own-try discipline as the capacity one.

    r-one-builder (2026-09-03): this was missing, and its absence is how the two
    market builders drifted — the page's embedded block attached a count basis
    while the .json twin published a bare `Facilities` integer. Northern
    Virginia reads 469 here, 768 as /api/v1/markets 'Northern Virginia' and 328
    as 'Ashburn'; all defensible, none of them interchangeable, and an unlabelled
    one is exactly the contradiction this work exists to remove.
    """
    try:
        from util.facility_count_basis import basis
        return basis(population, unit, grouping)
    except Exception:
        return None


def market_entity(slug: str, name: str, stats: dict | None, *,
                  canonical_slug: str | None = None,
                  as_of: str | None = None) -> dict:
    """Build the market entity as schema.org Dataset JSON-LD.

    `canonical_slug` is the slug whose page serves 200 — pass it so `url` never
    points at a redirect. A measure we do not hold is OMITTED, never zero-filled:
    a fabricated 0 MW is worse than an absent field, because a consumer cannot
    tell it from a real reading.
    """
    stats = stats or {}
    page_slug = canonical_slug or slug
    measured = []

    mw = stats.get("total_mw")
    if mw is not None:
        m = {
            "@type": "PropertyValue", "name": "Total Capacity",
            "value": round(float(mw), 1), "unitCode": "MAW", "unitText": "MW",
        }
        b = _capacity_basis_or_none("tracked", "sum_sites", "market_slug")
        if b:
            m["measurementTechnique"] = b["aggregation_means"]
            m["description"] = (
                f"population={b['population']}; aggregation={b['aggregation']}; "
                f"grouping={b['grouping']} — {b['compare_note']}")
        measured.append(m)

    fac = stats.get("facility_count")
    if fac is not None:
        f = {"@type": "PropertyValue", "name": "Facilities", "value": int(fac)}
        cb = _count_basis_or_none("tracked", "distinct_site", "market_slug")
        if cb:
            f["measurementTechnique"] = cb["unit_means"]
            f["description"] = (
                f"population={cb['population']}; unit={cb['unit']}; "
                f"grouping={cb['grouping']} — {cb['compare_note']}")
        measured.append(f)

    dcpi = stats.get("dcpi_score")
    if dcpi is not None:
        # r-brief-live-score (2026-09-06): the DCPI measure can be FRESHER than
        # the node. /markets/<slug> renders a stored narrative whose facility
        # and capacity readings are the brief's, but reads the score live —
        # they move on different clocks (DCPI 4x/day, briefs ~monthly). The
        # node-level dateModified stays the brief's, because it is the vintage
        # of most of what is in here; stamping it with the score's date would
        # over-claim the counts. So the score states its own, in the one place
        # schema.org lets a single PropertyValue carry free text.
        _dcpi_at = stats.get("dcpi_as_of")
        _dcpi_desc = "DC Hub Power Index — buildability composite, 0-100."
        if _dcpi_at:
            _dcpi_desc += f" Observed {str(_dcpi_at)[:19]}."
        measured.append({
            "@type": "PropertyValue", "name": "DCPI Score",
            "value": dcpi, "maxValue": 100,
            # A score is neither a count nor a capacity, so facility_count_basis
            # has no vocabulary for it — its METHOD is its basis, and it must be
            # stated or this is a bare number like the ones above used to be.
            "measurementTechnique": (
                "DC Hub Power Index — buildability composite scored 0-100 "
                "(excess-power headroom, grid constraint, time-to-power), "
                "verdict-gated. Comparable only against other DCPI scores; not "
                "a capacity, a ranking position, or a percentage."),
            "description": _dcpi_desc,
        })

    # r-pockets-structured-data (2026-09-06): the three DCPI COMPONENTS and,
    # when a surface ranks on one, its deployability rank. Optional and
    # omitted when absent, so /markets/<slug> and its .json twin are
    # byte-unchanged until they choose to pass them — /pockets/<slug> was the
    # surface that needed them, having published four numbers as prose with an
    # ld+json carrying no variableMeasured at all.
    #
    # Each states its own direction, because these are the scores where a
    # reader's intuition is wrong half the time: HIGHER excess is better,
    # LOWER constraint is better, and the two are not two views of one axis.
    for _key, _name, _max, _tech in (
        ("excess_power_score", "Excess Power Score", 100,
         "DCPI excess-power headroom, 0-100. HIGHER IS BETTER: 0 = no "
         "uncommitted headroom, 100 = ample. Not a capacity and not a "
         "percentage of anything."),
        ("constraint_score", "Grid Constraint Score", 100,
         "DCPI grid-constraint index, 0-100. LOWER IS BETTER: 0 = clear, "
         "100 = blocked. Not the inverse of the excess-power score; the two "
         "measure different things and can be high together."),
    ):
        _v = stats.get(_key)
        if _v is not None:
            # ★ THE BUILDER ROUNDS, NOT THE CALLER (r-ttp-one-precision
            # 2026-09-06). /markets passed these straight off the row while
            # /pockets pre-rounded to 1dp, so one market_power_scores row
            # published two different numbers for one measure the moment a
            # value carried a second decimal. Rounding here makes the two
            # surfaces identical by construction rather than by both callers
            # remembering — the same reason Total Capacity is rounded here and
            # not at its call sites.
            _m = {"@type": "PropertyValue", "name": _name,
                  "value": round(float(_v), 1), "maxValue": _max,
                  "measurementTechnique": _tech, "description": _tech}
            if stats.get("dcpi_as_of"):
                _m["description"] += f" Observed {str(stats['dcpi_as_of'])[:19]}."
            measured.append(_m)

    _ttp = stats.get("time_to_power_months")
    if _ttp is not None:
        _ttp_tech = ("DC Hub estimate of interconnection-to-energised months "
                     "for new load in this market. An estimate, not a utility "
                     "commitment or a quoted queue position.")
        measured.append({
            "@type": "PropertyValue", "name": "Time to Power",
            "value": round(float(_ttp), 1), "unitCode": "MON",
            "unitText": "months",
            # Both fields, like every other measure here: a consumer reading
            # only `description` must not get a bare number with no basis.
            "measurementTechnique": _ttp_tech, "description": _ttp_tech,
        })

    # A ranking is NOT a DCPI score, and the one place it must say so is here
    # — structured data is what an agent cites without reading the page.
    # `rank_label` / `rank_basis` come from util.deployability_rank via the
    # calling surface, never typed here, so there is one account of what the
    # number is.
    _rank = stats.get("rank_score")
    if _rank is not None and stats.get("rank_label"):
        measured.append({
            "@type": "PropertyValue", "name": stats["rank_label"],
            "value": _rank,
            "measurementTechnique": stats.get("rank_basis") or "",
            "description": stats.get("rank_basis") or "",
        })

    out = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "identifier": slug,
        "name": f"{name} Data Center Market — DC Hub",
        "description": (
            f"Live data-center market measurements for {name}, published by "
            f"DC Hub with the basis of each figure."),
        "url": f"{SITE}/markets/{page_slug}",
        "license": LICENSE_URL,
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "DC Hub", "url": SITE},
        "citation": CITE_AS,
        "spatialCoverage": {"@type": "Place", "name": name},
        "variableMeasured": measured,
    }
    if as_of:
        out["dateModified"] = as_of
        out["temporalCoverage"] = as_of
    return out
