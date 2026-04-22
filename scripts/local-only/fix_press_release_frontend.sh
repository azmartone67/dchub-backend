#!/usr/bin/env bash
# -------------------------------------------------------------------
# Fix /press-release on dchub.cloud by editing the CORRECT repo:
#   azmartone67/dchub-frontend  (the one CF Pages actually deploys)
# -------------------------------------------------------------------
set -e

# 1) Clone the frontend repo into a sibling directory
cd /home/runner
if [ ! -d dchub-frontend ]; then
  git clone https://github.com/azmartone67/dchub-frontend.git
fi
cd dchub-frontend
git checkout main
git pull origin main
echo "=== Current HEAD ==="
git log --oneline -5

# 2) Find and disable the bad _redirects rule
if grep -n "^/press-release" _redirects 2>/dev/null; then
  echo "=== FOUND the offending redirect, removing it ==="
  sed -i '/^\/press-release[[:space:]]*\/press[[:space:]]*301/d' _redirects
  echo "=== After removal: ==="
  grep -n "press" _redirects || echo "(no more press-related rules)"
else
  echo "!! no /press-release line in _redirects — check for _redirects.txt / routes"
  ls -la _redirects* routes* 2>/dev/null
fi

# 3) Ensure functions dir exists and drop in our two function files
mkdir -p functions

# press-release.js (list + detail, server-rendered from Railway API)
cat > functions/press-release.js <<'EOF'
// /press-release  ->  list view
// /press-release/<slug>  ->  detail view
// Server-rendered at CF Pages edge from Railway API.

const BACKEND = 'https://dchub-backend-production.up.railway.app';

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function shell(title, description, body) {
  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>${esc(title)}</title>
<meta name="description" content="${esc(description)}"/>
<link rel="stylesheet" href="/styles.css"/>
<style>
  .press-index{max-width:1100px;margin:2rem auto;padding:0 1rem}
  .press-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1.25rem;margin-top:1.5rem}
  .press-card{background:#111;border:1px solid #222;border-radius:10px;padding:1.25rem;transition:border-color .15s}
  .press-card:hover{border-color:#4af}
  .press-card a{color:inherit;text-decoration:none;display:block}
  .press-card-meta{display:flex;gap:.5rem;font-size:.8rem;color:#8aa;margin-bottom:.5rem}
  .press-card-tag{background:#223;color:#8cf;padding:.15rem .5rem;border-radius:4px;font-weight:600}
  .press-card-title{font-size:1.15rem;font-weight:700;line-height:1.3;margin:.25rem 0 .5rem}
  .press-card-subhead{color:#aaa;font-size:.92rem;line-height:1.4}
  .article-wrapper{max-width:760px;margin:2rem auto;padding:0 1rem}
  .article-meta-bar{display:flex;gap:.75rem;align-items:center;margin-bottom:1rem;color:#8aa;font-size:.85rem}
  .article-tag{background:#223;color:#8cf;padding:.2rem .6rem;border-radius:4px;font-weight:600}
  .article-headline{font-size:2rem;line-height:1.2;font-weight:800;margin:.5rem 0}
  .article-subheadline{font-size:1.2rem;color:#bbb;margin:.5rem 0 2rem;line-height:1.4}
  .article-body{font-size:1.05rem;line-height:1.7;color:#e0e0e0}
  .article-body p{margin:1.25rem 0}
  .share-bar{margin-top:2.5rem;padding-top:1.5rem;border-top:1px solid #333;display:flex;gap:1rem}
  .share-bar a{color:#8cf;text-decoration:none}
</style>
</head><body>
${body}
</body></html>`;
}

function renderList(releases) {
  if (!releases.length) {
    return shell('Press Releases | DC Hub',
      'Official DC Hub press releases and announcements.',
      `<main class="press-index">
        <h1>Press Releases</h1>
        <p>No press releases available at this time.</p>
      </main>`);
  }
  const cards = releases.map(r => `
    <article class="press-card">
      <a href="/press-release/${esc(r.slug)}">
        <div class="press-card-meta">
          <span class="press-card-tag">${esc(r.category)}</span>
          <span>${esc(r.date || '')}</span>
        </div>
        <h2 class="press-card-title">${esc(r.title)}</h2>
        <p class="press-card-subhead">${esc(r.subheadline || r.meta_description || '')}</p>
      </a>
    </article>`).join('');
  return shell('Press Releases | DC Hub',
    'Official DC Hub press releases and announcements.',
    `<main class="press-index">
      <h1>Press Releases</h1>
      <p>Official announcements from DC Hub.</p>
      <div class="press-grid">${cards}</div>
    </main>`);
}

function renderDetail(r) {
  const body = r.body_html || (r.body ? r.body.split(/\n\n+/).map(p => `<p>${esc(p)}</p>`).join('') : '');
  return shell(`${r.title} | DC Hub`,
    r.meta_description || r.subheadline || r.title,
    `<main class="article-wrapper">
      <div class="article-meta-bar">
        <span class="article-tag">${esc(r.category || 'Press Release')}</span>
        <span>${esc(r.date || '')}</span>
      </div>
      <h1 class="article-headline">${esc(r.title)}</h1>
      ${r.subheadline ? `<p class="article-subheadline">${esc(r.subheadline)}</p>` : ''}
      <div class="article-body">${body}</div>
      <nav class="share-bar">
        <a href="/press-release">&larr; All press releases</a>
      </nav>
    </main>`);
}

function renderNotFound(slug) {
  return shell('Not Found | DC Hub', 'Press release not found.',
    `<main class="article-wrapper">
      <h1>Press release not found</h1>
      <p>We couldn't find a press release matching <code>${esc(slug)}</code>.</p>
      <p><a href="/press-release">&larr; Back to all press releases</a></p>
    </main>`);
}

function renderError(msg) {
  return shell('Error | DC Hub', 'Error loading press release.',
    `<main class="article-wrapper">
      <h1>Temporarily unavailable</h1>
      <p>We couldn't load press releases right now. Please try again shortly.</p>
      <!-- ${esc(msg)} -->
    </main>`);
}

export async function onRequest(ctx) {
  const url = new URL(ctx.request.url);
  const parts = url.pathname.replace(/^\/+|\/+$/g, '').split('/');
  // parts[0] === 'press-release'
  const slug = parts[1];

  try {
    if (!slug) {
      const r = await fetch(`${BACKEND}/api/press-releases/list`, { cf: { cacheTtl: 300 } });
      if (!r.ok) return new Response(renderError(`list ${r.status}`), { status: 502, headers: { 'content-type': 'text/html; charset=utf-8' } });
      const j = await r.json();
      return new Response(renderList(j.releases || []), {
        headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'public, max-age=300' }
      });
    } else {
      const r = await fetch(`${BACKEND}/api/press-releases/${encodeURIComponent(slug)}`, { cf: { cacheTtl: 300 } });
      if (r.status === 404) return new Response(renderNotFound(slug), { status: 404, headers: { 'content-type': 'text/html; charset=utf-8' } });
      if (!r.ok) return new Response(renderError(`detail ${r.status}`), { status: 502, headers: { 'content-type': 'text/html; charset=utf-8' } });
      const j = await r.json();
      const rel = j.release || j;
      return new Response(renderDetail(rel), {
        headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'public, max-age=300' }
      });
    }
  } catch (e) {
    return new Response(renderError(String(e && e.message || e)), { status: 502, headers: { 'content-type': 'text/html; charset=utf-8' } });
  }
}
EOF

# press.js (legacy /press -> /press-release 301)
cat > functions/press.js <<'EOF'
// Legacy /press and /press/<slug> redirect to canonical /press-release path
export async function onRequest(ctx) {
  const url = new URL(ctx.request.url);
  const parts = url.pathname.replace(/^\/+|\/+$/g, '').split('/');
  // parts[0] === 'press'
  const slug = parts[1];
  const target = slug ? `/press-release/${slug}` : '/press-release';
  return Response.redirect(new URL(target, url.origin).toString(), 301);
}
EOF

echo "=== Files written ==="
ls -la functions/press*.js
echo "=== _redirects contents ==="
cat _redirects 2>/dev/null | head -20 || echo "(no _redirects file)"

# 4) Commit and push
git add -A
git status
git commit -m "fix: server-render /press-release from Railway API + drop 301 shim

- functions/press-release.js: list view + detail view via Pages Functions
- functions/press.js: legacy /press -> /press-release 301
- _redirects: remove '/press-release -> /press' rule from f8f0eb3

Unblocks the blank press-release page by actually reading from
/api/press-releases/list and /api/press-releases/<slug> at the edge."

git push origin main

echo ""
echo "=== DONE ==="
echo "CF Pages should auto-deploy in ~60s. Check: https://dchub.cloud/press-release"
