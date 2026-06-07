#!/usr/bin/env node
/**
 * Visual sentinel (2026-06-06) — the thing the brain couldn't do: SEE the
 * rendered site. Loads each key page in headless Chromium and flags it broken
 * when ANY of: non-200, an uncaught page error, near-empty body (blank page),
 * or broken/blank <img> elements (naturalWidth===0) — the exact class behind
 * the blank LinkedIn cards + the 0-byte /dc-hub-media we hit by hand.
 *
 * Posts the report to /api/v1/admin/visual-sentinel (needs DCHUB_ADMIN_KEY);
 * exits non-zero if any page is broken so the GH Actions run goes red too.
 */
import { chromium } from 'playwright';

const BASE = process.env.SENTINEL_BASE || 'https://dchub.cloud';
const ADMIN_KEY = process.env.DCHUB_ADMIN_KEY || '';
const REPORT_URL = (process.env.DCHUB_API_BASE || 'https://api.dchub.cloud') + '/api/v1/admin/visual-sentinel';

const PAGES = [
  { page: 'home',         path: '/' },
  { page: 'land-power',   path: '/land-power-map', mustInclude: ['Data Center'] },
  { page: 'dc-hub-media', path: '/dc-hub-media/',  mustInclude: ['DC Hub Media'] },
  { page: 'dcpi',         path: '/dcpi' },
  { page: 'pricing',      path: '/pricing' },
];

async function checkPage(browser, spec) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const pg = await ctx.newPage();
  const consoleErrors = [], pageErrors = [];
  pg.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 160)); });
  pg.on('pageerror', e => pageErrors.push(String(e).slice(0, 160)));
  const out = { page: spec.page, path: spec.path, ok: false };
  try {
    // NOT 'networkidle' — live pages (the map, the media feed) poll continuously
    // and never go idle, which false-times-out every page. domcontentloaded +
    // a fixed settle is the reliable strategy for sites with background polling.
    const resp = await pg.goto(BASE + spec.path, { waitUntil: 'domcontentloaded', timeout: 60000 });
    out.status = resp ? resp.status() : 0;
    await pg.waitForTimeout(6000);  // let async render + lazy images attempt to load
    const textLen = (await pg.evaluate(() => document.body ? document.body.innerText.trim().length : 0));
    const brokenImgs = await pg.evaluate(() =>
      Array.from(document.images)
        .filter(i => i.complete && i.naturalWidth === 0)
        .map(i => (i.currentSrc || i.src || '').slice(0, 90)).slice(0, 8));
    let missing = [];
    if (spec.mustInclude) {
      const html = (await pg.content());
      missing = spec.mustInclude.filter(s => !html.includes(s));
    }
    out.http_status = out.status;
    out.text_len = textLen;
    out.broken_images = brokenImgs;
    out.console_errors = consoleErrors.length;
    out.page_errors = pageErrors.slice(0, 3);
    out.missing_markers = missing;
    // verdict
    const reasons = [];
    if (out.status !== 200) reasons.push(`http ${out.status}`);
    if (textLen < 200) reasons.push(`near-empty body (${textLen} chars)`);
    if (brokenImgs.length) reasons.push(`${brokenImgs.length} broken/blank image(s)`);
    if (pageErrors.length) reasons.push(`${pageErrors.length} uncaught page error(s)`);
    if (missing.length) reasons.push(`missing: ${missing.join(', ')}`);
    out.ok = reasons.length === 0;
    if (!out.ok) out.issue = reasons.join('; ');
  } catch (e) {
    out.ok = false; out.issue = `load failed: ${String(e).slice(0, 140)}`;
  } finally {
    await ctx.close();
  }
  return out;
}

(async () => {
  const browser = await chromium.launch();
  const pages = [];
  for (const spec of PAGES) {
    const r = await checkPage(browser, spec);
    pages.push(r);
    console.log(`${r.ok ? '✅' : '❌'} ${r.page} (${r.path}) ${r.ok ? '' : '— ' + r.issue}`);
  }
  await browser.close();

  const report = { ran_at: new Date().toISOString(), source: 'github-actions', pages };
  const broken = pages.filter(p => !p.ok);

  // Post to backend (best-effort)
  if (ADMIN_KEY) {
    try {
      const res = await fetch(REPORT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Admin-Key': ADMIN_KEY },
        body: JSON.stringify(report),
      });
      console.log(`report POST → ${res.status}`);
    } catch (e) { console.log('report POST failed:', String(e).slice(0, 120)); }
  } else {
    console.log('DCHUB_ADMIN_KEY not set — skipping report POST (red job still signals).');
  }

  if (broken.length) {
    console.error(`\n🛑 ${broken.length}/${pages.length} page(s) look broken — see above.`);
    process.exit(1);
  }
  console.log(`\n✅ All ${pages.length} pages render clean.`);
})();
