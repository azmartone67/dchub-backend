#!/usr/bin/env bash
# dchub_evolve.sh — Phase H/I/J/K/L master orchestrator.
#
# One command, five phases:
#   H — verify the synthetic-testimonial filter is live in dc-hub-media
#       (frontend PR — proves Phase 288 shipped)
#   I — Brain v2 Layer 4 status: scaffolded, activates when
#       ANTHROPIC_API_KEY is set in Railway env
#   J — Cap-exceeded outreach status: scaffolded, activates when
#       DCHUB_RESEND_API_KEY is set; can be dry-run anytime
#   K — Ingestion audit: live read-only probe of every ingestion endpoint
#       (ISO grid, energy, fiber, gas, M&A, pipeline, DCPI, news)
#   L — DCPI methodology doc: drafted at docs/DCPI_METHODOLOGY.md
#
# Usage:
#   DCHUB_API_KEY=… bash scripts/dchub_evolve.sh
#
# Read-only. Re-runnable. Each section reports state + next-action TODO.

set -uo pipefail
CURL=/usr/bin/curl
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BASE="${DCHUB_BASE:-https://dchub.cloud}"
KEY="${DCHUB_API_KEY:-}"
ADMIN_KEY="${DCHUB_ADMIN_KEY:-}"

banner() { printf "\n\033[1;36m═══ %s ═══\033[0m\n" "$1"; }
ok()     { printf "  \033[1;32m✓\033[0m %s\n" "$1"; }
warn()   { printf "  \033[1;33m⚠\033[0m %s\n" "$1"; }
fail()   { printf "  \033[1;31m✗\033[0m %s\n" "$1"; }

H=()
[[ -n "$KEY" ]] && H=(-H "X-API-Key: $KEY")

# ─────────────────────────────────────────────────────────────────
banner "Phase H — dc-hub-media synthetic testimonial filter"

code=$("$CURL" -s -o /tmp/dchub_evolve_media.html -w "%{http_code}" --max-time 12 "${BASE}/dc-hub-media/")
if [[ "$code" == "200" ]]; then
  # Check if the page references /testimonials (our new CTA target)
  if /usr/bin/grep -q "/testimonials" /tmp/dchub_evolve_media.html 2>/dev/null; then
    ok "/dc-hub-media is live and links to /testimonials"
  else
    warn "/dc-hub-media is live but /testimonials CTA not yet deployed (PR #16 may still be merging)"
  fi
  # Probe feed-v3 to count synthetic-vs-real testimonials
  feed_summary=$("$CURL" -s --max-time 12 "${BASE}/api/v1/media/feed-v3" 2>/dev/null | python3 -c "
import json, sys, re
try: d = json.load(sys.stdin)
except: print('  unparseable feed'); sys.exit(0)
items = d.get('items', [])
synth = sum(1 for i in items if (i.get('category')=='testimonial' and (i.get('source','') in ('mcp-auto','mcp_auto') or (i.get('title') or '')=='Claude' or re.match(r'^Claude (used|queried)', i.get('title','') or ''))))
real_t = sum(1 for i in items if i.get('category')=='testimonial') - synth
print(f'  feed-v3: {len(items)} items · {synth} synthetic testimonials filtered · {real_t} real testimonials')
" 2>/dev/null)
  echo "$feed_summary"
else
  fail "/dc-hub-media returned HTTP $code"
fi

# ─────────────────────────────────────────────────────────────────
banner "Phase I — Brain v2 Layer 4 self-learning"

brain_status=$("$CURL" -s --max-time 12 "${BASE}/api/v1/brain/status" 2>/dev/null)
echo "$brain_status" | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except: print('  endpoint not yet deployed'); sys.exit(0)
print(f\"  layer: {d.get('layer')}  loaded: {d.get('loaded')}  active: {d.get('active')}\")
print(f\"  model: {d.get('model')}  proposed_fixes: {d.get('proposed_fixes_count')}  log: {d.get('learning_log_count')}\")
hint = d.get('hint')
if hint: print(f'  hint: {hint}')
" 2>&1 | /usr/bin/head -5

# ─────────────────────────────────────────────────────────────────
banner "Phase J — Cap-exceeded outreach"

out_status=$("$CURL" -s --max-time 12 "${BASE}/api/v1/outreach/cap-exceeded/status" 2>/dev/null)
echo "$out_status" | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except: print('  endpoint not yet deployed'); sys.exit(0)
print(f\"  loaded: {d.get('loaded')}  active (resend): {d.get('active')}  stripe link: {d.get('stripe_link_set')}\")
hint = d.get('hint')
if hint: print(f'  hint: {hint}')
" 2>&1 | /usr/bin/head -5

if [[ -n "$ADMIN_KEY" ]]; then
  echo "  (admin key set — running dry-run to see eligible cohort)"
  "$CURL" -s -X POST --max-time 15 \
    -H "X-Admin-Key: $ADMIN_KEY" \
    "${BASE}/api/v1/outreach/cap-exceeded/run" 2>/dev/null \
    | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except: sys.exit(0)
print(f\"  mode: {d.get('mode')}  would_send: {d.get('would_send','?')}\")
" 2>/dev/null
fi

# ─────────────────────────────────────────────────────────────────
banner "Phase K — Ingestion audit"

if [[ -x "$SCRIPT_DIR/dchub_ingestion_audit.sh" ]]; then
  DCHUB_API_KEY="$KEY" bash "$SCRIPT_DIR/dchub_ingestion_audit.sh" 2>&1 | /usr/bin/tail -40
else
  fail "$SCRIPT_DIR/dchub_ingestion_audit.sh not found"
fi

# ─────────────────────────────────────────────────────────────────
banner "Phase L — DCPI methodology doc"

methodology="$REPO_DIR/docs/DCPI_METHODOLOGY.md"
if [[ -f "$methodology" ]]; then
  lines=$(/usr/bin/wc -l < "$methodology" | /usr/bin/tr -d ' ')
  sections=$(/usr/bin/grep -c "^## " "$methodology")
  ok "docs/DCPI_METHODOLOGY.md present · $lines lines · $sections sections"
  echo "    publish to: https://dchub.cloud/dcpi#methodology"
  echo "    cite via: 'DC Hub Data Center Power Index. https://dchub.cloud/dcpi'"
else
  fail "docs/DCPI_METHODOLOGY.md not found"
fi

# ─────────────────────────────────────────────────────────────────
banner "Summary + next actions"
echo
echo "  Env vars to set in Railway to activate full self-evolution:"
echo "    ANTHROPIC_API_KEY            → Phase I (Brain v2 self-learning)"
echo "    DCHUB_RESEND_API_KEY         → Phase J (outreach email delivery)"
echo "    DCHUB_OUTREACH_FROM_EMAIL    → Phase J (sender address; default noreply@dchub.cloud)"
echo "    DCHUB_STRIPE_DEVELOPER_LINK  → Phase J (one-click upgrade link in emails)"
echo
echo "  Cron schedule (add to GH Actions):"
echo "    Phase I — every 1h:  curl -X POST -H \"X-Admin-Key: \$ADMIN\" \$BASE/api/v1/brain/learn"
echo "    Phase J — every 6h:  curl -X POST -H \"X-Admin-Key: \$ADMIN\" \$BASE/api/v1/outreach/cap-exceeded/run?send=true"
echo "    Phase K — every 6h:  bash scripts/dchub_ingestion_audit.sh --json > /tmp/audit.json"
echo
echo "  Verifier:"
echo "    bash scripts/dchub_postdeploy.sh   # end-to-end deploy health"
echo "    bash scripts/dchub_evolve.sh       # this script (Phase H-L status)"
