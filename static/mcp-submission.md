# DC Hub Nexus - MCP Server Submission

## Server Information

**Name:** DC Hub Nexus  
**Version:** 2.0.0  
**Category:** Data & Research  
**Website:** https://dchub.cloud  
**MCP Endpoint:** https://dchub.cloud/.well-known/mcp.json  

## Description

DC Hub Nexus is the definitive data center intelligence platform, providing comprehensive access to:
- **10,000+ data center facilities** worldwide with location, specifications, and operator details
- **700+ M&A transactions** with deal values, buyers, sellers, and dates
- **250+ GW capacity pipeline** tracking new developments and expansions
- **40+ infrastructure layers** including power substations, fiber routes, and water resources
- **Real-time news** from 60+ industry sources

## Use Cases

1. **Site Selection**: Analysts can query infrastructure data to evaluate potential data center locations
2. **Market Research**: Access M&A trends, capacity pipeline, and market statistics
3. **Due Diligence**: Search facilities by operator, location, or specifications
4. **Competitive Analysis**: Track operator expansions and new market entries
5. **Infrastructure Planning**: Analyze power, fiber, and water availability

## Tools Provided

### 1. search_facilities
Search data center facilities by location, provider, or query.
```json
{
  "name": "search_facilities",
  "description": "Search 10,000+ data center facilities",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "Search query"},
      "limit": {"type": "integer", "default": 20}
    },
    "required": ["query"]
  }
}
```

### 2. get_market_stats
Get platform statistics and market overview.
```json
{
  "name": "get_market_stats",
  "description": "Get data center market statistics",
  "inputSchema": {
    "type": "object",
    "properties": {}
  }
}
```

### 3. get_news
Get latest data center industry news.
```json
{
  "name": "get_news",
  "description": "Get latest news from 60+ sources",
  "inputSchema": {
    "type": "object",
    "properties": {
      "limit": {"type": "integer", "default": 20}
    }
  }
}
```

### 4. get_deals
Get M&A transactions and deals.
```json
{
  "name": "get_deals",
  "description": "Get 700+ M&A transactions",
  "inputSchema": {
    "type": "object",
    "properties": {
      "limit": {"type": "integer", "default": 20}
    }
  }
}
```

### 5. analyze_site
Analyze infrastructure for a location.
```json
{
  "name": "analyze_site",
  "description": "Analyze power, fiber, water infrastructure",
  "inputSchema": {
    "type": "object",
    "properties": {
      "location": {"type": "string", "description": "City or coordinates"}
    },
    "required": ["location"]
  }
}
```

### 6. get_providers
Get data center operators and providers.
```json
{
  "name": "get_providers",
  "description": "Get operator rankings and details",
  "inputSchema": {
    "type": "object",
    "properties": {
      "limit": {"type": "integer", "default": 50}
    }
  }
}
```

### 7. get_infrastructure
Get infrastructure layer data.
```json
{
  "name": "get_infrastructure",
  "description": "Get fiber routes, substations, permits",
  "inputSchema": {
    "type": "object",
    "properties": {
      "layer": {"type": "string", "enum": ["fiber", "substations", "permits", "properties"]}
    },
    "required": ["layer"]
  }
}
```

### 8. get_water_drought_status
Get water and drought analysis for locations.
```json
{
  "name": "get_water_drought_status",
  "description": "Analyze water availability and drought risk",
  "inputSchema": {
    "type": "object",
    "properties": {
      "location": {"type": "string"}
    },
    "required": ["location"]
  }
}
```

## Data Sources

- **PeeringDB** - Interconnection and peering data
- **OpenStreetMap** - Facility geolocation
- **Wikidata** - Entity enrichment
- **SEC EDGAR** - REIT financial filings
- **FCC** - Broadband and fiber data
- **HIFLD** - Power infrastructure
- **USGS** - Water resources
- **NOAA** - Drought monitoring

## Authentication

No authentication required for read-only access to public data.

## Rate Limits

- 100 requests per minute
- 10,000 requests per day

## Contact

- **Email:** api@dchub.cloud
- **Documentation:** https://dchub.cloud/api-docs
- **GitHub:** https://github.com/dchub-nexus

## Installation for Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dc-hub": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-remote", "https://dchub.cloud/mcp"]
    }
  }
}
```

Or use the HTTP endpoint directly:
```json
{
  "mcpServers": {
    "dc-hub": {
      "url": "https://dchub.cloud/mcp"
    }
  }
}
```
