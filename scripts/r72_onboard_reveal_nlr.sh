#!/usr/bin/env bash
# r72 (2026-05-26) — Gabriel / reVeal (NLR) partner onboarding.
#
# Issues a Developer-tier API key for Gabriel + prints the integration
# email body ready to paste into Gmail.
#
# Partnership context: DCHub_reVeal_Partnership.pdf (April 2026 / v2.5.7).
# Step 1 of 5 in the proposal: "Free Developer API key provided to NLR
# team immediately." This script does that.
#
# Usage:
#   chmod +x scripts/r72_onboard_reveal_nlr.sh
#   GABRIEL_EMAIL=gabriel.lastname@reveal-or-nlr-domain.gov \
#   DCHUB_ADMIN_KEY=<your-key> \
#     ./scripts/r72_onboard_reveal_nlr.sh

set -euo pipefail

if [ -z "${DCHUB_ADMIN_KEY:-}" ]; then
  echo "ERROR: DCHUB_ADMIN_KEY env var required."
  exit 1
fi
if [ -z "${GABRIEL_EMAIL:-}" ]; then
  echo "ERROR: GABRIEL_EMAIL env var required."
  echo "Pass Gabriel's email like:"
  echo "  GABRIEL_EMAIL=gabriel@example.com DCHUB_ADMIN_KEY=\$DCHUB_ADMIN_KEY $0"
  exit 1
fi

GABRIEL_NAME="${GABRIEL_NAME:-Gabriel}"
GABRIEL_COMPANY="${GABRIEL_COMPANY:-reVeal (NLR)}"
BASE="${DCHUB_API_BASE:-https://dchub-backend-production.up.railway.app}"

echo "→ Issuing Developer-tier key for $GABRIEL_NAME ($GABRIEL_EMAIL)…"

RESULT=$(curl -sS -X POST "$BASE/api/v1/admin/partner-key/issue" \
  -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data @<(cat <<JSON
{
  "partner_slug": "reveal-nlr",
  "email":        "$GABRIEL_EMAIL",
  "name":         "$GABRIEL_NAME",
  "company":      "$GABRIEL_COMPANY",
  "plan":         "developer",
  "label":        "reVeal Characterize integration (NLR/PR-6A20-99256)"
}
JSON
))

echo "$RESULT" | python3 -m json.tool

API_KEY=$(echo "$RESULT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")

if [ -z "$API_KEY" ]; then
  echo "ERROR: no key returned. Check the JSON above for details."
  exit 1
fi

echo
echo "════════════════════════════════════════════════════════════════"
echo "✅ Key issued. Now paste the email below into Gmail (To: $GABRIEL_EMAIL):"
echo "════════════════════════════════════════════════════════════════"
echo
cat <<EMAIL
Subject: DC Hub × reVeal — your Developer API key + integration starter

Hi $GABRIEL_NAME,

Great to have NLR / reVeal on board. Per the partnership doc we shared
(DCHub_reVeal_Partnership.pdf, v2.5.7), here's step 1: your Developer-
tier DC Hub API key. No credit card, no expiration, generous limits.

  Your API key: $API_KEY
  Tier:         Developer (2,000 calls/day, full data, all 29 tools)
  Header:       X-API-Key

Smoke test (Ashburn VA — the example we cited in the proposal):

  curl -H "X-API-Key: $API_KEY" \\
    "https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA"

That returns the reVeal-inspired composite suitability score plus the
2030–2050 deployment forecast (built specifically on top of your
methodology). With your Developer key the full forecast is unlocked
(free tier would show suitability only).

The 9 endpoints we mapped to reVeal's Characterize feature set are
all on the same key:

  /api/v1/grid/intelligence       (reserve margin, queue depth — fills slide 25 gap)
  /api/v1/grid-data               (live ISO/RTO load + curtailment)
  /api/v1/fiber/intel             (20+ carriers, route geometry)
  /api/v1/water-risk              (USGS live gauge readings)
  /api/v1/energy-prices           (EIA state-level, refreshed monthly)
  /api/v1/infrastructure          (HIFLD 79K+ substations, FEMA hazard risk)
  /api/v1/tax-incentives          (50 states — NEW dimension vs. current reVeal)
  /api/v1/renewable-energy        (PPA market depth — NEW dimension)
  /api/v1/site-forecast           (composite + 2050 forecast — the headline tool)

OpenAPI spec for everything: https://dchub.cloud/openapi.json
MCP server (drop into Claude / Cursor / your agent config):
  https://dchub.cloud/mcp
  Server card: https://dchub.cloud/.well-known/mcp/server-card.json

Proposed step 2 from the partnership doc:

  Swap water_availability (reVeal's lowest-importance feature per slide
  21) with DC Hub live USGS readings, retrain the random forest, compare
  AUC before/after. Happy to coordinate on the train/test split and pick
  a markets cohort that exercises the new dimensions cleanly.

Available for a 20-min walk-through this week if useful. Just reply.

Best,
Jonathan
Founder, DC Hub
azmartone@gmail.com · dchub.cloud
EMAIL

echo
echo "════════════════════════════════════════════════════════════════"
echo "Audit trail: this key is in /api/v1/admin/partner-key/audit"
echo "Kill switch: POST /api/v1/admin/partner-key/revoke/$(echo "$RESULT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key_prefix',''))")"
echo "════════════════════════════════════════════════════════════════"
