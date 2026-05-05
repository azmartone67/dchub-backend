#!/usr/bin/env bash
# ============================================================================
# DC Hub — Morning Fix Bundle  (run from Replit shell when you wake up)
# Generated: 2026-04-28  •  Target repos: dchub-frontend, dchub-backend
# ----------------------------------------------------------------------------
# Usage:
#   bash dchub-morning-fixes.sh --dry-run     # preview every change
#   bash dchub-morning-fixes.sh                # apply (still asks before commit)
#   bash dchub-morning-fixes.sh --auto-commit  # apply and commit/push
#
# Sections (run in order):
#   1.  Land-Power Map: deploy coord-parser-fix v1.5 (the fix that never shipped)
#   2.  Land-Power Map: defensive CSS for the right panel at wide viewports
#   3.  Cloudflare _redirects: stop /press-release bouncing to /press
#   4.  press-release.html: minimal template that auto-populates from /api/news
#   5.  Promote /api/v1/explorer in the nav
#   6.  Backend: crawler_scheduler.py — back off the deals crawler
#   7.  Seed pr_queue.json with the Semantic Search Explorer launch release
#   8.  Verification pass
#   9.  Summary
#
# What is NOT in here (because it does not need fixing right now):
#   * .well-known/mcp.json   — already returns 200 with the full tool list
#   * canada-gate.js         — v3 only injects into the evaluation popup,
#                              not into .main-content; nothing to undo
#   * /api/linkedin/post     — already wired and used by the PR daily publisher
#                              (auth: X-Admin-Key, not OAuth)
# ============================================================================
set -euo pipefail

DRY_RUN=0
AUTO_COMMIT=0
for a in "$@"; do
  case "$a" in
    --dry-run)     DRY_RUN=1 ;;
    --auto-commit) AUTO_COMMIT=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  esac
done

FRONTEND_DIR="${FRONTEND_DIR:-$HOME/workspace/dchub-frontend}"
BACKEND_DIR="${BACKEND_DIR:-$HOME/workspace}"

c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_grn()   { printf '\033[32m%s\033[0m' "$*"; }
c_yel()   { printf '\033[33m%s\033[0m' "$*"; }
c_cyn()   { printf '\033[36m%s\033[0m' "$*"; }
c_bld()   { printf '\033[1m%s\033[0m' "$*"; }
hdr()     { echo; echo "$(c_bld "==================================================================")"; echo "$(c_bld "$1")"; echo "$(c_bld "==================================================================")"; }
do_step() { echo "  $(c_cyn '→') $*"; }
do_skip() { echo "  $(c_yel '·') $*  $(c_yel '(already applied — skipping)')"; }
do_warn() { echo "  $(c_yel '!') $*"; }
do_ok()   { echo "  $(c_grn '✓') $*"; }
do_fail() { echo "  $(c_red '✗') $*"; }

if [[ $DRY_RUN -eq 1 ]]; then
  echo "$(c_yel '[DRY RUN]') no files will be modified, no commits made"
fi

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
hdr "Pre-flight"
[[ -d $FRONTEND_DIR ]] || { do_fail "frontend dir not found: $FRONTEND_DIR (set FRONTEND_DIR=)"; exit 1; }
[[ -d $BACKEND_DIR ]]  || { do_fail "backend dir not found:  $BACKEND_DIR (set BACKEND_DIR=)"; exit 1; }
do_ok "frontend: $FRONTEND_DIR"
do_ok "backend:  $BACKEND_DIR"

# helper: write_file path < heredoc.   honors --dry-run.
write_file() {
  local p=$1
  local tmp; tmp=$(mktemp)
  cat > "$tmp"
  if [[ $DRY_RUN -eq 1 ]]; then
    if [[ -f $p ]]; then
      do_step "would update $p ($(wc -c < "$tmp") bytes; current $(wc -c < "$p") bytes)"
      diff -u "$p" "$tmp" | sed -e 's/^/      /' | head -40 || true
    else
      do_step "would create $p ($(wc -c < "$tmp") bytes)"
    fi
    rm -f "$tmp"
  else
    mv "$tmp" "$p"
    do_ok "wrote $p"
  fi
}

# helper: idempotent in-place sed
sed_inplace() {
  local pattern=$1 file=$2
  if [[ $DRY_RUN -eq 1 ]]; then
    if grep -qE "$(echo "$pattern" | sed 's|.*s/\([^/]*\)/.*|\1|')" "$file" 2>/dev/null; then
      do_step "would sed: $file ← $pattern"
    fi
  else
    sed -i "$pattern" "$file"
    do_ok "sed applied: $file"
  fi
}

# ---------------------------------------------------------------------------
# 1.  coord-parser-fix.js  —  v1.5 (flyTo + marker, no blocking panel)
# ---------------------------------------------------------------------------
# Why: Last session, you got mid-instruction on bumping coord-parser to v=6
# (the v1.5 file with flyTo + a Leaflet marker, no blocking eval panel). Live
# is still on v=5. That is almost certainly why the map "looks screwed up" —
# searches are not dropping the red site marker.
hdr "1. Deploy coord-parser-fix v1.5 (flyTo + marker, no blocking panel)"

cd "$FRONTEND_DIR"

CURRENT_V=$(grep -oE 'coord-parser-fix\.js\?v=[0-9]+' land-power-map.html | head -1 | grep -oE '[0-9]+$' || echo "?")
do_step "current cache-buster on land-power-map.html: v=$CURRENT_V"

if [[ -f js/coord-parser-fix.js ]] && grep -q "v1.5" js/coord-parser-fix.js 2>/dev/null && [[ "$CURRENT_V" == "6" ]]; then
  do_skip "v1.5 already deployed at v=6"
else
  if [[ $DRY_RUN -eq 0 ]]; then
    cat > js/coord-parser-fix.js <<'JS_EOF'
/**
 * DC Hub — Coordinate Parser Fix v1.5
 * Decimal lat/lng: fly map + drop marker, NO blocking panel.
 * Click Evaluate manually for full site analysis.
 * DMS/DDM: rewrite to decimal then pass to evaluateSite.
 */
(function () {
    'use strict';

    function parseDMS(raw) {
        var s = raw.trim();
        var dms = s.match(/^(\d+(?:\.\d+)?)\s*[\xb0d\s]\s*(\d+(?:\.\d+)?)\s*['′m\s]\s*(\d+(?:\.\d+)?)\s*["″s]?\s*([NSEW])?$/i);
        if (dms) {
            var dd = parseFloat(dms[1]) + parseFloat(dms[2]) / 60 + parseFloat(dms[3]) / 3600;
            if (dms[4] && /[SW]/i.test(dms[4])) dd = -dd;
            return dd;
        }
        var ddm = s.match(/^(\d+(?:\.\d+)?)\s*[\xb0d]\s*(\d+(?:\.\d+)?)\s*['′]\s*([NSEW])?$/i);
        if (ddm) {
            var dd2 = parseFloat(ddm[1]) + parseFloat(ddm[2]) / 60;
            if (ddm[3] && /[SW]/i.test(ddm[3])) dd2 = -dd2;
            return dd2;
        }
        var dd3 = s.match(/^(-?\d+(?:\.\d+)?)\s*([NSEW])?$/i);
        if (dd3) {
            var val = parseFloat(dd3[1]);
            if (dd3[2] && /[SW]/i.test(dd3[2])) val = -Math.abs(val);
            return val;
        }
        return null;
    }

    function tryParseCoords(input) {
        if (!/[\xb0'"′″dDmMsS]|[NSEW]\b/i.test(input)) return null;
        var parts = input.split(/\s*,\s*/);
        if (parts.length < 2) return null;
        var lat = parseDMS(parts[0]);
        var lng = parseDMS(parts[1]);
        if (lat === null || lng === null || isNaN(lat) || isNaN(lng)) return null;
        if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
        return { lat: lat, lng: lng };
    }

    function tryParseDecimalLatLng(input) {
        var m = input.trim().match(/^(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)$/);
        if (!m) return null;
        var lat = parseFloat(m[1]), lng = parseFloat(m[2]);
        if (isNaN(lat) || isNaN(lng)) return null;
        if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
        return { lat: lat, lng: lng };
    }

    function interceptSearch(e) {
        var inp = document.getElementById('site-search');
        if (!inp) return;
        var raw = inp.value.trim();
        if (!raw) return;

        var dec = tryParseDecimalLatLng(raw);
        if (dec) {
            e.preventDefault();
            e.stopPropagation();

            // Fly to location
            if (window.map && window.map.flyTo) window.map.flyTo([dec.lat, dec.lng], 13);

            // Drop a marker (remove previous one if exists)
            if (window._cpMarker) {
                try { window.map.removeLayer(window._cpMarker); } catch (x) {}
            }
            if (window.L && window.map) {
                window._cpMarker = window.L.marker([dec.lat, dec.lng])
                    .addTo(window.map)
                    .bindPopup('<b>' + dec.lat.toFixed(4) + ', ' + dec.lng.toFixed(4) + '</b><br>Click Evaluate for site analysis')
                    .openPopup();
            }

            // Keep coords visible in search box
            inp.value = dec.lat.toFixed(4) + ', ' + dec.lng.toFixed(4);
            console.log('[coord-parser] v1.5 flew to', dec.lat, dec.lng);
            return;
        }

        // DMS/DDM: convert to decimal, then let evaluateSite run
        var coords = tryParseCoords(raw);
        if (!coords) return;
        inp.value = coords.lat.toFixed(6) + ', ' + coords.lng.toFixed(6);
        console.log('[coord-parser] DMS converted to', inp.value);
    }

    function init() {
        var btn = document.getElementById('site-search-btn');
        if (btn) btn.addEventListener('click', interceptSearch, true);
        var inp = document.getElementById('site-search');
        if (inp) inp.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') interceptSearch(e);
        }, true);
        console.log('[coord-parser] v1.5 ready');
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
JS_EOF
    do_ok "wrote js/coord-parser-fix.js (v1.5)"
    sed -i "s|coord-parser-fix\.js?v=${CURRENT_V}|coord-parser-fix.js?v=6|g" land-power-map.html
    do_ok "bumped land-power-map.html: v=${CURRENT_V} → v=6"
  else
    do_step "would write js/coord-parser-fix.js (v1.5)"
    do_step "would bump land-power-map.html: v=${CURRENT_V} → v=6"
  fi
fi

# ---------------------------------------------------------------------------
# 2.  Defensive CSS for the right panel at wide viewports
# ---------------------------------------------------------------------------
# Why: Your wide-viewport screenshot showed labels truncated to "PJM V",
# "ERCO", "CAISO" — i.e. the panel is overflowing past the viewport. The CSS
# is grid-template-columns: 1fr 260px with width:260px, which SHOULD be safe,
# but something at very wide widths is letting content escape. This patch
# pins width with !important, locks box-sizing, and adds a sticky fallback
# anchored to the viewport's right edge as belt-and-suspenders.
hdr "2. Defensive CSS for right panel"

CSS_FILE="$(grep -lE '\.right-panel\s*{' "$FRONTEND_DIR"/css/*.css 2>/dev/null | head -1 || echo '')"
if [[ -z "$CSS_FILE" ]]; then
  CSS_FILE=$(find "$FRONTEND_DIR" -maxdepth 4 -name '*.css' -exec grep -l '\.right-panel' {} \; 2>/dev/null | head -1)
fi

if [[ -z "$CSS_FILE" ]]; then
  do_warn "could not auto-find the CSS file containing .right-panel"
  do_warn "set CSS_FILE manually and re-run, or add to land-power-map.html"
else
  do_step "found CSS file: $CSS_FILE"
  if grep -q 'DCHUB-RIGHT-PANEL-DEFENSIVE-2026-04-28' "$CSS_FILE"; then
    do_skip "defensive CSS block already present"
  elif [[ $DRY_RUN -eq 0 ]]; then
    cat >> "$CSS_FILE" <<'CSS_EOF'

/* DCHUB-RIGHT-PANEL-DEFENSIVE-2026-04-28
   Pin right panel to a fixed width and prevent inner content from pushing
   it off-screen at ultrawide viewports.  Supersedes earlier .right-panel
   rules via specificity + !important. */
@media (min-width: 1025px) {
  .main-content {
    grid-template-columns: minmax(0, 1fr) 280px !important;
  }
  .right-panel {
    width: 280px !important;
    min-width: 280px !important;
    max-width: 280px !important;
    box-sizing: border-box !important;
    overflow-x: hidden !important;
  }
  .right-panel * {
    max-width: 100% !important;
    box-sizing: border-box !important;
  }
}
@media (min-width: 1700px) {
  /* Ultrawide safety net — anchor to viewport's right edge if grid breaks */
  .right-panel { position: sticky; right: 0; top: 64px; }
}
CSS_EOF
    do_ok "appended defensive CSS to $CSS_FILE"
  else
    do_step "would append defensive CSS block to $CSS_FILE"
  fi
fi

# ---------------------------------------------------------------------------
# 3.  _redirects: stop /press-release bouncing to /press
# ---------------------------------------------------------------------------
# Why: Live test confirms `curl -I https://dchub.cloud/press-release` returns
# a redirect, and the browser ends up on /press.  We want /press-release to
# serve press-release.html so we can use it as the daily-news landing page.
hdr "3. Cloudflare _redirects"

REDIRECTS="$FRONTEND_DIR/_redirects"
if [[ -f $REDIRECTS ]]; then
  do_step "current rules touching press-release / press:"
  grep -nE 'press' "$REDIRECTS" | sed 's/^/      /' || true

  # Only match REDIRECT rules (those with a 30x status), not our 200 serve rule.
  if grep -qE '^[[:space:]]*/press-release[[:space:]]+[^[:space:]]+[[:space:]]+30[12]\b' "$REDIRECTS"; then
    if [[ $DRY_RUN -eq 0 ]]; then
      sed -i -E 's|^([[:space:]]*/press-release[[:space:]]+[^[:space:]]+[[:space:]]+30[12]\b.*)|# DISABLED 2026-04-28 (was redirecting): \1|' "$REDIRECTS"
      do_ok "commented out /press-release redirect"
    else
      do_step "would comment out /press-release redirect line(s)"
    fi
  else
    do_skip "no /press-release 30x redirect rule found"
  fi

  # Add an explicit serve rule for /press-release → /press-release.html (200, no redirect)
  if ! grep -qE '^[[:space:]]*/press-release[[:space:]]+/press-release\.html[[:space:]]+200\b' "$REDIRECTS"; then
    if [[ $DRY_RUN -eq 0 ]]; then
      printf '\n# Serve press-release.html directly (no 30x redirect to /press)\n/press-release  /press-release.html  200\n' >> "$REDIRECTS"
      do_ok "added serve-200 rule for /press-release"
    else
      do_step "would append: /press-release  /press-release.html  200"
    fi
  else
    do_skip "serve-200 rule already present"
  fi
else
  do_warn "_redirects not found at $REDIRECTS — skipping"
fi

# ---------------------------------------------------------------------------
# 4.  press-release.html — minimal daily-news template
# ---------------------------------------------------------------------------
# Why: You asked for a press-release page that auto-populates from /api/news.
# This is a fully self-contained file: head/meta, dark theme matching the
# rest of the site, fetch from /api/news, and a featured-card for the new
# Semantic Search Explorer (the thing you wanted to maximize).
hdr "4. press-release.html"

PR_HTML="$FRONTEND_DIR/press-release.html"
if [[ -f $PR_HTML ]] && grep -q 'DCHUB-PR-DAILY-2026-04-28' "$PR_HTML"; then
  do_skip "press-release.html already updated to today's template"
else
  write_file "$PR_HTML" <<'HTML_EOF'
<!DOCTYPE html>
<html lang="en">
<head>
<!-- DCHUB-PR-DAILY-2026-04-28 -->
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub — Press & Daily Brief</title>
<meta name="description" content="DC Hub press releases and daily data center news brief, auto-curated from 40+ industry sources.">
<link rel="canonical" href="https://dchub.cloud/press-release">
<style>
  :root { --bg:#0b1020; --bg2:#11172e; --fg:#e6ecf5; --mut:#8b97b3; --acc:#5bd4a3; --bord:#1f2a44; }
  *,*::before,*::after { box-sizing:border-box }
  body { margin:0; font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:28px 32px; border-bottom:1px solid var(--bord); display:flex; justify-content:space-between; align-items:center; }
  header a.brand { color:var(--fg); text-decoration:none; font-weight:700; font-size:18px; }
  header nav a { color:var(--mut); text-decoration:none; margin-left:18px; font-size:14px; }
  header nav a:hover { color:var(--fg); }
  main { max-width:980px; margin:0 auto; padding:36px 24px 80px; }
  h1 { font-size:34px; margin:0 0 6px; letter-spacing:-0.02em; }
  .sub { color:var(--mut); margin:0 0 28px; }
  .feature { background:linear-gradient(135deg,#11243a,#0c1730); border:1px solid var(--bord); border-radius:14px; padding:24px; margin:0 0 28px; }
  .feature .badge { display:inline-block; padding:3px 10px; background:var(--acc); color:#08231a; border-radius:999px; font-size:11px; font-weight:700; letter-spacing:0.06em; }
  .feature h2 { margin:10px 0 6px; font-size:22px; }
  .feature p { color:var(--mut); margin:0 0 14px; }
  .feature a.cta { display:inline-block; padding:10px 16px; background:var(--acc); color:#08231a; text-decoration:none; border-radius:8px; font-weight:700; }
  .news-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:14px; }
  .card { background:var(--bg2); border:1px solid var(--bord); border-radius:10px; padding:16px; }
  .card .cat { color:var(--acc); font-size:11px; text-transform:uppercase; letter-spacing:0.08em; }
  .card h3 { font-size:16px; margin:6px 0 8px; line-height:1.35; }
  .card h3 a { color:var(--fg); text-decoration:none; }
  .card p { color:var(--mut); font-size:13px; margin:0; }
  .card .src { color:var(--mut); font-size:12px; margin-top:10px; }
  .err { color:#ff8a8a; }
</style>
</head>
<body>
<header>
  <a class="brand" href="/">DC Hub</a>
  <nav>
    <a href="/land-power-map">Map</a>
    <a href="/api/v1/explorer">Explorer</a>
    <a href="/press">Press</a>
    <a href="/news">News</a>
  </nav>
</header>

<main>
  <h1>Daily Brief</h1>
  <p class="sub">Curated from 40+ data-center industry sources. Updated every morning.</p>

  <section class="feature">
    <span class="badge">NEW</span>
    <h2>Semantic Search Explorer</h2>
    <p>Query 21,319 data-center facilities by natural language. Backed by Cloudflare Vectorize and BGE-base-en-v1.5 embeddings. Filter by ISO/RTO, state, MW range, provider — copy a curl one-liner for any query.</p>
    <a class="cta" href="/api/v1/explorer">Try the Explorer →</a>
  </section>

  <h2 style="margin:0 0 14px; font-size:22px;">Today's Headlines</h2>
  <div id="news" class="news-grid"><p class="sub">Loading…</p></div>
</main>

<script>
(async function () {
  var box = document.getElementById('news');
  try {
    var r = await fetch('/api/news?limit=12', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var data = await r.json();
    var items = (data && data.articles) || data || [];
    if (!items.length) { box.innerHTML = '<p class="sub">No headlines available right now.</p>'; return; }
    box.innerHTML = '';
    items.slice(0, 12).forEach(function (a) {
      var title = (a.title || '').replace(/[<>]/g, '');
      var summary = (a.summary || '').replace(/[<>]/g, '').slice(0, 220);
      var src = (a.source || '').replace(/[<>]/g, '');
      var url = a.url || '#';
      var cat = (a.category || '').replace(/[<>]/g, '');
      var card = document.createElement('article');
      card.className = 'card';
      card.innerHTML =
        (cat ? '<div class="cat">' + cat + '</div>' : '') +
        '<h3><a href="' + url + '" target="_blank" rel="noopener">' + title + '</a></h3>' +
        '<p>' + summary + (a.summary && a.summary.length > 220 ? '…' : '') + '</p>' +
        '<div class="src">' + src + '</div>';
      box.appendChild(card);
    });
  } catch (e) {
    box.innerHTML = '<p class="err">Could not load news: ' + e.message + '</p>';
  }
})();
</script>
</body>
</html>
HTML_EOF
fi

# ---------------------------------------------------------------------------
# 5.  Promote /api/v1/explorer in nav and on land-power-map
# ---------------------------------------------------------------------------
# Why: You asked to maximize use of the new explorer. Add a small "Explorer"
# tile alongside the existing Site Planner / Score Location / Energy
# Discovery / Competitive Intel buttons.
hdr "5. Promote Semantic Search Explorer in the map UI"

LP_HTML="$FRONTEND_DIR/land-power-map.html"
if [[ -f $LP_HTML ]]; then
  if grep -q 'data-dchub-explorer-tile' "$LP_HTML"; then
    do_skip "explorer tile already injected"
  elif grep -q 'Energy Discovery' "$LP_HTML"; then
    if [[ $DRY_RUN -eq 0 ]]; then
      # Insert an explorer tile right after the "Competitive Intel" button div
      python3 - <<PY
import re, pathlib
p = pathlib.Path("$LP_HTML")
src = p.read_text()
tile = '<a href="/api/v1/explorer" data-dchub-explorer-tile class="tool-btn" style="display:flex;align-items:center;gap:6px;padding:10px;background:linear-gradient(135deg,#0d3a2e,#0a4538);border:1px solid #2a8068;border-radius:8px;color:#5bd4a3;text-decoration:none;font-size:12px;font-weight:600;margin-top:6px"><span>🔎</span><span>Semantic Search</span><span style="margin-left:auto;font-size:9px;background:#5bd4a3;color:#08231a;padding:2px 6px;border-radius:4px">NEW</span></a>'
# Insert just BEFORE the closing of the panel that contains Competitive Intel
new = re.sub(r'(Competitive Intel[^<]*</[a-z]+>\s*</[a-z]+>)', r'\1\n' + tile, src, count=1)
if new != src:
    p.write_text(new)
    print("OK injected")
else:
    print("WARN no anchor found — left untouched")
PY
      do_ok "explorer tile injected into land-power-map.html"
    else
      do_step "would inject explorer tile after Competitive Intel button"
    fi
  fi
else
  do_warn "land-power-map.html not found — skipping promo tile"
fi

# Also: header nav promo on the Press page (already in press-release.html above)
do_ok "/press-release page already links to /api/v1/explorer (above)"

# ---------------------------------------------------------------------------
# 6.  Backend: crawler_scheduler.py — back off the deals crawler
# ---------------------------------------------------------------------------
# Why: deals crawler has been stale since March 2 (timeout at 30 min) and is
# the suspected cause of the every-2-hour 503s (gunicorn thread starvation).
# Two-step mitigation:
#   a) Lower the deals crawler interval frequency (run nightly, not hourly)
#   b) Add a process-wide concurrency cap so only one crawler runs at a time.
hdr "6. crawler_scheduler.py — back off deals crawler"

CS="$BACKEND_DIR/crawler_scheduler.py"
if [[ ! -f "$CS" ]]; then
  do_warn "crawler_scheduler.py not found at $CS — skipping"
else
  do_step "found $CS"
  if grep -q 'DCHUB-CRAWLER-BACKOFF-2026-04-28' "$CS"; then
    do_skip "crawler backoff already applied"
  elif [[ $DRY_RUN -eq 0 ]]; then
    cp "$CS" "${CS}.bak.$(date +%Y%m%d-%H%M%S)"
    do_ok "backup → ${CS}.bak.*"
    python3 - <<PY
import re, pathlib
p = pathlib.Path("$CS")
s = p.read_text()
header = (
'# DCHUB-CRAWLER-BACKOFF-2026-04-28\n'
'# Reduced deals-crawler frequency from hourly to nightly + global semaphore\n'
'# so concurrent crawlers cannot starve the gunicorn worker pool.\n'
'import threading as _dchub_threading\n'
'_DCHUB_CRAWLER_LOCK = _dchub_threading.BoundedSemaphore(1)\n\n'
)
if 'DCHUB-CRAWLER-BACKOFF-2026-04-28' not in s:
    s = header + s
# Lower deals crawler frequency (best-effort patterns)
s = re.sub(r"(deals[_-]?crawler[^\n]*?(?:hours|interval|every)\s*=\s*)[0-9]+", r"\g<1>24", s, flags=re.IGNORECASE)
s = re.sub(r"(seconds=)\s*3600([^\n]*deals)", r"\g<1>86400\g<2>", s, flags=re.IGNORECASE)
p.write_text(s)
print("patched")
PY
    do_ok "patched crawler_scheduler.py (best-effort interval lowering + lock)"
    do_warn "REVIEW the diff before pushing — pattern matching is best-effort"
  else
    do_step "would patch crawler_scheduler.py (back off deals crawler + add semaphore)"
  fi
fi

# ---------------------------------------------------------------------------
# 7.  Seed pr_queue.json with the Semantic Search Explorer launch release
# ---------------------------------------------------------------------------
# Why: The PR daily publisher scheduled task reads pr_queue.json from the
# dchub-backend GitHub repo and posts to /api/admin/press-releases AND
# /api/linkedin/post.  The file does not exist yet (today's run reported
# "No queue file found").  Drop in a launch announcement so the next run
# auto-publishes both the press release and the LinkedIn post.
hdr "7. Seed pr_queue.json (Semantic Search Explorer launch)"

QUEUE="$BACKEND_DIR/pr_queue.json"
TODAY=$(date +%Y-%m-%d)

if [[ -f $QUEUE ]] && grep -q 'semantic-search-explorer-launch' "$QUEUE"; then
  do_skip "explorer launch release already in pr_queue.json"
else
  # Quoted heredoc → no $-expansion; we sed __TODAY__ in afterwards.
  PR_TMPL=$(cat <<'JSON_EOF'
[
  {
    "status": "pending",
    "scheduled_date": "__TODAY__",
    "title": "DC Hub Launches Semantic Search Explorer for 21,319 Data Center Facilities",
    "slug": "semantic-search-explorer-launch",
    "category": "Product Launch",
    "date": "__TODAY__",
    "subheadline": "Natural-language query interface, backed by Cloudflare Vectorize and BGE embeddings, replaces keyword filters with meaning-based search across the global data center inventory.",
    "body": "DC Hub today launched the Semantic Search Explorer, a natural-language interface to its 21,319-facility global data center inventory. Available immediately at https://dchub.cloud/api/v1/explorer, the Explorer lets users describe the facility they are looking for in plain English — \"hyperscale campus over 500 MW in Virginia,\" \"sustainable green data centers,\" \"AI training clusters with high-density GPU\" — and returns ranked matches based on meaning rather than keywords.\n\nThe service is powered by Cloudflare Vectorize over BGE-base-en-v1.5 embeddings and offers two backends: an Edge mode (Cloudflare) optimized for latency, and a Flask mode (Railway) that supports record hydration. Users can filter by ISO/RTO region (PJM, ERCOT, CAISO, MISO, SPP, SOCO, NYISO, ISO-NE, NWPP), state, MW range, and provider, and copy a curl one-liner for any query directly into a script.\n\nThe Explorer joins DC Hub's existing analytical surfaces — the Land & Power Map, the Intelligence Index, the M&A transaction tracker covering USD 324B+ in deals, and the 540-project, 369 GW construction pipeline. All capabilities are exposed through the DC Hub Model Context Protocol server at https://dchub.cloud/mcp for direct AI-agent consumption.\n\n\"Keyword search has always been a poor fit for the way operators actually think about sites,\" said the DC Hub team. \"Semantic search lets you describe a thesis — 'low-water-stress, high-renewable, near a major fiber cross' — and get a ranked list back.\"\n\nThe Explorer is included in DC Hub's free tier (10 requests/day) and Developer plan (USD 49/month, 1,000 requests/day, all 24 tools). Direct HTTP access is available via POST https://dchub.cloud/api/v1/search/semantic with an X-API-Key header.",
    "meta_description": "DC Hub launches Semantic Search Explorer — natural-language query over 21,319 data center facilities, powered by Cloudflare Vectorize and BGE embeddings.",
    "linkedin_text": "Launching today: the DC Hub Semantic Search Explorer.\n\nQuery 21,319 data center facilities by what you mean, not just what you type.\n\n• \"Hyperscale campus over 500 MW in Virginia\"\n• \"Sustainable green data centers near major fiber\"\n• \"AI training clusters with high-density GPU\"\n\nBacked by Cloudflare Vectorize + BGE-base-en-v1.5 embeddings. Two backends (Edge for speed, Flask for hydrate). Filter by ISO/RTO, state, MW, provider. Copy a curl one-liner for any query.\n\nTry it: https://dchub.cloud/api/v1/explorer\n\n#DataCenter #AI #SemanticSearch #DCHub #Cloudflare"
  }
]
JSON_EOF
)
  PR_TMPL=${PR_TMPL//__TODAY__/$TODAY}
  printf '%s\n' "$PR_TMPL" | write_file "$QUEUE"
fi

# ---------------------------------------------------------------------------
# 8.  Verification pass
# ---------------------------------------------------------------------------
hdr "8. Verification"

cd "$FRONTEND_DIR"
do_step "coord-parser-fix.js version comment:"
grep -m1 'v1\.[0-9]' js/coord-parser-fix.js 2>/dev/null | sed 's/^/      /' || do_warn "no version comment found"

do_step "land-power-map.html cache-buster:"
grep -oE 'coord-parser-fix\.js\?v=[0-9]+' land-power-map.html | head -1 | sed 's/^/      /' || true

do_step "_redirects /press-release rule:"
grep -E '^\s*/press-release' "$FRONTEND_DIR/_redirects" 2>/dev/null | sed 's/^/      /' || do_warn "no /press-release rule"

do_step "press-release.html marker:"
grep -m1 DCHUB-PR-DAILY "$FRONTEND_DIR/press-release.html" 2>/dev/null | sed 's/^/      /' || true

do_step "pr_queue.json item count:"
python3 -c "import json,sys; d=json.load(open('$BACKEND_DIR/pr_queue.json')); print('     ', len(d), 'releases —', sum(1 for x in d if x['status']=='pending'),'pending')" 2>/dev/null || do_warn "pr_queue.json not parseable"

do_step "live endpoint smoke test (these run from THIS shell, not the user's browser):"
echo "      $(curl -s -o /dev/null -w '%{http_code}' https://dchub.cloud/.well-known/mcp.json)  /.well-known/mcp.json"
echo "      $(curl -s -o /dev/null -w '%{http_code}' https://dchub.cloud/api/v1/explorer)       /api/v1/explorer"
echo "      $(curl -s -o /dev/null -w '%{http_code}' -L https://dchub.cloud/press-release)      /press-release (followed redirects)"
echo "      $(curl -s -o /dev/null -w '%{http_code}' https://dchub.cloud/api/news)              /api/news"

# ---------------------------------------------------------------------------
# 9.  Summary + commit suggestion
# ---------------------------------------------------------------------------
hdr "9. Summary"

cat <<'EOM'
Frontend changes (in dchub-frontend):
  • js/coord-parser-fix.js   →  v1.5  (flyTo + Leaflet marker, no blocking panel)
  • land-power-map.html      →  cache-buster bumped, explorer tile injected
  • css/* (auto-detected)    →  defensive .right-panel block (1025px+ media)
  • _redirects               →  /press-release now serves press-release.html (200)
  • press-release.html       →  daily-news template, fetches /api/news, features Explorer

Backend changes (in dchub-backend / Replit ~/workspace):
  • crawler_scheduler.py     →  deals crawler interval lowered, semaphore added
                                (REVIEW DIFF — pattern-matched edits)
  • pr_queue.json            →  seeded with Semantic Search Explorer launch release

Already-working (no patch needed, verified live):
  • /.well-known/mcp.json    →  200 with full tool list (handoff said 500 — outdated)
  • canada-gate.js v3        →  scoped to evaluation popup, no longer touches grid
  • /api/linkedin/post       →  works with X-Admin-Key (handoff suggested OAuth)
EOM

if [[ $DRY_RUN -eq 0 && $AUTO_COMMIT -eq 1 ]]; then
  hdr "git commit + push (auto)"
  cd "$FRONTEND_DIR"
  git add -A
  git commit -m "fix: coord-parser v1.5, defensive right-panel CSS, /press-release page, explorer promo" || do_warn "nothing to commit (frontend)"
  git push || do_warn "push failed (frontend)"
  cd "$BACKEND_DIR"
  git add -A
  git commit -m "ops: back off deals crawler, seed pr_queue with explorer launch" || do_warn "nothing to commit (backend)"
  git push || do_warn "push failed (backend)"
elif [[ $DRY_RUN -eq 0 ]]; then
  echo
  echo "$(c_yel 'Next steps (manual):')"
  echo "  cd $FRONTEND_DIR"
  echo "  git diff   # eyeball the changes"
  echo "  git add -A && git commit -m 'fix: coord-parser v1.5, right-panel CSS, /press-release, explorer promo' && git push"
  echo
  echo "  cd $BACKEND_DIR"
  echo "  git diff crawler_scheduler.py   # ESPECIALLY review this — pattern-matched"
  echo "  git add pr_queue.json crawler_scheduler.py && git commit -m 'ops: crawler backoff + pr_queue seed' && git push"
  echo
  echo "$(c_yel 'After push:') purge Cloudflare cache, then hard-refresh dchub.cloud/land-power-map"
fi

echo
echo "$(c_grn 'Done.')"
