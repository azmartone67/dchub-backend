/**
 * dchub-neon-failover — Phase GG (2026-05-15)
 *
 * Reads the 5 most critical Railway endpoints directly from Neon when
 * Railway is unavailable. Strategy:
 *
 *   1. Try Railway with a tight timeout (FAILOVER_TIMEOUT_MS, default 12s).
 *   2. If Railway responds 2xx -> proxy as-is + set X-DCHub-Source: railway.
 *   3. If Railway times out / 5xx -> compute the response from Neon
 *      directly via Hyperdrive + set X-DCHub-Source: neon-failover.
 *
 * Read-only. No writes. No auth (these endpoints are already public on
 * the Railway side). The Worker stays out of the way when Railway is
 * healthy — failover is the exception, not the rule.
 *
 * Deployment: see wrangler.toml header.
 */

import { Client } from 'pg';

const ROUTES = {
  '/api/v1/health':                              healthHandler,
  '/api/v1/stats':                               statsHandler,
  '/api/v1/freshness/radar':                    radarHandler,
  '/api/v1/dcpi/scores':                        dcpiScoresHandler,
  '/api/v1/facilities/state-status-counts':     stateStatusCountsHandler,
};

// Prefix matchers for dynamic routes.
const PREFIX_ROUTES = [
  { prefix: '/api/v1/dcpi/scores/', handler: dcpiSingleScoreHandler },
];

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // Look up handler — exact match first, then prefix.
    let handler = ROUTES[path];
    let pathParam = null;
    if (!handler) {
      for (const { prefix, handler: h } of PREFIX_ROUTES) {
        if (path.startsWith(prefix)) {
          handler = h;
          pathParam = decodeURIComponent(path.slice(prefix.length));
          break;
        }
      }
    }
    if (!handler) {
      return new Response('Not found in failover worker', { status: 404 });
    }

    // 1. Try Railway first.
    const timeoutMs = parseInt(env.FAILOVER_TIMEOUT_MS || '12000', 10);
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), timeoutMs);
      const railwayUrl = env.RAILWAY_ORIGIN + path + url.search;
      const railwayResp = await fetch(railwayUrl, { signal: ac.signal });
      clearTimeout(t);
      if (railwayResp.ok) {
        // Stream the live Railway response back; tag the source.
        const headers = new Headers(railwayResp.headers);
        headers.set('X-DCHub-Source', 'railway');
        headers.set('X-DCHub-Worker', env.WORKER_VERSION || 'neon-failover/1');
        return new Response(railwayResp.body, {
          status: railwayResp.status, headers,
        });
      }
      // Non-2xx falls through to failover.
    } catch (_err) {
      // Timeout / network error -> failover.
    }

    // 2. Failover via Neon.
    const client = new Client(env.NEON_DB.connectionString);
    try {
      await client.connect();
      const payload = await handler(client, url, pathParam);
      return jsonResponse(payload, env, 'neon-failover');
    } catch (err) {
      return jsonResponse({
        error: 'failover_db_error',
        detail: String(err && err.message || err).slice(0, 200),
      }, env, 'neon-failover-error', 500);
    } finally {
      ctx.waitUntil(client.end());
    }
  },
};

function jsonResponse(payload, env, source, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'public, max-age=60, stale-while-revalidate=300',
      'Access-Control-Allow-Origin': '*',
      'X-DCHub-Source': source,
      'X-DCHub-Worker': env.WORKER_VERSION || 'neon-failover/1',
    },
  });
}

// ── Handlers (called only on Railway failover) ─────────────────────────

async function healthHandler(client) {
  const r = await client.query('SELECT 1 AS ok');
  return {
    status: 'ok',
    source: 'neon-failover',
    db: r.rows[0]?.ok === 1 ? 'connected' : 'disconnected',
    note: 'Railway unreachable — answered from Cloudflare/Neon failover.',
  };
}

async function statsHandler(client) {
  const [facilities, deals, substations, fiber, gas, pipeline] = await Promise.all([
    safeCount(client, 'SELECT COUNT(*) FROM facilities'),
    safeCount(client, 'SELECT COUNT(*) FROM deals'),
    safeCount(client, 'SELECT COUNT(*) FROM substations'),
    safeCount(client, 'SELECT COUNT(*) FROM fiber_routes'),
    safeCount(client, 'SELECT COUNT(*) FROM gas_pipelines'),
    safeCount(client, 'SELECT COUNT(*) FROM capacity_pipeline'),
  ]);
  return {
    success: true,
    facilities, deals, substations,
    fiber_routes: fiber, gas_pipelines: gas, curated_pipeline_count: pipeline,
    data_source: 'neon-failover',
    generated_at: new Date().toISOString(),
  };
}

async function radarHandler(client) {
  // Mirrors GET /api/v1/freshness/radar — single SELECT off the registry.
  const r = await client.query(`
    SELECT domain, source_table, source_ts_column, last_record_at,
           row_count, sla_hours, age_hours, status, detail, checked_at
      FROM data_domain_freshness ORDER BY domain
  `).catch(() => ({ rows: [] }));
  const domains = r.rows.map(row => ({
    ...row,
    last_record_at: row.last_record_at?.toISOString?.() ?? row.last_record_at,
    checked_at:     row.checked_at?.toISOString?.()     ?? row.checked_at,
  }));
  const summary = {
    domains: domains.length,
    fresh:   domains.filter(d => d.status === 'fresh').length,
    warning: domains.filter(d => d.status === 'warning').length,
    breach:  domains.filter(d => d.status === 'breach').length,
    unknown: domains.filter(d => d.status === 'unknown').length,
  };
  return {
    as_of: new Date().toISOString(),
    summary, domains,
  };
}

async function dcpiScoresHandler(client, url) {
  const iso = url.searchParams.get('iso');
  const verdict = url.searchParams.get('verdict');
  const limit = Math.min(parseInt(url.searchParams.get('limit') || '500', 10), 500);
  const params = [];
  let where = 'WHERE published = true';
  if (iso) { params.push(iso); where += ` AND iso = $${params.length}`; }
  if (verdict) { params.push(verdict); where += ` AND verdict = $${params.length}`; }
  params.push(limit);
  const sql = `
    SELECT DISTINCT ON (market_slug)
           market_slug, market_name, state, iso, verdict,
           excess_power_score, constraint_score, time_to_power_months,
           computed_at
      FROM market_power_scores
      ${where}
      ORDER BY market_slug, computed_at DESC
      LIMIT $${params.length}
  `;
  const r = await client.query(sql, params).catch(() => ({ rows: [] }));
  return {
    count: r.rows.length,
    scores: r.rows.map(row => ({
      ...row,
      computed_at: row.computed_at?.toISOString?.() ?? row.computed_at,
    })),
  };
}

async function dcpiSingleScoreHandler(client, _url, slug) {
  if (!slug) return { error: 'missing slug' };
  const r = await client.query(`
    SELECT * FROM market_power_scores
     WHERE market_slug = $1 AND published = true
     ORDER BY computed_at DESC LIMIT 1
  `, [slug]).catch(() => ({ rows: [] }));
  if (!r.rows[0]) return { error: 'market not found', slug };
  const row = r.rows[0];
  return {
    ...row,
    computed_at: row.computed_at?.toISOString?.() ?? row.computed_at,
  };
}

async function stateStatusCountsHandler(client) {
  // Mirrors /api/v1/facilities/state-status-counts (the /daily spine).
  const r = await client.query(`
    SELECT UPPER(TRIM(state)) AS st, LOWER(TRIM(status)) AS status, COUNT(*) AS n
      FROM facilities
     WHERE country IN ('US', 'USA', 'United States')
       AND state IS NOT NULL AND TRIM(state) <> ''
       AND status IS NOT NULL AND TRIM(status) <> ''
     GROUP BY 1, 2
  `).catch(() => ({ rows: [] }));
  const STATES = {
    AL:'ALABAMA',AK:'ALASKA',AZ:'ARIZONA',AR:'ARKANSAS',CA:'CALIFORNIA',
    CO:'COLORADO',CT:'CONNECTICUT',DE:'DELAWARE',DC:'WASHINGTON DC',
    FL:'FLORIDA',GA:'GEORGIA',HI:'HAWAII',ID:'IDAHO',IL:'ILLINOIS',
    IN:'INDIANA',IA:'IOWA',KS:'KANSAS',KY:'KENTUCKY',LA:'LOUISIANA',
    ME:'MAINE',MD:'MARYLAND',MA:'MASSACHUSETTS',MI:'MICHIGAN',MN:'MINNESOTA',
    MS:'MISSISSIPPI',MO:'MISSOURI',MT:'MONTANA',NE:'NEBRASKA',NV:'NEVADA',
    NH:'NEW HAMPSHIRE',NJ:'NEW JERSEY',NM:'NEW MEXICO',NY:'NEW YORK',
    NC:'NORTH CAROLINA',ND:'NORTH DAKOTA',OH:'OHIO',OK:'OKLAHOMA',
    OR:'OREGON',PA:'PENNSYLVANIA',RI:'RHODE ISLAND',SC:'SOUTH CAROLINA',
    SD:'SOUTH DAKOTA',TN:'TENNESSEE',TX:'TEXAS',UT:'UTAH',VT:'VERMONT',
    VA:'VIRGINIA',WA:'WASHINGTON',WV:'WEST VIRGINIA',WI:'WISCONSIN',WY:'WYOMING',
  };
  const OP = new Set(['operational','active','live','in service','online']);
  const UC = new Set(['under construction','construction','under_construction','expanding','pre-construction','commissioning']);
  const ANN = new Set(['announced','planned','planning','approved','proposed','in development','under development','permitting','development','pre-planning']);
  const states = {};
  for (const row of r.rows) {
    const name = STATES[row.st];
    if (!name) continue;
    const b = states[name] || (states[name] = { name, op: 0, uc: 0, ann: 0 });
    const n = parseInt(row.n || 0, 10);
    if (OP.has(row.status))  b.op  += n;
    else if (UC.has(row.status))  b.uc  += n;
    else if (ANN.has(row.status)) b.ann += n;
  }
  const out = Object.values(states).sort((a, b) => (b.op + b.uc + b.ann) - (a.op + a.uc + a.ann));
  return {
    success: true,
    unit: 'facilities',
    as_of: new Date().toISOString().slice(0, 10),
    source: 'neon-failover',
    states: out,
    totals: {
      op: out.reduce((s, r) => s + r.op, 0),
      uc: out.reduce((s, r) => s + r.uc, 0),
      ann: out.reduce((s, r) => s + r.ann, 0),
    },
    state_count: out.length,
    generated_at: new Date().toISOString(),
  };
}

async function safeCount(client, sql) {
  try {
    const r = await client.query(sql);
    return parseInt(r.rows[0]?.count || 0, 10);
  } catch (_e) {
    return 0;
  }
}
