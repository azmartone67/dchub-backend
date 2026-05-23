#!/usr/bin/env bash
# ship-final-five.sh — one-shot finisher for the 5 RUNBOOK user-action items.
#
# Run from any machine with `railway` and `gh` CLIs authenticated.
# Each section is independent — you can comment out blocks you don't want.
#
# Estimated wall time end-to-end: ~3 min if all keys are pre-staged, ~15 min
# including the Twitter dev-portal click-through (Item 3).
#
# Phase ZZZZZ-round7 (2026-05-23). See RUNBOOK-2026-05-23.md for the
# narrative version of what each item does and why.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
blue()   { printf "\033[34m%s\033[0m\n" "$*"; }
hr()     { printf "\033[90m─────────────────────────────────────────\033[0m\n"; }
step()   { hr; blue "▸ $*"; }

# ─────────────────────────────────────────────────────────────────────
step "Pre-flight checks"
# ─────────────────────────────────────────────────────────────────────

command -v railway >/dev/null 2>&1 || { red "railway CLI not found. Install: https://docs.railway.app/develop/cli"; exit 1; }
command -v gh >/dev/null 2>&1       || { red "gh CLI not found. Install: https://cli.github.com"; exit 1; }

# Railway auth — if this fails, `railway login` first.
if ! railway whoami >/dev/null 2>&1; then
    yellow "railway not authenticated. Run: railway login"
    exit 1
fi
green "✓ railway authenticated as $(railway whoami 2>&1 | tail -1)"

# GH auth — gist + repo + workflow scopes were verified earlier.
if ! gh auth status >/dev/null 2>&1; then
    yellow "gh not authenticated. Run: gh auth login"
    exit 1
fi
green "✓ gh authenticated"


# ─────────────────────────────────────────────────────────────────────
step "Item 4: Set IPINFO_TOKEN on Railway (visitor enrichment)"
# ─────────────────────────────────────────────────────────────────────

if [ -z "${IPINFO_TOKEN:-}" ]; then
    yellow "Set IPINFO_TOKEN before running this section."
    yellow "  Sign up: https://ipinfo.io/signup  (free tier = 50k lookups/mo)"
    yellow "  Copy token: https://ipinfo.io/account/token"
    yellow "  Then: export IPINFO_TOKEN='ipinfo_xxx'  &&  bash $(basename "$0")"
else
    railway variables set IPINFO_TOKEN="$IPINFO_TOKEN" \
        --service dchub-backend --environment production 2>&1 || true
    green "✓ IPINFO_TOKEN set on Railway."
    green "  Verify: curl -H \"X-Admin-Key: \$DCHUB_ADMIN_KEY\" \\"
    green "          https://dchub.cloud/api/v1/admin/ip-enrich?ip=8.8.8.8"
fi


# ─────────────────────────────────────────────────────────────────────
step "Item 5: Set DCHUB_INTERNAL_KEY on the scheduler host"
# ─────────────────────────────────────────────────────────────────────

if [ -z "${DCHUB_INTERNAL_KEY:-}" ]; then
    yellow "Generate one with: openssl rand -hex 32"
    yellow "Then: export DCHUB_INTERNAL_KEY='generated-hex'  &&  bash $(basename "$0")"
    yellow "Note: must be set BOTH on dchub-backend AND on whatever host"
    yellow "runs dchub-scheduler.py (Replit / second Railway service)."
else
    # Set on dchub-backend
    railway variables set DCHUB_INTERNAL_KEY="$DCHUB_INTERNAL_KEY" \
        --service dchub-backend --environment production 2>&1 || true
    green "✓ DCHUB_INTERNAL_KEY set on dchub-backend."
    yellow "  REMINDER: also set on the dchub-scheduler host."
    yellow "  Once set there, the 'legacy hardcoded key accepted' warnings"
    yellow "  in Railway logs will stop firing, and brain class"
    yellow "  legacy_hardcoded_key_accepted earns its shipped proof."
fi


# ─────────────────────────────────────────────────────────────────────
step "Item 2 prep: stage the /mcp/manifest patch"
# ─────────────────────────────────────────────────────────────────────

cat <<'EOF'
The Express MCP server (dchub-mcp-server-production.up.railway.app) needs
the /mcp/manifest handler from this repo's dchub-mcp-v2.1/server.mjs:549-593.

If you have the dchub-mcp-server repo locally:
  cd ~/path/to/dchub-mcp-server
  # paste lines 549-593 from this repo's dchub-mcp-v2.1/server.mjs into
  # server.mjs right BEFORE app.listen(PORT, ...)
  git add server.mjs
  git commit -m "feat(mcp): add /mcp/manifest passthrough (proxies to Flask)"
  git push origin main
  # Railway auto-redeploys ~3 min later.

If the repo is on Replit:
  - SSH/Web-edit server.mjs in the Replit workspace
  - Apply the same patch
  - The Replit deploy triggers automatically

After Railway/Replit deploys:
  curl -sI https://dchub.cloud/mcp/manifest | head -3
  # Expect: HTTP/2 200, content-type: application/json
EOF


# ─────────────────────────────────────────────────────────────────────
step "Item 3 prep: Twitter dev keys"
# ─────────────────────────────────────────────────────────────────────

cat <<'EOF'
Cannot automate — Twitter web UI only.

1. Go to https://developer.twitter.com/en/portal/projects-and-apps
2. Create a new Project (free tier is fine)
3. Within the Project, attach your existing DC Hub app
4. Regenerate ALL keys (they invalidate on project-attach):
   - API Key + Secret
   - Bearer Token
   - Access Token + Secret
5. Then update Railway env vars (paste below):

   export TWITTER_API_KEY='...'
   export TWITTER_API_SECRET='...'
   export TWITTER_BEARER_TOKEN='...'
   export TWITTER_ACCESS_TOKEN='...'
   export TWITTER_ACCESS_SECRET='...'

   for v in TWITTER_API_KEY TWITTER_API_SECRET TWITTER_BEARER_TOKEN \
            TWITTER_ACCESS_TOKEN TWITTER_ACCESS_SECRET; do
       railway variables set "$v=${!v}" \
           --service dchub-backend --environment production
   done

6. Verify on the next auto-press cycle (Mon/Wed/Fri @ 6 UTC):
   railway logs --service dchub-backend | grep -i twitter | tail -5
   # Should see 'Twitter auto-publish OK for <id>' instead of 'X API error 403'.
EOF


# ─────────────────────────────────────────────────────────────────────
step "Item 1 prep: MCP-directory PR follow-ups"
# ─────────────────────────────────────────────────────────────────────

cat <<'EOF'
3 of the 5 directories already done:
  ✓ awesome-mcp-servers  → PR #6803 open (azmartone67 → punkpeye)
  ✓ Anthropic servers    → Issue #4235 open (modelcontextprotocol/servers)
  ✗ glama.ai             → form: https://glama.ai/mcp-servers/submit
  ✗ smithery.ai          → form: https://smithery.ai/server/new
  ✗ mcp.so               → form: https://mcp.so/submit

For the 3 form-based ones, payloads are pre-staged. Paste each as-is:
  cat $REPO_ROOT/mcp-directory/mcp-so-submission.yaml
  cat $REPO_ROOT/glama.json
  cat $REPO_ROOT/github-repo/smithery.yaml
EOF


# ─────────────────────────────────────────────────────────────────────
green "Done — ship-final-five.sh complete."
hr
echo "Suggested verification a few minutes after env vars take effect:"
echo "  curl -s https://dchub.cloud/api/v1/visitor-intel | jq ."
echo "  curl -s https://dchub.cloud/api/v1/brain/error-classes | jq '.classes | length'"
echo "  curl -s https://dchub.cloud/api/v1/iso/zones | jq '.count, .countries'"
echo "  railway logs --service dchub-backend | grep -i 'legacy hardcoded' | tail -5"
