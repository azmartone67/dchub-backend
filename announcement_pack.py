# DC Hub MCP Tier Gating Launch — Announcement Pack
# ===================================================
# Ready-to-post content for LinkedIn + MCP directories
# March 22, 2026


# ═══════════════════════════════════════════════════
# 1. LINKEDIN POST (Primary — Jonathan's profile)
# ═══════════════════════════════════════════════════

LINKEDIN_POST = """
DC Hub's MCP server just got a major upgrade.

If you're using AI agents for data center site selection, market analysis, or infrastructure research — you can now connect DC Hub directly to Claude, ChatGPT, Cursor, Windsurf, or any MCP-compatible tool.

What's new:

→ 15 tools, all live. Search 11,000+ facilities, score any site for data center suitability, pull M&A deal data, get real-time infrastructure intel (substations, transmission, gas pipelines, fiber), energy pricing, and more.

→ Free tier for everyone. 10 calls/day, 5 results per query with basic fields. Enough to evaluate, not enough to build a spreadsheet.

→ Developer tier ($49/mo). Full data — coordinates, power capacity, connectivity specs, detailed site scoring, fiber intelligence — 1,000 calls/day. Built for brokers, consultants, and developers building on DC Hub data.

→ Works inside your AI workflow. One line to connect:

claude mcp add dchub --transport http https://dchub.cloud/mcp

Or paste this into your MCP config:
{
  "mcpServers": {
    "dchub": {
      "serverUrl": "https://dchub.cloud/mcp"
    }
  }
}

This is data center intelligence delivered as a protocol, not a dashboard. Ask your AI agent "score Phoenix for a 100MW data center" and get an answer backed by HIFLD, EIA, FEMA, EPA, and USGS data — instantly.

Published on the Official MCP Registry. Listed on Glama, PulseMCP, and growing.

Try it free: dchub.cloud/connect
Developer plan: dchub.cloud/pricing

#DataCenters #MCP #AI #SiteSelection #Infrastructure #DataCenterIntelligence
"""


# ═══════════════════════════════════════════════════
# 2. LINKEDIN POST — SHORT VERSION (engagement bait)
# ═══════════════════════════════════════════════════

LINKEDIN_SHORT = """
Your AI agent can now search 11,000+ data centers, score sites, and pull infrastructure data — in one line:

claude mcp add dchub --transport http https://dchub.cloud/mcp

15 tools. Free tier. $49/mo for full data.

DC Hub is the only data center intelligence platform on MCP.

dchub.cloud/connect

#MCP #DataCenters #AI
"""


# ═══════════════════════════════════════════════════
# 3. GLAMA UPDATE (glama.ai listing)
# ═══════════════════════════════════════════════════

GLAMA_UPDATE = """
DC Hub MCP Server — v2.0 Update

15 tools now live (was 11):
• search_facilities — 11,000+ global data centers
• get_facility — Full specs, power, connectivity
• list_transactions — $185B+ M&A deal database
• get_market_intel — Vacancy, pricing, inventory across 44 markets
• get_news — 40+ sources, AI-categorized
• analyze_site — Composite scoring (power/fiber/gas/risk/carbon)
• get_intelligence_index — Real-time market pulse
• get_pipeline — 540+ projects, 369GW tracked
• get_infrastructure — Substations, transmission, gas, power plants (213K+ records)
• get_fiber_intel — 1,069 routes, 13 carriers, 19 metros
• get_energy_prices — EIA retail rates, 50 states
• get_renewable_energy — Solar/wind PPAs, 7GW tracked
• get_tax_incentives — 50 states
• get_water_risk — USGS stress data + cooling recommendations
• compare_sites — Side-by-side location scoring

New: Tiered access
• Free: 10 calls/day, 5 results, basic fields
• Developer ($49/mo): 1,000 calls/day, full data
• Pro/Enterprise: Custom limits

Endpoint: https://dchub.cloud/mcp
Transport: streamable-http
"""


# ═══════════════════════════════════════════════════
# 4. PULSEMCP / MCP.SO / AWESOME-MCP-SERVERS
# ═══════════════════════════════════════════════════

DIRECTORY_DESCRIPTION = """
DC Hub — Data Center Intelligence for AI Agents

The only MCP server for data center infrastructure intelligence. 15 tools covering facility search (11K+ global), M&A transactions ($185B+), site scoring, energy pricing, power infrastructure (79K substations, 56K transmission lines, 50K gas pipelines), fiber connectivity (1,069 routes), renewable energy PPAs, tax incentives, water risk, and real-time market intelligence.

Free tier: 10 calls/day, 5 results per query
Developer: $49/mo, 1,000 calls/day, full data

Endpoint: https://dchub.cloud/mcp
Transport: streamable-http
Registry: cloud.dchub/mcp-server (Official MCP Registry)
"""


# ═══════════════════════════════════════════════════
# 5. AWESOME-MCP-SERVERS PR DESCRIPTION
# ═══════════════════════════════════════════════════

AWESOME_MCP_PR = """
### DC Hub — Data Center Intelligence

- **URL**: https://dchub.cloud/mcp
- **Transport**: streamable-http
- **Tools**: 15 (facility search, site scoring, M&A deals, infrastructure, energy, fiber, news)
- **Data**: 11,000+ facilities, 213K+ infrastructure records, $185B+ M&A, 50 states energy/tax data
- **Free tier**: 10 calls/day
- **Category**: Data & Analytics / Infrastructure / Real Estate

One-line install:
```
claude mcp add dchub --transport http https://dchub.cloud/mcp
```
"""


# ═══════════════════════════════════════════════════
# 6. TWITTER/X POST (if applicable)
# ═══════════════════════════════════════════════════

TWITTER_POST = """
DC Hub is now on MCP. 15 tools. 11K+ data centers. Free tier.

Ask your AI agent to score a site for a 100MW data center — and get answers backed by EIA, FEMA, USGS, and HIFLD data.

One line to connect:
claude mcp add dchub --transport http https://dchub.cloud/mcp

dchub.cloud/connect
"""


# ═══════════════════════════════════════════════════
# 7. EMAIL TO MCP DIRECTORY MAINTAINERS
# ═══════════════════════════════════════════════════

DIRECTORY_EMAIL = """
Subject: DC Hub MCP Server — v2.0 with 15 tools + tiered access

Hi,

DC Hub (dchub.cloud) just shipped a major update to our MCP server:

• 15 tools (up from 11) covering data center facility search, site scoring, M&A deals, infrastructure mapping, energy pricing, fiber intelligence, and more
• Tiered access: Free (10 calls/day), Developer ($49/mo, 1K/day), Pro/Enterprise
• 213,000+ infrastructure data points (substations, transmission lines, gas pipelines, power plants)
• Published on the Official MCP Registry as cloud.dchub/mcp-server

Endpoint: https://dchub.cloud/mcp
Connect page: https://dchub.cloud/connect
Transport: streamable-http

We're the only data center intelligence platform available via MCP. Would appreciate an updated listing if possible.

Best,
Jonathan Martone
Founder, DC Hub
jonathan@dchub.cloud
"""
