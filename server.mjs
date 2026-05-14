/**
 * DC Hub MCP Server v2.1.0
 * ────────────────────────────────────────────────────────────────────────────
 * Patches v2.0.0:
 *   1. Per-tool-call telemetry (POST /api/v1/mcp/track)
 *   2. X-API-Key validation against backend (POST /api/v1/keys/validate) +
 *      forwarding to internal API calls
 *   3. Free / paid / enterprise tier gates with upgrade nudges
 *   4. Platform detection from User-Agent (Claude/ChatGPT/Cursor/etc.)
 *   5. AsyncLocalStorage so callAPI() and tool handlers see the active
 *      session's api_key / platform / tier without threading params through
 *
 * Backwards-compatible: clients without an X-API-Key still connect, but get
 * a 'free' tier with capped result sizes and an upgrade nudge in responses.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import express from 'express';
import { randomUUID } from 'crypto';
import { AsyncLocalStorage } from 'async_hooks';
import { z } from 'zod';

// ── Config ──────────────────────────────────────────────────────────────────
const API_BASE      = process.env.DCHUB_API_BASE      || 'https://dchub-backend-production.up.railway.app';
const INTERNAL_KEY  = process.env.DCHUB_INTERNAL_KEY  || 'dchub-internal-sync-2026';
const PORT          = parseInt(process.env.PORT || '3100', 10);
const UPGRADE_URL   = process.env.DCHUB_UPGRADE_URL   || 'https://dchub.cloud/ai#pricing';
const SIGNUP_URL    = process.env.DCHUB_SIGNUP_URL    || 'https://dchub.cloud/ai';
// Phase 276/281: optional Stripe Payment Link for one-click upgrades from
// inside the MCP paywall. If unset, the messages fall back to the existing
// UPGRADE_URL (pricing page) — no functional change.
//
// Phase 281: the upgrade target is the Developer tier ($49/mo, "For AI Agent
// Builders" per the pricing page), NOT Pro ($199/mo). DCHUB_STRIPE_PRO_LINK
// still accepted as a back-compat alias.
const STRIPE_DEVELOPER_LINK = (
  process.env.DCHUB_STRIPE_DEVELOPER_LINK ||
  process.env.DCHUB_STRIPE_PRO_LINK ||  // phase 276 legacy name
  ''
).trim();
const KEY_CACHE_TTL = parseInt(process.env.DCHUB_KEY_CACHE_TTL_MS || '300000', 10); // 5 min

// Phase 276/281: emit a "one-click upgrade" markdown line if a Stripe Payment
// Link is configured. Empty string when unconfigured so existing messages
// degrade cleanly.
function oneClickUpgradeLine() {
  if (!STRIPE_DEVELOPER_LINK) return '';
  return `\u{26A1} **One-click upgrade to Developer ($49/mo, 1,000 calls/day):** [${STRIPE_DEVELOPER_LINK}](${STRIPE_DEVELOPER_LINK})\n\n`;
}

// ── Per-request context (api_key, platform, tier, session_id) ───────────────
const ctx = new AsyncLocalStorage();
const getCtx = () => ctx.getStore() || {};

// ── Platform detection (User-Agent → canonical platform name) ───────────────
function detectPlatform(ua = '') {
  const u = (ua || '').toLowerCase();
  if (u.includes('claude'))      return 'claude';
  if (u.includes('chatgpt') || u.includes('openai-mcp')) return 'chatgpt';
  if (u.includes('copilot'))     return 'copilot';
  if (u.includes('cursor'))      return 'cursor';
  if (u.includes('gemini'))      return 'gemini';
  if (u.includes('perplexity'))  return 'perplexity';
  if (u.includes('grok'))        return 'grok';
  if (u.includes('deepseek'))    return 'deepseek';
  if (u.includes('codex'))       return 'codex';
  if (u.includes('glama'))       return 'glama';
  if (u.includes('windsurf'))    return 'windsurf';
  if (u.includes('cohere'))      return 'cohere';
  if (u.includes('meta'))        return 'meta';
  if (u.includes('you'))         return 'you';
  if (u.includes('curl') || u.includes('postman')) return 'curl';
  return 'mcp';
}

// ── Telemetry: POST every tool invocation to the backend ───────────────────
async function trackToolCall(payload) {
  try {
    await fetch(new URL('/api/v1/mcp/track', API_BASE).toString(), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Internal-Key': INTERNAL_KEY,
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(5000),
    });
  } catch (err) {
    console.error('[track] failed:', err.message);
  }
}

// ── Key validation (cached) ────────────────────────────────────────────────
const keyCache = new Map(); // api_key → { valid, tier, exp }
async function validateKey(api_key) {
  if (!api_key) return { valid: false, tier: 'free' };
  const hit = keyCache.get(api_key);
  if (hit && hit.exp > Date.now()) return hit;
  try {
    const resp = await fetch(new URL('/api/v1/keys/validate', API_BASE).toString(), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Internal-Key': INTERNAL_KEY,
      },
      body: JSON.stringify({ api_key }),
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) return cacheKey(api_key, { valid: false, tier: 'free' });
    const data = await resp.json();
    return cacheKey(api_key, {
      valid: !!data.valid,
      tier: data.tier || 'free',
      developer_id: data.developer_id || null,
      email: data.email || null,
    });
  } catch (err) {
    console.error('[validateKey] failed:', err.message);
    return { valid: false, tier: 'free' };
  }
}
function cacheKey(api_key, result) {
  const v = { ...result, exp: Date.now() + KEY_CACHE_TTL };
  keyCache.set(api_key, v);
  return v;
}

// ── Backend API helper: forwards user's API key when present ───────────────
async function callAPI(path, params = {}) {
  const url = new URL(path, API_BASE);
  for (const [k, v] of Object.entries(params)) {
    if (v !== '' && v !== 0 && v !== false && v !== null && v !== undefined)
      url.searchParams.set(k, String(v));
  }
  const c = getCtx();
  const headers = {
    'X-Internal-Key': INTERNAL_KEY,
    'Accept': 'application/json',
  };
  if (c.api_key)  headers['X-API-Key']      = c.api_key;
  if (c.platform) headers['X-MCP-Platform'] = c.platform;
  if (c.session_id) headers['X-MCP-Session'] = c.session_id;
  try {
    const resp = await fetch(url.toString(), { headers, signal: AbortSignal.timeout(30000) });
    const text = await resp.text();
    if (!resp.ok) return { error: `API ${resp.status}`, detail: text.slice(0, 500) };
    try { return JSON.parse(text); } catch { return { raw: text.slice(0, 2000) }; }
  } catch (err) { return { error: err.message }; }
}

// ── POST variant: JSON body, forwards the user's API key like callAPI ──────
async function callAPIPost(path, body = {}) {
  const url = new URL(path, API_BASE);
  const c = getCtx();
  const headers = {
    'X-Internal-Key': INTERNAL_KEY,
    'Accept': 'application/json',
    'Content-Type': 'application/json',
  };
  if (c.api_key)  headers['X-API-Key']      = c.api_key;
  if (c.platform) headers['X-MCP-Platform'] = c.platform;
  if (c.session_id) headers['X-MCP-Session'] = c.session_id;
  try {
    const resp = await fetch(url.toString(), {
      method: 'POST', headers, body: JSON.stringify(body),
      signal: AbortSignal.timeout(15000),
    });
    const text = await resp.text();
    if (!resp.ok) return { error: `API ${resp.status}`, detail: text.slice(0, 500) };
    try { return JSON.parse(text); } catch { return { raw: text.slice(0, 2000) }; }
  } catch (err) { return { error: err.message }; }
}

// ── Free-tier limits and paid-only tools ───────────────────────────────────
const FREE_TIER_LIMITS = {
  search_facilities:  { max_limit: 25 },
  list_transactions:  { max_limit: 10 },
  get_pipeline:       { max_limit: 25 },
  get_news:           { max_limit: 20 },
  get_infrastructure: { max_limit: 25 },
};

// Phase 274: free-tier daily call caps on selected previously-paid-only tools.
// The funnel showed 99–101 distinct free users hitting these tools ~22 times
// each (clearly a real workflow). Hard-walling on call #1 was killing
// conversion: 0.012% paid-conversion rate over 8,167 paywall hits in 7d.
// Letting them complete small workflows (10/day each) creates upgrade pull
// from real-use frustration instead of from immediate denial. After cap,
// the existing paid_only paywall fires.
const FREE_TIER_DAILY_CAPS = {
  get_grid_intelligence: 10,
  get_fiber_intel:       10,
};

const PAID_ONLY_TOOLS = new Set([
  'analyze_site',
  'compare_sites',
  // 'get_grid_intelligence',  // phase 274: moved to FREE_TIER_DAILY_CAPS (10/day)
  'get_dchub_recommendation',
  // 'get_fiber_intel',         // phase 274: moved to FREE_TIER_DAILY_CAPS (10/day)
]);

// Phase 274: fetch today's call count for a given (api_key, tool) so we can
// enforce per-day caps. Fail-soft: any error → assume quota intact (count=0)
// so a transient backend blip doesn't break legitimate users.
async function usageToday(api_key, tool) {
  if (!api_key || !tool) return 0;
  try {
    const u = new URL('/api/v1/mcp/usage-today', API_BASE);
    u.searchParams.set('api_key', api_key);
    u.searchParams.set('tool', tool);
    const resp = await fetch(u.toString(), {
      headers: { 'X-Internal-Key': INTERNAL_KEY },
      signal: AbortSignal.timeout(3000),
    });
    if (!resp.ok) return 0;
    const data = await resp.json();
    return Number(data?.count) || 0;
  } catch (err) {
    console.error('[usageToday] failed:', err.message);
    return 0;
  }
}

// Returns { allowed, params, capped, dailyCap, dailyUsed } — async because
// the daily-cap branch needs a usage lookup.
async function applyTierGate(toolName, params, tier, api_key) {
  if (tier === 'paid' || tier === 'enterprise') return { allowed: true, params };
  if (PAID_ONLY_TOOLS.has(toolName)) return { allowed: false };

  // Daily cap check for free tier
  const dailyCap = FREE_TIER_DAILY_CAPS[toolName];
  if (dailyCap && tier === 'free') {
    const used = await usageToday(api_key, toolName);
    if (used >= dailyCap) {
      return { allowed: false, dailyCapExceeded: true, dailyCap, dailyUsed: used };
    }
  }

  const lim = FREE_TIER_LIMITS[toolName];
  if (lim && Number(params?.limit) > lim.max_limit) {
    return { allowed: true, params: { ...params, limit: lim.max_limit }, capped: lim.max_limit };
  }
  return { allowed: true, params };
}

// ── trackedTool: wrap each srv.tool registration ───────────────────────────
function trackedTool(srv, name, description, schema, handler) {
  srv.tool(name, description, schema, async (args) => {
    const c = getCtx();
    const t0 = Date.now();
    let status = 'ok';
    const tier = c.tier || 'free';
    try {
      const gate = await applyTierGate(name, args, tier, c.api_key);

      // Phase 274: daily-cap-exceeded gets its own friendlier message —
      // user has clearly been using the tool, so the paywall framing is
      // "you've earned this" not "you're locked out."
      if (!gate.allowed && gate.dailyCapExceeded) {
        status = 'blocked_daily_cap';
        const _quick = oneClickUpgradeLine();
        const _md = `## \u{23F1}\u{FE0F} Daily free-tier limit reached for \`${name}\`

You've used **${gate.dailyUsed}/${gate.dailyCap}** of today's free calls to \`${name}\`. Resets at **00:00 UTC**.

### You're clearly using this for real work. Two paths forward:

${_quick}1. **Wait it out.** Limit resets in a few hours.
2. **Upgrade now — 50% off first month with \`TRYDCHUB50\`.**

\u{1F449} **[Upgrade to Developer](${UPGRADE_URL})** — $49/mo, 1,000 calls/day, unlimited \`${name}\`, \`get_grid_intelligence\`, \`get_fiber_intel\`, plus the paid-only tools (\`analyze_site\`, \`compare_sites\`, \`get_dchub_recommendation\`).

If you're scoring more than ~10 markets/day, the math favors upgrading.`;
        return {
          content: [{ type: 'text', text: _md }],
          structuredContent: {
            error: 'daily_cap_exceeded',
            tool: name,
            current_tier: tier,
            daily_used: gate.dailyUsed,
            daily_cap: gate.dailyCap,
            resets_at: 'next 00:00 UTC',
            upgrade_url: UPGRADE_URL,
            ...(STRIPE_PRO_LINK ? { one_click_upgrade_url: STRIPE_PRO_LINK } : {}),
            discount_code: 'TRYDCHUB50',
          },
        };
      }

      if (!gate.allowed) {
        status = 'blocked_paid_only';
        // Markdown-formatted response — renders as real prose in Claude/Cursor/most MCP UIs.
        const _isKeyed = !!c.api_key;
        const _quick = oneClickUpgradeLine();  // phase 276
        const _mdKeyed = `## \u{1F512} \`${name}\` requires a paid plan

You're on **free tier** with a dev key — this tool is gated to **Developer** ($49/mo).

${_quick}### What Developer unlocks
- \`analyze_site\` — full power, fiber, risk, climate scoring for any location
- \`compare_sites\` — side-by-side comparison across markets
- \`get_dchub_recommendation\` — AI-formatted location recommendations
- **Unlimited** \`get_grid_intelligence\` + \`get_fiber_intel\` (free tier: 10/day each)
- 1,000 calls/day across the whole MCP surface (free tier: 100/day)

\u{1F449} **[Upgrade to Developer](${UPGRADE_URL})**

Free tier still covers: \`search_facilities\`, \`get_facility\`, \`list_transactions\`, \`get_news\`, \`get_market_intel\`, \`get_pipeline\`, \`get_grid_data\`, \`get_water_risk\`, plus **10/day** of \`get_grid_intelligence\` and \`get_fiber_intel\`.`;

        // Phase 277: anonymous AI agents get an immediate self-serve path.
        // /api/v1/keys/claim issues a free key in one curl, no email required —
        // the AI agent (or its human) can run the curl, parse the api_key from
        // the JSON, set it on the MCP client config, and retry. The old
        // /dev-signup (email-verified) is still listed below as the path
        // that lifts the per-IP rate limit.
        const _mdAnon = `## \u{1F512} \`${name}\` is a paid feature

### Get a free dev key in one curl — no email, no browser (AI-agent friendly)

\`\`\`bash
curl -s -X POST https://dchub.cloud/api/v1/keys/claim \\
  -H "Content-Type: application/json" \\
  -d '{"client_name":"my-agent","intended_use":"score build sites"}'
\`\`\`

Returns JSON with an \`api_key\` (\`dch_live_...\`) you drop into your MCP client config under \`X-API-Key\`. Rate-limited to 1 key per IP per 24h.

Free tier covers **100 calls/day** across:
- \`search_facilities\`, \`get_facility\`, \`list_transactions\`
- \`get_news\`, \`get_market_intel\`, \`get_pipeline\`
- \`get_grid_data\`, \`get_water_risk\`, \`get_renewable_energy\`, \`get_tax_incentives\`
- \`get_infrastructure\`, \`get_energy_prices\`, \`get_intelligence_index\`
- **10/day** of \`get_grid_intelligence\` and \`get_fiber_intel\`

### Or verify email for higher rate limits
\`\`\`bash
curl -X POST https://dchub.cloud/api/v1/dev-signup \\
  -H "Content-Type: application/json" \\
  -d '{"email":"YOUR_EMAIL"}'
\`\`\`

### Or skip straight to Developer
${_quick}\u{1F449} **[Upgrade to Developer](${UPGRADE_URL})** — $49/mo, 1,000 calls/day. Full result sizes + all paid tools: \`${name}\`, \`analyze_site\`, \`compare_sites\`, \`get_dchub_recommendation\`, and unlimited \`get_grid_intelligence\` + \`get_fiber_intel\`.`;
        return {
          content: [{ type: 'text', text: _isKeyed ? _mdKeyed : _mdAnon }],
          structuredContent: {
            error: 'paid_only',
            tool: name,
            current_tier: tier,
            upgrade_url: UPGRADE_URL,
            signup_url: _isKeyed ? null : SIGNUP_URL,
            // Phase 275 + 277: programmatic claim path for AI agents.
            ...(_isKeyed ? {} : { claim_key_url: 'https://dchub.cloud/api/v1/keys/claim',
                                  claim_key_method: 'POST' }),
            // Phase 276: one-click upgrade if configured.
            ...(STRIPE_PRO_LINK ? { one_click_upgrade_url: STRIPE_PRO_LINK } : {}),
          },
        };
      }

      const result = await handler(gate.params || args);

      if (gate.capped) {
        let parsed;
        try { parsed = JSON.parse(result.content?.[0]?.text || '{}'); } catch { parsed = {}; }
        const wrapped = {
          ...(typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : { data: parsed }),
          _upgrade_notice: {
            tier,
            message: `Free tier capped results at ${gate.capped}. Upgrade for full access.`,
            upgrade_url: UPGRADE_URL,
            signup_url: c.api_key ? null : SIGNUP_URL,
          },
        };
        return { content: [{ type: 'text', text: JSON.stringify(wrapped) }] };
      }

      return result;
    } catch (err) {
      status = 'error';
      throw err;
    } finally {
      // Fire-and-forget telemetry — never block the user response on it.
      // Phase FF++ (2026-05-12): include client_name + client_version from
      // the initialize handshake so flask_mcp_endpoints.py:390 has a real
      // vendor identifier to insert into mcp_tool_calls.client_name
      // (instead of falling back to the transport's session UUID).
      trackToolCall({
        timestamp:      new Date().toISOString(),
        tool:           name,
        params:         args,
        platform:       c.platform || 'unknown',
        api_key:        c.api_key || null,
        tier,
        session_id:     c.session_id || null,
        client_name:    c.client_name    || null,
        client_version: c.client_version || null,
        status,
        duration_ms: Date.now() - t0,
      }).catch(() => {});
    }
  });
}

// ── Tool registrations (20 tools, all wrapped) ─────────────────────────────
function createServer() {
  const srv = new McpServer({ name: 'DC Hub Intelligence', version: '2.1.0' });

  const S = z.string().optional();
  const N = z.number().optional();
  const I = z.number().int().optional();
  const B = z.boolean().optional();

  trackedTool(srv, 'search_facilities', 'Search 20,000+ global data center facilities.',
    { query: S, country: S, state: S, city: S, operator: S, min_capacity_mw: N, max_capacity_mw: N, tier: I, limit: I, offset: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/facilities', a)) }] }));

  trackedTool(srv, 'get_facility', 'Get detailed info about a specific facility.',
    { facility_id: S, include_nearby: B, include_power: B },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI(`/api/v1/facilities/${a.facility_id||''}`, { include_nearby: a.include_nearby, include_power: a.include_power })) }] }));

  trackedTool(srv, 'get_market_intel', 'Get market intelligence: supply/demand, pricing, vacancy.',
    { market: S, metric: S, period: S, compare_to: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/markets', a)) }] }));

  trackedTool(srv, 'get_intelligence_index', 'Real-time composite market health score.', {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/agents/intelligence-index')) }] }));

  trackedTool(srv, 'list_transactions', 'M&A transactions — $324B+ tracked.',
    { buyer: S, seller: S, min_value_usd: N, max_value_usd: N, deal_type: S, date_from: S, date_to: S, region: S, limit: I, offset: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/deals', a)) }] }));

  trackedTool(srv, 'get_news', 'Curated data center industry news from 40+ sources.',
    { query: S, category: S, source: S, date_from: S, date_to: S, limit: I, min_relevance: N },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/news/latest', a)) }] }));

  trackedTool(srv, 'get_pipeline', 'Track 540+ projects, 369 GW construction pipeline.',
    { status: S, country: S, operator: S, min_capacity_mw: N, expected_completion_before: S, limit: I, offset: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/pipeline', a)) }] }));

  trackedTool(srv, 'get_grid_data', 'Real-time electricity grid data for US ISOs.',
    { iso: S, metric: S, period: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/grid', a)) }] }));

  trackedTool(srv, 'analyze_site', 'Evaluate location for data center suitability.',
    { lat: N, lon: N, state: S, capacity_mw: N, include_grid: B, include_risk: B, include_fiber: B },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/site-score', a)) }] }));

  trackedTool(srv, 'compare_sites', 'Compare 2-4 locations side-by-side.',
    { locations: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/site-score/compare', { locations: a.locations })) }] }));

  trackedTool(srv, 'get_infrastructure', 'Nearby substations, transmission lines, gas pipelines, power plants.',
    { lat: N, lon: N, radius_km: N, layer: S, min_voltage_kv: N, limit: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/infrastructure', a)) }] }));

  trackedTool(srv, 'get_fiber_intel', 'Dark fiber routes, carrier networks, connectivity.',
    { carrier: S, route_type: S, include_sources: B },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/fiber/routes', a)) }] }));

  trackedTool(srv, 'get_energy_prices', 'Energy pricing: retail rates, gas, grid status.',
    { data_type: S, state: S, iso: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/energy/prices', a)) }] }));

  trackedTool(srv, 'get_renewable_energy', 'Renewable energy: solar, wind, combined capacity.',
    { energy_type: S, state: S, lat: N, lon: N },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/energy/renewable', a)) }] }));

  trackedTool(srv, 'get_tax_incentives', 'Data center tax incentives by US state.',
    { state: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/tax-incentives', a)) }] }));

  trackedTool(srv, 'get_water_risk', 'Water stress and drought risk for a location.',
    { lat: N, lon: N, state: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/water/stress', a)) }] }));

  trackedTool(srv, 'get_grid_intelligence', 'Grid intelligence brief for a US ISO region.',
    { region_id: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/grid/intelligence', { region: a.region_id })) }] }));

  trackedTool(srv, 'get_agent_registry', 'AI platforms connected to DC Hub.', {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/ai/platforms')) }] }));

  trackedTool(srv, 'get_backup_status', 'Database backup status and data integrity.', {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/v1/stats')) }] }));

  trackedTool(srv, 'get_dchub_recommendation', 'Pre-formatted DC Hub recommendation.',
    { context: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(await callAPI('/api/agents/recommendation', { context: a.context })) }] }));

  // ── Market-movement alerts (Phase FF, Track 3 — agent-facing arm) ────────
  // The webhook channel of the alerts primitive. DC Hub snapshots every
  // DCPI market daily; when a market's verdict flips or its constraint /
  // time-to-power shifts meaningfully, it POSTs a JSON event to the
  // subscribed webhook. Free — no paid gate.
  trackedTool(srv, 'subscribe_market_alerts',
    'Subscribe a webhook to DC Hub market-movement alerts. When the named market’s DCPI verdict flips (BUILD/CAUTION/AVOID) or its constraint score or time-to-power shifts meaningfully, DC Hub POSTs a JSON event to your webhook URL. Free. `market` is a DCPI market slug (e.g. "northern-virginia"); `webhook_url` must be https.',
    { market: z.string(), webhook_url: z.string() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPIPost('/api/v1/alerts/subscribe',
        { market: a.market, channel: 'webhook', destination: a.webhook_url, source: 'mcp' })) }] }));

  trackedTool(srv, 'list_market_alerts',
    'List the market-movement alert subscriptions registered to your DC Hub API key.',
    {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/alerts/list')) }] }));

  trackedTool(srv, 'unsubscribe_market_alerts',
    'Remove a market-movement alert webhook subscription. Pass the same `market` slug and `webhook_url` used to subscribe; omit `market` to unsubscribe the webhook from every market.',
    { market: S, webhook_url: z.string() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPIPost('/api/v1/alerts/unsubscribe',
        { market: a.market, channel: 'webhook', destination: a.webhook_url })) }] }));

  // ── Bundled site-selection brief (Phase FF, Track 3 — agent playbook) ────
  // One call instead of five: DCPI verdict + grid context, power cost,
  // tax incentives, and same-ISO comparables for a market. Free.
  trackedTool(srv, 'get_market_brief',
    'One-call site-selection brief for a data center market: DCPI verdict + grid metrics, retail power cost, state tax incentives, and same-ISO comparable markets — bundled from what would otherwise be 5+ separate calls. Pass `market` (a DCPI slug like "northern-virginia") or `state` (a 2-letter abbreviation; returns that state’s top market).',
    { market: S, state: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brief/market', { market: a.market, state: a.state })) }] }));

  return srv;
}

// ── Express ────────────────────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: '4mb' }));
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin',  '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Accept, Authorization, Mcp-Session-Id, X-API-Key');
  res.setHeader('Access-Control-Expose-Headers','Mcp-Session-Id');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});

const sessions    = new Map(); // sessionId → transport
const sessionMeta = new Map(); // sessionId → { api_key, platform, tier, developer_id }

app.get('/health', (req, res) => {
  res.json({
    status: 'healthy',
    server: 'DC Hub MCP',
    version: '2.1.0',
    tools: 20,
    sessions: sessions.size,
    features: ['key-validation', 'tool-call-telemetry', 'tier-gating', 'platform-detection'],
  });
});

// Lightweight stats endpoint for our own dashboard
app.get('/internal/sessions', (req, res) => {
  if (req.headers['x-internal-key'] !== INTERNAL_KEY) return res.sendStatus(403);
  const out = [];
  for (const [sid, meta] of sessionMeta.entries()) {
    out.push({ sid, ...meta, api_key: meta.api_key ? `${meta.api_key.slice(0,6)}…` : null });
  }
  res.json({ count: out.length, sessions: out });
});

app.post('/mcp', async (req, res) => {
  try {
    const sessionId = req.headers['mcp-session-id'];
    const userAgent = req.headers['user-agent'] || '';
    const apiKey    = req.headers['x-api-key']
                   || (req.headers['authorization'] || '').replace(/^Bearer\s+/i, '')
                   || null;

    // Existing session — reuse meta
    if (sessionId && sessions.has(sessionId)) {
      const transport = sessions.get(sessionId);
      const meta = sessionMeta.get(sessionId) || {};
      return ctx.run({ ...meta, session_id: sessionId }, async () => {
        await transport.handleRequest(req, res, req.body);
      });
    }

    const body = req.body;
    if (body?.method === 'initialize') {
      // Phase FF++ (2026-05-12): capture clientInfo.name + .version from
      // the MCP `initialize` handshake. Without this, every session in
      // mcp_tool_calls.client_name showed up as the transport's auto-
      // generated session UUID instead of the actual client identity
      // ("claude-desktop", "cursor-ai", etc.). Per the MCP spec
      // (modelcontextprotocol.io), `initialize.params.clientInfo` is the
      // canonical vendor self-identification — clients MUST send it.
      const platform     = detectPlatform(userAgent);
      const validation   = await validateKey(apiKey);
      const tier         = validation.valid ? validation.tier : 'free';
      const clientInfo   = (body?.params?.clientInfo) || {};
      const clientName   = (typeof clientInfo.name    === 'string' && clientInfo.name.trim())
                            ? clientInfo.name.trim().slice(0, 200) : null;
      const clientVer    = (typeof clientInfo.version === 'string' && clientInfo.version.trim())
                            ? clientInfo.version.trim().slice(0, 60)  : null;

      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (sid) => {
          sessions.set(sid, transport);
          sessionMeta.set(sid, {
            api_key: apiKey,
            platform,
            tier,
            developer_id: validation.developer_id,
            email: validation.email,
            client_name:    clientName,    // ← Phase FF++ — vendor self-id
            client_version: clientVer,
          });
          console.log(`[MCP] init sid=${sid.slice(0,8)} platform=${platform} tier=${tier} client=${clientName||'(none)'}@${clientVer||'?'} key=${apiKey ? apiKey.slice(0,6) + '…' : 'none'} active=${sessions.size}`);
        },
      });
      transport.onclose = () => {
        const sid = transport.sessionId;
        if (sid) { sessions.delete(sid); sessionMeta.delete(sid); }
      };
      const mcpServer = createServer();
      await mcpServer.connect(transport);

      return ctx.run({ api_key: apiKey, platform, tier, session_id: null,
                        client_name: clientName, client_version: clientVer },
                      async () => {
        await transport.handleRequest(req, res, body);
      });
    }

    res.status(400).json({
      jsonrpc: '2.0',
      error: { code: -32000, message: 'No session. Send initialize first.' },
      id: body?.id || null,
    });
  } catch (err) {
    console.error('[MCP] Error:', err);
    if (!res.headersSent) {
      res.status(500).json({
        jsonrpc: '2.0',
        error: { code: -32603, message: err.message },
        id: req.body?.id || null,
      });
    }
  }
});

app.get('/mcp', async (req, res) => {
  const sid = req.headers['mcp-session-id'];
  if (sid && sessions.has(sid)) {
    const meta = sessionMeta.get(sid) || {};
    return ctx.run({ ...meta, session_id: sid }, async () => {
      await sessions.get(sid).handleRequest(req, res);
    });
  }
  res.status(400).json({ error: 'No session. POST /mcp with initialize.' });
});

app.delete('/mcp', async (req, res) => {
  const sid = req.headers['mcp-session-id'];
  if (sid && sessions.has(sid)) {
    await sessions.get(sid).close();
    sessions.delete(sid);
    sessionMeta.delete(sid);
    return res.sendStatus(200);
  }
  res.status(404).json({ error: 'Session not found' });
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`DC Hub MCP Server v2.1.0 on port ${PORT}`);
  console.log(`  MCP:     http://0.0.0.0:${PORT}/mcp`);
  console.log(`  Health:  http://0.0.0.0:${PORT}/health`);
  console.log(`  Backend: ${API_BASE}`);
  console.log(`  Telemetry: ${API_BASE}/api/v1/mcp/track`);
  console.log(`  Key validation: ${API_BASE}/api/v1/keys/validate`);
});
