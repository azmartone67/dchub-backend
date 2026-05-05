#!/usr/bin/env bash
# finish_v21.sh — pick up where we left off and ship MCP v2.1.
# Idempotent. Run from ~/workspace:
#     bash dchub-mcp-v2.1/finish_v21.sh

set -u
cd "${HOME}/workspace"
HERE="$(pwd)"
BUNDLE="${HERE}/dchub-mcp-v2.1"
TEST_API_KEY="${TEST_API_KEY:-dch_live_af5708dbec3ac4fbf15d8a81eee5f6ba}"

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yel()   { printf "\033[33m%s\033[0m\n" "$*"; }
blue()  { printf "\033[34m%s\033[0m\n" "$*"; }
hr()    { printf "\033[90m────────────────────────────────────────\033[0m\n"; }
step()  { hr; blue "▸ $*"; }

[ -z "${DCHUB_INTERNAL_KEY:-}" ] && { red "DCHUB_INTERNAL_KEY not set in this shell"; exit 1; }
[ -z "${NEON_DATABASE_URL:-}"  ] && { red "NEON_DATABASE_URL not set in this shell"; exit 1; }

# ── 1. Test local Flask blueprint ──────────────────────────────────────────
step "1/5  Testing Flask blueprint locally"

RESP=$(curl -sS -o /tmp/v21_resp.json -w "%{http_code}" -X POST \
  http://localhost:5000/api/v1/keys/validate \
  -H "X-Internal-Key: $DCHUB_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"api_key\":\"$TEST_API_KEY\"}" 2>/dev/null || echo "000")

BODY=$(cat /tmp/v21_resp.json 2>/dev/null || echo "")

if [ "$RESP" = "200" ] && echo "$BODY" | grep -q '"valid":true'; then
  green "  Blueprint LIVE. Response: $BODY"
elif [ "$RESP" = "404" ]; then
  red "  Blueprint NOT registered (HTTP 404)."
  yel "  Most likely: gunicorn is still running pre-patch code."
  yel "  Stop and Run the workflow in Replit, then rerun this script."
  yel "  If you've already restarted, check the startup log for:"
  yel "      [mcp v2.1] blueprint registration FAILED: <reason>"
  yel "  Run this to confirm the patched main.py is in place:"
  yel "      grep -n 'flask_mcp_endpoints' main.py"
  exit 2
else
  red "  Unexpected HTTP $RESP. Body:"
  printf "%s\n" "$BODY" | sed 's/^/    /'
  exit 2
fi

# ── 2. Apply Cloudflare Worker patch to worker.js ──────────────────────────
step "2/5  Patching worker.js (mcp.json: 7 wrong tools → real 20)"

if [ ! -f worker.js ]; then
  yel "  no ~/workspace/worker.js — skipping. Apply via Cloudflare dashboard or skip if your Worker repo is elsewhere."
else
  TOOLS_BEFORE=$(grep -c '"name":' worker.js || echo "?")
  python3 "${BUNDLE}/apply_worker_patch.py" --worker worker.js && green "  worker.js patched" || {
    yel "  patch reported no changes (already patched? or pattern not found). Continuing."
  }
  TOOLS_AFTER=$(python3 - <<'PYEOF' 2>/dev/null
import re, json
with open('worker.js') as f:
    src = f.read()
m = re.search(r"'/\.well-known/mcp\.json'\s*:\s*\{[^}]*body:\s*JSON\.stringify\(\s*(\{.*?\})\s*,\s*null,\s*2\)", src, re.DOTALL)
if m:
    try:
        d = json.loads(m.group(1))
        print(len(d.get('tools', [])))
    except Exception:
        print('?')
else:
    print('?')
PYEOF
)
  yel "  inline mcp.json now advertises ${TOOLS_AFTER} tools (was 7)"
fi

# ── 3. Stage server.mjs into the Railway-bound MCP repo ────────────────────
step "3/5  Staging server.mjs for Railway deploy"

# Common locations for the dchub-mcp-server repo
MCP_REPO=""
for cand in "${HERE}/dchub-mcp-server" "${HERE}/github-repo" "${HERE}/../dchub-mcp-server"; do
  if [ -d "$cand/.git" ]; then MCP_REPO="$cand"; break; fi
done

if [ -n "$MCP_REPO" ]; then
  cp "${BUNDLE}/server.mjs" "${MCP_REPO}/server.mjs"
  green "  copied server.mjs into ${MCP_REPO}"
  pushd "$MCP_REPO" >/dev/null
  git diff --stat server.mjs || true
  popd >/dev/null
else
  yel "  could not auto-locate the dchub-mcp-server repo (looked in dchub-mcp-server/, github-repo/, ..)"
  yel "  Manually:   cp ${BUNDLE}/server.mjs <repo>/server.mjs"
fi

# ── 4. Print the two git pushes you still need to do ───────────────────────
step "4/5  Git pushes you need to run (Cloudflare + Railway both pull from GitHub)"

cat <<EOF

  A. Cloudflare Worker (auto-deploys from GitHub):
       cd ~/workspace
       git add worker.js
       git commit -m "MCP v2.1: fix /.well-known/mcp.json — 20 real tools"
       git push origin main

  B. Railway MCP server (auto-deploys from GitHub):
EOF
if [ -n "$MCP_REPO" ]; then
  echo "       cd ${MCP_REPO}"
else
  echo "       cd <your dchub-mcp-server repo>"
fi
cat <<EOF
       git add server.mjs
       git commit -m "MCP v2.1: telemetry + key validation + tier gates"
       git push origin main

  Run those, wait ~60s for both to deploy, then:
       bash ${BUNDLE}/finish_v21.sh smoke

EOF

# ── 5. Optional: smoke test against production ─────────────────────────────
if [ "${1:-}" = "smoke" ] || [ "${1:-}" = "--smoke" ]; then
  step "5/5  End-to-end smoke test against https://dchub.cloud"

  # Verify discovery now lists 20 tools
  PROD_TOOLS=$(curl -fsS https://dchub.cloud/.well-known/mcp.json 2>/dev/null \
    | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('tools',[])))" 2>/dev/null || echo "?")
  if [ "$PROD_TOOLS" = "20" ]; then
    green "  /.well-known/mcp.json → 20 tools ✓"
  else
    red "  /.well-known/mcp.json → $PROD_TOOLS tools (expected 20). Worker may not have redeployed yet."
  fi

  # Verify health
  HEALTH=$(curl -fsS https://dchub.cloud/health 2>/dev/null || echo "{}")
  if echo "$HEALTH" | grep -q '"version":"2.1'; then
    green "  /health → server.mjs v2.1 ✓"
  else
    red "  /health did not return v2.1. Railway may not have redeployed yet."
    echo "     got: $HEALTH" | head -c 300; echo
  fi

  # Run the full smoke script
  yel "  Running test_smoke.sh end-to-end…"
  MCP_URL=https://dchub.cloud/mcp \
    API_KEY="$TEST_API_KEY" \
    DCHUB_INTERNAL_KEY="$DCHUB_INTERNAL_KEY" \
    bash "${BUNDLE}/test_smoke.sh"

  step "6/6  Tracking → backend"
  STATS=$(curl -fsS -H "X-Internal-Key: $DCHUB_INTERNAL_KEY" \
    https://dchub-backend-production.up.railway.app/api/v1/mcp/stats?days=1 2>/dev/null || echo "{}")
  TOOL_CALLS=$(echo "$STATS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('funnel',{}).get('tool_calls',0))" 2>/dev/null || echo "0")
  if [ "$TOOL_CALLS" -gt 0 ]; then
    green "  Backend stats show $TOOL_CALLS tool_calls in last 24h — verification gap CLOSED 🎯"
  else
    yel "  Backend stats show 0 tool_calls. Either smoke test didn't reach the patched server, or telemetry is still being recorded. Try again in 30s."
  fi
else
  yel "Run this same script with the 'smoke' flag once both pushes have deployed:"
  echo  "    bash ${BUNDLE}/finish_v21.sh smoke"
fi
