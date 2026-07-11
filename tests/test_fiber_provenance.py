"""Dark-fiber provenance honesty pass (2026-07-11) — source→v mapping,
vintage as_of, service_class, and the metro dark-fiber collection stamp.

House rules (reference_dchub_green_main_0709): pre-merge pytest has NO
DB / JWT_SECRET and must NEVER import main. Everything here runs against
routes/fiber_provenance.py (pure) + routes/provenance.py (pure).

Run:  python3 -m pytest tests/test_fiber_provenance.py -v
"""
from __future__ import annotations

import pytest

from routes.fiber_provenance import (
    DARK_BASIS,
    INFERRED_SOURCE_PREFIXES,
    METRO_FIBER_AS_OF,
    PUBLISHED_SOURCE_PREFIXES,
    ZAYO_2016_WAYBACK_AS_OF,
    ZAYO_2016_WAYBACK_SOURCE,
    metro_fiber_provenance_block,
    service_class,
    source_as_of,
    source_v,
)


# ─── source → v mapping (fiber_routes.source) ─────────────────────────────

@pytest.mark.parametrize("source", [
    "carrier_kmz",
    "carrier_kmz:zayo_2016wayback",
    "carrier_kmz:lumen",
    "ntia",
    "ntia_middle_mile",
    "state_broadband",
    "state_broadband_va",
    "regional_carrier",
    "regional_carrier:firstlight",
])
def test_source_v_published_datasets(source):
    """Carrier/government-published datasets → v='published' (prefix match)."""
    assert source_v(source) == "published"


@pytest.mark.parametrize("source", [
    "seed",
    "land_power_seed",
    "cfp_metro_synth",
    "peeringdb",
    "peeringdb_ix",
])
def test_source_v_inferred_datasets(source):
    """DC Hub synthetic/seeded rows → v='inferred'."""
    assert source_v(source) == "inferred"


@pytest.mark.parametrize("source", [None, "", "  ", "osm", "kmz",
                                    "manual_entry", 42])
def test_source_v_unknown_omits(source):
    """Unknown sources → None: the feature omits ``v`` and inherits the
    collection default_v (which the fiber-routes stamp sets to 'inferred' —
    floors never over-claim)."""
    assert source_v(source) is None


def test_source_v_case_and_whitespace_insensitive():
    assert source_v("  Carrier_KMZ:Zayo  ") == "published"
    assert source_v("PeeringDB") == "inferred"


def test_source_v_never_raises():
    class Boom:
        def __str__(self):
            raise RuntimeError("boom")
    assert source_v(Boom()) is None


def test_source_v_values_stay_in_locked_enum():
    """v1 enum lock: only {verified, published, tracked, inferred} may ever
    be emitted; this mapper only produces the two fiber tiers."""
    tiers = {source_v(p) for p in
             PUBLISHED_SOURCE_PREFIXES + INFERRED_SOURCE_PREFIXES}
    assert tiers == {"published", "inferred"}
    assert tiers <= {"verified", "published", "tracked", "inferred"}


def test_prefix_tables_do_not_overlap():
    """A source must never be classifiable as both published and inferred."""
    for pub in PUBLISHED_SOURCE_PREFIXES:
        for inf in INFERRED_SOURCE_PREFIXES:
            assert not pub.startswith(inf)
            assert not inf.startswith(pub)


# ─── vintage honesty: per-feature as_of ───────────────────────────────────

def test_zayo_2016_wayback_gets_vintage_as_of():
    assert ZAYO_2016_WAYBACK_SOURCE == "carrier_kmz:zayo_2016wayback"
    assert source_as_of("carrier_kmz:zayo_2016wayback") \
        == ZAYO_2016_WAYBACK_AS_OF == "2016"
    assert source_as_of("  CARRIER_KMZ:ZAYO_2016WAYBACK ") == "2016"


@pytest.mark.parametrize("source", [
    None, "", "carrier_kmz", "carrier_kmz:zayo", "ntia", "seed",
])
def test_other_sources_get_no_per_feature_as_of(source):
    """Bytes discipline: as_of ONLY where it adds vintage information."""
    assert source_as_of(source) is None


# ─── service_class: dark | lit | unknown ─────────────────────────────────

@pytest.mark.parametrize("route_type", ["dark", "dark_fiber", "DARK",
                                        " Dark_Fiber "])
def test_service_class_dark(route_type):
    assert service_class(route_type) == "dark"


@pytest.mark.parametrize("route_type", ["lit", "lit_fiber"])
def test_service_class_lit(route_type):
    assert service_class(route_type) == "lit"


@pytest.mark.parametrize("route_type", [
    None, "", "metro", "metro_ring", "longhaul", "long_haul",
    "ix_interconnect", "dc_interconnect", "enterprise_lateral", 7,
])
def test_service_class_unknown_is_the_floor(route_type):
    """Anything not explicitly dark/lit is 'unknown' — per-route lit
    capacity is not tracked, so 'lit' must never be assumed."""
    assert service_class(route_type) == "unknown"


def test_dark_basis_names_the_claim():
    """'dark' asserts a carrier ADVERTISES dark fiber on the corridor —
    not confirmed strands. The paired dark_basis field says so."""
    assert DARK_BASIS == "carrier-advertised"


# ─── metro dark-fiber collection stamp shape ─────────────────────────────

def test_metro_stamp_shape():
    blk = metro_fiber_provenance_block()
    # v1 lock keys always present.
    assert blk["provenance_version"] == 1
    assert blk["license"] == "CC-BY-4.0"
    assert blk["cite_as"] == "DC Hub, dchub.cloud"
    assert blk["fallback_url"] == "https://dchub.cloud"
    # Carrier profiles from public materials = tracked intelligence — not
    # carrier-verified inventory, not published geometry.
    assert blk["default_v"] == "tracked"
    # Seed vintage is explicit (scripts/Metro_dark_fiber_seed.py).
    assert blk["as_of"] == METRO_FIBER_AS_OF == "2026-03-17"
    # Method names the truth: public carrier materials + approximations.
    assert "public carrier" in blk["method"]
    assert "approximation" in blk["method"]
    assert "metro_dark_fiber" in blk["source"]
