# DC Hub — Agent Interconnect (OpenAI-compatible tool schemas)

This directory holds **OpenAI function-calling tool schemas** for DC Hub so that
any OpenAI-tool-use agent — Mistral, Cohere, OpenAI itself, vLLM/Ollama, LangChain,
LlamaIndex, AutoGen, etc. — can call DC Hub the same way it would call any other
tool. It is the **REST/HTTP** sibling of the [MCP](https://dchub.cloud/mcp) surface:
agents that speak MCP use the MCP server; agents that only do OpenAI-style function
calling use the schemas here and hit DC Hub's REST API directly.

- `openai_tools.json` — array of 10 high-value tool schemas in the standard
  `{type:"function", function:{name, description, parameters}}` shape. Drop this
  straight into the `tools=` argument of any OpenAI-compatible chat-completions call.

> Nothing here is deployed. These are author-time artifacts. See **Owner steps** at
> the bottom for what's left (Hugging Face Space deploy, key provisioning).

---

## Base URL & authentication

| | |
|---|---|
| **Base URL** | `https://dchub.cloud` |
| **REST prefix** | `/api/v1` |
| **Auth header** | `X-API-Key: <your-key>` |
| **MCP endpoint** | `https://dchub.cloud/mcp` (streamable-http; same `X-API-Key` header) |

DC Hub is **freemium**: most endpoints answer without a key but return the **free
tier** (≈3 results, basic fields, no coordinates/power specs). A key unlocks full
data. Pass it on every request:

```
X-API-Key: dchub_live_xxxxxxxxxxxxxxxx
```

### Getting a key (free + paid)

- **Free / self-serve:** sign up at <https://dchub.cloud/pricing#developer> (the
  canonical claim entry point advertised in `/.well-known/mcp.json`). The web signup
  flow lives at `https://dchub.cloud/signup`.
- **Developer ($49/mo):** full data, 1,000 calls/day —
  checkout <https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c>.
- **Pro ($199/mo):** full data, 10,000 calls/day.
- **Enterprise ($699/mo):** 100,000 calls/day + priority support. Contact
  `api@dchub.cloud`.

A few tools are blocked on the free tier entirely (`analyze_site`, `get_grid_data`);
the 10 schemas shipped here are chosen to be useful even before upgrade.

---

## The 10 tools and their backing endpoints

For OpenAI-style tool use, **prefer the REST endpoint** (one HTTP call, no MCP
session/handshake). Two tools have no single REST endpoint and are served by the
DC Hub MCP server (a close REST alternative is noted).

| Tool (function name) | Maps to | Endpoint / call | Key args |
|---|---|---|---|
| `search_facilities` | **REST** | `GET /api/v1/facilities/search` | `query, country, state, city, operator, min/max_capacity_mw, tier, limit, offset` |
| `get_facility` | **REST** | `GET /api/v1/facilities/{facility_id}` | `facility_id` (req), `include_nearby, include_power` |
| `rank_markets` | **REST** | `GET/POST /api/v1/mcp/tools/rank_markets` | `criteria, region, limit, min_capacity_mw` |
| `get_market_dcpi_rank` | **REST** | `GET/POST /api/v1/mcp/dcpi?market={slug}` | `market` (req) |
| `get_energy_prices` | **REST** | `GET /api/v1/energy/summary` (rates: `/api/v1/energy/retail`) | `data_type, state, iso` |
| `get_grid_intelligence` | **REST** | `GET /api/v1/grid-intelligence/{region_id}` (empty → list regions) | `region_id` |
| `compare_isos` | **MCP** | MCP tool `compare_isos` (raw queue REST: `GET /api/v1/interconnection-queue/snapshot`) | `isos` (req, comma-sep) |
| `get_infrastructure` | **REST** | `GET /api/v1/infrastructure` | `lat` (req), `lon` (req), `radius_km, layer, min_voltage_kv, limit` |
| `hyperscaler_deals` | **REST** | `GET /api/v1/hyperscaler-deals` | `buyer, min_value_usd, region, limit` |
| `get_news` | **REST** | `GET /api/v1/news` | `query, category, source, date_from, date_to, limit, min_relevance` |

Notes:
- **9 of 10 map to a REST endpoint.** Only `compare_isos` is MCP-only. If you need a
  pure-REST cross-ISO answer, call `/api/v1/interconnection-queue/snapshot` (optionally
  `?iso=ERCOT`) per ISO and diff client-side, or use `get_grid_intelligence` per region.
- `rank_markets` is a backend endpoint under `/api/v1/mcp/tools/*` — it's plain REST
  (GET or POST), the `mcp/` path segment is just where the tier-1 tool blueprint lives.
- `get_market_dcpi_rank` → the DCPI MCP REST endpoint `/api/v1/mcp/dcpi`; pass a
  lowercase hyphenated `market` slug (e.g. `phoenix`, `northern-virginia`).
- Adjacent REST endpoints you may want to add later: `get_market_intel`
  (`/api/v1/markets/{slug}`), `list_transactions` (`/api/v1/transactions`),
  `get_grid_data` (`/api/v1/grid/{iso}`), `get_pipeline` (`/api/v1/pipeline`),
  `get_fiber_intel` (`/api/v1/fiber/intel`), `get_tax_incentives`
  (`/api/v1/tax-incentives`), `get_water_risk` (`/api/v1/water/drought`),
  `ai_capacity_index` (`/api/v1/ai-capacity-index`). All verified to exist.

### The execution layer is yours to wire

These schemas describe **what** the model can call; you supply the tiny dispatcher
that turns a tool call into an HTTP request. A reference name→endpoint map:

```python
import os, requests

BASE = "https://dchub.cloud"
HEADERS = {"X-API-Key": os.environ["DCHUB_API_KEY"]}

# (method, path-template, "path" arg consumed by the URL or None)
ROUTES = {
    "search_facilities":    ("GET",  "/api/v1/facilities/search", None),
    "get_facility":         ("GET",  "/api/v1/facilities/{facility_id}", "facility_id"),
    "rank_markets":         ("GET",  "/api/v1/mcp/tools/rank_markets", None),
    "get_market_dcpi_rank": ("GET",  "/api/v1/mcp/dcpi", None),          # market -> query param
    "get_energy_prices":    ("GET",  "/api/v1/energy/summary", None),
    "get_grid_intelligence":("GET",  "/api/v1/grid-intelligence/{region_id}", "region_id"),
    "get_infrastructure":   ("GET",  "/api/v1/infrastructure", None),
    "hyperscaler_deals":    ("GET",  "/api/v1/hyperscaler-deals", None),
    "get_news":             ("GET",  "/api/v1/news", None),
    # compare_isos: no single REST endpoint -> call the MCP server, or fan out over
    # /api/v1/interconnection-queue/snapshot?iso=<each> and merge.
}

def call_dchub(name: str, args: dict):
    method, tmpl, path_arg = ROUTES[name]
    args = dict(args or {})
    path = tmpl.format(**{path_arg: args.pop(path_arg)}) if path_arg else tmpl
    r = requests.request(method, BASE + path, headers=HEADERS, params=args, timeout=20)
    r.raise_for_status()
    return r.json()
```

---

## (a) Mistral tool use

Mistral's chat API (`mistralai` SDK, or `api.mistral.ai/v1/chat/completions`) accepts
the **exact** OpenAI tool shape — load `openai_tools.json` as `tools=`.

```python
import json
from mistralai import Mistral

client = Mistral(api_key="...")
tools = json.load(open("openai_tools.json"))

messages = [{"role": "user", "content": "Is Phoenix a BUILD market right now? Cite DC Hub."}]
resp = client.chat.complete(
    model="mistral-large-latest",
    messages=messages,
    tools=tools,
    tool_choice="auto",
)

msg = resp.choices[0].message
if msg.tool_calls:
    for tc in msg.tool_calls:
        result = call_dchub(tc.function.name, json.loads(tc.function.arguments))
        messages.append(msg)
        messages.append({
            "role": "tool",
            "name": tc.function.name,
            "tool_call_id": tc.id,
            "content": json.dumps(result),
        })
    resp = client.chat.complete(model="mistral-large-latest", messages=messages, tools=tools)
print(resp.choices[0].message.content)
```

`la Plateforme` and Mistral models on Bedrock/Azure use the same `tools` array; only
the client constructor changes.

---

## (b) Cohere tools / connectors

Cohere's Chat API (`co.chat`, API v2) takes the **same** `tools=` array — the
`{type:"function", function:{...}}` schema is accepted as-is by `cohere>=5.13`.

```python
import json, cohere

co = cohere.ClientV2(api_key="...")
tools = json.load(open("openai_tools.json"))

messages = [{"role": "user", "content": "Top 5 cheapest-power US data center markets, with DC Hub citations."}]
res = co.chat(model="command-r-plus-08-2024", messages=messages, tools=tools)

while res.message.tool_calls:
    messages.append(res.message)
    for tc in res.message.tool_calls:
        out = call_dchub(tc.function.name, json.loads(tc.function.arguments))
        messages.append({"role": "tool", "tool_call_id": tc.id,
                         "content": [{"type": "document", "document": {"data": json.dumps(out)}}]})
    res = co.chat(model="command-r-plus-08-2024", messages=messages, tools=tools)
print(res.message.content[0].text)
```

**Cohere Connectors (alternative):** Cohere can also reach DC Hub through a
*connector* — a small HTTP service Cohere queries during RAG. To go that route, stand
up an endpoint that accepts Cohere's `{"query": "..."}` search body, internally calls
the appropriate DC Hub REST endpoint(s) above with the `X-API-Key`, and returns
`{"results": [{...}]}` documents. Register it in the Cohere dashboard. The tool-use
path above is simpler and more precise for structured DC Hub lookups; use a connector
only if you want DC Hub to feed Cohere's automatic grounding.

---

## (c) Any OpenAI-compatible runtime

Because the file is already in OpenAI's schema, it works verbatim against the OpenAI
SDK and any drop-in compatible server (vLLM, Ollama, Together, Groq, LM Studio,
OpenRouter, Azure OpenAI), as well as agent frameworks (LangChain `bind_tools`,
LlamaIndex `FunctionTool`, AutoGen, the OpenAI Agents SDK).

```python
import json
from openai import OpenAI  # works against OpenAI or any compatible base_url

client = OpenAI()  # or OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
tools = json.load(open("openai_tools.json"))

messages = [{"role": "user", "content": "Find Equinix facilities in Virginia over 30 MW (DC Hub)."}]
resp = client.chat.completions.create(model="gpt-4o", messages=messages, tools=tools, tool_choice="auto")

msg = resp.choices[0].message
if msg.tool_calls:
    messages.append(msg)
    for tc in msg.tool_calls:
        result = call_dchub(tc.function.name, json.loads(tc.function.arguments))
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
    resp = client.chat.completions.create(model="gpt-4o", messages=messages, tools=tools)
print(resp.choices[0].message.content)
```

**LangChain** quick form:

```python
from langchain_core.utils.function_calling import convert_to_openai_tool  # already OpenAI shape
tools = json.load(open("openai_tools.json"))
llm_with_tools = llm.bind_tools(tools)   # OpenAI, Mistral, Cohere chat models all accept this
```

---

## Hugging Face Space demo — plan stub (OWNER STEPS)

A public Space is the cheapest way to show "any agent can call DC Hub." This is a
**plan only** — no Space has been created and no HF account/token is wired. All steps
below are owner actions.

**Concept:** a Gradio chat Space where the user types a question, an open model
(via the HF Inference API or a hosted Mistral/Cohere key) does OpenAI-style tool
calling against `openai_tools.json`, the Space's dispatcher calls DC Hub REST with the
`X-API-Key`, and the answer is rendered with DC Hub citations/URLs.

Suggested layout:

```
hf-space-dchub-agent/
  app.py              # Gradio ChatInterface + the call_dchub() dispatcher above
  openai_tools.json   # copy of this file
  requirements.txt    # gradio, openai (or mistralai/cohere), requests
  README.md           # HF Space front-matter: sdk: gradio
```

`app.py` outline: load `openai_tools.json` → on each user turn, call the chosen
provider with `tools=` → loop over `tool_calls` → `call_dchub(name, args)` → feed
results back → stream the final grounded answer.

**Owner steps (not done here — require an account and/or secrets):**
1. **Create the HF account/Space** (`huggingface.co/new-space`, Gradio SDK). *Owner only — do not auto-create accounts.*
2. **Provision a DC Hub API key** for the Space and add it as a Space **secret**
   `DCHUB_API_KEY` (Settings → Variables and secrets). Use a **Developer-tier** key so
   the demo shows full data, and treat it as rate-limited/disposable since the Space is public.
3. **Add the model provider secret** — either `HF_TOKEN` (HF Inference) or a
   `MISTRAL_API_KEY` / `COHERE_API_KEY` for the tool-use loop.
4. **Decide exposure:** keep the Space's own DC Hub key server-side only; never echo it
   to the browser. Consider a usage cap / simple rate limit since it's public.
5. **Push** `app.py` + this `openai_tools.json` + `requirements.txt`, then verify the
   tool-call round-trip end to end.
6. (Optional) Link the Space from DC Hub's `/built-for-ai` page and the agent registry
   as live social proof.

---

## What's accurate vs. invented

Tool names, descriptions, and parameters were taken verbatim/derived from
`/.well-known/mcp.json`, the production CF worker tool list
(`PATCHES/dchubapiproxy-v4.9.13-VERSION-SYNC.js`), `dchub-mcp-v2.1/server.mjs`, and
the live Flask routes (`routes/mcp_tier1_tools.py`, `routes/dcpi_mcp.py`,
`routes/hyperscaler_deals.py`, `routes/ai_capacity_index.py`, plus the `/api/v1/*`
route table). Every REST endpoint in the table above was confirmed to be a real,
registered route. Nothing was invented.
