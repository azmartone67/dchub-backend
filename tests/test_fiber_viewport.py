"""tests/test_fiber_viewport.py — the carrier-identity and vertex-budget rules
behind the 2026-09-03 fiber viewport fix.

Context. A customer compared the Land & Power map to FiberLocator at
2675 Olthoff Dr, Muskegon MI and we showed Bluebird where they showed Zayo,
Uniti, US Signal and 123Net. None of that was missing data — fiber_routes held
64,836 routes and 19,545 of them are Zayo. Three things hid them:

  1. ?bbox= tested the route's START POINT, so a route crossing the viewport
     without beginning in it was dropped. (SQL + migration; the SQL half is
     verified against a real Postgres in the PR, not here — these tests take
     no DB.)
  2. ?carrier= was exact `provider = %s` against a column holding one carrier
     under several spellings.
  3. Every response was clamped to 2,000 rows, so `total` sat on the cap and
     read as a count.

These cover (2) and the vertex budget that makes raising (3) safe.

House rules: no DB, no network, never import main.py. Everything under test is
the REAL production function imported from routes/fiber_viewport — not a
restatement of it here.

Run:  python3 -m pytest tests/test_fiber_viewport.py -v
"""
from __future__ import annotations

import pytest

from routes.fiber_viewport import (MIN_PREFIX_LEN, match_carriers, norm_carrier,
                                   thin_coords, thinning_stride)

# The spellings actually present in fiber_routes.provider, measured
# 2026-09-03. Route counts in comments are that day's live values.
KNOWN = [
    "123Net",                        # 8
    "123NET",                        # 7
    "Cogent",                        # 115
    "Cogent Communications, Inc.",   # 11
    "GTT",                           # 74
    "GTT Communications (AS3257)",   # 14
    "Windstream",                    # 74
    "Windstream Wholesale",          # 14
    "Uniti",                         # 69
    "Unitas Global",                 # 2   <- must NEVER fold into Uniti
    "Zayo",                          # 19,545
    "US Signal",                     # 7
    "Bluebird Network",              # 11,193
    "Everstream",                    # 21
]


# ─── norm_carrier ────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("123Net", "123NET"),
    ("123 Net", "123net"),
    ("AT&T", "ATT"),
    ("Cogent Communications, Inc.", "cogentcommunicationsinc"),
])
def test_norm_folds_case_and_punctuation(a, b):
    assert norm_carrier(a) == norm_carrier(b)


@pytest.mark.parametrize("junk", [None, "", "   ", "!!!", "-,.()"])
def test_norm_of_nothing_is_empty(junk):
    assert norm_carrier(junk) == ""


# ─── match_carriers: the bug ─────────────────────────────────────────────

def test_case_variant_spellings_are_folded_together():
    """The reported shape: `provider = '123Net'` returned 8 of 15 routes."""
    assert match_carriers("123Net", KNOWN) == ["123Net", "123NET"]
    # and it is symmetric — either spelling finds both
    assert match_carriers("123NET", KNOWN) == ["123Net", "123NET"]


@pytest.mark.parametrize("query,expected", [
    ("Cogent",     ["Cogent", "Cogent Communications, Inc."]),
    ("GTT",        ["GTT", "GTT Communications (AS3257)"]),
    ("Windstream", ["Windstream", "Windstream Wholesale"]),
])
def test_legal_suffixes_and_asn_tags_fold_into_the_parent(query, expected):
    assert match_carriers(query, KNOWN) == expected


# ─── match_carriers: what must NOT happen ────────────────────────────────

def test_uniti_does_not_swallow_unitas_global():
    """The reason the rule is normalized PREFIX and not substring.

    'unit' is a substring of both 'uniti' and 'unitasglobal'; a substring rule
    would report Unitas Global's routes as Uniti's — inventing coverage for a
    carrier at an address, which is worse than the bug being fixed.
    """
    assert match_carriers("Uniti", KNOWN) == ["Uniti"]
    assert "Unitas Global" not in match_carriers("Uniti", KNOWN)


def test_unknown_carrier_returns_empty_not_everything():
    """[] means "unknown carrier" and the caller renders an empty result. If
    this ever returned the full list, an unrecognised name would silently
    return EVERY carrier's routes."""
    assert match_carriers("NotACarrier", KNOWN) == []
    assert match_carriers("", KNOWN) == []
    assert match_carriers(None, KNOWN) == []


def test_short_queries_never_match_by_prefix():
    """A 1-2 char query matches only exactly. 'US' must not sweep in every
    carrier whose name starts with those letters."""
    assert len("US") < MIN_PREFIX_LEN
    assert match_carriers("US", KNOWN) == []
    assert match_carriers("US Signal", KNOWN) == ["US Signal"]


def test_a_carrier_is_never_matched_by_a_word_it_merely_contains():
    assert match_carriers("Network", KNOWN) == []      # not Bluebird Network
    assert match_carriers("Communications", KNOWN) == []
    assert match_carriers("Stream", KNOWN) == []       # not Everstream


def test_match_is_stable_and_order_follows_input():
    assert match_carriers("Cogent", KNOWN) == match_carriers("Cogent", KNOWN)
    assert match_carriers("cogent", list(reversed(KNOWN))) == [
        "Cogent Communications, Inc.", "Cogent"]


def test_blank_and_none_names_in_the_column_are_skipped():
    """provider is nullable; a NULL row must not crash the resolver or match
    an empty query."""
    assert match_carriers("Zayo", [None, "", "   ", "Zayo"]) == ["Zayo"]


# ─── vertex budget ───────────────────────────────────────────────────────

def test_under_budget_means_no_thinning():
    assert thinning_stride(1000, 300000) == 1
    assert thin_coords([[0, 0], [1, 1], [2, 2]], 1) == [[0, 0], [1, 1], [2, 2]]


def test_stride_brings_the_total_under_budget():
    for total in (300001, 500000, 2_000_000, 27_388 * 100):
        s = thinning_stride(total, 300000)
        assert s > 1
        assert total // s <= 300000, f"stride {s} leaves {total // s} vertices"


def test_thinning_preserves_both_endpoints():
    """Endpoints are where a route meets a building. A thinned route may lose
    interior detail but must still start and end where the real one does."""
    coords = [[i, i] for i in range(100)]
    out = thin_coords(coords, 7)
    assert out[0] == coords[0]
    assert out[-1] == coords[-1]
    assert len(out) < len(coords)


def test_thinning_never_drops_a_route_below_a_drawable_line():
    """The budget spends bytes on PRESENCE. A route reduced to <2 points would
    vanish from the map, which is the exact failure this whole change fixes."""
    for n in (2, 3, 5, 50):
        coords = [[i, i] for i in range(n)]
        for stride in (2, 10, 1000, 10**6):
            out = thin_coords(coords, stride)
            assert len(out) >= 2, f"n={n} stride={stride} -> {out}"
            assert out[0] == coords[0] and out[-1] == coords[-1]


def test_a_closed_ring_survives_an_enormous_stride():
    """★ The case that makes the `len(out) < 2` guard non-vacuous.

    Metro fiber is full of RINGS, where the last vertex equals the first. For
    a ring, `coords[::stride]` with a large stride yields a single point AND
    the "append the last vertex" step is a no-op — because the last vertex IS
    the first — so the result would be a 1-point 'line' that Leaflet cannot
    draw. The ring would vanish from the map.

    Found by mutation testing: deleting the guard left every other test in
    this file green, because none of them used a ring.
    """
    ring = [[-87.68, 41.91], [-87.64, 41.88], [-87.66, 41.86],
            [-87.70, 41.87], [-87.71, 41.90], [-87.68, 41.91]]
    assert ring[0] == ring[-1], "fixture must actually be a closed ring"
    for stride in (2, 5, 50, 10**6):
        out = thin_coords(ring, stride)
        assert len(out) >= 2, f"ring collapsed to {out} at stride {stride}"
        assert out[0] == ring[0]


def test_two_point_lines_are_returned_untouched():
    coords = [[-86.2, 43.2], [-85.7, 42.9]]
    assert thin_coords(coords, 99) == coords


def test_degenerate_budget_does_not_raise():
    """A misconfigured budget must not blow up inside a response builder."""
    assert thinning_stride(1000, 0) == 1
    assert thinning_stride(1000, -5) == 1
    assert thin_coords([], 5) == []
    assert thin_coords(None, 5) is None
