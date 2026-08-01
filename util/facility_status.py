"""Canonical facility-status vocabulary + the write-boundary normalizer.

2026-07-31: discovery ingest paths had been stamping nonconforming lowercase
statuses onto discovered_facilities — 10,435 rows of `'active'` (the
_stage_facilities_batch / _stage_facility defaults and the PeeringDB batch
importer) and osm_crawler's `'operational'` — while the canonical vocabulary
is Title-Case. Consumers then keyed on the drifted literal (routes/radar.py
excludes status='active' as its empty-shell proxy). Every ingest write path
now routes its status through canon_status() so only canonical casing lands.

Semantics: discovery sources (OSM telecom=data_center tags, PeeringDB
listings, DataCenterMap) list facilities that exist and operate, so their
"active"-style statuses mean Operational. Unknown non-empty values pass
through unchanged — visible in the status enum for future canon additions,
never silently rewritten.
"""

CANONICAL_STATUSES = frozenset({
    "Operational",
    "Under Construction",
    "Planned",
    "Announced",
    "Approved",
    "Expanding",
    "Under Development",
})

_ALIASES = {
    "operational":        "Operational",
    "active":             "Operational",
    "live":               "Operational",
    "operating":          "Operational",
    "open":               "Operational",
    "under construction": "Under Construction",
    "construction":       "Under Construction",
    "planned":            "Planned",
    "proposed":           "Planned",
    "announced":          "Announced",
    "approved":           "Approved",
    "expanding":          "Expanding",
    "under development":  "Under Development",
    "development":        "Under Development",
}


def canon_status(raw, default=None):
    """Map an ingest-supplied status onto the canonical Title-Case vocabulary.

    Empty/None -> ``default`` (facility ingest paths pass 'Operational' —
    see module docstring). Known variants map case-insensitively onto canon;
    unknown non-empty values pass through UNCHANGED.
    """
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    if s in CANONICAL_STATUSES:
        return s
    return _ALIASES.get(s.lower(), s)
