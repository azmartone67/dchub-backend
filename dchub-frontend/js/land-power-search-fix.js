/**
 * land-power-search-fix.js v2 — Address Search Bar Safety Net
 * =============================================================
 * Deploy: Upload to Cloudflare Pages as js/land-power-search-fix.js
 * Add to land-power-map.html AFTER land-power-app.js:
 *   <script src="js/land-power-search-fix.js?v=2"></script>
 *
 * HOW IT WORKS:
 *   land-power-app.js now exposes window.evaluateSite on success.
 *   This script waits 3s, then checks:
 *     - If window.evaluateSite EXISTS → original handlers are alive.
 *       We only patch the fiber ID mismatch and add diagnostic logging.
 *     - If window.evaluateSite is MISSING → app.js crashed before
 *       the search handlers registered. We wire up full fallback
 *       autocomplete + evaluate using Nominatim + map zoom + pin.
 *
 * NON-DESTRUCTIVE: Never overwrites working original handlers.
 *
 * Author: DC Hub / Claude session Mar 18 2026
 */

(function() {
  'use strict';

  var INIT_DELAY  = 3000;
  var MAX_RETRIES = 8;
  var RETRY_MS    = 1500;
  var attempt     = 0;

  function init() {
    attempt++;

    var searchInput = document.getElementById('site-search');
    var searchBtn   = document.getElementById('site-search-btn');
    var dropdown    = document.getElementById('autocomplete-dropdown');

    if (!searchInput || !searchBtn) {
      if (attempt < MAX_RETRIES) setTimeout(init, RETRY_MS);
      else console.error('[Search Fix] Elements not found after retries');
      return;
    }

    if (window._searchFixApplied) return;
    window._searchFixApplied = true;

    // ── Check if original handlers are alive ────────────────
    var originalAlive = typeof window.evaluateSite === 'function';

    if (originalAlive) {
      console.info('[Search Fix] ✅ window.evaluateSite found — original handlers alive. Standby mode only.');
      applyFiberFixes();
      addDiagnostics();
      return;
    }

    // ── Original handlers are DEAD — wire full fallback ─────
    console.warn('[Search Fix] ⚠️ window.evaluateSite NOT found — land-power-app.js crashed before search handlers.');
    console.warn('[Search Fix] Wiring full fallback search...');

    var acTimeout     = null;
    var selectedIndex = -1;

    // ── Autocomplete on typing ──────────────────────────────
    searchInput.addEventListener('input', function() {
      var query = searchInput.value.trim();
      clearTimeout(acTimeout);
      selectedIndex = -1;

      if (!dropdown) return;
      if (query.length < 3) { dropdown.classList.remove('show'); return; }
      if (/^-?\d+\.?\d*\s*,\s*-?\d+\.?\d*$/.test(query)) { dropdown.classList.remove('show'); return; }

      dropdown.innerHTML = '<div class="autocomplete-loading">🔍 Searching...</div>';
      dropdown.classList.add('show');

      acTimeout = setTimeout(function() { fetchSuggestions(query); }, 300);
    });

    function fetchSuggestions(query) {
      var url = 'https://nominatim.openstreetmap.org/search?format=json&addressdetails=1&limit=6'
              + '&countrycodes=us,ca,mx,gb,de,nl,ie,fr,se,fi,sg,jp,au,in,hk,ae,sa'
              + '&q=' + encodeURIComponent(query);

      fetch(url, { headers: { 'Accept': 'application/json' } })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (!dropdown) return;
          if (!data || data.length === 0) {
            dropdown.innerHTML = '<div class="autocomplete-loading">No results found</div>';
            return;
          }
          var html = '';
          data.forEach(function(item) {
            var addr = item.address || {};
            var mainPart = item.display_name.split(',')[0];
            var subPart  = item.display_name.split(',').slice(1, 4).join(',').trim();
            var city     = addr.city || addr.town || addr.village || addr.county || '';
            var state    = addr.state || '';
            var postcode = addr.postcode || '';
            var country  = addr.country_code ? addr.country_code.toUpperCase() : '';
            var formatted = mainPart;
            if (city) formatted = mainPart + ', ' + city;
            if (state && country === 'US') formatted += ', ' + state;
            if (postcode) formatted += ' ' + postcode;
            if (country && country !== 'US') formatted += ', ' + country;

            html += '<div class="autocomplete-item" data-lat="' + item.lat
                  + '" data-lng="' + item.lon
                  + '" data-display="' + formatted.replace(/"/g, '&quot;') + '">'
                  + '<div class="addr-main">' + mainPart + '</div>'
                  + '<div class="addr-sub">' + subPart + '</div>'
                  + '</div>';
          });
          dropdown.innerHTML = html;

          dropdown.querySelectorAll('.autocomplete-item').forEach(function(el) {
            el.addEventListener('click', function() {
              var lat = parseFloat(this.dataset.lat);
              var lng = parseFloat(this.dataset.lng);
              var display = this.dataset.display;
              searchInput.value = display;
              dropdown.classList.remove('show');
              doEvaluate(lat, lng, display);
            });
          });
        })
        .catch(function(err) {
          console.error('[Search Fix] autocomplete error:', err);
          if (dropdown) dropdown.innerHTML = '<div class="autocomplete-loading">Error fetching results</div>';
        });
    }

    // ── Keyboard nav ────────────────────────────────────────
    searchInput.addEventListener('keydown', function(e) {
      if (!dropdown) return;
      var items = dropdown.querySelectorAll('.autocomplete-item');
      if (!items.length) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); selectedIndex = Math.min(selectedIndex + 1, items.length - 1); highlight(items); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); selectedIndex = Math.max(selectedIndex - 1, 0); highlight(items); }
      else if (e.key === 'Enter' && selectedIndex >= 0) { e.preventDefault(); items[selectedIndex].click(); }
      else if (e.key === 'Escape') { dropdown.classList.remove('show'); }
    });

    function highlight(items) {
      items.forEach(function(el, i) {
        el.style.background = i === selectedIndex ? 'var(--accent)' : '';
        el.style.color = i === selectedIndex ? '#fff' : '';
      });
    }

    // ── Evaluate (fallback — zoom + pin) ────────────────────
    function doEvaluate(lat, lng, address) {
      console.info('[Search Fix] Evaluate:', lat, lng, address);

      // Try original if it appeared late (e.g. async load)
      if (typeof window.evaluateSite === 'function') {
        window.evaluateSite(lat, lng, address);
        return;
      }

      // Fallback: zoom + marker
      if (typeof map !== 'undefined') {
        map.setView([lat, lng], 14);
        try {
          var label = address || (lat.toFixed(4) + ', ' + lng.toFixed(4));
          L.marker([lat, lng]).addTo(map)
            .bindPopup(
              '<div style="text-align:center;min-width:220px;">' +
              '<div style="font-size:16px;font-weight:700;margin-bottom:8px;">📍 ' + label + '</div>' +
              '<div style="font-size:12px;color:#6b7280;">' + lat.toFixed(6) + ', ' + lng.toFixed(6) + '</div>' +
              '<div style="margin-top:10px;padding:8px;background:#1a1a2e;border-radius:8px;font-size:11px;color:#f59e0b;">' +
              '⚠️ Full site analysis unavailable — please reload the page to restore evaluation engine</div>' +
              '</div>'
            ).openPopup();
        } catch (err) {
          console.error('[Search Fix] marker fallback failed:', err);
        }
      }
    }

    // ── Evaluate button ─────────────────────────────────────
    searchBtn.addEventListener('click', function() {
      var input = searchInput.value.trim();
      if (!input) return;

      var m = input.match(/^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$/);
      if (m) {
        var lat = parseFloat(m[1]);
        var lng = parseFloat(m[2]);
        if (lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180) {
          doEvaluate(lat, lng, input);
          return;
        }
      }

      var origText = searchBtn.textContent;
      searchBtn.textContent = '⏳ Searching...';
      searchBtn.disabled = true;

      fetch('https://nominatim.openstreetmap.org/search?format=json&q='
            + encodeURIComponent(input) + '&countrycodes=us&limit=5', {
        headers: { 'Accept': 'application/json', 'User-Agent': 'DCHub Site Evaluator (dchub.cloud)' }
      })
      .then(function(r) { if (!r.ok) throw new Error('Geocoding ' + r.status); return r.json(); })
      .then(function(data) {
        searchBtn.textContent = origText;
        searchBtn.disabled = false;
        if (data && data.length > 0) {
          var result = data[0];
          var shortAddr = result.display_name.split(',').slice(0, 3).join(',');
          doEvaluate(parseFloat(result.lat), parseFloat(result.lon), shortAddr);
        } else {
          alert('📍 Address not found.\n\nTry:\n• More specific address (include city, state)\n• Lat,lng format: 39.0438,-77.4874\n• Right-click on map');
        }
      })
      .catch(function(err) {
        searchBtn.textContent = origText;
        searchBtn.disabled = false;
        console.error('[Search Fix] Geocoding error:', err);
        alert('📍 Geocoding error. Try lat,lng format or right-click on map.');
      });
    });

    // Enter key
    searchInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter' && selectedIndex < 0) searchBtn.click();
    });

    // Close dropdown on outside click
    document.addEventListener('click', function(e) {
      if (dropdown && !e.target.closest('.cb-search')) dropdown.classList.remove('show');
    });

    // Wire map right-click if missing
    if (typeof map !== 'undefined') {
      var hasCtx = false;
      try { hasCtx = map._events && map._events.contextmenu && map._events.contextmenu.length > 0; } catch(e) {}
      if (!hasCtx) {
        map.on('contextmenu', function(e) { doEvaluate(e.latlng.lat, e.latlng.lng, null); });
        console.info('[Search Fix] ✅ Right-click evaluate wired');
      }
    }

    console.info('[Search Fix] ✅ Full fallback search active');
    applyFiberFixes();
    addDiagnostics();
  }

  // ── Fiber ID mismatch fixes (always applied) ─────────────
  function applyFiberFixes() {
    if (typeof window.searchFiberAddress === 'function') {
      window.searchFiberAddress = function() {
        var input = document.getElementById('fiber-address-search')
                 || document.getElementById('fiber-address-input');
        if (!input || !input.value.trim()) return;
        var query = input.value.trim();
        fetch('https://nominatim.openstreetmap.org/search?format=json&q='
              + encodeURIComponent(query) + '&limit=1')
          .then(function(r) { return r.json(); })
          .then(function(data) {
            if (data && data.length > 0 && typeof map !== 'undefined') {
              map.setView([parseFloat(data[0].lat), parseFloat(data[0].lon)], 12);
              L.marker([parseFloat(data[0].lat), parseFloat(data[0].lon)]).addTo(map)
                .bindPopup('<b>📍 ' + query + '</b><br>' + data[0].display_name.substring(0, 80))
                .openPopup();
            }
          })
          .catch(function(err) { console.error('[Search Fix] Fiber geocode error:', err); });
      };
    }

    if (typeof window.handleFiberAddressInput === 'function') {
      window.handleFiberAddressInput = function(val) {
        var dd = document.getElementById('fiber-address-dropdown');
        if (!dd) return;
        if (!val || val.length < 3) { dd.style.display = 'none'; return; }
        if (window._fiberAddrTimer) clearTimeout(window._fiberAddrTimer);
        window._fiberAddrTimer = setTimeout(function() {
          fetch('https://nominatim.openstreetmap.org/search?format=json&q='
                + encodeURIComponent(val) + '&limit=5&countrycodes=us')
            .then(function(r) { return r.json(); })
            .then(function(data) {
              if (!data || data.length === 0) { dd.style.display = 'none'; return; }
              var html = '';
              data.forEach(function(item) {
                var safe = item.display_name.replace(/'/g, '').substring(0, 60);
                html += '<div class="fiber-address-item" style="padding:6px 8px;cursor:pointer;border-bottom:1px solid #333;font-size:11px;"'
                     + ' onmouseover="this.style.background=\'#333\'"'
                     + ' onmouseout="this.style.background=\'transparent\'"'
                     + ' onclick="document.getElementById(\'fiber-address-search\').value=\'' + safe + '\';document.getElementById(\'fiber-address-dropdown\').style.display=\'none\'">'
                     + item.display_name.substring(0, 60) + '</div>';
              });
              dd.innerHTML = html;
              dd.style.display = 'block';
            })
            .catch(function() { dd.style.display = 'none'; });
        }, 300);
      };
    }
  }

  // ── Diagnostic logging ────────────────────────────────────
  function addDiagnostics() {
    if (window._lpAppErrors && window._lpAppErrors.length > 0) {
      console.warn('[Search Fix] 🔴 land-power-app.js had ' + window._lpAppErrors.length + ' error(s):');
      window._lpAppErrors.forEach(function(e) {
        console.warn('  Line ' + e.line + ': ' + e.msg);
      });
    }
    console.info('[Search Fix] Status: evaluateSite=' + (typeof window.evaluateSite === 'function' ? '✅' : '❌')
               + ' _doEvaluateSite=' + (typeof window._doEvaluateSite === 'function' ? '✅' : '❌')
               + ' clearSiteMarkers=' + (typeof window.clearSiteMarkers === 'function' ? '✅' : '❌'));
  }

  // ── Start ─────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { setTimeout(init, INIT_DELAY); });
  } else {
    setTimeout(init, INIT_DELAY);
  }

})();
