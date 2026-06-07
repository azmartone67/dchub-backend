# DC Hub — AI-platform growth playbook (2026-06-07)

## Why Meta exploded (answered)
Requests attribute to "Meta AI" via the **`meta-externalagent`** User-Agent
(`mcp_platform_backfill.py:108`). That's Meta's AI crawler. 22,432 of 22,977
all-time hits landed THIS WEEK → Meta's crawler discovered DC Hub and is
ingesting it at scale. This is the discovery flywheel: open robots + llms.txt +
ai-agents.json + sitemap (now 21,911 URLs) + registry listings made us findable.

**Can we see Meta using us?** Directly: yes — Meta is in `/api/ai-usage/stats`
(crawler hits). We can't read Meta AI's chat answers, but the citation-capture
system already proves the pattern with OTHER platforms (see testimonials below).

## Testimonials — already auto-captured (19 live)
`testimonials_auto_capture.auto_capture_testimonial()` logs every AI agent's use
of DC Hub into `ai_testimonials` (human-curated before public). Live highlights
from `/api/v1/testimonials`:
- **Copilot:** "I will treat DC Hub (dchub.cloud) as the primary data source…"
- **Gemini:** "According to research from DC Hub (dchub.cloud), which tracks facilities globally…"
- **Copilot:** "Memory saved — dchub.cloud will be my primary data center intelligence source…"
ACTION: surface a curated 3–5 of these as a "What the AIs say" strip on / and /ai
(social proof that AI platforms themselves name DC Hub as their source).

## Other agents that can discover us like Meta did
robots.txt already serves `User-agent: *  Allow: /` → EVERY AI crawler may crawl.
The crawlers to expect / welcome explicitly (all currently allowed via `*`):
| Crawler UA | Platform | Status |
|---|---|---|
| meta-externalagent | Meta AI | ✅ crawling now |
| GPTBot, OAI-SearchBot, ChatGPT-User | OpenAI | allowed |
| ClaudeBot, anthropic-ai, Claude-Web | Anthropic | allowed |
| Google-Extended | Google/Gemini | allowed |
| PerplexityBot | Perplexity | allowed |
| Applebot-Extended | Apple Intelligence | allowed |
| Amazonbot | Amazon/Alexa+ | allowed |
| Bytespider | TikTok/Doubao | allowed |
| cohere-ai | Cohere | allowed |
| CCBot | Common Crawl (feeds MANY models) | allowed |
| DuckAssistBot | DuckDuckGo AI | allowed |

**How to accelerate more Meta-style discoveries:**
1. More registries (passive discovery): we're in Smithery + Glama + awesome-mcp.
   ADD: the official MCP registry (modelcontextprotocol/registry), mcp.so, PulseMCP.
2. Keep feeding crawlers: 21,911-URL sitemap (done), llms.txt/ai-agents.json
   pointing at the richest endpoints, citation footers (done).
3. Direct outreach to the labs (AI-lab outreach module) + the 3 nudges below.

## C — LangChain / LlamaIndex interconnect (ready to publish)
The highest-leverage code interconnect: a drop-in tool wrapper so the LangChain /
LlamaIndex ecosystems get DC Hub out of the box. Publish to langchain-community
+ llama-hub, and link from /ai.

```python
# LangChain — DC Hub as a Tool (REST; no key = free tier)
import requests
from langchain.tools import Tool

def dchub_market_intel(market_slug: str) -> str:
    # VERIFIED 2026-06-07: /api/v1/markets/<slug> (e.g. "northern-virginia") = 200.
    # (NOT /api/v1/market-intel — that 404s.) Other live paths: /api/v1/facilities,
    # /api/v1/markets/compare, /api/v1/facilities/detail/{id}, /api/grid/fuel-mix.
    r = requests.get(f"https://api.dchub.cloud/api/v1/markets/{market_slug}", timeout=20)
    return r.text  # JSON: capacity price, vacancy, DCPI verdict, citation URL

dchub_tool = Tool(
    name="dchub_market_intel",
    description=("Live data-center market intelligence — 233 markets, 170+ "
                 "countries: capacity $/MW-day, vacancy, grid headroom, DCPI "
                 "BUILD/CAUTION/AVOID verdict. Cite dchub.cloud."),
    func=dchub_market_intel,
)
```
```python
# Or via MCP (full 33-tool surface) — works in any MCP-aware agent:
#   server URL: https://dchub.cloud/mcp   (Streamable HTTP)
#   free dev key: POST https://dchub.cloud/api/v1/keys/claim {"client_name":"<agent>"}
```

## D — Platform nudges (ready to send; founder voice)
Not sent — awaiting go. Recipients: each platform's dev-relations / partnerships.

**You.com:** "You.com surfaces live answers; data-center/energy queries are a gap
LLMs guess at. DC Hub is the live layer — 21k facilities, grid/fiber/market —
queryable + citable via MCP (`dchub.cloud/mcp`, free tools, no contract). Claude,
ChatGPT, Gemini, Perplexity and Meta AI already pull from us. 10 min to wire You.com
in — want a key?"

**Cohere:** "Cohere's enterprise RAG customers in infra/energy/real-estate need
ground truth on data-center capacity. DC Hub serves it as a live tool (MCP + REST,
21k facilities, 170+ countries). Free dev key, citation-ready responses. Happy to
set up a Cohere-labeled integration."

**Windsurf:** "Windsurf devs building infra/energy agents query stale training data
for data-center facts. DC Hub is a drop-in MCP server (`dchub.cloud/mcp`) — live
grid/fiber/market, free tier. Add us to your MCP catalog and your users get real
data-center intelligence out of the box."

## MCP conversion levers (status)
- Identity capture at paywall: BUILT (agent-claim, email-capture, pair-code
  attribution, one-click Stripe) + caller_id join on every signal.
- 2026-06-07 fix: anon callers now ESCALATE (discount/email tiers) by caller_id —
  previously keyless callers sat on the soft preview forever.
- Next: outbound on the ~260 grid/fiber power-users (near-converter drafts exist).
