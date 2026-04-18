#!/usr/bin/env python3
"""
patch-dchub-p0.py — one-shot P0 security patcher for dchubapiproxy Worker.

Takes a clean v4.5.5 Worker source and emits v4.5.7 with:
  - version bump (4.5.5 -> 4.5.7)
  - header changelog entries for v4.5.6 + v4.5.7
  - requireAdminKey() + verifyStripeSignature() helpers
  - Stripe webhook HMAC-SHA256 verification
  - constant-time admin key check on 5 admin routes
  - Stripe route no longer pre-guards on KV binding before sig verification;
    KV check moved inside handler after HMAC passes

Exits non-zero with a clear message if any anchor is missing or ambiguous —
the script refuses to produce a half-patched file.

Usage (Replit shell or anywhere with python3):
    python3 patch-dchub-p0.py worker-v4.5.5.js worker-v4.5.7.js
    python3 patch-dchub-p0.py worker.js                     # overwrite in place
    curl -s https://.../worker.js | python3 patch-dchub-p0.py - out.js
"""
from __future__ import annotations
import sys
import pathlib

# ----------------------------------------------------------------------------
# Patch definitions. Each entry is (label, old_literal, new_literal, expected_count).
# expected_count=None means "exactly 1". Use >=1 only for replace_all cases.
# ----------------------------------------------------------------------------

HEADER_OLD = (
    " * DC Hub API Proxy Worker v4.5.5 \u2014 FEMA Flood Zone Proxy\n"
    " * ================================================================================\n"
    " * v4.5.5 CHANGES (Apr 16 2026):"
)
HEADER_NEW = (
    " * DC Hub API Proxy Worker v4.5.7 \u2014 Stripe Webhook Check Reorder\n"
    " * ================================================================================\n"
    " * v4.5.7 CHANGES (Apr 17 2026):\n"
    " *   - SEC: /api/stripe/mcp-webhook route no longer short-circuits on a missing\n"
    " *          DCHUB_API_KEYS KV binding before verifying the Stripe signature.\n"
    " *          Signature verification now runs first; the KV check moved inside\n"
    " *          handleStripeWebhook, after the HMAC passes. Prevents unauthenticated\n"
    " *          callers from probing internal binding state via pre-guard 500s and\n"
    " *          ensures unsigned traffic is always rejected with 401.\n"
    " *\n"
    " * v4.5.6 CHANGES (Apr 17 2026):\n"
    " *   - SEC: Stripe webhook now verifies HMAC-SHA256 signature against\n"
    " *          env.STRIPE_WEBHOOK_SECRET before provisioning API keys. Previously\n"
    " *          any POST body would trigger provisioning \u2014 critical P0.\n"
    " *   - SEC: Admin endpoints now constant-time-compare X-Admin-Key against\n"
    " *          env.ADMIN_SECRET. Previously any non-empty header passed.\n"
    " *          Affected: /api/admin/create-api-key, /api/admin/usage,\n"
    " *                    /api/admin/revoke-api-key, /api/cache/purge,\n"
    " *                    /api/admin/seed-api-cache, /api/admin/seed-mcp-cache\n"
    " *   - Required secrets: STRIPE_WEBHOOK_SECRET, ADMIN_SECRET (both already set)\n"
    " *\n"
    " * v4.5.5 CHANGES (Apr 16 2026):"
)

VERSION_OLD = "const WORKER_VERSION   = '4.5.5';"
VERSION_NEW = "const WORKER_VERSION   = '4.5.7';"

STRIPE_OLD = (
    "async function handleStripeWebhook(request, env) {\n"
    "  try {\n"
    "    const event = await request.json();"
)
STRIPE_NEW = (
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
    "    if (!env.DCHUB_API_KEYS) return json({ error: 'DCHUB_API_KEYS KV not configured' }, 500);\n"
    "    const event = JSON.parse(rawBody);"
)

ADMIN_OLD = (
    "      const adminKey = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';\n"
    "      if (!adminKey) return addCORS(json({ error: 'X-Admin-Key required' }, 401), request);"
)
ADMIN_NEW = (
    "      const adminChk = requireAdminKey(request, env, url);\n"
    "      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);"
)

# v4.5.7: remove the KV pre-guard from the Stripe route so signature verification
# runs first regardless of binding state.
ROUTE_OLD = (
    "    if (pathname === '/api/stripe/mcp-webhook' && request.method === 'POST') {\n"
    "      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);\n"
    "      return addCORS(await handleStripeWebhook(request, env), request);\n"
    "    }"
)
ROUTE_NEW = (
    "    if (pathname === '/api/stripe/mcp-webhook' && request.method === 'POST') {\n"
    "      return addCORS(await handleStripeWebhook(request, env), request);\n"
    "    }"
)

PATCHES = [
    ("header",         HEADER_OLD,  HEADER_NEW,  1),
    ("version bump",   VERSION_OLD, VERSION_NEW, 1),
    ("stripe+helpers", STRIPE_OLD,  STRIPE_NEW,  1),
    ("stripe route",   ROUTE_OLD,   ROUTE_NEW,   1),
    ("admin checks",   ADMIN_OLD,   ADMIN_NEW,   5),
]


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.exit(code)


def apply_patches(src: str) -> str:
    if "WORKER_VERSION   = '4.5.7'" in src:
        die("input already at v4.5.7 — refusing to patch twice")
    if "WORKER_VERSION   = '4.5.6'" in src:
        die("input is at v4.5.6. This patcher goes 4.5.5 -> 4.5.7. "
            "Paste the pre-patched v4.5.5 source or apply the v4.5.7 delta manually.")
    for label, old, new, expected in PATCHES:
        count = src.count(old)
        if count != expected:
            die(f"patch '{label}' anchor count = {count}, expected {expected}")
        src = src.replace(old, new)
    # post-condition sanity
    if "const adminKey =" in src:
        die("post-check: legacy 'const adminKey' still present")
    if src.count("requireAdminKey(request, env, url)") != 6:
        die("post-check: expected 6 requireAdminKey refs (1 def + 5 calls)")
    if "verifyStripeSignature(rawBody, sigHeader, env.STRIPE_WEBHOOK_SECRET)" not in src:
        die("post-check: Stripe signature call not wired")
    # v4.5.7: KV check must be AFTER signature verification, not in the Stripe
    # route pre-guard. Note: other admin routes legitimately still guard on
    # env.DCHUB_API_KEYS — only the Stripe route pre-guard must be gone.
    stripe_preguard = (
        "    if (pathname === '/api/stripe/mcp-webhook' && request.method === 'POST') {\n"
        "      if (!env.DCHUB_API_KEYS)"
    )
    if stripe_preguard in src:
        die("post-check: stale KV pre-guard still in Stripe route dispatcher")
    if "if (!sigOk) return json({ error: 'Invalid Stripe signature' }, 401);\n    if (!env.DCHUB_API_KEYS)" not in src:
        die("post-check: KV check not placed after signature verification")
    return src


def read_input(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    p = pathlib.Path(arg)
    if not p.is_file():
        die(f"input file not found: {arg}")
    return p.read_text(encoding="utf-8")


def write_output(arg: str, data: str) -> None:
    if arg == "-":
        sys.stdout.write(data)
        return
    pathlib.Path(arg).write_text(data, encoding="utf-8")


def main(argv: list[str]) -> None:
    if len(argv) not in (2, 3):
        die("usage: patch-dchub-p0.py <input.js|-> [output.js|-]\n"
            "       if output omitted, overwrites input in place", code=2)
    inp = argv[1]
    out = argv[2] if len(argv) == 3 else inp
    src = read_input(inp)
    patched = apply_patches(src)
    write_output(out, patched)
    if out != "-":
        sys.stderr.write(
            f"OK: patched {inp!s} -> {out!s}  "
            f"({len(patched.splitlines())} lines, {len(patched)} chars)\n"
        )


if __name__ == "__main__":
    main(sys.argv)
