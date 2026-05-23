# Issue to open: anthropic/anthropic-cookbook or modelcontextprotocol/servers

The Anthropic-maintained example list lives at:
https://github.com/modelcontextprotocol/servers (community list, well-trafficked).

## Title

```
Add DC Hub Intelligence (data center / energy market intelligence) to community servers
```

## Body

```markdown
DC Hub provides a hosted Streamable-HTTP MCP server for real-time data
center intelligence. Free tier requires no setup.

**Endpoint:** https://dchub.cloud/mcp
**Manifest:** https://dchub.cloud/mcp/manifest
**Tools:** 50+ covering facility search, M&A transactions, ISO grid data,
site scoring, and capacity pipeline tracking.

**Why this is useful for the community list:**
- Hosted (no install / npm step) — agents can connect immediately
- Real-world data set (20,000+ facilities, 975+ deals, 7 US ISOs)
- Tested with Claude Desktop, Claude Code, Cursor, ChatGPT, Continue
- Open API + manifest discovery — fits the MCP discovery convention
- Free tier exists; paid tier ($12/mo) for deeper queries

**Suggested README entry:**

`- **[DC Hub Intelligence](https://dchub.cloud/mcp)** — Data center
intelligence: 20K+ facilities, M&A tracking, grid analytics, site
scoring. (Hosted, Streamable HTTP)`

Happy to submit a PR with the README edit if preferred — open this
issue first so the maintainers can confirm placement and category.
```

## After issue accepted

If they greenlight, open the PR adding the entry to README.md under the
appropriate category. Mention this issue in the PR body.
