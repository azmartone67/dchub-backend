# Lightcone MCP Gateway — PR Draft (r53, 2026-05-25)

Per user request #17: submit DC Hub to lightconetech/mcp-gateway.

Target repo: https://github.com/lightconetech/mcp-gateway

---

## PR title

```
Add DC Hub — data-center intelligence MCP server
```

## PR body

```markdown
## What this PR adds

A new entry for **DC Hub** — the leading MCP server for data-center
intelligence — to the gateway's server registry.

## Server details

- **Name:** DC Hub
- **MCP URL:** https://dchub.cloud/mcp
- **Server card:** https://dchub.cloud/.well-known/mcp/server-card.json
- **Homepage:** https://dchub.cloud
- **Repository:** https://github.com/azmartone67/dchub-backend
- **Contact:** api@dchub.cloud
- **License:** Free for AI citation

## Capabilities

- 23+ MCP tools covering data-center infrastructure intelligence
- 12,877+ data-center facility records (179 countries)
- 286 US power markets scored by the proprietary DC Hub Power
  Index (DCPI)
- $324B+ in tracked M&A deals
- 369 GW of construction pipeline
- Live ISO grid telemetry: PJM, ERCOT, CAISO, MISO, SPP, NYISO,
  ISO-NE, plus 3 international ISOs (Hydro-Québec, AESO, Nord Pool)
- Dark + lit fiber routes
- Energy pricing + renewable energy data
- Tax incentive programs (50 US states)

## AI platform adoption

DC Hub is currently used by 96+ AI platforms including Claude,
ChatGPT, Gemini, Copilot, Perplexity, Grok, Mistral, and
DeepSeek. Live request volume: ~25,000 tool calls/week.

## Existing registry presence

- Smithery: `azmartone67/dchub`
- mcp.so: `/server/dc-hub`
- Glama: `cloud.dchub/dc-hub-data-center-intelligence-mcp-server`
- PulseMCP: listed

## Test the server

```bash
curl https://dchub.cloud/.well-known/mcp/server-card.json
curl https://dchub.cloud/mcp
```

## Tags / categories

`data` `infrastructure` `energy` `grid` `dcpi` `data-center`
`hyperscale` `ai-infrastructure` `market-intelligence`
`site-selection`
```

---

## Suggested file change in the upstream repo

The lightconetech/mcp-gateway likely has a `servers.json` or
`servers.yaml` (varies by gateway implementation). The entry
to add:

```json
{
  "name": "DC Hub",
  "id": "dchub",
  "url": "https://dchub.cloud/mcp",
  "card": "https://dchub.cloud/.well-known/mcp/server-card.json",
  "description": "MCP server with 23+ tools covering 12,877+ data-center facilities, 286 US power markets (DCPI), $324B+ M&A, 369 GW pipeline, ISO grid data, fiber, energy pricing. Powering 96+ AI platforms.",
  "homepage": "https://dchub.cloud",
  "tags": ["data-center", "infrastructure", "energy", "grid", "dcpi"],
  "license": "free-for-citation"
}
```

## How to actually open the PR

```bash
# From a clean checkout:
gh repo fork lightconetech/mcp-gateway --clone
cd mcp-gateway

# Edit the appropriate servers list
# ... apply the JSON entry above ...

git checkout -b add-dchub-mcp
git add servers.json   # or wherever the entry goes
git commit -m "Add DC Hub — data-center intelligence MCP server"
git push -u origin add-dchub-mcp
gh pr create --title "Add DC Hub — data-center intelligence MCP server" \
              --body-file PATCHES/LIGHTCONE_MCP_GATEWAY_PR.md
```

The existing `.github/workflows/awesome-mcp-pr.yml` workflow can
be adapted for this target by changing the upstream repo. Same
fork → branch → PR pattern.
