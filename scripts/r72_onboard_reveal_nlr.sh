#!/usr/bin/env bash
# r74 (2026-05-26) — Gabriel + Galen / NLR ENTERPRISE onboarding.
#
# Issues an Enterprise-tier API key for a single NLR contact + prints
# the partnership email body ready to paste into Gmail.
#
# Partnership context: DCHub_NLR_Enterprise_License_Proposal.pdf
# (April 2026). Strategic partnership rate, DOE co-marketing rights,
# joint-research IP. NDA → MOU → license at $10K/yr (closed at
# operator-stated $3K/yr).
#
# r74 changes vs r72:
#   - plan default: developer → ENTERPRISE
#   - email body reflects partnership terms (not generic Developer)
#   - DOE co-marketing acknowledgement
#   - References Galen as second contact
#
# Usage (for each contact, run separately):
#   chmod +x scripts/r72_onboard_reveal_nlr.sh
#
#   # Gabriel:
#   CONTACT_EMAIL=Gabriel.Zuckerman@nlr.gov \
#   CONTACT_NAME="Gabriel Zuckerman" \
#   DCHUB_ADMIN_KEY=$DCHUB_ADMIN_KEY \
#     ./scripts/r72_onboard_reveal_nlr.sh
#
#   # Galen (when his email arrives):
#   CONTACT_EMAIL=Galen.Lastname@nlr.gov \
#   CONTACT_NAME="Galen Lastname" \
#   DCHUB_ADMIN_KEY=$DCHUB_ADMIN_KEY \
#     ./scripts/r72_onboard_reveal_nlr.sh

set -euo pipefail

if [ -z "${DCHUB_ADMIN_KEY:-}" ]; then
  echo "ERROR: DCHUB_ADMIN_KEY env var required."
  exit 1
fi

# Back-compat: GABRIEL_EMAIL/GABRIEL_NAME still accepted as aliases for
# CONTACT_EMAIL/CONTACT_NAME so prior scripts keep working.
CONTACT_EMAIL="${CONTACT_EMAIL:-${GABRIEL_EMAIL:-}}"
CONTACT_NAME="${CONTACT_NAME:-${GABRIEL_NAME:-}}"

if [ -z "$CONTACT_EMAIL" ]; then
  echo "ERROR: CONTACT_EMAIL (or GABRIEL_EMAIL) env var required."
  echo "  Run with: CONTACT_EMAIL=user@nlr.gov CONTACT_NAME='First Last' \\"
  echo "            DCHUB_ADMIN_KEY=\$DCHUB_ADMIN_KEY $0"
  exit 1
fi
if [ -z "$CONTACT_NAME" ]; then
  CONTACT_NAME="${CONTACT_EMAIL%@*}"
fi

CONTACT_COMPANY="${CONTACT_COMPANY:-NLR}"
# r76-a (2026-05-26): default back to developer. NLR contract (NDA + MOU
# + License Agreement) targets 90-day execution per Jonathan's email to
# Gabe. Until the License Agreement is signed, the partnership operates
# on a Developer-tier key — full API surface, no Pro-only gates beyond
# what already exists. Flip to PLAN=enterprise on this script call AFTER
# the contract executes:
#   PLAN=enterprise CONTACT_EMAIL=... ./scripts/r72_onboard_reveal_nlr.sh
# Idempotent partner-key/issue revokes the prior Developer row + mints
# an Enterprise replacement, so the upgrade is a one-shell-line cutover.
PLAN="${PLAN:-developer}"
BASE="${DCHUB_API_BASE:-https://dchub-backend-production.up.railway.app}"

echo "→ Issuing ${PLAN}-tier key for $CONTACT_NAME ($CONTACT_EMAIL)…"

RESULT=$(curl -sS -X POST "$BASE/api/v1/admin/partner-key/issue" \
  -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data @<(cat <<JSON
{
  "partner_slug":    "reveal-nlr",
  "email":           "$CONTACT_EMAIL",
  "name":            "$CONTACT_NAME",
  "company":         "$CONTACT_COMPANY",
  "plan":            "$PLAN",
  "label":           "NLR Year-1 Research Seed (FY 2026, \$3K) → Year-2 Strategic Partnership (\$10K). reVeal Characterize integration, NLR/PR-6A20-99256. Full API surface (25+ endpoints) Day 1. Partnership rights active: co-authorship, reference, conference, reVeal v2 first-look.",
  "stripe_url":      "https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e",
  "amount_usd_year": 3000,
  "term_months":     12,
  "renewal_terms":   "Year 2+ converts to Strategic Partnership at \$10K/yr when NLR's dedicated DC-siting funding closes. CPI-U capped. 60-day written notice. NDA + MOU + License Agreement target execution within 90 days."
}
JSON
))

echo "$RESULT" | python3 -m json.tool

API_KEY=$(echo "$RESULT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))")
KEY_PREFIX=$(echo "$RESULT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key_prefix',''))")

if [ -z "$API_KEY" ]; then
  echo "ERROR: no key returned. Check the JSON above for details."
  exit 1
fi

GIVEN_NAME="${CONTACT_NAME%% *}"   # first word of name for casual greeting

# Title-case the plan once for reuse (macOS bash 3.2 compatible).
PLAN_TITLE="$(printf %s "$PLAN" | tr '[:lower:]' '[:upper:]' | cut -c1)$(printf %s "$PLAN" | cut -c2-)"

# Tier-appropriate framing for the addendum body. The endpoint surface
# is the same in both cases (per the License Schedule A.5 + NLR partner
# rights flag in routes/partner_key_issuer.py), but the framing differs:
# Developer = pre-execution Research Seed; Enterprise = post-License.
if [ "$PLAN" = "enterprise" ]; then
  TIER_LINE="unlocked at your Enterprise tier — none of the Developer paywalls"
  KEY_LABEL="Enterprise API key"
else
  TIER_LINE="active under your NLR Research Seed terms — partner key, full Schedule A surface"
  KEY_LABEL="$PLAN_TITLE API key (NLR Research Seed)"
fi

echo
echo "════════════════════════════════════════════════════════════════"
echo "✅ $PLAN_TITLE key issued for $GIVEN_NAME."
echo "════════════════════════════════════════════════════════════════"
echo
echo "  Key:    $API_KEY"
echo "  Header: X-API-Key"
echo "  Tier:   $PLAN_TITLE"
echo
echo "Smoke test (Ashburn VA — composite + 2050 forecast):"
echo "  curl -H \"X-API-Key: $API_KEY\" \\"
echo "    \"https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA\""
echo
echo "═══ Technical addendum — paste into the strategic email or send as follow-up ═══"
echo
cat <<TECH
$GIVEN_NAME — technical onboarding details:

  Your $KEY_LABEL: $API_KEY
  Header on every request: X-API-Key

The full reVeal Characterize feature-set mapping (10 endpoints
$TIER_LINE):

  /api/v1/site-forecast        composite + 2050 forecast (headline)
  /api/v1/grid-intelligence    reserve margin + queue depth
  /api/v1/grid/data            raw ISO load timeseries
  /api/v1/fiber/intel          per-facility carrier intel
  /api/v1/fiber/routes         route geometry, 20+ carriers
  /api/v1/energy/retail        EIA state-level retail electricity rates
  /api/v1/energy/renewable     renewable capacity + PPA market depth
  /api/v1/water/stress         live USGS water-stress readings
  /api/v1/infrastructure       HIFLD substations (79K+) + FEMA hazard
  /api/v1/tax-incentives       50-state DC tax abatements

  OpenAPI:      https://dchub.cloud/openapi.json
  MCP server:   https://dchub.cloud/mcp
                (server card: /.well-known/mcp/server-card.json)
TECH
echo
echo "════════════════════════════════════════════════════════════════"
echo "Audit:  /api/v1/admin/partner-key/audit"
echo "Revoke: POST /api/v1/admin/partner-key/revoke/$KEY_PREFIX"
echo "Label:  NLR Year-1 Research Seed (FY 2026, \$3K) → Year-2 Strategic (\$10K)"
echo "════════════════════════════════════════════════════════════════"
