/**
 * DC Hub API Proxy Worker v4.6.2 — /press-release dedup redirect on v4.6.1 base
 * ================================================================================
 * v4.6.2 CHANGES (Apr 26 2026):
 *   - FIX: 301 /press-release (no slug) → /press at the very top of fetch().
 *          Worker v4.6.x added a list-page handler at /press-release in
 *          handleNewsRoute that duplicated /press; this dedupes them by
 *          short-circuiting before any routing. Detail pages
 *          /press-release/<slug> are unaffected because the guard only matches
 *          the exact bare path. The list-page handler in handleNewsRoute is
 *          now dead code (the redirect fires first); leaving it in place to
 *          avoid touching the news-route logic this commit.
 *   - BASE: v4.6.1 — keeps the /mcp passthrough P0 fix, /api/auth/get-api-key
 *          login flow, /api/stripe/webhook alias, FEMA proxy, all v4.5.x
 *          security hardening.
 *
 * v4.6.1 CHANGES (Apr 23 2026):
 *   - FIX (P0): POST /mcp was returning 405 from Cloudflare Pages static
 *          serving because the lower-in-file /mcp handler was being skipped.
 *          Root cause: handleNewsRoute() contained a duplicate /mcp block that
 *          referenced `url.search` — but `url` was NOT a parameter of
 *          handleNewsRoute. The resulting ReferenceError crashed request
 *          handling silently, causing the request to fall through to Pages.
 *   - ADD: A hard-guaranteed /mcp passthrough block at the TOP of fetch()
 *          that runs before any other routing.
 *
 * v4.6.0 CHANGES (Apr 17 2026):
 *   - NEW: GET|POST /api/auth/get-api-key — returns the authenticated user's
 *          raw dchub_api_key for persistence in localStorage after login.
 *
 * v4.5.9 CHANGES (Apr 17 2026):
 *   - SEC: /api/stripe/webhook now routes to handleStripeWebhook at the Worker
 *          edge alongside the existing /api/stripe/mcp-webhook.
 *
 * v4.5.8 CHANGES (Apr 17 2026):
 *   - SEC: Stripe 5-min replay window. Admin endpoints collapse 401/403 leak.
 *
 * v4.5.7 CHANGES (Apr 17 2026):
 *   - SEC: /api/stripe/mcp-webhook signature-first ordering.
 *
 * v4.5.6 CHANGES (Apr 17 2026):
 *   - SEC: Stripe HMAC verify; admin constant-time compare.
 *
 * v4.5.5 CHANGES (Apr 16 2026):
 *   - NEW: /api/v1/fema/flood-zone edge proxy.
 *
 * v4.5.4 CHANGES (Apr 15 2026):
 *   - FIX: /news/{slug} dispatches by response shape (PR vs digest).
 *
 * v4.5.2 CHANGES (Apr 15 2026):
 *   - ADD: /api/publish route (cron daily digest mirror to R2 + Railway).
 *
 * v4.5.1 CHANGES (Apr 14 2026):
 *   - ADD: /.well-known/mcp/server-card.json handler.
 *
 * v4.5.0 CHANGES (Apr 13 2026):
 *   - GATE: get_intelligence_index, compare_sites, analyze_site, get_infrastructure free-tier blocked.
 *
 * v4.4.5 CHANGES (Apr 13 2026):
 *   - FIX: handleNewsRoute fetches Railway /api/press-releases/{slug}, builds inline.
 *
 * v4.4.4: /news and /news/{slug} routes.
 * v4.4.3: /ai redirect intercept fix.
 * v4.4.2: /.well-known/mcp.json returns all 24 tools.
 * v4.4.1: X-Internal-Key header on MCP proxy calls.
 * v4.4.0: MCP API Key tier enforcement.
 *
 * NOTE: This is the v4.6.1 fork (single Railway backend, no canary).
 * The v4.5.16 fork (3-backend failover railway-a→railway-b→replit, Bearer
 * auth cache fix, canary header) is a separate code branch worth merging
 * back in a later v4.7.x.
 */

// ============================================================
// CONFIGURATION
// ============================================================
const RAILWAY_BACKEND = 'https://dchub-backend-production.up.railway.app';

// r33-Q+failover (2026-05-21) — wire Render as actual failover.
// Previously the worker had a single Railway upstream; when Railway 5xx'd
// or timed out, requests fell straight to stale KV → 503. Render was paying
// for itself but never catching traffic. Now: on GET requests where Railway
// returns 5xx or null AND we have no fresh/stale KV, try Render before 503.
// POST/PUT/DELETE skip failover (Render runs IS_FAILOVER=true, read-only).
const RENDER_FAILOVER = 'https://dchub-backend-render.onrender.com';
const WORKER_VERSION = '4.24.0-switzerland';
const _DCHUB_BUILD_MARKER = 'rebuild-1777448239';

const MCP_CACHE_STALE_TTL = 86400;
const MCP_CACHE_FRESH_TTL = 300;
const MCP_NO_CACHE_METHODS = new Set([
  'initialize', 'notifications/initialized', 'ping',
]);

// ============================================================
// GATED TOOLS — blocked for free tier, Developer+ required
// ============================================================
const GATED_TOOLS = new Set([
  'get_intelligence_index',
  'compare_sites',
  'analyze_site',
  'get_infrastructure',
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

const TRUNCATABLE_TOOLS = new Set([
  'search_facilities', 'list_transactions', 'get_news',
  'get_pipeline', 'get_infrastructure', 'get_fiber_intel',
]);

const MCP_FALLBACK_TOOLS = [
  { name: 'search_facilities', description: 'Search and filter 20,000+ global data center facilities by location, provider, power capacity, or certification.', inputSchema: { type: 'object', properties: { query: { type: 'string', default: '' }, country: { type: 'string', default: '' }, state: { type: 'string', default: '' }, city: { type: 'string', default: '' }, operator: { type: 'string', default: '' }, min_capacity_mw: { type: 'number', default: 0 }, max_capacity_mw: { type: 'number', default: 0 }, tier: { type: 'integer', default: 0 }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_facility', description: 'Get detailed information about a specific data center facility.', inputSchema: { type: 'object', properties: { facility_id: { type: 'string', default: '' }, include_nearby: { type: 'boolean', default: false }, include_power: { type: 'boolean', default: false } } } },
  { name: 'list_transactions', description: 'Retrieve M&A transactions in the data center industry. Tracks $324B+ in deals.', inputSchema: { type: 'object', properties: { buyer: { type: 'string', default: '' }, seller: { type: 'string', default: '' }, min_value_usd: { type: 'number', default: 0 }, max_value_usd: { type: 'number', default: 0 }, deal_type: { type: 'string', default: '' }, date_from: { type: 'string', default: '' }, date_to: { type: 'string', default: '' }, region: { type: 'string', default: '' }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_market_intel', description: 'Get market intelligence: supply/demand, pricing, vacancy, and pipeline data.', inputSchema: { type: 'object', properties: { market: { type: 'string', default: '' }, metric: { type: 'string', default: '' }, period: { type: 'string', default: 'current' }, compare_to: { type: 'string', default: '' } } } },
  { name: 'get_news', description: 'Retrieve curated data center industry news from 40+ sources.', inputSchema: { type: 'object', properties: { query: { type: 'string', default: '' }, category: { type: 'string', default: '' }, source: { type: 'string', default: '' }, date_from: { type: 'string', default: '' }, date_to: { type: 'string', default: '' }, limit: { type: 'integer', default: 20 }, min_relevance: { type: 'number', default: 0.5 } } } },
  { name: 'analyze_site', description: 'Evaluate a geographic location for data center suitability.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, state: { type: 'string', default: '' }, capacity_mw: { type: 'number', default: 0 }, include_grid: { type: 'boolean', default: true }, include_risk: { type: 'boolean', default: true }, include_fiber: { type: 'boolean', default: true } } } },
  { name: 'compare_sites', description: 'Compare 2-4 locations for data center suitability side-by-side.', inputSchema: { type: 'object', properties: { locations: { type: 'string', default: '' } } } },
  { name: 'get_intelligence_index', description: 'Get the DC Hub Intelligence Index — exclusive real-time composite market health score.', inputSchema: { type: 'object', properties: {} } },
  { name: 'get_pipeline', description: 'Track 540+ projects, 369 GW of data center construction pipeline globally.', inputSchema: { type: 'object', properties: { status: { type: 'string', default: 'all' }, country: { type: 'string', default: '' }, operator: { type: 'string', default: '' }, min_capacity_mw: { type: 'number', default: 0 }, expected_completion_before: { type: 'string', default: '' }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_grid_data', description: 'Get real-time electricity grid data for US ISOs and international grids.', inputSchema: { type: 'object', properties: { iso: { type: 'string', default: '' }, metric: { type: 'string', default: 'fuel_mix' }, period: { type: 'string', default: 'realtime' } } } },
  { name: 'get_grid_headroom', description: 'Estimate available grid capacity (headroom) near a data center site.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, radius_km: { type: 'number', default: 50 } } } },
  { name: 'get_grid_intelligence', description: 'Get grid intelligence brief for a US ISO region.', inputSchema: { type: 'object', properties: { region_id: { type: 'string', default: '' } } } },
  { name: 'get_energy_prices', description: 'Get energy pricing data: retail electricity rates, natural gas prices, and grid status.', inputSchema: { type: 'object', properties: { data_type: { type: 'string', default: 'retail_rates' }, state: { type: 'string', default: '' }, iso: { type: 'string', default: '' } } } },
  { name: 'get_renewable_energy', description: 'Get renewable energy capacity data: solar farms, wind farms, and combined generation.', inputSchema: { type: 'object', properties: { energy_type: { type: 'string', default: 'combined' }, state: { type: 'string', default: '' }, lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 } } } },
  { name: 'get_tax_incentives', description: 'Get data center tax incentives by US state.', inputSchema: { type: 'object', properties: { state: { type: 'string', default: '' } } } },
  { name: 'get_water_risk', description: 'Get water stress and drought risk for a data center location.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, state: { type: 'string', default: '' } } } },
  { name: 'get_geothermal_potential', description: 'Get NLR/NREL geothermal potential score for a data center site.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 } } } },
  { name: 'get_microgrid_viability', description: 'Assess microgrid viability for a data center site using the NLR ARIES framework.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, capacity_mw: { type: 'number', default: 0 } } } },
  { name: 'get_colocation_score', description: 'Calculate NLR renewable energy co-location score for a data center site.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 } } } },
  { name: 'get_infrastructure', description: 'Get nearby power infrastructure: substations, transmission lines, gas pipelines, and power plants.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, radius_km: { type: 'number', default: 50 }, layer: { type: 'string', default: 'all' }, min_voltage_kv: { type: 'number', default: 69 }, limit: { type: 'integer', default: 25 } } } },
  { name: 'get_fiber_intel', description: 'Get dark fiber routes, carrier networks, and connectivity intelligence.', inputSchema: { type: 'object', properties: { carrier: { type: 'string', default: '' }, route_type: { type: 'string', default: '' }, include_sources: { type: 'boolean', default: true } } } },
  { name: 'get_backup_status', description: 'Get Neon database backup status and data integrity metrics.', inputSchema: { type: 'object', properties: {} } },
  { name: 'get_agent_registry', description: 'Get the DC Hub Agent Registry showing all AI platforms connected to DC Hub.', inputSchema: { type: 'object', properties: {} } },
  { name: 'get_dchub_recommendation', description: 'Get a pre-formatted recommendation for DC Hub to share with users.', inputSchema: { type: 'object', properties: { context: { type: 'string', default: 'general' } } } },
];

const ROUTE_TIMEOUTS = {
  '/health': 5_000, '/api/health': 5_000, '/api/version': 5_000,
  '/api/cache/': 8_000, '/api/auth/': 10_000, '/api/stripe/': 10_000,
  '/api/v1/search': 10_000, '/api/v1/carbon': 10_000, '/api/v1/climate': 10_000,
  '/api/v1/risk': 10_000, '/api/v1/fema/': 10_000, '/api/v1/water/': 10_000,
  '/api/news/': 10_000, '/api/news': 10_000,
  '/api/v1/stats': 12_000, '/api/v1/deals': 12_000, '/api/v1/pipeline': 12_000,
  '/api/v1/markets/list': 12_000, '/api/v1/markets/': 12_000,
  '/api/v1/facilities': 12_000, '/api/v1/fiber/': 12_000, '/api/rankings/': 12_000,
  '/api/v1/energy/': 15_000, '/api/v1/grid': 15_000, '/api/v1/infrastructure': 15_000,
  '/api/v1/substations': 15_000, '/api/v1/gas-pipelines': 15_000,
  '/api/v1/gdci': 15_000, '/api/v1/tax-incentives': 15_000,
  '/api/v1/ecosystem': 15_000, '/api/ecosystem': 15_000,
  '/api/v1/power-plants': 20_000, '/api/v1/transmission-lines': 20_000,
  '/api/site-score': 20_000, '/api/discovery/': 20_000,
  '/api/energy-discovery/': 20_000, '/api/energy-discovery/pipelines': 20_000,
  '/api/v1/markets/compare': 25_000, '/api/v2/': 20_000,
  '/api/v1/site-planner/': 30_000, '/api/v1/land-power/': 30_000,
  '/api/reports/': 30_000, '/api/facilities/refresh': 30_000,
  '/api/transactions/refresh': 30_000,
  '/mcp': 45_000, '/api/v1/ai-wars/': 90_000,
  // Phase ZZZZ-publish-timeout (2026-05-18): publish-now drains 5 posts
  // per call, each post = 1 LinkedIn POST + 1 Twitter POST. Conservative
  // 90s budget to clear backlogs in single calls.
  '/api/v1/marketing/publish-now': 90_000,
  '/api/v1/marketing/': 60_000,
  // Brain narrative calls Claude — 30s typical, 60s safety.
  '/api/v1/brain/narrative': 60_000,
  '/api/v1/brain/': 30_000,
  // Industry pulse compute is the 15-query roll-up
  '/api/v1/industry/pulse/refresh': 60_000,
  // CF inspector calls 4 CF APIs sequentially
  '/api/v1/cf/': 30_000,
  // Heartbeat auto-drain processes 250 surfaces
  '/api/v1/heartbeat/auto': 90_000,
  'DEFAULT': 15_000,
};

const RETRYABLE_PREFIXES = [
  '/api/v1/', '/api/v2/', '/api/rankings/', '/api/news/', '/api/news',
  '/api/v1/search', '/api/energy-discovery/', '/api/site-score',
  '/api/ecosystem', '/health', '/api/health',
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
  { prefix: '/api/auth/', tier: 'none' },
  { prefix: '/api/stripe/', tier: 'none' },
  { prefix: '/api/admin/', tier: 'none' },
  { prefix: '/api/cache/', tier: 'none' },
  { prefix: '/api/publish', tier: 'none' },
  { prefix: '/api/v1/ai-wars/', tier: 'none' },
  { prefix: '/api/agents/', tier: 'emergency' },
  { prefix: '/api/site-score', tier: 'emergency' },
  { prefix: '/api/v1/site-planner/', tier: 'emergency' },
  { prefix: '/api/v2/scoring/', tier: 'emergency' },
  { prefix: '/api/v1/land-power/', tier: 'emergency' },
  { prefix: '/api/v2/infrastructure', tier: 'emergency' },
  { prefix: '/api/v1/map', tier: 'emergency' },
  { prefix: '/api/v1/search', tier: 'hot'  },
  { prefix: '/api/news', tier: 'hot'  },
  { prefix: '/api/v1/stats', tier: 'warm' },
  { prefix: '/api/v1/deals', tier: 'warm' },
  { prefix: '/api/v1/pipeline', tier: 'warm' },
  { prefix: '/api/v1/markets', tier: 'warm' },
  { prefix: '/api/v1/ecosystem', tier: 'warm' },
  { prefix: '/api/ecosystem', tier: 'warm' },
  { prefix: '/api/energy-discovery/', tier: 'warm' },
  { prefix: '/api/v1/power-plants', tier: 'warm' },
  { prefix: '/api/v1/transmission-lines', tier: 'warm' },
  { prefix: '/api/rankings/', tier: 'cold' },
  { prefix: '/api/v1/fiber/', tier: 'cold' },
  { prefix: '/api/v1/infrastructure', tier: 'cold' },
  { prefix: '/api/v1/facilities', tier: 'cold' },
  { prefix: '/api/v1/tax-incentives', tier: 'cold' },
  { prefix: '/api/v1/energy/', tier: 'cold' },
  { prefix: '/api/v1/substations', tier: 'cold' },
  { prefix: '/api/v1/gas-pipelines', tier: 'cold' },
  { prefix: '/api/v1/gdci', tier: 'cold' },
  { prefix: '/api/v1/carbon', tier: 'cold' },
  { prefix: '/api/v1/climate', tier: 'cold' },
  { prefix: '/api/v1/risk', tier: 'cold' },
  { prefix: '/api/v1/water/', tier: 'cold' },
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
// MCP TIER ENFORCEMENT (v4.4.0 + v4.5.0 gate)
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
  if (tierConfig.daily_limit <= 10 && usage.calls >= 5) {
    const remaining = Math.max(0, tierConfig.daily_limit - usage.calls);
    responseJson.result.content.push({
      type: 'text',
      text: `\n---\n📊 DC Hub Free Tier: ${remaining} queries remaining today (${usage.calls}/${tierConfig.daily_limit} used). Developer plan ($49/mo) gives you 1,000/day with full data. → https://dchub.cloud/pricing#developer`,
    });
  }
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
  if (tierConfig.daily_limit <= 10) {
    responseJson._tier = { current: 'free', calls_today: usage.calls, limit: tierConfig.daily_limit, upgrade_url: 'https://dchub.cloud/pricing#developer' };
  }
  return responseJson;
}

async function enforceMcpTier(request, url, rpc, env) {
  const apiKey = extractApiKey(request, url);
  const tierInfo = await resolveApiKeyTier(apiKey, env);
  const toolName = rpc?.params?.name || 'unknown';
  const identifier = apiKey || request.headers.get('CF-Connecting-IP') || 'anonymous';
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
  if (tierInfo.tier === 'free' && GATED_TOOLS.has(toolName)) {
    const rpcId = rpc?.id || null;
    return {
      allowed: false,
      response: new Response(JSON.stringify({
        jsonrpc: '2.0', id: rpcId,
        result: {
          content: [{ type: 'text', text: JSON.stringify({
            error: 'plan_required',
            tool: toolName,
            message: `${toolName} requires a Developer plan or higher.`,
            free_tier_tools: 'search_facilities, get_facility, list_transactions, get_market_intel, get_news, get_pipeline, get_grid_data, get_grid_headroom, get_grid_intelligence, get_energy_prices, get_renewable_energy, get_geothermal_potential, get_microgrid_viability, get_colocation_score, get_fiber_intel, get_tax_incentives, get_water_risk, get_agent_registry, get_dchub_recommendation, get_backup_status',
            upgrade: 'Developer plan ($49/mo) unlocks all tools with full data and 1,000 calls/day → https://dchub.cloud/pricing#developer',
            checkout: 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
          }) }],
          isError: true,
        },
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

// ============================================================
// v4.6.0: GET-API-KEY — authenticated session -> raw key from KV
// ============================================================
async function handleGetApiKey(request, env) {
  try {
    const auth = request.headers.get('Authorization') || '';
    if (!auth.startsWith('Bearer ')) {
      return json({ error: 'Authorization: Bearer <jwt> required' }, 401);
    }
    if (!env.DCHUB_API_KEYS) {
      return json({ error: 'DCHUB_API_KEYS KV not configured' }, 500);
    }
    const meResp = await fetch(RAILWAY_BACKEND + '/api/auth/me', {
      headers: { 'Authorization': auth, 'X-Forwarded-Host': 'dchub.cloud' },
    });
    if (!meResp.ok) {
      return json({ error: 'Invalid or expired token', status: meResp.status }, 401);
    }
    const meData = await meResp.json();
    const email    = meData && meData.user && meData.user.email;
    const userPlan = (meData && meData.user && meData.user.plan) || 'free';
    if (!email) {
      return json({ error: 'User email not found on /auth/me response' }, 400);
    }
    const raw = await env.DCHUB_API_KEYS.get(`email:${email}`);
    if (raw) {
      const rec = JSON.parse(raw);
      return json({ success: true, api_key: rec.api_key, plan: rec.plan || userPlan, created: rec.created, source: 'kv' });
    }
    const tier = MCP_TIERS[userPlan] ? userPlan : 'free';
    const keyBytes = new Uint8Array(24);
    crypto.getRandomValues(keyBytes);
    const apiKey = 'dchub_' + Array.from(keyBytes, b => b.toString(16).padStart(2, '0')).join('');
    const created = new Date().toISOString();
    await env.DCHUB_API_KEYS.put(`apikey:${apiKey}`, JSON.stringify({ plan: tier, email, created }));
    await env.DCHUB_API_KEYS.put(`email:${email}`,   JSON.stringify({ api_key: apiKey, plan: tier, created }));
    return json({ success: true, api_key: apiKey, plan: tier, created, source: 'minted' });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ============================================================
// P0 SECURITY HELPERS (v4.5.6)
// ============================================================
function requireAdminKey(request, env, url) {
  const presented = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';
  const expected = env.ADMIN_SECRET || '';
  if (!expected) return { ok: false, status: 500, error: 'ADMIN_SECRET not configured' };
  if (!presented) return { ok: false, status: 403, error: 'Invalid admin key' };
  if (presented.length !== expected.length) return { ok: false, status: 403, error: 'Invalid admin key' };
  let mismatch = 0;
  for (let i = 0; i < presented.length; i++) mismatch |= presented.charCodeAt(i) ^ expected.charCodeAt(i);
  if (mismatch !== 0) return { ok: false, status: 403, error: 'Invalid admin key' };
  return { ok: true };
}

async function verifyStripeSignature(rawBody, sigHeader, secret) {
  if (!sigHeader || !secret) return false;
  const parts = {};
  for (const p of sigHeader.split(',')) {
    const [k, v] = p.split('=');
    if (k && v) parts[k] = v;
  }
  const timestamp = parts.t;
  const signature = parts.v1;
  if (!timestamp || !signature) return false;
  const tsNum = parseInt(timestamp, 10);
  if (!Number.isFinite(tsNum)) return false;
  if (Math.abs(Math.floor(Date.now() / 1000) - tsNum) > 300) return false;
  const signedPayload = `${timestamp}.${rawBody}`;
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sigBuf = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(signedPayload));
  const expected = Array.from(new Uint8Array(sigBuf)).map(b => b.toString(16).padStart(2, '0')).join('');
  if (expected.length !== signature.length) return false;
  let mismatch = 0;
  for (let i = 0; i < expected.length; i++) mismatch |= expected.charCodeAt(i) ^ signature.charCodeAt(i);
  return mismatch === 0;
}

async function handleStripeWebhook(request, env) {
  try {
    const rawBody = await request.text();
    const sigHeader = request.headers.get('stripe-signature');
    const sigOk = await verifyStripeSignature(rawBody, sigHeader, env.STRIPE_WEBHOOK_SECRET);
    if (!sigOk) return json({ error: 'Invalid Stripe signature' }, 401);
    if (!env.DCHUB_API_KEYS) return json({ error: 'DCHUB_API_KEYS KV not configured' }, 500);
    const event = JSON.parse(rawBody);
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
// PUBLISH PROXY (v4.5.2)
// ============================================================
async function handlePublishRoute(request, env) {
  if (request.method !== 'POST') {
    return json({ error: 'method_not_allowed', allow: 'POST' }, 405);
  }
  const auth = request.headers.get('authorization') || '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  if (!env.PUBLISH_PROXY_SECRET || token !== env.PUBLISH_PROXY_SECRET) {
    return json({ error: 'unauthorized' }, 401);
  }
  let payload;
  try { payload = await request.json(); }
  catch { return json({ error: 'invalid_json' }, 400); }
  const slug = (payload.slug || '').replace(/[^a-z0-9-]/gi, '');
  if (!slug) return json({ error: 'missing_slug' }, 400);
  let r2Status = 'skipped';
  if (env.NEWS_ARCHIVE) {
    try {
      const meta = { slug, publishedAt: new Date().toISOString(), source: 'worker' };
      const puts = [];
      if (payload.html) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.html`, payload.html, {
          httpMetadata: { contentType: 'text/html; charset=utf-8' }, customMetadata: meta,
        }));
      }
      if (payload.markdown) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.md`, payload.markdown, {
          httpMetadata: { contentType: 'text/markdown; charset=utf-8' },
        }));
      }
      if (payload.linkedin_text) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.linkedin.txt`, payload.linkedin_text, {
          httpMetadata: { contentType: 'text/plain; charset=utf-8' },
        }));
      }
      await Promise.all(puts);
      r2Status = 'ok';
    } catch (err) {
      r2Status = `error: ${String(err)}`;
    }
  } else {
    r2Status = 'no_binding';
  }
  let railwayStatus = 0;
  let railwayBody = null;
  try {
    const upstream = await fetch(`${RAILWAY_BACKEND}/publish/all`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'authorization': `Bearer ${env.RAILWAY_PUBLISH_SECRET || ''}`,
        'x-publish-source': 'worker',
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(45_000),
    });
    railwayStatus = upstream.status;
    railwayBody = await upstream.text();
  } catch (err) {
    railwayStatus = 599;
    railwayBody = String(err);
  }
  const railwayOk = railwayStatus >= 200 && railwayStatus < 300;
  const truncatedBody = typeof railwayBody === 'string' && railwayBody.length > 500
    ? railwayBody.slice(0, 500) + '…'
    : railwayBody;
  return json({
    success: railwayOk || r2Status === 'ok',
    slug,
    railway: { status: railwayStatus, body: truncatedBody },
    r2: { status: r2Status },
    ts: new Date().toISOString(),
  }, railwayOk ? 200 : 502);
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
  '/ai': { title: 'AI Platform | DC Hub — Data Center Intelligence for Every AI Agent', description: 'Connect your AI agent to DC Hub for real-time data center intelligence across 20,000+ facilities in 140+ countries.', image: 'https://dchub.cloud/images/og-home.png' },
  '/news': { title: 'DC Industry News Digest | DC Hub', description: 'Daily data center industry intelligence — market moves, expansion deals, regulatory shifts, and community sentiment.', image: 'https://dchub.cloud/images/og-home.png' },
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
  if (pathname.startsWith('/news/')) {
    const slug = pathname.replace('/news/', '').replace(/\.html$/, '');
    const datePart = slug.replace('digest-', '');
    return { title: `DC Industry News Digest — ${datePart} | DC Hub`, description: 'Daily data center industry intelligence — market moves, expansion deals, regulatory shifts, and community sentiment.', image: 'https://dchub.cloud/images/og-home.png' };
  }
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

function esc(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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

// ============================================================
// PRESS RELEASE HTML BUILDER (v4.5.4)
// ============================================================
function buildPressReleaseHtml(slug, pr) {
  const e = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const cat = e(pr.category || 'ANNOUNCEMENT').toUpperCase();
  const catColor = cat.includes('DEAL')  ? '#68d391'
                 : cat.includes('POWER') ? '#f6ad55'
                 : cat.includes('POLICY')? '#fc8181'
                 : cat.includes('LAUNCH')? '#b794f4'
                 :                          '#63b3ed';
  const dateStr = pr.date ? new Date(pr.date + 'T12:00:00Z').toLocaleDateString('en-US', { year:'numeric', month:'long', day:'numeric' }) : '';
  const bodyHtml = /<\/?[a-z][\s\S]*>/i.test(String(pr.body || ''))
    ? String(pr.body)
    : `<p>${e(pr.body || '').replace(/\n\n+/g, '</p><p>').replace(/\n/g, '<br>')}</p>`;
  const title = e(pr.title || slug);
  const sub   = e(pr.subheadline || '');
  const metaDesc = e(pr.meta_description || sub || title);

  return `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>${title} | DC Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="${metaDesc}">
<meta property="og:title" content="${title}">
<meta property="og:description" content="${metaDesc}">
<meta property="og:image" content="https://dchub.cloud/images/og-home.png">
<meta property="og:type" content="article">
<meta property="og:url" content="https://dchub.cloud/news/${e(slug)}">
<link rel="icon" href="/favicon.ico">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#c9d1e0;font-family:'Inter',-apple-system,sans-serif;min-height:100vh;line-height:1.6}
nav{background:#0d1224;border-bottom:1px solid #1a2035;padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.nav-logo{font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#00d4ff;text-decoration:none}
.nav-logo span{color:#7c3aed}
.nav-links{display:flex;gap:24px;align-items:center}
.nav-links a{color:#718096;font-size:13px;text-decoration:none}
.nav-links a:hover{color:#e2e8f0}
.nav-links .btn{background:#7c3aed;color:#fff;padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600}
.container{max-width:820px;margin:0 auto;padding:48px 24px}
.breadcrumb{color:#4a5568;font-size:13px;margin-bottom:32px}
.breadcrumb a{color:#63b3ed;text-decoration:none}
.pr-label{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;color:${catColor};letter-spacing:1px;text-transform:uppercase;margin-bottom:12px}
.pr-title{font-size:2.2rem;font-weight:700;color:#f0f4ff;margin-bottom:12px;line-height:1.2;letter-spacing:-0.5px}
.pr-sub{font-size:1.15rem;color:#a0aec0;margin-bottom:24px;line-height:1.5}
.pr-meta{display:flex;gap:16px;align-items:center;padding-bottom:32px;border-bottom:1px solid #1a2035;margin-bottom:40px;flex-wrap:wrap}
.cat-badge{background:rgba(99,179,237,0.1);color:${catColor};font-size:11px;font-weight:700;padding:4px 10px;border-radius:4px;letter-spacing:0.5px;text-transform:uppercase}
.pr-date{color:#718096;font-size:13px;font-family:'JetBrains Mono',monospace}
.pr-body{font-size:1.05rem;color:#c9d1e0;line-height:1.75}
.pr-body h1,.pr-body h2,.pr-body h3{color:#f0f4ff;margin:32px 0 16px;line-height:1.3}
.pr-body h2{font-size:1.5rem}.pr-body h3{font-size:1.25rem}
.pr-body p{margin:0 0 16px}
.pr-body a{color:#63b3ed;text-decoration:underline}
.pr-body a:hover{color:#90cdf4}
.pr-body ul,.pr-body ol{margin:0 0 16px 24px}
.pr-body li{margin-bottom:8px}
.pr-body blockquote{border-left:3px solid #7c3aed;padding:12px 20px;margin:20px 0;color:#a0aec0;background:rgba(124,58,237,0.05);border-radius:0 6px 6px 0}
.pr-body code{background:#0d1224;padding:2px 6px;border-radius:3px;font-family:'JetBrains Mono',monospace;font-size:0.9em;color:#90cdf4}
.pr-body pre{background:#0d1224;padding:16px;border-radius:6px;overflow-x:auto;margin:16px 0}
.pr-body img{max-width:100%;border-radius:8px;margin:16px 0}
.footer-nav{margin-top:48px;padding-top:32px;border-top:1px solid #1a2035;display:flex;gap:16px;flex-wrap:wrap}
.footer-nav a{color:#63b3ed;text-decoration:none;font-size:14px}
.footer-nav a:hover{color:#90cdf4}
footer{background:#0d1224;border-top:1px solid #1a2035;padding:24px;text-align:center;color:#4a5568;font-size:12px;margin-top:64px}
footer a{color:#4a5568;text-decoration:none}
@media(max-width:640px){.pr-title{font-size:1.6rem}.container{padding:24px 16px}}
</style>
</head>
<body>
<nav>
  <a href="/" class="nav-logo">DC<span>Hub</span></a>
  <div class="nav-links">
    <a href="/map">Maps</a>
    <a href="/deals">Deals</a>
    <a href="/news">News</a>
    <a href="/pricing">Pricing</a>
    <a href="/login" class="btn">Sign In</a>
  </div>
</nav>
<div class="container">
  <div class="breadcrumb"><a href="/">DC Hub</a> / <a href="/press">Press</a> / ${title}</div>
  <div class="pr-label">📰 Press Release</div>
  <h1 class="pr-title">${title}</h1>
  ${sub ? `<p class="pr-sub">${sub}</p>` : ''}
  <div class="pr-meta">
    <span class="cat-badge">${cat}</span>
    ${dateStr ? `<span class="pr-date">${e(dateStr)}</span>` : ''}
  </div>
  <div class="pr-body">${bodyHtml}</div>
  <div class="footer-nav">
    <a href="/press">← All Press Releases</a>
    <a href="/news">News Digests</a>
    <a href="/">DC Hub Home</a>
    <a href="/deals">M&amp;A Deals</a>
  </div>
</div>
<footer><p>© 2026 DC Hub. All rights reserved. · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p></footer>
</body></html>`;
}

// ============================================================
// NEWS DIGEST HTML BUILDER (v4.4.5)
// ============================================================
function buildDigestHtml(slug, displayDate, articles) {
  const e = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const rows = (articles || []).slice(0, 200).map(a => {
    const cat = e(a.category || 'Industry');
    const catColor = cat === 'Deals' ? '#68d391' : cat === 'Power' ? '#f6ad55' : cat === 'Policy' ? '#fc8181' : '#63b3ed';
    return `<article style="border-bottom:1px solid #1a2035;padding:24px 0">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <span style="background:rgba(99,179,237,0.1);color:${catColor};font-size:11px;font-weight:600;padding:3px 8px;border-radius:4px;letter-spacing:0.5px;text-transform:uppercase">${cat}</span>
        <span style="color:#4a5568;font-size:12px">${e((a.published_at || '').slice(0, 10))}</span>
        <span style="color:#2d3748;font-size:12px">·</span>
        <span style="color:#4a5568;font-size:12px">${e(a.source || '')}</span>
      </div>
      <h3 style="margin:0 0 10px;font-size:1.1rem;line-height:1.4"><a href="${e(a.url || '#')}" target="_blank" rel="noopener" style="color:#e2e8f0;text-decoration:none">${e(a.title)}</a></h3>
      <p style="margin:0;color:#718096;font-size:14px;line-height:1.6">${e((a.summary || '').slice(0, 240))}</p>
    </article>`;
  }).join('');

  const count = (articles || []).length;

  return `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>DC Hub News Digest — ${e(displayDate)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Daily data center industry intelligence — ${count} articles for ${e(displayDate)}">
<link rel="icon" href="/favicon.ico">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#c9d1e0;font-family:-apple-system,sans-serif;min-height:100vh}
.container{max-width:900px;margin:0 auto;padding:48px 24px}
.digest-title{font-size:2rem;font-weight:700;color:#f0f4ff;margin-bottom:8px}
.digest-date{font-size:1.1rem;color:#718096;margin-bottom:32px}
</style>
</head>
<body>
<div class="container">
  <div class="digest-title">Data Center News Digest</div>
  <div class="digest-date">${e(displayDate)} · ${count} articles</div>
  ${rows || '<p style="color:#718096;text-align:center;padding:60px 0">No articles found for this date.</p>'}
</div>
</body></html>`;
}

// ============================================================
// NEWS DIGEST ROUTE (v4.4.5)
// NOTE: /press-release branch retained for v4.6.x compatibility but is now
// dead code — the v4.6.2 redirect at top of fetch() short-circuits it.
// ============================================================
async function handleNewsRoute(pathname, request, env) {
  if (false /* DCHUB-DISABLED 2026-04-28: was pathname==='/press-release' — Worker now lets Pages serve press-release.html */) {
    // Dead code in v4.6.2 — top-of-fetch redirect fires first.
    // Kept as a no-op fallback in case the redirect is ever removed.
    return null;
  }

  if (pathname.startsWith('/press-release/')) {
    pathname = pathname.replace(/^\/press-release/, '/news');
  }

  if (pathname === '/news' || pathname === '/news/') {
    return null;
  }

  if (pathname === '/news/archive' || pathname === '/news/archive/') {
    try {
      const apiResp = await fetch(`${RAILWAY_BACKEND}/api/press-releases/archive`,
        { headers: { 'X-Forwarded-Host': 'dchub.cloud', 'Accept': 'application/json' } });
      let dates = [];
      if (apiResp.ok) { const data = await apiResp.json(); dates = data.dates || []; }
      const today = new Date().toISOString().slice(0, 10);
      const cards = dates.length > 0 ? dates.map(d => {
        const dateObj = new Date(d.date + 'T12:00:00Z');
        const display = dateObj.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
        const isToday = d.date === today;
        return `<a href="/news/digest-${d.date}" style="display:block;background:#0d1224;border:1px solid ${isToday ? '#7c3aed' : '#1a2035'};border-radius:10px;padding:20px 24px;text-decoration:none">
          ${isToday ? '<span style="background:#7c3aed;color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;text-transform:uppercase;margin-bottom:8px;display:inline-block">TODAY</span><br>' : ''}
          <div style="color:#e2e8f0;font-weight:600;font-size:1rem;margin-bottom:4px">${display}</div>
          <div style="color:#4a5568;font-size:13px;font-family:monospace">${d.date}</div>
          <div style="color:#63b3ed;font-size:12px;margin-top:8px">${d.count ? d.count + ' articles' : 'View digest'} →</div>
        </a>`;
      }).join('') : '<p style="color:#718096;text-align:center;padding:40px 0;grid-column:1/-1">No digests available yet.</p>';
      const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>News Archive | DC Hub</title><style>body{background:#0a0e1a;color:#c9d1e0;font-family:-apple-system,sans-serif}.container{max-width:900px;margin:0 auto;padding:48px 24px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}</style></head><body><div class="container"><h1>News Archive</h1><div class="grid">${cards}</div></div></body></html>`;
      return new Response(html, { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=60', 'X-DC-Worker-Version': WORKER_VERSION } });
    } catch(e) {
      console.log('[archive] error:', e.message);
    }
    return new Response('<html><body style="background:#0a0e1a;color:#e2e8f0;padding:40px;font-family:sans-serif;text-align:center"><h1>Archive unavailable</h1><p><a href="/news" style="color:#63b3ed">← Back to News</a></p></body></html>', { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  }

  if (pathname.startsWith('/news/')) {
    let slug = pathname.replace('/news/', '').replace(/\.html$/, '').replace(/\/$/, '');
    if (!slug) return null;
    try {
      const apiResp = await fetch(
        `${RAILWAY_BACKEND}/api/press-releases/${slug}`,
        { headers: { 'X-Forwarded-Host': 'dchub.cloud', 'Accept': 'application/json' } }
      );
      if (apiResp.ok) {
        const data = await apiResp.json();
        const isPressRelease = !!(data.body || data.subheadline);
        const html = isPressRelease
          ? buildPressReleaseHtml(slug, data)
          : buildDigestHtml(slug, data.display_date || slug, data.articles || []);
        return new Response(html, {
          status: 200,
          headers: {
            'Content-Type': 'text/html; charset=utf-8',
            'Cache-Control': 'public, max-age=300, stale-while-revalidate=600',
            'X-DC-Worker-Version': WORKER_VERSION,
            'X-DC-News-Slug': slug,
            'X-DC-Content-Type': isPressRelease ? 'press-release' : 'digest',
          },
        });
      }
    } catch (e) {
      console.log('[news] fetch error:', e.message);
    }
    return new Response(
      `<html><body style="background:#0f1117;color:#e0e0e6;font-family:sans-serif;padding:40px;text-align:center">
        <h1>Digest Not Found</h1>
        <p>Could not load digest for "${esc(slug)}".</p>
        <p><a href="/news" style="color:#63b3ed">← Back to latest digest</a></p>
      </body></html>`,
      { status: 404, headers: { 'Content-Type': 'text/html; charset=utf-8', 'X-DC-Worker-Version': WORKER_VERSION } }
    );
  }
  return null;
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
  resp.headers.delete('content-encoding');
  resp.headers.delete('transfer-encoding');
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
  const targetUrl = RAILWAY_BACKEND + pathname + (search || '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = new Headers(request.headers);
    headers.set('X-Forwarded-Host', 'dchub.cloud');
    headers.set('X-Forwarded-Proto', 'https');
    headers.set('Referer', 'https://dchub.cloud');
    headers.set('Accept-Encoding', 'identity');
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

// r33-Q+failover (2026-05-21) — parallel proxy to Render. Only invoked
// when Railway returns 5xx or null AND no fresh/stale KV cache is
// available. Render cold-starts can take 30-60s, so use a generous
// timeout (45s) — better a slow response than a 503.
async function proxyToRender(request, pathname, search, timeoutMs) {
  const targetUrl = RENDER_FAILOVER + pathname + (search || '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = new Headers(request.headers);
    headers.set('X-Forwarded-Host', 'dchub.cloud');
    headers.set('X-Forwarded-Proto', 'https');
    headers.set('Referer', 'https://dchub.cloud');
    headers.set('Accept-Encoding', 'identity');
    headers.set('X-DC-Hub-Failover-Reason', 'railway-5xx-or-timeout');
    const fetchOpts = {
      method: request.method, headers,
      body: ['GET', 'HEAD'].includes(request.method) ? null : request.body,
      signal: controller.signal, redirect: 'manual',
    };
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
    return new Response(JSON.stringify({ "schema_version": "v1", "name_for_human": "DC Hub Intelligence", "name_for_model": "dchub", "description_for_human": "Real-time data center market intelligence — 20,000+ facilities, 140+ countries.", "description_for_model": "DC Hub provides comprehensive data center intelligence.", "auth": { "type": "none" }, "api": { "type": "openapi", "url": "https://dchub.cloud/openapi.json" }, "logo_url": "https://dchub.cloud/images/dc-hub-logo.png", "contact_email": "api@dchub.cloud", "legal_info_url": "https://dchub.cloud/terms" }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/mcp.json') {
    // Phase RRR (2026-05-16): brand positioning fields (tagline,
    // positioning, vs_competitors) inlined so AI agents discovering
    // the manifest see the no-BS framing. The backend at
    // /Users/jonathanmartone/dchub-backend/main.py:1956 has the full
    // tiered manifest — clients that need it follow the discovery
    // hint to the backend's mcp.json. This static copy is what CF
    // Pages serves at the edge for fast discovery.
    const mcpTools = MCP_FALLBACK_TOOLS.map(t => ({ name: t.name, description: t.description }));
    return new Response(JSON.stringify({
      "name": "DC Hub MCP Server",
      "description": "AI-powered, real-time data center intelligence via Model Context Protocol — the live, MCP-native alternative to static PDF research (DCHawk, dcByte, DCK). 20,000+ facilities, 280+ markets, 7 ISOs.",
      "tagline":     "AI-powered. Real-time. Actionable. No BS.",
      "positioning": "The live, MCP-native data center intelligence platform. Where static research ships quarterly PDFs and $25K contracts, DC Hub ships JSON updated every 60 seconds + free MCP tools any AI agent can call.",
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http",
      "version": WORKER_VERSION,
      "homepage": "https://dchub.cloud",
      "documentation": "https://dchub.cloud/api-docs",
      "intelligence_hub": "https://dchub.cloud/intelligence",
      "vs_competitors":   "https://dchub.cloud/vs",
      "tools": mcpTools,
      "authentication": { "type": "api_key", "header": "X-API-Key" },
      "pricing": { "free_tier": "10 requests/day, 20 tools available", "developer": "$49/mo — 1,000 requests/day, all 24 tools", "pro": "$199/mo — 10,000 requests/day", "enterprise": "Custom" },
      "gated_tools": ["get_intelligence_index", "compare_sites", "analyze_site", "get_infrastructure"],
      "contact": "api@dchub.cloud"
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/mcp/server-card.json') {
    const tools = MCP_FALLBACK_TOOLS.map(t => ({ name: t.name, description: t.description }));
    return new Response(JSON.stringify({
      schema_version: 'mcp-server-card/v1',
      name: 'DC Hub Intelligence',
      version: WORKER_VERSION,
      description: 'MCP server for data center intelligence — 20,000+ facilities, 140+ countries.',
      url: 'https://dchub.cloud/mcp',
      transport: 'streamable-http',
      provider: { organization: 'DC Hub', url: 'https://dchub.cloud', contact: 'api@dchub.cloud' },
      authentication: { type: 'api_key', header: 'X-API-Key', optional_for: ['free_tier'] },
      tools,
      gated_tools: ['get_intelligence_index', 'compare_sites', 'analyze_site', 'get_infrastructure'],
      pricing: {
        free: '10 calls/day — 5 results per call, 20 tools',
        developer: '$49/mo — 1,000 calls/day, all 24 tools',
        pro: '$199/mo — 10,000 calls/day',
        enterprise: 'Custom — 100,000 calls/day',
      },
      documentation: 'https://dchub.cloud/api-docs',
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/agent.json') {
    return new Response(JSON.stringify({
      "name":        "DC Hub Intelligence Agent",
      "description": "AI-powered, real-time data center intelligence. The live, MCP-native alternative to static research (DCHawk, dcByte, DCK).",
      "tagline":     "AI-powered. Real-time. Actionable. No BS.",
      "url":         "https://dchub.cloud/mcp",
      "version":     "2.1.0",
      "vs_competitors":  "https://dchub.cloud/vs",
      "intelligence_hub":"https://dchub.cloud/intelligence",
      "provider":    { "organization": "DC Hub", "url": "https://dchub.cloud" }
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/oauth-protected-resource') {
    return new Response(JSON.stringify({ resource: 'https://dchub.cloud/mcp', authorization_servers: [], bearer_methods_supported: ['header'], scopes_supported: [], resource_documentation: 'https://dchub.cloud/api-docs' }), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/oauth-authorization-server') {
    return new Response(JSON.stringify({ issuer: 'https://dchub.cloud', response_types_supported: [], grant_types_supported: [] }), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
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

    // ══════════════════════════════════════════════════════════════
    // v4.6.2: 301 /press-release (no slug) → /press to dedupe list pages.
    // /press-release/<slug> detail pages are unaffected because the guard
    // only matches the exact bare path. Runs FIRST so it can't be skipped
    // by any handler bug downstream. Same pattern as the v4.6.1 /mcp guard.
    // ══════════════════════════════════════════════════════════════
    if (pathname === '/press-release' || pathname === '/press-release/') {
      // DCHUB 2026-04-28 v2: pass the bare request to Pages assets.
      // Pages auto-resolves /press-release → press-release.html via its
      // pretty-URL handler. Don't rewrite to .html ourselves — that triggers
      // a 308 strip-extension redirect → loop back to the Worker.
      return env.ASSETS.fetch(request);
    }

    // ══════════════════════════════════════════════════════════════
    // Phase FF (2026-05-22): /brain/innovation early backend passthrough.
    // The PHASE_282 backend-page block (below) was NOT forwarding this path
    // (404, no x-dc-hub-source header) despite Set membership — same symptom
    // as /freshness, /enterprise. Rather than rely on that shadowed block,
    // forward here FIRST (same proven pattern as the /press-release + /mcp
    // guards above) so it can't be skipped by any downstream handler. Backend
    // route brain_innovation_bp serves /brain/innovation (verified 200).
    // ══════════════════════════════════════════════════════════════
    if (pathname === '/brain/innovation' || pathname === '/brain/innovation/') {
      try {
        const fwdHeaders = new Headers(request.headers);
        fwdHeaders.delete('host');
        fwdHeaders.delete('cf-connecting-ip');
        fwdHeaders.delete('cf-ray');
        fwdHeaders.delete('cf-visitor');
        const upstream = await fetch(`${RAILWAY_BACKEND}${pathname}${url.search}`, {
          method: request.method,
          headers: fwdHeaders,
          body: (request.method === 'GET' || request.method === 'HEAD') ? undefined : request.body,
          redirect: 'manual',
        });
        const h = new Headers(upstream.headers);
        h.set('X-DC-Worker-Version', WORKER_VERSION);
        h.set('x-dc-hub-source', 'worker-brain-innovation-early');
        h.delete('cf-cache-status');
        return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: h });
      } catch (e) {
        return new Response('brain/innovation proxy failed: ' + (e && e.message ? e.message : String(e)),
          { status: 502, headers: { 'X-DC-Worker-Version': WORKER_VERSION, 'x-dc-hub-source': 'worker-brain-innovation-error' } });
      }
    }

    // ══════════════════════════════════════════════════════════════
    // v4.6.1 HARD-GUARANTEED MCP PASSTHROUGH (runs before ANY routing)
    // DO NOT move this. DO NOT add logic above it (except the press redirect).
    // ══════════════════════════════════════════════════════════════
    if (pathname === '/mcp' || pathname === '/mcp/' || pathname.startsWith('/mcp/')) {
      if (request.method === 'OPTIONS') {
        return new Response(null, {
          status: 204,
          headers: {
            'Access-Control-Allow-Origin':  '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Accept, Authorization, Mcp-Session-Id, X-API-Key',
            'Access-Control-Expose-Headers':'Mcp-Session-Id, X-DC-Worker-Version, x-dc-hub-backend, x-dc-hub-source',
            'Access-Control-Max-Age':       '86400',
            'Cache-Control':                'no-store, no-cache, must-revalidate, private',
          },
        });
      }
      try {
        const fwdHeaders = new Headers(request.headers);
        fwdHeaders.delete('host');
        fwdHeaders.delete('cf-connecting-ip');
        fwdHeaders.delete('cf-ray');
        fwdHeaders.delete('cf-visitor');
        fwdHeaders.delete('x-forwarded-proto');
        const upstream = await fetch(`${RAILWAY_BACKEND}${pathname}${url.search}`, {
          method:   request.method,
          headers:  fwdHeaders,
          body:     (request.method === 'GET' || request.method === 'HEAD') ? undefined : request.body,
          redirect: 'manual',
        });
        const h = new Headers(upstream.headers);
        h.set('X-DC-Worker-Version', WORKER_VERSION);
        h.set('x-dc-hub-backend',    'railway');
        h.set('x-dc-hub-source',     'worker-mcp-passthrough');
        h.set('Cache-Control',       'no-store, no-cache, must-revalidate, private');
        h.set('Access-Control-Allow-Origin',   '*');
        h.set('Access-Control-Expose-Headers', 'Mcp-Session-Id, X-DC-Worker-Version, x-dc-hub-backend, x-dc-hub-source');
        h.delete('cf-cache-status');
        return new Response(upstream.body, {
          status:     upstream.status,
          statusText: upstream.statusText,
          headers:    h,
        });
      } catch (e) {
        return new Response(
          JSON.stringify({ error: 'mcp proxy failed', detail: e && e.message ? e.message : String(e) }),
          { status: 502, headers: { 'Content-Type':  'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate, private', 'X-DC-Worker-Version': WORKER_VERSION, 'x-dc-hub-source': 'worker-mcp-error', 'Access-Control-Allow-Origin': '*' } }
        );
      }
    }

    // ══════════════════════════════════════════════════════════════
    // Phase 282 — hard-guaranteed proxy for backend-served routes
    // that aren't under /api/* (so they aren't caught by the generic
    // API proxy at the bottom of fetch()). These paths exist on
    // Railway/Flask but were previously falling through to Pages
    // and 404'ing because Pages doesn't have them.
    //
    // Matches /freshness, /enterprise, /health/deep, and
    // /.well-known/ai-agents.json (the phases 268, 272, 269, 280
    // additions). Mirrors the /mcp passthrough pattern above.
    // ══════════════════════════════════════════════════════════════
    {
      const PHASE_282_RAILWAY_PATHS = new Set([
        '/freshness',
        '/enterprise',
        '/health/deep',
        '/.well-known/ai-agents.json',
        '/digest',  // phase 283: missed in phase 282 — Flask 302 redirect to /news (phase 280)
        // Phase RRR (2026-05-16): new brand-positioning surfaces
        '/vs',
        '/bs-translator',
        '/power-totals',  // /dcpi/totals already routes via /dcpi prefix
        '/intelligence',  // backend's customer-facing pulse page
        // Phase QA-sweep-2 (2026-05-16): pages shipped after RRR
        '/pocket-listings',
        '/spare-capacity',
        '/sentinel',
        // Phase IIII (2026-05-16): public ops transparency console
        '/transparency',
        // Phase YYYY (2026-05-16): operator profiles directory
        '/operators',
        // Phase BBBBB (2026-05-16): industry events directory
        '/events',
        // Phase ZZZZ-edge (2026-05-18): /markets index (no slug) was
        // timing out HTTP=000. The /markets/* prefix below only catches
        // sub-paths; bare /markets fell through to CF Pages which
        // doesn't have a /markets asset and hung. Adding here forces
        // it to Railway where the market_intelligence_page handler
        // returns the same HTML /market-intelligence does (200/157ms).
        '/markets',
        // Phase ZZZZ-pulse (2026-05-18): industry source-of-truth page.
        // /industry/pulse returns a Schema.org Dataset HTML; was hitting
        // CF SPA fallback because /industry/* wasn't allowlisted.
        '/industry/pulse',
        '/industry',
        // Phase ZZZZ-nav-untrap (2026-05-18): brain narrative flagged
        // /dcpi/totals + /transactions + /cited-by + /vs as nav_missing
        // findings — the templates DO have dchub-nav.js but CF was
        // returning SPA fallback because these paths weren't allowlisted.
        '/dcpi',
        '/dcpi/totals',
        '/cited-by',
        '/vs',
        '/competitive',
        '/heartbeat',
        // Phase ZZZZ-audit-bridge (2026-05-18): populate the OTHER
        // audit dashboard's expected JSON URLs from Railway.
        '/health.json',
        // Phase ZZZZ-audit-404-fix (2026-05-18): three audit-flagged 404s
        '/AGENTS.md',
        // Phase ZZZZ-partnerships (2026-05-19): Switzerland positioning
        '/partnerships',
        '/media/outreach',
        // Phase FF (2026-05-22): brain transparency page. Backend route
        // brain_innovation_bp serves /brain/innovation (HTML) and
        // /api/v1/brain/innovation (JSON, already proxied via /api/v1/brain/).
        // The HTML path was 404ing because it wasn't allowlisted here, so CF
        // returned its SPA fallback instead of forwarding to Railway.
        '/brain/innovation',
        // Phase ZZZZZ (2026-05-23): /pockets/<slug> + /pockets index were
        // 404'ing CF-side. _routes.json sends them here but the worker had
        // no rule, so CF's SSRF guard returned "DNS points to prohibited IP".
        // Flask backend routes/pockets.py:1002 + 492 serve these fine.
        '/pockets',
        // Phase ZZZZZ-bulk (2026-05-23): triage sweep — paths where Flask
        // returns 200 but CF was 404'ing because the worker had no rule.
        '/ai-partners',
        '/ai-partners.html',
        '/ai/discover',
        '/ai/facts',
        '/ai/facts.json',
        '/ai/gpts',
        '/ai/learn',
        '/ai/learn/news',
        '/ai/llms.txt',
        '/ai/outreach',
        '/ai/platforms',
        '/ai/schema/facility',
        '/capacity-map',
        '/capacity-map.html',
        '/mcp/manifest',
        // Phase ZZZZZ-bulk (2026-05-23): triage sweep — paths where Flask
        // returns 200 but CF was 404'ing because the worker had no rule.
        '/ai-data-source',
        '/ai/discovery',
        '/ai/learn/deals',
        '/ai/learn/facilities',
        '/ai/learn/market-intel',
        '/ai/robots.txt',
        '/ai/tracking/export',
        '/alive',
        '/auth.md',
        '/dchub2026.txt',
        '/digest/today',
        // Phase ZZZZZ-round3 (2026-05-23): /integrations exact + /ui — Flask
        // returns 200 here but CF was static-404ing.
        '/integrations',
        '/ui',
        '/visitor-intelligence',
      ]);
      // Phase YYYY (2026-05-16): also forward prefix-paths to Railway
      // for surfaces with dynamic sub-routes (e.g. /operators/<slug>).
      // The Set check covers literal paths; this prefix-check covers
      // path families. Keep tight: only paths we know are backend-served.
      // Phase ZZZZ-CCCCC (2026-05-16): +/markets/<slug>/deep-dive,
      // /reports/quarterly, /events.
      const PHASE_282_PREFIXES = [
        '/operators/',
        '/spare-capacity/',
        '/transactions/',
        '/markets/',
        '/reports/',
        // Phase ZZZZ-pulse (2026-05-18): /industry/<anything> passthrough
        '/industry/',
        // Phase ZZZZ-nav-untrap (2026-05-18): /dcpi/* and /vs/* passthrough
        // so per-slug detail pages reach Railway.
        '/dcpi/',
        '/integrations/',  // round 3 — chatgpt/copilot/grok integration manifests
        '/vs/',
        // Phase ZZZZ-audit-bridge (2026-05-18): /qa/*, /scripts/*, /data/*
        // for the OTHER audit dashboard. /data/* already worked via some
        // other rule but adding explicitly for clarity.
        '/qa/',
        '/scripts/',
        '/data/',
        '/api/v1/cf-analytics/',
        // Phase ZZZZ-audit-404-fix (2026-05-18): legacy ISO paths
        '/iso/',
        // Phase ZZZZZ (2026-05-23): /pockets/<slug> per-market detail pages.
        '/pockets/',
      ];
      if (PHASE_282_RAILWAY_PATHS.has(pathname) ||
          PHASE_282_PREFIXES.some(p => pathname.startsWith(p))) {
        try {
          const fwdHeaders = new Headers(request.headers);
          fwdHeaders.delete('host');
          fwdHeaders.delete('cf-connecting-ip');
          fwdHeaders.delete('cf-ray');
          fwdHeaders.delete('cf-visitor');
          fwdHeaders.delete('x-forwarded-proto');
          const upstream = await fetch(`${RAILWAY_BACKEND}${pathname}${url.search}`, {
            method:   request.method,
            headers:  fwdHeaders,
            body:     (request.method === 'GET' || request.method === 'HEAD') ? undefined : request.body,
            redirect: 'manual',
          });
          const h = new Headers(upstream.headers);
          h.set('X-DC-Worker-Version', WORKER_VERSION);
          h.set('x-dc-hub-backend',    'railway');
          h.set('x-dc-hub-source',     'worker-phase282-passthrough');
          h.delete('cf-cache-status');
          return new Response(upstream.body, {
            status:     upstream.status,
            statusText: upstream.statusText,
            headers:    h,
          });
        } catch (e) {
          return new Response(
            JSON.stringify({ error: 'phase282 proxy failed', path: pathname, detail: e && e.message ? e.message : String(e) }),
            { status: 502, headers: { 'Content-Type': 'application/json', 'X-DC-Worker-Version': WORKER_VERSION, 'x-dc-hub-source': 'worker-phase282-error' } }
          );
        }
      }
    }

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
      // /ai redirect intercept
      const strippedPath = pathname.endsWith('/') && pathname.length > 1 ? pathname.slice(0, -1) : pathname;
      if (strippedPath === '/ai') {
        const probeResp = await fetch(request, { redirect: 'manual' });
        if (probeResp.status >= 300 && probeResp.status < 400) {
          const contentResp = await fetch(request, { redirect: 'follow' });
          if (contentResp.ok) {
            const resp = new Response(contentResp.body, { status: 200, headers: contentResp.headers });
            resp.headers.delete('Location');
            resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
            resp.headers.set('X-DC-Rewrite', '/ai (redirect intercepted)');
            resp.headers.set('Cache-Control', 'public, max-age=120, stale-while-revalidate=300');
            return resp;
          }
        }
        if (probeResp.status !== 0) {
          const resp = new Response(probeResp.body, probeResp);
          resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          return resp;
        }
      }

      // MCP HANDLER (legacy in-file — top-of-fetch passthrough above will normally win)
      if (pathname === '/mcp' || pathname === '/mcp/') {
        // Should never reach here because of the v4.6.1 top-of-fetch passthrough.
        return new Response(JSON.stringify({ error: 'unreachable mcp branch' }), { status: 500, headers: { 'Content-Type': 'application/json' } });
      }

      // Discovery paths → Railway
      if (isDiscoveryPath(pathname)) {
        const resp = await proxyToRailway(request, pathname, url.search, 3600, 10000);
        if (resp) return addCORS(new Response(resp.body, resp), request);
      }

      // News digest routes
      if (pathname.startsWith('/news') || pathname.startsWith('/press-release')) {
        const newsResp = await handleNewsRoute(pathname, request, env);
        if (newsResp) return newsResp;
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

    // Publish proxy
    if (pathname === '/api/publish') {
      return addCORS(await handlePublishRoute(request, env), request);
    }

    // FEMA Flood Zone Proxy
    if (pathname === '/api/v1/fema/flood-zone' && isGet) {
      const lat = url.searchParams.get('lat');
      const lng = url.searchParams.get('lng');
      if (!lat || !lng || isNaN(parseFloat(lat)) || isNaN(parseFloat(lng))) {
        return addCORS(json({ success: false, error: 'lat and lng query parameters required (numeric)' }, 400), request);
      }
      const femaUrl = 'https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query'
        + '?geometry=' + encodeURIComponent(lng + ',' + lat)
        + '&geometryType=esriGeometryPoint&inSR=4326&outSR=4326'
        + '&spatialRel=esriSpatialRelIntersects'
        + '&outFields=FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE,DEPTH,LEN_UNIT'
        + '&returnGeometry=false&f=json';
      try {
        const controller = new AbortController();
        const femaTimeout = setTimeout(() => controller.abort(), 8000);
        const femaResp = await fetch(femaUrl, { signal: controller.signal });
        clearTimeout(femaTimeout);
        if (!femaResp.ok) {
          return addCORS(json({ success: false, error: 'FEMA API error', status: femaResp.status }, 502), request);
        }
        const femaData = await femaResp.json();
        const features = femaData.features || [];
        if (features.length === 0) {
          const result = addCORS(json({
            success: true,
            data: { flood_zone: 'X', zone_subtype: 'AREA OF MINIMAL FLOOD HAZARD', sfha: false, base_flood_elevation: null, depth: null, source: 'fema_nfhl', note: 'No NFHL features found at this point' },
            query: { lat: parseFloat(lat), lng: parseFloat(lng) },
            raw_feature_count: 0
          }), request);
          result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          result.headers.set('Cache-Control', 'public, max-age=3600');
          return result;
        }
        const attrs = features[0].attributes || {};
        const floodZone = attrs.FLD_ZONE || 'X';
        const zoneSubtype = attrs.ZONE_SUBTY || null;
        const sfhaRaw = attrs.SFHA_TF;
        const sfha = sfhaRaw === 'T' || sfhaRaw === 'True' || sfhaRaw === true;
        let bfe = attrs.STATIC_BFE != null ? attrs.STATIC_BFE : null;
        let depth = attrs.DEPTH != null ? attrs.DEPTH : null;
        if (bfe !== null && bfe < -100) bfe = null;
        if (depth !== null && depth < 0) depth = null;
        const result = addCORS(json({
          success: true,
          data: { flood_zone: floodZone, zone_subtype: zoneSubtype, sfha: sfha, base_flood_elevation: bfe, depth: depth, source: 'fema_nfhl' },
          query: { lat: parseFloat(lat), lng: parseFloat(lng) },
          raw_feature_count: features.length
        }), request);
        result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        result.headers.set('Cache-Control', 'public, max-age=3600');
        return result;
      } catch (e) {
        return addCORS(json({ success: false, error: 'FEMA API unreachable', message: e.name === 'AbortError' ? 'FEMA API timeout (8s)' : String(e.message || e) }, 504), request);
      }
    }

    // v4.6.0: get-api-key
    if (pathname === '/api/auth/get-api-key' && (request.method === 'GET' || request.method === 'POST')) {
      return addCORS(await handleGetApiKey(request, env), request);
    }

    // API key management routes
    if (pathname === '/api/admin/create-api-key' && request.method === 'POST') {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);
      return addCORS(await handleCreateApiKey(request, env), request);
    }
    if (pathname === '/api/admin/usage' && isGet) {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      return addCORS(await handleUsageCheck(request, url, env), request);
    }
    if (pathname === '/api/admin/revoke-api-key' && request.method === 'POST') {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);
      return addCORS(await handleRevokeApiKey(request, env), request);
    }
    if ((pathname === '/api/stripe/mcp-webhook' || pathname === '/api/stripe/webhook') && request.method === 'POST') {
      return addCORS(await handleStripeWebhook(request, env), request);
    }

    // Cache status
    if (pathname === '/api/cache/status' && env.DCHUB_CACHE) {
      const list = await env.DCHUB_CACHE.list({ prefix: 'kv:', limit: 50 });
      const mcpList = await env.DCHUB_CACHE.list({ prefix: 'mcp:', limit: 50 });
      const keys = [];
      for (const k of list.keys) { const raw = await env.DCHUB_CACHE.get(k.name); let age = null; if (raw) { try { const e = JSON.parse(raw); age = Math.round((Date.now() - e.ts) / 1000); } catch(e) {} } keys.push({ path: k.name.replace('kv:', ''), age_seconds: age, type: 'api' }); }
      for (const k of mcpList.keys) { const raw = await env.DCHUB_CACHE.get(k.name); let age = null; if (raw) { try { const e = JSON.parse(raw); age = Math.round((Date.now() - e.ts) / 1000); } catch(e) {} } keys.push({ path: k.name, age_seconds: age, type: 'mcp' }); }
      return addCORS(json({ cached_endpoints: keys.length, keys, worker_version: WORKER_VERSION }), request);
    }

    // Cache purge
    if (pathname === '/api/cache/purge' && request.method === 'POST' && env.DCHUB_CACHE) {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      const list = await env.DCHUB_CACHE.list({ prefix: 'kv:', limit: 200 });
      const mcpList = await env.DCHUB_CACHE.list({ prefix: 'mcp:', limit: 200 });
      let deleted = 0;
      for (const key of list.keys) { await env.DCHUB_CACHE.delete(key.name); deleted++; }
      for (const key of mcpList.keys) { await env.DCHUB_CACHE.delete(key.name); deleted++; }
      return addCORS(json({ success: true, purged: deleted, includes_mcp: true }), request);
    }

    // Admin: seed caches
    if ((pathname === '/api/admin/seed-api-cache' || pathname === '/api/admin/seed-mcp-cache') && env.DCHUB_CACHE) {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      const results = await seedApiCache(env.DCHUB_CACHE);
      return addCORS(json({ success: true, worker_version: WORKER_VERSION, ...results }), request);
    }

    // Version
    if (pathname === '/api/version' || pathname === '/api/v1/version') {
      return addCORS(json({ version: WORKER_VERSION, source: 'cloudflare-worker', backend: 'railway', mcp_tiers: Object.fromEntries(Object.entries(MCP_TIERS).map(([k, v]) => [k, { daily_limit: v.daily_limit, results_limit: v.results_limit }])), gated_tools: [...GATED_TOOLS], cron: '0 */6 * * * (every 6 hours)', timestamp: new Date().toISOString() }), request);
    }

    // STEP 1: KV fresh cache
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

    // STEP 2: Proxy to Railway
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

    // STEP 2.5: Render failover (GETs only — Render runs IS_FAILOVER=true read-only)
    // r33-Q+failover (2026-05-21) — try Render before falling through to stale cache.
    // Was: Railway 5xx → stale KV → 503. Now: Railway 5xx → Render → stale KV → 503.
    if (isGet) {
      const renderResp = await proxyToRender(request, pathname, url.search, 45000);
      if (renderResp && renderResp.status < 500) {
        const result = addCORS(new Response(renderResp.body, renderResp), request);
        result.headers.set('x-dc-hub-backend', 'render');
        result.headers.set('x-dc-hub-failover', 'true');
        result.headers.set('X-Failover-Mode', 'render-active');
        result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        return result;
      }
    }

    // STEP 3: Stale KV
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

    // STEP 4: 503
    const errResp = addCORS(json({ error: 'Service temporarily unavailable', message: 'Backend unreachable and no cached data available. Please retry shortly.', status: 503, worker_version: WORKER_VERSION, tip: 'This message lands when Railway, Render failover, and KV stale cache are all unavailable.' }, 503), request);
    errResp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
    errResp.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
    return errResp;
  }
};
