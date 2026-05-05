#!/usr/bin/env bash
# Rotates CANARY_SECRET on GitHub and prints the value to paste into Cloudflare.
set -euo pipefail
: "${GH_REPO:=azmartone67/dchub-backend}"
NEW=$(openssl rand -hex 32)
echo -n "$NEW" | gh secret set CANARY_SECRET --repo "$GH_REPO"
echo "GitHub secret CANARY_SECRET updated for $GH_REPO"
echo
echo "Now paste this into Cloudflare dashboard:"
echo "  Workers & Pages → dchubapiproxy → Settings → Variables and Secrets"
echo "  Update CANARY_SECRET to:"
echo
echo "$NEW"
echo
echo "After saving on Cloudflare, verify with:"
echo "  curl -sS -D - -o /dev/null -H \"X-Dchub-Canary: $NEW\" \\"
echo "    https://dchubapiproxy.azmartone.workers.dev/api/health | grep x-backend-used"
echo "  (should print: x-backend-used: replit)"
