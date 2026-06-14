#!/usr/bin/env bash
# CF WAF allowlist for VERIFIED search + AI crawlers (unblock Bing/Yandex).
#
# Filed: 2026-06-14. Companion to cf_waf_allowlist_self_probes.sh.
#
# WHY: live edge tests on 2026-06-14 showed Cloudflare returning 403 to the
# Bingbot and YandexBot user-agents while letting Googlebot/Applebot/
# DuckDuckBot/PerplexityBot through. Bing Webmaster Tools independently
# reported "Limited crawl capacity / High severity". Root cause: the zone runs
# aggressive WAF Managed Rules + Rate Limiting + Super Bot Fight Mode, and the
# only skip rules are for our own dchub-* probes and /mcp — verified search
# crawlers were never allow-listed, so Bing/Yandex got swept up in the block.
#
# THE FIX: add ONE high-priority custom rule that SKIPs all security products
# for Cloudflare-VERIFIED bots (cf.client.bot == true → real Googlebot/Bingbot/
# YandexBot/etc. from their verified IP ranges; spoofers fail this check and are
# still subject to the WAF). Belt-and-suspenders: also match the bingbot/yandex
# UA substrings so the unblock holds even if a verified-bot signal is flaky.
#
# Idempotent: looks for an existing rule with our description and PATCHes it;
# otherwise creates it at the top of the custom ruleset.
#
# Required env:
#   CF_WAF_EDIT_TOKEN  — CF API token with Zone > Zone WAF > Edit on dchub.cloud
#                        (generate at https://dash.cloudflare.com/profile/api-tokens;
#                         the Railway/wrangler tokens are zone-read only).
# Optional env:
#   ZONE_ID            — defaults to dchub.cloud zone

set -euo pipefail

ZONE_ID="${ZONE_ID:-1cb22dda8d50546d6edf0c09a8be5128}"
TOKEN="${CF_WAF_EDIT_TOKEN:-}"
DESC="Allow verified search + AI crawlers (Bing/Yandex unblock 2026-06-14)"
EXPR='(cf.client.bot) or (lower(http.user_agent) contains "bingbot") or (lower(http.user_agent) contains "yandex")'
API="https://api.cloudflare.com/client/v4"
PHASE="http_request_firewall_custom"

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: CF_WAF_EDIT_TOKEN is not set." >&2
    echo "  1. Generate a token at https://dash.cloudflare.com/profile/api-tokens" >&2
    echo "     - Permission:     Zone > Zone WAF > Edit" >&2
    echo "     - Zone Resources: Include > Specific zone > dchub.cloud" >&2
    echo "  2. export CF_WAF_EDIT_TOKEN=\"...\"" >&2
    echo "  3. Re-run this script." >&2
    exit 1
fi

echo "==> Verifying token..."
curl -sS -H "Authorization: Bearer $TOKEN" "$API/user/tokens/verify" \
    | grep -q '"success":true' || { echo "ERROR: token verify failed" >&2; exit 1; }
echo "    OK"

# Skip-action body shared by create + update (mirrors the working MCP/self-probe rules).
ACTION_PARAMS='{
  "phases": ["http_ratelimit", "http_request_firewall_managed", "http_request_sbfm"],
  "products": ["bic", "hot", "rateLimit", "securityLevel", "uaBlock", "waf", "zoneLockdown"],
  "ruleset": "current"
}'

echo "==> Looking for an existing '$DESC' rule..."
EXISTING_ID=$(curl -sS -H "Authorization: Bearer $TOKEN" \
    "$API/zones/$ZONE_ID/rulesets/phases/$PHASE/entrypoint" \
    | python3 -c "
import json,sys
d=json.load(sys.stdin)
for r in (d.get('result') or {}).get('rules') or []:
    if r.get('description')=='''$DESC''':
        print(r['id']); break
")

if [[ -n "$EXISTING_ID" ]]; then
    echo "    found rule $EXISTING_ID — patching to current spec"
    curl -sS -X PATCH \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        "$API/zones/$ZONE_ID/rulesets/phases/$PHASE/entrypoint/rules/$EXISTING_ID" \
        -d "$(python3 -c "
import json
print(json.dumps({
  'action':'skip',
  'action_parameters': json.loads('''$ACTION_PARAMS'''),
  'description': '''$DESC''',
  'enabled': True,
  'expression': '''$EXPR''',
  'logging': {'enabled': True},
}))")" | python3 -c "import json,sys;d=json.load(sys.stdin);print('    success' if d.get('success') else '    FAILED: '+json.dumps(d.get('errors')))"
else
    echo "    none found — creating new rule"
    curl -sS -X POST \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        "$API/zones/$ZONE_ID/rulesets/phases/$PHASE/entrypoint/rules" \
        -d "$(python3 -c "
import json
print(json.dumps({
  'action':'skip',
  'action_parameters': json.loads('''$ACTION_PARAMS'''),
  'description': '''$DESC''',
  'enabled': True,
  'expression': '''$EXPR''',
  'logging': {'enabled': True},
  'position': {'index': 1},
}))")" | python3 -c "import json,sys;d=json.load(sys.stdin);print('    success' if d.get('success') else '    FAILED: '+json.dumps(d.get('errors')))"
fi

echo
echo "==> Verifying (wait ~30s for edge propagation, then):"
echo "    curl -sI -A 'Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)' https://dchub.cloud/ | head -1"
echo "    # expect: HTTP/2 200  (was 403)"
