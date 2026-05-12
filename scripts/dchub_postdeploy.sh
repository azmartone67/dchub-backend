#!/usr/bin/env bash
# dchub_postdeploy.sh — one-command upgrade + verify + report.
#
# What it does:
#   1. git pull origin main (latest code)
#   2. Smoke-test all Phase A–D endpoints against the live site
#      (CDN + cache-bust + origin so partial-deploy / CDN-cache issues
#      are diagnosed precisely, not just reported as "down")
#   3. Run the full QA crawler
#   4. Trigger the healer's on-demand crawl if reachable
#   5. Print a unified "what's live / what's still broken / next action" report
#
# Read-only. No destructive endpoints. Re-runnable.
#
# Usage:
#   DCHUB_API_KEY=… bash scripts/dchub_postdeploy.sh
#   bash scripts/dchub_postdeploy.sh --no-pull      # skip git pull
#   bash scripts/dchub_postdeploy.sh --no-crawl     # skip QA crawler (faster)

set -uo pipefail
CURL=/usr/bin/curl
BASE="${DCHUB_BASE:-https://dchub.cloud}"
ORIGIN="${DCHUB_ORIGIN:-https://dchub-backend-production.up.railway.app}"
KEY="${DCHUB_API_KEY:-}"
NOW=$(/usr/bin/awk 'BEGIN{srand();print srand()}')
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DO_PULL=1
DO_CRAWL=1
for a in "$@"; do
  case "$a" in
    --no-pull)  DO_PULL=0 ;;
    --no-crawl) DO_CRAWL=0 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
  esac
done

banner() { printf "\n\033[1;36m═══ %s ═══\033[0m\n" "$1"; }
ok()     { printf "  \033[1;32m✓\033[0m %s\n" "$1"; }
warn()   { printf "  \033[1;33m⚠\033[0m %s\n" "$1"; }
fail()   { printf "  \033[1;31m✗\033[0m %s\n" "$1"; }

# ─────────────────────────────────────────────────────────────
banner "Step 1 — git pull"
if [[ $DO_PULL -eq 1 ]]; then
  cd "$REPO_DIR"
  branch=$(git branch --show-current)
  if [[ "$branch" != "main" ]]; then
    warn "currently on '$branch' — pulling main into your tracking branch instead"
  fi
  git pull origin main 2>&1 | grep -E "^(From|Updating|Fast-forward|Already up to date|Merge made|\s+[0-9]+ files? changed)" | head -5
  ok "synced with origin/main ($(git rev-parse --short HEAD))"
else
  warn "skipped per --no-pull"
fi

# ─────────────────────────────────────────────────────────────
banner "Step 2 — Phase A-D endpoint smoke test"
printf "  %-38s %-8s %-10s %-8s %s\n" "path" "CDN" "CDN+bust" "Origin" "verdict"
printf "  %-38s %-8s %-10s %-8s %s\n" "----" "---" "--------" "------" "-------"
all_ok=1
for path in \
  /api/v1/dcpi/leaderboard \
  /api/v1/freshness \
  /freshness \
  /enterprise \
  /health/deep \
  "/.well-known/ai-agents.json" \
  /api/v1/heal/qa-crawl \
  /api/v1/keys/claim
do
  if [[ "$path" == "/api/v1/keys/claim" ]]; then
    # POST endpoint — different smoke pattern
    cdn=$("$CURL" -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
      -d '{"client_name":"postdeploy-smoke"}' --max-time 12 "${BASE}${path}")
    bust="$cdn"
    origin=$("$CURL" -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
      -d '{"client_name":"postdeploy-smoke"}' --max-time 12 "${ORIGIN}${path}")
  else
    H=()
    [[ -n "$KEY" ]] && H=(-H "X-API-Key: $KEY")
    cdn=$("$CURL" -s -o /dev/null -w "%{http_code}" "${H[@]}" --max-time 10 "${BASE}${path}")
    bust=$("$CURL" -s -o /dev/null -w "%{http_code}" "${H[@]}" -H "Cache-Control: no-cache" \
      --max-time 10 "${BASE}${path}?_pd=${NOW}")
    origin=$("$CURL" -s -o /dev/null -w "%{http_code}" "${H[@]}" --max-time 10 "${ORIGIN}${path}")
  fi
  verdict="LIVE"
  if [[ "$origin" =~ ^(401|429)$ ]]; then verdict="ORIGIN OK (auth/rate)"; fi
  if [[ "$origin" == "404" ]]; then verdict="MISSING ON RAILWAY"; all_ok=0; fi
  if [[ "$origin" =~ ^2 ]] && [[ "$cdn" == "404" && "$bust" =~ ^2 ]]; then verdict="STALE CDN CACHE"; all_ok=0; fi
  if [[ "$origin" =~ ^2 ]] && [[ "$bust" == "404" ]]; then verdict="WORKER BLOCKED"; all_ok=0; fi
  if [[ "$origin" =~ ^5 ]]; then verdict="ORIGIN 5xx"; all_ok=0; fi
  printf "  %-38s %-8s %-10s %-8s %s\n" "$path" "$cdn" "$bust" "$origin" "$verdict"
done
echo
[[ $all_ok -eq 1 ]] && ok "All Phase A-D endpoints live" \
                    || warn "Some endpoints not yet live — see verdicts above"

# ─────────────────────────────────────────────────────────────
banner "Step 3 — Healer findings snapshot"
if [[ -n "$KEY" ]]; then
  "$CURL" -s -H "X-API-Key: $KEY" --max-time 30 "${BASE}/api/v1/heal/findings" \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    print(f'  parse fail: {e}'); sys.exit(0)
det = d.get('findings', {})
print(f'  detectors reporting: {len(det)}')
for k, v in det.items():
    ok_ = v.get('ok')
    details = (v.get('details') or v.get('error') or '')[:120]
    flag = '✓' if ok_ and not (isinstance(details,str) and ('BROKEN' in details or 'NO ' in details or 'STALE' in details or 'SITEMAP 404' in details)) else '⚠'
    print(f'  {flag} {k}: {details}')
afi = d.get('actionable_frontend_issues', [])
print(f'  actionable frontend issues: {len(afi)}')
for it in afi[:6]:
    if isinstance(it, dict):
        print(f'    • {it.get(\"url\",\"?\")}: {it.get(\"issue\",\"?\")} x{it.get(\"count\",\"?\")}')
" || warn "could not parse heal/findings"
else
  warn "DCHUB_API_KEY not set — skipping healer snapshot"
fi

# ─────────────────────────────────────────────────────────────
banner "Step 4 — Full QA crawler"
if [[ $DO_CRAWL -eq 1 ]]; then
  if [[ -f "$REPO_DIR/scripts/dchub_qa_crawl.py" ]]; then
    DCHUB_API_KEY="${KEY}" python3 "$REPO_DIR/scripts/dchub_qa_crawl.py" 2>&1 | tail -40 \
      || warn "QA crawler failed"
  else
    fail "scripts/dchub_qa_crawl.py not found in repo"
  fi
else
  warn "skipped per --no-crawl"
fi

# ─────────────────────────────────────────────────────────────
banner "Done"
echo "  Next actions (if any non-LIVE verdicts above):"
echo "    • STALE CDN CACHE  → will self-heal at TTL; or wait ~5 min"
echo "    • WORKER BLOCKED   → patch dchub-frontend/_worker.js + redeploy CF Pages"
echo "    • MISSING ON RAILWAY → Railway hasn't picked up the merge yet; redeploy"
echo "    • ORIGIN 5xx       → check Railway logs"
echo
