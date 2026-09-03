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
        measured.append({
            "@type": "PropertyValue", "name": "Facilities", "value": int(fac),
        })

    dcpi = stats.get("dcpi_score")
    if dcpi is not None:
        measured.append({
            "@type": "PropertyValue", "name": "DCPI Score",
            "value": dcpi, "maxValue": 100,
            "description": "DC Hub Power Index — buildability composite, 0-100.",
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
