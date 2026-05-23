# PR target: punkpeye/awesome-mcp-servers#6727 (or new PR)

## Where to paste

The list is grouped by category. DC Hub fits under **🏢 Industry Verticals**
or **🌐 Data & Search**. Paste the entry alphabetically by server name.

## Entry (markdown, paste verbatim into the README)

```markdown
- [DC Hub Intelligence](https://dchub.cloud/mcp) - Real-time data center intelligence platform. 20,000+ facilities, 50+ tools covering M&A transactions, capacity pipeline, energy/grid analytics, site scoring across 285 markets and 7 ISOs.  🌐 ☁️
```

## PR description boilerplate

```text
Title: feat: add DC Hub Intelligence MCP server

Body:
DC Hub is a hosted Streamable-HTTP MCP server giving AI agents live
data-center intelligence: 20,000+ facilities, 975+ M&A transactions,
real-time ISO/RTO grid data, and site-suitability scoring for any US
lat/long.

Production endpoint: https://dchub.cloud/mcp
Discovery manifest: https://dchub.cloud/api/v1/mcp/manifest
Public homepage:   https://dchub.cloud
Source repo:       https://github.com/azmartone67/dchub-backend (Apache-2.0)

Free tier requires no auth (rate-limited). Paid tier unlocks deep
queries — typed key passed via X-API-Key header.

Tested clients: Claude Desktop, Claude Code, Cursor, ChatGPT (custom
GPTs), Continue, Codeium, n8n.
```

## Emoji legend used (from the awesome list's own conventions)

- 🌐 → web-based / hosted service
- ☁️ → cloud / SaaS first-party
- 📊 → data & analytics (alternative slot)
- 🔌 → integrations (alternative slot)

DC Hub is hosted + cloud + analytics → 🌐 ☁️ is the most accurate pair.

## After PR merge

Update [README.md](README.md) `State` column to `Live`.
