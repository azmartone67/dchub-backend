// === DC Hub iteration 5: edge fast-path semantic search ===
// Adds /api/v1/search/edge with iteration 4 filter parity served entirely
// at the Cloudflare edge via env.AI + env.VECTORIZE bindings (no Railway
// round-trip). Sub-50ms target.

const IT5_GRID_TERRITORIES = {
  PJM:      new Set(['PA','NJ','MD','DC','DE','OH','KY','NC','TN','IL','IN','MI','VA','WV']),
  ERCOT:    new Set(['TX']),
  CAISO:    new Set(['CA']),
  SPP:      new Set(['KS','OK','NE','ND','SD','AR','LA']),
  MISO:     new Set(['IL','IN','IA','MI','MN','MO','MS','MT','ND','SD','WI','AR','KY','LA']),
  SOCO:     new Set(['GA','AL','MS','FL']),
  NYISO:    new Set(['NY']),
  'ISO-NE': new Set(['CT','MA','ME','NH','RI','VT']),
  NWPP:     new Set(['WA','OR','ID','MT','UT','WY']),
  AESO:     new Set(),
};

function it5JsonResp(obj, status) {
  return new Response(JSON.stringify(obj, null, 2), {
    status: status || 200,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
    },
  });
}

function it5ApplyFilters(matches, filters) {
  if (!filters || Object.keys(filters).length === 0) return matches;
  const grid = filters.grid ? filters.grid.toUpperCase() : null;
  const gridStates = grid ? IT5_GRID_TERRITORIES[grid] : null;
  const explicitStates = filters.states
    ? new Set(filters.states.split(',').map(s => s.trim().toUpperCase()).filter(Boolean))
    : null;
  const providerQ = (filters.provider || '').toLowerCase();
  const countryQ  = (filters.country  || '').toUpperCase();
  const statusQ   = (filters.status   || '').toLowerCase();
  return matches.filter(m => {
    const md = m.metadata || {};
    const st = (md.state    || '').toUpperCase();
    const co = (md.country  || '').toUpperCase();
    const pr = (md.provider || '').toLowerCase();
    const ss = (md.status   || '').toLowerCase();
    const mw = md.power_mw || 0;
    if (gridStates && !gridStates.has(st)) return false;
    if (explicitStates && !explicitStates.has(st)) return false;
    if (providerQ && !pr.includes(providerQ)) return false;
    if (countryQ && co !== countryQ) return false;
    if (statusQ && !ss.includes(statusQ)) return false;
    if (filters.min_mw != null && mw < filters.min_mw) return false;
    if (filters.max_mw != null && mw > filters.max_mw) return false;
    return true;
  });
}

async function it5HandleEdgeSearch(request, env) {
  const t0 = Date.now();
  const url = new URL(request.url);
  const q = (url.searchParams.get('q') || '').trim();
  if (!q) return it5JsonResp({ error: 'q parameter required' }, 400);

  if (!env.AI || !env.VECTORIZE) {
    return it5JsonResp({
      error: 'feature_unavailable',
      message: 'AI or VECTORIZE binding missing. Redeploy worker with both bindings attached.',
    }, 503);
  }

  const topK = Math.max(1, Math.min(parseInt(url.searchParams.get('topK') || '5', 10) || 5, 50));

  const filters = {};
  for (const k of ['grid','states','provider','country','status']) {
    const v = (url.searchParams.get(k) || '').trim();
    if (v) filters[k] = v;
  }
  for (const k of ['min_mw','max_mw']) {
    const raw = url.searchParams.get(k);
    if (raw != null && raw !== '') {
      const n = parseFloat(raw);
      if (!isNaN(n)) filters[k] = n;
    }
  }
  if (filters.grid && !IT5_GRID_TERRITORIES[filters.grid.toUpperCase()]) {
    return it5JsonResp({
      error: 'unknown-grid',
      grid: filters.grid,
      available: Object.keys(IT5_GRID_TERRITORIES).sort(),
    }, 400);
  }

  const tEmbed = Date.now();
  const emb = await env.AI.run('@cf/baai/bge-base-en-v1.5', { text: [q] });
  const vector = emb && emb.data && emb.data[0];
  if (!vector) {
    return it5JsonResp({ error: 'embed-failed', detail: emb }, 502);
  }
  const embedMs = Date.now() - tEmbed;

  const filterCount = Object.keys(filters).length;
  const fetchK = filterCount > 0 ? Math.min(topK * 5, 50) : topK;

  const tQuery = Date.now();
  const qres = await env.VECTORIZE.query(vector, { topK: fetchK, returnMetadata: 'all' });
  const queryMs = Date.now() - tQuery;

  let matches = qres.matches || [];
  const preFilter = matches.length;
  if (filterCount > 0) matches = it5ApplyFilters(matches, filters);
  const postFilter = matches.length;
  matches = matches.slice(0, topK);

  const flat = matches.map(m => ({
    id: m.id,
    score: m.score,
    name:     (m.metadata || {}).name,
    provider: (m.metadata || {}).provider,
    city:     (m.metadata || {}).city,
    state:    (m.metadata || {}).state,
    country:  (m.metadata || {}).country,
    lat:      (m.metadata || {}).lat,
    lng:      (m.metadata || {}).lng,
    power_mw: (m.metadata || {}).power_mw,
    status:   (m.metadata || {}).status,
  }));

  return it5JsonResp({
    query: q,
    topK: topK,
    count: flat.length,
    runtime: 'cloudflare-edge',
    matches: flat,
    filters: filterCount ? filters : null,
    filter_stats: {
      fetched: preFilter,
      matched_filters: postFilter,
      returned: flat.length,
    },
    timing_ms: {
      embed: embedMs,
      query: queryMs,
      total: Date.now() - t0,
    },
    index: 'dchub-facilities',
    model: '@cf/baai/bge-base-en-v1.5',
    note: 'Hydration not available on edge runtime; use Flask /api/v1/search/semantic?hydrate=true for full Neon row.',
  }, 200);
}

function it5HandleGrids() {
  const territories = {};
  for (const grid of Object.keys(IT5_GRID_TERRITORIES)) {
    territories[grid] = [...IT5_GRID_TERRITORIES[grid]].sort();
  }
  return it5JsonResp({
    grids: Object.keys(IT5_GRID_TERRITORIES).sort(),
    territories: territories,
    runtime: 'cloudflare-edge',
    note: 'State-level approximation. Some states span multiple ISOs; the listed grid is the primary coverage for filtering.',
  }, 200);
}
// === end iteration 5 helpers ===
