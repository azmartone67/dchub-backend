"""OG cards must be cached under a key THIS ZONE OWNS (2026-09-06).

`proxyToRailway` fetches `RAILWAY_BACKEND + pathname` with
`cf: {cacheTtl, cacheEverything: true}`. The Cloudflare cache key for a
subrequest is the URL being fetched — so the generated cards were stored under
`dchub-backend-production.up.railway.app/...`, a key the dchub.cloud zone does
not own. `purge_cache {files: [...]}` therefore could not address them.

Measured 2026-09-06, one purge call, both URLs reported success:true by CF:

    /images/og-default.png   -> cf-cache-status: MISS   (a normal zone object)
    the homepage CARD        -> cf-cache-status: HIT, age 2250 -> 2326

The asset tier now caches through the Cache API keyed on the PUBLIC request,
and passes edgeTtl 0 to the proxy so the unpurgeable copy is not recreated.

These tests EXECUTE the worker's real declarations under node — they fail on a
behaviour regression, not on an edited comment.
"""
import json
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO, "worker.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")


def _block(src, start, end):
    lines = src.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines) if ln.startswith(start))
    except StopIteration:
        raise AssertionError(f"worker.js: no line starts with {start!r}")
    try:
        j = next(n for n, ln in enumerate(lines[i + 1:], i + 1) if ln.rstrip() == end)
    except StopIteration:
        raise AssertionError(f"worker.js: no {end!r} after {start!r}")
    out = "\n".join(lines[i:j + 1])
    assert len(out) > 40, f"empty block for {start!r}"
    return out


def _node(expr, extra=""):
    src = open(WORKER, encoding="utf-8").read()
    js = "\n".join([
        _block(src, "const CACHE_TIERS = {", "};"),
        _block(src, "const ROUTE_CACHE_MAP = [", "];"),
        _block(src, "function getRouteTier(", "}"),
        _block(src, "function _assetCacheKey(", "}"),
        extra,
        f"console.log(JSON.stringify({expr}));",
    ])
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


def test_the_og_tier_opts_into_the_public_cache_key():
    """Without this flag the cards go back to being cached under the Railway
    URL, where no purge can reach them."""
    tier = _node("getRouteTier('/api/v1/og/dynamic.png')")
    assert tier.get("publicKeyCache") is True, (
        "the OG tier no longer requests a public cache key — purge-by-URL "
        "cannot address Railway-keyed objects")


def test_live_api_tiers_do_not_take_the_new_path():
    """Scoped on purpose: this is the card path, not the API hot path."""
    # `!!` because JSON.stringify(undefined) emits nothing at all, which would
    # come back as a decode error rather than a clean false.
    for p in ("/api/v1/stats/canonical", "/api/news", "/api/v1/facilities"):
        assert _node(f"!!getRouteTier({p!r}).publicKeyCache") is False, \
            f"{p} was switched onto the asset cache path"


def test_the_cache_key_is_the_public_url_not_the_origin():
    key = _node("_assetCacheKey(new URL('https://api.dchub.cloud/api/v1/og/"
                "dynamic.png?style=editorial&title=X')).url")
    assert key.startswith("https://api.dchub.cloud/"), \
        f"cache key is not on our zone: {key}"
    assert "railway" not in key, f"cache key still points at the origin: {key}"
    assert "style=editorial" in key and "title=X" in key, \
        "query params must stay in the key — cards differ only by query string"


def test_cache_busting_noise_is_stripped_from_the_key():
    """A ?_=<ms> probe must not create a one-shot entry that a later
    purge-by-URL could never name."""
    a = _node("_assetCacheKey(new URL('https://api.dchub.cloud/api/v1/og/"
              "dynamic.png?style=editorial&_=1757030000000')).url")
    b = _node("_assetCacheKey(new URL('https://api.dchub.cloud/api/v1/og/"
              "dynamic.png?style=editorial&_=1757039999999')).url")
    assert a == b, f"cache-buster survived into the key: {a} vs {b}"
    assert "_=" not in a


def test_the_proxy_is_told_not_to_double_cache_under_the_origin_key():
    """The edgeTtl passed to proxyWithRetry must be 0 for publicKeyCache tiers.
    Caching in BOTH places recreates the unpurgeable copy."""
    src = open(WORKER, encoding="utf-8").read()
    line = next((l for l in src.splitlines() if l.strip().startswith("const edgeTtl =")), None)
    assert line, "the edgeTtl derivation moved — re-check this guard"
    assert "!_pkc" in line, (
        f"publicKeyCache tiers still pass a non-zero edgeTtl, so cf.cacheEverything "
        f"will cache them under the Railway URL again: {line.strip()}")


def test_the_asset_cache_is_actually_consulted_and_filled():
    src = open(WORKER, encoding="utf-8").read()
    assert "await assetCacheMatch(url)" in src, "nothing reads the public-key cache"
    assert "assetCachePut(ctx, url, resp, tier.edgeTtl)" in src, \
        "nothing writes the public-key cache — it would miss forever"
    assert "x-dc-hub-backend', 'edge-asset-cache'" in src, \
        "a hit from this cache is indistinguishable from an origin fetch"


def test_only_a_200_is_stored():
    """caches.default.put() rejects non-200s, and storing an error under a
    7-day key would pin a failure into the fleet."""
    src = open(WORKER, encoding="utf-8").read()
    fn = _block(src, "function assetCachePut(", "}")
    assert "resp.status !== 200" in fn, "assetCachePut would store non-200 responses"
