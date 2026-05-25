# Funnel fixes — next steps for items 3 + 7

These are the two items from the round-40 funnel diagnosis that need
**design or external work**, not just a code drop. The shell command
landed items 1, 2, 4, 6, 8. Item 5 (Starter $9 tier) shipped in
round-39.

---

## Item 3 — Anonymous-with-fingerprint 100/day middle tier

**Goal:** capture the long-tail "I just want to try one query" user
who would otherwise bounce off the 10/day anonymous cap, without
forcing email collection.

**Why this isn't a one-line code change:**
- Need a stable fingerprint key on first request (browser fingerprint or
  signed-cookie HMAC of CF-Connecting-IP + UA + nonce — IP alone fails
  for shared NATs).
- Worker (`dchubapiproxy-v4.9.X.js`) needs new tier slot between `free`
  (10/day) and `developer` (1000/day): suggest `anon_plus` at 100/day.
- Conversion path: at 80/100 calls the worker injects a "save your
  progress — get a free email key (1000/day)" nudge. Same path as
  current 429 nudge, just earlier.

**Acceptance:** anonymous user makes 11th call, gets the 100/day tier
silently. Hits 80, sees the email upgrade nudge. Hits 100, blocked
with the 429 nudge.

---

## Item 7 — Agent runtime registry submissions + demo agents

**Goal:** sign DC Hub up to every place an AI agent looks for MCP
servers. Distribution is zero-cost; you already have all the surfaces
(`/.well-known/mcp.json`, `/.well-known/agent.json`, `/mcp/manifest`).

**External submissions (~30 min each):**
- [ ] **Smithery** — https://smithery.ai/server/new — Submit dchub.cloud/mcp.
- [ ] **Glama** — `.well-known/glama.json` already correct. Submit at
      https://glama.ai/mcp/servers/new.
- [ ] **MCP Servers list (Anthropic docs)** — PR to
      https://github.com/modelcontextprotocol/servers — add a one-liner
      for DC Hub.
- [ ] **Cline marketplace** — PR to
      https://github.com/cline/mcp-marketplace.
- [ ] **Continue.dev hub** — submit at https://hub.continue.dev.
- [ ] **agent.ai** — list as available tool source.

**Demo agents to ship publicly (~1 day each):**
- [ ] **"Find 50 MW in MISO" agent on agent.ai** — single-purpose
      site-finder agent that pre-uses DC Hub. Outputs branded.
- [ ] **"AI Capacity Index daily" Twitter/LinkedIn bot** — posts the
      `/ai-capacity-index/today.json` value at 9am UTC. Auto-cites
      DC Hub. Cheap, high-leverage.
- [ ] **VS Code extension** — wraps DC Hub MCP as a Cursor-style
      command pallete. Submit to VS Code marketplace.

**One-click "Connect DC Hub" buttons:**
- [ ] PR to Claude Desktop docs adding a "Add DC Hub" deeplink button
- [ ] PR to Cursor MCP onboarding docs
- [ ] PR to Cline MCP marketplace doc

---

## What's landed (recap)

- **Item 1** (`migrations/2026-05-25_funnel_instrumentation.sql`) —
  event_type column + activation funnel view + conversion summary view.
- **Item 2** (`routes/onboarding_page.py`) — HTML onboarding page at
  `/onboard/<code>` with prefilled config + test button.
- **Item 4** (`routes/seo_agent_alternates.py`) — JSON alternate-link
  injector for SEO pages, registered via `register_alternate_hook(app)`.
- **Item 5** — Starter $9 tier card on `pricing.html` (round-39).
- **Item 6a** (`routes/hyperscaler_rss.py`) — RSS feed of $1B+ deals.
- **Item 6b** (`routes/ai_capacity_daily.py`) — Daily citable index
  with JSON / plaintext / SVG badge endpoints.
- **Item 8** (same migration as 1) — `referrer` and `user_agent`
  columns on `mcp_call_log` + `v_paywall_attribution` view that buckets
  by source (Claude / ChatGPT / Perplexity / Cursor / Cline / Browser).

## main.py wire-up needed (5 lines)

```python
from routes.onboarding_page    import onboarding_bp;       app.register_blueprint(onboarding_bp)
from routes.hyperscaler_rss    import hyperscaler_rss_bp;  app.register_blueprint(hyperscaler_rss_bp)
from routes.ai_capacity_daily  import ai_capacity_daily_bp; app.register_blueprint(ai_capacity_daily_bp)
from routes.seo_agent_alternates import register_alternate_hook
register_alternate_hook(app)
```

Then apply the SQL migration:
```bash
railway run psql "$NEON_DATABASE_URL" -f migrations/2026-05-25_funnel_instrumentation.sql
```
