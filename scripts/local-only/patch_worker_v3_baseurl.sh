#!/usr/bin/env bash
set -u
cd ~/dchub-frontend || exit 1
rm -f _worker.js.bak
git checkout main && git pull --rebase origin main
cp _worker.js _worker.js.bak

python3 <<'PY'
import pathlib, re, sys
p = pathlib.Path('_worker.js')
src = p.read_text()

# Replace the existing alias block (from v4.5.13) with a smarter one that:
#  - for bare /press-release[/]: render a list from /api/press-releases/list
#  - for /press-release/<slug>: alias to /news/<slug> so existing branch handles it
old = re.compile(
    r"  // v4\.5\.13: alias /press-release\* -> /news\* \(same renderer handles both\)\n"
    r"  if \(pathname\.startsWith\('/press-release'\)\) \{\n"
    r"    pathname = pathname\.replace\(/\^\\/press-release/, '/news'\);\n"
    r"  \}\n"
)

new = r'''  // v4.5.14: /press-release handling
  if (pathname === '/press-release' || pathname === '/press-release/') {
    try {
      const apiResp = await fetch(`${RAILWAY_BACKEND}/api/press-releases/list`,
        { headers: { 'X-Forwarded-Host': 'dchub.cloud', 'Accept': 'application/json' } });
      if (apiResp.ok) {
        const data = await apiResp.json();
        const releases = data.releases || [];
        const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        const cards = releases.length ? releases.map(r => `
          <a href="/press-release/${esc(r.slug)}" style="display:block;background:#0d1224;border:1px solid #1a2035;border-radius:10px;padding:22px 24px;text-decoration:none" onmouseover="this.style.borderColor='#63b3ed'" onmouseout="this.style.borderColor='#1a2035'">
            <div style="display:inline-block;background:#1a2035;color:#7c3aed;font-size:10px;font-weight:700;padding:3px 8px;border-radius:4px;letter-spacing:0.5px;text-transform:uppercase;margin-bottom:10px">${esc(r.category||'Press Release')}</div>
            <div style="color:#f0f4ff;font-weight:600;font-size:1.05rem;line-height:1.35;margin-bottom:8px">${esc(r.title)}</div>
            ${r.subheadline ? `<div style="color:#a0aec0;font-size:13px;line-height:1.5;margin-bottom:10px">${esc(r.subheadline)}</div>` : ''}
            <div style="color:#4a5568;font-size:12px;font-family:monospace">${esc(r.date||'')} → Read more</div>
          </a>`).join('') : '<p style="color:#718096;text-align:center;padding:40px 0;grid-column:1/-1">No press releases yet.</p>';
        const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Press Releases | DC Hub</title><meta name="description" content="Official DC Hub press releases and announcements."><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="/favicon.ico"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet"><style>*{box-sizing:border-box;margin:0;padding:0}body{background:#0a0e1a;color:#c9d1e0;font-family:'Inter',-apple-system,sans-serif}nav{background:#0d1224;border-bottom:1px solid #1a2035;padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}.nav-logo{font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#00d4ff;text-decoration:none}.nav-logo span{color:#7c3aed}.nav-links{display:flex;gap:24px;align-items:center}.nav-links a{color:#718096;font-size:13px;text-decoration:none}.nav-links a:hover{color:#e2e8f0}.nav-links .btn{background:#7c3aed;color:#fff;padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600}.container{max-width:1100px;margin:0 auto;padding:48px 24px}.breadcrumb{color:#4a5568;font-size:13px;margin-bottom:32px}.breadcrumb a{color:#63b3ed;text-decoration:none}.page-label{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;color:#7c3aed;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px}.page-title{font-size:2rem;font-weight:700;color:#f0f4ff;margin-bottom:8px}.page-sub{color:#718096;font-size:15px;margin-bottom:32px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}footer{background:#0d1224;border-top:1px solid #1a2035;padding:24px;text-align:center;color:#4a5568;font-size:12px;margin-top:64px}footer a{color:#4a5568;text-decoration:none}</style></head><body><nav><a href="/" class="nav-logo">DC<span>Hub</span></a><div class="nav-links"><a href="/map">Maps</a><a href="/deals">Deals</a><a href="/news">News</a><a href="/pricing">Pricing</a><a href="/login" class="btn">Sign In</a></div></nav><div class="container"><div class="breadcrumb"><a href="/">DC Hub</a> / Press Releases</div><div class="page-label">📰 Official Announcements</div><div class="page-title">Press Releases</div><div class="page-sub">Official press releases and announcements from DC Hub — ${releases.length} published</div><div class="grid">${cards}</div></div><footer><p>© 2026 DC Hub. <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p></footer></body></html>`;
        return new Response(html, { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=300', 'X-DC-Worker-Version': WORKER_VERSION } });
      }
    } catch(e) { console.log('[press-release list] error:', e.message); }
    return new Response('<html><body style="background:#0a0e1a;color:#e2e8f0;padding:40px;font-family:sans-serif;text-align:center"><h1>Press releases temporarily unavailable</h1><p><a href="/" style="color:#63b3ed">← Home</a></p></body></html>', { status: 502, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  }
  // /press-release/<slug>: alias to /news/<slug> (existing branch handles it)
  if (pathname.startsWith('/press-release/')) {
    pathname = pathname.replace(/^\/press-release/, '/news');
  }
'''

n = len(old.findall(src))
if n != 1:
    print(f'FAIL: expected 1 match for alias block, got {n}', file=sys.stderr); sys.exit(2)
src = old.sub(new, src, count=1)

# bump version
src = re.sub(r"'4\.5\.13'", "'4.5.14'", src, count=1)

p.write_text(src)
print('OK: patched _worker.js')
PY

if [ $? -ne 0 ]; then
  echo "Python failed - restoring backup"
  cp _worker.js.bak _worker.js
  exit 1
fi

echo "=== sanity ==="
grep -n "v4\.5\.14" _worker.js | head -3
grep -n "api/press-releases/list" _worker.js | head -3
if command -v node >/dev/null 2>&1; then
  node --check _worker.js && echo "node --check OK" || { cp _worker.js.bak _worker.js; echo "SYNTAX ERROR - restored"; exit 3; }
fi

rm -f _worker.js.bak
git add _worker.js
git commit -m "_worker.js v4.5.14: render /press-release list page

Bare /press-release now fetches /api/press-releases/list from Railway
and renders a grid of cards linking to /press-release/<slug>.
/press-release/<slug> continues to alias to /news/<slug> so the
existing buildPressReleaseHtml renderer handles the detail view."

git push origin main

echo ""
echo "=== DONE. Wait ~75s for CF Pages deploy, then:"
echo "curl -sI https://dchub.cloud/press-release | grep -iE 'HTTP|worker-version'"
echo "curl -s https://dchub.cloud/press-release | grep -oE '<title>[^<]+</title>'"
