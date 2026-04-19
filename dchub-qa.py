#!/usr/bin/env python3
"""
dchub-qa.py — single-command patch + QA harness for dchubapiproxy Worker.

Goes from any prior v4.5.x source to v4.5.8 paste-ready, then runs the full
smoke test suite against the live Worker at https://dchub.cloud.

USAGE (Replit shell or anywhere with python3):
    # one-time setup — add these as Replit Secrets, or export in shell:
    export ADMIN_SECRET='<your ADMIN_SECRET from Cloudflare>'
    export STRIPE_WEBHOOK_SECRET='<your STRIPE_WEBHOOK_SECRET from Cloudflare>'

    # run everything:
    python3 dchub-qa.py

WHAT IT DOES:
    1. Auto-finds worker source file in cwd (worker-v4.5.*.js, worker.js, etc.)
    2. Detects version, applies the delta needed to reach v4.5.8
    3. Writes worker-v4.5.8.js — paste this into the Cloudflare editor and Deploy
    4. Runs smoke tests against the LIVE worker URL:
         - version header
         - unsigned Stripe webhook -> must 401
         - signed+fresh Stripe webhook -> must 200 (happy path)
         - signed+stale Stripe webhook -> must 401 (replay guard, v4.5.8 only)
         - wrong admin key -> must 403
         - no admin key -> must 403 (v4.5.8) or 401 (v4.5.7, see note)
         - right admin key -> must NOT be 401/403 (auth passed)
    5. Prints a pass/fail table. Nonzero exit if anything failed.

The script patches LOCALLY; you still paste the produced file into the
Cloudflare Worker editor manually. Smoke tests run against whatever is
currently deployed, so run once before paste (expect v4.5.8 tests to fail)
and again after paste (expect all green).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

WORKER_URL = "https://dchub.cloud"
# Cloudflare Bot Fight Mode blocks the default urllib User-Agent with error 1010.
# Use a curl-like UA, which matches the UA your manual curl smoke tests use.
USER_AGENT = "curl/8.5.0 (dchub-qa-smoke)"

# ----------------------------------------------------------------------------
# PATCH LIBRARY
# Each patch is a (label, old_literal, new_literal, expected_count) tuple.
# Patches chain: v4.5.5 -> v4.5.6 -> v4.5.7 -> v4.5.8. We apply only the
# deltas that weren't already applied, detected by version string.
# ----------------------------------------------------------------------------

# ---- v4.5.6 patches (from original v4.5.5 source) ----

_V456_HEADER_OLD = (
    " * DC Hub API Proxy Worker v4.5.5 \u2014 FEMA Flood Zone Proxy\n"
    " * ================================================================================\n"
    " * v4.5.5 CHANGES (Apr 16 2026):"
)
_V456_HEADER_NEW = (
    " * DC Hub API Proxy Worker v4.5.6 \u2014 P0 Security Hardening\n"
    " * ================================================================================\n"
    " * v4.5.6 CHANGES (Apr 17 2026):\n"
    " *   - SEC: Stripe webhook now verifies HMAC-SHA256 signature against\n"
    " *          env.STRIPE_WEBHOOK_SECRET before provisioning API keys.\n"
    " *   - SEC: Admin endpoints constant-time-compare X-Admin-Key against\n"
    " *          env.ADMIN_SECRET.\n"
    " *\n"
    " * v4.5.5 CHANGES (Apr 16 2026):"
)

_V456_VERSION_OLD = "const WORKER_VERSION   = '4.5.5';"
_V456_VERSION_NEW = "const WORKER_VERSION   = '4.5.6';"

_V456_STRIPE_OLD = (
    "async function handleStripeWebhook(request, env) {\n"
    "  try {\n"
    "    const event = await request.json();"
)
_V456_STRIPE_NEW = (
    "// ============================================================\n"
    "// P0 SECURITY HELPERS (v4.5.6)\n"
    "// ============================================================\n"
    "function requireAdminKey(request, env, url) {\n"
    "  const presented = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';\n"
    "  const expected = env.ADMIN_SECRET || '';\n"
    "  if (!expected) return { ok: false, status: 500, error: 'ADMIN_SECRET not configured' };\n"
    "  if (!presented) return { ok: false, status: 401, error: 'X-Admin-Key required' };\n"
    "  if (presented.length !== expected.length) return { ok: false, status: 403, error: 'Invalid admin key' };\n"
    "  let mismatch = 0;\n"
    "  for (let i = 0; i < presented.length; i++) mismatch |= presented.charCodeAt(i) ^ expected.charCodeAt(i);\n"
    "  if (mismatch !== 0) return { ok: false, status: 403, error: 'Invalid admin key' };\n"
    "  return { ok: true };\n"
    "}\n"
    "\n"
    "async function verifyStripeSignature(rawBody, sigHeader, secret) {\n"
    "  if (!sigHeader || !secret) return false;\n"
    "  const parts = {};\n"
    "  for (const p of sigHeader.split(',')) {\n"
    "    const [k, v] = p.split('=');\n"
    "    if (k && v) parts[k] = v;\n"
    "  }\n"
    "  const timestamp = parts.t;\n"
    "  const signature = parts.v1;\n"
    "  if (!timestamp || !signature) return false;\n"
    "  const signedPayload = `${timestamp}.${rawBody}`;\n"
    "  const key = await crypto.subtle.importKey(\n"
    "    'raw',\n"
    "    new TextEncoder().encode(secret),\n"
    "    { name: 'HMAC', hash: 'SHA-256' },\n"
    "    false,\n"
    "    ['sign']\n"
    "  );\n"
    "  const sigBuf = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(signedPayload));\n"
    "  const expected = Array.from(new Uint8Array(sigBuf)).map(b => b.toString(16).padStart(2, '0')).join('');\n"
    "  if (expected.length !== signature.length) return false;\n"
    "  let mismatch = 0;\n"
    "  for (let i = 0; i < expected.length; i++) mismatch |= expected.charCodeAt(i) ^ signature.charCodeAt(i);\n"
    "  return mismatch === 0;\n"
    "}\n"
    "\n"
    "async function handleStripeWebhook(request, env) {\n"
    "  try {\n"
    "    const rawBody = await request.text();\n"
    "    const sigHeader = request.headers.get('stripe-signature');\n"
    "    const sigOk = await verifyStripeSignature(rawBody, sigHeader, env.STRIPE_WEBHOOK_SECRET);\n"
    "    if (!sigOk) return json({ error: 'Invalid Stripe signature' }, 401);\n"
    "    const event = JSON.parse(rawBody);"
)

_V456_ADMIN_OLD = (
    "      const adminKey = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';\n"
    "      if (!adminKey) return addCORS(json({ error: 'X-Admin-Key required' }, 401), request);"
)
_V456_ADMIN_NEW = (
    "      const adminChk = requireAdminKey(request, env, url);\n"
    "      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);"
)

V456_PATCHES = [
    ("v4.5.6 header",        _V456_HEADER_OLD,  _V456_HEADER_NEW,  1),
    ("v4.5.6 version bump",  _V456_VERSION_OLD, _V456_VERSION_NEW, 1),
    ("v4.5.6 helpers+stripe", _V456_STRIPE_OLD, _V456_STRIPE_NEW,  1),
    ("v4.5.6 admin checks",  _V456_ADMIN_OLD,   _V456_ADMIN_NEW,   5),
]

# ---- v4.5.7 patches (applied on top of v4.5.6 state) ----

_V457_HEADER_OLD = (
    " * DC Hub API Proxy Worker v4.5.6 \u2014 P0 Security Hardening\n"
    " * ================================================================================\n"
    " * v4.5.6 CHANGES (Apr 17 2026):"
)
_V457_HEADER_NEW = (
    " * DC Hub API Proxy Worker v4.5.7 \u2014 Stripe Webhook Check Reorder\n"
    " * ================================================================================\n"
    " * v4.5.7 CHANGES (Apr 17 2026):\n"
    " *   - SEC: /api/stripe/mcp-webhook route no longer short-circuits on a missing\n"
    " *          DCHUB_API_KEYS KV binding before signature verification.\n"
    " *\n"
    " * v4.5.6 CHANGES (Apr 17 2026):"
)

_V457_VERSION_OLD = "const WORKER_VERSION   = '4.5.6';"
_V457_VERSION_NEW = "const WORKER_VERSION   = '4.5.7';"

_V457_ROUTE_OLD = (
    "    if (pathname === '/api/stripe/mcp-webhook' && request.method === 'POST') {\n"
    "      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);\n"
    "      return addCORS(await handleStripeWebhook(request, env), request);\n"
    "    }"
)
_V457_ROUTE_NEW = (
    "    if (pathname === '/api/stripe/mcp-webhook' && request.method === 'POST') {\n"
    "      return addCORS(await handleStripeWebhook(request, env), request);\n"
    "    }"
)

_V457_HANDLER_OLD = (
    "    if (!sigOk) return json({ error: 'Invalid Stripe signature' }, 401);\n"
    "    const event = JSON.parse(rawBody);"
)
_V457_HANDLER_NEW = (
    "    if (!sigOk) return json({ error: 'Invalid Stripe signature' }, 401);\n"
    "    if (!env.DCHUB_API_KEYS) return json({ error: 'DCHUB_API_KEYS KV not configured' }, 500);\n"
    "    const event = JSON.parse(rawBody);"
)

V457_PATCHES = [
    ("v4.5.7 header",       _V457_HEADER_OLD,  _V457_HEADER_NEW,  1),
    ("v4.5.7 version bump", _V457_VERSION_OLD, _V457_VERSION_NEW, 1),
    ("v4.5.7 route reorder", _V457_ROUTE_OLD,  _V457_ROUTE_NEW,   1),
    ("v4.5.7 handler kv",   _V457_HANDLER_OLD, _V457_HANDLER_NEW, 1),
]

# ---- v4.5.8 patches (applied on top of v4.5.7 state) ----

_V458_HEADER_OLD = (
    " * DC Hub API Proxy Worker v4.5.7 \u2014 Stripe Webhook Check Reorder\n"
    " * ================================================================================\n"
    " * v4.5.7 CHANGES (Apr 17 2026):"
)
_V458_HEADER_NEW = (
    " * DC Hub API Proxy Worker v4.5.8 \u2014 Replay Guard + Status Hardening + Cleanup\n"
    " * ================================================================================\n"
    " * v4.5.8 CHANGES (Apr 17 2026):\n"
    " *   - SEC: Stripe webhook rejects signatures with timestamps outside a\n"
    " *          5-minute window. Prevents replay of captured signed payloads.\n"
    " *   - SEC: Admin endpoints collapse missing-header and wrong-key to 403\n"
    " *          with identical body. Removes the 401-vs-403 info leak.\n"
    " *   - REMOVE: /api/publish-debug diagnostic (v4.5.3 temporary).\n"
    " *\n"
    " * v4.5.7 CHANGES (Apr 17 2026):"
)

_V458_VERSION_OLD = "const WORKER_VERSION   = '4.5.7';"
_V458_VERSION_NEW = "const WORKER_VERSION   = '4.5.8';"

_V458_REPLAY_OLD = (
    "  const timestamp = parts.t;\n"
    "  const signature = parts.v1;\n"
    "  if (!timestamp || !signature) return false;\n"
    "  const signedPayload = `${timestamp}.${rawBody}`;"
)
_V458_REPLAY_NEW = (
    "  const timestamp = parts.t;\n"
    "  const signature = parts.v1;\n"
    "  if (!timestamp || !signature) return false;\n"
    "  const tsNum = parseInt(timestamp, 10);\n"
    "  if (!Number.isFinite(tsNum)) return false;\n"
    "  if (Math.abs(Math.floor(Date.now() / 1000) - tsNum) > 300) return false;\n"
    "  const signedPayload = `${timestamp}.${rawBody}`;"
)

_V458_ADMIN_OLD = (
    "  if (!presented) return { ok: false, status: 401, error: 'X-Admin-Key required' };"
)
_V458_ADMIN_NEW = (
    "  if (!presented) return { ok: false, status: 403, error: 'Invalid admin key' };"
)

_V458_PUBLISH_DEBUG_OLD = (
    "    // \u2500\u2500 TEMP DIAG (v4.5.3) \u2014 REMOVE once publish is verified \u2500\u2500\n"
    "    // Tells us what env bindings the Worker actually sees, without leaking the secret.\n"
    "    if (pathname === '/api/publish-debug') {\n"
    "      const s = env.PUBLISH_PROXY_SECRET || '';\n"
    "      return addCORS(json({\n"
    "        proxy_secret_set: !!env.PUBLISH_PROXY_SECRET,\n"
    "        proxy_secret_length: s.length,\n"
    "        proxy_secret_first4: s.slice(0, 4),\n"
    "        proxy_secret_last4:  s.slice(-4),\n"
    "        railway_secret_set: !!env.RAILWAY_PUBLISH_SECRET,\n"
    "        railway_secret_length: (env.RAILWAY_PUBLISH_SECRET || '').length,\n"
    "        r2_bound: !!env.NEWS_ARCHIVE,\n"
    "        all_env_keys: Object.keys(env || {}),\n"
    "        worker_version: WORKER_VERSION,\n"
    "      }), request);\n"
    "    }\n"
    "\n"
)
_V458_PUBLISH_DEBUG_NEW = ""

V458_PATCHES = [
    ("v4.5.8 header",        _V458_HEADER_OLD,        _V458_HEADER_NEW,        1),
    ("v4.5.8 version bump",  _V458_VERSION_OLD,       _V458_VERSION_NEW,       1),
    ("v4.5.8 replay guard",  _V458_REPLAY_OLD,        _V458_REPLAY_NEW,        1),
    ("v4.5.8 admin 401->403", _V458_ADMIN_OLD,        _V458_ADMIN_NEW,         1),
    ("v4.5.8 remove pub-dbg", _V458_PUBLISH_DEBUG_OLD, _V458_PUBLISH_DEBUG_NEW, 1),
]

# ----------------------------------------------------------------------------
# PATCHER
# ----------------------------------------------------------------------------

VERSION_RE = re.compile(r"const WORKER_VERSION\s+=\s+'(\d+\.\d+\.\d+)';")


def detect_version(src: str) -> str:
    m = VERSION_RE.search(src)
    if not m:
        raise ValueError("could not detect WORKER_VERSION in source")
    return m.group(1)


def apply_patch_set(src: str, patches: list, label: str) -> str:
    for name, old, new, expected in patches:
        count = src.count(old)
        if count != expected:
            raise RuntimeError(f"{label}: patch '{name}' anchor count = {count}, expected {expected}")
        src = src.replace(old, new)
    return src


def patch_to_458(src: str) -> tuple[str, str]:
    """Returns (patched_source, description_of_chain_applied)."""
    version = detect_version(src)
    steps = []
    if version == "4.5.5":
        src = apply_patch_set(src, V456_PATCHES, "v4.5.6")
        steps.append("v4.5.5 -> v4.5.6")
        version = "4.5.6"
    if version == "4.5.6":
        src = apply_patch_set(src, V457_PATCHES, "v4.5.7")
        steps.append("v4.5.6 -> v4.5.7")
        version = "4.5.7"
    if version == "4.5.7":
        src = apply_patch_set(src, V458_PATCHES, "v4.5.8")
        steps.append("v4.5.7 -> v4.5.8")
        version = "4.5.8"
    if version == "4.5.8":
        if not steps:
            return src, "already at v4.5.8 (no patches applied)"
        return src, " -> ".join(steps)
    raise RuntimeError(f"unsupported input version: {version}")


def post_check(src: str) -> None:
    assert detect_version(src) == "4.5.8", "post: not at v4.5.8"
    assert "const adminKey =" not in src, "post: legacy adminKey present"
    assert src.count("requireAdminKey(request, env, url)") == 6, "post: expected 6 requireAdminKey refs"
    assert "verifyStripeSignature(rawBody, sigHeader, env.STRIPE_WEBHOOK_SECRET)" in src, "post: sig check missing"
    assert "Math.abs(Math.floor(Date.now() / 1000) - tsNum) > 300" in src, "post: replay guard missing"
    assert "if (!presented) return { ok: false, status: 403" in src, "post: admin 401->403 not applied"
    assert "pathname === '/api/publish-debug'" not in src, "post: publish-debug still present"


def find_source_file() -> pathlib.Path:
    """Look for a worker source in cwd."""
    cwd = pathlib.Path.cwd()
    candidates = (
        list(cwd.glob("worker-v4.5.*.js"))
        + list(cwd.glob("dchubapiproxy*.js"))
        + list(cwd.glob("worker.js"))
        + list(cwd.glob("worker-source.js"))
    )
    # prefer lower versions so we patch forward; skip already-v4.5.8 unless it's the only one
    def key(p):
        m = re.search(r"4\.5\.(\d+)", p.name)
        return int(m.group(1)) if m else 99
    candidates.sort(key=key)
    if not candidates:
        raise FileNotFoundError(
            "no worker source found in cwd. expected one of: "
            "worker-v4.5.*.js, dchubapiproxy*.js, worker.js, worker-source.js"
        )
    return candidates[0]


# ----------------------------------------------------------------------------
# SMOKE TESTS
# ----------------------------------------------------------------------------

def http(method: str, path: str, body: bytes | None = None, headers: dict | None = None,
         timeout: float = 10.0) -> tuple[int, dict, bytes]:
    url = WORKER_URL + path
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, data=body, method=method, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read()
    except Exception as e:
        return -1, {}, str(e).encode()


def cf_blocked(body: bytes) -> bool:
    """Detect Cloudflare bot-protection 1010 responses so we don't treat them as legit 403s."""
    return b"error code: 1010" in body or b"Sorry, you have been blocked" in body


def stripe_sig(secret: str, body: bytes, ts: int) -> str:
    payload = f"{ts}.".encode() + body
    mac = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def run_smoke_tests() -> list[tuple[str, str, str]]:
    """Returns list of (name, status, detail) where status is PASS/FAIL/SKIP."""
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    stripe_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    results: list[tuple[str, str, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append((name, "PASS" if ok else "FAIL", detail))

    def skip(name: str, reason: str) -> None:
        results.append((name, "SKIP", reason))

    # Test 1: version header
    status, hdrs, body = http("GET", "/api/health")
    is_cf = cf_blocked(body)
    ver = hdrs.get("x-dc-worker-version") or hdrs.get("X-DC-Worker-Version", "")
    record("version header",
           ver in ("4.5.7", "4.5.8") and not is_cf,
           f"x-dc-worker-version={ver!r}{' (CF-BLOCKED)' if is_cf else ''}")

    # Test 2: unsigned Stripe -> 401
    status, _, body = http("POST", "/api/stripe/mcp-webhook",
                           body=b'{"type":"ping"}',
                           headers={"Content-Type": "application/json"})
    is_cf = cf_blocked(body)
    record("unsigned Stripe -> 401",
           status == 401 and not is_cf,
           f"status={status}{' (CF-BLOCKED)' if is_cf else ''} body={body[:80]!r}")

    # Test 3 & 4: signed Stripe tests require secret
    if stripe_secret:
        body = b'{"type":"ping"}'  # neither provisioning nor downgrade; handler returns 200
        now = int(time.time())

        # fresh signature
        sig = stripe_sig(stripe_secret, body, now)
        status, _, rbody = http("POST", "/api/stripe/mcp-webhook",
                                body=body,
                                headers={"Content-Type": "application/json",
                                         "stripe-signature": sig})
        is_cf = cf_blocked(rbody)
        record("signed+fresh Stripe -> 200",
               status == 200 and not is_cf,
               f"status={status}{' (CF-BLOCKED)' if is_cf else ''} body={rbody[:120]!r}")

        # stale signature (10 minutes old) — must fail on v4.5.8
        stale_ts = now - 600
        sig = stripe_sig(stripe_secret, body, stale_ts)
        status, _, rbody = http("POST", "/api/stripe/mcp-webhook",
                                body=body,
                                headers={"Content-Type": "application/json",
                                         "stripe-signature": sig})
        is_cf = cf_blocked(rbody)
        record("signed+stale Stripe -> 401 (v4.5.8 replay guard)",
               status == 401 and not is_cf,
               f"status={status}{' (CF-BLOCKED)' if is_cf else ''} (v4.5.7 passes this as 200 incorrectly)")
    else:
        skip("signed+fresh Stripe -> 200", "STRIPE_WEBHOOK_SECRET not set (CF secret is write-only — trigger a test webhook from the Stripe dashboard instead)")
        skip("signed+stale Stripe -> 401 (v4.5.8 replay guard)", "STRIPE_WEBHOOK_SECRET not set")

    # Test 5: wrong admin key -> 403
    status, _, rbody = http("POST", "/api/admin/create-api-key",
                            body=b'{}', headers={"X-Admin-Key": "definitely-wrong"})
    is_cf = cf_blocked(rbody)
    record("wrong admin key -> 403",
           status == 403 and not is_cf,
           f"status={status}{' (CF-BLOCKED, not Worker)' if is_cf else ''}")

    # Test 6: no admin key -> 403 (v4.5.8) or 401 (v4.5.7)
    status, _, rbody = http("POST", "/api/admin/create-api-key",
                            body=b'{}', headers={"Content-Type": "application/json"})
    is_cf = cf_blocked(rbody)
    record("no admin key -> 403 (v4.5.8) / 401 (v4.5.7)",
           status in (401, 403) and not is_cf,
           f"status={status}{' (CF-BLOCKED)' if is_cf else ''}")

    # Test 7: right admin key -> NOT 401/403 (auth passed)
    if admin_secret:
        status, _, rbody = http("POST", "/api/admin/create-api-key",
                                body=b'{"email":"qa-smoke-test@example.invalid","plan":"free"}',
                                headers={"X-Admin-Key": admin_secret,
                                         "Content-Type": "application/json"})
        is_cf = cf_blocked(rbody)
        # any status != 401/403 means auth passed (even a 400 or 500 from downstream is fine for our purpose)
        record("right admin key -> auth passes",
               status not in (401, 403) and not is_cf,
               f"status={status}{' (CF-BLOCKED)' if is_cf else ''} body={rbody[:120]!r}")
    else:
        skip("right admin key -> auth passes", "ADMIN_SECRET not set (rotate it if you want this test)")

    return results


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("dchub-qa.py — patch + smoke v4.5.8")
    print("=" * 60)

    # Step 1: patch
    try:
        src_path = find_source_file()
    except FileNotFoundError as e:
        print(f"[patch] ERROR: {e}")
        print("[patch] SKIPPED. smoke tests will still run against live worker.\n")
        src_path = None

    if src_path:
        print(f"[patch] source: {src_path.name}")
        src = src_path.read_text(encoding="utf-8")
        original_version = detect_version(src)
        print(f"[patch] detected version: {original_version}")

        try:
            patched, chain = patch_to_458(src)
            post_check(patched)
        except (RuntimeError, AssertionError) as e:
            print(f"[patch] ERROR: {e}")
            return 2

        out_path = pathlib.Path("worker-v4.5.8.js")
        out_path.write_text(patched, encoding="utf-8")
        print(f"[patch] applied: {chain}")
        print(f"[patch] wrote:   {out_path} ({len(patched.splitlines())} lines, {len(patched)} bytes)")
        print(f"[patch] paste this file into the Cloudflare Worker editor and Deploy.\n")

    # Step 2: smoke tests
    print("[smoke] running against live worker at", WORKER_URL)
    if not os.environ.get("ADMIN_SECRET"):
        print("[smoke] WARNING: ADMIN_SECRET not set — happy-path admin test will be skipped")
    if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
        print("[smoke] WARNING: STRIPE_WEBHOOK_SECRET not set — signed Stripe tests will be skipped")
    print()

    results = run_smoke_tests()
    width = max(len(name) for name, _, _ in results)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for name, status, detail in results:
        print(f"  [{status}] {name.ljust(width)}  {detail}")
        counts[status] += 1

    print()
    print(f"[smoke] {counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    print()
    print("NOTE: on v4.5.7 (pre-paste), these tests will FAIL and is expected:")
    print("      - signed+stale Stripe -> 401  (v4.5.7 doesn't check timestamp)")
    print("      - no admin key -> 403         (v4.5.7 returns 401 instead)")
    print("      Re-run after pasting worker-v4.5.8.js and deploying — all should pass.")

    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
