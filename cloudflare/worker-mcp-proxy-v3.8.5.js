/**
 * DC Hub API Proxy Worker v3.8.7 — Server-Side Anon Session Enforcement
 * ================================================================
 *
 * v3.8.6 CHANGES (Mar 4 2026):
 *   - ADDED: /api/discovery/facilities as Neon-direct route (fixes homepage 403)
 *   - FIXED: Homepage content no longer blocked for anonymous users
 *
 * v3.8.5 CHANGES (Feb 26 2026):
 *   - FIXED: Non-API routes (homepage, static pages) now proxy to Railway
 *     instead of falling through to nonexistent Cloudflare Pages origin
 *
 * v3.8.3 CHANGES (Feb 26 2026):
 *   - FIXED: /.well-known/mcp.json, /.well-known/agent.json, /.well-known/security.txt
 *     now served INLINE from Worker (no backend proxy needed)
 *
 * v3.8.1 CHANGES (Feb 25 2026):
 *   - FIXED: /.well-known/ paths rewritten to Railway-friendly aliases
 *
 * v3.8 CHANGES (Feb 25 2026):
 *   - ADDED: AI Testimonials Neon-direct routes
 *   - ADDED: /api/v1/facilities/by-market and /api/v1/facilities/by-provider
 *
 * v3.7 CHANGES (Feb 24 2026):
 *   - ADDED: AI discovery file routing to Railway backend
 *
 * v3.6 CHANGES (Feb 23 2026):
 *   - ADDED: /dashboard proxied to Railway backend
 *
 * v3.5 CHANGES (Feb 22 2026):
 *   - ADDED: /api/ai/tracking as Neon-direct route
 *
 * v3.4 CHANGES (Feb 21 2026):
 *   - SWAPPED: Railway is now PRIMARY backend, Replit is FAILOVER
 *
 * ARCHITECTURE:
 *   Browser → Cloudflare Worker → Neon (direct SQL for reads)
 *                                → Railway (primary backend)
 *                                → Replit (failover backend)
 */

const RAILWAY_BACKEND = 'https://dchub-backend-production.up.railway.app';
const REPLIT_BACKEND  = 'https://dc-hub-replit-fixedzip--azmartone1.replit.app';

const PRIMARY_TIMEOUT  = 12000;
const FAILOVER_TIMEOUT = 15000;
const CACHE_TTL  = 300;
const STALE_TTL  = 7200;

const ALLOWED_ORIGINS = [
  'https://dchub.cloud',
  'https://www.dchub.cloud',
  'http://localhost:8788',
  'http://localhost:3000',
];

const DISCOVERY_PATHS = [
  '/openapi.json',
  '/AGENTS.md',
  '/llms.txt',
  '/llms-full.txt',
  '/robots.txt',
  '/ai-plugin.json',
  '/mcp-server-card.json',
  '/.well-known/',
];

const INLINE_DISCOVERY = {
  '/.well-known/mcp.json': {
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify({
      "name": "DC Hub Intelligence",
      "description": "Real-time data center market intelligence — 20,000+ facilities, 140+ countries. Live M&A deals, capacity pipelines, power availability, and market analytics.",
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http",
      "version": "1.0.0",
      "tools": [
        { "name": "search_facilities", "description": "Search data center facilities by location, provider, capacity, or certification" },
        { "name": "get_facility", "description": "Get detailed profile for a specific data center facility" },
        { "name": "search_deals", "description": "Search M&A transactions by buyer, seller, value, or date range" },
        { "name": "get_market_report", "description": "Get AI-generated market intelligence report for a region or provider" },
        { "name": "get_site_score", "description": "Get site suitability score for a location based on power, fiber, risk, and climate" },
        { "name": "get_fuel_mix", "description": "Get power generation fuel mix for a region" },
        { "name": "search_news", "description": "Search latest data center industry news and announcements" }
      ],
      "authentication": { "type": "api_key", "header": "X-API-Key" },
      "pricing": { "free_tier": "100 requests/day", "pro": "$49/mo", "enterprise": "Custom" },
      "contact": "api@dchub.cloud"
    }, null, 2)
  },
  '/.well-known/agent.json': {
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify({
      "name": "DC Hub Intelligence",
      "description": "The live intelligence layer for the global data center market. 20,000+ facilities across 140+ countries with real-time M&A tracking, capacity pipelines, and market analytics.",
      "url": "https://dchub.cloud",
      "version": "1.0.0",
      "capabilities": {
        "streaming": true,
        "pushNotifications": false,
        "stateTransitionHistory": false
      },
      "skills": [
        { "id": "facility-search", "name": "Data Center Search", "description": "Search and filter 20,000+ data center facilities worldwide" },
        { "id": "deal-tracker", "name": "M&A Deal Tracker", "description": "Track data center M&A transactions in real-time" },
        { "id": "market-intelligence", "name": "Market Intelligence", "description": "AI-generated market reports and analytics" },
        { "id": "site-scoring", "name": "Site Scoring", "description": "Evaluate locations for data center suitability" }
      ],
      "authentication": { "schemes": ["api_key"], "credentials": { "api_key": { "header": "X-API-Key" } } },
      "provider": { "organization": "DC Hub", "url": "https://dchub.cloud" },
      "defaultInputModes": ["text"],
      "defaultOutputModes": ["text"]
    }, null, 2)
  },
  '/.well-known/mcp-registry-auth': {
    contentType: 'text/plain; charset=utf-8',
    body: `v=MCPv1; k=ed25519; p=8LE9YOct4SKYuIJT8JGMK6z9lhfPMbCM5pQCp5FTRBg=`
  },
  '/.well-known/security.txt': {
    contentType: 'text/plain; charset=utf-8',
    body: `Contact: mailto:security@dchub.cloud\nPreferred-Languages: en\nCanonical: https://dchub.cloud/.well-known/security.txt\nPolicy: https://dchub.cloud/terms\nExpires: 2027-01-01T00:00:00.000Z`
  }
};

let failoverState = {
  primaryHealthy: true,
  lastPrimaryFailure: 0,
  consecutivePrimaryFailures: 0,
  failoverHealthy: true,
  lastFailoverFailure: 0,
};

const CIRCUIT_THRESHOLD = 3;
const CIRCUIT_RECOVERY_MS = 60000;

function shouldSkipPrimary() {
  if (failoverState.consecutivePrimaryFailures >= CIRCUIT_THRESHOLD) {
    const elapsed = Date.now() - failoverState.lastPrimaryFailure;
    if (elapsed < CIRCUIT_RECOVERY_MS) return true;
    failoverState.consecutivePrimaryFailures = 0;
  }
  return false;
}

function markPrimaryFailure() {
  failoverState.primaryHealthy = false;
  failoverState.lastPrimaryFailure = Date.now();
  failoverState.consecutivePrimaryFailures++;
}

function markPrimarySuccess() {
  failoverState.primaryHealthy = true;
  failoverState.consecutivePrimaryFailures = 0;
}

const NEON_ROUTES = {
  '/api/health': {
    query: `SELECT 'healthy' as status, NOW() as timestamp,
      (SELECT COUNT(*) FROM facilities) as facility_count,
      (SELECT COUNT(*) FROM deals) as deal_count,
      (SELECT COUNT(*) FROM news_articles) as news_count,
      'neon-direct' as source`,
    transform: (rows) => ({
      status: 'healthy', timestamp: rows[0].timestamp,
      facility_count: parseInt(rows[0].facility_count),
      deal_count: parseInt(rows[0].deal_count),
      news_count: parseInt(rows[0].news_count),
      source: 'neon-direct', version: '2.5.2', worker: '3.8.6',
      failover: {
        primary: 'railway', primary_healthy: failoverState.primaryHealthy,
        failover: 'replit', failover_healthy: failoverState.failoverHealthy,
        consecutive_primary_failures: failoverState.consecutivePrimaryFailures,
      }
    })
  },
  '/api/news/live': {
    query: `SELECT id, title, summary, source, url, category, image_url, published_at, is_breaking, relevance_score
            FROM news_articles WHERE published_at IS NOT NULL AND published_at != '' ORDER BY published_at DESC LIMIT 50`,
    transform: (rows) => ({ articles: rows, total: rows.length, source: 'neon-direct' })
  },
  '/api/news': {
    query: `SELECT id, title, summary, source, url, category, image_url, published_at, is_breaking, relevance_score
            FROM news_articles WHERE published_at IS NOT NULL AND published_at != '' ORDER BY published_at DESC LIMIT 50`,
    transform: (rows) => ({ articles: rows, total: rows.length, source: 'neon-direct' })
  },
  '/api/agent/news': {
    query: `SELECT id, title, summary, source, url, category, published_at, relevance_score
            FROM news_articles WHERE published_at IS NOT NULL AND published_at != '' ORDER BY published_at DESC LIMIT 20`,
    transform: (rows) => ({ articles: rows, count: rows.length, source: 'neon-direct' })
  },
  '/api/v1/stats': {
    query: `SELECT (SELECT COUNT(*) FROM facilities) as total_facilities,
      (SELECT COUNT(*) FROM deals) as total_deals,
      (SELECT COUNT(*) FROM news_articles) as total_news,
      (SELECT COUNT(*) FROM capacity_pipeline) as total_pipeline,
      (SELECT COUNT(*) FROM ecosystem_companies) as total_ecosystem,
      (SELECT COUNT(DISTINCT country) FROM facilities) as total_countries`,
    transform: (rows) => ({
      success: true,
      stats: {
        facilities: parseInt(rows[0].total_facilities), deals: parseInt(rows[0].total_deals),
        news_articles: parseInt(rows[0].total_news), pipeline_projects: parseInt(rows[0].total_pipeline),
        ecosystem_companies: parseInt(rows[0].total_ecosystem), countries: parseInt(rows[0].total_countries)
      },
      source: 'neon-direct'
    })
  },
  '/api/agent/stats': {
    query: `SELECT (SELECT COUNT(*) FROM facilities) as total_facilities,
      (SELECT COUNT(*) FROM deals) as total_deals,
      (SELECT COUNT(*) FROM news_articles) as total_news,
      (SELECT COUNT(*) FROM capacity_pipeline) as total_pipeline`,
    transform: (rows) => ({
      total_facilities: parseInt(rows[0].total_facilities), total_deals: parseInt(rows[0].total_deals),
      total_news: parseInt(rows[0].total_news), total_pipeline: parseInt(rows[0].total_pipeline),
      source: 'neon-direct'
    })
  },
  '/api/v1/deals': {
    query: `SELECT id, buyer, seller, value, mw, market, date, year, type, region, status, notes FROM deals ORDER BY date DESC LIMIT 50`,
    transform: (rows) => ({
      success: true,
      data: rows.map(r => ({ id: r.id, buyer: r.buyer, seller: r.seller, value_usd: r.value, mw: r.mw, market: r.market, date: r.date, year: r.year, deal_type: r.type, region: r.region, status: r.status, notes: r.notes })),
      total: rows.length, source: 'neon-direct'
    })
  },
  '/api/transactions/public': {
    query: `SELECT id, buyer, seller, value, mw, market, date, year, type, region, status, notes FROM deals ORDER BY date DESC LIMIT 15`,
    transform: (rows) => ({
      success: true,
      transactions: rows.map(r => ({ id: r.id, buyer: r.buyer, seller: r.seller, value: r.value, mw: r.mw, market: r.market, date: r.date, year: r.year, type: r.type, region: r.region, status: r.status, notes: r.notes })),
      total: rows.length, source: 'neon-direct'
    })
  },
  '/api/v1/map': {
    query: `SELECT id, name, provider, city, state, country, latitude, longitude, power_mw, status
            FROM facilities WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0 AND longitude != 0 ORDER BY id LIMIT 500`,
    transform: (rows) => ({
      success: true,
      data: rows.map(r => ({ id: r.id, name: r.name, provider: r.provider, city: r.city, state: r.state, country: r.country, lat: parseFloat(r.latitude) || 0, lng: parseFloat(r.longitude) || 0, power_mw: parseFloat(r.power_mw) || 0, status: r.status || 'active' })),
      total: rows.length, source: 'neon-direct'
    })
  },

  // v3.8.6 NEW — fixes homepage 403 for anonymous users
  '/api/discovery/facilities': {
    query: `SELECT id, name, provider, city, state, country, region,
            latitude, longitude, power_mw, status,
            created_at as discovered_at
            FROM facilities
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ORDER BY created_at DESC NULLS LAST
            LIMIT 20`,
    transform: (rows) => ({
      success: true,
      facilities: rows.map(r => ({
        id: r.id, name: r.name, provider: r.provider,
        market: [r.city, r.state].filter(Boolean).join(', '),
        city: r.city, state: r.state, country: r.country, region: r.region,
        latitude: parseFloat(r.latitude) || 0,
        longitude: parseFloat(r.longitude) || 0,
        power_mw: parseFloat(r.power_mw) || 0,
        status: r.status || 'active',
        discovered_at: r.discovered_at,
        is_duplicate: false
      })),
      total: rows.length, source: 'neon-direct'
    })
  },

  '/api/agent/facilities': {
    query: `SELECT id, name, provider, city, state, country, power_mw, tier_level, status FROM facilities ORDER BY name ASC LIMIT 100`,
    transform: (rows) => ({ facilities: rows, count: rows.length, source: 'neon-direct' })
  },
  '/api/v1/pipeline': {
    query: `SELECT * FROM capacity_pipeline ORDER BY id DESC LIMIT 50`,
    transform: (rows) => ({ success: true, data: rows, total: rows.length, source: 'neon-direct' })
  },
  '/api/v1/markets': {
    query: `SELECT country, state, city, COUNT(*) as facility_count,
            SUM(CASE WHEN power_mw IS NOT NULL THEN CAST(power_mw AS FLOAT) ELSE 0 END) as total_power_mw
            FROM facilities WHERE city IS NOT NULL AND city != '' GROUP BY country, state, city ORDER BY facility_count DESC LIMIT 50`,
    transform: (rows) => ({
      success: true,
      markets: rows.map(r => ({ ...r, facility_count: parseInt(r.facility_count), total_power_mw: parseFloat(r.total_power_mw || 0) })),
      source: 'neon-direct'
    })
  },
  '/api/ecosystem': {
    query: `SELECT * FROM ecosystem_companies ORDER BY name ASC`,
    transform: (rows) => ({ success: true, companies: rows, total: rows.length, source: 'neon-direct' })
  },
  '/api/founding-members': {
    query: `SELECT * FROM users WHERE role = 'founding_member' OR plan = 'founding'`,
    transform: (rows) => ({
      success: true,
      members: rows.map(r => ({ id: r.id, name: r.name, company: r.company, joined: r.created_at })),
      count: rows.length, source: 'neon-direct'
    })
  },
  '/api/market-report': {
    query: `SELECT (SELECT COUNT(*) FROM facilities) as total_facilities, (SELECT COUNT(*) FROM deals) as total_deals,
      (SELECT SUM(CASE WHEN value IS NOT NULL THEN value ELSE 0 END) FROM deals) as total_deal_value,
      (SELECT COUNT(*) FROM capacity_pipeline) as pipeline_projects, (SELECT COUNT(DISTINCT country) FROM facilities) as countries_covered`,
    transform: (rows) => ({
      success: true, report: { total_facilities: parseInt(rows[0].total_facilities), total_deals: parseInt(rows[0].total_deals),
        total_deal_value: parseFloat(rows[0].total_deal_value || 0), pipeline_projects: parseInt(rows[0].pipeline_projects),
        countries_covered: parseInt(rows[0].countries_covered) }, source: 'neon-direct'
    })
  },
  '/api/v1/market-report': {
    query: `SELECT (SELECT COUNT(*) FROM facilities) as total_facilities, (SELECT COUNT(*) FROM deals) as total_deals,
      (SELECT SUM(CASE WHEN value IS NOT NULL THEN value ELSE 0 END) FROM deals) as total_deal_value,
      (SELECT COUNT(*) FROM capacity_pipeline) as pipeline_projects, (SELECT COUNT(DISTINCT country) FROM facilities) as countries_covered`,
    transform: (rows) => ({
      success: true, report: { total_facilities: parseInt(rows[0].total_facilities), total_deals: parseInt(rows[0].total_deals),
        total_deal_value: parseFloat(rows[0].total_deal_value || 0), pipeline_projects: parseInt(rows[0].pipeline_projects),
        countries_covered: parseInt(rows[0].countries_covered) }, source: 'neon-direct'
    })
  },
  '/api/v1/version': {
    query: `SELECT 'healthy' as status, NOW() as timestamp`,
    transform: (rows) => ({ version: '2.5.2', worker: '3.8.6', timestamp: rows[0].timestamp, source: 'neon-direct' })
  },
  '/api/agent/query-stats': {
    query: `SELECT (SELECT COUNT(*) FROM facilities) as facilities, (SELECT COUNT(*) FROM deals) as deals, (SELECT COUNT(*) FROM news_articles) as news`,
    transform: (rows) => ({ stats: { facilities: parseInt(rows[0].facilities), deals: parseInt(rows[0].deals), news: parseInt(rows[0].news) }, source: 'neon-direct' })
  },
  '/api/ai-query-stats': {
    query: `SELECT (SELECT COUNT(*) FROM facilities) as facilities, (SELECT COUNT(*) FROM deals) as deals,
      (SELECT COUNT(*) FROM news_articles) as news, (SELECT COUNT(*) FROM ai_usage_tracking) as ai_queries`,
    transform: (rows) => ({
      success: true, stats: { facilities: parseInt(rows[0].facilities), deals: parseInt(rows[0].deals),
        news: parseInt(rows[0].news), ai_queries: parseInt(rows[0].ai_queries) }, source: 'neon-direct'
    })
  },
  '/api/ai/tracking': {
    query: `SELECT platform, total_requests, first_seen, last_seen, requests_7d, color, name, company FROM ai_cumulative ORDER BY total_requests DESC`,
    transform: (rows) => {
      const platforms = {}; let allTime = 0; const chartData = {};
      rows.forEach(r => {
        const key = r.platform || 'unknown'; const tr = parseInt(r.total_requests) || 0;
        platforms[key] = { name: r.name || key, company: r.company || '', color: r.color || '#64748b', total_requests: tr, requests_7d: parseInt(r.requests_7d) || 0, first_seen: r.first_seen, last_seen: r.last_seen };
        chartData[key] = { name: r.name || key, color: r.color || '#64748b', requests_7d: parseInt(r.requests_7d) || 0 };
        allTime += tr;
      });
      return { status: 'live', tracking: 'persistent', total_requests_all_time: allTime, total_requests_today: 0,
        platforms, chart_data: chartData, platforms_active: rows.filter(r => parseInt(r.total_requests) > 0).length,
        platforms_tracked: rows.length, recent_activity: [], daily_breakdown: [],
        generated_at: new Date().toISOString(), note: 'Direct Neon PostgreSQL — Worker neon-direct', source: 'neon-direct' };
    }
  },
  '/api/v1/ai-tracking/stats': {
    query: `SELECT COALESCE((SELECT COUNT(*) FROM ai_usage_tracking WHERE tracked_at >= NOW() - INTERVAL '1 day'), 0) as total_today,
      COALESCE((SELECT COUNT(*) FROM ai_usage_tracking), 0) as total_all_time,
      COALESCE((SELECT COUNT(DISTINCT platform) FROM ai_usage_tracking WHERE platform IS NOT NULL AND platform != '' AND platform != 'unknown'), 0) as active_platforms`,
    custom: true,
    secondQuery: `SELECT COALESCE(platform, 'unknown') as platform, COUNT(*) as total_requests,
      COALESCE((SELECT COUNT(*) FROM ai_usage_tracking t2 WHERE t2.platform = ai_usage_tracking.platform AND t2.tracked_at >= NOW() - INTERVAL '7 days'), 0) as requests_7d,
      MAX(tracked_at) as last_seen FROM ai_usage_tracking WHERE platform IS NOT NULL AND platform != '' GROUP BY platform ORDER BY total_requests DESC`,
    transform: null
  },
  '/ai/platforms': {
    query: `SELECT id, name, status, integration_type, description, company, color, mcp_active FROM ai_platforms ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, name ASC`,
    transform: (rows) => ({
      success: true,
      platforms: rows.map(r => ({ id: r.id, name: r.name, status: r.status || 'pending', integration_type: r.integration_type || '', description: r.description || '', company: r.company || '', color: r.color || '', mcp_active: r.mcp_active || false })),
      active_platforms: rows.filter(r => r.status === 'active').length, total: rows.length, source: 'neon-direct'
    })
  },
  '/api/v1/platform-cards': {
    query: `SELECT id, name, category, icon, icon_bg, card_class, status, status_class, description, method, link_url, link_text, link_external, brand_color, ai_wars_score, ai_wars_rank, ai_wars_note, sort_order FROM platform_cards ORDER BY COALESCE(sort_order, 999), category, name`,
    transform: (rows) => {
      const catMap = {};
      rows.forEach(r => {
        const cat = r.category || 'ai_platforms';
        if (!catMap[cat]) catMap[cat] = { category: cat, label: cat === 'ai_platforms' ? 'AI Platforms' : cat === 'infrastructure' ? 'AI Infrastructure & GPU Cloud' : cat, cards: [] };
        catMap[cat].cards.push({ id: r.id, name: r.name, icon: r.icon, icon_bg: r.icon_bg, card_class: r.card_class || 'generic', status: r.status, status_class: r.status_class || 'status-ready', description: r.description, method: r.method, link_url: r.link_url, link_text: r.link_text, link_external: r.link_external === true || r.link_external === 'true', brand_color: r.brand_color, ai_wars_score: r.ai_wars_score ? parseInt(r.ai_wars_score) : null, ai_wars_rank: r.ai_wars_rank ? parseInt(r.ai_wars_rank) : null, ai_wars_note: r.ai_wars_note });
      });
      return { success: true, categories: Object.values(catMap), total_platforms: rows.length, source: 'neon-direct' };
    }
  },
  '/api/v1/testimonials': {
    query: `SELECT id, platform, agent_name, quote, context, query, category, featured, created_at
            FROM ai_testimonials WHERE approved = TRUE
            ORDER BY featured DESC, created_at DESC LIMIT 50`,
    transform: (rows) => ({
      success: true,
      testimonials: rows.map(r => ({
        id: r.id, platform: r.platform, agent_name: r.agent_name,
        quote: r.quote, context: r.context, query: r.query,
        category: r.category, featured: r.featured, created_at: r.created_at
      })),
      count: rows.length, source: 'neon-direct'
    })
  },
  '/api/v1/testimonials/stats': {
    query: `SELECT COUNT(*) as total,
      COUNT(*) FILTER (WHERE approved = TRUE) as approved,
      COUNT(DISTINCT platform) as platforms,
      COUNT(*) FILTER (WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '7 days') as this_week
      FROM ai_testimonials`,
    transform: (rows) => ({
      success: true,
      stats: { total: parseInt(rows[0].total), approved: parseInt(rows[0].approved), platforms: parseInt(rows[0].platforms), this_week: parseInt(rows[0].this_week) },
      source: 'neon-direct'
    })
  },
  '/health': {
    query: `SELECT 'healthy' as status, NOW() as timestamp, (SELECT COUNT(*) FROM facilities) as facility_count, (SELECT COUNT(*) FROM deals) as deal_count, (SELECT COUNT(*) FROM news_articles) as news_count, 'neon-direct' as source`,
    transform: (rows) => ({ status: 'healthy', timestamp: rows[0].timestamp, facility_count: parseInt(rows[0].facility_count), deal_count: parseInt(rows[0].deal_count), news_count: parseInt(rows[0].news_count), source: 'neon-direct', version: '3.8.6' })
  },
};

const TRANSPARENT_PROXY_PATHS = [
  '/api/auth/', '/api/login', '/api/register', '/api/stripe/',
  '/api/v1/land-power/', '/api/land-power/',
];

const BACKEND_ONLY_PATHS = [
  '/api/admin/', '/api/subscribe', '/api/webhook', '/api/ai/query', '/api/mcp',
];

const BACKEND_HTML_PATHS = ['/dashboard'];

function isDiscoveryPath(pathname) {
  return DISCOVERY_PATHS.some(p => pathname === p || pathname.startsWith(p));
}

async function queryNeon(sql, databaseUrl) {
  const connUrl = new URL(databaseUrl);
  const host = connUrl.hostname;
  const httpHost = host.replace('-pooler', '');
  const neonHttpUrl = `https://${httpHost}/sql`;
  const response = await fetch(neonHttpUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Neon-Connection-String': databaseUrl, 'Neon-Raw-Text-Output': 'true', 'Neon-Array-Mode': 'false' },
    body: JSON.stringify({ query: sql, params: [] })
  });
  if (!response.ok) { const errText = await response.text(); throw new Error(`Neon HTTP query failed (${response.status}): ${errText}`); }
  const result = await response.json();
  const data = Array.isArray(result) ? result[0] : (result.result || result);
  if (data.rows && data.rows.length > 0 && !Array.isArray(data.rows[0])) return data.rows;
  if (data.fields && data.rows) return data.rows.map(row => { const obj = {}; data.fields.forEach((field, i) => { obj[field.name] = row[i]; }); return obj; });
  return data.rows || [];
}

function getAllowedOrigin(requestOrigin) {
  if (requestOrigin && ALLOWED_ORIGINS.includes(requestOrigin)) return requestOrigin;
  return 'https://dchub.cloud';
}

function handleCORS(request) {
  const origin = request ? request.headers.get('Origin') : null;
  const allowOrigin = (origin && ALLOWED_ORIGINS.includes(origin)) ? origin : '*';
  const headers = { 'Access-Control-Allow-Origin': allowOrigin, 'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With', 'Access-Control-Max-Age': '86400' };
  if (allowOrigin !== '*') headers['Access-Control-Allow-Credentials'] = 'true';
  return new Response(null, { status: 204, headers });
}

function addCORSHeaders(response, request) {
  const origin = request ? request.headers.get('Origin') : null;
  if (origin && ALLOWED_ORIGINS.includes(origin)) { response.headers.set('Access-Control-Allow-Origin', origin); response.headers.set('Access-Control-Allow-Credentials', 'true'); }
  else { response.headers.set('Access-Control-Allow-Origin', '*'); response.headers.delete('Access-Control-Allow-Credentials'); }
  response.headers.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  response.headers.set('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With');
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try { const response = await fetch(url, { ...options, signal: controller.signal }); clearTimeout(timeoutId); return response; }
  catch (err) { clearTimeout(timeoutId); throw err; }
}

async function proxyWithFailover(request, pathname, search) {
  const isGet = request.method === 'GET';
  const proxyHeaders = {
    'Accept': 'application/json', 'Content-Type': request.headers.get('Content-Type') || 'application/json',
    'User-Agent': request.headers.get('User-Agent') || 'DCHub-Worker/3.8.6', 'X-Worker-Version': 'DCHub-Worker/3.8.6',
    'X-Forwarded-For': request.headers.get('CF-Connecting-IP') || '', 'X-Forwarded-Proto': 'https',
  };
  const authHeader = request.headers.get('Authorization'); if (authHeader) proxyHeaders['Authorization'] = authHeader;
  const apiKey = request.headers.get('X-API-Key'); if (apiKey) proxyHeaders['X-API-Key'] = apiKey;
  let bodyForRetry = null; let bodyForPrimary = null;
  if (!isGet) { const bodyBytes = await request.arrayBuffer(); bodyForPrimary = bodyBytes; bodyForRetry = bodyBytes.slice(0); }
  const backends = shouldSkipPrimary()
    ? [{ name: 'replit', url: REPLIT_BACKEND, timeout: FAILOVER_TIMEOUT }]
    : [{ name: 'railway', url: RAILWAY_BACKEND, timeout: PRIMARY_TIMEOUT }, { name: 'replit', url: REPLIT_BACKEND, timeout: FAILOVER_TIMEOUT }];
  for (const backend of backends) {
    try {
      const backendUrl = backend.url + pathname + search;
      const body = backend === backends[0] ? bodyForPrimary : bodyForRetry;
      const backendResponse = await fetchWithTimeout(backendUrl, { method: request.method, headers: proxyHeaders, body: isGet ? undefined : body }, backend.timeout);
      if (backendResponse.status === 404 && pathname.startsWith('/api/')) { continue; }
      if (backendResponse.status >= 502 && backendResponse.status <= 504 && backend.name === 'railway') { markPrimaryFailure(); continue; }
      const contentType = backendResponse.headers.get('Content-Type') || '';
      if (!contentType.includes('json') && backendResponse.status >= 400 && backend.name === 'railway') { markPrimaryFailure(); continue; }
      if (backend.name === 'railway') markPrimarySuccess();
      const responseBody = await backendResponse.text();
      const response = new Response(responseBody, { status: backendResponse.status, headers: { 'Content-Type': 'application/json' } });
      response.headers.set('x-dc-hub-source', backend.name); response.headers.set('x-dc-hub-backend', backend.name); response.headers.set('x-dc-hub-cache', 'MISS');
      return response;
    } catch (err) { console.error(`[FAILOVER] ${backend.name} failed for ${pathname}: ${err.message}`); if (backend.name === 'railway') markPrimaryFailure(); }
  }
  return null;
}

async function proxyDiscoveryPath(request, pathname, search) {
  const inlineFile = INLINE_DISCOVERY[pathname];
  if (inlineFile) {
    return new Response(inlineFile.body, {
      status: 200,
      headers: {
        'Content-Type': inlineFile.contentType,
        'Access-Control-Allow-Origin': '*',
        'Cache-Control': 'public, max-age=3600',
        'x-dc-hub-source': 'worker-inline',
        'x-dc-hub-worker': '3.8.6',
      },
    });
  }
  const backends = shouldSkipPrimary()
    ? [{ name: 'replit', url: REPLIT_BACKEND, timeout: FAILOVER_TIMEOUT }]
    : [{ name: 'railway', url: RAILWAY_BACKEND, timeout: PRIMARY_TIMEOUT }, { name: 'replit', url: REPLIT_BACKEND, timeout: FAILOVER_TIMEOUT }];
  for (const backend of backends) {
    try {
      let backendPath = pathname;
      if (pathname === '/.well-known/ai-plugin.json') backendPath = '/ai-plugin.json';
      if (pathname === '/.well-known/mcp/server-card.json') backendPath = '/mcp-server-card.json';
      const backendUrl = backend.url + backendPath + search;
      const backendResponse = await fetchWithTimeout(backendUrl, { method: 'GET', headers: { 'User-Agent': request.headers.get('User-Agent') || 'DCHub-Worker/3.8.6', 'Accept': '*/*' } }, backend.timeout);
      if (backendResponse.status >= 502 && backendResponse.status <= 504 && backend.name === 'railway') { markPrimaryFailure(); continue; }
      if (backend.name === 'railway') markPrimarySuccess();
      const contentType = backendResponse.headers.get('Content-Type') || 'text/plain';
      const responseBody = await backendResponse.text();
      return new Response(responseBody, { status: backendResponse.status, headers: { 'Content-Type': contentType, 'Access-Control-Allow-Origin': '*', 'x-dc-hub-source': backend.name, 'x-dc-hub-backend': backend.name, 'Cache-Control': 'public, max-age=3600' } });
    } catch (err) { console.error(`[DISCOVERY] ${backend.name} failed for ${pathname}: ${err.message}`); if (backend.name === 'railway') markPrimaryFailure(); }
  }
  return new Response(JSON.stringify({ error: 'Discovery file temporarily unavailable', path: pathname }), { status: 502, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
}

async function transparentProxyWithFailover(request, pathname, search) {
  const requestOrigin = request.headers.get('Origin') || 'https://dchub.cloud';
  const headers = new Headers();
  for (const [key, value] of request.headers.entries()) { const lk = key.toLowerCase(); if (lk.startsWith('cf-') || lk === 'host' || lk === 'x-forwarded-for') continue; headers.set(key, value); }
  headers.set('X-Forwarded-For', request.headers.get('CF-Connecting-IP') || '');
  headers.set('X-Forwarded-Proto', 'https'); headers.set('X-Forwarded-Host', 'dchub.cloud');
  let bodyForRetry = null; let bodyForPrimary = null;
  const hasBody = request.method !== 'GET' && request.method !== 'HEAD';
  if (hasBody) { const bodyBytes = await request.arrayBuffer(); bodyForPrimary = bodyBytes; bodyForRetry = bodyBytes.slice(0); }
  const backends = shouldSkipPrimary()
    ? [{ name: 'replit', url: REPLIT_BACKEND, timeout: FAILOVER_TIMEOUT }]
    : [{ name: 'railway', url: RAILWAY_BACKEND, timeout: PRIMARY_TIMEOUT }, { name: 'replit', url: REPLIT_BACKEND, timeout: FAILOVER_TIMEOUT }];
  for (const backend of backends) {
    try {
      const backendUrl = backend.url + pathname + search;
      const body = backend === backends[0] ? bodyForPrimary : bodyForRetry;
      const backendResponse = await fetchWithTimeout(backendUrl, { method: request.method, headers, body: hasBody ? body : undefined, redirect: 'manual' }, backend.timeout);
      if (backendResponse.status >= 502 && backendResponse.status <= 504 && backend.name === 'railway') { markPrimaryFailure(); continue; }
      if (backend.name === 'railway') markPrimarySuccess();
      const responseHeaders = new Headers();
      for (const [key, value] of backendResponse.headers.entries()) { if (key.toLowerCase().startsWith('access-control-')) continue; responseHeaders.set(key, value); }
      const location = backendResponse.headers.get('Location');
      if (location) responseHeaders.set('Location', location.replace(RAILWAY_BACKEND, 'https://dchub.cloud').replace(REPLIT_BACKEND, 'https://dchub.cloud'));
      const origin = getAllowedOrigin(requestOrigin);
      responseHeaders.set('Access-Control-Allow-Origin', origin); responseHeaders.set('Access-Control-Allow-Credentials', 'true');
      responseHeaders.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
      responseHeaders.set('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With');
      responseHeaders.set('x-dc-hub-backend', backend.name);
      return new Response(backendResponse.body, { status: backendResponse.status, statusText: backendResponse.statusText, headers: responseHeaders });
    } catch (err) { console.error(`[FAILOVER-TRANSPARENT] ${backend.name} failed for ${pathname}: ${err.message}`); if (backend.name === 'railway') markPrimaryFailure(); }
  }
  const origin = getAllowedOrigin(requestOrigin);
  const errResp = jsonResponse({ error: 'Backend temporarily unreachable', message: 'Both primary and failover backends are unreachable. Please try again.', status: 502 }, 502);
  errResp.headers.set('Access-Control-Allow-Origin', origin); errResp.headers.set('Access-Control-Allow-Credentials', 'true'); errResp.headers.set('x-dc-hub-backend', 'none');
  return errResp;
}

function findNeonRoute(pathname) {
  if (NEON_ROUTES[pathname]) return NEON_ROUTES[pathname];
  for (const [route, config] of Object.entries(NEON_ROUTES)) { if (pathname === route || pathname.startsWith(route + '/')) return config; }
  return null;
}
function isBackendOnly(pathname) { return BACKEND_ONLY_PATHS.some(p => pathname.startsWith(p)); }
function isTransparentProxy(pathname) { return TRANSPARENT_PROXY_PATHS.some(p => pathname.startsWith(p)); }
function isBackendHtml(pathname) { return BACKEND_HTML_PATHS.includes(pathname); }

function trackRequest(request, pathname, ctx) {
  const userAgent = request.headers.get('User-Agent') || '';
  const ip = request.headers.get('CF-Connecting-IP') || '';
  if (!pathname.startsWith('/api/') && !pathname.startsWith('/ai/') && !pathname.startsWith('/mcp')) return;
  if (pathname.includes('/tracking') || pathname.includes('/health') || pathname === '/api/v1/failover-status') return;
  ctx.waitUntil(fetch(RAILWAY_BACKEND + '/api/ai/track-request', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: pathname, user_agent: userAgent, ip, timestamp: new Date().toISOString(), source: 'worker' })
  }).catch(() => {}));
}

// ============================================================
// ANON SESSION — KV-backed free tier enforcement
// ============================================================
const ANON_TTL_SECS  = 60;          // free session duration
const ANON_KV_TTL    = 86400;       // KV record lives 24h
const ANON_COOKIE    = 'dchub_anon'; // cookie name
const ANON_SECRET    = 'dchub2026'; // simple hmac secret — override via env var

function anonKvKey(id) { return `anon:session:${id}`; }

// Simple HMAC-less signature: sha256 not available in CF workers without SubtleCrypto
// We use a signed token: base64(id) + '.' + base64(hmac-ish checksum)
async function signId(id, secret) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig  = await crypto.subtle.sign('HMAC', key, enc.encode(id));
  return id + '.' + btoa(String.fromCharCode(...new Uint8Array(sig))).replace(/[+/=]/g, c => ({ '+': '-', '/': '_', '=': '' }[c]));
}

async function verifyToken(token, secret) {
  try {
    const dot = token.lastIndexOf('.');
    if (dot < 0) return null;
    const id  = token.substring(0, dot);
    const expected = await signId(id, secret);
    return expected === token ? id : null;
  } catch (e) { return null; }
}

function generateId() {
  return crypto.randomUUID().replace(/-/g, '').substring(0, 24);
}

function parseCookies(header) {
  const cookies = {};
  (header || '').split(';').forEach(c => {
    const [k, ...v] = c.trim().split('=');
    if (k) cookies[k.trim()] = v.join('=');
  });
  return cookies;
}

// GET /api/anon-session/check — returns remaining seconds and lock state
async function handleAnonCheck(request, env) {
  const secret = env.ANON_SECRET || ANON_SECRET;
  const cookies = parseCookies(request.headers.get('Cookie'));
  const token = cookies[ANON_COOKIE];
  const fp = new URL(request.url).searchParams.get('fp') || '';

  if (!token) {
    // No session yet — return full time remaining (init not called yet)
    return jsonResponse({ remaining: ANON_TTL_SECS, locked: false, new_session: true });
  }

  const id = await verifyToken(token, secret);
  if (!id) {
    return jsonResponse({ remaining: ANON_TTL_SECS, locked: false, new_session: true });
  }

  const record = await env.DCHUB_KV.get(anonKvKey(id), 'json').catch(() => null);
  if (!record) {
    return jsonResponse({ remaining: ANON_TTL_SECS, locked: false, new_session: true });
  }

  if (record.locked) {
    return jsonResponse({ remaining: 0, locked: true, reason: record.locked_reason || 'expired' });
  }

  const elapsed = Math.floor((Date.now() - record.start) / 1000);
  const remaining = Math.max(0, ANON_TTL_SECS - elapsed);

  // Auto-lock if elapsed on server side
  if (remaining <= 0) {
    record.locked = true;
    record.locked_reason = 'timer';
    await env.DCHUB_KV.put(anonKvKey(id), JSON.stringify(record), { expirationTtl: ANON_KV_TTL });
    return jsonResponse({ remaining: 0, locked: true, reason: 'timer' });
  }

  return jsonResponse({ remaining, locked: false, layers: record.layers || 0 });
}

// POST /api/anon-session/init — creates session, sets cookie
async function handleAnonInit(request, env) {
  const secret = env.ANON_SECRET || ANON_SECRET;
  const cookies = parseCookies(request.headers.get('Cookie'));
  const existingToken = cookies[ANON_COOKIE];
  const body = await request.json().catch(() => ({}));
  const fp = body.fp || '';

  // Check if existing valid session
  if (existingToken) {
    const id = await verifyToken(existingToken, secret);
    if (id) {
      const record = await env.DCHUB_KV.get(anonKvKey(id), 'json').catch(() => null);
      if (record) {
        if (record.locked) {
          return jsonResponse({ remaining: 0, locked: true, reason: record.locked_reason || 'expired', session_id: id });
        }
        const elapsed = Math.floor((Date.now() - record.start) / 1000);
        const remaining = Math.max(0, ANON_TTL_SECS - elapsed);
        if (remaining > 0) {
          return jsonResponse({ remaining, locked: false, session_id: id, layers: record.layers || 0 });
        }
        // Expired — lock it
        record.locked = true; record.locked_reason = 'timer';
        await env.DCHUB_KV.put(anonKvKey(id), JSON.stringify(record), { expirationTtl: ANON_KV_TTL });
        return jsonResponse({ remaining: 0, locked: true, reason: 'timer', session_id: id });
      }
    }
  }

  // Check fingerprint — same device may already have an exhausted session
  if (fp) {
    const fpKey = `anon:fp:${fp}`;
    const fpRecord = await env.DCHUB_KV.get(fpKey, 'json').catch(() => null);
    if (fpRecord && fpRecord.locked) {
      return jsonResponse({ remaining: 0, locked: true, reason: 'fingerprint_match' });
    }
  }

  // Create new session
  const id = generateId();
  const token = await signId(id, secret);
  const record = { start: Date.now(), fp, locked: false, layers: 0 };
  await env.DCHUB_KV.put(anonKvKey(id), JSON.stringify(record), { expirationTtl: ANON_KV_TTL });

  const resp = jsonResponse({ remaining: ANON_TTL_SECS, locked: false, session_id: id, layers: 0 });
  resp.headers.set('Set-Cookie',
    `${ANON_COOKIE}=${token}; Path=/; Max-Age=${ANON_KV_TTL}; SameSite=Lax; Secure`
  );
  return resp;
}

// POST /api/anon-session/lock — marks session locked
async function handleAnonLock(request, env) {
  const secret = env.ANON_SECRET || ANON_SECRET;
  const cookies = parseCookies(request.headers.get('Cookie'));
  const token = cookies[ANON_COOKIE];
  const body = await request.json().catch(() => ({}));
  const fp = body.fp || '';
  const reason = body.reason || 'timer';

  if (token) {
    const id = await verifyToken(token, secret);
    if (id) {
      const record = await env.DCHUB_KV.get(anonKvKey(id), 'json').catch(() => ({}));
      record.locked = true;
      record.locked_reason = reason;
      await env.DCHUB_KV.put(anonKvKey(id), JSON.stringify(record), { expirationTtl: ANON_KV_TTL });
    }
  }

  // Also lock by fingerprint so incognito/other browsers on same device are blocked
  if (fp) {
    const fpKey = `anon:fp:${fp}`;
    await env.DCHUB_KV.put(fpKey, JSON.stringify({ locked: true, reason, ts: Date.now() }), { expirationTtl: ANON_KV_TTL });
  }

  return jsonResponse({ success: true });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const pathname = url.pathname;

    const isWorkerRoute = pathname.startsWith('/api/') || pathname === '/health' || pathname.startsWith('/ai/') || isBackendHtml(pathname) || isDiscoveryPath(pathname);

    if (!isWorkerRoute) {
      const frontendUrl = 'https://dchub.azmartone.workers.dev' + url.pathname + url.search;
      try {
        const frontendResp = await fetch(frontendUrl, { method: request.method, headers: request.headers, redirect: 'manual' });
        const respHeaders = new Headers(frontendResp.headers);
        respHeaders.set('x-dc-hub-source', 'frontend-worker');
        return new Response(frontendResp.body, { status: frontendResp.status, statusText: frontendResp.statusText, headers: respHeaders });
      } catch (err) {
        console.error('[FRONTEND] dchub worker fetch failed:', err.message);
        return new Response('Frontend temporarily unavailable', { status: 502 });
      }
    }

    if (request.method === 'OPTIONS') return handleCORS(request);

    const isGet = request.method === 'GET';

    trackRequest(request, pathname, ctx);

    if (isDiscoveryPath(pathname)) return proxyDiscoveryPath(request, pathname, url.search);
    if (isBackendHtml(pathname)) return transparentProxyWithFailover(request, pathname, url.search);

    if (pathname === '/api/v1/failover-status') {
      const resp = jsonResponse({
        primary: 'railway', primary_url: RAILWAY_BACKEND, primary_healthy: failoverState.primaryHealthy,
        failover: 'replit', failover_url: REPLIT_BACKEND, failover_healthy: failoverState.failoverHealthy,
        consecutive_primary_failures: failoverState.consecutivePrimaryFailures,
        circuit_open: shouldSkipPrimary(),
        last_primary_failure: failoverState.lastPrimaryFailure ? new Date(failoverState.lastPrimaryFailure).toISOString() : null,
        worker_version: '3.8.6'
      });
      addCORSHeaders(resp, request);
      return resp;
    }

    if (isTransparentProxy(pathname)) return transparentProxyWithFailover(request, pathname, url.search);

    // ── Anon session endpoints ──────────────────────────────────
    if (pathname === '/api/anon-session/init' && request.method === 'POST') {
      const resp = await handleAnonInit(request, env);
      addCORSHeaders(resp, request);
      return resp;
    }
    if (pathname === '/api/anon-session/check' && request.method === 'GET') {
      const resp = await handleAnonCheck(request, env);
      addCORSHeaders(resp, request);
      return resp;
    }
    if (pathname === '/api/anon-session/lock' && request.method === 'POST') {
      const resp = await handleAnonLock(request, env);
      addCORSHeaders(resp, request);
      return resp;
    }

    const cache = caches.default;
    const cacheKey = new Request(url.toString(), { method: 'GET' });
    if (isGet) {
      try {
        const cached = await cache.match(cacheKey);
        if (cached) {
          const cacheTime = cached.headers.get('x-cache-time');
          const age = cacheTime ? (Date.now() - parseInt(cacheTime)) / 1000 : Infinity;
          if (age < CACHE_TTL) {
            const resp = new Response(cached.body, cached);
            resp.headers.set('x-dc-hub-cache', 'HIT'); resp.headers.set('x-dc-hub-cache-age', Math.round(age).toString());
            addCORSHeaders(resp, request); return resp;
          }
        }
      } catch (e) {}
    }

    if (isGet && !isBackendOnly(pathname)) {
      const neonRoute = findNeonRoute(pathname);
      if (neonRoute && env.NEON_DATABASE_URL) {
        try {
          if (neonRoute.custom) {
            const [statsRows, platformRows] = await Promise.all([queryNeon(neonRoute.query, env.NEON_DATABASE_URL), queryNeon(neonRoute.secondQuery, env.NEON_DATABASE_URL)]);
            const platforms = {};
            platformRows.forEach(r => { platforms[r.platform.toLowerCase()] = { total_requests: parseInt(r.total_requests), requests_7d: parseInt(r.requests_7d), last_seen: r.last_seen }; });
            const data = { tracking: 'persistent', total_requests_today: parseInt(statsRows[0].total_today), total_requests_all_time: parseInt(statsRows[0].total_all_time), platforms_active: parseInt(statsRows[0].active_platforms), platforms, chart_data: platforms, source: 'neon-direct' };
            const response = jsonResponse(data, 200);
            response.headers.set('x-dc-hub-source', 'neon-direct'); response.headers.set('x-dc-hub-cache', 'MISS');
            addCORSHeaders(response, request);
            const toCache = response.clone(); toCache.headers.set('x-cache-time', Date.now().toString()); toCache.headers.set('Cache-Control', `public, max-age=${CACHE_TTL}`);
            ctx.waitUntil(cache.put(cacheKey, toCache)); return response;
          }
          const rows = await queryNeon(neonRoute.query, env.NEON_DATABASE_URL);
          const data = neonRoute.transform(rows);
          const response = jsonResponse(data, 200);
          response.headers.set('x-dc-hub-source', 'neon-direct'); response.headers.set('x-dc-hub-cache', 'MISS');
          addCORSHeaders(response, request);
          const toCache = response.clone(); toCache.headers.set('x-cache-time', Date.now().toString()); toCache.headers.set('Cache-Control', `public, max-age=${CACHE_TTL}`);
          ctx.waitUntil(cache.put(cacheKey, toCache)); return response;
        } catch (neonErr) { console.error(`[NEON] Direct query failed for ${pathname}:`, neonErr.message); }
      }
    }

    const proxyResponse = await proxyWithFailover(request, pathname, url.search);
    if (proxyResponse) {
      addCORSHeaders(proxyResponse, request);
      if (isGet && proxyResponse.status === 200) {
        const toCache = proxyResponse.clone(); toCache.headers.set('x-cache-time', Date.now().toString()); toCache.headers.set('Cache-Control', `public, max-age=${CACHE_TTL}`);
        ctx.waitUntil(cache.put(cacheKey, toCache));
      }
      return proxyResponse;
    }

    if (isGet) {
      try {
        const stale = await cache.match(cacheKey);
        if (stale) {
          const cacheTime = stale.headers.get('x-cache-time');
          const age = cacheTime ? (Date.now() - parseInt(cacheTime)) / 1000 : Infinity;
          if (age < STALE_TTL) {
            const resp = new Response(stale.body, stale);
            resp.headers.set('x-dc-hub-cache', 'STALE'); resp.headers.set('x-dc-hub-cache-age', Math.round(age).toString()); resp.headers.set('x-dc-hub-source', 'stale-cache');
            addCORSHeaders(resp, request); return resp;
          }
        }
      } catch (e) {}
    }

    const errResponse = jsonResponse({ error: 'Service temporarily unavailable', message: 'Database, primary backend, and failover backend are all unreachable. Please retry shortly.', status: 503, source: 'dc-hub-worker', worker_version: '3.8.6' }, 503);
    addCORSHeaders(errResponse, request);
    return errResponse;
  }
};
