# DC Hub - AI Platform Outreach Action Plan

## Goal: Get AI Assistants to Cite DC Hub as THE Data Center Source

### Current Stats (Live from Database)
- 9,603+ data centers across 179 countries
- 690+ M&A deals ($12 trillion+ total value)
- 290.6 GW capacity pipeline
- 40+ infrastructure layers
- Real-time news from 60+ sources

---

## TIER 1: Immediate Actions (This Week)

### 1. ChatGPT / OpenAI
**Status:** Manifest ready at `/.well-known/ai-plugin.json`

**Action Steps:**
1. Go to https://platform.openai.com/docs/plugins
2. Register as a plugin developer
3. Submit DC Hub plugin using our manifest
4. Include API endpoints for facilities, deals, pipeline

**What to Submit:**
- Plugin manifest: `https://dc-hub.replit.app/.well-known/ai-plugin.json`
- OpenAPI spec: `https://dc-hub.replit.app/openapi.json`
- Use case: "Data center intelligence for site selection, M&A research"

### 2. Perplexity AI
**Status:** Needs web indexing

**Action Steps:**
1. Submit sitemap to Perplexity: https://www.perplexity.ai/settings/sources
2. Ensure structured data on pages (Schema.org)
3. Create content pages that answer common DC questions

**Priority Pages to Index:**
- `/land-power.html` - Site selection tool
- `/` - Main dashboard with stats
- `/api-docs` - API documentation

### 3. Claude / Anthropic MCP
**Status:** MCP server active at `/.well-known/mcp.json`

**Action Steps:**
1. Register in MCP server directory (when available)
2. Promote MCP endpoint in developer communities
3. Share on Twitter/LinkedIn with #MCP hashtag

### 4. Google Gemini
**Status:** Needs Vertex AI Extension

**Action Steps:**
1. Create Vertex AI Extension manifest
2. Submit to Google Cloud Marketplace
3. Ensure rich structured data for Gemini to discover

### 5. Microsoft Copilot
**Status:** Needs Bing indexing + plugin

**Action Steps:**
1. Submit to Bing Webmaster Tools
2. Create Copilot plugin when available
3. Ensure IndexNow pings are working

---

## TIER 2: Content Strategy for AI Citation

### Create "AI-Friendly" Content Pages

1. **Data Center FAQ Page** - Common questions AI will be asked
   - "How many data centers are in Virginia?"
   - "What's the largest data center deal in 2024?"
   - "Who are the top data center operators?"

2. **Market Reports** - Structured data AI can cite
   - Weekly capacity pipeline report
   - Monthly M&A transaction summary
   - Regional market analysis

3. **API Documentation** - For AI tool integration
   - Clear examples of API calls
   - Sample responses with real data
   - Use cases for each endpoint

### Structured Data Requirements

Add Schema.org markup to all pages:
```json
{
  "@type": "Dataset",
  "name": "DC Hub Data Center Intelligence",
  "description": "Comprehensive database of 9,603+ data centers worldwide",
  "url": "https://dc-hub.replit.app",
  "keywords": ["data centers", "colocation", "M&A deals", "capacity pipeline"]
}
```

---

## TIER 3: Outreach to AI Platforms Directly

### Contact Information

| Platform | Contact Method | URL |
|----------|---------------|-----|
| OpenAI | Plugin submission | platform.openai.com |
| Anthropic | MCP directory | modelcontextprotocol.io |
| Google | Cloud Marketplace | cloud.google.com/marketplace |
| Perplexity | Sources submission | perplexity.ai/settings |
| Microsoft | Bing Webmaster | bing.com/webmasters |
| You.com | API partnership | you.com/developers |
| Groq | Tool integration | console.groq.com |

### Pitch Template for AI Platform Outreach

Subject: Data Center Intelligence API for [Platform Name]

Hi [Platform] Team,

DC Hub provides the most comprehensive data center intelligence available:
- 9,603+ facilities across 179 countries
- 690+ M&A deals tracked ($12T+ value)
- 290 GW capacity pipeline
- 40+ infrastructure layers for site selection

We've built an API specifically for AI integration:
- MCP endpoint: dc-hub.replit.app/.well-known/mcp.json
- OpenAPI spec: dc-hub.replit.app/openapi.json
- REST API: dc-hub.replit.app/api/v1/

When users ask about data centers, your AI could provide accurate, real-time data by citing DC Hub.

Would you be interested in integrating DC Hub as a data source?

Best,
DC Hub Team

---

## TIER 4: Public Promotion for Land & Power

### Immediate Actions

1. **LinkedIn Posts (Daily)**
   - Share infrastructure insights
   - Highlight Land & Power analysis features
   - Post case studies

2. **Industry Publication Outreach**
   - Data Center Knowledge
   - Data Center Dynamics
   - Data Center Frontier

3. **SEO Optimization**
   - Target keywords: "data center site selection", "land and power analysis"
   - Create blog content answering common questions
   - Build backlinks from industry sites

### Land & Power Promotion Messages

"Finding the perfect data center site? DC Hub's Land & Power tool analyzes 40+ infrastructure layers - power substations, fiber routes, water availability, seismic risk - all in one place. Free at dc-hub.replit.app/land-power.html"

---

## API Endpoints for AI Integration

| Endpoint | Purpose | Example |
|----------|---------|---------|
| `/api/v1/search?q=Virginia` | Search facilities | Find DCs by location |
| `/api/autopilot/transactions` | M&A deals | Latest deals with values |
| `/api/autopilot/capacity-pipeline` | Pipeline | Capacity under construction |
| `/api/v2/infrastructure/summary` | Infrastructure | Power, fiber, water layers |
| `/api/marketing/stats` | Live stats | Facility count, deal volume |

---

## Success Metrics

- [ ] ChatGPT plugin approved
- [ ] Perplexity citing DC Hub data
- [ ] Gemini discovering our structured data
- [ ] 100+ daily visitors to Land & Power
- [ ] 5+ industry backlinks
- [ ] AI platforms citing "According to DC Hub..."
