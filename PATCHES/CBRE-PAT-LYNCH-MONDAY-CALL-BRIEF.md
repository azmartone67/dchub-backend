# CBRE × DC Hub Alliance — Pat Lynch Call Brief

*Monday call · alliance creation*

## The 30-second open

> "Pat — DC Hub's MCP server just crossed 389,383 AI-agent requests since
> launch, with Claude/ChatGPT/Gemini citing us 96K+ times in 30 days.
> CBRE's H2 report is the most-cited DC research in the industry but it
> ships 6 months stale by November. I want to find the version of an
> alliance where CBRE keeps its analytical authority and DC Hub provides
> the daily refresh layer — no channel conflict, transparent attribution.
> Worth 30 minutes to scope?"

## The numbers to drop (memorize 3)

- **389,383** lifetime AI-agent requests (defensible — full platform
  attribution: Direct 142K, MCP 103K, Claude 54K, ChatGPT 26K, Gemini 16K)
- **1.55M/year** annualized run-rate from 7-day window
- **110,236** verified citations in last 30 days (CC-BY-4.0, public)
- **286 markets** scored daily by DCPI (vs CBRE's tier-1 focus)
- **$324B+** in tracked M&A across the global DC asset class
- **10 ISOs** covered (7 US + Hydro-Québec + AESO + Nord Pool)

## The three asks (rank-ordered, pick what fits)

### Ask 1 (lowest friction): co-authored H2 2026 report
DC Hub provides the live-data layer + methodology appendix. CBRE provides
the analytical narrative + distribution. Both attributed. **Revenue: $0
upfront, 50/50 license fee on enterprise data resales.**

### Ask 2 (medium): DC Hub data feeds into CBRE's internal DCPI equivalent
CBRE buys a $75K-$250K/yr Pro or Strategic license. Custom DCPI weights
matched to CBRE's investor screening lens. Quarterly briefings with
your DCPI team. **Revenue: $75K-$250K ARR.**

### Ask 3 (highest): formal Switzerland alliance
Joint announcement. Logo on each other's surfaces. DC Hub as "official
data partner" for CBRE DCS publications. Revenue share on inbound deals
where DC Hub data sourced the lead. **Revenue: variable — best case
$500K-$1M ARR if pipeline shares meaningfully.**

## What Pat probably worries about

- **"Doesn't this undercut our research authority?"**
  > No — we're the underlying data primitive (like Refinitiv to Bloomberg
  > analysts). Your narrative + brokerage relationships are the moat.
  > We make those faster and more defensible. CBRE's H2 stays the
  > publication of record; we just make sure its data isn't 6 months stale.

- **"Channel conflict with our brokers?"**
  > Switzerland model — we don't broker deals. We license data. Same model
  > as Capital IQ / S&P. Brokers love us because we never compete with them.

- **"Compliance / legal review?"**
  > CC-BY-4.0 license on the public layer means no NDA needed for
  > evaluation. Commercial tier is a standard SaaS contract — happy to
  > work with your procurement team's template.

- **"Why now / why us first?"**
  > You're my colleague at CBRE DCS — easiest path. But the other 8
  > broker / advisory firms in our public Switzerland framework
  > (https://dchub.cloud/news/partnership-cbre-2026-w22) are getting the
  > same invitation. First-mover gets co-branded research distribution.

## The press release that already exists

Published 2026-05-26: *"DC Hub Publishes Open Partnership Framework —
Switzerland Model for the Data Center Intelligence Layer"*

→ https://dchub.cloud/news/partnership-cbre-2026-w22

Names CBRE alongside JLL, Cushman, Newmark, DCD, DCByte, DataCenterHawk,
Structure Research, Synergy. Public invitation — no closed deal language.
Pat can read it cold and see the framework on offer.

## What to walk out of the call with

- [ ] Pat agrees to schedule a 30-min follow-up with one CBRE DCS analyst
      to do data-quality cross-check on 10 sample markets
- [ ] Pat introduces DC Hub to whoever owns the H2 2026 report on the
      CBRE side
- [ ] DC Hub sends a one-paragraph email + the framework press release
      URL to Pat's CBRE distribution list for awareness

## Live demo URLs (if Pat wants to see it)

- DCPI live: https://dchub.cloud/dcpi
- International DCPI: https://dchub.cloud/dcpi/intl
- Enterprise pricing: https://dchub.cloud/enterprise
- AI citation receipts: https://dchub.cloud/api/v1/agents/citations.json
- Live press headline metric: https://dchub.cloud/api/v1/mcp/funnel

## After the call (Monday EOD)

If green light → POST `/api/v1/enterprise/inquiry` with Pat's name and
"strategic-pilot" tier so the inquiry pipeline tracks it from day 1.

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"name":"Pat Lynch","email":"<pat@cbre.com>","firm":"CBRE DCS",
       "tier":"strategic","use_case":"broker_partnership",
       "notes":"Monday call — alliance scoping, follow-up scheduled."}' \
  "https://dchub.cloud/api/v1/enterprise/inquiry"
```
