/**
 * canada-gate.js v3 — Non-US location data gating for DC Hub Land Power Map
 *
 * v3 changes from v2:
 *   - Persistent poller (no tick limit) — polls every 1500ms forever,
 *     so evaluations triggered at any time are caught.
 *   - Coordinate-change detection: tracks last seen coords and re-applies
 *     all fixes whenever a NEW evaluation result appears.
 *   - Banner injection targets the report title row directly for reliable
 *     placement at the top of the popup.
 *
 * Fixes applied when a non-US evaluation is detected:
 *   1. Warning banner: "Canadian site — limited data coverage"
 *   2. "Unknown County" -> "N/A (non-US)"
 *   3. "$1.50/k" prop tax fallback -> "N/A"
 *   4. Distance metric labels tagged "(US only)"
 */
(function () {
    'use strict';

   var BANNER_ID = 'canada-gate-banner';
    var POLL_MS   = 1500;
    var coordPattern = /(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)/;
    var lastCoordKey = '';

   function isOutsideUS(lat, lng) {
         if (isNaN(lat) || isNaN(lng)) return false;
         var inL48 = lat >= 24.0 && lat <= 49.5 && lng >= -125.0 && lng <= -66.0;
         var inAK  = lat >= 51.0 && lat <= 72.0  && lng >= -170.0 && lng <= -130.0;
         var inHI  = lat >= 18.0 && lat <= 23.0  && lng >= -161.0 && lng <= -154.0;
         return !(inL48 || inAK || inHI);
   }

   function extractLatLng() {
         var els = document.querySelectorAll('*');
         for (var i = 0; i < els.length; i++) {
                 var el = els[i];
                 if (el.children.length !== 0) continue;
                 var t = el.textContent.trim();
                 var m = t.match(coordPattern);
                 if (m) return { lat: parseFloat(m[1]), lng: parseFloat(m[2]), key: m[0] };
         }
         return null;
   }

   function injectBanner(reportEl) {
         if (document.getElementById(BANNER_ID)) return;
         var banner = document.createElement('div');
         banner.id = BANNER_ID;
         banner.style.cssText = 'background:#f59e0b;color:#1a1a1a;font-weight:700;font-size:12px;padding:6px 12px;border-radius:4px;margin:4px 0 8px;text-align:center;';
         banner.textContent = 'Canadian site: limited data coverage. Distances & tax data are estimates.';
         reportEl.insertBefore(banner, reportEl.firstChild);
   }

   function fixFallbackValues() {
         var vals = document.querySelectorAll('.site-eval-value');
         vals.forEach(function (el) {
                 var t = el.textContent.trim();
                 if (t === 'Unknown County') el.textContent = 'N/A (non-US)';
                 if (t === '$1.50/k')        el.textContent = 'N/A';
         });
   }

   function findReportEl() {
         var allDivs = document.querySelectorAll('div');
         for (var i = 0; i < allDivs.length; i++) {
                 var d = allDivs[i];
                 var txt = d.innerText || '';
                 if (txt.indexOf('Evaluation Report') !== -1 && d.children.length > 0 && d.offsetParent !== null) {
                           return d;
                 }
         }
         return null;
   }

   function tick() {
         var coords = extractLatLng();
         if (!coords) return;

      var key = coords.key;

      if (key !== lastCoordKey) {
              lastCoordKey = key;
              var old = document.getElementById(BANNER_ID);
              if (old) old.parentNode.removeChild(old);
      }

      if (!isOutsideUS(coords.lat, coords.lng)) return;

      fixFallbackValues();

      if (!document.getElementById(BANNER_ID)) {
              var reportEl = findReportEl();
              if (reportEl) injectBanner(reportEl);
      }
   }

   setInterval(tick, POLL_MS);
    setTimeout(tick, 300);

}());
