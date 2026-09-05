"""Edge cache guard for the OG/social cards (2026-09-05).

FENCES two defects measured in production on 2026-09-05, worker 4.9.52:

  origin  https://dchub-backend-production.up.railway.app/api/v1/og/dynamic.png
          cache-control: public, max-age=604800, immutable
  edge    https://api.dchub.cloud/api/v1/og/dynamic.png
          cache-control: public, max-age=180, stale-while-revalidate=360

  1. `/api/v1/og/` had NO entry in ROUTE_CACHE_MAP, so getRouteTier fell through
     to the `warm` default (browserMaxAge 180, edgeTtl 300) that exists for
     volatile API data. edgeTtl is passed to CF as `cf.cacheTtl`, which
     OVERRIDES origin headers, so the edge re-fetched — and the origin re-ran a
     ~1.4s PIL render — every 5 minutes, forever.
  2. The anonymous-GET path rewrote Cache-Control to the tier default
     unconditionally. `immutable` was discarded outright.

These tests do NOT grep for the fix. They EXTRACT the real declarations out of
worker.js and EXECUTE them in node, so they exercise the shipped logic and fail
if its BEHAVIOUR regresses — not merely if a string is edited.
"""
import json
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO, "worker.js")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available on this host"
)


def _block(src, start_line, end_line):
    """Extract a top-level declaration: from the line starting with
    `start_line` through the first subsequent line equal to `end_line`.

    Fails closed — a formatting change that breaks extraction raises here
    rather than silently yielding an empty block that would vacuously pass.
    """
    lines = src.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines) if ln.startswith(start_line))
    except StopIteration:
        raise AssertionError(f"worker.js: no line starts with {start_line!r}")
    try:
        j = next(n for n, ln in enumerate(lines[i + 1:], i + 1)
                 if ln.rstrip() == end_line)
    except StopIteration:
        raise AssertionError(f"worker.js: no {end_line!r} terminator after {start_line!r}")
    block = "\n".join(lines[i:j + 1])
    assert len(block) > 40, f"extracted block for {start_line!r} looks empty: {block!r}"
    return block


def _harness(expr):
    """Eval the REAL worker declarations plus `expr`, return the JSON result."""
    src = open(WORKER, encoding="utf-8").read()
    js = "\n".join([
        _block(src, "const CACHE_TIERS = {", "};"),
        _block(src, "const ROUTE_CACHE_MAP = [", "];"),
        _block(src, "function getRouteTier(", "}"),
        _block(src, "function cacheControlFor(", "}"),
        f"console.log(JSON.stringify({expr}));",
    ])
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


# ── 1. the OG prefix must resolve to a long-lived tier ──────────────────────

def test_og_cards_are_not_on_the_volatile_warm_default():
    """A 1.4s render behind a 300s edge TTL is the whole bug. Both the edge TTL
    and the client max-age must be at least a day."""
    tier = _harness("getRouteTier('/api/v1/og/dynamic.png')")
    assert tier["edgeTtl"] >= 86400, (
        f"OG edgeTtl is {tier['edgeTtl']}s — CF will re-fetch and the origin will "
        f"re-render the card that often. This is the 2026-09-05 regression.")
    assert tier["browserMaxAge"] >= 86400, (
        f"OG browserMaxAge is {tier['browserMaxAge']}s — every crawler re-requests "
        f"that often.")


def test_og_cards_stay_out_of_the_kv_text_lane():
    """kvCacheStore does JSON.stringify(await resp.text()) and this worker has
    no isTextualContentType guard, so PNG bytes must never become KV-cacheable."""
    tier = _harness("getRouteTier('/api/v1/og/dynamic.png')")
    assert tier["kvStaleTtl"] == 0 and tier["kvFreshTtl"] == 0, (
        "OG cards are binary; a non-zero KV TTL routes PNG bytes through the "
        f"JSON text lane. Got {tier}")


def test_the_change_did_not_widen_caching_for_live_api_data():
    """Negative control: the fix must be scoped. Volatile endpoints keep warm."""
    for path in ("/api/v1/stats/canonical", "/api/v1/deals"):
        tier = _harness(f"getRouteTier({path!r})")
        assert tier["browserMaxAge"] == 180 and tier["edgeTtl"] == 300, (
            f"{path} should still be warm(180/300), got {tier}")


# ── 2. the rewrite must not shorten what the origin asked for ───────────────

def test_the_measured_production_header_is_no_longer_clamped():
    """The exact origin header measured on 2026-09-05 must survive untouched."""
    got = _harness("cacheControlFor('public, max-age=604800, immutable', 180)")
    assert got is None, (
        f"origin asked for 604800s and the worker still rewrites it to {got!r} — "
        f"this is the exact production defect.")


def test_tier_default_still_applies_when_origin_asks_for_less_or_nothing():
    """Existing behaviour must be preserved everywhere the origin did NOT ask
    for longer — otherwise this 'fix' is a blanket cache widening."""
    expected = "public, max-age=180, stale-while-revalidate=360"
    assert _harness("cacheControlFor(null, 180)") == expected
    assert _harness("cacheControlFor('public, max-age=60', 180)") == expected
    # equal is not greater — the tier still wins
    assert _harness("cacheControlFor('public, max-age=180', 180)") == expected


def test_origin_no_store_is_never_widened_into_a_public_cache():
    for cc in ("no-store", "private, max-age=0", "no-cache",
               "no-store, no-cache, must-revalidate, private"):
        assert _harness(f"cacheControlFor({cc!r}, 180)") is None, (
            f"origin said {cc!r} and the worker turned it into a public cache")


def test_a_tier_that_opted_out_of_rewriting_still_opts_out():
    """browserMaxAge 0 (the `none`/`emergency` tiers) must never set a header."""
    assert _harness("cacheControlFor('public, max-age=604800', 0)") is None
    assert _harness("cacheControlFor(null, 0)") is None
