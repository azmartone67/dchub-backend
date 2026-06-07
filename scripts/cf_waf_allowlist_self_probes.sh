#!/usr/bin/env bash
# CF WAF allowlist for DC Hub self-probes (stop DDoSing ourselves)
#
# Filed: 2026-06-06. See docs/cf-waf-bypass-self-probes-2026-06-06.md
#
# This script PATCHes the existing self-probe allow rule in the
# http_request_firewall_custom ruleset to cover:
#   - IP 162.220.232.99 (Railway egress)
#   - User-Agents starting with "DCHub-" or "dchub-"
#   - User-Agents containing "DCHubHealer"
#   - User-Agents (case-insensitive) containing "dchub" (legacy net)
# ...and to skip ALL security products (waf, rateLimit, bic, hot, uaBlock,
# securityLevel, zoneLockdown) across all three relevant phases.
#
# Idempotent — safe to re-run.
#
# Required env:
#   CF_WAF_EDIT_TOKEN  — a CF API token with Zone WAF: Edit on dchub.cloud
#                        (the Railway CLOUDFLARE_API_TOKEN is zone-read only,
#                        so generate a new token at:
#                        https://dash.cloudflare.com/profile/api-tokens )
#
# Optional env:
#   ZONE_ID            — defaults to dchub.cloud zone
#   RULE_ID            — defaults to the existing rule from 2026-05-20

set -euo pipefail

ZONE_ID="${ZONE_ID:-1cb22dda8d50546d6edf0c09a8be5128}"
RULE_ID="${RULE_ID:-5f9a1d837e9c4608a1a52efc174d7677}"
TOKEN="${CF_WAF_EDIT_TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: CF_WAF_EDIT_TOKEN is not set." >&2
    echo "  1. Generate token at https://dash.cloudflare.com/profile/api-tokens" >&2
    echo "     - Permission: Zone > Zone WAF > Edit" >&2
    echo "     - Zone Resources: Include > Specific zone > dchub.cloud" >&2
    echo "  2. export CF_WAF_EDIT_TOKEN=\"...\"" >&2
    echo "  3. Re-run this script." >&2
    exit 1
fi

echo "==> Verifying token..."
verify=$(curl -sS -H "Authorization: Bearer $TOKEN" \
    "https://api.cloudflare.com/client/v4/user/tokens/verify")
if ! echo "$verify" | grep -q '"success":true'; then
    echo "ERROR: token verify failed:" >&2
    echo "$verify" >&2
    exit 1
fi
echo "    OK"

echo "==> Snapshotting current rule (for rollback reference)..."
BEFORE=$(curl -sS -H "Authorization: Bearer $TOKEN" \
    "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/rulesets/phases/http_request_firewall_custom/entrypoint" \
    | python3 -c "
import json,sys
d=json.load(sys.stdin)
for r in d['result']['rules']:
    if r['id']=='$RULE_ID':
        print(json.dumps(r, indent=2))
        break
")
if [[ -z "$BEFORE" ]]; then
    echo "ERROR: rule $RULE_ID not found in entrypoint ruleset" >&2
    exit 1
fi
echo "$BEFORE" > "/tmp/cf-rule-$RULE_ID-before-$(date +%Y%m%d-%H%M%S).json"
echo "    snapshot saved to /tmp/cf-rule-$RULE_ID-before-*.json"

echo "==> Patching rule $RULE_ID..."
PAYLOAD=$(cat <<'JSON'
{
  "action": "skip",
  "action_parameters": {
    "phases": [
      "http_ratelimit",
      "http_request_firewall_managed",
      "http_request_sbfm"
    ],
    "products": [
      "bic",
      "hot",
      "rateLimit",
      "securityLevel",
      "uaBlock",
      "waf",
      "zoneLockdown"
    ],
    "ruleset": "current"
  },
  "description": "DC Hub internal probes - self-DDoS bypass (2026-06-06)",
  "enabled": true,
  "expression": "(ip.src eq 162.220.232.99) or (starts_with(http.user_agent, \"DCHub-\")) or (starts_with(http.user_agent, \"dchub-\")) or (http.user_agent contains \"DCHubHealer\") or (lower(http.user_agent) contains \"dchub\")",
  "logging": { "enabled": true }
}
JSON
)

RESPONSE=$(curl -sS -X PATCH \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/rulesets/phases/http_request_firewall_custom/entrypoint/rules/$RULE_ID" \
    -d "$PAYLOAD")

if ! echo "$RESPONSE" | grep -q '"success":true'; then
    echo "ERROR: PATCH failed:" >&2
    echo "$RESPONSE" | python3 -m json.tool >&2 || echo "$RESPONSE" >&2
    exit 2
fi
echo "    OK"

echo "==> Verifying new rule state..."
curl -sS -H "Authorization: Bearer $TOKEN" \
    "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/rulesets/phases/http_request_firewall_custom/entrypoint" \
    | python3 -c "
import json,sys
d=json.load(sys.stdin)
for r in d['result']['rules']:
    if r['id']=='$RULE_ID':
        print('rule_id:', r['id'])
        print('desc:   ', r['description'])
        print('expr:   ', r['expression'])
        print('phases: ', r.get('action_parameters',{}).get('phases',[]))
        print('products:', r.get('action_parameters',{}).get('products',[]))
        break
"

echo ""
echo "==> Live verify (after ~30s edge propagation):"
echo "    curl -sI 'https://dchub.cloud/api/v1/version' -H 'User-Agent: DCHub-FailoverProbe/1.0' | head -3"
echo ""
echo "==> Done. Expected impact: ~218K requests/day unblocked"
echo "    (1.53M/7d previously blocked from 162.220.232.99)."
