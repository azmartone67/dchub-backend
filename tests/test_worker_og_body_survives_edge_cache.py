"""The anonymous OG card must still have a BODY after the edge cache takes its copy.

FENCES the 2026-09-08 outage (worker 4.9.60, introduced by #3991 on 2026-09-05).

Measured in production, same URL, one header apart:

    GET /api/v1/og/dynamic.png?style=editorial            -> 500  (CF 1101)
    GET  ... same URL + `X-API-Key: <anything>`            -> 200  275KB PNG
    origin dchub-backend-production.up.railway.app/...     -> 200  280KB PNG

The origin was healthy the whole time. `hasApiKey` (now `hasCredential`) is the ONLY input that moved,
because it is what clears `_pkc` and skips the public-key edge cache. So every
real visitor and every social unfurl got a blank image while every authenticated
monitor got a 200 — which is why nothing alarmed.

Cause: STEP 2 handed the origin stream to the client with
`new Response(resp.body, resp)` and THEN passed the same spent `resp` to
`assetCachePut`, which cloned it to store a copy. The worker's own tail:

    TypeError: Body has already been used. It can only be used once.
               Use tee() first if you need to read it twice.

`assetCachePut`'s try/catch could not save it: the failure surfaces as a
REJECTED promise inside `ctx.waitUntil`, which a try/catch cannot see, and an
unhandled waitUntil rejection fails the whole request.

These tests do NOT grep for the fix. They EXTRACT the shipped STEP-2 sequence and
the real `assetCachePut` out of worker.js and EXECUTE them against a streaming
body, so they fail if the BEHAVIOUR regresses — including if someone reorders the
clone back, which is the mistake that is easy to make again.
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
    """Extract a top-level declaration. Fails closed rather than yielding an
    empty block that would vacuously pass."""
    lines = src.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines) if ln.startswith(start_line))
    except StopIteration:
        raise AssertionError(f"worker.js: no line starts with {start_line!r}")
    try:
        j = next(n for n, ln in enumerate(lines[i + 1:], i + 1)
                 if ln.rstrip() == end_line)
    except StopIteration:
        raise AssertionError(f"worker.js: no {end_line!r} after {start_line!r}")
    block = "\n".join(lines[i:j + 1])
    assert len(block) > 40, f"extracted block for {start_line!r} is empty: {block!r}"
    return block


def _line(src, start):
    """One top-level single-line declaration, failing closed."""
    hit = [ln for ln in src.splitlines() if ln.startswith(start)]
    assert len(hit) == 1, f"worker.js: expected one line starting {start!r}, got {len(hit)}"
    return hit[0]


def _step2_sequence(src):
    """Extract the SHIPPED STEP-2 response-assembly statements verbatim.

    This is the ordering under test, so it must come out of worker.js rather
    than be retyped here — a copy in the test would keep passing after the
    shipped order regressed.
    """
    lines = src.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines)
                 if ln.strip() == "let cacheClone = null;")
    except StopIteration:
        raise AssertionError("worker.js: STEP 2 'let cacheClone = null;' not found")
    try:
        j = next(n for n, ln in enumerate(lines[i + 1:], i + 1)
                 if ln.strip() == "return result;")
    except StopIteration:
        raise AssertionError("worker.js: no 'return result;' after STEP 2 start")
    seq = "\n".join(lines[i:j + 1])
    assert "assetCachePut(" in seq, (
        "extracted STEP-2 block never calls assetCachePut — extraction drifted, "
        f"so this test would prove nothing. Got:\n{seq}")
    assert "new Response(resp.body, resp)" in seq, (
        "extracted STEP-2 block never hands the origin body to the client — "
        f"extraction drifted. Got:\n{seq}")
    return seq


ASSET_TIER = "{ browserMaxAge: 604800, edgeTtl: 604800, publicKeyCache: true, pkcAsset: true }"
WARM_TIER = "{ browserMaxAge: 180, edgeTtl: 300, publicKeyCache: true }"


def _run(og_path="/api/v1/og/dynamic.png", has_api_key=False, tier=ASSET_TIER,
         content_type="image/png", cache_control="public, max-age=604800, immutable",
         set_cookie=None, status=200, put_rejects=None, cache_drops=False,
         seed_readback=None, seed_put_error=None):
    """Execute the REAL assetCachePut + the REAL STEP-2 ordering on a streaming
    body, and report what the client and the cache each ended up with."""
    src = open(WORKER, encoding="utf-8").read()
    extra_hdr = f", 'set-cookie': {json.dumps(set_cookie)}" if set_cookie else ""
    js = f"""
{_block(src, "function _assetCacheKey(", "}")}
{_block(src, "function _pkcStorable(", "}")}
{_block(src, "function _pkcSkipReason(", "}")}
{_line(src, "const _pkcPutErrors = ")}
{_block(src, "function _pkcNotePutError(", "}")}
{_line(src, "const _pkcReadback = ")}
{_block(src, "function _pkcNoteReadback(", "}")}
{_block(src, "function originAllowsSharedStore(", "}")}
{_block(src, "function assetCachePut(", "}")}

// ---- stubs for everything STEP 2 closes over ------------------------------
const PNG = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 1, 2, 3, 4]);
const mkOrigin = () => new Response(
  new ReadableStream({{ start(c) {{ c.enqueue(PNG); c.close(); }} }}),
  {{ status: {status}, headers: {{ 'content-type': {json.dumps(content_type)},
                             'cache-control': {json.dumps(cache_control)}{extra_hdr} }} }});

let stored = null;
globalThis.caches = {{ default: {{
  async put(_req, res) {{
    if ({json.dumps(put_rejects)} !== null) throw new Error({json.dumps(put_rejects or "")});
    stored = new Uint8Array(await res.arrayBuffer());
  }},
  // The fake cache models both outcomes: a real store (match returns what put
  // kept) and Cloudflare accepting then dropping it (cache_drops).
  async match() {{ return ({str(cache_drops).lower()} || !stored) ? undefined : new Response(stored); }},
}} }};

const waits = [];
const ctx = {{ waitUntil: (p) => waits.push(p) }};
const env = {{ DCHUB_CACHE: null }};
const request = new Request('https://api.dchub.cloud{og_path}');
const url = new URL('https://api.dchub.cloud{og_path}?style=editorial&title=X');
const pathname = '{og_path}';
const isGet = true;
const hasCredential = {str(has_api_key).lower()};
const startTime = Date.now();
const attempts = 1;
const WORKER_VERSION = 'test';
const tier = {tier};
const _pkc = !!(tier.publicKeyCache && isGet && !hasCredential);
const _pkcStrip = !!tier.pkcAsset;
const addCORS = (r) => r;
const cacheControlFor = () => 'public, max-age=604800';
const kvIsCacheable = () => false;
const kvCacheKey = (u) => 'kv:' + u;
const kvCacheStore = async () => {{}};
const resp = mkOrigin();
// A PREVIOUS request's outcome for this key, as the isolate would hold it.
const _seedKey = _assetCacheKey(url, !!tier.pkcAsset).url;
if ({json.dumps(seed_readback)} !== null) _pkcReadback.set(_seedKey, {json.dumps(seed_readback)});
if ({json.dumps(seed_put_error)} !== null) _pkcPutErrors.set(_seedKey, {json.dumps(seed_put_error)});

// ---- the SHIPPED sequence, verbatim from worker.js -----------------------
const result = await (async () => {{
{_step2_sequence(src)}
}})();

// An unhandled waitUntil rejection is what took the route down; surface it.
let waitError = null;
try {{ await Promise.all(waits); }} catch (e) {{ waitError = String(e && e.message || e); }}

let clientBytes = null, clientError = null;
try {{ clientBytes = new Uint8Array(await result.arrayBuffer()).length; }}
catch (e) {{ clientError = String(e && e.message || e); }}

console.log(JSON.stringify({{
  client_bytes: clientBytes,
  client_error: clientError,
  stored_bytes: stored ? stored.length : null,
  wait_error: waitError,
  store_verdict: result.headers.get('x-dc-edge-store'),
  put_errors: Object.fromEntries(_pkcPutErrors),
  readback: Object.fromEntries(_pkcReadback),
  readback_header: result.headers.get('x-dc-edge-store-readback'),
  last_error_header: result.headers.get('x-dc-edge-store-last-error'),
  expected_bytes: PNG.length,
}}));
"""
    out = subprocess.run(["node", "--input-type=module", "-e", js],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    # The worker's own console.log lines ([edge-store] ...) share stdout; the
    # harness's result is always the last line.
    return json.loads(out.stdout.strip().splitlines()[-1])


# ── the outage invariant ───────────────────────────────────────────────────

def test_anonymous_card_still_has_a_body_after_the_edge_cache_copies_it():
    """THE regression. The client must receive the full image even though the
    asset cache takes its own copy of the same origin response."""
    r = _run()
    assert r["client_error"] is None, (
        "the anonymous OG card lost its body to the edge-cache copy: "
        f"{r['client_error']}. This is the 2026-09-08 blank-image outage — "
        "clone the origin response BEFORE handing resp.body to the client.")
    assert r["client_bytes"] == r["expected_bytes"], (
        f"client got {r['client_bytes']} of {r['expected_bytes']} bytes")


def test_the_cache_still_gets_its_copy():
    """Negative control: fixing the client must not silently disable caching —
    that would swap a visible outage for a 1.4s render on every request."""
    r = _run()
    assert r["stored_bytes"] == r["expected_bytes"], (
        f"asset cache stored {r['stored_bytes']} bytes, expected "
        f"{r['expected_bytes']} — the card is no longer being cached at all.")


def test_a_failing_cache_put_cannot_fail_the_request():
    """`assetCachePut` is documented 'never fail the request'. Its try/catch
    cannot see a rejected promise handed to waitUntil, so the .catch() is the
    only thing that makes the docstring true."""
    src = open(WORKER, encoding="utf-8").read()
    js = f"""
{_block(src, "function _assetCacheKey(", "}")}
{_block(src, "function assetCachePut(", "}")}
globalThis.caches = {{ default: {{ async put() {{ throw new TypeError('edge cache is angry'); }} }} }};
const waits = [];
const ctx = {{ waitUntil: (p) => waits.push(p) }};
const resp = new Response(new Uint8Array([1,2,3]), {{ status: 200 }});
assetCachePut(ctx, new URL('https://api.dchub.cloud/api/v1/og/dynamic.png'), resp, 604800);
let err = null;
try {{ await Promise.all(waits); }} catch (e) {{ err = String(e && e.message || e); }}
console.log(JSON.stringify({{ escaped: err }}));
"""
    out = subprocess.run(["node", "--input-type=module", "-e", js],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    assert json.loads(out.stdout)["escaped"] is None, (
        "a rejected caches.default.put() escaped assetCachePut into waitUntil. "
        "An unhandled waitUntil rejection fails the whole request — this is "
        "exactly how the OG route started answering 1101.")


# ── the warm (API) tier: same STEP-2 code, stricter store gate (4.9.75) ─────

def test_warm_tier_stores_a_public_api_response_and_keeps_the_client_body():
    """/api/v1/stats answers `public, ...`: it must now be stored under the
    public key, and the client must still get every byte."""
    r = _run("/api/v1/stats", tier=WARM_TIER, content_type="application/json",
             cache_control="public, max-age=300, s-maxage=300, stale-while-revalidate=86400")
    assert r["client_error"] is None and r["client_bytes"] == r["expected_bytes"]
    assert r["stored_bytes"] == r["expected_bytes"], (
        "a public warm-tier response was not stored — the warm tier would have "
        "NO edge copy at all, since it no longer passes cf.cacheTtl to the origin fetch")


@pytest.mark.parametrize("cc,cookie", [
    ("private, no-store, max-age=0", None),                # /api/v1/pipeline, measured
    ("no-store, no-cache, must-revalidate, max-age=0", None),  # /api/v1/deals, measured
    ("public, max-age=300", "sid=abc; Path=/"),           # per-visitor response
])
def test_warm_tier_never_stores_what_a_shared_cache_must_not_keep(cc, cookie):
    r = _run("/api/v1/pipeline", tier=WARM_TIER, content_type="application/json",
             cache_control=cc, set_cookie=cookie)
    assert r["client_error"] is None and r["client_bytes"] == r["expected_bytes"]
    assert r["stored_bytes"] is None, (
        f"warm tier stored a response the origin marked {cc!r} / Set-Cookie={cookie!r} "
        "under a zone-wide key — one caller's body would be served to everyone")


def test_asset_tier_still_stores_regardless_of_origin_directives():
    """The asset tier's old behaviour is kept on purpose (deterministic PNGs)."""
    r = _run(cache_control="private, max-age=0")
    assert r["stored_bytes"] == r["expected_bytes"]


# ── edge-store diagnostics (4.9.77) ────────────────────────────────────────

@pytest.mark.parametrize("kw,verdict", [
    (dict(cache_control="public, max-age=300"), "put"),
    (dict(cache_control="private, no-store, max-age=0"), "skip:origin-cache-control"),
    (dict(cache_control="public, max-age=300", set_cookie="sid=1"), "skip:set-cookie"),
    (dict(cache_control="public, max-age=300", status=404), "skip:status-404"),
])
def test_every_warm_miss_says_what_happened_to_the_store(kw, verdict):
    r = _run("/api/v1/stats", tier=WARM_TIER, content_type="application/json", **kw)
    assert r["store_verdict"] == verdict, (
        f"x-dc-edge-store said {r['store_verdict']!r}, expected {verdict!r}")
    assert r["client_error"] is None and r["client_bytes"] == r["expected_bytes"]


def test_a_rejected_put_is_recorded_not_swallowed_and_never_fails_the_request():
    """The whole point: the /stats put() rejection was invisible. It must now
    land in _pkcPutErrors (-> x-dc-edge-store-last-error on the next miss) and
    still must not escape into waitUntil."""
    r = _run("/api/v1/stats", tier=WARM_TIER, content_type="application/json",
             cache_control="public, max-age=300", put_rejects="boom: put refused")
    assert r["wait_error"] is None, f"put() rejection escaped into waitUntil: {r['wait_error']}"
    assert r["client_error"] is None and r["client_bytes"] == r["expected_bytes"]
    errs = r["put_errors"]
    assert list(errs) == ["https://api.dchub.cloud/api/v1/stats?style=editorial&title=X"], errs
    assert "boom: put refused" in next(iter(errs.values()))


def test_the_asset_tier_gets_the_verdict_header_too():
    r = _run()
    assert r["store_verdict"] == "put"


# ── readback after put (4.9.78) ────────────────────────────────────────────

@pytest.mark.parametrize("drops,verdict", [(False, "readback-hit"), (True, "readback-miss")])
def test_a_resolved_put_is_read_back_and_the_outcome_recorded(drops, verdict):
    """put() resolving is not proof of a store: /api/v1/stats resolved every
    put and never hit. The readback must say which it was."""
    r = _run("/api/v1/stats", tier=WARM_TIER, content_type="application/json",
             cache_control="public, max-age=300", cache_drops=drops)
    assert r["wait_error"] is None
    assert r["client_error"] is None and r["client_bytes"] == r["expected_bytes"]
    rb = r["readback"]
    assert list(rb) == ["https://api.dchub.cloud/api/v1/stats?style=editorial&title=X"], rb
    assert next(iter(rb.values())).endswith(verdict), rb


def test_a_rejected_put_is_not_read_back():
    r = _run("/api/v1/stats", tier=WARM_TIER, content_type="application/json",
             cache_control="public, max-age=300", put_rejects="refused")
    assert r["readback"] == {}, r["readback"]
    assert r["wait_error"] is None


def test_the_previous_outcome_is_returned_on_the_next_miss():
    """The readback and the put() error land AFTER the response they belong to,
    so the only place a curl can see them is the NEXT miss for the same key."""
    r = _run("/api/v1/stats", tier=WARM_TIER, content_type="application/json",
             cache_control="private", seed_readback="T0 readback-miss",
             seed_put_error="T0 boom")
    assert r["readback_header"] == "T0 readback-miss", r
    assert r["last_error_header"] == "T0 boom", r
