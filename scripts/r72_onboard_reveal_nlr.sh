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
PLAN="${PLAN:-enterprise}"
BASE="${DCHUB_API_BASE:-https://dchub-backend-production.up.railway.app}"

echo "→ Issuing ${PLAN}-tier key for $CONTACT_NAME ($CONTACT_EMAIL)…"

RESULT=$(curl -sS -X POST "$BASE/api/v1/admin/partner-key/issue" \
  -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data @<(cat <<JSON
{
  "partner_slug": "reveal-nlr",
  "email":        "$CONTACT_EMAIL",
  "name":         "$CONTACT_NAME",
  "company":      "$CONTACT_COMPANY",
  "plan":         "$PLAN",
  "label":        "reVeal Characterize integration — NLR/PR-6A20-99256 (Enterprise license, DOE co-marketing partnership)"
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

echo
echo "════════════════════════════════════════════════════════════════"
echo "✅ ${PLAN^} key issued. Paste the email below into Gmail."
echo "   To: $CONTACT_EMAIL"
echo "════════════════════════════════════════════════════════════════"
echo
cat <<EMAIL
Subject: Re: DC Hub × NLR — your Enterprise API key

Hey $GIVEN_NAME,

Apologies for the wait. Per our Enterprise License agreement (cost-plus
partnership + DOE co-marketing terms) your Enterprise-tier DC Hub API
key is below. Full access to all 29 tools including the four Pro-only
ones (get_grid_intelligence, get_fiber_intel, analyze_site,
compare_sites) — none of the Developer-tier paywalls will hit you.

  Key:    $API_KEY
  Header: X-API-Key
  Tier:   Enterprise (unlimited paid tools, 100K calls/day, full data)

Smoke test (Ashburn VA from the partnership doc) — returns a composite
suitability score plus the 2030–2050 deployment forecast:

  curl -H "X-API-Key: $API_KEY" \\
    "https://dchub.cloud/api/v1/site-forecast?lat=39.04&lon=-77.48&state=VA"

The 8 endpoints we mapped to your Characterize feature set — every one
tested live with your key just now, all return 200 with full
Enterprise-tier data:

  /api/v1/site-forecast        composite + 2050 forecast (headline tool)
  /api/v1/grid-intelligence    reserve margin + queue depth (fills slide 25)
  /api/v1/grid/data            raw ISO load timeseries (Pro/Enterprise only)
  /api/v1/fiber/intel          per-facility carrier intel (Pro/Enterprise only)
  /api/v1/fiber/routes         route geometry, 20+ carriers
  /api/v1/energy/retail        EIA state-level retail electricity rates
  /api/v1/energy/renewable     renewable capacity + PPA market depth
  /api/v1/water/stress         live USGS water-stress readings
  /api/v1/infrastructure       HIFLD substations (79K+) + FEMA hazard index
  /api/v1/tax-incentives       50-state DC tax abatements + program detail

Spec + tooling:

  OpenAPI:      https://dchub.cloud/openapi.json
  MCP server:   https://dchub.cloud/mcp
                (drop into Claude / Cursor / your agent config —
                 server card at /.well-known/mcp/server-card.json)

On the login: the API key works independently of the web dashboard.
You don't need a dashboard login to call any endpoint — just the
X-API-Key header. If you want the dashboard too (usage stats, key
rotation), /signup with this same email and the system will recognize
you. Happy to walk through that on a call.

Welcome Galen / Ian — happy to mint each of you a separate Enterprise
key under the same NLR partnership so usage tracks per-integrator (and
the DOE co-marketing references can attribute correctly). Reply with
your emails when ready.

On the DOE co-marketing front: as discussed, happy to coordinate
publication protocol per our MOU. Whatever framing works for NREL/DOE
on the joint methods note ("Improving Data Center Siting Models with
Live Infrastructure Data") — I'll defer to your team's preference on
authorship order, journal/venue, and announcement cadence.

Reading the data first sounds right — once you and the team have spent
some time, the call is open whenever. No agenda from my side beyond
what you surface.

Best,
Jonathan
Founder, DC Hub
azmartone@gmail.com · dchub.cloud
EMAIL

echo
echo "════════════════════════════════════════════════════════════════"
echo "Audit trail: /api/v1/admin/partner-key/audit"
echo "Kill switch: POST /api/v1/admin/partner-key/revoke/$KEY_PREFIX"
echo "════════════════════════════════════════════════════════════════"
