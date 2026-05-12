#!/usr/bin/env bash
# dchub_ingestion_audit.sh — Phase K — verify data ingestion pipelines.
#
# Reports for each major data domain:
#   ISO (PJM, ERCOT, CAISO, MISO, NYISO, SPP, ISO-NE)
#   Power (retail electricity rates, grid intelligence)
#   Fiber (submarine cables, dark fiber routes)
#   Gas (gas processing plants, compressor stations, pipelines)
#   M&A transactions
#   Pipeline (interconnection queue)
#   News (industry news feed)
#   DCPI (data center power index)
#
# For each domain:
#   • Latest record timestamp (how fresh is the data?)
#   • Total row count
#   • Reachable via public API? (status code)
#   • Backing endpoint at Railway origin
#
# Read-only. No destructive endpoints. Re-runnable.
#
# Usage:
#   DCHUB_API_KEY=… bash scripts/dchub_ingestion_audit.sh
#   bash scripts/dchub_ingestion_audit.sh --json > audit.json

set -uo pipefail
CURL=/usr/bin/curl
BASE="${DCHUB_BASE:-https://dchub.cloud}"
ORIGIN="${DCHUB_ORIGIN:-https://dchub-backend-production.up.railway.app}"
KEY="${DCHUB_API_KEY:-}"

JSON_MODE=0
[[ "${1:-}" == "--json" ]] && JSON_MODE=1

banner() { [[ $JSON_MODE -eq 0 ]] && printf "\n\033[1;36m═══ %s ═══\033[0m\n" "$1"; }
ok()     { [[ $JSON_MODE -eq 0 ]] && printf "  \033[1;32m✓\033[0m %s\n" "$1"; }
warn()   { [[ $JSON_MODE -eq 0 ]] && printf "  \033[1;33m⚠\033[0m %s\n" "$1"; }
fail()   { [[ $JSON_MODE -eq 0 ]] && printf "  \033[1;31m✗\033[0m %s\n" "$1"; }
row()    { [[ $JSON_MODE -eq 0 ]] && printf "  %-35s %-12s %s\n" "$1" "$2" "$3"; }

H=()
[[ -n "$KEY" ]] && H=(-H "X-API-Key: $KEY")

probe_endpoint() {
  local path="$1"
  local code
  code=$("$CURL" -s -o /dev/null -w "%{http_code}" "${H[@]}" --max-time 12 "${BASE}${path}" 2>/dev/null || echo "000")
  echo "$code"
}

probe_count_and_freshness() {
  # GET an endpoint, parse out a count + last-updated timestamp from common shapes
  local path="$1"
  local body
  body=$("$CURL" -s "${H[@]}" --max-time 15 "${BASE}${path}" 2>/dev/null)
  python3 -c "
import sys, json, re
try: d = json.loads('''$body'''.replace(chr(0), ''))
except Exception:
    print('?', '?'); sys.exit(0)
# Find a count
count = '?'
for k in ('count','total','total_count','total_records','n','rows'):
    if isinstance(d, dict) and k in d and isinstance(d[k], (int,float)):
        count = int(d[k]); break
if count == '?' and isinstance(d, dict):
    for v in d.values():
        if isinstance(v, list): count = len(v); break
# Find a last-updated
ts = '?'
for k in ('last_updated','as_of','updated_at','generated_at','computed_at','latest_period'):
    if isinstance(d, dict) and k in d and d[k]: ts = str(d[k])[:19]; break
print(count, ts)
" 2>/dev/null
}

# ─────────────────────────────────────────────────────────────────────
banner "Phase K — Data Ingestion Audit"

[[ $JSON_MODE -eq 0 ]] && printf "  %-35s %-12s %s\n" "domain / endpoint" "status" "freshness · count"
[[ $JSON_MODE -eq 0 ]] && printf "  %-35s %-12s %s\n" "─────────────────" "──────" "─────────────────"

declare -a RESULTS

probe() {
  local domain="$1" endpoint="$2"
  local code count_ts count ts
  code=$(probe_endpoint "$endpoint")
  if [[ "$code" =~ ^2 ]]; then
    count_ts=$(probe_count_and_freshness "$endpoint")
    count=$(echo "$count_ts" | /usr/bin/awk '{print $1}')
    ts=$(echo "$count_ts" | /usr/bin/awk '{print $2}')
    row "$domain ($endpoint)" "$code" "$ts · n=$count"
    RESULTS+=("{\"domain\":\"$domain\",\"endpoint\":\"$endpoint\",\"status\":$code,\"count\":\"$count\",\"freshness\":\"$ts\"}")
  elif [[ "$code" =~ ^(401|403)$ ]]; then
    row "$domain ($endpoint)" "$code" "(gated — needs Pro+)"
    RESULTS+=("{\"domain\":\"$domain\",\"endpoint\":\"$endpoint\",\"status\":$code,\"note\":\"gated\"}")
  else
    row "$domain ($endpoint)" "$code" "✗ unreachable"
    RESULTS+=("{\"domain\":\"$domain\",\"endpoint\":\"$endpoint\",\"status\":$code,\"note\":\"unreachable\"}")
  fi
}

# ──── ISO / Grid ────
# Phase 293: correct path is /api/v1/grid/<iso> (path param), not ?iso= query.
# /api/v1/grid (no ISO) doesn't exist as a route — skip it.
probe "ISO grid (PJM)"          "/api/v1/grid/pjm"
probe "ISO grid (ERCOT)"        "/api/v1/grid/ercot"
probe "ISO grid (CAISO)"        "/api/v1/grid/caiso"
probe "ISO grid (MISO)"         "/api/v1/grid/miso"
probe "ISO grid (NYISO)"        "/api/v1/grid/nyiso"
probe "ISO grid (SPP)"          "/api/v1/grid/spp"
probe "Grid intelligence (PJM)" "/api/v1/grid-intelligence/pjm"

# ──── Power / energy ────
probe "Energy retail rates"     "/api/v1/energy/electricity-rates"
probe "Energy summary (CA)"     "/api/v1/energy/summary?state=CA"
probe "Energy summary (TX)"     "/api/v1/energy/summary?state=TX"
# Phase 293: renewable endpoints live under /api/renewable/*, not /api/v1/renewable
probe "Renewable solar"         "/api/renewable/solar"
probe "Renewable wind"          "/api/renewable/wind"
probe "Renewable combined"      "/api/renewable/combined"

# ──── Fiber / connectivity ────
probe "Fiber routes (IX)"       "/api/v1/connectivity/ix"
probe "Fiber networks"          "/api/v1/connectivity/networks"
probe "Fiber coverage"          "/api/v1/connectivity/fiber-coverage?lat=33.4&lon=-112.0&radius_km=50"

# ──── Gas ────
probe "Gas prices"              "/api/v1/energy/gas-prices"
probe "Gas storage"             "/api/v1/energy/gas-storage"
probe "Gas pipelines"           "/api/v1/gas-pipelines?limit=10"

# ──── M&A / pipeline ────
probe "M&A deals"               "/api/v1/deals?limit=10"
probe "Construction pipeline"   "/api/v1/pipeline?limit=10"

# ──── News / DCPI ────
# Phase 293: real news paths are /api/news, /api/news-feed, /api/news/live
probe "News (live)"             "/api/news/live"
probe "News (feed)"             "/api/news-feed"
probe "DCPI live count"         "/api/v1/dcpi/live-count"
probe "DCPI quality"            "/api/v1/dcpi/quality"
probe "DCPI leaderboard"        "/api/v1/dcpi/leaderboard"

# ──── Facilities ────
probe "Facilities (paged)"      "/api/v1/facilities?limit=10"

# ──── Brain v2 + Outreach (phases 289-290) ────
probe "Brain v2 status"         "/api/v1/brain/status"
probe "Outreach status"         "/api/v1/outreach/cap-exceeded/status"

# ─────────────────────────────────────────────────────────────────────
banner "Summary"

# Count by status
total=${#RESULTS[@]}
banner=""
ok_count=0
gated_count=0
fail_count=0
for r in "${RESULTS[@]}"; do
  case "$r" in
    *\"status\":2*) ok_count=$((ok_count+1)) ;;
    *\"status\":401*|*\"status\":403*) gated_count=$((gated_count+1)) ;;
    *) fail_count=$((fail_count+1)) ;;
  esac
done

[[ $JSON_MODE -eq 0 ]] && printf "  endpoints probed: %d\n" "$total"
[[ $JSON_MODE -eq 0 ]] && printf "  \033[1;32m✓ ok:\033[0m %d   \033[1;33m⚠ gated:\033[0m %d   \033[1;31m✗ unreachable:\033[0m %d\n" "$ok_count" "$gated_count" "$fail_count"

if [[ $JSON_MODE -eq 1 ]]; then
  printf "{\n  \"ran_at\": \"%s\",\n  \"base\": \"%s\",\n  \"summary\": {\"total\": %d, \"ok\": %d, \"gated\": %d, \"unreachable\": %d},\n  \"endpoints\": [\n    " \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BASE" "$total" "$ok_count" "$gated_count" "$fail_count"
  printf "%s,\n    " "${RESULTS[@]:0:$((total-1))}"
  printf "%s\n" "${RESULTS[$((total-1))]}"
  printf "  ]\n}\n"
else
  echo
  echo "  Next actions:"
  echo "    • ✓ ok = data flowing → check freshness column for staleness"
  echo "    • ⚠ gated = endpoint requires Pro plan → expected for some surfaces"
  echo "    • ✗ unreachable = ingestion pipeline may be broken or route is missing"
fi
