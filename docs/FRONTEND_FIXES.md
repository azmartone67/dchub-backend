# DC Hub — Frontend Fixes

Hand this to a Claude Code session scoped to **`azmartone67/dchub-frontend`** (`main`).
Both backend halves are already merged & live in `dchub-backend`; these are the
remaining frontend-only changes.

---

## Fix 1 — Integration logos (integrations page)

**Backend is done:** `GET /api/v1/mcp/platforms` now returns, per platform:
`logo_url`, `category` (`platform` / `other`), `connected_count`,
`hidden_noise_count`, `tools_count: 38`, and **ISO-formatted** `last_seen` /
`first_seen`. The noise (curl/qa/scanners/registry crawlers) is already filtered
server-side.

**Change on the page** (where each platform card draws its icon/avatar) — render
the API logo, keep the letter avatar as the `onerror` fallback so a missing image
can never 404 to a broken icon:

```js
const initial = ((p.platform || p.name || '?')[0] || '?').toUpperCase();
const avatar = p.logo_url
  ? `<img src="${p.logo_url}" alt="" width="28" height="28" loading="lazy"
         style="border-radius:6px;object-fit:contain"
         onerror="this.outerHTML='<div class=\\'platform-letter\\'>${initial}</div>'">`
  : `<div class="platform-letter">${initial}</div>`;
```

While you're in there (all now provided by the API — stop hardcoding):
- **Tool count:** use `data.tools_count` (38), not the literal `11` → kills the "38/11".
- **Connected count:** use `data.connected_count`.
- **Timestamps:** `last_seen`/`first_seen` are ISO strings now, so
  `p.last_seen ? timeAgo(p.last_seen) : '—'` parses cleanly → kills "NaNd ago" /
  "Invalid Date".
- No client-side noise filtering needed (server already did it); optionally show
  `data.hidden_noise_count` as a muted "+N registry probes hidden".

**PR title:** `fix: render API-provided platform logos + honest counts on integrations`

---

## Fix 2 — Measure tool "Clear" leaves stale labels (`js/land-power-app.js`)

Live symptom: Clear logs success but the green `X.XX mi` distance tags + dashed
lines stay on the map.

**Root cause:** the `.measure-dist-label` markers are added with `.addTo(map)`
instead of `.addTo(measureLayer)`, so `measureLayer.clearLayers()` misses them.

**(a)** In the map-click handler, find the `.measure-dist-label` `L.marker([midLat,midLng], …)`
and change its `.addTo(map)` → **`.addTo(measureLayer)`**. Confirm the `Point N`
label and the dashed `L.polyline` also use `.addTo(measureLayer)`.

**(b)** Replace `clearMeasure()` with this hardened version (sweeps any stray
artifact off the map regardless of how it was attached):

```js
function clearMeasure() {
    console.log('🗑️ Clearing measurements...');
    measurePoints = []; measureMarkers = []; measureLines = [];
    if (measureLayer) measureLayer.clearLayers();
    // Defensive sweep: kill ANY stray measure artifact added straight to the map
    try {
        map.eachLayer(function(lyr){
            var ic = lyr.options && lyr.options.icon;
            var cn = (ic && ic.options && ic.options.className) || '';
            var strayLabel = cn.indexOf('measure-label') !== -1 || cn.indexOf('measure-dist-label') !== -1;
            var strayLine  = (lyr instanceof L.Polyline) && lyr.options && lyr.options.dashArray === '10, 5';
            if (strayLabel || strayLine) map.removeLayer(lyr);
        });
    } catch(e) {}
    try { document.querySelectorAll('.measure-label,.measure-dist-label')
        .forEach(function(el){ (el.closest('.leaflet-marker-icon')||el).remove(); }); } catch(e){}
    var r=document.getElementById('measure-result'); if(r) r.style.display='none';
    var s=document.getElementById('measure-segments'); if(s) s.innerHTML='';
    var inf=document.getElementById('measure-info'); if(inf) inf.innerHTML='👆 Click on map to set <strong>Point 1</strong>';
    measureMode=false; window.measureMode=false;
    ['measure-btn','measure-panel','map'].forEach(function(id){var el=document.getElementById(id); if(el) el.classList.remove('active','measure-cursor');});
    console.log('✅ Measurements cleared (hardened)');
}
```

**(c)** Cache-bust: bump the include in `land-power-map.html`
`js/land-power-app.js?v=NNN` → next number.

**Verify:** drop 3+ points → Clear → all green `mi` tags + dashed lines vanish,
panel resets; Undo removes only the last segment's label + line.

**PR title:** `fix: measure-tool Clear removes all artifacts (stray distance labels)`

---

### Immediate console hotfix (use now, before deploy) — paste in F12 on the live map:
```js
window.clearMeasure && window.clearMeasure();
document.querySelectorAll('.measure-label,.measure-dist-label')
  .forEach(e => (e.closest('.leaflet-marker-icon') || e).remove());
```
