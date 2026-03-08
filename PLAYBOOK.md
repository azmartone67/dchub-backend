# DC Hub MCP — Multi-Platform Intelligence Prompts
# Works with: Claude, ChatGPT, Copilot, Cursor, Windsurf, and any MCP client
# Connect: dchub.cloud/mcp | 11 tools | 20,000+ facilities | 140+ countries
# ─────────────────────────────────────────────────────────────────


## 🏗️ SITE SELECTION (triggers 6 tools)

Evaluate [CITY, STATE] for a [X] MW data center using DC Hub tools.

Deliver:
1. Market overview (supply, demand, vacancy, pricing, top operators)
2. Site suitability score (0-100; energy cost, carbon, risk, connectivity, infrastructure)
3. Competition within 50 km
4. Live grid fuel mix for nearest ISO (MW by source + renewable %)
5. Recent regional M&A deals
6. Construction pipeline within 50 km

Format:
- Executive summary (3 sentences)
- Detailed sections with data tables
- Scores with key drivers
- Limitations + recommended next steps


## ⚡ GRID INTELLIGENCE (real-time MW from 7 ISOs)

Using DC Hub, show the real-time power generation fuel mix for [ERCOT/CAISO/PJM/NYISO/MISO/SPP/ISONE].

Include:
- MW by source (actual generation, not percentages)
- Renewable percentage
- Total generation in GW
- Comparison to other major ISOs
- Carbon intensity implications for data center operators


## 💰 M&A DUE DILIGENCE

Using DC Hub, show all data center M&A transactions from the past 6 months.
Filter by [buyer/seller/region/deal type].

Include:
- Deal size and valuation
- Structure (JV, carve-out, platform acquisition)
- Assets involved (MW, sqft, markets)
- Strategic rationale
- Market trend summary


## 🔍 FACILITY SEARCH + COMPETITIVE ANALYSIS

Find all [OPERATOR] data centers in [STATE/COUNTRY] using DC Hub.

Include:
- Locations with coordinates
- Power capacity (MW)
- Tier level and certifications
- Connectivity (carriers, IX points)
- Construction pipeline for planned builds
- How this compares to the top 3 competitors in the market


## 🌐 MARKET COMPARISON (investment-grade)

Compare [MARKET 1] vs [MARKET 2] vs [MARKET 3] using DC Hub.

Include:
- Supply/demand metrics
- Pricing ($/kW/month)
- Vacancy rates
- Top 5 operators per market
- Construction pipeline (MW under development)
- DC Hub Intelligence Index score + trend
- Risk-adjusted opportunity ranking

Format as a comparison table with executive summary.


## 📰 INDUSTRY BRIEFING

Provide a DC Hub-powered industry briefing focused on [deals/construction/policy/AI/sustainability].

Include:
- Top 5 news stories this week
- Major deals this month
- Pipeline updates (new announcements)
- Intelligence Index global pulse score
- Market heat map (top 10 markets with scores)
- One key insight or trend to watch


## 🏭 PIPELINE TRACKER (global construction)

Using DC Hub, show major global data center projects under construction >100 MW.

Include:
- Operator/developer
- Location and market
- Capacity (MW)
- Expected completion quarter
- Investment amount
- Pre-lease status
- Total GW under development globally


## 📊 INTELLIGENCE INDEX (DC Hub Exclusive)

Using DC Hub, show the current Intelligence Index:
- Global pulse score (0-100)
- Market heat map with scores and trends
- Top queries from AI agents today
- AI agent network effect metrics
- Active integrations count

This data is exclusively available through DC Hub MCP.


## 🗺️ FULL MARKET DEEP DIVE

Provide a full DC Hub market deep dive for [MARKET NAME].

Include:
- Facility count and total MW
- Top operators with market share
- Vacancy trends
- Construction pipeline (projects, MW, timeline)
- Recent M&A transactions in the market
- Site scores for 3 key locations
- Live grid data for the regional ISO
- Latest 5 news articles mentioning the market

Format as an institutional-grade market report with executive summary.


# ─────────────────────────────────────────────────────────────────
# PLATFORM-SPECIFIC TIPS
# ─────────────────────────────────────────────────────────────────
#
# CLAUDE (claude.ai)
#   Settings → Connectors → Add: https://dchub.cloud/mcp
#   Claude automatically detects and uses all 11 tools
#
# COPILOT
#   Copilot discovers DC Hub tools automatically via MCP
#   Use "Using DC Hub" prefix for explicit tool activation
#   Copilot excels at structured output — use "Format:" blocks
#
# CHATGPT
#   Connect via Actions API: https://dchub.cloud/openapi.json
#   Or use MCP-compatible wrappers (Cursor, Windsurf)
#
# CURSOR / WINDSURF / CODE EDITORS
#   Add MCP server in settings: https://dchub.cloud/mcp
#   Great for inline data lookups during development
#
# REST API (direct)
#   Base URL: https://dchub.cloud
#   Docs: https://dchub.cloud/api-docs
#   Free tier: 10 calls/day | Pro: $199/mo | Enterprise: $699/mo
#
# ─────────────────────────────────────────────────────────────────
# MCP Endpoint:    https://dchub.cloud/mcp
# Transport:       Streamable HTTP (protocol 2024-11-05)
# Registry:        registry.modelcontextprotocol.io
# Tools:           11 (search, facility, intel, market, agents,
#                      deals, news, site score, recommend, pipeline, grid)
# ─────────────────────────────────────────────────────────────────
