"""Fiber-surface provenance helpers (provenance v1, 2026-07-11) — PURE.

Dark-fiber honesty pass for the Gemini partnership: maps fiber_routes.source
values to the per-record confidence tier (``v``), stamps the one known
vintage-honesty case (the 2016-wayback Zayo KMZ), classifies route_type into
a dark/lit/unknown ``service_class``, and builds the collection-level
provenance block for the metro dark-fiber surface.

Lives OUTSIDE main.py so pre-merge tests (no DB / JWT_SECRET, never import
main — reference_dchub_green_main_0709) can pin the mapping:
tests/test_fiber_provenance.py.

FAIL-SOFT CONTRACT (same as routes/provenance.py): every helper catches
everything and degrades to the conservative answer — omit the field
(inherits the collection default_v) rather than over-claim.
"""
from __future__ import annotations

from routes.provenance import provenance_block

# ── source → per-record v (locked enum: verified|published|tracked|inferred) ─
#
# "published" = the geometry/attributes come from a carrier- or government-
# published dataset (surveyed carrier KMZ, NTIA middle-mile, state broadband
# offices, regional-carrier maps). "inferred" = DC Hub synthetic/seeded rows
# (endpoint pairs, metro synthesis, PeeringDB-derived presence). Unknown
# sources return None → the feature omits ``v`` and inherits the collection
# default_v (which the fiber-routes surface sets to "inferred" — the floor
# never over-claims).
PUBLISHED_SOURCE_PREFIXES = (
    "carrier_kmz",
    "ntia",
    "state_broadband",
    "regional_carrier",
)
INFERRED_SOURCE_PREFIXES = (
    "seed",
    "land_power_seed",
    "cfp_metro_synth",
    "peeringdb",
)

# Vintage-honesty case: the Zayo KMZ recovered via the Wayback Machine is a
# 2016 snapshot — per-feature as_of makes the decade-old vintage explicit.
ZAYO_2016_WAYBACK_SOURCE = "carrier_kmz:zayo_2016wayback"
ZAYO_2016_WAYBACK_AS_OF = "2016"

# What a "dark" service_class actually asserts: the carrier advertises dark
# fiber on/around this corridor — NOT confirmed strand counts or a quote.
DARK_BASIS = "carrier-advertised"

_DARK_ROUTE_TYPES = ("dark", "dark_fiber")
_LIT_ROUTE_TYPES = ("lit", "lit_fiber")

# Seed date of the metro_dark_fiber / metro_fiber_summary tables
# (scripts/Metro_dark_fiber_seed.py — public carrier materials, 2026-03-17).
METRO_FIBER_AS_OF = "2026-03-17"


def _norm(value):
    """Lower-cased, stripped string form of value; '' on any failure."""
    try:
        return str(value or "").strip().lower()
    except Exception:
        return ""


def source_v(source):
    """fiber_routes.source → per-record ``v`` ('published' | 'inferred'),
    or None for unknown sources (feature omits ``v`` → inherits the
    collection default_v). NEVER raises."""
    s = _norm(source)
    if not s:
        return None
    try:
        if s.startswith(PUBLISHED_SOURCE_PREFIXES):
            return "published"
        if s.startswith(INFERRED_SOURCE_PREFIXES):
            return "inferred"
    except Exception:
        pass
    return None


def source_as_of(source):
    """Per-feature as_of ONLY for known-vintage datasets — currently just
    the 2016-wayback Zayo KMZ → '2016'. None otherwise (bytes discipline:
    never stamp an as_of that adds no information). NEVER raises."""
    if _norm(source) == ZAYO_2016_WAYBACK_SOURCE:
        return ZAYO_2016_WAYBACK_AS_OF
    return None


def service_class(route_type):
    """fiber_routes.route_type → 'dark' | 'lit' | 'unknown'. 'dark' means the
    carrier ADVERTISES dark fiber on this corridor (pair with DARK_BASIS);
    everything not explicitly dark/lit is 'unknown' — per-route lit capacity
    is not tracked. NEVER raises."""
    t = _norm(route_type)
    if t in _DARK_ROUTE_TYPES:
        return "dark"
    if t in _LIT_ROUTE_TYPES:
        return "lit"
    return "unknown"


def metro_fiber_provenance_block():
    """Collection-level provenance for /api/v1/fiber/metro[/<market>]
    (metro_dark_fiber + metro_fiber_summary). default_v='tracked': carrier
    dark-fiber profiles collected from public carrier materials — tracked
    intelligence, not carrier-verified inventory. NEVER raises
    (provenance_block is fail-soft)."""
    return provenance_block(
        source=("DC Hub metro dark-fiber intelligence "
                "(metro_dark_fiber + metro_fiber_summary)"),
        method=("carrier dark-fiber profiles collected from public carrier "
                "materials (service pages, route maps, press releases, FCC "
                "filings); route_miles_approx and on_net_buildings are "
                "approximations, not surveyed or carrier-confirmed figures"),
        as_of=METRO_FIBER_AS_OF,
        default_v="tracked",
    )
