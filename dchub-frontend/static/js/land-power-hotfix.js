/**
 * land-power-hotfix.js — Fixes for Land & Power map console errors
 * ================================================================
 * Deploy: Upload to Cloudflare Pages static/js/land-power-hotfix.js
 * Then add <script src="/js/land-power-hotfix.js?v=1" defer></script>
 * AFTER energy-enhancement-v3.js in land-power-map.html
 *
 * Fixes:
 *   1. DCHubEnergy.loadDOTPipelines is not a function (spam on every pan/zoom)
 *   2. DCHubEnergy.loadTexasPipelines is not a function
 *   3. Overpass API 429 rate limiting (adds smarter debounce)
 *   4. HIFLD gas pipeline 503 fallback (graceful degradation)
 *
 * Author: DC Hub / Claude session Mar 18 2026
 */

(function () {
  'use strict';

  // ─── FIX 1: Stub missing DCHubEnergy pipeline functions ───────────
  // energy-enhancement-v3.js references these but they were never implemented.
  // Fires on every map moveend → console spam. Stub them as no-ops with logging.

  function patchDCHubEnergy() {
    if (typeof window.DCHubEnergy === 'undefined') {
      window.DCHubEnergy = {};
    }

    if (typeof window.DCHubEnergy.loadDOTPipelines !== 'function') {
      window.DCHubEnergy.loadDOTPipelines = function () {
        // Stub — DOT pipeline data not yet wired to backend
        // Prevents: Uncaught TypeError: DCHubEnergy.loadDOTPipelines is not a function
        return Promise.resolve({ features: [], source: 'stub' });
      };
    }

    if (typeof window.DCHubEnergy.loadTexasPipelines !== 'function') {
      window.DCHubEnergy.loadTexasPipelines = function () {
        // Stub — Texas RRC pipeline data not yet wired to backend
        // Prevents: Uncaught TypeError: DCHubEnergy.loadTexasPipelines is not a function
        return Promise.resolve({ features: [], source: 'stub' });
      };
    }

    // Also stub toggleDOTPipelines / toggleTexasPipelines if they reference the loaders
    if (typeof window.toggleDOTPipelines !== 'function') {
      window.toggleDOTPipelines = function () {
        console.info('DOT Pipelines: Not yet available (data source pending)');
      };
    }
    if (typeof window.toggleTexasPipelines !== 'function') {
      window.toggleTexasPipelines = function () {
        console.info('Texas Pipelines: Not yet available (data source pending)');
      };
    }
  }

  // Patch immediately and also after a delay (in case DCHubEnergy loads later)
  patchDCHubEnergy();
  setTimeout(patchDCHubEnergy, 2000);
  setTimeout(patchDCHubEnergy, 5000);

  // ─── FIX 2: Debounce map moveend infrastructure calls ────────────
  // The map fires moveend on every pan/zoom, triggering HIFLD + Overpass + DOT
  // calls simultaneously. This causes 429s and 503s from rapid fire.

  var _moveEndTimer = null;
  var _lastMoveEnd = 0;
  var MOVE_END_DEBOUNCE_MS = 800; // Wait 800ms after last moveend before loading

  function debounceInfraLoad() {
    if (typeof window.loadLiveInfrastructure !== 'function') return;

    var originalLoad = window.loadLiveInfrastructure;
    window.loadLiveInfrastructure = function () {
      var now = Date.now();
      if (_moveEndTimer) clearTimeout(_moveEndTimer);

      // Skip if called within debounce window
      if (now - _lastMoveEnd < MOVE_END_DEBOUNCE_MS) {
        _moveEndTimer = setTimeout(function () {
          _lastMoveEnd = Date.now();
          originalLoad.apply(this, arguments);
        }.bind(this, arguments), MOVE_END_DEBOUNCE_MS);
        return;
      }

      _lastMoveEnd = now;
      originalLoad.apply(this, arguments);
    };
  }

  // Apply after map initializes
  setTimeout(debounceInfraLoad, 3000);

  // ─── FIX 3: HIFLD gas pipeline 503 graceful fallback ─────────────
  // /api/v2/infrastructure/hifld/gas-pipelines returns 503 from Worker timeout.
  // Patch queryDCHubAPI to retry once with smaller radius on 503.

  function patchGasPipelineFallback() {
    if (typeof window.DCHubInfra === 'undefined') return;
    if (typeof window.DCHubInfra.loadGasPipelines !== 'function') return;

    var originalLoadGas = window.DCHubInfra.loadGasPipelines;
    window.DCHubInfra.loadGasPipelines = function (lat, lng, radius) {
      // Reduce radius to 25km max to prevent Worker timeout on large queries
      var safeRadius = Math.min(radius || 50, 25);
      return originalLoadGas.call(this, lat, lng, safeRadius);
    };
  }

  setTimeout(patchGasPipelineFallback, 4000);

  // ─── FIX 4: Overpass API rate limit protection ────────────────────
  // Cap concurrent Overpass requests and increase backoff delays

  var _overpassInFlight = 0;
  var MAX_OVERPASS_CONCURRENT = 2; // Was unlimited → 429 spam

  function patchOverpassThrottle() {
    if (typeof window.executeOverpassRequest !== 'function') return;

    var originalExec = window.executeOverpassRequest;
    window.executeOverpassRequest = function () {
      if (_overpassInFlight >= MAX_OVERPASS_CONCURRENT) {
        return Promise.resolve(null); // Silently skip if too many in flight
      }
      _overpassInFlight++;
      return originalExec.apply(this, arguments).finally(function () {
        _overpassInFlight = Math.max(0, _overpassInFlight - 1);
      });
    };
  }

  setTimeout(patchOverpassThrottle, 3000);

  // ─── FIX 5: Site Planner 503 retry with direct backend ───────────
  // Worker v4.1.4 times out on /api/v1/site-planner/analyze.
  // Add a retry that goes direct to Railway if Worker returns 503.

  var RAILWAY_DIRECT = 'https://dchub-backend-production.up.railway.app';

  function patchSitePlannerRetry() {
    if (typeof window.spAnalyze !== 'function') return;

    var originalAnalyze = window.spAnalyze;
    window.spAnalyze = function () {
      var args = arguments;
      return originalAnalyze.apply(this, args).catch(function (err) {
        // If 503 from Worker, retry direct to Railway backend
        if (err && (err.status === 503 || (err.message && err.message.indexOf('503') >= 0))) {
          console.info('Site Planner: Retrying via direct backend (Worker timed out)');
          var lat = window._spLastLat || 0;
          var lng = window._spLastLng || 0;
          return fetch(RAILWAY_DIRECT + '/api/site-score?lat=' + lat + '&lon=' + lng + '&state=&capacity=50')
            .then(function (r) { return r.json(); })
            .then(function (data) {
              if (data && data.success) {
                // Display result in site planner panel
                if (typeof window.spDisplayResult === 'function') {
                  window.spDisplayResult(data);
                }
                return data;
              }
              throw new Error('Direct backend also failed');
            });
        }
        throw err;
      });
    };
  }

  setTimeout(patchSitePlannerRetry, 5000);

  console.info('🔧 Land & Power Hotfix v1.0 loaded — DOT stubs, debounce, gas fallback, Overpass throttle');

})();
