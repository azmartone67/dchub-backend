"""The canonical machine-readable representation of one facility.

r-facility-entity (2026-09-03). The market work gave 249 market pages typed,
based, citable measurements. Facilities are 20,300+ pages — the bulk of the
corpus and where most inbound crawl traffic lands — and they had:

    JSON-LD @types  : Dataset, Place, GeoCoordinates, PostalAddress, ...
    license CC-BY   : yes
    variableMeasured: NO      <- a Dataset wrapper with no measurements in it
    /facilities/<slug>.json   : 404

So a crawler got the "you may cite this" envelope around numbers that existed
only in prose.

★ THE RECONCILIATION NOTE IS THE POINT. A facility's power_mw is ONE record,
not an aggregate. Summing facility MW across a market will NOT reproduce that
market's published total — different population, different dedup, different
grouping (see util/facility_count_basis). Saying so on the record is what stops
an agent deriving a third number from our own data and concluding we
contradict ourselves.

Pure: no Flask, no DB, no network. Callers supply the record.
"""
from __future__ import annotations

SITE = "https://dchub.cloud"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
CITE_AS = "DC Hub, dchub.cloud"

#: Why a facility MW is not a market MW. Published on every record that has one.
NOT_AN_AGGREGATE = (
    "Single facility record, not an aggregate. power_mw is this site's capacity "
    "as tracked by DC Hub. Summing facility MW across a market does NOT "
    "reproduce that market's published total — the market figure applies its "
    "own population, de-duplication and grouping (see the market's "
    "variableMeasured basis). Do not derive one from the other."
)


def facility_measures(fac: dict | None) -> list[dict]:
    """The typed measurements for a facility record.

    A measure we do not hold is OMITTED, never zero-filled — and for facilities
    that matters more than usual: power_mw is frequently absent (a DISCLOSURE
    gap, not a shut-down site), and a fabricated 0 MW is indistinguishable from
    a real reading of zero.
    """
    fac = fac or {}
    out = []
    mw = fac.get("power_mw")
    try:
        mw = float(mw) if mw is not None else None
    except (TypeError, ValueError):
        mw = None
    if mw is not None and mw > 0:
        out.append({
            "@type": "PropertyValue",
            "name": "Power Capacity",
            "value": round(mw, 1),
            "unitCode": "MAW",
            "unitText": "MW",
            "measurementTechnique": NOT_AN_AGGREGATE,
        })
    status = (fac.get("status") or "").strip()
    if status:
        out.append({
            "@type": "PropertyValue",
            "name": "Lifecycle Status",
            "value": status,
            "description": ("Operational lifecycle per util/status_taxonomy. A "
                            "market's 'operational' population is filtered on "
                            "this field."),
        })
    return out


def facility_entity(fac: dict | None, *, canonical_url: str,
                    display_name: str, as_of: str | None = None) -> dict:
    """The facility as schema.org Dataset JSON-LD — the .json twin's body."""
    fac = fac or {}
    out = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "@id": canonical_url + "#dataset",
        "identifier": fac.get("canonical_slug") or fac.get("id"),
        "name": f"{display_name} — facility intelligence record",
        "description": (
            f"Structured data-center facility record (location, operator, power "
            f"and connectivity context) for {display_name}."),
        "url": canonical_url,
        "license": LICENSE_URL,
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "DC Hub", "url": SITE},
        "citation": CITE_AS,
        "variableMeasured": facility_measures(fac),
    }
    place = {}
    if fac.get("city"):
        place["addressLocality"] = fac["city"]
    if fac.get("state"):
        place["addressRegion"] = fac["state"]
    if fac.get("country"):
        place["addressCountry"] = fac["country"]
    lat, lon = fac.get("latitude"), fac.get("longitude")
    node = {"@type": "Place", "name": display_name}
    if place:
        node["address"] = dict({"@type": "PostalAddress"}, **place)
    if lat is not None and lon is not None:
        try:
            node["geo"] = {"@type": "GeoCoordinates",
                           "latitude": float(lat), "longitude": float(lon)}
        except (TypeError, ValueError):
            pass
    out["spatialCoverage"] = node
    if fac.get("provider"):
        out["provider"] = {"@type": "Organization", "name": fac["provider"]}
    if as_of:
        out["dateModified"] = as_of
    return out
