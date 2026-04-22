/**
 * Cloudflare Pages Function: /press-release + /press-release/<slug>
 * Server-rendered list + detail views from the Flask backend.
 * v3.2 — replaces previous 301-redirect shim.
 */
const BACKEND = 'https://dchub-backend-production.up.railway.app';

export async function onRequest(context) {
  const url = new URL(context.request.url);
  const p = url.pathname;

  if (p === '/press-release' || p === '/press-release/') {
    try {
      const r = await fetch(`${BACKEND}/api/press-releases/list`, { headers: { 'Accept': 'application/json' } });
      const data = await r.json();
      return html(renderList(data.releases || []));
    } catch (e) { return html(renderError(e.message), 500); }
  }

  const m = p.match(/^\/press-release\/([^/]+)\/?$/);
  if (m) {
    const slug = m[1];
    try {
      const r = await fetch(`${BACKEND}/api/press-releases/${slug}`, { headers: { 'Accept': 'application/json' } });
      if (!r.ok) return html(renderNotFound(slug), 404);
      const pr = await r.json();
      return html(renderDetail(pr));
    } catch (e) { return html(renderError(e.message), 500); }
  }

  return context.next();
}

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=300' }
  });
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderList(releases) {
  const cards = releases.map(r => `
    <a class="press-card" href="/press-release/${esc(r.slug)}">
      <div class="press-card-meta">
        <span class="press-card-tag">${esc(r.category || 'Press Release')}</span>
        <span class="press-card-date">${esc(r.date || '')}</span>
      </div>
      <h2 class="press-card-title">${esc(r.title)}</h2>
      <p class="press-card-subhead">${esc(r.subheadline || r.meta_description || '')}</p>
    </a>
  `).join('');
  return shell('Press Releases | DC Hub', `
    <div class="breadcrumb"><div class="breadcrumb-inner">
      <a href="/">DC Hub</a><span class="breadcrumb-sep">/</span><span>Press Releases</span>
    </div></div>
    <main class="press-index">
      <h1 class="press-index-title">Press Releases</h1>
      <p class="press-index-lede">Official announcements from DC Hub.</p>
      <div class="press-grid">
        ${cards || '<p class="press-empty">No press releases yet.</p>'}
      </div>
    </main>
  `, 'Official press releases and announcements from DC Hub — Data Center Intelligence.');
}

function renderDetail(pr) {
  return shell(`${esc(pr.title)} | DC Hub`, `
    <div class="breadcrumb"><div class="breadcrumb-inner">
      <a href="/">DC Hub</a><span class="breadcrumb-sep">/</span>
      <a href="/press-release">Press Releases</a><span class="breadcrumb-sep">/</span>
      <span>${esc(pr.title)}</span>
    </div></div>
    <article class="article-wrapper">
      <div class="article-meta-bar">
        <span class="article-tag">${esc(pr.category || 'Press Release')}</span>
        <span class="article-date">${esc(pr.date || '')}</span>
      </div>
      <h1 class="article-headline">${esc(pr.title)}</h1>
      <p class="article-subheadline">${esc(pr.subheadline || '')}</p>
      <hr class="article-divider">
      <div class="article-body">${pr.body || ''}</div>
      <div class="share-bar">
        <span class="share-label">Share</span>
        <a href="https://www.linkedin.com/sharing/share-offsite/?url=https://dchub.cloud/press-release/${esc(pr.slug)}" class="share-btn" target="_blank" rel="noopener">LinkedIn</a>
        <a href="https://twitter.com/intent/tweet?url=https://dchub.cloud/press-release/${esc(pr.slug)}&text=${encodeURIComponent(pr.title || '')}" class="share-btn" target="_blank" rel="noopener">X</a>
      </div>
    </article>
  `, pr.meta_description || pr.subheadline || '');
}

function renderNotFound(slug) {
  return shell('Press Release Not Found | DC Hub', `
    <main class="press-notfound">
      <h1>Press release not found</h1>
      <p>No press release at <code>/press-release/${esc(slug)}</code>.</p>
      <p><a href="/press-release">See all press releases →</a></p>
    </main>
  `);
}

function renderError(msg) {
  return shell('Error | DC Hub', `
    <main class="press-notfound">
      <h1>Something went wrong</h1>
      <p>${esc(msg)}</p>
      <p><a href="/press-release">Back to press releases →</a></p>
    </main>
  `);
}

function shell(title, content, description = '') {
  return `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title>
${description ? `<meta name="description" content="${esc(description)}">` : ''}
<style>
body{font-family:-apple-system,'Segoe UI',sans-serif;margin:0;color:#0f172a;background:#f8fafc;}
.breadcrumb{background:#fff;border-bottom:1px solid #e2e8f0;padding:12px 0;}
.breadcrumb-inner{max-width:1080px;margin:0 auto;padding:0 24px;font-size:14px;color:#64748b;}
.breadcrumb-inner a{color:#0284c7;text-decoration:none;}
.breadcrumb-sep{margin:0 8px;color:#cbd5e1;}
.press-index,.press-notfound{max-width:1080px;margin:48px auto;padding:0 24px;}
.press-index-title{font-size:42px;margin:0 0 12px;}
.press-index-lede{font-size:18px;color:#64748b;margin:0 0 40px;}
.press-grid{display:grid;gap:20px;}
.press-card{display:block;background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:24px;text-decoration:none;color:inherit;transition:border-color .15s,transform .15s;}
.press-card:hover{border-color:#0284c7;transform:translateY(-2px);}
.press-card-meta{display:flex;gap:12px;align-items:center;margin-bottom:12px;font-size:13px;}
.press-card-tag{background:#e0f2fe;color:#0369a1;padding:3px 10px;border-radius:999px;font-weight:600;}
.press-card-date{color:#64748b;}
.press-card-title{font-size:22px;margin:0 0 8px;color:#0f172a;}
.press-card-subhead{font-size:15px;color:#475569;margin:0;line-height:1.5;}
.press-empty{color:#64748b;text-align:center;padding:48px;}
.article-wrapper{max-width:760px;margin:48px auto;padding:0 24px 80px;}
.article-meta-bar{display:flex;gap:12px;align-items:center;margin-bottom:16px;font-size:13px;}
.article-tag{background:#e0f2fe;color:#0369a1;padding:3px 10px;border-radius:999px;font-weight:600;}
.article-date{color:#64748b;}
.article-headline{font-size:40px;line-height:1.15;margin:0 0 16px;}
.article-subheadline{font-size:20px;color:#475569;margin:0 0 24px;line-height:1.45;}
.article-divider{border:none;border-top:1px solid #e2e8f0;margin:24px 0;}
.article-body{font-size:17px;line-height:1.7;color:#1e293b;}
.article-body h2{font-size:26px;margin-top:36px;}
.article-body h3{font-size:20px;margin-top:28px;}
.article-body p{margin:16px 0;}
.share-bar{display:flex;gap:12px;align-items:center;margin-top:48px;padding-top:24px;border-top:1px solid #e2e8f0;}
.share-label{font-weight:600;color:#64748b;}
.share-btn{padding:6px 14px;border:1px solid #e2e8f0;border-radius:6px;text-decoration:none;color:#475569;font-size:14px;}
.share-btn:hover{border-color:#0284c7;color:#0284c7;}
.press-notfound h1{font-size:32px;}
.press-notfound a{color:#0284c7;}
</style></head>
<body>
<script src="/js/dchub-nav.js"></script>
${content}
</body></html>`;
}
