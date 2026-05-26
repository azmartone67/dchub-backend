#!/usr/bin/env bash
# r59-b (2026-05-25) — one-shot press_releases DB insert for the
# "DCPI goes global" announcement.
#
# The /press page reads from the press_releases Postgres table via
# /api/press-releases. Static /press/releases/dcpi-international.html
# is the canonical content but doesn't appear on /press until this
# row exists.
#
# Run once:
#   chmod +x scripts/r59_press_dcpi_international.sh
#   DCHUB_ADMIN_KEY=<your-key> ./scripts/r59_press_dcpi_international.sh
#
# Idempotent — the endpoint uses ON CONFLICT (slug) DO UPDATE.

set -euo pipefail

if [ -z "${DCHUB_ADMIN_KEY:-}" ]; then
  echo "ERROR: DCHUB_ADMIN_KEY env var required."
  echo "Run: DCHUB_ADMIN_KEY=<key> $0"
  exit 1
fi

BASE="${DCHUB_API_BASE:-https://dchub-backend-production.up.railway.app}"

read -r -d '' BODY <<'JSON' || true
{
  "title":      "DC Hub Power Index Goes Global — 16 New International Markets Across UK, EU, Japan, Australia, Singapore, and Canada",
  "slug":       "dcpi-international-expansion",
  "category":   "Product Release",
  "date":       "2026-05-25",
  "subheadline":"The first daily-refreshing public scorecard of data center power availability now spans 9 countries.",
  "meta_description": "DC Hub today expanded the Data Center Power Index (DCPI) with 16 new international markets across 9 countries — daily-refreshing, free to cite, MCP- and API-accessible.",
  "body": "<p><strong>NEW YORK — May 25, 2026</strong> — DC Hub today announced an international expansion of the <strong>Data Center Power Index (DCPI)</strong>, adding 16 new markets across nine countries: the United Kingdom (London, Manchester), Ireland (Dublin), Germany (Frankfurt), the Netherlands (Amsterdam), France (Paris, Marseille), Sweden (Stockholm), Japan (Tokyo, Osaka), Australia (Sydney, Melbourne), Singapore, and Canada (Toronto, Montréal, Vancouver). Every market receives the same two-axis treatment as the existing 280+ U.S. markets — daily-refreshing Excess Power and Constraint scores with full methodology disclosure.</p><p>The international set is not generic. Each market's defaults are calibrated from the underlying ISO's published outlook documents: ENTSO-E Winter Outlook 2024 for European markets, AEMO ESOO 2024 for Australia, NGESO ETYS 2024 for the UK, EirGrid Generation Capacity Statement for Ireland, IESO Annual Planning Outlook for Ontario, METI/OCCTO 2024 for Japan, and EMA Singapore statistics post-data-center moratorium easing.</p><blockquote>\"London has a 144-month interconnection queue. Dublin's grid operator effectively imposed a data-center moratorium. Montréal sits on 1.5 GW of stranded Hydro-Québec capacity. Until today, none of those facts were anywhere on a single, daily-refreshing scorecard — and nobody else was going to build it because the brokerages don't have fee revenue in any of them.\" — Jonathan Martone, Founder, DC Hub</blockquote><p>The international launch surfaces the same kind of overlooked-market opportunities the U.S. index has called out since launch. Montréal (Excess Power 62, BUILD) leads the international set on a combination of Hydro-Québec's stated 5 GW of available capacity, an 18-month interconnection queue (vs. London's 144-month), and a 70% queue-approval rate. Stockholm (Excess Power 58, BUILD) benefits from chronic Nordic hydro surplus, low curtailment, and a 26% reserve margin. At the other extreme, London's combination of a 7% reserve margin, 10% queue-approval rate, and 11% annual demand growth produces a low Excess Power score and AVOID verdict.</p><p>The 16 new markets are queryable through the same API surface as the U.S. set: <code>GET /api/v1/dcpi/scores?iso=NGESO</code> filters by grid operator; <code>GET /api/v1/dcpi/scores/london</code> deep-dives one market; MCP tool <code>compare_isos</code> provides natural-language access via Claude, ChatGPT, and Cursor. The full historical dataset for every market — U.S. and international — is downloadable as open CSV at <a href=\"https://dchub.cloud/data/dcpi-history.csv\">dchub.cloud/data/dcpi-history.csv</a>.</p><p>Full release: <a href=\"https://dchub.cloud/press/releases/dcpi-international.html\">dchub.cloud/press/releases/dcpi-international.html</a></p>",
  "published":  true
}
JSON

echo "→ POSTing to $BASE/api/admin/press-releases"
RES=$(curl -sS -X POST "$BASE/api/admin/press-releases" \
  -H "Authorization: Bearer $DCHUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data "$BODY")
echo "$RES" | python3 -m json.tool || echo "$RES"

# Verify it now shows up on the public feed
echo
echo "→ Verifying on /api/press-releases…"
curl -s "$BASE/api/press-releases" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  intl = [r for r in d if 'international' in (r.get('slug') or '')]
  if intl:
    print('✅ on press feed:', intl[0].get('title')[:90])
  else:
    print('⚠️  not on press feed yet (may need a moment).')
except Exception as e:
  print('parse failed:', e)
"
