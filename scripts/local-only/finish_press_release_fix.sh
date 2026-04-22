#!/usr/bin/env bash
# Finish the press-release fix in the dchub-frontend repo.
# Safe to re-run.
set -u  # no -e: we want to handle greps returning nonzero
cd ~/dchub-frontend || { echo "repo not found"; exit 1; }

git checkout main
git pull --rebase origin main

mkdir -p functions

# --- functions/press-release.js : list + detail, server-rendered from Railway ---
cat > functions/press-release.js <<'EOF'
const BACKEND = 'https://dchub-backend-production.up.railway.app';
const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
function shell(title, desc, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>${esc(title)}</title><meta name="description" content="${esc(desc)}"/><link rel="stylesheet" href="/styles.css"/>
<style>.press-index{max-width:1100px;margin:2rem auto;padding:0 1rem}.press-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1.25rem;margin-top:1.5rem}.press-card{background:#111;border:1px solid #222;border-radius:10px;padding:1.25rem}.press-card:hover{border-color:#4af}.press-card a{color:inherit;text-decoration:none;display:block}.press-card-meta{display:flex;gap:.5rem;font-size:.8rem;color:#8aa;margin-bottom:.5rem}.press-card-tag{background:#223;color:#8cf;padding:.15rem .5rem;border-radius:4px;font-weight:600}.press-card-title{font-size:1.15rem;font-weight:700;line-height:1.3;margin:.25rem 0 .5rem}.press-card-subhead{color:#aaa;font-size:.92rem;line-height:1.4}.article-wrapper{max-width:760px;margin:2rem auto;padding:0 1rem}.article-meta-bar{display:flex;gap:.75rem;align-items:center;margin-bottom:1rem;color:#8aa;font-size:.85rem}.article-tag{background:#223;color:#8cf;padding:.2rem .6rem;border-radius:4px;font-weight:600}.article-headline{font-size:2rem;line-height:1.2;font-weight:800;margin:.5rem 0}.article-subheadline{font-size:1.2rem;color:#bbb;margin:.5rem 0 2rem;line-height:1.4}.article-body{font-size:1.05rem;line-height:1.7;color:#e0e0e0}.article-body p{margin:1.25rem 0}.share-bar{margin-top:2.5rem;padding-top:1.5rem;border-top:1px solid #333}.share-bar a{color:#8cf;text-decoration:none}</style>
</head><body>${body}</body></html>`;
}
function renderList(releases){
  if(!releases.length) return shell('Press Releases | DC Hub','Official DC Hub press releases.',`<main class="press-index"><h1>Press Releases</h1><p>No press releases available.</p></main>`);
  const cards = releases.map(r=>`<article class="press-card"><a href="/press-release/${esc(r.slug)}"><div class="press-card-meta"><span class="press-card-tag">${esc(r.category)}</span><span>${esc(r.date||'')}</span></div><h2 class="press-card-title">${esc(r.title)}</h2><p class="press-card-subhead">${esc(r.subheadline||r.meta_description||'')}</p></a></article>`).join('');
  return shell('Press Releases | DC Hub','Official DC Hub press releases.',`<main class="press-index"><h1>Press Releases</h1><p>Official announcements from DC Hub.</p><div class="press-grid">${cards}</div></main>`);
}
function renderDetail(r){
  const body = r.body_html || (r.body ? r.body.split(/\n\n+/).map(p=>`<p>${esc(p)}</p>`).join('') : '');
  return shell(`${r.title} | DC Hub`, r.meta_description||r.subheadline||r.title, `<main class="article-wrapper"><div class="article-meta-bar"><span class="article-tag">${esc(r.category||'Press Release')}</span><span>${esc(r.date||'')}</span></div><h1 class="article-headline">${esc(r.title)}</h1>${r.subheadline?`<p class="article-subheadline">${esc(r.subheadline)}</p>`:''}<div class="article-body">${body}</div><nav class="share-bar"><a href="/press-release">&larr; All press releases</a></nav></main>`);
}
export async function onRequest(ctx){
  const url = new URL(ctx.request.url);
  const parts = url.pathname.replace(/^\/+|\/+$/g,'').split('/');
  const slug = parts[1];
  const H = {'content-type':'text/html; charset=utf-8','cache-control':'public, max-age=300'};
  try {
    if (!slug) {
      const r = await fetch(`${BACKEND}/api/press-releases/list`, { cf:{ cacheTtl:300 } });
      if (!r.ok) return new Response(shell('Error','','<main class="article-wrapper"><h1>Temporarily unavailable</h1></main>'), {status:502, headers:H});
      const j = await r.json();
      return new Response(renderList(j.releases||[]), {headers:H});
    }
    const r = await fetch(`${BACKEND}/api/press-releases/${encodeURIComponent(slug)}`, { cf:{ cacheTtl:300 } });
    if (r.status===404) return new Response(shell('Not found','',`<main class="article-wrapper"><h1>Not found</h1><p><a href="/press-release">Back</a></p></main>`), {status:404, headers:H});
    if (!r.ok) return new Response(shell('Error','','<main class="article-wrapper"><h1>Temporarily unavailable</h1></main>'), {status:502, headers:H});
    const j = await r.json();
    return new Response(renderDetail(j.release||j), {headers:H});
  } catch(e) {
    return new Response(shell('Error','','<main class="article-wrapper"><h1>Temporarily unavailable</h1></main>'), {status:502, headers:H});
  }
}
EOF

# --- functions/press.js : legacy /press -> /press-release 301 ---
cat > functions/press.js <<'EOF'
export async function onRequest(ctx){
  const url = new URL(ctx.request.url);
  const parts = url.pathname.replace(/^\/+|\/+$/g,'').split('/');
  const slug = parts[1];
  return Response.redirect(new URL(slug?`/press-release/${slug}`:'/press-release', url.origin).toString(), 301);
}
EOF

# --- _routes.json : carve /press-release* and /press* out of _worker.js ---
# Functions handle these paths; _worker.js keeps everything else.
cat > _routes.json <<'EOF'
{
  "version": 1,
  "include": ["/press-release", "/press-release/*", "/press", "/press/*"],
  "exclude": []
}
EOF

# --- neutralize any /press-release redirect rule in _redirects (keep everything else) ---
if [ -f _redirects ]; then
  cp _redirects _redirects.bak
  # comment out lines that redirect /press-release (with or without trailing match) to /press
  sed -i -E 's|^(/press-release[[:space:]]+/press([[:space:]]+301)?)|# [disabled] \1|I' _redirects
  # also neutralize any standalone "/press-release -> /press" style
  sed -i -E 's|^(/press-release[[:space:]]+/press.*)|# [disabled] \1|' _redirects
  if ! diff -q _redirects _redirects.bak >/dev/null 2>&1; then
    echo "=== _redirects changed, diff: ==="
    diff _redirects.bak _redirects || true
  else
    echo "=== _redirects unchanged (no matching rule) ==="
  fi
  rm _redirects.bak
fi

echo "=== files staged ==="
ls -la functions/press*.js _routes.json
echo ""
git add -A
git status --short
git commit -m "fix(press-release): server-render from Railway + route around _worker.js

- functions/press-release.js : list + detail via /api/press-releases/list
  and /api/press-releases/<slug>
- functions/press.js : legacy /press -> /press-release 301
- _routes.json : include /press-release* and /press* so Pages Functions
  handle these paths instead of _worker.js
- _redirects : disable any '/press-release -> /press' rule if present

Fixes blank press-release page. Data source is Neon via Railway
dchub-backend-production.up.railway.app."

git push origin main

echo ""
echo "=== DONE. CF Pages will auto-deploy in ~60s ==="
echo "Check: https://dchub.cloud/press-release"
