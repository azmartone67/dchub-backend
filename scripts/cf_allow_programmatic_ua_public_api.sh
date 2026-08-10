#!/usr/bin/env bash
# Turn Browser Integrity Check OFF for the FREE, KEYLESS read surface.
#
# Filed: 2026-08-10. See docs/cf-urllib-block-2026-08-10.md
#
# PROBLEM (measured 2026-08-10): any request whose User-Agent starts with
# "Python-urllib" gets HTTP 403 / "error code: 1010" on every public path of
# dchub.cloud — /api/v1/dcpi/*, /api/v1/stats, /llms.txt, /sitemap-dcpi.xml,
# /robots.txt. That is the exact three-line verification script an analyst or an
# agent writes against our deliberately-keyless API.
#
# Cloudflare's own firewall event log names the cause: `source=bic rule=bic`.
# It is Browser Integrity Check, zone-wide.
#
# ★★★ WHY THIS IS A CONFIGURATION RULE AND NOT A WAF CUSTOM RULE
# An earlier version of this script wrote a WAF custom rule with
# `action: skip, products: ["bic"]`. **That does not work, and it was measured
# not working on this zone before being reverted.** BIC is evaluated BEFORE the
# http_request_firewall_custom phase, so the request is already blocked by the
# time a custom rule could skip it. Escalating to all seven products with
# byte-identical action_parameters to the working "/mcp" rule still returned 403.
# Worse, such a rule also skips `waf` and `rateLimit` — a real security loss on
# the public surface in exchange for nothing.
#
# The only mechanism that disables BIC per-path is a **Configuration Rule**
# (ruleset phase `http_config_settings`), which runs early, before security.
#
# ★ SCOPED BY PATH ONLY — deliberately no User-Agent clause. BIC's entire job is
#   to judge the UA; a rule that disabled BIC only for UAs we name would admit
#   anyone who claims that UA, which is strictly worse than disabling it for the
#   public read paths outright. Method is pinned to GET/HEAD and the paths are
#   already public and keyless, so nothing gated is exposed. A Configuration Rule
#   changes only the one named setting — WAF managed rules, rate limiting and
#   security level stay fully active on these paths.
#
# ★ NOT THE ZONE WORKER. Deploying worker.js is a manual dashboard paste because
#   a script PUT drops its bindings. This is the rulesets API. No worker bindings
#   are touched.
#
# ★ THE WRITE PATH IS UNTESTED. None of the four Cloudflare tokens in Railway can
#   reach http_config_settings, so this script's PUT could not be exercised here.
#   The DRY_RUN/read path was. Run DRY_RUN=1 first.
#
# Idempotent — matched by description, so re-running updates the same rule.
#
# Required env:
#   CF_CONFIG_RULES_TOKEN — CF API token with Zone > Config Rules > Edit on
#                           dchub.cloud. No existing token has this; create one at
#                           https://dash.cloudflare.com/profile/api-tokens
#                           (CF_WAF_EDIT_TOKEN is Zone WAF only — verified 2026-08-10:
#                           it reads/PUTs the firewall ruleset, 403s on config rules,
#                           and rejects POST .../rules with 10405.)
#
# Optional env:
#   ZONE_ID  — defaults to dchub.cloud
#   DRY_RUN  — set to 1 to print the payload and current state, then exit

set -euo pipefail

ZONE_ID="${ZONE_ID:-1cb22dda8d50546d6edf0c09a8be5128}"
TOKEN="${CF_CONFIG_RULES_TOKEN:-}"
DESC="Allow programmatic clients on public keyless API (2026-08-10)"
API="https://api.cloudflare.com/client/v4"
PHASE="http_config_settings"

if [[ -z "$TOKEN" ]]; then
    cat >&2 <<'EOF'
ERROR: CF_CONFIG_RULES_TOKEN is not set.
  1. https://dash.cloudflare.com/profile/api-tokens -> Create Token -> Custom
       Permission:     Zone > Config Rules > Edit
       Zone Resources: Include > Specific zone > dchub.cloud
  2. export CF_CONFIG_RULES_TOKEN="..."
  3. Re-run this script.

  Or just do it in the dashboard: Rules -> Configuration Rules -> Create rule,
  set Browser Integrity Check = Off, with the expression in
  docs/cf-urllib-block-2026-08-10.md. That is the entire fix.
EOF
    exit 1
fi

# Path list — keep in sync with DEFAULT_PATHS in
# scripts/check_public_api_programmatic_access.py, which is the regression check.
read -r -d '' EXPRESSION <<'EXPR' || true
(http.request.method in {"GET" "HEAD"}) and (starts_with(http.request.uri.path, "/api/v1/dcpi/") or starts_with(http.request.uri.path, "/sitemap") or starts_with(http.request.uri.path, "/.well-known/") or http.request.uri.path in {"/api/v1/stats" "/api/v1/health" "/api/v1/version" "/llms.txt" "/llms-full.txt" "/robots.txt" "/ai" "/agent"})
EXPR

RULE=$(python3 - "$DESC" "$EXPRESSION" <<'PY'
import json, sys
desc, expr = sys.argv[1], sys.argv[2]
print(json.dumps({
    "action": "set_config",
    "action_parameters": {"bic": False},   # Browser Integrity Check: Off
    "description": desc,
    "enabled": True,
    "expression": expr,
}))
PY
)

# ★ Parse the JSON; do NOT grep it. The rulesets endpoints return pretty-printed
#   JSON (`"success": true`, with a space) while /user/tokens/verify returns it
#   compact — so `grep '"success":true'` reports a good read as a failure. That
#   bug aborted a live run on 2026-08-10.
ok_json() {
    python3 -c "
import json, sys
try:
    sys.exit(0 if json.load(sys.stdin).get('success') else 1)
except Exception:
    sys.exit(1)
"
}

echo "==> Verifying token..."
if ! curl -sS -H "Authorization: Bearer $TOKEN" "$API/user/tokens/verify" | ok_json; then
    echo "ERROR: token verify failed." >&2
    exit 1
fi
echo "    OK"

echo "==> Reading current $PHASE entrypoint..."
ENTRY=$(curl -sS -H "Authorization: Bearer $TOKEN" \
    "$API/zones/$ZONE_ID/rulesets/phases/$PHASE/entrypoint" || true)
if ! echo "$ENTRY" | ok_json; then
    echo "NOTE: could not read the $PHASE entrypoint:" >&2
    echo "$ENTRY" | python3 -m json.tool >&2 || echo "$ENTRY" >&2
    echo "" >&2
    echo "If this is 'request is not authorized', the token lacks Config Rules." >&2
    echo "If the phase simply has no rules yet, the PUT below still creates it." >&2
    ENTRY=''
fi

if [[ -n "$ENTRY" ]]; then
    STAMP=$(date +%Y%m%d-%H%M%S)
    echo "$ENTRY" > "/tmp/cf-${PHASE}-before-${STAMP}.json"
    echo "    snapshot: /tmp/cf-${PHASE}-before-${STAMP}.json"
    echo "==> Existing configuration rules:"
    echo "$ENTRY" | python3 -c "
import json, sys
for i, r in enumerate(json.load(sys.stdin)['result'].get('rules', []) or []):
    print('    %2d. [%s] %s' % (i, r.get('action'), (r.get('description') or '')[:60]))
    print('        expr: %s' % (r.get('expression') or '')[:150])
    print('        set:  %s' % json.dumps(r.get('action_parameters') or {})[:120])
"
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo ""
    echo "DRY_RUN=1 — rule that WOULD be written:"
    echo "$RULE" | python3 -m json.tool
    exit 0
fi

# Full-ruleset PUT: preserves existing rules, replaces ours by description.
# (POST .../rules is rejected by some token scopes with 10405; PUT is accepted.)
export ENTRY_JSON="$ENTRY"
BODY=$(python3 - "$RULE" <<'PY'
import json, os, sys
rule = json.loads(sys.argv[1])
entry = os.environ.get('ENTRY_JSON', '')
KEEP = ('action', 'action_parameters', 'description', 'enabled', 'expression', 'logging')
rules = []
if entry:
    try:
        rules = [{k: v for k, v in r.items() if k in KEEP}
                 for r in json.loads(entry)['result'].get('rules', []) or []]
    except Exception:
        rules = []
rules = [r for r in rules if r.get('description') != rule['description']]
rules.append(rule)
print(json.dumps({"rules": rules}))
PY
)

echo "==> Writing $PHASE ruleset..."
RESP=$(curl -sS -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    "$API/zones/$ZONE_ID/rulesets/phases/$PHASE/entrypoint" -d "$BODY")

if ! echo "$RESP" | ok_json; then
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
echo "    Do not read the CF API response as proof. A 'success: true' write that"
echo "    does not change the live 403 is exactly what a WAF-skip rule produced"
echo "    on 2026-08-10 — see docs/cf-urllib-block-2026-08-10.md."
