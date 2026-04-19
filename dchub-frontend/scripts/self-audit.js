#!/usr/bin/env node
/**
 * DC Hub — Site Self-Audit & Evolution
 * ------------------------------------------------------------------
 * Runs on schedule. For every market in the registry:
 *   1. Hits every live /api/v1/* endpoint the market page consumes.
 *   2. Records latency, HTTP status, payload shape, and any errors.
 *   3. Diffs "interesting" fields (pipeline_mw, avg_price_kwh, vacancy,
 *      gdci score) against last run's snapshot stored in KV.
 *   4. Identifies top 5 "movers" and stalest data.
 *   5. Writes an audit report + a LinkedIn-ready top-10 payload.
 *   6. Optionally emails the report (expects SMTP or a /api/notify/email
 *      endpoint to be wired).
 *
 * Designed to run in two places:
 *   - Cloudflare cron (weekly, production) — set WORKER_CRON=1
 *   - Local Node invocation — just `node scripts/self-audit.js`
 *
 * Env vars:
 *   DCHUB_API_BASE   default https://dchub.cloud/api/v1
 *   DCHUB_API_KEY    Developer/Enterprise key (unlocks full data)
 *   SNAPSHOT_PATH    default ./scripts/.audit-snapshot.json
 *   REPORT_OUT       default ./scripts/.audit-report.json
 *   LINKEDIN_OUT     default ./scripts/.linkedin-top10.json
 *   MAIL_TO          optional, email recipient
 */

const fs = require('fs');
const path = require('path');

const CFG = {
  API_BASE:     process.env.DCHUB_API_BASE    || 'https://dchub.cloud/api/v1',
  API_KEY:      process.env.DCHUB_API_KEY     || '',
  REGISTRY_URL: process.env.DCHUB_REGISTRY    || 'https://dchub.cloud/markets/registry.json',
  SNAPSHOT:     process.env.SNAPSHOT_PATH     || path.join(__dirname, '.audit-snapshot.json'),
  REPORT_OUT:   process.env.REPORT_OUT        || path.join(__dirname, '.audit-report.json'),
  LINKEDIN_OUT: process.env.LINKEDIN_OUT      || path.join(__dirname, '.linkedin-top10.json'),
  MAIL_TO:      process.env.MAIL_TO           || 'hello@dchub.cloud',
  MAX_PARALLEL: 8,
  TIMEOUT_MS:   10000,
};

const hdr = { 'Accept': 'application/json' };
if (CFG.API_KEY) hdr['Authorization'] = `Bearer ${CFG.API_KEY}`;

// ---------------------------------------------------------------

async function fetchJSON(url) {
  const t0 = Date.now();
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), CFG.TIMEOUT_MS);
  try {
    const r = await fetch(url, { headers: hdr, signal: ctl.signal });
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch {}
    return { ok: r.ok, status: r.status, latency: Date.now() - t0, data, bodyLen: text.length, error: null };
  } catch (e) {
    return { ok: false, status: 0, latency: Date.now() - t0, data: null, bodyLen: 0, error: e.message };
  } finally {
    clearTimeout(to);
  }
}

async function pool(items, worker, n = CFG.MAX_PARALLEL) {
  const out = [];
  let i = 0;
  async function next() {
    while (i < items.length) {
      const idx = i++;
      out[idx] = await worker(items[idx], idx);
    }
  }
  await Promise.all(Array.from({ length: n }, next));
  return out;
}

function loadSnapshot() {
  try { return JSON.parse(fs.readFileSync(CFG.SNAPSHOT, 'utf8')); }
  catch { return { ts: null, markets: {} }; }
}

function saveSnapshot(snap) { fs.writeFileSync(CFG.SNAPSHOT, JSON.stringify(snap, null, 2)); }

// ---------------------------------------------------------------

async function auditMarket(slug, m) {
  const q = (o) => Object.entries(o).filter(([,v]) => v != null && v !== '').map(([k,v]) => `${k}=${encodeURIComponent(v)}`).join('&');
  const endpoints = [
    { key: 'marketIntel', url: `${CFG.API_BASE}/markets/${slug}` },
    { key: 'gdci',        url: `${CFG.API_BASE}/gdci?${q({ market: slug })}` },
    { key: 'pipeline',    url: `${CFG.API_BASE}/pipeline?${q({ market: slug })}` },
    { key: 'news',        url: `${CFG.API_BASE}/news?${q({ market: slug, limit: 1 })}` },
    { key: 'facilities',  url: `${CFG.API_BASE}/facilities?${q({ city: m.name.split(/[,\/]/)[0].trim(), limit: 1 })}` },
  ];
  if (m.iso)   endpoints.push({ key: 'grid',   url: `${CFG.API_BASE}/grid?${q({ iso: m.iso })}` });
  if (m.state && m.country === 'US') {
    endpoints.push({ key: 'energy', url: `${CFG.API_BASE}/energy/retail-rates?${q({ state: m.state })}` });
    endpoints.push({ key: 'tax',    url: `${CFG.API_BASE}/tax-incentives?${q({ state: m.state })}` });
  }
  const results = await pool(endpoints, async ep => ({ key: ep.key, url: ep.url, ...await fetchJSON(ep.url) }), 4);
  const byKey = {};
  for (const r of results) byKey[r.key] = r;

  // Extract "interesting" fields for diffing
  const snapFields = {};
  const mi = byKey.marketIntel?.data?.data || byKey.marketIntel?.data || {};
  if (mi.pipeline_mw)    snapFields.pipeline_mw = Number(mi.pipeline_mw);
  if (mi.avg_price_kwh)  snapFields.avg_price_kwh = Number(mi.avg_price_kwh);
  if (mi.vacancy_rate)   snapFields.vacancy_rate = Number(mi.vacancy_rate);
  if (mi.facilities_count) snapFields.facilities_count = Number(mi.facilities_count);
  const gd = byKey.gdci?.data?.data || byKey.gdci?.data || {};
  if (gd.score != null)  snapFields.gdci_score = Number(gd.score);
  if (gd.rank  != null)  snapFields.gdci_rank  = Number(gd.rank);

  return { slug, endpoints: byKey, snapFields };
}

function computeDiff(prevFields, currFields) {
  const delta = {};
  for (const k of Object.keys(currFields)) {
    const a = prevFields?.[k];
    const b = currFields[k];
    if (a == null || b == null) continue;
    const d = b - a;
    const pct = a !== 0 ? (d / a) * 100 : 0;
    if (Math.abs(pct) > 0.5) delta[k] = { prev: a, curr: b, delta: d, pct_change: Number(pct.toFixed(2)) };
  }
  return delta;
}

// ---------------------------------------------------------------

async function main() {
  const t0 = Date.now();
  console.log(`[audit] starting @ ${new Date().toISOString()}`);
  console.log(`[audit] registry: ${CFG.REGISTRY_URL}`);
  console.log(`[audit] api_base: ${CFG.API_BASE}`);
  console.log(`[audit] auth: ${CFG.API_KEY ? 'Bearer (' + CFG.API_KEY.slice(0,6) + '…)' : 'anonymous'}`);

  const regResp = await fetchJSON(CFG.REGISTRY_URL);
  if (!regResp.ok) { console.error('[audit] registry fetch failed:', regResp); process.exit(1); }
  const registry = regResp.data.markets;
  const slugs = Object.keys(registry);

  const previous = loadSnapshot();
  const curr = { ts: new Date().toISOString(), markets: {} };

  const marketResults = await pool(slugs, async slug => auditMarket(slug, registry[slug]));

  // Aggregate health
  const endpointHealth = {};  // key -> { total, ok, errors[] }
  const movers = []; // { slug, delta_summary }
  const stale = []; // markets with only registry data (API returned nothing useful)

  for (const r of marketResults) {
    curr.markets[r.slug] = { snapFields: r.snapFields };
    for (const [k, res] of Object.entries(r.endpoints)) {
      const h = endpointHealth[k] = endpointHealth[k] || { total: 0, ok: 0, errors: [] };
      h.total++;
      if (res.ok) h.ok++;
      else h.errors.push({ slug: r.slug, status: res.status, error: res.error, url: res.url });
    }
    const delta = computeDiff(previous.markets?.[r.slug]?.snapFields, r.snapFields);
    if (Object.keys(delta).length) movers.push({ slug: r.slug, name: registry[r.slug].name, delta });
    if (Object.keys(r.snapFields).length === 0) stale.push(r.slug);
  }

  // Rank movers by magnitude of gdci_score change, then pipeline_mw pct
  movers.sort((a, b) => {
    const ga = Math.abs(a.delta.gdci_score?.pct_change || 0);
    const gb = Math.abs(b.delta.gdci_score?.pct_change || 0);
    if (gb !== ga) return gb - ga;
    return Math.abs(b.delta.pipeline_mw?.pct_change || 0) - Math.abs(a.delta.pipeline_mw?.pct_change || 0);
  });

  const report = {
    generated_at: curr.ts,
    duration_ms: Date.now() - t0,
    markets_checked: slugs.length,
    endpoint_health: endpointHealth,
    stale_markets: stale,
    top_movers: movers.slice(0, 10),
    bugs_flagged: detectBugs(endpointHealth),
    linkedin_candidates: movers.slice(0, 3),
  };

  fs.writeFileSync(CFG.REPORT_OUT, JSON.stringify(report, null, 2));
  saveSnapshot(curr);
  console.log(`[audit] report → ${CFG.REPORT_OUT}`);

  // LinkedIn Top 10: combine movers + any static "always interesting" markets
  const linkedin = buildLinkedInPayload(report, registry, marketResults);
  fs.writeFileSync(CFG.LINKEDIN_OUT, JSON.stringify(linkedin, null, 2));
  console.log(`[audit] linkedin top10 → ${CFG.LINKEDIN_OUT}`);

  // Summary
  console.log('[audit] summary:');
  console.log(`  markets: ${slugs.length}`);
  for (const [k, h] of Object.entries(endpointHealth)) {
    const pct = Math.round(h.ok/h.total*100);
    const flag = pct < 80 ? '⚠️ ' : pct < 100 ? '· ' : '✓ ';
    console.log(`  ${flag}${k}: ${h.ok}/${h.total} ok (${pct}%)`);
  }
  console.log(`  movers: ${movers.length}`);
  console.log(`  stale:  ${stale.length}`);
  console.log(`[audit] done in ${Math.round((Date.now() - t0)/1000)}s`);
  return report;
}

function detectBugs(health) {
  const bugs = [];
  for (const [k, h] of Object.entries(health)) {
    const pct = h.ok / h.total;
    if (pct < 0.5) bugs.push({ severity: 'critical', type: 'endpoint_majority_failing', endpoint: k, ok_pct: Math.round(pct*100), sample_errors: h.errors.slice(0,3) });
    else if (pct < 0.9) bugs.push({ severity: 'warning', type: 'endpoint_partial_failing', endpoint: k, ok_pct: Math.round(pct*100), sample_errors: h.errors.slice(0,2) });

    // Tier drift: 403 responses claiming "free_tier" in error body while rejecting
    const tierDrift = h.errors.filter(e => e.status === 403).slice(0, 1);
    if (tierDrift.length) bugs.push({ severity: 'info', type: 'possible_tier_config_drift', endpoint: k, note: 'Verify free_tier_tools list vs enforcement. 403s observed.', sample: tierDrift[0] });
  }
  return bugs;
}

function buildLinkedInPayload(report, registry, results) {
  const top = (report.top_movers || []).slice(0, 10).map((m, i) => {
    const full = registry[m.slug];
    const data = results.find(r => r.slug === m.slug)?.snapFields || {};
    const primary = m.delta.gdci_score || m.delta.pipeline_mw || m.delta.avg_price_kwh || Object.values(m.delta)[0];
    const direction = (primary?.pct_change || 0) > 0 ? '📈' : '📉';
    return {
      rank: i + 1,
      slug: m.slug,
      name: full.name,
      flag: full.flag,
      tier: full.tier,
      headline: `${direction} ${full.name} ${primary?.pct_change?.toFixed(1)}% move in ${Object.keys(m.delta)[0]}`,
      metrics: data,
      deltas: m.delta,
      url: `https://dchub.cloud/markets/${m.slug}`,
    };
  });

  const post = {
    title: `🔥 DC Hub Weekly — Top 10 Data Center Market Movers`,
    subtitle: `Week of ${new Date().toISOString().slice(0,10)} · Powered by DC Hub GDCI`,
    intro: `The global data center landscape shifted this week. Here are the 10 biggest movers by GDCI score, pipeline delta, and power-price swings — all pulled live from DC Hub's MCP intelligence backend.`,
    items: top,
    cta: `See full live intelligence: https://dchub.cloud/gdci — or grab the API: https://dchub.cloud/api-docs`,
    hashtags: ['#datacenter','#AIinfrastructure','#GDCI','#colocation','#hyperscale','#dchub'],
  };

  return { generated_at: report.generated_at, post, raw: top };
}

// ---------------------------------------------------------------
// Run when invoked directly
if (require.main === module) {
  main().catch(e => { console.error('[audit] FATAL', e); process.exit(2); });
}

module.exports = { main, auditMarket, detectBugs, buildLinkedInPayload };
