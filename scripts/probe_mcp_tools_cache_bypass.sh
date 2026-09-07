#!/usr/bin/env bash
# Canary for the zone Cache Rule that keeps /api/v1/mcp/tools/* out of the CF edge.
#
# WHY THIS EXISTS (2026-09-06/07)
# ------------------------------
# dchub-backend#4038 gated export_facility_csv behind a developer-tier key. The
# ORIGIN gate worked immediately, but the edge kept serving the pre-gate body:
# 10/10 un-cache-busted probes returned HTTP 200 / cf-cache-status: HIT /
# 1,003,629 bytes of the full registry to an anonymous caller. Three layers were
# needed to close it — the origin gate, the worker's ROUTE_CACHE_MAP entry
# (dchub-frontend#1409), and a ZONE Cache Rule (#1413, rule id
# b3ce82fb57cc4834b43fb5516b3bdb9e, appended last).
#
# Only the first two live in git. The zone rule lives in the Cloudflare
# dashboard, nothing in CI reads the zone ruleset, and it was ALREADY silently
# broken once within an hour of being applied — reordered from last to first,
# which in the cache phase means "overridden by the rule after it". This script
# is the only thing that would notice.
#
# ★ WHAT TO ASSERT ON, AND WHAT NOT TO
# The obvious probe — "does export_facility_csv return 401 to anon?" — is
# WORTHLESS as a cache canary. 4xx responses are not cached in the first place,
# so that probe reads cf-cache-status: BYPASS whether or not the zone rule
# exists. It stayed green through the entire hour the rule was inverted.
# The canary must therefore ride a CACHEABLE 200 on the same prefix:
# /api/v1/mcp/tools/reports/health.
#
# ★ AND IT NEEDS A CONTROL. "Nothing under the prefix is cached" also passes if
# somebody bypasses the cache zone-wide, which would be a large, silent
# performance regression. /api/v1/stats must still cache. One assertion without
# the other is half a test.
#
# Cache-busts every run: a pre-existing entry for a fixed URL would mask a
# missing rule on the second and later runs.
#
# Exit 0 = rule in force. Exit 1 = named failure. Exit 2 = could not measure
# (network/DNS) — deliberately NOT conflated with "the rule is gone".
set -uo pipefail

HOST="${DCHUB_PROBE_HOST:-https://dchub.cloud}"
GATED_PATH="/api/v1/mcp/tools/reports/health"   # cacheable 200 on the guarded prefix
CONTROL_PATH="/api/v1/stats"                     # cacheable 200 OUTSIDE it
# CF reports an uncached, origin-served response as DYNAMIC, and an explicit
# bypass-rule match as BYPASS. Either proves the rule is winning; MISS/HIT/
# EXPIRED/REVALIDATED all mean the response entered the cache.
UNCACHED_OK="DYNAMIC BYPASS"

fail=0
note() { printf '  %s\n' "$*"; }

_cf() { # url -> "cf_status http_code"; empty cf_status on transport error
  local url="$1" hdr code cf
  hdr=$(mktemp)
  code=$(curl -s -o /dev/null -D "$hdr" -w '%{http_code}' --max-time 15 "$url" 2>/dev/null) || { rm -f "$hdr"; echo " 000"; return; }
  cf=$(grep -i '^cf-cache-status:' "$hdr" | tr -d '\r' | awk '{print $2}')
  rm -f "$hdr"
  echo "${cf:-none} ${code}"
}

echo "zone Cache Rule canary — $(date -u +%FT%TZ) — $HOST"

# ── 1. the guarded prefix must NOT enter the cache ────────────────────────
Z="$HOST$GATED_PATH?canary=$(date +%s)$RANDOM"
transport_err=0
for i in 1 2; do
  read -r cf code <<<"$(_cf "$Z")"
  note "guarded  probe$i: cf=$cf http=$code"
  if [ "$code" = "000" ]; then transport_err=1; continue; fi
  case " $UNCACHED_OK " in
    *" $cf "*) ;;
    *)
      note "FAIL: $GATED_PATH returned cf-cache-status=$cf — the response was cached."
      note "      The zone Cache Rule for /api/v1/mcp/tools/* is missing, disabled,"
      note "      or no longer the LAST matching rule (cache phase = last match wins;"
      note "      moving it above 'Cache Public API' silently disables it)."
      note "      Fix: Caching > Cache Rules, bypass rule for that prefix, placed LAST."
      fail=$((fail + 1))
      ;;
  esac
done

# ── 2. the control must STILL cache (else we over-bypassed) ───────────────
C="$HOST$CONTROL_PATH"
ctl_cached=0
for i in 1 2; do
  read -r cf code <<<"$(_cf "$C")"
  note "control  probe$i: cf=$cf http=$code"
  if [ "$code" = "000" ]; then transport_err=1; continue; fi
  case "$cf" in HIT|MISS|EXPIRED|REVALIDATED|STALE) ctl_cached=1 ;; esac
done
if [ "$transport_err" -eq 0 ] && [ "$ctl_cached" -eq 0 ]; then
  note "FAIL: $CONTROL_PATH is no longer cached either — the bypass is too broad."
  note "      Check the rule's expression really is scoped to /api/v1/mcp/tools/."
  fail=$((fail + 1))
fi

# ── 3. and the origin gate itself is still shut ───────────────────────────
read -r cf code <<<"$(_cf "$HOST/api/v1/mcp/tools/export_facility_csv?limit=10000&canary=$(date +%s)$RANDOM")"
note "gate     anon:     cf=$cf http=$code (want 401)"
if [ "$code" != "000" ] && [ "$code" != "401" ]; then
  note "FAIL: anonymous bulk export returned $code, not 401 — the ORIGIN gate is open."
  fail=$((fail + 1))
fi

echo "---"
if [ "$transport_err" -eq 1 ] && [ "$fail" -eq 0 ]; then
  echo "canary: COULD NOT MEASURE (transport error) — not a verdict on the rule"
  exit 2
fi
if [ "$fail" -gt 0 ]; then
  echo "canary: $fail check(s) FAILED"
  exit 1
fi
echo "canary: rule in force — guarded prefix uncached, control still cached, gate shut"
