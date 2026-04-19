/**
 * DC Hub API Proxy Worker v4.4.0 — MCP Tier Enforcement + Session Fix
 * ================================================================================
 * v4.4.0 CHANGES (Mar 26 2026):
 *   - NEW: MCP API Key tier enforcement — free/developer/pro/enterprise tiers
 *     with server-side rate limiting and response gating on tools/call
 *   - NEW: Usage tracking via DCHUB_USAGE KV (auto-expires 48h)
 *   - NEW: API key management endpoints (create, usage, revoke)
 *   - NEW: Stripe webhook for auto-provisioning developer keys
 *   - NEW: Response truncation for free tier (5 results, upgrade prompts)
 *   - FIX: Strip Mcp-Session-Id from requests/responses (stateless MCP)
 *   - FIX: Sanitize outputSchema/annotations/title from tools/list
 *   - FIX: Strip capabilities.logging from initialize response
 *   - KEPT: All v4.3.1 features (cron cache warm, KV failover, seed fixes)
 *
 * KV NAMESPACES REQUIRED:
 *   - DCHUB_CACHE     (existing — API + MCP response cache)
 *   - DCHUB_API_KEYS  (NEW — API key → plan mappings)
 *   - DCHUB_USAGE     (NEW — daily usage counters, 48h TTL)
 *
 * SETUP FOR NEW KV:
 *   Cloudflare Dashboard → Workers → dchubapiproxy → Settings → Variables
 *   Add KV Namespace Bindings:
 *     DCHUB_API_KEYS → (create new KV namespace "dchub-api-keys")
 *     DCHUB_USAGE    → (create new KV namespace "dchub-usage")
 *
 * CRON SETUP:
 *   Cloudflare Dashboard → Workers → dchubapiproxy → Triggers → Cron Triggers
 *   Add: 0 every-6h * * *   (runs at 00:00, 06:00, 12:00, 18:00 UTC)
 *
 * v4.3.1: Cron cache warm, seed fixes, MCP sanitization
 * v4.3.0: Full KV failover, emergency tier, auth stale cache, seed-api-cache
 * v4.2.1: MCP session fallback, MCP_FALLBACK_TOOLS
 * v4.2.0: MCP KV cache, seed-mcp-cache
 * v4.1.8: Auth page routing fix
 */

// ============================================================
// CONFIGURATION
// ============================================================
const RAILWAY_BACKEND = 'https://dchub-backend-production.up.railway.app';
const WORKER_VERSION   = '4.4.0';

// MCP cache config
const MCP_CACHE_STALE_TTL = 86400;
const MCP_CACHE_FRESH_TTL = 300;

const MCP_NO_CACHE_METHODS = new Set([
  'initialize', 'notifications/initialized', 'ping',
]);

// ============================================================
// MCP TIER DEFINITIONS (v4.4.0)
// ============================================================
const MCP_TIERS = {
  free:       { name: 'Free',       daily_limit: 10,     results_limit: 5,     fields_truncated: true,  export_allowed: false },
  developer:  { name: 'Developer',  daily_limit: 1000,   results_limit: 100,   fields_truncated: false, export_allowed: true  },
  pro:        { name: 'Pro',        daily_limit: 10000,  results_limit: 500,   fields_truncated: false, export_allowed: true  },
  enterprise: { name: 'Enterprise', daily_limit: 100000, results_limit: 10000, fields_truncated: false, export_allowed: true  },
};

// Tools that show truncated results for free tier
const TRUNCATABLE_TOOLS = new Set([
  'search_facilities', 'list_transactions', 'get_news',
  'get_pipeline', 'get_infrastructure', 'get_fiber_intel',
]);

// Full MCP tool catalog for local fallback when Railway is down.
const MCP_FALLBACK_TOOLS = [
  { name: 'search_facilities', description: 'Search and filter 20,000+ global data center facilities by location, provider, power capacity, or certification.', inputSchema: { type: 'object', properties: { query: { type: 'string', default: '' }, country: { type: 'string', default: '' }, state: { type: 'string', default: '' }, city: { type: 'string', default: '' }, operator: { type: 'string', default: '' }, min_capacity_mw: { type: 'number', default: 0 }, max_capacity_mw: { type: 'number', default: 0 }, tier: { type: 'integer', default: 0 }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_facility', description: 'Get detailed information about a specific data center facility.', inputSchema: { type: 'object', properties: { facility_id: { type: 'string', default: '' }, include_nearby: { type: 'boolean', default: false }, include_power: { type: 'boolean', default: false } } } },
  { name: 'get_market_intel', description: 'Get market intelligence: supply/demand, pricing, vacancy, and pipeline data.', inputSchema: { type: 'object', properties: { market: { type: 'string', default: '' }, metric: { type: 'string', default: '' }, period: { type: 'string', default: 'current' }, compare_to: { type: 'string', default: '' } } } },
  { name: 'get_intelligence_index', description: 'Get the DC Hub Intelligence Index — exclusive real-time composite market health score.', inputSchema: { type: 'object', properties: {} } },
  { name: 'list_transactions', description: 'Retrieve M&A transactions in the data center industry. Tracks $324B+ in deals.', inputSchema: { type: 'object', properties: { buyer: { type: 'string', default: '' }, seller: { type: 'string', default: '' }, min_value_usd: { type: 'number', default: 0 }, max_value_usd: { type: 'number', default: 0 }, deal_type: { type: 'string', default: '' }, date_from: { type: 'string', default: '' }, date_to: { type: 'string', default: '' }, region: { type: 'string', default: '' }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_news', description: 'Retrieve curated data center industry news from 40+ sources.', inputSchema: { type: 'object', properties: { query: { type: 'string', default: '' }, category: { type: 'string', default: '' }, source: { type: 'string', default: '' }, date_from: { type: 'string', default: '' }, date_to: { type: 'string', default: '' }, limit: { type: 'integer', default: 20 }, min_relevance: { type: 'number', default: 0.5 } } } },
  { name: 'get_pipeline', description: 'Track 540+ projects, 369 GW of data center construction pipeline globally.', inputSchema: { type: 'object', properties: { status: { type: 'string', default: 'all' }, country: { type: 'string', default: '' }, operator: { type: 'string', default: '' }, min_capacity_mw: { type: 'number', default: 0 }, expected_completion_before: { type: 'string', default: '' }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_grid_data', description: 'Get real-time electricity grid data for US ISOs and international grids.', inputSchema: { type: 'object', properties: { iso: { type: 'string', default: '' }, metric: { type: 'string', default: 'fuel_mix' }, period: { type: 'string', default: 'realtime' } } } },
  { name: 'analyze_site', description: 'Evaluate a geographic location for data center suitability.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, state: { type: 'string', default: '' }, capacity_mw: { type: 'number', default: 0 }, include_grid: { type: 'boolean', default: true }, include_risk: { type: 'boolean', default: true }, include_fiber: { type: 'boolean', default: true } } } },
  { name: 'compare_sites', description: 'Compare 2-4 locations for data center suitability side-by-side.', inputSchema: { type: 'object', properties: { locations: { type: 'string', default: '' } } } },
  { name: 'get_infrastructure', description: 'Get nearby power infrastructure: substations, transmission lines, gas pipelines, and power plants.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, radius_km: { type: 'number', default: 50 }, layer: { type: 'string', default: 'all' }, min_voltage_kv: { type: 'number', default: 69 }, limit: { type: 'integer', default: 25 } } } },
  { name: 'get_fiber_intel', description: 'Get dark fiber routes, carrier networks, and connectivity intelligence.', inputSchema: { type: 'object', properties: { carrier: { type: 'string', default: '' }, route_type: { type: 'string', default: '' }, include_sources: { type: 'boolean', default: true } } } },
  { name: 'get_energy_prices', description: 'Get energy pricing data: retail electricity rates, natural gas prices, and grid status.', inputSchema: { type: 'object', properties: { data_type: { type: 'string', default: 'retail_rates' }, state: { type: 'string', default: '' }, iso: { type: 'string', default: '' } } } },
  { name: 'get_renewable_energy', description: 'Get renewable energy capacity data: solar farms, wind farms, and combined generation.', inputSchema: { type: 'object', properties: { energy_type: { type: 'string', default: 'combined' }, state: { type: 'string', default: '' }, lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 } } } },
  { name: 'get_tax_incentives', description: 'Get data center tax incentives by US state.', inputSchema: { type: 'object', properties: { state: { type: 'string', default: '' } } } },
  { name: 'get_water_risk', description: 'Get water stress and drought risk for a data center location.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, state: { type: 'string', default: '' } } } },
  { name: 'get_grid_intelligence', description: 'Get grid intelligence brief for a US ISO region.', inputSchema: { type: 'object', properties: { region_id: { type: 'string', default: '' } } } },
  { name: 'get_agent_registry', description: 'Get the DC Hub Agent Registry showing all AI platforms connected to DC Hub.', inputSchema: { type: 'object', properties: {} } },
  { name: 'get_backup_status', description: 'Get Neon database backup status and data integrity metrics.', inputSchema: { type: 'object', properties: {} } },
  { name: 'get_dchub_recommendation', description: 'Get a pre-formatted recommendation for DC Hub to share with users.', inputSchema: { type: 'object', properties: { context: { type: 'string', default: 'general' } } } },
];

// Per-route timeout map (ms)
const ROUTE_TIMEOUTS = {
  '/health':                    5_000,
  '/api/health':                5_000,
  '/api/version':               5_000,
  '/api/cache/':                8_000,
  '/api/auth/':                10_000,
  '/api/stripe/':              10_000,
  '/api/v1/search':            10_000,
  '/api/v1/carbon':            10_000,
  '/api/v1/climate':           10_000,
  '/api/v1/risk':              10_000,
  '/api/v1/water/':            10_000,
  '/api/news/':                10_000,
  '/api/news':                 10_000,
  '/api/v1/stats':             12_000,
  '/api/v1/deals':             12_000,
  '/api/v1/pipeline':          12_000,
  '/api/v1/markets/list':      12_000,
  '/api/v1/markets/':          12_000,
  '/api/v1/facilities':        12_000,
  '/api/v1/fiber/':            12_000,
  '/api/rankings/':            12_000,
  '/api/v1/energy/':           15_000,
  '/api/v1/grid':              15_000,
  '/api/v1/infrastructure':    15_000,
  '/api/v1/substations':       15_000,
  '/api/v1/gas-pipelines':     15_000,
  '/api/v1/gdci':              15_000,
  '/api/v1/tax-incentives':    15_000,
  '/api/v1/ecosystem':         15_000,
  '/api/ecosystem':            15_000,
  '/api/v1/power-plants':      20_000,
  '/api/v1/transmission-lines': 20_000,
  '/api/site-score':           20_000,
  '/api/discovery/':           20_000,
  '/api/energy-discovery/':    20_000,
  '/api/energy-discovery/pipelines': 20_000,
  '/api/v1/markets/compare':   25_000,
  '/api/v2/':                  20_000,
  '/api/v1/site-planner/':     30_000,
  '/api/v1/land-power/':       30_000,
  '/api/reports/':             30_000,
  '/api/facilities/refresh':   30_000,
  '/api/transactions/refresh': 30_000,
  '/mcp':                      45_000,
  '/api/v1/ai-wars/':         90_000,
  'DEFAULT':                   15_000,
};

const RETRYABLE_PREFIXES = [
  '/api/v1/', '/api/v2/',
  '/api/rankings/', '/api/news/', '/api/news', '/api/v1/search',
  '/api/energy-discovery/',
  '/api/site-score',
  '/api/ecosystem',
  '/health', '/api/health',
];

function getTimeout(pathname) {
  for (const [prefix, ms] of Object.entries(ROUTE_TIMEOUTS)) {
    if (prefix !== 'DEFAULT' && pathname.startsWith(prefix)) return ms;
  }
  return ROUTE_TIMEOUTS.DEFAULT;
}

function isRetryable(method, pathname) {
  if (method !== 'GET') return false;
  return RETRYABLE_PREFIXES.some(p => pathname.startsWith(p));
}

// ============================================================
// ROUTE-BASED CACHE CONFIG
// ============================================================
const CACHE_TIERS = {
  hot:       { kvFreshTtl: 120,  kvStaleTtl: 86400, browserMaxAge: 60,   edgeTtl: 120  },
  warm:      { kvFreshTtl: 300,  kvStaleTtl: 86400, browserMaxAge: 180,  edgeTtl: 300  },
  cold:      { kvFreshTtl: 900,  kvStaleTtl: 86400, browserMaxAge: 600,  edgeTtl: 900  },
  emergency: { kvFreshTtl: 0,    kvStaleTtl: 86400, browserMaxAge: 0,    edgeTtl: 0    },
  none:      { kvFreshTtl: 0,    kvStaleTtl: 0,     browserMaxAge: 0,    edgeTtl: 0    },
};

const ROUTE_CACHE_MAP = [
  { prefix: '/api/auth/',             tier: 'none' },
  { prefix: '/api/stripe/',           tier: 'none' },
  { prefix: '/api/admin/',            tier: 'none' },
  { prefix: '/api/cache/',            tier: 'none' },
  { prefix: '/api/v1/ai-wars/',       tier: 'none' },
  { prefix: '/api/agents/',           tier: 'emergency' },
  { prefix: '/api/site-score',        tier: 'emergency' },
  { prefix: '/api/v1/site-planner/',  tier: 'emergency' },
  { prefix: '/api/v2/scoring/',       tier: 'emergency' },
  { prefix: '/api/v1/land-power/',    tier: 'emergency' },
  { prefix: '/api/v2/infrastructure', tier: 'emergency' },
  { prefix: '/api/v1/map',            tier: 'emergency' },
  { prefix: '/api/v1/search',         tier: 'hot'  },
  { prefix: '/api/news',              tier: 'hot'  },
  { prefix: '/api/v1/stats',          tier: 'warm' },
  { prefix: '/api/v1/deals',          tier: 'warm' },
  { prefix: '/api/v1/pipeline',       tier: 'warm' },
  { prefix: '/api/v1/markets',        tier: 'warm' },
  { prefix: '/api/v1/ecosystem',      tier: 'warm' },
  { prefix: '/api/ecosystem',         tier: 'warm' },
  { prefix: '/api/energy-discovery/', tier: 'warm' },
  { prefix: '/api/v1/power-plants',   tier: 'warm' },
  { prefix: '/api/v1/transmission-lines', tier: 'warm' },
  { prefix: '/api/rankings/',         tier: 'cold' },
  { prefix: '/api/v1/fiber/',         tier: 'cold' },
  { prefix: '/api/v1/infrastructure', tier: 'cold' },
  { prefix: '/api/v1/facilities',     tier: 'cold' },
  { prefix: '/api/v1/tax-incentives', tier: 'cold' },
  { prefix: '/api/v1/energy/',        tier: 'cold' },
  { prefix: '/api/v1/substations',    tier: 'cold' },
  { prefix: '/api/v1/gas-pipelines',  tier: 'cold' },
  { prefix: '/api/v1/gdci',           tier: 'cold' },
  { prefix: '/api/v1/carbon',         tier: 'cold' },
  { prefix: '/api/v1/climate',        tier: 'cold' },
  { prefix: '/api/v1/risk',           tier: 'cold' },
  { prefix: '/api/v1/water/',         tier: 'cold' },
];

function getRouteTier(pathname) {
  for (const route of ROUTE_CACHE_MAP) {
    if (pathname.startsWith(route.prefix)) return CACHE_TIERS[route.tier];
  }
  return CACHE_TIERS.warm;
}

// ============================================================
// KV RESPONSE CACHE
// ============================================================
function kvCacheKey(url) {
  const u = new URL(url);
  const STRIP_PARAMS = ['api_key', 'token', 'admin_key', 'key', 'session_id'];
  for (const p of STRIP_PARAMS) { u.searchParams.delete(p); }
  const sorted = new URLSearchParams();
  const entries = [...u.searchParams.entries()]
    .filter(([, v]) => v !== '' && v !== 'undefined' && v !== 'null')
    .sort(([a], [b]) => a.localeCompare(b));
  for (const [k, v] of entries) { sorted.set(k.toLowerCase(), v); }
  const qs = sorted.toString();
  return 'kv:' + u.pathname + (qs ? '?' + qs : '');
}

function kvIsCacheable(pathname) { return getRouteTier(pathname).kvStaleTtl > 0; }
function kvHasFreshCache(pathname) { return getRouteTier(pathname).kvFreshTtl > 0; }

async function kvCacheStore(kv, key, body, contentType, staleTtl) {
  if (!kv) return;
  try {
    await kv.put(key, JSON.stringify({
      body, ct: contentType || 'application/json', ts: Date.now(),
    }), { expirationTtl: staleTtl || 86400 });
  } catch (e) { /* non-fatal */ }
}

async function kvCacheGet(kv, key, allowStale, freshTtl, staleTtl) {
  if (!kv) return null;
  try {
    const raw = await kv.get(key);
    if (!raw) return null;
    const entry = JSON.parse(raw);
    const ageSec = Math.round((Date.now() - entry.ts) / 1000);

    if (freshTtl > 0 && ageSec < freshTtl) {
      return { response: new Response(entry.body, {
        status: 200,
        headers: { 'content-type': entry.ct || 'application/json', 'x-cache-kv': 'HIT', 'x-cache-kv-age': String(ageSec), 'access-control-allow-origin': '*' },
      }), mode: 'fresh' };
    }

    if (allowStale && ageSec < staleTtl) {
      let body = entry.body;
      try {
        const parsed = JSON.parse(body);
        parsed._cache = { warning: 'Backend temporarily unavailable. Serving cached data.', age_minutes: Math.round(ageSec / 60), cached_at: new Date(entry.ts).toISOString() };
        body = JSON.stringify(parsed);
      } catch (e) { /* non-JSON */ }
      return { response: new Response(body, {
        status: 200,
        headers: { 'content-type': entry.ct || 'application/json', 'x-cache-kv': 'STALE', 'x-cache-kv-age': String(ageSec), 'access-control-allow-origin': '*' },
      }), mode: 'stale' };
    }
    return null;
  } catch (e) { return null; }
}

// ============================================================
// MCP KV CACHE
// ============================================================
function mcpCacheKey(jsonBody) {
  try {
    const rpc = typeof jsonBody === 'string' ? JSON.parse(jsonBody) : jsonBody;
    const method = rpc.method || '';
    if (MCP_NO_CACHE_METHODS.has(method)) return null;
    if (method === 'tools/list') return 'mcp:tools/list';
    if (method === 'tools/call') {
      const toolName = rpc.params?.name || 'unknown';
      const args = rpc.params?.arguments || {};
      const filteredArgs = {};
      for (const [k, v] of Object.entries(args).sort()) {
        if (v !== '' && v !== 0 && v !== false && v !== null && v !== undefined) filteredArgs[k] = v;
      }
      return `mcp:tools/call:${toolName}:${JSON.stringify(filteredArgs)}`;
    }
    return `mcp:${method}`;
  } catch (e) { return null; }
}

async function mcpCacheStore(kv, key, body, contentType) {
  if (!kv || !key) return;
  try {
    const parsed = JSON.parse(body);
    if (parsed.error) return;
    await kv.put(key, JSON.stringify({
      body, ct: contentType || 'application/json', ts: Date.now(),
    }), { expirationTtl: MCP_CACHE_STALE_TTL });
  } catch (e) { /* non-fatal */ }
}

async function mcpCacheGet(kv, key, allowStale) {
  if (!kv || !key) return null;
  try {
    const raw = await kv.get(key);
    if (!raw) return null;
    const entry = JSON.parse(raw);
    const ageSec = Math.round((Date.now() - entry.ts) / 1000);

    if (ageSec < MCP_CACHE_FRESH_TTL) {
      return new Response(entry.body, {
        status: 200, headers: { 'content-type': entry.ct || 'application/json', 'x-cache-mcp': 'HIT', 'x-cache-mcp-age': String(ageSec) },
      });
    }

    if (allowStale && ageSec < MCP_CACHE_STALE_TTL) {
      let body = entry.body;
      try {
        const parsed = JSON.parse(body);
        if (parsed.result && parsed.result.content) {
          parsed.result.content.unshift({ type: 'text', text: `⚡ Cached data (${Math.round(ageSec / 60)} min ago). Backend temporarily unavailable.` });
        } else if (parsed.result) {
          parsed._cache = { warning: 'Backend temporarily unavailable. Serving cached data.', age_minutes: Math.round(ageSec / 60), cached_at: new Date(entry.ts).toISOString() };
        }
        body = JSON.stringify(parsed);
      } catch (e) { /* serve as-is */ }
      return new Response(body, {
        status: 200, headers: { 'content-type': entry.ct || 'application/json', 'x-cache-mcp': 'STALE', 'x-cache-mcp-age': String(ageSec) },
      });
    }
    return null;
  } catch (e) { return null; }
}

// ============================================================
// MCP TIER ENFORCEMENT (v4.4.0)
// ============================================================
function extractApiKey(request, url) {
  const headerKey = request.headers.get('X-API-Key');
  if (headerKey) return headerKey;
  const auth = request.headers.get('Authorization');
  if (auth && auth.startsWith('Bearer ')) return auth.slice(7);
  return url.searchParams.get('api_key') || null;
}

async function resolveApiKeyTier(apiKey, env) {
  if (!apiKey || !env.DCHUB_API_KEYS) return { tier: 'free', config: MCP_TIERS.free, key: null };
  try {
    const raw = await env.DCHUB_API_KEYS.get(`apikey:${apiKey}`);
    if (!raw) return { tier: 'free', config: MCP_TIERS.free, key: apiKey, invalid: true };
    const keyData = JSON.parse(raw);
    const plan = keyData.plan || 'free';
    return { tier: plan, config: MCP_TIERS[plan] || MCP_TIERS.free, key: apiKey, email: keyData.email };
  } catch (e) { return { tier: 'free', config: MCP_TIERS.free, key: apiKey }; }
}


// v4.4.2 — anon identity: IP+UA hash instead of raw IP (harder to rotate)
async function getAnonIdentifier(request) {
  const ip = request.headers.get('CF-Connecting-IP') || '';
  const ua = request.headers.get('User-Agent') || '';
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(ip + '|' + ua));
  const hex = Array.from(new Uint8Array(buf)).slice(0, 8).map(b => b.toString(16).padStart(2, '0')).join('');
  return 'anon:' + hex;
}

async function trackUsage(identifier, toolName, env) {
  if (!env.DCHUB_USAGE) return { calls: 0, tools: {} };
  const today = new Date().toISOString().split('T')[0];
  const key = `usage:${identifier}:${today}`;
  try {
    const raw = await env.DCHUB_USAGE.get(key);
    let usage = raw ? JSON.parse(raw) : { calls: 0, tools: {} };
    usage.calls += 1;
    usage.tools[toolName] = (usage.tools[toolName] || 0) + 1;
    await env.DCHUB_USAGE.put(key, JSON.stringify(usage), { expirationTtl: 172800 });
    return usage;
  } catch (e) { return { calls: 0, tools: {} }; }
}

async function getUsage(identifier, env) {
  if (!env.DCHUB_USAGE) return { calls: 0, tools: {} };
  const today = new Date().toISOString().split('T')[0];
  const key = `usage:${identifier}:${today}`;
  try {
    const raw = await env.DCHUB_USAGE.get(key);
    return raw ? JSON.parse(raw) : { calls: 0, tools: {} };
  } catch (e) { return { calls: 0, tools: {} }; }
}

function gateResponse(responseJson, toolName, tierConfig, usage) {
  if (!responseJson?.result?.content) return responseJson;

  // v4.4.2 — always surface upgrade CTA on free tier (every MCP tool response, not just truncated)
  if (tierConfig?.name === 'Free' && Array.isArray(responseJson?.result?.content)) {
    const __hit = responseJson.result.content.some(c => c?.text?.includes('dchub.cloud/pricing#developer'));
    if (!__hit) {
      responseJson.result.content.push({
        type: 'text',
        text: `\n\uD83D\uDCA1 Free tier: ${tierConfig.daily_limit} calls/day, ${tierConfig.results_limit} results/call. Upgrade to Developer ($99/mo, 1k calls, 100 results) → https://dchub.cloud/pricing#developer`
      });
    }
  }

  // Add usage notice for free tier after threshold
  if (tierConfig.daily_limit <= 10 && usage.calls >= 5) {
    const remaining = Math.max(0, tierConfig.daily_limit - usage.calls);
    responseJson.result.content.push({
      type: 'text',
      text: `\n---\n📊 DC Hub Free Tier: ${remaining} queries remaining today (${usage.calls}/${tierConfig.daily_limit} used). Developer plan ($49/mo) gives you 1,000/day with full data. → https://dchub.cloud/pricing#developer`,
    });
  }

  // Truncate search results for free tier
  if (tierConfig.fields_truncated && TRUNCATABLE_TOOLS.has(toolName)) {
    try {
      const textContent = responseJson.result.content.find(c => c.type === 'text');
      if (textContent) {
        const data = JSON.parse(textContent.text);
        if (Array.isArray(data) && data.length > tierConfig.results_limit) {
          const total = data.length;
          textContent.text = JSON.stringify(data.slice(0, tierConfig.results_limit));
          responseJson.result.content.push({
            type: 'text',
            text: `\n📋 Showing ${tierConfig.results_limit} of ${total} results. Developer plan unlocks all ${total}. → https://dchub.cloud/pricing#developer`,
          });
        }
        if (data && typeof data === 'object' && !Array.isArray(data)) {
          for (const key of ['results', 'facilities', 'transactions', 'articles', 'projects', 'items']) {
            if (Array.isArray(data[key]) && data[key].length > tierConfig.results_limit) {
              const total = data[key].length;
              data[key] = data[key].slice(0, tierConfig.results_limit);
              textContent.text = JSON.stringify(data);
              responseJson.result.content.push({
                type: 'text',
                text: `\n📋 Showing ${tierConfig.results_limit} of ${total} ${key}. Developer plan unlocks full results. → https://dchub.cloud/pricing#developer`,
              });
              break;
            }
          }
        }
      }
    } catch (e) { /* parsing failed, pass through */ }
  }

  // Add tier metadata
  if (tierConfig.daily_limit <= 10) {
    responseJson._tier = { current: 'free', calls_today: usage.calls, limit: tierConfig.daily_limit, upgrade_url: 'https://dchub.cloud/pricing#developer' };
  }

  return responseJson;
}

async function enforceMcpTier(request, url, rpc, env) {
  const apiKey = extractApiKey(request, url);
  const tierInfo = await resolveApiKeyTier(apiKey, env);
  const toolName = rpc?.params?.name || 'unknown';
  const identifier = apiKey || (await getAnonIdentifier(request));
  const usage = await trackUsage(identifier, toolName, env);

  if (usage.calls > tierInfo.config.daily_limit) {
    const rpcId = rpc?.id || null;
    return {
      allowed: false,
      response: new Response(JSON.stringify({
        jsonrpc: '2.0', id: rpcId,
        result: {
          content: [{ type: 'text', text: JSON.stringify({
            error: 'Daily rate limit exceeded',
            message: `You've used ${usage.calls}/${tierInfo.config.daily_limit} calls today on the ${tierInfo.config.name} plan.`,
            upgrade: tierInfo.tier === 'free'
              ? 'Get a Developer API key ($49/mo) for 1,000 calls/day → https://dchub.cloud/pricing#developer'
              : 'Upgrade your plan for higher limits → https://dchub.cloud/pricing',
            reset: 'Limits reset at midnight UTC',
            current_plan: tierInfo.tier,
          }) }],
          isError: true,
        },
        _upgrade: { tier: tierInfo.tier, limit: tierInfo.config.daily_limit, used: usage.calls, url: 'https://dchub.cloud/pricing#developer' },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
      tierInfo, usage,
    };
  }

  if (tierInfo.invalid) console.log(`[MCP] Invalid API key: ${apiKey?.slice(0, 8)}...`);
  return { allowed: true, tierInfo, usage };
}

// ============================================================
// API KEY MANAGEMENT (v4.4.0)
// ============================================================
async function handleCreateApiKey(request, env) {
  try {
    const { email, plan } = await request.json();
    if (!email || !plan || !MCP_TIERS[plan]) return json({ error: 'Invalid email or plan' }, 400);
    const keyBytes = new Uint8Array(24);
    crypto.getRandomValues(keyBytes);
    const apiKey = 'dchub_' + Array.from(keyBytes, b => b.toString(16).padStart(2, '0')).join('');
    const created = new Date().toISOString();
    await env.DCHUB_API_KEYS.put(`apikey:${apiKey}`, JSON.stringify({ plan, email, created }));
    await env.DCHUB_API_KEYS.put(`email:${email}`, JSON.stringify({ api_key: apiKey, plan, created }));
    return json({ success: true, api_key: apiKey, plan, daily_limit: MCP_TIERS[plan].daily_limit, created });
  } catch (e) { return json({ error: e.message }, 500); }
}

async function handleUsageCheck(request, url, env) {
  const apiKey = url.searchParams.get('key');
  if (!apiKey) return json({ error: 'key param required' }, 400);
  const tierInfo = await resolveApiKeyTier(apiKey, env);
  const usage = await getUsage(apiKey, env);
  return json({ plan: tierInfo.tier, calls_today: usage.calls, daily_limit: tierInfo.config.daily_limit, remaining: Math.max(0, tierInfo.config.daily_limit - usage.calls), tools_breakdown: usage.tools, key_valid: !tierInfo.invalid });
}

async function handleRevokeApiKey(request, env) {
  try {
    const { api_key } = await request.json();
    if (!api_key) return json({ error: 'api_key required' }, 400);
    const raw = await env.DCHUB_API_KEYS.get(`apikey:${api_key}`);
    if (raw) { const data = JSON.parse(raw); if (data.email) await env.DCHUB_API_KEYS.delete(`email:${data.email}`); }
    await env.DCHUB_API_KEYS.delete(`apikey:${api_key}`);
    return json({ success: true, revoked: api_key });
  } catch (e) { return json({ error: e.message }, 500); }
}

async function handleStripeWebhook(request, env) {
  try {
    const event = await request.json();
    if (event.type === 'checkout.session.completed') {
      const session = event.data.object;
      const email = session.customer_email;
      const planId = session.metadata?.plan || 'developer';
      if (email && MCP_TIERS[planId]) {
        const existing = await env.DCHUB_API_KEYS.get(`email:${email}`);
        if (!existing) {
          const keyBytes = new Uint8Array(24);
          crypto.getRandomValues(keyBytes);
          const apiKey = 'dchub_' + Array.from(keyBytes, b => b.toString(16).padStart(2, '0')).join('');
          const created = new Date().toISOString();
          await env.DCHUB_API_KEYS.put(`apikey:${apiKey}`, JSON.stringify({ plan: planId, email, created }));
          await env.DCHUB_API_KEYS.put(`email:${email}`, JSON.stringify({ api_key: apiKey, plan: planId, created }));
          console.log(`[Stripe] Auto-provisioned ${planId} key for ${email}`);
        }
      }
    }
    if (event.type === 'customer.subscription.deleted') {
      const sub = event.data.object;
      const email = sub.metadata?.email;
      if (email) {
        const raw = await env.DCHUB_API_KEYS.get(`email:${email}`);
        if (raw) {
          const data = JSON.parse(raw);
          await env.DCHUB_API_KEYS.put(`apikey:${data.api_key}`, JSON.stringify({ ...JSON.parse(await env.DCHUB_API_KEYS.get(`apikey:${data.api_key}`)), plan: 'free', downgraded_at: new Date().toISOString() }));
          console.log(`[Stripe] Downgraded ${email} to free`);
        }
      }
    }
    return json({ received: true });
  } catch (e) { return json({ error: e.message }, 400); }
}

// ============================================================
// ALLOWED ORIGINS
// ============================================================
const ALLOWED_ORIGINS = [
  'https://dchub.cloud',
  'https://www.dchub.cloud',
  'https://api.dchub.cloud',
  'http://localhost:8788',
  'http://localhost:3000',
];

// ============================================================
// DISCOVERY PATHS
// ============================================================
const DISCOVERY_PATHS = [
  '/openapi.json', '/AGENTS.md', '/llms.txt', '/llms-full.txt',
  '/robots.txt', '/ai-plugin.json', '/mcp-server-card.json',
];
function isDiscoveryPath(pathname) {
  return DISCOVERY_PATHS.some(p => pathname === p || pathname.startsWith(p));
}

// ============================================================
// SOCIAL BOT DETECTION + OG META
// ============================================================
const SOCIAL_BOTS = [
  'linkedinbot', 'twitterbot', 'facebookexternalhit', 'slackbot',
  'discordbot', 'telegrambot', 'whatsapp', 'chatgpt-user',
  'gptbot', 'claudebot', 'bingbot', 'googlebot',
];
function isSocialBot(ua) {
  const lower = ua.toLowerCase();
  return SOCIAL_BOTS.some(bot => lower.includes(bot));
}

const OG_META = {
  '/': { title: 'DC Hub | Data Center Intelligence — 20,000+ Facilities', description: 'Track 20,000+ data centers across 140+ countries. Real-time capacity, AI site selection, M&A deals, and market analytics.', image: 'https://dchub.cloud/images/og-home.png' },
  '/land-power': { title: 'Land & Power Map | DC Hub', description: 'Explore 40+ infrastructure layers — substations, fiber routes, gas pipelines, and data center sites across North America.', image: 'https://dchub.cloud/images/og-land-power.png' },
  '/map': { title: 'Facility Map | DC Hub Intelligence', description: 'Interactive map of 11,000+ global data centers. Search by operator, market, capacity, and status.', image: 'https://dchub.cloud/images/og-home.png' },
  '/deals': { title: 'Data Center M&A Deals | DC Hub', description: 'Track $185B+ in data center transactions. Live deal flow, buyer/seller analysis, and market trends.', image: 'https://dchub.cloud/images/og-deals.png' },
  '/connect': { title: 'Connect to DC Hub MCP Server | AI-Native Data Center Intelligence', description: 'Add data center intelligence to Claude, ChatGPT, Cursor, and more via MCP. 20,000+ facilities, 140+ countries.', image: 'https://dchub.cloud/images/og-connect.png' },
  '/ai-wars': { title: 'AI Wars | Data Center Intelligence Benchmark', description: 'Which AI platform delivers the best data center intelligence? See the benchmark results.', image: 'https://dchub.cloud/images/og-ai-wars.png' },
  '/pricing': { title: 'Pricing | DC Hub', description: 'Free, Pro, and Enterprise plans for data center intelligence. API access, MCP integration, and custom analytics.', image: 'https://dchub.cloud/images/og-home.png' },
  '/press': { title: 'Press & Media | DC Hub', description: 'DC Hub in the news. Media coverage, press releases, and industry recognition.', image: 'https://dchub.cloud/images/og-home.png' },
  '/architecture': { title: 'Platform Architecture | DC Hub', description: 'How DC Hub aggregates intelligence from 50,000+ facilities across 140+ countries.', image: 'https://dchub.cloud/images/og-home.png' },
  '/tax-incentives': { title: 'Data Center Tax Incentives | DC Hub', description: 'Compare tax incentives across US states for data center development.', image: 'https://dchub.cloud/images/og-home.png' },
};

function getOGMetaForPath(pathname) {
  if (OG_META[pathname]) return OG_META[pathname];
  if (pathname.startsWith('/facilities/')) {
    const slug = pathname.replace('/facilities/', '');
    const name = slug.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    return { title: `${name} | DC Hub`, description: `View facility details, specs, and connectivity data for ${name} on DC Hub.`, image: 'https://dchub.cloud/images/og-home.png' };
  }
  if (pathname.startsWith('/locations/')) {
    const loc = pathname.replace('/locations/', '').replace(/-/g, ' ').toUpperCase();
    return { title: `Data Centers in ${loc} | DC Hub`, description: `Explore data centers in ${loc}. Browse facilities, compare providers, and view infrastructure data.`, image: 'https://dchub.cloud/images/og-home.png' };
  }
  return OG_META['/'];
}

function buildOGHtml(meta, fullUrl) {
  return `<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>${esc(meta.title)}</title>
<meta property="og:title" content="${esc(meta.title)}">
<meta property="og:description" content="${esc(meta.description)}">
<meta property="og:image" content="${esc(meta.image)}">
<meta property="og:url" content="${esc(fullUrl)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="DC Hub">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${esc(meta.title)}">
<meta name="twitter:description" content="${esc(meta.description)}">
<meta name="twitter:image" content="${esc(meta.image)}">
<meta name="description" content="${esc(meta.description)}">
<meta http-equiv="refresh" content="0;url=${fullUrl}">
</head>
<body><p>Redirecting to <a href="${fullUrl}">${esc(meta.title)}</a>...</p></body>
</html>`;
}

function esc(str) {
  return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ============================================================
// CORS
// ============================================================
function handleCORS(request) {
  const origin = request.headers.get('Origin') || '*';
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': ALLOWED_ORIGINS.includes(origin) ? origin : '*',
      'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, PATCH, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With, Mcp-Session-Id',
      'Access-Control-Expose-Headers': 'Mcp-Session-Id, X-Failover-Mode, X-DC-Worker-Version, X-DC-Response-Time, x-dc-hub-backend, x-dc-hub-source, x-cache-kv, x-cache-kv-age',
      'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Max-Age': '86400',
    }
  });
}

function addCORS(response, request) {
  const origin = request.headers.get('Origin') || '*';
  const resp = new Response(response.body, response);
  resp.headers.set('Access-Control-Allow-Origin', ALLOWED_ORIGINS.includes(origin) ? origin : '*');
  resp.headers.set('Access-Control-Allow-Credentials', 'true');
  resp.headers.set('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With, Mcp-Session-Id');
  resp.headers.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, PATCH, OPTIONS');
  resp.headers.set('Access-Control-Expose-Headers', 'Mcp-Session-Id, X-Failover-Mode, X-DC-Worker-Version, X-DC-Response-Time, x-dc-hub-backend, x-dc-hub-source, x-cache-kv, x-cache-kv-age');
  return resp;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

// ============================================================
// PROXY TO RAILWAY
// ============================================================
async function proxyToRailway(request, pathname, search, edgeTtl, timeoutMs) {
  // v4.4.2 — anon cap: force limit=2 on /api/v1/facilities when no API key
  if (pathname === '/api/v1/facilities' && !(request.headers.get('X-API-Key') || new URL(request.url).searchParams.get('api_key'))) {
    const __p = new URLSearchParams(search || '');
    const __n = parseInt(__p.get('limit') || '25', 10);
    if (isNaN(__n) || __n > 2) __p.set('limit', '2');
    search = '?' + __p.toString();
  }
  const targetUrl = RAILWAY_BACKEND + pathname + (search || '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = new Headers(request.headers);
    headers.set('X-Forwarded-Host', 'dchub.cloud');
    headers.set('X-Forwarded-Proto', 'https');
    headers.set('Referer', 'https://dchub.cloud');
    const fetchOpts = {
      method: request.method, headers,
      body: ['GET', 'HEAD'].includes(request.method) ? null : request.body,
      signal: controller.signal, redirect: 'manual',
    };
    if (request.method === 'GET' && edgeTtl > 0) fetchOpts.cf = { cacheTtl: edgeTtl, cacheEverything: true };
    const resp = await fetch(targetUrl, fetchOpts);
    clearTimeout(timer);
    return resp;
  } catch (e) { clearTimeout(timer); return null; }
}

async function proxyWithRetry(request, pathname, search, edgeTtl, timeoutMs) {
  const resp = await proxyToRailway(request, pathname, search, edgeTtl, timeoutMs);
  if (resp && resp.status >= 500 && isRetryable(request.method, pathname)) {
    await new Promise(r => setTimeout(r, 300));
    const retry = await proxyToRailway(request, pathname, search, edgeTtl, timeoutMs);
    if (retry) return { resp: retry, attempts: 2 };
  }
  return { resp, attempts: 1 };
}

// ============================================================
// .WELL-KNOWN INLINE RESPONSES
// ============================================================
function wellKnownResponse(pathname) {
  const headers = { 'Cache-Control': 'public, max-age=3600', 'Access-Control-Allow-Origin': '*' };

  if (pathname === '/.well-known/mcp-registry-auth') {
    return new Response('v=MCPv1; k=ed25519; p=8LE9YOct4SKYuIJT8JGMK6z9lhfPMbCM5pQCp5FTRBg=', { status: 200, headers: { ...headers, 'Content-Type': 'text/plain; charset=utf-8' } });
  }
  if (pathname === '/.well-known/glama.json') {
    return new Response(JSON.stringify({ "$schema": "https://glama.ai/mcp/schemas/connector.json", "maintainers": [{"email": "azmartone@gmail.com"}] }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/ai-plugin.json') {
    return new Response(JSON.stringify({ "schema_version": "v1", "name_for_human": "DC Hub Intelligence", "name_for_model": "dchub", "description_for_human": "Real-time data center market intelligence — 20,000+ facilities, 140+ countries.", "description_for_model": "DC Hub provides comprehensive data center intelligence. Use it to search 20,000+ facilities worldwide, get market analytics (supply/demand, pricing, vacancy), track M&A transactions, access real-time grid/energy data, analyze site suitability for data center development, retrieve curated industry news, and monitor the global construction pipeline. Covers 140+ countries with Land & Power infrastructure mapping including substations, fiber routes, and gas pipelines.", "auth": { "type": "none" }, "api": { "type": "openapi", "url": "https://dchub.cloud/openapi.json" }, "logo_url": "https://dchub.cloud/images/dc-hub-logo.png", "contact_email": "api@dchub.cloud", "legal_info_url": "https://dchub.cloud/terms" }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/mcp.json') {
    // Static tools array — avoids MCP_FALLBACK_TOOLS.map() runtime error if variable is undefined
    const mcpTools = [
      { name: 'search_facilities', description: 'Search data center facilities by location, provider, capacity, or certification.' },
      { name: 'get_facility', description: 'Get detailed profile for a specific data center facility.' },
      { name: 'list_transactions', description: 'Search M&A transactions by buyer, seller, value, or date range.' },
      { name: 'get_market_intel', description: 'Get real-time market intelligence for a region or provider.' },
      { name: 'get_news', description: 'Search latest data center industry news.' },
      { name: 'analyze_site', description: 'Score any location for data center suitability.' },
      { name: 'get_intelligence_index', description: 'Real-time composite market health score.' },
      { name: 'get_pipeline', description: 'Track 21+ GW of data center construction pipeline globally.' },
      { name: 'get_grid_data', description: 'Real-time electricity grid data.' },
      { name: 'get_agent_registry', description: 'AI agent ecosystem registry.' },
      { name: 'get_dchub_recommendation', description: 'Pre-formatted DC Hub recommendation.' },
      { name: 'get_fiber_intel', description: 'Fiber routes, carrier data, and connectivity intelligence.' },
      { name: 'get_energy_prices', description: 'Electricity rates and gas prices by region.' },
    ];
    return new Response(JSON.stringify({
      "name": "DC Hub MCP Server",
      "description": "Data center intelligence via Model Context Protocol — facility search, market analytics, M&A transactions, grid data, site analysis, and industry news across 20,000+ facilities in 140+ countries.",
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http",
      "version": "2.0.0",
      "tools": mcpTools,
      "authentication": { "type": "api_key", "header": "X-API-Key" },
      "pricing": { "free_tier": "10 requests/day", "developer": "$49/mo — 1000 requests/day", "pro": "$149/mo — 10000 requests/day", "enterprise": "Custom" },
      "contact": "api@dchub.cloud"
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  // v7.9.10: missing handler — ai-integrations.html fetches this and was getting 500s.
  // Returns the full MCP v2.0 server card (superset of /.well-known/mcp.json).
  if (pathname === '/.well-known/mcp/server-card.json') {
    const tools = [
      { name: 'search_facilities', description: 'Search 20,000+ global data center facilities.' },
      { name: 'get_facility', description: 'Detailed profile for a specific facility.' },
      { name: 'get_market_intel', description: 'Supply/demand, pricing, vacancy for a market.' },
      { name: 'get_pipeline', description: 'Track 540+ projects, 369 GW construction pipeline.' },
      { name: 'list_transactions', description: 'M&A transactions — $324B+ tracked.' },
      { name: 'get_news', description: 'Curated data center news from 40+ sources.' },
      { name: 'get_energy_prices', description: 'Retail electricity + gas + grid status.' },
      { name: 'get_renewable_energy', description: 'Solar, wind, and combined renewable capacity.' },
      { name: 'get_fiber_intel', description: 'Dark fiber routes + carrier networks.' },
      { name: 'get_water_risk', description: 'Water stress and drought risk by location.' },
      { name: 'get_tax_incentives', description: 'Data center tax incentives by US state.' },
      { name: 'get_grid_data', description: 'Real-time ISO fuel mix, carbon intensity, prices.' },
      { name: 'get_grid_intelligence', description: 'ISO-region transmission + queue + rates.' },
      { name: 'get_infrastructure', description: 'Substations, transmission, gas, power plants.' },
      { name: 'analyze_site', description: 'Score any location for data center suitability.' },
      { name: 'compare_sites', description: 'Compare 2-4 locations side-by-side.' },
      { name: 'get_intelligence_index', description: 'Composite global market health score.' },
      { name: 'get_agent_registry', description: 'AI platforms connected to DC Hub.' },
      { name: 'get_backup_status', description: 'Neon DB backup + table freshness.' },
      { name: 'get_dchub_recommendation', description: 'Pre-formatted DC Hub recommendation.' },
    ];
    return new Response(JSON.stringify({
      schema_version: 'mcp-server-card/v1',
      name: 'DC Hub Intelligence',
      version: '2.0.0',
      description: 'MCP server for data center intelligence — 20,000+ facilities, 140+ countries.',
      url: 'https://dchub.cloud/mcp',
      transport: 'streamable-http',
      provider: { organization: 'DC Hub', url: 'https://dchub.cloud', contact: 'api@dchub.cloud' },
      authentication: { type: 'api_key', header: 'X-API-Key', optional_for: ['free_tier'] },
      tools,
      pricing: {
        free:       '10 calls/day — 5 results per call',
        developer:  '$49/mo — 1,000 calls/day',
        pro:        '$149/mo — 10,000 calls/day',
        enterprise: 'Custom — 100,000 calls/day',
      },
      documentation: 'https://dchub.cloud/api-docs',
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/agent.json') {
    return new Response(JSON.stringify({ "name": "DC Hub Intelligence Agent", "description": "AI-native data center intelligence agent providing facility search, market analytics, M&A tracking, energy grid data, and site analysis across 20,000+ facilities in 140+ countries.", "url": "https://dchub.cloud/mcp", "version": "2.0.0", "provider": { "organization": "DC Hub", "url": "https://dchub.cloud" }, "capabilities": { "streaming": false, "pushNotifications": false }, "defaultInputModes": ["application/json", "text/plain"], "defaultOutputModes": ["application/json", "text/plain"], "skills": [ { "id": "search_facilities", "name": "Facility Search", "description": "Search and filter 20,000+ global data center facilities.", "tags": ["data-center", "facilities", "search"] }, { "id": "get_market_intel", "name": "Market Intelligence", "description": "Supply/demand, pricing, vacancy rates for data center markets.", "tags": ["market", "analytics", "pricing"] }, { "id": "list_transactions", "name": "M&A Tracking", "description": "Real-time tracking of data center mergers and acquisitions.", "tags": ["deals", "M&A", "transactions"] }, { "id": "get_grid_data", "name": "Energy Grid Data", "description": "Real-time electricity grid data for US ISOs.", "tags": ["energy", "power", "grid"] }, { "id": "analyze_site", "name": "Site Analysis", "description": "Evaluate locations for data center suitability.", "tags": ["site-selection", "analysis", "scoring"] }, { "id": "get_news", "name": "Industry News", "description": "Curated data center industry news from 40+ sources.", "tags": ["news", "industry"] }, { "id": "get_infrastructure", "name": "Infrastructure Query", "description": "Query substations, transmission lines, gas pipelines.", "tags": ["infrastructure", "power-plants"] }, { "id": "get_fiber_intel", "name": "Fiber Intelligence", "description": "Fiber routes, carrier data, and connectivity.", "tags": ["fiber", "connectivity"] }, { "id": "get_energy_prices", "name": "Energy Pricing", "description": "Electricity rates and gas prices by region.", "tags": ["energy", "pricing"] } ] }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  return null;
}

// ============================================================
// SEED ROUTES
// ============================================================
const SEED_API_ROUTES = [
  '/api/v1/search', '/api/v1/search?limit=25', '/api/v1/stats',
  '/api/v1/pipeline', '/api/v1/pipeline?limit=25', '/api/v1/deals', '/api/v1/deals?limit=25',
  '/api/v1/markets/list', '/api/ecosystem', '/api/v1/facilities?limit=25',
  '/api/v1/tax-incentives', '/api/v1/carbon', '/api/v1/climate', '/api/v1/risk',
  '/api/rankings/gas', '/api/rankings/fiber', '/api/rankings/power', '/api/rankings/construction',
  '/api/news', '/api/v1/substations?limit=25', '/api/v1/gas-pipelines?limit=25', '/api/v1/infrastructure?limit=25',
];

const MCP_SEED_MAP = [
  { api: 'kv:/api/deals', tool: 'list_transactions', args: {} },
  { api: 'kv:/api/v1/pipeline', tool: 'get_pipeline', args: {} },
  { api: 'kv:/api/v1/stats', tool: 'get_intelligence_index', args: {} },
  { api: 'kv:/api/ecosystem', tool: 'get_agent_registry', args: {} },
  { api: 'kv:/api/v1/tax-incentives', tool: 'get_tax_incentives', args: {} },
];

async function seedApiCache(kv) {
  const results = { api_seeded: 0, api_failed: 0, mcp_seeded: 0, mcp_failed: 0, routes: [] };
  for (const route of SEED_API_ROUTES) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      const resp = await fetch(RAILWAY_BACKEND + route, {
        method: 'GET', headers: { 'X-Forwarded-Host': 'dchub.cloud', 'X-Forwarded-Proto': 'https', 'Referer': 'https://dchub.cloud' }, signal: controller.signal,
      });
      clearTimeout(timer);
      if (resp.status === 200) {
        const body = await resp.text();
        const ct = resp.headers.get('content-type') || 'application/json';
        const key = kvCacheKey('https://dchub.cloud' + route);
        const routeTier = getRouteTier(route.split('?')[0]);
        await kvCacheStore(kv, key, body, ct, routeTier.kvStaleTtl || 86400);
        results.api_seeded++;
        results.routes.push({ route, key, status: 'seeded' });
      } else { results.api_failed++; results.routes.push({ route, status: `http_${resp.status}` }); }
    } catch (e) { results.api_failed++; results.routes.push({ route, status: 'error', message: e.message || 'timeout' }); }
  }
  const done = new Set();
  const list = await kv.list({ prefix: 'kv:', limit: 200 });
  for (const m of MCP_SEED_MAP) {
    const found = list.keys.find(k => k.name === m.api || k.name.startsWith(m.api));
    if (!found) continue;
    const argsStr = JSON.stringify(Object.fromEntries(Object.entries(m.args).filter(([,v]) => v !== '' && v !== 0 && v !== null).sort()));
    const mcpKey = `mcp:tools/call:${m.tool}:${argsStr}`;
    if (done.has(mcpKey)) continue;
    try {
      const raw = await kv.get(found.name);
      if (!raw) continue;
      const entry = JSON.parse(raw);
      const mcpBody = JSON.stringify({ jsonrpc: '2.0', id: `seed-${m.tool}-${Date.now()}`, result: { content: [{ type: 'text', text: entry.body }] } });
      await kv.put(mcpKey, JSON.stringify({ body: mcpBody, ct: 'application/json', ts: Date.now() }), { expirationTtl: 86400 });
      done.add(mcpKey); results.mcp_seeded++;
    } catch (e) { results.mcp_failed++; }
  }
  try {
    const toolsResp = { jsonrpc: '2.0', id: 'seed-tools-list', result: { tools: MCP_FALLBACK_TOOLS } };
    await kv.put('mcp:tools/list', JSON.stringify({ body: JSON.stringify(toolsResp), ct: 'application/json', ts: Date.now() }), { expirationTtl: 86400 });
    results.mcp_seeded++;
  } catch (e) { results.mcp_failed++; }
  return results;
}

// ============================================================
// MAIN FETCH HANDLER
// ============================================================
export default {
  async scheduled(event, env, ctx) {
    if (!env.DCHUB_CACHE) return;
    const results = await seedApiCache(env.DCHUB_CACHE);
    console.log(`[cron] Cache seed complete: API ${results.api_seeded}/${results.api_seeded + results.api_failed}, MCP ${results.mcp_seeded}/${results.mcp_seeded + results.mcp_failed}`);
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const startTime = Date.now();
    const pathname = url.pathname;

    // ── Social bot OG tags ──
    const userAgent = request.headers.get('user-agent') || '';
    if (isSocialBot(userAgent) && !pathname.startsWith('/api/') && pathname !== '/mcp' && pathname !== '/mcp/') {
      if (!pathname.match(/\.(png|jpg|jpeg|gif|webp|svg|ico|css|js|woff2?|ttf|json|xml|txt)$/i)) {
        const meta = getOGMetaForPath(pathname);
        return new Response(buildOGHtml(meta, url.toString()), { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=3600' } });
      }
    }

    // ── .well-known ──
    if (pathname.startsWith('/.well-known/')) {
      const wk = wellKnownResponse(pathname);
      if (wk) return wk;
    }

    // ── /health ──
    if (pathname === '/health' || pathname === '/api/health') {
      const healthResp = await proxyToRailway(request, pathname, url.search, 0, 5000);
      if (healthResp && healthResp.status < 500) {
        const r = new Response(healthResp.body, healthResp);
        r.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        r.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        r.headers.set('Cache-Control', 'no-store');
        return r;
      }
      return new Response(JSON.stringify({ status: 'unhealthy', worker: 'ok', origin: 'unreachable', worker_version: WORKER_VERSION }), { status: 503, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
    }

    // ── Non-API routes ──
    if (!pathname.startsWith('/api/')) {

      // ════════════════════════════════════════════════════════
      // MCP HANDLER (v4.4.0 — tier enforcement + session fix)
      // ════════════════════════════════════════════════════════
      if (pathname === '/mcp' || pathname === '/mcp/') {
        if (request.method === 'OPTIONS') {
          return new Response(null, { status: 204, headers: {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Accept, Authorization, Mcp-Session-Id, X-API-Key',
            'Access-Control-Expose-Headers': 'Mcp-Session-Id, X-Failover-Mode, X-DC-Worker-Version, X-DC-Response-Time, x-dc-hub-backend, x-dc-hub-source',
            'Access-Control-Max-Age': '86400',
          }});
        }

        let bodyText = null;
        let rpc = null;
        if (request.method === 'POST') {
          try { bodyText = await request.text(); rpc = JSON.parse(bodyText); } catch (e) { /* body read failed */ }
        }

        const rpcMethod = rpc?.method || '';
        const rpcId = rpc?.id || null;

        // ── v4.4.0: Enforce tier on tools/call ──
        if (rpcMethod === 'tools/call') {
          const enforcement = await enforceMcpTier(request, url, rpc, env);
          if (!enforcement.allowed) {
            const r = addCORS(enforcement.response, request);
            r.headers.delete('Mcp-Session-Id');
            r.headers.set('X-DC-Worker-Version', WORKER_VERSION);
            r.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
            r.headers.set('x-dc-hub-backend', 'worker-rate-limit');
            return r;
          }

          // Strip Mcp-Session-Id — stateless MCP, prevents stale session bugs
          const mcpHeaders = new Headers(request.headers);
          mcpHeaders.delete('Mcp-Session-Id');
          const mcpReq = new Request(request.url, { method: request.method, headers: mcpHeaders, body: bodyText });
          const mcpResp = await proxyToRailway(mcpReq, pathname, url.search, 0, getTimeout(pathname));

          if (mcpResp && mcpResp.status < 500) {
            // Gate the response based on tier
            const respText = await mcpResp.text();
            let respJson;
            try {
              respJson = JSON.parse(respText);
              respJson = gateResponse(respJson, rpc.params?.name || 'unknown', enforcement.tierInfo.config, enforcement.usage);
            } catch (e) {
              respJson = null;
            }

            const finalBody = respJson ? JSON.stringify(respJson) : respText;

            // Cache the ungated version
            const mcpKey = mcpCacheKey(bodyText);
            if (mcpKey && env.DCHUB_CACHE) {
              ctx.waitUntil(mcpCacheStore(env.DCHUB_CACHE, mcpKey, respText, 'application/json'));
            }

            const result = addCORS(new Response(finalBody, { status: mcpResp.status, headers: mcpResp.headers }), request);
            result.headers.delete('Mcp-Session-Id');
            result.headers.set('x-dc-hub-backend', 'railway');
            result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
            result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
            return result;
          }

          // Railway down — try KV cache for tools/call
          const mcpKey = mcpCacheKey(bodyText);
          if (mcpKey && env.DCHUB_CACHE) {
            const cached = await mcpCacheGet(env.DCHUB_CACHE, mcpKey, true);
            if (cached) {
              const result = addCORS(cached, request);
              result.headers.set('x-dc-hub-backend', 'kv-stale-cache');
              result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
              result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
              return result;
            }
          }
          return addCORS(json({
            jsonrpc: '2.0', id: rpcId,
            result: { content: [{ type: 'text', text: JSON.stringify({ error: 'Backend temporarily unavailable', message: 'DC Hub backend is down and no cached data is available for this query. Try again shortly.' }) }], isError: true },
          }), request);
        }

        // ── Non tools/call MCP methods ──
        // Strip Mcp-Session-Id for all methods
        const mcpHeaders = new Headers(request.headers);
        mcpHeaders.delete('Mcp-Session-Id');
        const mcpReq = bodyText !== null
          ? new Request(request.url, { method: request.method, headers: mcpHeaders, body: bodyText })
          : new Request(request, { headers: mcpHeaders });
        const mcpResp = await proxyToRailway(mcpReq, pathname, url.search, 0, getTimeout(pathname));

        if (mcpResp && mcpResp.status < 500) {
          let finalResp = mcpResp;
          // Sanitize tools/list and initialize responses
          if (rpcMethod === 'tools/list' || rpcMethod === 'initialize') {
            try {
              const respText = await mcpResp.text();
              const respJson = JSON.parse(respText);
              if (rpcMethod === 'tools/list' && respJson.result?.tools) {
                for (const tool of respJson.result.tools) {
                  delete tool.outputSchema;
                  delete tool.annotations;
                  delete tool.title;
                }
              }
              if (rpcMethod === 'initialize' && respJson.result?.capabilities) {
                delete respJson.result.capabilities.logging;
              }
              const sanitized = JSON.stringify(respJson);
              finalResp = new Response(sanitized, { status: mcpResp.status, headers: mcpResp.headers });
              const mcpKey = mcpCacheKey(bodyText);
              if (mcpKey && env.DCHUB_CACHE) ctx.waitUntil(mcpCacheStore(env.DCHUB_CACHE, mcpKey, sanitized, 'application/json'));
            } catch (e) {
              finalResp = new Response(mcpResp.body, mcpResp);
            }
          } else {
            const mcpKey = mcpCacheKey(bodyText);
            if (mcpKey && env.DCHUB_CACHE) {
              const clonedResp = mcpResp.clone();
              ctx.waitUntil((async () => { try { const b = await clonedResp.text(); await mcpCacheStore(env.DCHUB_CACHE, mcpKey, b, clonedResp.headers.get('content-type') || 'application/json'); } catch (e) {} })());
            }
          }

          const result = addCORS(new Response(finalResp.body, finalResp), request);
          result.headers.delete('Mcp-Session-Id');
          result.headers.set('x-dc-hub-backend', 'railway');
          result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
          return result;
        }

        // Railway DOWN — local MCP fallback
        if (rpcMethod === 'initialize') {
          const r = addCORS(json({ jsonrpc: '2.0', id: rpcId, result: { protocolVersion: '2024-11-05', capabilities: { tools: { listChanged: false } }, serverInfo: { name: 'DC Hub Intelligence (cached)', version: WORKER_VERSION + '-fallback' } } }), request);
          r.headers.set('x-dc-hub-backend', 'worker-fallback');
          r.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          return r;
        }
        if (rpcMethod.startsWith('notifications/')) return addCORS(new Response('', { status: 200, headers: { 'Content-Type': 'application/json' } }), request);
        if (rpcMethod === 'ping') return addCORS(json({ jsonrpc: '2.0', id: rpcId, result: {} }), request);
        if (rpcMethod === 'tools/list') {
          const r = addCORS(json({ jsonrpc: '2.0', id: rpcId, result: { tools: MCP_FALLBACK_TOOLS } }), request);
          r.headers.set('x-dc-hub-backend', 'worker-fallback');
          r.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          return r;
        }
        if (rpcMethod === 'prompts/list') return addCORS(json({ jsonrpc: '2.0', id: rpcId, result: { prompts: [] } }), request);
        if (rpcMethod === 'resources/list') return addCORS(json({ jsonrpc: '2.0', id: rpcId, result: { resources: [] } }), request);

        return addCORS(json({ jsonrpc: '2.0', id: rpcId, error: { code: -32601, message: `Method not found: ${rpcMethod}` }, worker_version: WORKER_VERSION }, 200), request);
      }

      // Discovery paths → Railway
      if (isDiscoveryPath(pathname)) {
        const resp = await proxyToRailway(request, pathname, url.search, 3600, 10000);
        if (resp) return addCORS(new Response(resp.body, resp), request);
      }

      // Pages passthrough
      const pagesResp = await fetch(request);
      const contentType = pagesResp.headers.get('content-type') || '';
      if (contentType.includes('text/html')) {
        const resp = new Response(pagesResp.body, pagesResp);
        resp.headers.set('Cache-Control', 'public, max-age=120, stale-while-revalidate=300');
        resp.headers.set('Link', ['<https://dchub-backend-production.up.railway.app>; rel=preconnect', '<https://unpkg.com>; rel=preconnect', '<https://fonts.googleapis.com>; rel=preconnect', '<https://fonts.gstatic.com>; rel=preconnect; crossorigin'].join(', '));
        resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        return resp;
      }
      if (url.search.includes('v=') || url.pathname.match(/\.[0-9a-f]{8,}\.(js|css)$/)) {
        const resp = new Response(pagesResp.body, pagesResp);
        resp.headers.set('Cache-Control', 'public, max-age=31536000, immutable');
        return resp;
      }
      if (url.pathname.match(/\.(png|jpg|jpeg|gif|webp|svg|ico|woff2?|ttf|eot)$/i)) {
        const resp = new Response(pagesResp.body, pagesResp);
        resp.headers.set('Cache-Control', 'public, max-age=86400, stale-while-revalidate=604800');
        return resp;
      }
      if (url.pathname.match(/\.(js|css)$/i)) {
        const resp = new Response(pagesResp.body, pagesResp);
        resp.headers.set('Cache-Control', 'public, max-age=300, stale-while-revalidate=600');
        return resp;
      }
      return pagesResp;
    }

    // ================================================================
    // API ROUTES
    // ================================================================
    if (request.method === 'OPTIONS') return handleCORS(request);

    const isGet = request.method === 'GET';
    const tier = getRouteTier(pathname);
    const timeoutMs = getTimeout(pathname);
    const hasApiKey = request.headers.get('X-API-Key') || url.searchParams.get('api_key');

    // ── v4.4.0: API key management routes ──
    if (pathname === '/api/admin/create-api-key' && request.method === 'POST') {
      const adminKey = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';
      if (!adminKey) return addCORS(json({ error: 'X-Admin-Key required' }, 401), request);
      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);
      return addCORS(await handleCreateApiKey(request, env), request);
    }
    if (pathname === '/api/admin/usage' && isGet) {
      const adminKey = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';
      if (!adminKey) return addCORS(json({ error: 'X-Admin-Key required' }, 401), request);
      return addCORS(await handleUsageCheck(request, url, env), request);
    }
    if (pathname === '/api/admin/revoke-api-key' && request.method === 'POST') {
      const adminKey = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';
      if (!adminKey) return addCORS(json({ error: 'X-Admin-Key required' }, 401), request);
      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);
      return addCORS(await handleRevokeApiKey(request, env), request);
    }
    if (pathname === '/api/stripe/mcp-webhook' && request.method === 'POST') {
      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);
      return addCORS(await handleStripeWebhook(request, env), request);
    }

    // ── Cache status ──
    if (pathname === '/api/cache/status' && env.DCHUB_CACHE) {
      const list = await env.DCHUB_CACHE.list({ prefix: 'kv:', limit: 50 });
      const mcpList = await env.DCHUB_CACHE.list({ prefix: 'mcp:', limit: 50 });
      const keys = [];
      for (const k of list.keys) { const raw = await env.DCHUB_CACHE.get(k.name); let age = null; if (raw) { try { const e = JSON.parse(raw); age = Math.round((Date.now() - e.ts) / 1000); } catch(e) {} } keys.push({ path: k.name.replace('kv:', ''), age_seconds: age, type: 'api' }); }
      for (const k of mcpList.keys) { const raw = await env.DCHUB_CACHE.get(k.name); let age = null; if (raw) { try { const e = JSON.parse(raw); age = Math.round((Date.now() - e.ts) / 1000); } catch(e) {} } keys.push({ path: k.name, age_seconds: age, type: 'mcp' }); }
      return addCORS(json({ cached_endpoints: keys.length, keys, worker_version: WORKER_VERSION }), request);
    }

    // ── Cache purge ──
    if (pathname === '/api/cache/purge' && request.method === 'POST' && env.DCHUB_CACHE) {
      const adminKey = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';
      if (!adminKey) return addCORS(json({ error: 'X-Admin-Key required' }, 401), request);
      const list = await env.DCHUB_CACHE.list({ prefix: 'kv:', limit: 200 });
      const mcpList = await env.DCHUB_CACHE.list({ prefix: 'mcp:', limit: 200 });
      let deleted = 0;
      for (const key of list.keys) { await env.DCHUB_CACHE.delete(key.name); deleted++; }
      for (const key of mcpList.keys) { await env.DCHUB_CACHE.delete(key.name); deleted++; }
      return addCORS(json({ success: true, purged: deleted, includes_mcp: true }), request);
    }

    // ── Admin: seed caches ──
    if ((pathname === '/api/admin/seed-api-cache' || pathname === '/api/admin/seed-mcp-cache') && env.DCHUB_CACHE) {
      const adminKey = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';
      if (!adminKey) return addCORS(json({ error: 'X-Admin-Key required' }, 401), request);
      const results = await seedApiCache(env.DCHUB_CACHE);
      return addCORS(json({ success: true, worker_version: WORKER_VERSION, ...results }), request);
    }

    // ── Version ──
    if (pathname === '/api/version' || pathname === '/api/v1/version') {
      return addCORS(json({ version: WORKER_VERSION, source: 'cloudflare-worker', backend: 'railway', mcp_tiers: Object.fromEntries(Object.entries(MCP_TIERS).map(([k, v]) => [k, { daily_limit: v.daily_limit, results_limit: v.results_limit }])), cron: '0 */6 * * * (every 6 hours)', timestamp: new Date().toISOString() }), request);
    }

    // ════════════════════════════════════════════════════════════
    // FOUR-STEP FAILOVER
    // ════════════════════════════════════════════════════════════

    // ── STEP 1: KV fresh cache ──
    if (isGet && !hasApiKey && env.DCHUB_CACHE && kvHasFreshCache(pathname)) {
      const kvResult = await kvCacheGet(env.DCHUB_CACHE, kvCacheKey(url.toString()), false, tier.kvFreshTtl, tier.kvStaleTtl);
      if (kvResult) {
        const resp = addCORS(kvResult.response, request);
        resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        resp.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        if (tier.browserMaxAge > 0) resp.headers.set('Cache-Control', `public, max-age=${tier.browserMaxAge}, stale-while-revalidate=${tier.browserMaxAge * 2}`);
        return resp;
      }
    }

    // ── STEP 2: Proxy to Railway ──
    const edgeTtl = (isGet && !hasApiKey) ? tier.edgeTtl : 0;
    const { resp, attempts } = await proxyWithRetry(request, pathname, url.search, edgeTtl, timeoutMs);

    if (resp && resp.status < 500) {
      let cacheClone = null;
      if (isGet && resp.status === 200 && env.DCHUB_CACHE && kvIsCacheable(pathname)) cacheClone = resp.clone();
      const result = addCORS(new Response(resp.body, resp), request);
      result.headers.set('x-dc-hub-backend', 'railway');
      result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
      result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
      result.headers.set('X-DC-Attempts', String(attempts));
      if (isGet && !hasApiKey && tier.browserMaxAge > 0) result.headers.set('Cache-Control', `public, max-age=${tier.browserMaxAge}, stale-while-revalidate=${tier.browserMaxAge * 2}`);
      if (cacheClone) ctx.waitUntil((async () => { const body = await cacheClone.text(); await kvCacheStore(env.DCHUB_CACHE, kvCacheKey(url.toString()), body, cacheClone.headers.get('content-type') || 'application/json', tier.kvStaleTtl); })());
      return result;
    }

    // ── STEP 3: Stale KV ──
    if (isGet && env.DCHUB_CACHE && kvIsCacheable(pathname)) {
      const kvResult = await kvCacheGet(env.DCHUB_CACHE, kvCacheKey(url.toString()), true, tier.kvFreshTtl, tier.kvStaleTtl);
      if (kvResult) {
        const staleResp = addCORS(kvResult.response, request);
        staleResp.headers.set('x-dc-hub-source', 'kv-stale-cache');
        staleResp.headers.set('X-Failover-Mode', hasApiKey ? 'stale-authenticated' : 'stale-anonymous');
        staleResp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        staleResp.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        return staleResp;
      }
    }

    // ── STEP 4: 503 ──
    const errResp = addCORS(json({ error: 'Service temporarily unavailable', message: 'Backend unreachable and no cached data available. Please retry shortly.', status: 503, worker_version: WORKER_VERSION, tip: 'If this persists, check https://dchub.cloud/api/health' }, 503), request);
    errResp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
    errResp.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
    return errResp;
  }
};
