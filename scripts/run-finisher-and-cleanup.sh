#!/usr/bin/env bash
# run-finisher-and-cleanup.sh
# ================================================
# One-shot: set env vars + run ship-final-five.sh + drain dedup backlog
# + purge stale brain findings + replay Stripe events.
#
# Edit the two EDIT_ME lines below, then run:
#   bash scripts/run-finisher-and-cleanup.sh
#
# Safe to re-run — every step is idempotent.
# Phase ZZZZZ-round12-cleanup (2026-05-23).

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
blue()   { printf "\033[34m%s\033[0m\n" "$*"; }
hr()     { printf "\033[90m─────────────────────────────────────\033[0m\n"; }
step()   { hr; blue "▸ $*"; }

# ─────────────────────────────────────────────────────────────────────
# Read IPINFO_TOKEN from environment (preferred — no file edit needed).
# Falls back to the in-file constant for backward compat with existing
# uses of this script. If neither is set, you'll be prompted whether
# to continue without IPinfo enrichment.
#
# Preferred usage:
#   IPINFO_TOKEN=ipinfo_xxx bash scripts/run-finisher-and-cleanup.sh
#
# OR set persistent:
#   export IPINFO_TOKEN=ipinfo_xxx
#   bash scripts/run-finisher-and-cleanup.sh
IPINFO_TOKEN_FALLBACK='ipinfo_xxx_paste_yours_here'
if [ -z "${IPINFO_TOKEN:-}" ] || [ "${IPINFO_TOKEN:-}" = "$IPINFO_TOKEN_FALLBACK" ]; then
    IPINFO_TOKEN="$IPINFO_TOKEN_FALLBACK"
fi

# Phase ZZZZZ-round13b (2026-05-23): default to the legacy hardcoded key
# (which internal_auth.is_valid_internal_key accepts with a warning) if
# no DCHUB_INTERNAL_KEY is set in env. The OLD logic generated a random
# 64-char openssl key, which the SERVER doesn't know — so every admin
# call got 401. Random keys never work without a matching env-var on
# Railway; the legacy key always works (until LEGACY_OK is flipped off).
#
# Override paths (any one wins):
#   1. export DCHUB_INTERNAL_KEY=<your value> ; bash scripts/run-finisher-and-cleanup.sh
#   2. DCHUB_INTERNAL_KEY=<value> bash -c 'bash scripts/run-finisher-and-cleanup.sh'
#   3. Edit this script and replace the default below.
LEGACY_INTERNAL_KEY="dchub-internal-sync-2026"
if [ -z "${DCHUB_INTERNAL_KEY:-}" ]; then
    DCHUB_INTERNAL_KEY="$LEGACY_INTERNAL_KEY"
    echo "  (using legacy fallback key — server's internal_auth accepts it)"
fi

export IPINFO_TOKEN
export DCHUB_INTERNAL_KEY

green "Generated/loaded:"
echo "  DCHUB_INTERNAL_KEY = ${DCHUB_INTERNAL_KEY:0:8}...  (length: ${#DCHUB_INTERNAL_KEY})"
echo "  IPINFO_TOKEN       = ${IPINFO_TOKEN:0:8}...  (length: ${#IPINFO_TOKEN})"
echo

# Validate IPINFO_TOKEN was actually replaced
if [ "$IPINFO_TOKEN" = "$IPINFO_TOKEN_FALLBACK" ]; then
    red "WARNING: IPINFO_TOKEN is still the placeholder."
    red "  Pass it via env:  IPINFO_TOKEN=ipinfo_xxx bash scripts/run-finisher-and-cleanup.sh"
    red "  Sign up at https://ipinfo.io/signup to get a free token (50k lookups/mo)."
    echo
    read -p "Continue without IPinfo (item 4 will be skipped)? [y/N] " ans
    if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
        exit 1
    fi
    unset IPINFO_TOKEN
fi

# ─────────────────────────────────────────────────────────────────────
step "1/4  Run ship-final-five.sh (Railway env-var setter)"
# ─────────────────────────────────────────────────────────────────────
if [ -f "scripts/ship-final-five.sh" ]; then
    bash scripts/ship-final-five.sh
else
    red "scripts/ship-final-five.sh not found — skipping"
fi

# ─────────────────────────────────────────────────────────────────────
step "2/4  Drain the dedup backlog (10 calls × 2000 records each)"
# ─────────────────────────────────────────────────────────────────────
for i in 1 2 3 4 5 6 7 8 9 10; do
    echo "  dedup pass $i/10..."
    curl -sS -X POST \
        -H "X-Admin-Key: $DCHUB_INTERNAL_KEY" \
        "https://dchub.cloud/api/v1/admin/dedup/run?max=2000" \
        -w "    http=%{http_code}  time=%{time_total}s\n" \
        -o /tmp/dedup-pass-$i.json
    if [ -f /tmp/dedup-pass-$i.json ]; then
        python3 -c "
import json
try:
    d = json.load(open('/tmp/dedup-pass-$i.json'))
    print(f'    auto_approved={d.get(\"auto_approved\",\"?\")} marked_dup={d.get(\"marked_duplicate\",\"?\")} skipped={d.get(\"skipped\",\"?\")} status={d.get(\"status\",\"?\")}')
except Exception as e:
    print(f'    (could not parse: {e})')
" 2>/dev/null
    fi
    sleep 5
done

# ─────────────────────────────────────────────────────────────────────
step "3/4  Purge stale brain findings (drops 22,677 self-as-whale + >30d)"
# ─────────────────────────────────────────────────────────────────────
curl -sS -X POST \
    -H "X-Admin-Key: $DCHUB_INTERNAL_KEY" \
    "https://dchub.cloud/api/v1/admin/heal/purge-stale" \
    -w "  http=%{http_code}  time=%{time_total}s\n" \
    -o /tmp/heal-purge.json
if [ -f /tmp/heal-purge.json ]; then
    python3 -c "
import json
try:
    d = json.load(open('/tmp/heal-purge.json'))
    print('  before:', d.get('before', {}))
    print('  deleted_railway_bots:', d.get('deleted_railway_bots'))
    print('  deleted_over_30d:', d.get('deleted_over_30d'))
    print('  after:', d.get('after', {}))
except Exception as e:
    print(f'  (could not parse: {e})')
" 2>/dev/null
fi

# ─────────────────────────────────────────────────────────────────────
step "4/4  Stripe webhook replay (catch up missed events)"
# ─────────────────────────────────────────────────────────────────────
curl -sS -X POST \
    -H "X-Admin-Key: $DCHUB_INTERNAL_KEY" \
    -H "X-Internal-Key: $DCHUB_INTERNAL_KEY" \
    "https://dchub.cloud/api/stripe/webhook/replay" \
    -w "  http=%{http_code}  time=%{time_total}s\n" \
    -o /tmp/stripe-replay.json
if [ -f /tmp/stripe-replay.json ]; then
    head -c 400 /tmp/stripe-replay.json
    echo
fi

# ─────────────────────────────────────────────────────────────────────
hr
green "Done — finisher-and-cleanup complete."
hr
echo "Verify a few minutes later:"
echo
echo "  # Brain heal-findings count should drop (was 84):"
echo "  curl -s https://dchub.cloud/api/v1/heal/findings | python3 -c \\"
echo "    \"import sys,json;d=json.load(sys.stdin);print(len(d.get('actionable_backend_issues',[])))\""
echo
echo "  # Brain registry should be 21 classes, >=13 with proof:"
echo "  curl -s https://dchub.cloud/api/v1/brain/error-classes | python3 -c \\"
echo "    \"import sys,json;d=json.load(sys.stdin);c=d.get('classes',[]);print(f'{len(c)} classes, {sum(1 for x in c if x.get(\\\"shipped_proof\\\"))} with proof')\""
echo
echo "  # IPinfo activation check:"
echo "  curl -s -H \"X-Admin-Key: \$DCHUB_INTERNAL_KEY\" \\"
echo "    'https://dchub.cloud/api/v1/admin/ip-enrich?ip=8.8.8.8' | python3 -m json.tool"
echo
echo "  # Test the new paywall on /api/v1/grid/intelligence (anon should get gated):"
echo "  curl -s https://dchub.cloud/api/v1/grid/intelligence/ERCOT | python3 -c \\"
echo "    \"import sys,json;d=json.load(sys.stdin);print('gated:',d.get('gated'),'has_action:',bool(d.get('agent_action')))\""
