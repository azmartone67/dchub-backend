"""The anonymous OG card must still have a BODY after the edge cache takes its copy.

FENCES the 2026-09-08 outage (worker 4.9.60, introduced by #3991 on 2026-09-05).

Measured in production, same URL, one header apart:

    GET /api/v1/og/dynamic.png?style=editorial            -> 500  (CF 1101)
    GET  ... same URL + `X-API-Key: <anything>`            -> 200  275KB PNG
    origin dchub-backend-production.up.railway.app/...     -> 200  280KB PNG

The origin was healthy the whole time. `hasApiKey` is the ONLY input that moved,
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


def _run(og_path="/api/v1/og/dynamic.png", has_api_key=False):
    """Execute the REAL assetCachePut + the REAL STEP-2 ordering on a streaming
    body, and report what the client and the cache each ended up with."""
    src = open(WORKER, encoding="utf-8").read()
    js = f"""
{_block(src, "function _assetCacheKey(", "}")}
{_block(src, "function assetCachePut(", "}")}

// ---- stubs for everything STEP 2 closes over ------------------------------
const PNG = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 1, 2, 3, 4]);
const mkOrigin = () => new Response(
  new ReadableStream({{ start(c) {{ c.enqueue(PNG); c.close(); }} }}),
  {{ status: 200, headers: {{ 'content-type': 'image/png',
                             'cache-control': 'public, max-age=604800, immutable' }} }});

let stored = null;
globalThis.caches = {{ default: {{
  async put(_req, res) {{ stored = new Uint8Array(await res.arrayBuffer()); }},
  async match() {{ return null; }},
}} }};

const waits = [];
const ctx = {{ waitUntil: (p) => waits.push(p) }};
const env = {{ DCHUB_CACHE: null }};
const request = new Request('https://api.dchub.cloud{og_path}');
const url = new URL('https://api.dchub.cloud{og_path}?style=editorial&title=X');
const pathname = '{og_path}';
const isGet = true;
const hasApiKey = {str(has_api_key).lower()};
const startTime = Date.now();
const attempts = 1;
const WORKER_VERSION = 'test';
const tier = {{ browserMaxAge: 604800, edgeTtl: 604800, publicKeyCache: true }};
const _pkc = !!(tier.publicKeyCache && isGet && !hasApiKey);
const addCORS = (r) => r;
const cacheControlFor = () => 'public, max-age=604800';
const kvIsCacheable = () => false;
const kvCacheKey = (u) => 'kv:' + u;
const kvCacheStore = async () => {{}};
const resp = mkOrigin();

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
  expected_bytes: PNG.length,
}}));
"""
    out = subprocess.run(["node", "--input-type=module", "-e", js],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


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
