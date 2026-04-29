/**
 * DC Hub — Market Intelligence Page
 * ------------------------------------------------------------------
 * Shared renderer for /markets/<slug>.html pages. Every page is a
 * 35-line SEO wrapper; this script turns it into a live intelligence
 * dashboard by fanning out to /api/v1/* endpoints (which proxy to
 * the DC Hub MCP backend on Railway).
 *
 * Failure philosophy: any endpoint can 4xx/5xx — we render what we
 * have and hide what we don't. Never show a broken page.
 *
 * Version: 8.0 (2026-04-16)
 */
(function () {
  'use strict';

  const API_BASE = '/api/v1';
  const REGISTRY_URL = '/markets/registry.json';
  const CACHE_KEY = 'dchub-market-cache-v8';
  const CACHE_TTL_MS = 15 * 60 * 1000; // 15 min client cache

  // ---------------------------------------------------------------
  // Utilities
  // ---------------------------------------------------------------

  const qs = (sel, root = document) => root.querySelector(sel);
  const h = (tag, attrs = {}, children = []) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') el.className = v;
      else if (k === 'html') el.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) el.setAttribute(k, v);
    }
    (Array.isArray(children) ? children : [children]).forEach(c => {
      if (c == null) return;
      el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return el;
  };

  const fmtMW = (v) => {
    if (v == null || isNaN(v)) return '—';
    const n = Number(v);
    return n >= 1000 ? (n / 1000).toFixed(1) + ' GW' : Math.round(n) + ' MW';
  };
  const fmtPct = (v) => (v == null || isNaN(v) ? '—' : (Number(v) * (Math.abs(v) < 1 ? 100 : 1)).toFixed(1) + '%');
  const fmtKwh = (v) => (v == null || isNaN(v) ? '—' : '$' + Number(v).toFixed(3) + '/kWh');
  const fmtNum = (v) => (v == null || isNaN(v) ? '—' : Number(v).toLocaleString());

  const relativeTime = (iso) => {
    if (!iso) return '';
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.round(diff / 60) + ' min ago';
    if (diff < 86400) return Math.round(diff / 3600) + ' h ago';
    return Math.round(diff / 86400) + ' d ago';
  };

  function authHeaders() {
    const headers = { 'Accept': 'application/json' };
    try {
      const token = localStorage.getItem('dchub_token');
      if (token) headers['Authorization'] = 'Bearer ' + token;
      const apiKey = localStorage.getItem('dchub_api_key');
      if (apiKey) headers['X-API-Key'] = apiKey;
    } catch (e) { /* localStorage disabled */ }
    return headers;
  }

  async function fetchJSON(url, opts = {}) {
    const cache = readCache(url);
    if (cache && !opts.bypass) return cache;
    try {
      const resp = await fetch(url, { headers: authHeaders() });
      if (!resp.ok) return { _error: resp.status, _url: url };
      const data = await resp.json();
      writeCache(url, data);
      return data;
    } catch (e) {
      return { _error: 'network', _url: url, _message: e.message };
    }
  }

  function readCache(url) {
    try {
      const raw = sessionStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const store = JSON.parse(raw);
      const entry = store[url];
      if (!entry) return null;
      if (Date.now() - entry.ts > CACHE_TTL_MS) return null;
      return entry.data;
    } catch { return null; }
  }

  function writeCache(url, data) {
    try {
      const raw = sessionStorage.getItem(CACHE_KEY);
      const store = raw ? JSON.parse(raw) : {};
      store[url] = { ts: Date.now(), data };
      sessionStorage.setItem(CACHE_KEY, JSON.stringify(store));
    } catch { /* quota / disabled — ignore */ }
  }

  // ---------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------

  async function boot() {
    const slug = qs('meta[name="market-slug"]')?.content;
    const container = qs('#market-container');
    if (!slug || !container) return;

    const registry = await fetchJSON(REGISTRY_URL);
    if (registry._error) return renderFatal(container, 'Registry failed to load');

    const market = registry.markets[slug];
    if (!market) return renderFatal(container, `Unknown market: ${slug}`);

    // Render the scaffold with static metadata — guaranteed paint even
    // if all API calls fail.
    renderScaffold(container, slug, market, registry);

    // Fan out to every live endpoint in parallel.
    const calls = buildEndpoints(slug, market);
    const results = await Promise.allSettled(calls.map(c => fetchJSON(c.url).then(d => ({ key: c.key, data: d }))));

    const bag = {};
    for (const r of results) if (r.status === 'fulfilled') bag[r.value.key] = r.value.data;

    // Render each data section; each renderer is responsible for its own
    // fallback/hide behaviour.
    renderStats(market, bag.marketIntel, bag.energy);
    renderGdciBadge(market, bag.gdci);
    renderGrid(market, bag.grid);
    renderEnergy(market, bag.energy);
    renderFacilities(market, bag.facilities);
    renderPipeline(market, bag.pipeline);
    renderTax(market, bag.tax);
    renderInfra(market, bag.infra);
    renderFiber(market, bag.fiber);
    renderNews(market, bag.news);
    renderRelated(registry, market);
    stampFooter(bag);
  }

  // ---------------------------------------------------------------
  // Endpoints — adjust here if backend routes change
  // ---------------------------------------------------------------

  function buildEndpoints(slug, m) {
    const q = (obj) => Object.entries(obj).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
    const list = [
      { key: 'marketIntel', url: `${API_BASE}/markets/${slug}` },
      { key: 'gdci',        url: `${API_BASE}/gdci?${q({ market: slug })}` },
      { key: 'facilities',  url: `${API_BASE}/facilities?${q({ city: m.name.split(/[,\/]/)[0].trim(), country: m.country, limit: 12 })}` },
      { key: 'pipeline',    url: `${API_BASE}/pipeline?${q({ market: slug })}` },
      { key: 'news',        url: `${API_BASE}/news?${q({ market: slug, limit: 5 })}` },
      { key: 'fiber', url: `${API_BASE}/fiber/metro/${slug}` },
      { key: 'infra',       url: `${API_BASE}/infrastructure?${q({ lat: m.lat, lon: m.lon, layer: 'all', radius_km: 50, limit: 15 })}` },
    ];
    if (m.iso) list.push({ key: 'grid', url: `${API_BASE}/grid-headroom?iso=${encodeURIComponent(m.iso)}` });
    if (m.state && m.country === 'US') {
      list.push({ key: 'energy', url: `${API_BASE}/energy/summary?${q({ state: m.state })}` });
      list.push({ key: 'tax',    url: `${API_BASE}/tax-incentives?${q({ state: m.state })}` });
    }
    return list;
  }

  // ---------------------------------------------------------------
  // Scaffold — painted immediately from registry (fast first paint + SEO)
  // ---------------------------------------------------------------

  function renderScaffold(container, slug, m, reg) {
    const tierMeta = reg.tiers[m.tier] || { label: m.tier, color: '#3b82f6' };
    container.innerHTML = '';
    container.appendChild(h('div', { class: 'bc' }, [
      h('a', { href: '/' }, 'DC Hub'), ' › ',
      h('a', { href: '/markets/' }, 'Markets'), ' › ',
      m.name,
    ]));
    container.appendChild(h('div', { class: 'mp-hero' }, [
      h('div', { class: 'mp-ht' }, [
        h('span', { class: 'fl' }, m.flag),
        h('h1', {}, m.name),
        h('span', { class: 'bdg', style: `background:${tierMeta.color}1f;color:${tierMeta.color}` }, tierMeta.label),
        h('span', { id: 'mp-gdci-badge' }),
      ]),
      h('div', { class: 'tl' }, m.tagline),
      h('div', { class: 'desc' }, m.description || ''),
      h('div', { class: 'sg', id: 'mp-stats' }, loadingCards(5)),
    ]));

    const layout = h('div', { class: 'mp-ct' }, [
      h('div', { class: 'mp-main' }, [
        section('⚡ Live Grid Status', 'mp-grid'),
        section('💰 Energy Pricing', 'mp-energy'),
        section('🏢 Top Facilities', 'mp-facilities'),
        section('🏗️ Pipeline & Absorption', 'mp-pipeline'),
        section('📰 Latest Market News', 'mp-news'),
      ]),
      h('div', { class: 'mp-side' }, [
        section('🏛️ Tax Incentives', 'mp-tax'),
        section('🔌 Power Infrastructure', 'mp-infra'),
        section('🌐 Fiber & Connectivity', 'mp-fiber'),
        section('🔗 Related Markets', 'mp-related'),
      ]),
    ]);
    container.appendChild(layout);
    container.appendChild(h('div', { class: 'mp-cta' }, [
      h('a', { href: '/', class: 'btn' }, '🗺️ Explore on Map'),
      h('a', { href: '/markets/', class: 'btn bo' }, '← All Markets'),
      h('a', { href: `mailto:hello@dchub.cloud?subject=Market Inquiry — ${encodeURIComponent(m.name)}`, class: 'btn bo' }, '📧 Contact'),
    ]));
    container.appendChild(h('div', { class: 'mp-stamp', id: 'mp-stamp' }, '⏳ Loading live data…'));
  }

  function section(title, id) {
    return h('section', { class: 'mp-section', id: `${id}-wrap` }, [
      h('h2', {}, title),
      h('div', { class: 'mp-skel', id }, 'Loading…'),
    ]);
  }

  function loadingCards(n) {
    return Array.from({ length: n }, () => h('div', { class: 'sc sc-skel' }, [
      h('div', { class: 'v' }, '—'),
      h('div', { class: 'l' }, 'Loading'),
    ]));
  }

  function hideSection(id) {
    const wrap = qs(`#${id}-wrap`);
    if (wrap) wrap.style.display = 'none';
  }

  function setHTML(id, htmlStr) {
    const el = qs(`#${id}`);
    if (el) el.innerHTML = htmlStr;
  }

  // ---------------------------------------------------------------
  // Renderers — each defensively handles missing data
  // ---------------------------------------------------------------

  function renderStats(m, data, energyData) {
    const el = qs('#mp-stats');
    if (!el) return;
    el.innerHTML = '';
    const d = data && !data._error ? (data.data || data) : {};
    const st = d.stats || {};
    const bs = d.by_status || {};
    const e = energyData && !energyData._error ? (energyData.data || energyData) : {};
    const rr = e.retail_rates || {};
    const avgKwh = rr.avg_cents_kwh != null ? rr.avg_cents_kwh / 100 : (e.avg_price_kwh ?? e.average_price_kwh ?? e.retail_avg_kwh ?? e.avg_kwh);
    const stats = [
      { v: st.facility_count ?? d.facilities_count ?? d.facilities ?? '—', l: 'Facilities' },
      { v: bs.Operational != null ? bs.Operational : (d.total_supply_mw != null ? fmtMW(d.total_supply_mw) : '—'), l: 'Operational' },
      { v: bs['Under Construction'] != null ? bs['Under Construction'] : (d.pipeline_mw != null ? fmtMW(d.pipeline_mw) : '—'), l: 'Pipeline' },
      { v: avgKwh != null ? fmtKwh(avgKwh) : '—', l: 'Avg $/kWh' },
      { v: d.vacancy_rate != null ? fmtPct(d.vacancy_rate) : '—', l: 'Vacancy' },
    ];
    stats.forEach(s => el.appendChild(h('div', { class: 'sc' }, [
      h('div', { class: 'v' }, String(s.v)),
      h('div', { class: 'l' }, s.l),
    ])));
    if (!data || data._error) el.appendChild(h('div', { class: 'sc sc-note' }, 'Live stats unavailable — showing static registry'));
  }

  function renderGdciBadge(m, data) {
    const el = qs('#mp-gdci-badge');
    if (!el) return;
    if (!data || data._error) return;
    const d = data.data || data;
    const score = d.score ?? d.index_score;
    const rank  = d.rank ?? d.global_rank;
    const delta = d.weekly_change ?? d.delta;
    if (score == null && rank == null) return;
    const arrow = delta > 0 ? '▲' : delta < 0 ? '▼' : '•';
    const color = delta > 0 ? '#22c55e' : delta < 0 ? '#ef4444' : '#64748b';
    el.innerHTML = '';
    el.appendChild(h('a', { href: '/gdci', class: 'gdci-badge', title: 'DC Hub Global Intelligence Index' }, [
      h('span', { class: 'gb-rank' }, rank ? `#${rank}` : ''),
      h('span', { class: 'gb-score' }, score != null ? `Score ${score}` : ''),
      delta != null ? h('span', { class: 'gb-delta', style: `color:${color}` }, `${arrow} ${Math.abs(delta)}`) : null,
    ]));
  }

  function renderGrid(m, data) {
    if (!data || data._error) return hideSection('mp-grid');
    const d = data.data || data;
    const rows = [];
    if (d.current_demand_mw) rows.push(['Current demand', fmtMW(d.current_demand_mw)]);
    if (d.peak_today_mw)     rows.push(['Peak today',     fmtMW(d.peak_today_mw)]);
    if (d.reserve_margin)    rows.push(['Reserve margin', fmtPct(d.reserve_margin)]);
    if (d.queue_gw)          rows.push(['Interconnection queue', (d.queue_gw).toFixed(1) + ' GW']);
    if (d.wait_years)        rows.push(['Avg queue wait', d.wait_years + ' years']);
    if (d.fuel_mix) {
      const fm = d.fuel_mix;
      const line = Object.entries(fm).sort(([,a],[,b]) => b-a).slice(0,3).map(([k,v]) => `${k} ${Math.round(v*100)}%`).join(' · ');
      rows.push(['Fuel mix', line]);
    }
    if (!rows.length) return hideSection('mp-grid');
    setHTML('mp-grid', rows.map(([k,v]) => `<div class="ir"><span class="il">${k}</span><span class="iv">${v}</span></div>`).join('')
      + (d.source ? `<div class="src">Source: ${d.source}</div>` : ''));
  }

  function renderEnergy(m, data) {
    if (!data || data._error) return hideSection('mp-energy');
    const d = data.data || data;
    const rate = d.retail_rate_kwh ?? d.industrial_rate_kwh ?? d.avg_rate;
    const trend = d.yoy_change ?? d.change_yoy;
    if (rate == null) return hideSection('mp-energy');
    const trendStr = trend != null ? `${trend > 0 ? '+' : ''}${(trend*100).toFixed(1)}% YoY` : '';
    setHTML('mp-energy',
      `<div class="big-num">${fmtKwh(rate)}</div>`+
      `<div class="sub">Industrial retail rate ${trendStr ? '· ' + trendStr : ''}</div>`+
      (d.state_avg ? `<div class="ir"><span class="il">State avg</span><span class="iv">${fmtKwh(d.state_avg)}</span></div>` : '')+
      (d.iso_avg ? `<div class="ir"><span class="il">ISO avg</span><span class="iv">${fmtKwh(d.iso_avg)}</span></div>` : '')+
      (d.source ? `<div class="src">Source: ${d.source}</div>` : '')
    );
  }

  function renderFacilities(m, data) {
    if (!data || data._error) return hideSection('mp-facilities');
    const list = data.data || data.facilities || data.results || data;
    if (!Array.isArray(list) || !list.length) return hideSection('mp-facilities');
    const rows = list.slice(0, 10).map(f => {
      const slug = f.slug || f.id;
      const name = f.name || f.facility_name || '(unnamed)';
      const op = f.provider || f.operator || '';
      const mw = f.power_mw && typeof f.power_mw === 'number' ? fmtMW(f.power_mw) : (typeof f.power_mw === 'string' && !f.power_mw.includes('Upgrade') ? f.power_mw : '');
      const href = slug ? `/facilities/${slug}` : '#';
      return `<a class="rc" href="${href}"><span><strong>${name}</strong>${op ? ` <span class="op">${op}</span>` : ''}</span><span class="ra">${mw || '→'}</span></a>`;
    });
    setHTML('mp-facilities', rows.join('') +
      `<a class="mp-more" href="/facilities?city=${encodeURIComponent(m.name)}">See all facilities in ${m.name} →</a>`);
  }

  function renderPipeline(m, data) {
    if (!data || data._error) return hideSection('mp-pipeline');
    const d = data.data || data;
    const items = d.projects || d.developments || [];
    const stats = [
      ['Under construction', d.under_construction_mw != null ? fmtMW(d.under_construction_mw) : null],
      ['Planned',           d.planned_mw != null ? fmtMW(d.planned_mw) : null],
      ['Committed/preleased', d.preleased_pct != null ? fmtPct(d.preleased_pct) : null],
      ['Absorption rate',   d.absorption_rate != null ? fmtPct(d.absorption_rate) : null],
    ].filter(([,v]) => v);
    if (!stats.length && !items.length) return hideSection('mp-pipeline');
    let html = stats.map(([k,v]) => `<div class="ir"><span class="il">${k}</span><span class="iv">${v}</span></div>`).join('');
    if (items.length) {
      html += '<div class="mp-subhead">Top projects</div>';
      html += items.slice(0,6).map(p =>
        `<div class="proj"><div class="proj-name">${p.name || p.project || '—'}</div>`+
        `<div class="proj-meta">${p.operator || ''} · ${p.capacity_mw ? fmtMW(p.capacity_mw) : ''} · ${p.eta || p.completion || ''}</div></div>`).join('');
    }
    setHTML('mp-pipeline', html);
  }

  function renderTax(m, data) {
    if (!data || data._error) return hideSection('mp-tax');
    const d = data.data || data;
    const programs = d.programs || d.incentives || [];
    if (!programs.length) return hideSection('mp-tax');
    setHTML('mp-tax', programs.slice(0,5).map(p =>
      `<div class="inc"><div class="inc-name">${p.name || p.program}</div>`+
      `<div class="inc-meta">${p.type || ''}${p.savings ? ' · ' + p.savings : ''}</div></div>`
    ).join('') + `<a class="mp-more" href="/tax-incentives?state=${m.state}">All ${m.state} incentives →</a>`);
  }

  function renderInfra(m, data) {
    if (!data || data._error) return hideSection('mp-infra');
    const d = data.data || data;
    const subs = d.substations?.length || d.substations_count || 0;
    const tx   = d.transmission?.length || d.transmission_count || 0;
    const gas  = d.gas_pipelines?.length || d.gas_pipelines_count || 0;
    const plants = d.power_plants?.length || d.power_plants_count || 0;
    if (!subs && !tx && !gas && !plants) return hideSection('mp-infra');
    setHTML('mp-infra',
      `<div class="ir"><span class="il">Substations (50km)</span><span class="iv">${subs}</span></div>`+
      `<div class="ir"><span class="il">Transmission lines</span><span class="iv">${tx}</span></div>`+
      `<div class="ir"><span class="il">Gas pipelines</span><span class="iv">${gas}</span></div>`+
      `<div class="ir"><span class="il">Power plants</span><span class="iv">${plants}</span></div>`+
      `<a class="mp-more" href="/land-power-map?lat=${m.lat}&lon=${m.lon}">Open on Land & Power map →</a>`
    );
  }

  function renderFiber(m, data) {
    if (!data || data._error) return hideSection('mp-fiber');
    const d = data.data || data;
    const carriers = d.carriers || d.top_carriers || [];
    const routes = d.routes_count || d.long_haul_count;
    if (!carriers.length && !routes) return hideSection('mp-fiber');
    let html = '';
    if (carriers.length) html += `<div class="ir"><span class="il">Major carriers</span><span class="iv">${carriers.slice(0,6).map(c => c.name || c.carrier || c).join(', ')}</span></div>`;
    if (routes)           html += `<div class="ir"><span class="il">Long-haul routes</span><span class="iv">${routes}</span></div>`;
    if (d.ix_presence)    html += `<div class="ir"><span class="il">IX presence</span><span class="iv">${d.ix_presence}</span></div>`;
    setHTML('mp-fiber', html);
  }

  function renderNews(m, data) {
    if (!data || data._error) return hideSection('mp-news');
    const items = data.data || data.articles || data.items || [];
    if (!Array.isArray(items) || !items.length) return hideSection('mp-news');
    setHTML('mp-news', items.slice(0,5).map(n =>
      `<a class="news-item" href="${n.url || '#'}" target="_blank" rel="noopener">`+
      `<div class="news-title">${n.title || n.headline}</div>`+
      `<div class="news-meta">${n.source || ''} · ${n.published_at ? relativeTime(n.published_at) : ''}</div></a>`
    ).join('') + `<a class="mp-more" href="/news?market=${m.name.replace(/\s/g,'-').toLowerCase()}">All news →</a>`);
  }

  function renderRelated(reg, m) {
    const related = (m.related || []).map(slug => reg.markets[slug]).filter(Boolean);
    if (!related.length) return hideSection('mp-related');
    setHTML('mp-related', related.map(r =>
      `<a class="rc" href="${r.name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'')}">`+
      `<span>${r.flag} ${r.name}</span><span class="ra">→</span></a>`
    ).join(''));
  }

  function stampFooter(bag) {
    const el = qs('#mp-stamp');
    if (!el) return;
    const ok = Object.values(bag).filter(v => v && !v._error).length;
    const total = Object.keys(bag).length;
    const failures = Object.entries(bag).filter(([,v]) => v && v._error);
    el.innerHTML = `<span class="ok-dot"></span> Live intelligence · ${ok}/${total} sources responding · updated ${new Date().toLocaleTimeString()}`;
    if (failures.length) {
      el.innerHTML += ` <button class="retry-btn" onclick="location.reload()">Retry</button>`;
      console.warn('[dchub] Offline sources:', failures);
    }
  }

  function renderFatal(container, msg) {
    container.innerHTML = '';
    container.appendChild(h('div', { class: 'mp-fatal' }, [
      h('h2', {}, 'Market page failed to load'),
      h('p',  {}, msg),
      h('a',  { href: '/markets/', class: 'btn' }, '← All Markets'),
    ]));
  }

  // ---------------------------------------------------------------
  // Go
  // ---------------------------------------------------------------
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
