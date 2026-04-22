#!/usr/bin/env bash
# Comprehensive QA across Cloudflare, Railway, Neon, GitHub (both repos), Replit.
# Read-only - no writes, no deploys. Takes ~60s.
set -u
REPORT=~/workspace/FULL_STACK_QA_$(date +%Y%m%d_%H%M).txt
: > "$REPORT"
_() { echo "$@" | tee -a "$REPORT"; }
_h() { echo "" | tee -a "$REPORT"; echo "===== $1 =====" | tee -a "$REPORT"; }

_ "Full-stack QA: $(date)"
_ "Run by: $(whoami)@$(hostname)"

# ─────────────────────────────────────────────────────────────
# 1) CLOUDFLARE EDGE
# ─────────────────────────────────────────────────────────────
_h "1. CLOUDFLARE EDGE (dchub.cloud)"
_ "--- main edge worker version ---"
curl -sI "https://dchub.cloud/?$(date +%s)" | grep -iE "HTTP|worker-version|server|cf-ray" | tee -a "$REPORT"

_ ""
_ "--- /press-release list ---"
curl -sI "https://dchub.cloud/press-release?$(date +%s)" | grep -iE "HTTP|worker-version" | tee -a "$REPORT"
CARDS=$(curl -s "https://dchub.cloud/press-release?$(date +%s)" | grep -c 'href="/press-release/')
_ "card links rendered: $CARDS"

_ ""
_ "--- /press-release/<slug> detail ---"
curl -sI "https://dchub.cloud/press-release/dc-hub-launches-air-permitting-intelligence?$(date +%s)" | grep -iE "HTTP|worker-version" | tee -a "$REPORT"

_ ""
_ "--- /news/ shared renderer ---"
for p in "news" "news/archive"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "https://dchub.cloud/$p?$(date +%s)")
  _ "/$p -> $code"
done

_ ""
_ "--- /.well-known endpoints ---"
for p in mcp.json agent.json ai-plugin.json oauth-protected-resource oauth-authorization-server; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "https://dchub.cloud/.well-known/$p?$(date +%s)")
  _ ".well-known/$p -> $code"
done

_ ""
_ "--- MCP endpoint shape ---"
curl -sI "https://dchub.cloud/mcp" | grep -iE "HTTP|content-type" | tee -a "$REPORT"
curl -s "https://dchub.cloud/.well-known/mcp.json" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f'  MCP: {d.get(\"name\")}, version={d.get(\"version\")}, tools={len(d.get(\"tools\", []))}')
except Exception as e:
    print(f'  MCP json parse failed: {e}')
" | tee -a "$REPORT"

_ ""
_ "--- API proxy (worker → Railway) ---"
for p in "api/facilities?limit=1" "api/press-releases/list" "api/deals?limit=1"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "https://dchub.cloud/$p")
  _ "/$p -> $code"
done

# ─────────────────────────────────────────────────────────────
# 2) RAILWAY BACKEND
# ─────────────────────────────────────────────────────────────
_h "2. RAILWAY BACKEND (direct, bypasses edge)"
BE="https://dchub-backend-production.up.railway.app"
_ "--- health ---"
curl -s "$BE/api/health" | python3 -m json.tool 2>/dev/null | tee -a "$REPORT"

_ ""
_ "--- /api/me (auth verify) ---"
code=$(curl -s -o /dev/null -w "%{http_code}" "$BE/api/me")
_ "status: $code (401 = expected without JWT, means endpoint alive)"

_ ""
_ "--- press releases from Neon ---"
curl -s "$BE/api/press-releases/list" | python3 -c "
import sys, json
d = json.load(sys.stdin)
rels = d.get('releases', [])
print(f'  success={d.get(\"success\")}, count={d.get(\"count\")}')
if rels:
    print(f'  most recent: {rels[0].get(\"date\")} | {rels[0].get(\"slug\")}')
    print(f'  oldest: {rels[-1].get(\"date\")} | {rels[-1].get(\"slug\")}')
" 2>&1 | tee -a "$REPORT"

_ ""
_ "--- announcements table (fresh news, daily cron) ---"
curl -s "$BE/api/press-releases/archive" | python3 -c "
import sys, json
d = json.load(sys.stdin)
dates = d.get('dates', [])
print(f'  archive dates: {len(dates)}')
if dates:
    for x in dates[:3]:
        print(f'    {x.get(\"date\")}: {x.get(\"count\", \"?\")} items')
" 2>&1 | tee -a "$REPORT"

_ ""
_ "--- key endpoints sanity ---"
for p in "api/facilities?limit=1" "api/deals?limit=1" "api/news?limit=1" "api/market-intel?market=ashburn"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$BE/$p")
  _ "$p -> $code"
done

# ─────────────────────────────────────────────────────────────
# 3) GITHUB — dchub-backend repo (Replit workspace = cloned backend)
# ─────────────────────────────────────────────────────────────
_h "3. GITHUB: dchub-backend (Replit workspace)"
cd ~/workspace
_ "--- current HEAD + upstream sync ---"
git fetch origin main 2>/dev/null
git log --oneline -1 | tee -a "$REPORT"
_ "origin/main HEAD:"; git log origin/main --oneline -1 | tee -a "$REPORT"
AHEAD=$(git rev-list --count origin/main..HEAD 2>/dev/null)
BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null)
_ "local is $AHEAD ahead, $BEHIND behind origin/main"

_ ""
_ "--- recent commits (last 10) ---"
git log origin/main --oneline -10 | tee -a "$REPORT"

_ ""
_ "--- uncommitted changes? ---"
git status -s | head -10 | tee -a "$REPORT"

_ ""
_ "--- today's session commits (by author time since 12h ago) ---"
git log origin/main --since="12 hours ago" --oneline | tee -a "$REPORT"

# ─────────────────────────────────────────────────────────────
# 4) GITHUB — dchub-frontend repo
# ─────────────────────────────────────────────────────────────
_h "4. GITHUB: dchub-frontend"
if [ -d ~/dchub-frontend ]; then
  cd ~/dchub-frontend
  git fetch origin main 2>/dev/null
  _ "--- HEAD ---"
  git log --oneline -1 | tee -a "$REPORT"
  _ "--- recent commits ---"
  git log origin/main --oneline -10 | tee -a "$REPORT"
  _ "--- today's commits ---"
  git log origin/main --since="12 hours ago" --oneline | tee -a "$REPORT"
  _ "--- current WORKER_VERSION in repo ---"
  grep -nE "WORKER_VERSION\s*=" _worker.js | head -2 | tee -a "$REPORT"
  _ "--- any version 4.6.x in repo history? ---"
  git log --all -p -S "'4.6.0'" -- _worker.js 2>/dev/null | head -5 | tee -a "$REPORT"
  _ "--- branches + tags ---"
  git branch -a | head -10 | tee -a "$REPORT"
  git tag 2>/dev/null | tail -20 | tee -a "$REPORT"
else
  _ "dchub-frontend NOT cloned locally"
fi

# ─────────────────────────────────────────────────────────────
# 5) REPLIT WORKSPACE
# ─────────────────────────────────────────────────────────────
_h "5. REPLIT WORKSPACE"
cd ~/workspace
_ "--- disk usage ---"
du -sh ~/workspace 2>/dev/null | tee -a "$REPORT"
_ "--- stashes remaining ---"
git stash list | tee -a "$REPORT"
_ "--- running python procs ---"
ps aux | grep -E "python|flask|main\.py" | grep -v grep | awk '{print $2, $11, $12, $13}' | head -5 | tee -a "$REPORT"
_ "--- key data files in repo ---"
ls -la data/*.json 2>/dev/null | head -5 | tee -a "$REPORT"

# ─────────────────────────────────────────────────────────────
# 6) NEON POSTGRES (via Railway API)
# ─────────────────────────────────────────────────────────────
_h "6. NEON POSTGRES freshness (via Railway)"
H=$(curl -s "$BE/api/health")
echo "$H" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(f'  source: {d.get(\"source\")}')
    print(f'  deal_count: {d.get(\"deal_count\")}')
    print(f'  facility_count: {d.get(\"facility_count\")}')
    print(f'  news_count: {d.get(\"news_count\")}')
    print(f'  backend_version: {d.get(\"version\")}')
except Exception as e:
    print(f'parse error: {e}')
" | tee -a "$REPORT"

# ─────────────────────────────────────────────────────────────
# 7) ZIP FILE vs LIVE WORKER comparison
# ─────────────────────────────────────────────────────────────
_h "7. UPLOADED ZIP (v4.6.2-powered-shell) vs LIVE"
_ "Zip inner worker version: 4.6.0 (Apr 17)"
_ "Live worker version (from dchub.cloud header above): see section 1"
_ "Repo HEAD worker version: $(grep -E 'WORKER_VERSION\s*=' ~/dchub-frontend/_worker.js 2>/dev/null | head -1)"
_ ""
_ "--- does zip's worker have the /press-release alias fix we added tonight? ---"
_ "NO - zip is from Apr 17, our patches landed Apr 22"
_ ""
_ "--- does zip have _routes.json? ---"
_ "NO - relies on default (worker handles everything)"

# ─────────────────────────────────────────────────────────────
# 8) SUMMARY — problems / drift detection
# ─────────────────────────────────────────────────────────────
_h "8. SUMMARY"
_ "Report written to: $REPORT"
_ ""
_ "Key things to check manually:"
_ "  - Railway boot logs: duplicate registration warnings gone?"
_ "  - CF dashboard: mcp-proxy showing latest zip uploaded this morning?"
_ "  - CF dashboard: INTERNAL_SYNC_SECRET present on the correct worker?"
_ ""
_ "Full output above was also saved to: $REPORT"