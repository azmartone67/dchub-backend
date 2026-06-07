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
    await pg.waitForTimeout(5000);
    // Scroll to trigger lazy-loads + give cross-origin assets time, then reset.
    await pg.evaluate(() => window.scrollTo(0, document.body.scrollHeight)).catch(() => {});
    await pg.waitForTimeout(2500);
    await pg.evaluate(() => window.scrollTo(0, 0)).catch(() => {});
    const textLen = (await pg.evaluate(() => document.body ? document.body.innerText.trim().length : 0));
    // Split broken images by origin: a broken FIRST-PARTY image (our og cards,
    // hero, gallery — the blank-card class) is a hard fail; a broken THIRD-PARTY
    // CDN icon (e.g. simpleicons) is a soft warning, not a red alarm.
    const imgInfo = await pg.evaluate(() => {
      const origin = location.origin;
      return Array.from(document.images)
        .filter(i => i.complete && i.naturalWidth === 0 && i.clientHeight > 2)
        .map(i => {
          const s = i.currentSrc || i.getAttribute('src') || '';
          let abs = s;
          try { abs = s.startsWith('http') ? s : new URL(s, location.href).href; } catch (e) {}
          return { src: abs.slice(0, 90), same: abs.startsWith(origin) };
        }).slice(0, 16);
    });
    const sameBroken = imgInfo.filter(i => i.same).map(i => i.src);
    const crossBroken = imgInfo.filter(i => !i.same).map(i => i.src);
    let missing = [];
    if (spec.mustInclude) {
      const html = (await pg.content());
      missing = spec.mustInclude.filter(s => !html.includes(s));
    }
    out.http_status = out.status;
    out.text_len = textLen;
    out.broken_images = sameBroken;                  // hard-fail set (our assets)
    out.broken_images_thirdparty = crossBroken;      // warn set (external CDNs)
    out.console_errors = consoleErrors.length;
    out.page_errors = pageErrors.slice(0, 3);
    out.missing_markers = missing;
    // verdict — fail only on things WE own/control
    const reasons = [];
    if (out.status !== 200) reasons.push(`http ${out.status}`);
    if (textLen < 200) reasons.push(`near-empty body (${textLen} chars)`);
    if (sameBroken.length) reasons.push(`${sameBroken.length} broken first-party image(s)`);
    if (pageErrors.length) reasons.push(`${pageErrors.length} uncaught page error(s)`);
    if (missing.length) reasons.push(`missing: ${missing.join(', ')}`);
    out.ok = reasons.length === 0;
    if (crossBroken.length) out.warning = `${crossBroken.length} third-party image(s) not loading: ${crossBroken.slice(0, 3).join(', ')}`;
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
    console.log(`${r.ok ? '✅' : '❌'} ${r.page} (${r.path})${r.ok ? '' : ' — ' + r.issue}${r.warning ? '  ⚠ ' + r.warning : ''}`);
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
