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

// Phase GG (2026-05-15): identified-tier caps. Identifying a key (POST
// /api/v1/keys/identify with an email) is the soft mid-step on the
// upgrade ladder — it's free, no password, no payment. Before tonight
// the only reward was lifting the OVERALL daily quota (25 -> 100). Now
// it also lifts the per-tool caps on the gateway tools — same 2.5×
// improvement that paid users got, but conditional on a verifiable
// email. This gives 100+ heavy users (the audit shows ~100 distinct
// users hammering get_grid_intelligence at 30+ calls/user) a concrete
// reason to identify — which both (a) un-anonymizes them so we can
// nurture (PR #133/134's weekly digest) and (b) creates a softer
// conversion point before the $49/mo ask.
const IDENTIFIED_TIER_DAILY_CAPS = {
  get_grid_intelligence: 25,
  get_fiber_intel:       25,
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

// Returns { allowed, params, capped, dailyCap, dailyUsed, tierLevel } —
// async because the daily-cap branch needs a usage lookup.
// `identified` (bool) is "free key with an email on file" — gets higher
// per-tool caps than anonymous-keyed (Phase GG 2026-05-15).
async function applyTierGate(toolName, params, tier, api_key, identified) {
  if (tier === 'paid' || tier === 'enterprise') return { allowed: true, params };
  if (PAID_ONLY_TOOLS.has(toolName)) return { allowed: false };

  // Daily cap check for free tier. Identified keys get the higher cap
  // (free identification = soft mid-rung of the upgrade ladder).
  const capTable = identified ? IDENTIFIED_TIER_DAILY_CAPS : FREE_TIER_DAILY_CAPS;
  const dailyCap = capTable[toolName];
  if (dailyCap && tier === 'free') {
    const used = await usageToday(api_key, toolName);
    if (used >= dailyCap) {
      return {
        allowed: false, dailyCapExceeded: true,
        dailyCap, dailyUsed: used,
        tierLevel: identified ? 'free_identified' : 'free_anonymous',
      };
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
      const gate = await applyTierGate(name, args, tier, c.api_key, !!c.email);

      // Phase 274: daily-cap-exceeded gets its own friendlier message —
      // user has clearly been using the tool, so the paywall framing is
      // "you've earned this" not "you're locked out."
      if (!gate.allowed && gate.dailyCapExceeded) {
        status = 'blocked_daily_cap';
        const _quick = oneClickUpgradeLine();
        const _identified = !!c.email;
        // Phase GG (2026-05-15): tiered cap-exceeded message. The audit
        // shows ~100 users hitting get_grid_intelligence's per-tool cap
        // with 0.05% conversion to paid — agents can't "click a Stripe
        // link" in-protocol, so a single hard CTA leaks the funnel.
        // Now we offer THREE explicit paths in priority order:
        //   1. IDENTIFY (free, agent-callable, lifts cap 10 -> 25 if
        //      not already identified)  — only shown to non-identified
        //   2. WAIT (no friction, just delayed)
        //   3. UPGRADE (paid, the actual unlimited path)
        // Each path is a structured `paths[]` entry agents can parse
        // and act on without the markdown chrome they tend to gloss over.
        const _identifyBlock = _identified ? '' : `
**1️⃣ Free — Identify your key (one curl, no password, agent-callable)**

\`\`\`bash
curl -X POST https://dchub.cloud/api/v1/keys/identify \\
  -H "X-API-Key: YOUR_DCHUB_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"email":"you@company.com"}'
\`\`\`

Lifts your per-tool cap on \`${name}\` **10 → 25/day** immediately, lifts your overall quota **25 → 100/day**, queues a weekly digest of the markets you query, and turns on market-movement alerts. Free, no payment.

`;
        const _md = `## \u{23F1}\u{FE0F} Daily free-tier limit reached for \`${name}\`

You've used **${gate.dailyUsed}/${gate.dailyCap}** of today's calls to \`${name}\`. Resets at **00:00 UTC**.

### Three paths forward (in order of friction):
${_identifyBlock}**${_identified ? '1️⃣' : '2️⃣'} Wait it out** — daily cap resets at 00:00 UTC.

**${_identified ? '2️⃣' : '3️⃣'} Upgrade to Developer — $49/mo, 50% off first month with \`TRYDCHUB50\`**

${_quick}\u{1F449} **[Upgrade now](${UPGRADE_URL})** — 1,000 calls/day, **unlimited** \`${name}\`, \`get_grid_intelligence\`, \`get_fiber_intel\`, plus the paid-only tools (\`analyze_site\`, \`compare_sites\`, \`get_dchub_recommendation\`).

If you're scoring more than ~10 markets/day, the math favors upgrading.`;

        const _paths = [];
        if (!_identified) {
          _paths.push({
            priority: 1, name: 'identify', cost: 'free',
            agent_callable: true,
            action: 'POST /api/v1/keys/identify',
            method: 'POST',
            url: 'https://dchub.cloud/api/v1/keys/identify',
            body: { api_key: c.api_key || '<your key>', email: '<email>' },
            unlocks: `per-tool cap on ${name} 10 -> 25/day, overall 25 -> 100/day, market alerts, weekly digest`,
          });
        }
        _paths.push({
          priority: _identified ? 1 : 2, name: 'wait', cost: 'time',
          agent_callable: true,
          action: 'retry after next 00:00 UTC',
        });
        _paths.push({
          priority: _identified ? 2 : 3, name: 'upgrade', cost: '$49/mo',
          agent_callable: false,  // requires human at Stripe
          action: 'human visits checkout',
          url: STRIPE_PRO_LINK || UPGRADE_URL,
          unlocks: '1000/day + unlimited per-tool + all paid tools',
          discount_code: 'TRYDCHUB50',
        });

        return {
          content: [{ type: 'text', text: _md }],
          structuredContent: {
            error: 'daily_cap_exceeded',
            tool: name,
            current_tier: tier,
            current_identified: _identified,
            daily_used: gate.dailyUsed,
            daily_cap: gate.dailyCap,
            resets_at: 'next 00:00 UTC',
            paths: _paths,
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

  // ── DCPI (Data Center Power Index) — DC Hub's moat data ─────────────────
  // Phase GG (2026-05-15): expose DCPI directly in MCP. Was only reachable
  // via get_market_brief (bundled) or by scraping the /dcpi pages. Heavy
  // agents (102 users on get_grid_intelligence) need DCPI as a first-class
  // signal: BUILD/CAUTION/AVOID verdicts + the four numeric scores
  // (excess_power, constraint, time_to_power, queue_wait) tell an agent
  // exactly which markets to advance and which to skip.
  trackedTool(srv, 'get_dcpi_scores',
    'DCPI (Data Center Power Index) scores for all tracked markets — DC Hub\'s headline build/avoid signal. Returns each market\'s verdict (BUILD/CAUTION/AVOID), excess power score, constraint score, time-to-power months, ISO, and state. Filter by ISO or verdict; sort by any score.',
    { iso: S, verdict: S, sort_by: S, limit: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/dcpi/scores', {
        iso: a.iso, verdict: a.verdict, sort_by: a.sort_by, limit: a.limit })) }] }));

  trackedTool(srv, 'get_dcpi_market',
    'Full DCPI snapshot for one market by slug (e.g. "northern-virginia", "phoenix", "dallas-fort-worth"). Returns the verdict, all four scores, top risks + opportunities, queue wait, reserve margin, curtailment, and the underlying grid metrics. Pairs with get_market_brief when you also need energy cost + tax incentives.',
    { slug: z.string() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI(`/api/v1/dcpi/scores/${encodeURIComponent(a.slug || '')}`)) }] }));

  trackedTool(srv, 'get_dcpi_movers',
    'Biggest DCPI movers — markets whose excess-power or constraint score shifted most over the rolling window. Use to spot emerging BUILD opportunities or markets newly flagged AVOID. Returns delta + before/after scores per market.',
    { window: S, limit: I, direction: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/dcpi/movers', {
        window: a.window, limit: a.limit, direction: a.direction })) }] }));

  trackedTool(srv, 'get_dcpi_iso',
    'DCPI rolled up to the ISO level — per-ISO aggregate of BUILD/CAUTION/AVOID counts, average excess-power, average constraint, and total tracked markets. Use to compare grid operators (PJM vs ERCOT vs CAISO etc.) at a glance.',
    { iso: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI(a.iso ? `/api/v1/dcpi/iso/${encodeURIComponent(a.iso)}` : '/api/v1/dcpi/iso-comparison')) }] }));

  // ── Bundled site-selection brief (Phase FF, Track 3 — agent playbook) ────
  // One call instead of five: DCPI verdict + grid context, power cost,
  // tax incentives, and same-ISO comparables for a market. Free.
  trackedTool(srv, 'get_market_brief',
    'One-call site-selection brief for a data center market: DCPI verdict + grid metrics, retail power cost, state tax incentives, and same-ISO comparable markets — bundled from what would otherwise be 5+ separate calls. Pass `market` (a DCPI slug like "northern-virginia") or `state` (a 2-letter abbreviation; returns that state’s top market).',
    { market: S, state: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brief/market', { market: a.market, state: a.state })) }] }));

  // ── Phase GG (2026-05-14): per-site capacity + ISO snapshot + pocket listings ──
  // Site capacity report: bundled per-facility view of metadata, capacity
  // rollup, pipeline, DCPI verdict, peer facilities, and recent news.
  trackedTool(srv, 'get_site_capacity_report',
    'One-call per-site capacity report for a data center: site metadata + capacity rollup (operational MW, under-construction MW, planned MW, utilization %) + capacity_pipeline rows for the market + DCPI verdict if the market is scored + peer facilities in same city/state + recent news. Pass `site` as numeric id, slug, or facility name (fuzzy).',
    { site: z.string() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI(`/api/v1/sites/${encodeURIComponent(a.site || '')}/capacity-report`)) }] }));

  // ISO snapshot: heartbeat freshness + DCPI rollup + pipeline + facilities.
  trackedTool(srv, 'get_iso_snapshot',
    'Comprehensive snapshot for one ISO (ERCOT, CAISO, NYISO, MISO, PJM, SPP, ISONE, IESO, AESO, TVA, BPA): heartbeat freshness + DCPI verdict rollup (BUILD/CAUTION/AVOID counts + avg scores) + capacity pipeline + facility footprint. Single-call ISO health check.',
    { iso: z.string() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI(`/api/v1/iso/${encodeURIComponent(a.iso || '')}/snapshot`)) }] }));

  // ISO head-to-head comparison: all 11 tracked ISOs ranked.
  trackedTool(srv, 'get_iso_comparison',
    'Head-to-head comparison across all 11 tracked ISOs (ERCOT, CAISO, NYISO, MISO, PJM, SPP, ISONE, IESO, AESO, TVA, BPA). Each row carries DCPI rollup, pipeline totals, facility count, and heartbeat freshness — ranked by avg excess-power score (best opportunities first).',
    {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/iso/comparison')) }] }));

  // Pocket listings: curated exclusive site access (free = teaser, Pro+ = full).
  trackedTool(srv, 'get_pocket_listings',
    'Pocket-listing marketplace: curated exclusive data center sites available off-market. Free tier sees public listings + teaser count of pocket-tier inventory; Pro+ identified tier sees full pocket-listing roster. Optionally filter by `state`, `market`, or `min_mw` (minimum capacity).',
    { state: S, market: S, min_mw: I, limit: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/listings', {
        state: a.state, market: a.market, min_mw: a.min_mw, limit: a.limit })) }] }));

  trackedTool(srv, 'get_pocket_listing',
    'Detailed view of a single pocket listing by slug or id — capacity, asking price, contact info (Pro+), full detail JSON. Free tier returns the public teaser only.',
    { listing: z.string() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI(`/api/v1/listings/${encodeURIComponent(a.listing || '')}`)) }] }));

  // ── Phase GG (2026-05-14): agent leverage Bundles 1, 2, 3 ───────────
  // Bundle 1: session warm-up + negative-space inventory. CALL THESE FIRST.
  trackedTool(srv, 'get_dchub_index',
    'CALL FIRST in every session. One-call warm-up: valid enums (ISO codes, DCPI market slugs, verdicts, states), per-domain freshness window, active radar issues, coverage totals, and a drill-deeper map. Saves 5-6 discovery calls and prevents identifier-fumbling.',
    {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/agent/index')) }] }));

  trackedTool(srv, 'get_coverage',
    'Negative-space inventory: what DC Hub has AND explicitly does NOT track for a given domain. Pass `domain` (facilities, dcpi, pipeline, news, transactions, listings, fiber, water, tax, grid) and optionally `region` (US state or country code). Use BEFORE fishing for data we may not have.',
    { domain: z.string(), region: S },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/agent/coverage', {
        domain: a.domain, region: a.region })) }] }));

  // Bundle 2: persona-shaped bundled briefs (one call replaces 6+).
  trackedTool(srv, 'get_developer_brief',
    'For data center developers / site selectors: ranked site-selection shortlist with explicit rationale per market. Score = excess_power − constraint × 0.5 − overshoot × 5, +10 for BUILD verdict. Pass `load_mw` (target capacity), optional `state` (2-letter), `deadline_months` (default 36).',
    { load_mw: z.number().optional(), state: S, deadline_months: z.number().optional() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brief/developer', {
        load_mw: a.load_mw, state: a.state, deadline_months: a.deadline_months })) }] }));

  trackedTool(srv, 'get_buyer_brief',
    'For buyers / acquirers: candidate facilities matching size criteria + pocket-listing inventory + recent transaction comparables in the same geography. Pass optional `market` slug, `state` (2-letter), `min_mw` (default 50).',
    { market: S, state: S, min_mw: z.number().optional() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brief/buyer', {
        market: a.market, state: a.state, min_mw: a.min_mw })) }] }));

  trackedTool(srv, 'get_investor_brief',
    'For investors / analysts: operator scorecard with footprint (facility count + MW + geographic spread), pipeline contribution, M&A history (acquisitions + targets), recent news mentions, and peer comparables. Pass `operator` name (fuzzy matched).',
    { operator: z.string() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brief/investor', { operator: a.operator })) }] }));

  trackedTool(srv, 'get_policy_brief',
    'For policymakers / regulators: state-level rollup — installed base (facilities + MW + operator diversity), pipeline pressure (planned + under-construction MW), grid stress (DCPI verdict mix + averages), tax-incentive programs, and rough jobs-supported estimate. Pass `state` (2-letter code, required).',
    { state: z.string() },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brief/policy', { state: a.state })) }] }));

  // Bundle 3: diff feed — call to see what changed since last session.
  trackedTool(srv, 'get_changes_since',
    'Cross-domain diff feed. Returns new pipeline projects, news articles, DCPI re-scores, transactions, pocket listings, and discovered facilities since the timestamp you pass. Pass `since` as ISO-8601 OR shorthand like "24h" / "7d" (defaults to 24h). Cache the response\'s `generated_at` to pass back next session — skip re-pulling everything.',
    { since: S, limit: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/changes/since', {
        since: a.since, limit: a.limit })) }] }));

  // ── Phase GG (2026-05-14): Bundle 4 — Brain learning loop ──────────────
  // Brain's letter-grade self-assessment. Agents should check this BEFORE
  // trusting brain-auto-applied fixes; fall back to deterministic paths
  // when the grade is C or below.
  trackedTool(srv, 'get_brain_self_assessment',
    'Brain\'s letter-grade self-assessment (A/B/C/D/F) with rationale. Returns weighted score across fix-success, human-rejection, cron health, volume, and memory depth. Use this BEFORE trusting brain-auto-applied fixes — fall back to deterministic logic when grade is C or below.',
    {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brain/self-assessment')) }] }));

  trackedTool(srv, 'get_brain_effectiveness',
    'Month-over-month brain effectiveness: text+code proposal volume by month, outcome verification rate (did fixes actually work?), human-rejection rate, false-positive memory depth, chronic-stuck-issue count. Answers "is brain learning?".',
    {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brain/effectiveness')) }] }));

  trackedTool(srv, 'get_brain_outcomes',
    'Recent post-merge outcome verifications. For each approved brain proposal, did the pattern actually stop appearing? Pass optional `limit` (default 50, max 200).',
    { limit: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brain/outcomes', { limit: a.limit })) }] }));

  trackedTool(srv, 'get_brain_temporal_patterns',
    'Issues classified by temporal shape: chronic (always broken), intermittent (sporadic), spiking (>=5 in last 24h), resolved (no occurrence in 7+ days), or unknown. Pass optional `classification` to filter, `limit` (default 50).',
    { classification: S, limit: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brain/temporal-patterns', {
        classification: a.classification, limit: a.limit })) }] }));

  trackedTool(srv, 'get_brain_model_performance',
    'Per-(layer, model) brain performance over last 60 days: runs, approval rate, rejection rate, avg latency, plus a per-layer recommended-model output based on highest approval rate among models with >=10 runs.',
    {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/brain/model-performance')) }] }));

  // ── Phase GG (2026-05-15): Bundle 5C — broadcast + newsletter ─────────
  trackedTool(srv, 'get_subscriber_count',
    'Counts of addressable email recipients by tier: how many users with email per plan + how many newsletter-only subscribers. Use to size a broadcast audience before sending.',
    {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/subscribers/count')) }] }));

  trackedTool(srv, 'get_recent_broadcasts',
    'Recent broadcast history with subject, target tiers, eligible/sent/failed counts, and trigger source. Use to audit what went out and when.',
    { limit: I },
    async (a) => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/admin/broadcasts', { limit: a.limit })) }] }));

  trackedTool(srv, 'get_broadcast_health',
    'Broadcast subsystem health: schema readiness, RESEND_API_KEY configured?, active subscribers, total broadcasts, users-with-email-by-plan breakdown.',
    {},
    async () => ({ content: [{ type: 'text', text: JSON.stringify(
      await callAPI('/api/v1/broadcast/health')) }] }));

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
    tools: 40,
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

const MCP_LANDING_HTML = `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect DC Hub MCP · Claude, Cursor, Cline</title>
<meta name="description" content="Add DC Hub's MCP server to any AI agent runtime. 40 tools, 21,000+ facilities, no signup needed for the free tier.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<script src="https://dchub.cloud/js/dchub-nav.js" defer></script>
<style>
  body{max-width:860px;margin:0 auto;padding:32px 24px;line-height:1.6}
  header{margin:40px 0 28px}
  .eyebrow{color:var(--dch-indigo);font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:600}
  h1{font-size:2.4rem;margin:0 0 14px;letter-spacing:-.02em;line-height:1.15}
  .lead{color:var(--dch-text-mute);font-size:1.05rem;max-width:640px}
  .urlbox{background:rgba(129,140,248,.08);border:1px solid rgba(129,140,248,.3);border-radius:12px;padding:18px 22px;margin:24px 0}
  .urlbox-label{font-weight:600;color:var(--dch-indigo);margin-bottom:10px}
  .url-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  code.url{background:var(--dch-bg);padding:10px 16px;border-radius:8px;font-size:1.05rem;flex:1;min-width:280px;font-family:'JetBrains Mono',monospace}
  .btn{padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:.92rem;display:inline-block;cursor:pointer;font-family:inherit;border:none}
  .btn-primary{background:var(--dch-grad-brand);color:#fff}
  .btn-secondary{background:var(--dch-surface);border:1px solid var(--dch-border);color:var(--dch-text)}
  .btn-secondary:hover{border-color:var(--dch-indigo);color:var(--dch-indigo)}
  ol{padding-left:22px;margin:16px 0}
  ol li{margin-bottom:10px}
  .pane{background:var(--dch-surface);border:1px solid var(--dch-border);border-radius:12px;padding:22px;margin:20px 0}
  .pane h2{margin:0 0 12px;font-size:1.15rem}
  pre{background:var(--dch-bg);border:1px solid var(--dch-border);border-radius:8px;padding:14px 16px;overflow-x:auto;font-family:'JetBrains Mono',monospace;font-size:.85rem;line-height:1.5;margin:10px 0 0;position:relative}
  .copybtn{position:absolute;top:8px;right:8px;font-size:.72rem;color:var(--dch-indigo);background:var(--dch-surface);border:1px solid var(--dch-border);padding:3px 10px;border-radius:6px;cursor:pointer;font-family:inherit}
  code{background:var(--dch-surface);padding:1px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:.86em}
  footer{margin-top:36px;padding-top:18px;border-top:1px solid var(--dch-border);color:var(--dch-text-dim);font-size:.85rem}
  footer a{color:var(--dch-indigo);text-decoration:none}
</style>
</head><body>
<header>
  <div class="eyebrow">Model Context Protocol · MCP Server</div>
  <h1>Connect DC Hub to your AI in 30 seconds.</h1>
  <p class="lead">Native MCP server. 40 tools covering 21,000+ facilities, M&amp;A, grid intelligence, fiber, water risk, tax incentives. No signup needed for the free tier.</p>
</header>

<div class="urlbox">
  <div class="urlbox-label">Step 1 — Copy this URL:</div>
  <div class="url-row">
    <code class="url" id="mcpurl">https://dchub.cloud/mcp</code>
    <button class="btn btn-primary" onclick="copyUrl(this)">copy URL</button>
    <a href="https://claude.ai/settings/connectors" class="btn btn-secondary" target="_blank" rel="noopener">open Claude settings →</a>
  </div>
</div>

<div class="pane">
  <h2>Step 2 — Add to Claude.ai</h2>
  <ol>
    <li>Click <strong>open Claude settings →</strong> above (or visit <a href="https://claude.ai/settings/connectors" target="_blank" rel="noopener">claude.ai/settings/connectors</a>)</li>
    <li>Click <strong>+ Add custom connector</strong></li>
    <li>Name: <code>DC Hub</code> — URL: paste the URL you just copied — <strong>leave auth blank</strong> (we don’t use OAuth; tools work anonymously at 5 calls/day, or add an X-API-Key header for 1k/day)</li>
    <li>Save. The DC Hub connector appears in every chat under the 🔌 menu.</li>
  </ol>
</div>

<div class="pane">
  <h2>Other runtimes</h2>
  <p><strong>Claude Desktop</strong> — add to <code>claude_desktop_config.json</code>:</p>
  <pre><button class="copybtn" onclick="copyPre(this)">copy</button><code>"dchub": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "https://dchub.cloud/mcp"]
}</code></pre>
  <p style="margin-top:18px"><strong>Cursor / Cline / Continue.dev</strong> — streamable-http MCP config:</p>
  <pre><button class="copybtn" onclick="copyPre(this)">copy</button><code>"dchub": {
  "transport": "streamable-http",
  "url": "https://dchub.cloud/mcp"
}</code></pre>
</div>

<footer>
  Cited by ChatGPT, Claude, Gemini, Perplexity, Copilot, Cursor, Cline, Continue.dev ·
  <a href="https://dchub.cloud/cited-by">See receipts</a> ·
  <a href="https://dchub.cloud/pricing">Pricing</a> ·
  <a href="https://dchub.cloud/api-docs">REST API</a> ·
  <a href="https://dchub.cloud/.well-known/mcp.json">Manifest</a>
</footer>

<script>
function copyUrl(btn){
  navigator.clipboard.writeText('https://dchub.cloud/mcp').then(function(){
    var p = btn.textContent;
    btn.textContent = '✓ copied — now click "open Claude settings"';
    setTimeout(function(){ btn.textContent = p; }, 4000);
  });
}
function copyPre(btn){
  var code = btn.parentElement.querySelector('code');
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(function(){
    var p = btn.textContent;
    btn.textContent = 'copied!';
    setTimeout(function(){ btn.textContent = p; }, 1500);
  });
}
</script>
</body></html>`;

app.get('/mcp', async (req, res) => {
  const sid = req.headers['mcp-session-id'];
  if (sid && sessions.has(sid)) {
    const meta = sessionMeta.get(sid) || {};
    return ctx.run({ ...meta, session_id: sid }, async () => {
      await sessions.get(sid).handleRequest(req, res);
    });
  }
  // r33-J round 9 (2026-05-21): browser users hitting GET /mcp used
  // to see raw JSON-RPC error — completely unhelpful when trying to
  // figure out how to connect. If the request Accepts text/html
  // (i.e. a human in a browser), serve the connection landing page
  // inline (no redirect chain — /connect → /mcp would loop).
  // Agents using Accept: */* or application/json still get the
  // original 400 JSON shim.
  const accept = (req.headers.accept || '').toLowerCase();
  if (accept.includes('text/html')) {
    res.set('Content-Type', 'text/html; charset=utf-8');
    res.set('Cache-Control', 'public, max-age=300');
    return res.send(MCP_LANDING_HTML);
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
