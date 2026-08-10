#!/usr/bin/env bash
# Allow well-behaved programmatic clients on the FREE, KEYLESS surface.
#
# Filed: 2026-08-10. See docs/cf-urllib-block-2026-08-10.md
#
# PROBLEM (measured 2026-08-10): any request whose User-Agent *starts with*
# "Python-urllib" gets HTTP 403 / "error code: 1010" at the Cloudflare edge on
# every public path — /api/v1/dcpi/*, /api/v1/stats, /llms.txt,
# /sitemap-dcpi.xml, /robots.txt. That is the exact three-line verification
# script an analyst or an agent writes against our deliberately-keyless API.
#
# This script installs (or updates) ONE custom rule that skips the UA-signature
# security products for GET/HEAD requests to the public read surface, from a
# short list of mainstream HTTP client UAs.
#
# SCOPING — three conditions must ALL hold for the skip to apply:
#     method   ∈ {GET, HEAD}          (never a write path)
#   AND path   ∈ the free keyless surface
#   AND UA     ∈ the programmatic client allowlist
# So this is NOT a global UA allowance and NOT a global path allowance; admin
# and write paths are untouched, and the allowance cannot be used to POST.
#
# PRODUCTS — deliberately only `bic` and `uaBlock`, the two that key off the
# browser/UA signature. `waf`, `rateLimit`, `hot` and `securityLevel` stay
# ACTIVE on the public API, so this does not become a bot hole. (The older
# self-probe rule, scripts/cf_waf_allowlist_self_probes.sh, skips all seven —
# do not copy that breadth here.)
#
# ★ THIS IS NOT THE ZONE WORKER. Deploying worker.js is a manual dashboard
#   paste because a script PUT drops its bindings. This is the rulesets API on
#   a WAF custom rule — a different object, and the established way this repo
#   manages WAF rules (see the 2026-06-06 self-probe script). No worker
#   bindings are touched.
#
# Idempotent — safe to re-run. Matched by description, so re-running updates
# the same rule instead of stacking duplicates.
#
# Required env:
#   CF_WAF_EDIT_TOKEN — CF API token with Zone > Zone WAF > Edit on dchub.cloud.
#                       The Railway CLOUDFLARE_API_TOKEN is read-only and cannot
#                       even READ the rulesets (verified 2026-08-10: HTTP 403
#                       code 10000 "Authentication error"). Generate one at
#                       https://dash.cloudflare.com/profile/api-tokens
#
# Optional env:
#   ZONE_ID  — defaults to dchub.cloud
#   DRY_RUN  — set to 1 to print the payload and exit without writing

set -euo pipefail

ZONE_ID="${ZONE_ID:-1cb22dda8d50546d6edf0c09a8be5128}"
TOKEN="${CF_WAF_EDIT_TOKEN:-}"
DESC="Allow programmatic clients on public keyless API (2026-08-10)"
API="https://api.cloudflare.com/client/v4"
PHASE="http_request_firewall_custom"

if [[ -z "$TOKEN" ]]; then
    cat >&2 <<'EOF'
ERROR: CF_WAF_EDIT_TOKEN is not set.
  1. https://dash.cloudflare.com/profile/api-tokens -> Create Token -> Custom
       Permission:     Zone > Zone WAF > Edit
       Zone Resources: Include > Specific zone > dchub.cloud
  2. export CF_WAF_EDIT_TOKEN="..."
  3. Re-run this script.
EOF
    exit 1
fi

# ── the rule ─────────────────────────────────────────────────────────
# Keep PATHS in sync with DEFAULT_PATHS in
# scripts/check_public_api_programmatic_access.py — that script is the
# regression check for this rule.
read -r -d '' EXPRESSION <<'EXPR' || true
(http.request.method in {"GET" "HEAD"}) and (starts_with(http.request.uri.path, "/api/v1/dcpi/") or starts_with(http.request.uri.path, "/sitemap") or starts_with(http.request.uri.path, "/.well-known/") or http.request.uri.path in {"/api/v1/stats" "/api/v1/health" "/api/v1/version" "/llms.txt" "/llms-full.txt" "/robots.txt" "/ai" "/agent"}) and (starts_with(http.user_agent, "Python-urllib") or starts_with(http.user_agent, "python-requests") or starts_with(http.user_agent, "python-httpx") or starts_with(http.user_agent, "httpx") or starts_with(http.user_agent, "Go-http-client") or starts_with(http.user_agent, "node-fetch") or starts_with(http.user_agent, "axios") or starts_with(http.user_agent, "okhttp") or starts_with(http.user_agent, "curl") or starts_with(http.user_agent, "Wget") or starts_with(http.user_agent, "Java/") or starts_with(http.user_agent, "Ruby") or starts_with(http.user_agent, "PostmanRuntime"))
EXPR

PAYLOAD=$(python3 - "$DESC" "$EXPRESSION" <<'PY'
import json, sys
desc, expr = sys.argv[1], sys.argv[2]
print(json.dumps({
    "action": "skip",
    "action_parameters": {
        # ONLY the UA/browser-signature products. waf / rateLimit / hot /
        # securityLevel stay on for this traffic.
        "products": ["bic", "uaBlock"],
    },
    "description": desc,
    "enabled": True,
    "expression": expr,
    "logging": {"enabled": True},
}))
PY
)

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "DRY_RUN=1 — payload that WOULD be written:"
    echo "$PAYLOAD" | python3 -m json.tool
    exit 0
fi

echo "==> Verifying token..."
if ! curl -sS -H "Authorization: Bearer $TOKEN" "$API/user/tokens/verify" | grep -q '"success":true'; then
    echo "ERROR: token verify failed." >&2
    exit 1
fi
echo "    OK"

echo "==> Reading current $PHASE entrypoint..."
ENTRY=$(curl -sS -H "Authorization: Bearer $TOKEN" \
    "$API/zones/$ZONE_ID/rulesets/phases/$PHASE/entrypoint")
if ! echo "$ENTRY" | grep -q '"success":true'; then
    echo "ERROR: could not read ruleset:" >&2
    echo "$ENTRY" | python3 -m json.tool >&2 || echo "$ENTRY" >&2
    exit 2
fi

STAMP=$(date +%Y%m%d-%H%M%S)
echo "$ENTRY" > "/tmp/cf-${PHASE}-before-${STAMP}.json"
echo "    snapshot: /tmp/cf-${PHASE}-before-${STAMP}.json"

# ★ Rule ORDER matters: a `block` rule earlier in the ruleset terminates before
#   a later `skip` is ever evaluated. Report what is there so the operator can
#   see whether something is blocking Python-urllib ahead of us.
echo "==> Existing rules in evaluation order:"
echo "$ENTRY" | python3 -c "
import json, sys
rules = json.load(sys.stdin)['result'].get('rules', []) or []
for i, r in enumerate(rules):
    print('    %2d. [%s] %s' % (i, r.get('action'), (r.get('description') or '')[:70]))
    expr = r.get('expression') or ''
    if 'urllib' in expr.lower() or 'user_agent' in expr:
        print('        expr: %s' % expr[:200])
"

RULE_ID=$(echo "$ENTRY" | python3 -c "
import json, sys
for r in json.load(sys.stdin)['result'].get('rules', []) or []:
    if r.get('description') == '''$DESC''':
        print(r['id']); break
")

if [[ -n "$RULE_ID" ]]; then
    echo "==> Updating existing rule $RULE_ID..."
    RESP=$(curl -sS -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        "$API/zones/$ZONE_ID/rulesets/phases/$PHASE/entrypoint/rules/$RULE_ID" -d "$PAYLOAD")
else
    # Create at position 0 so it is evaluated before any pre-existing block.
    echo "==> Creating rule at position 0..."
    RESP=$(curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        "$API/zones/$ZONE_ID/rulesets/phases/$PHASE/entrypoint/rules" \
        -d "$(echo "$PAYLOAD" | python3 -c "
import json,sys
d=json.load(sys.stdin); d['position']={'index':1}; print(json.dumps(d))
")")
fi

if ! echo "$RESP" | grep -q '"success":true'; then
    echo "ERROR: write failed:" >&2
    echo "$RESP" | python3 -m json.tool >&2 || echo "$RESP" >&2
    exit 3
fi
echo "    OK"

echo ""
echo "==> Wait ~30s for edge propagation, then verify FROM THE OUTSIDE:"
echo ""
echo "    python3 scripts/check_public_api_programmatic_access.py --base https://dchub.cloud"
echo ""
echo "    That guard sends urllib's real default UA, cache-busts every request,"
echo "    and exits non-zero on any 403. Do not read the CF API response as proof."
