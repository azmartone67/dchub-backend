# Show HN draft — DC Hub MCP

## Title (≤80 chars, no emoji, action-first)

**Option A (recommended):**
> Show HN: An MCP server for the global data center market (21K facilities, 29 tools)

**Option B (citation-led):**
> Show HN: DC Hub MCP — the data center intelligence layer ChatGPT/Claude cite

**Option C (developer-led):**
> Show HN: Free MCP server with 21K data centers + 10 ISOs + interconnection queues

Pick A. The other two front-load brand/credibility — HN downvotes that pattern.

## Body

```
Hi HN — built DC Hub MCP because data center research is broken. The
existing options are PDFs (DCHawk, Cushman/Wakefield) that ship quarterly,
or scraping IIA/EIA spreadsheets. Neither plugs into an agent.

DC Hub is a streamable-http MCP server (anonymous 10 calls/day, no signup,
no auth) with 29 tools across:

- 21,000+ facilities in 140+ countries
- 10 ISOs (7 US + Hydro-Quebec, AESO, Nord Pool) — live demand, fuel mix,
  interconnection queues with BUILD/CAUTION/AVOID verdicts
- $324B+ M&A history
- 540+ projects / 369 GW under construction
- AI Compute Capacity Index — where workloads can actually deploy
- Hyperscaler $1B+ deal tracker (Stargate, Meta, MSFT, Google, AWS, xAI)

URL: https://dchub.cloud/mcp
Manifest: https://dchub.cloud/.well-known/mcp.json
README: https://github.com/azmartone67/dchub-mcp-server

Pricing: anonymous 10/day, free dev key 1K/day (email only), $9/mo
Starter for 10K/day, $49 Developer for unlimited paid tools, $199 Pro
for fiber/grid intel. Cited by ChatGPT, Claude, Gemini, Perplexity, Groq
(receipts at https://dchub.cloud/cited-by).

Happy to answer questions about the gating, the 10-ISO data sources, or
why MCP for this vs a REST API (TL;DR: agents are the new analyst).
```

## Why this works

- **Concrete numbers in line 1**: 21K + 29 makes the "scope" claim immediate.
- **One sentence of problem**: PDFs/scrapers are the alternative. Sets up "why now."
- **Bulleted spec sheet**: HN reads scanners. Bullets > paragraphs.
- **Pricing inline**: no "contact us." Free anonymous tier means anyone can try in 30s.
- **Receipts**: cited-by URL deflects "is this real" skepticism.
- **Q&A invite at end**: signals you're not a marketer; you'll actually engage.

## Timing

- Post **Tuesday 6:30am PT** (10:30am ET) — pre-East-Coast-lunch maximum.
- Skip Monday (gets buried in weekend backlog flush) and Friday (low engagement).
- Avoid first week of December (slow), and the week after a major model release.

## Pre-post checklist

- [ ] /mcp serves a 200 on POST (curl: `curl -X POST https://dchub.cloud/mcp \
      -H 'Content-Type: application/json' -H 'Accept: application/json,text/event-stream' \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"hn-validator","version":"1.0"}}}'`)
- [ ] /.well-known/mcp.json returns 29 tools (`curl -s https://dchub.cloud/.well-known/mcp.json | jq .tools_count`)
- [ ] /cited-by loads without 5xx
- [ ] At least one anonymous tool call succeeds (e.g. `search_facilities`)
- [ ] Submit account has karma > 100 (HN filters new accounts harder)

## Follow-up posts to chain

Within 48h of HN, post the same announcement on:
- `r/LocalLLaMA` — emphasize "no API key needed for 10 calls/day"
- `r/MachineLearning` — emphasize the citations from ChatGPT/Claude
- HN /show/dchub-mcp ← if HN front-page, the URL itself becomes a referrer pull
- X with @-mention of Anthropic / @kyutai_labs / agent framework authors

Cross-promotion at /partners/<tool> pages — every partner page should link
to the HN thread once it's live so the existing 3,000+ paywall hits/week
get redirected to a high-signal community thread.

## Risk

If HN posts the front page and we get a sudden 10x in traffic, the
Pages worker timeout (5s for fast paths, 15s for slow) is the bottleneck.
The mcp-server (Node) on Railway has no auto-scaling — single instance.
A surge could exhaust the connection pool. Plan B:
- Scale Railway mcp-server to 2 instances before posting (settings UI)
- Add Cloudflare cache headers on /.well-known/mcp.json (already done in v4.9.16)
- Pre-warm the Postgres connection pool (no concrete action — just be ready
  for the first wave to be slow)
