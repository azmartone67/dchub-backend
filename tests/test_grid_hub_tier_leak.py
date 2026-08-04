#!/usr/bin/env python3
"""The /grid hub is tier-varying and must never be publicly cacheable.

`grid_hub()` calls `_user_tier(request)` and gates 7 of the 9 ISO cards for free
callers (FREE_TIER_ISOS = PJM, ERCOT), nulling demand_mw / headroom_pct /
gen_mix. It returned a plain `Response(html)` with no Cache-Control, so the
main.py catch-all stamped `public, max-age=300, s-maxage=300` — a blanket
default for handlers that set nothing, not a decision about this page.

Measured live before the fix, on a fresh cache-busted URL:

    seed WITH  X-API-Key  ->  cf-cache-status: MISS   (populates the entry)
    then ANON  same URL   ->  cf-cache-status: HIT age:0

A Pro key holder's full render was therefore served to anonymous visitors. The
zone's tier-varying bypass could not prevent it: that rule keys on the
dchub_token / dchub_refresh COOKIE, while `_user_tier` resolves paid tier from
the X-API-Key HEADER first — the caller shape an MCP or API client uses.

Its own children `/grid/<iso>` were hardened for this on 2026-06-23; the hub was
missed. These tests pin the hub to the same guard, and pin the reason so the
header cannot be "cleaned up" later by someone who sees a public page and
assumes a public cache is fine.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "routes" / "grid_public_routes.py").read_text()


def _fn(name: str) -> str:
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SRC, node) or ""
    raise AssertionError(f"{name} not found — renamed?")


def _code_only(src: str) -> str:
    """Strip comments — a header named in prose must not satisfy the assertion."""
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


class TestHubIsNotPubliclyCacheable:
    def test_the_hub_sets_no_store(self):
        src = _code_only(_fn("grid_hub"))
        assert "no-store" in src, \
            "a tier-varying body with no Cache-Control inherits the main.py " \
            "catch-all's public, max-age=300 and is stored per-URL"

    def test_the_hub_sets_cdn_cache_control_too(self):
        # Cache-Control alone is not enough at this edge: CDN-Cache-Control is
        # what the sibling handlers set, and it is read in preference by CF.
        src = _code_only(_fn("grid_hub"))
        assert "CDN-Cache-Control" in src

    def test_the_hub_matches_its_own_children(self):
        # The children were hardened first; the hub must not drift from them.
        hub = _code_only(_fn("grid_hub"))
        child = _code_only(_fn("grid_iso"))
        for header in ('"Cache-Control": "private, no-store, max-age=0"',
                       '"CDN-Cache-Control": "no-store"'):
            assert header in child, f"the child stopped setting {header}"
            assert header in hub, f"the hub does not match the child on {header}"


class TestTheGateStillExists:
    def test_the_hub_still_resolves_tier(self):
        # If this ever stops being true the page is no longer tier-varying and
        # the no-store could be revisited — but that must be a deliberate
        # change, not a silent one.
        assert "_user_tier(request)" in _code_only(_fn("grid_hub"))

    def test_free_tier_isos_are_still_a_subset(self):
        # The leak only matters because most ISOs are gated. If FREE_TIER_ISOS
        # ever covered everything, the page would be uniform and this test
        # should be revisited deliberately.
        assert "FREE_TIER_ISOS" in SRC
        tree = ast.parse(SRC)
        free = isos = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if getattr(t, "id", None) == "FREE_TIER_ISOS":
                    free = len(getattr(node.value, "elts", []))
                if getattr(t, "id", None) == "ISOS":
                    isos = len(getattr(node.value, "keys", []))
        assert free and isos and free < isos, \
            f"FREE_TIER_ISOS={free} of ISOS={isos} — if all ISOs became free " \
            "the page would stop being tier-varying; revisit deliberately"
