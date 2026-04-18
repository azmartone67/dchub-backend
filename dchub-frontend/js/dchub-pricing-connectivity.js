/**
 * DC Hub — Energy Pricing & Connectivity Enhancement
 * Adds EIA pricing panel and PeeringDB network overlay to Land & Power map.
 * 
 * Load after land-power-app.js:
 *   <script src="/js/dchub-pricing-connectivity.js?v=1"></script>
 * 
 * Requires: Leaflet, existing DC Hub map instance (window.map)
 */

(function () {
  'use strict';

  const API_BASE = 'https://dchub.cloud';
  const PRICING_CACHE = new Map();
  const CACHE_TTL = 300000; // 5 min

  // ═══════════════════════════════════════════════════════════
  // HELPERS
  // ═══════════════════════════════════════════════════════════

  function cached(key, fetchFn) {
    const entry = PRICING_CACHE.get(key);
    if (entry && Date.now() - entry.ts < CACHE_TTL) return Promise.resolve(entry.data);
    return fetchFn().then(data => {
      PRICING_CACHE.set(key, { data, ts: Date.now() });
      return data;
    });
  }

  async function apiGet(path) {
    try {
      const r = await fetch(API_BASE + path, { credentials: 'include' });
      if (!r.ok) return null;
      const d = await r.json();
      return d.success ? d : null;
    } catch (e) {
      console.warn('[DCHub Pricing]', e.message);
      return null;
    }
  }

  // State lookup from lat/lng (approximate US)
  const STATE_BOUNDS = {
    AZ: [31.3, 37.0, -114.8, -109.0], CA: [32.5, 42.0, -124.4, -114.1],
    TX: [25.8, 36.5, -106.6, -93.5], VA: [36.5, 39.5, -83.7, -75.2],
    GA: [30.4, 35.0, -85.6, -80.8], IL: [36.9, 42.5, -91.5, -87.0],
    NJ: [38.9, 41.4, -75.6, -73.9], NY: [40.5, 45.0, -79.8, -71.8],
    OR: [42.0, 46.3, -124.6, -116.5], WA: [45.5, 49.0, -124.8, -116.9],
    OH: [38.4, 42.3, -84.8, -80.5], PA: [39.7, 42.3, -80.5, -74.7],
    NV: [35.0, 42.0, -120.0, -114.0], NC: [33.8, 36.6, -84.3, -75.5],
    FL: [24.5, 31.0, -87.6, -80.0], CO: [37.0, 41.0, -109.1, -102.0]
  };

  function guessState(lat, lng) {
    for (const [st, [minLat, maxLat, minLng, maxLng]] of Object.entries(STATE_BOUNDS)) {
      if (lat >= minLat && lat <= maxLat && lng >= minLng && lng <= maxLng) return st;
    }
    return '';
  }

  // ═══════════════════════════════════════════════════════════
  // PRICING PANEL
  // ═══════════════════════════════════════════════════════════

  function createPricingPanel() {
    const panel = document.createElement('div');
    panel.id = 'dchub-pricing-panel';
    panel.innerHTML = `
      <div class="dchub-pp-header">
        <span class="dchub-pp-icon">⚡</span>
        <span class="dchub-pp-title">Energy Pricing</span>
        <button class="dchub-pp-close" onclick="this.parentElement.parentElement.style.display='none'">&times;</button>
      </div>
      <div class="dchub-pp-body" id="dchub-pp-body">
        <div class="dchub-pp-placeholder">Click map or run site analysis to load pricing</div>
      </div>
    `;
    panel.style.cssText = `
      position: absolute; bottom: 80px; left: 12px; z-index: 1000;
      width: 320px; background: rgba(9,9,11,0.95); border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px; font-family: 'Instrument Sans', sans-serif; color: #e0e0e0;
      display: none; backdrop-filter: blur(12px); box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    `;
    document.body.appendChild(panel);
    injectStyles();
    return panel;
  }

  function injectStyles() {
    if (document.getElementById('dchub-pricing-css')) return;
    const s = document.createElement('style');
    s.id = 'dchub-pricing-css';
    s.textContent = `
      .dchub-pp-header { display:flex; align-items:center; gap:8px; padding:12px 14px; border-bottom:1px solid rgba(255,255,255,0.06); }
      .dchub-pp-icon { font-size:18px; }
      .dchub-pp-title { font-weight:600; font-size:14px; flex:1; color:#fff; }
      .dchub-pp-close { background:none; border:none; color:#888; font-size:20px; cursor:pointer; padding:0 4px; }
      .dchub-pp-close:hover { color:#fff; }
      .dchub-pp-body { padding:12px 14px; font-size:13px; line-height:1.6; }
      .dchub-pp-placeholder { color:#666; font-style:italic; text-align:center; padding:20px 0; }
      .dchub-pp-row { display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid rgba(255,255,255,0.03); }
      .dchub-pp-label { color:#999; }
      .dchub-pp-value { color:#fff; font-weight:500; font-variant-numeric:tabular-nums; }
      .dchub-pp-value.low { color:#4ade80; }
      .dchub-pp-value.mid { color:#facc15; }
      .dchub-pp-value.high { color:#f87171; }
      .dchub-pp-section { font-weight:600; color:#3478f6; font-size:12px; text-transform:uppercase; letter-spacing:0.5px; margin-top:10px; margin-bottom:4px; }
      .dchub-pp-networks { margin-top:8px; }
      .dchub-pp-net-badge { display:inline-block; background:rgba(52,120,246,0.15); color:#60a5fa; border:1px solid rgba(52,120,246,0.2); border-radius:4px; padding:2px 6px; font-size:11px; margin:2px; }
      .dchub-pp-stat { display:inline-flex; align-items:center; gap:4px; background:rgba(255,255,255,0.04); border-radius:6px; padding:4px 8px; margin:2px; font-size:12px; }
      .dchub-pp-stat b { color:#fff; }
      .dchub-pp-toggle { position:absolute; bottom:80px; left:12px; z-index:999; background:rgba(9,9,11,0.9); border:1px solid rgba(255,255,255,0.08); border-radius:8px; padding:8px 12px; cursor:pointer; color:#e0e0e0; font-size:13px; font-family:'Instrument Sans',sans-serif; display:flex; align-items:center; gap:6px; }
      .dchub-pp-toggle:hover { background:rgba(52,120,246,0.15); border-color:rgba(52,120,246,0.3); }
    `;
    document.head.appendChild(s);
  }

  function priceClass(cents) {
    if (cents < 6) return 'low';
    if (cents < 10) return 'mid';
    return 'high';
  }

  async function loadPricing(lat, lng, state) {
    const st = state || guessState(lat, lng);
    if (!st) return;

    const panel = document.getElementById('dchub-pricing-panel');
    if (!panel) return;
    panel.style.display = 'block';

    const body = document.getElementById('dchub-pp-body');
    body.innerHTML = '<div class="dchub-pp-placeholder">Loading pricing...</div>';

    // Fetch pricing + connectivity in parallel
    const [pricing, connectivity] = await Promise.all([
      cached('pricing-' + st, () => apiGet('/api/v1/energy/site-pricing?state=' + st)),
      cached('conn-' + lat.toFixed(2) + '-' + lng.toFixed(2),
        () => apiGet('/api/v1/connectivity/site-connectivity?lat=' + lat + '&lng=' + lng + '&state=' + st + '&radius=50'))
    ]);

    let html = '';

    // Electricity
    if (pricing && pricing.electricity) {
      const e = pricing.electricity;
      html += '<div class="dchub-pp-section">Electricity — ' + st + '</div>';
      if (e.ind) html += row('Industrial', e.ind.price_cents_kwh + '¢/kWh', priceClass(e.ind.price_cents_kwh));
      if (e.com) html += row('Commercial', e.com.price_cents_kwh + '¢/kWh', priceClass(e.com.price_cents_kwh));
      if (e.res) html += row('Residential', e.res.price_cents_kwh + '¢/kWh', priceClass(e.res.price_cents_kwh));
      if (e.ind) html += row('$/MWh (Industrial)', '$' + e.ind.price_dollars_mwh, priceClass(e.ind.price_cents_kwh));
      if (e.ind && e.ind.period) html += row('Period', e.ind.period, '');
    }

    // Natural Gas
    if (pricing && pricing.natural_gas && Object.keys(pricing.natural_gas).length > 0) {
      html += '<div class="dchub-pp-section">Natural Gas — ' + st + '</div>';
      for (const [sector, data] of Object.entries(pricing.natural_gas)) {
        if (data && data.price_dollars_mcf) {
          html += row(sector.charAt(0).toUpperCase() + sector.slice(1), '$' + data.price_dollars_mcf.toFixed(2) + '/Mcf', '');
        }
      }
    }

    // Gas Storage
    if (pricing && pricing.gas_storage) {
      const gs = pricing.gas_storage;
      html += '<div class="dchub-pp-section">Gas Storage (US)</div>';
      html += row('Working Gas', gs.working_gas_bcf ? gs.working_gas_bcf.toFixed(0) + ' Bcf' : '—', '');
      html += row('Weekly Change', gs.net_change_bcf ? (gs.net_change_bcf > 0 ? '+' : '') + gs.net_change_bcf.toFixed(0) + ' Bcf' : '—',
        gs.net_change_bcf > 0 ? 'low' : 'high');
    }

    // Connectivity
    if (connectivity) {
      html += '<div class="dchub-pp-section">Connectivity (50km)</div>';
      const nets = connectivity.networks || {};
      html += row('Networks', nets.unique_networks || 0, '');
      html += row('Facilities', nets.unique_facilities || 0, '');

      // IX
      const ixes = connectivity.internet_exchanges || [];
      if (ixes.length > 0) {
        html += row('Internet Exchanges', ixes.length, '');
        ixes.slice(0, 3).forEach(ix => {
          html += '<div style="padding-left:12px;color:#999;font-size:12px;">' + ix.name + (ix.participants ? ' (' + ix.participants + ' peers)' : '') + '</div>';
        });
      }

      // Top networks
      if (nets.top_networks && nets.top_networks.length > 0) {
        html += '<div class="dchub-pp-networks">';
        nets.top_networks.slice(0, 8).forEach(n => {
          html += '<span class="dchub-pp-net-badge">' + (n.network || '').substring(0, 20) + '</span>';
        });
        if (nets.top_networks.length > 8) html += '<span class="dchub-pp-net-badge">+' + (nets.top_networks.length - 8) + ' more</span>';
        html += '</div>';
      }

      // Fiber coverage
      const fiber = connectivity.fiber_coverage || [];
      if (fiber.length > 0) {
        html += '<div class="dchub-pp-section">Fiber Coverage</div>';
        fiber.slice(0, 3).forEach(f => {
          html += row(f.county, (f.coverage_pct || 0) + '% / ' + (f.providers || 0) + ' ISPs', '');
        });
      }
    }

    if (!html) {
      html = '<div class="dchub-pp-placeholder">No pricing data for ' + st + '</div>';
    }

    body.innerHTML = html;
  }

  function row(label, value, cls) {
    return '<div class="dchub-pp-row"><span class="dchub-pp-label">' + label + '</span><span class="dchub-pp-value ' + (cls || '') + '">' + value + '</span></div>';
  }

  // ═══════════════════════════════════════════════════════════
  // NETWORK FACILITY MAP LAYER
  // ═══════════════════════════════════════════════════════════

  let networkLayer = null;

  async function loadNetworkFacilities(map, lat, lng, radius) {
    if (!map || !window.L) return;

    // Remove old layer
    if (networkLayer) {
      map.removeLayer(networkLayer);
      networkLayer = null;
    }

    const data = await apiGet('/api/v1/connectivity/networks?lat=' + lat + '&lng=' + lng + '&radius=' + (radius || 50) + '&limit=100');
    if (!data || !data.data || data.data.length === 0) return;

    // Group by facility
    const facilities = {};
    data.data.forEach(d => {
      const key = d.facility_id;
      if (!facilities[key]) {
        facilities[key] = {
          name: d.facility_name,
          lat: d.latitude,
          lng: d.longitude,
          city: d.city,
          networks: []
        };
      }
      if (d.network_name) facilities[key].networks.push(d.network_name);
    });

    networkLayer = L.layerGroup();

    Object.values(facilities).forEach(fac => {
      if (!fac.lat || !fac.lng) return;

      const size = Math.min(12, 5 + fac.networks.length * 0.5);

      const marker = L.circleMarker([fac.lat, fac.lng], {
        radius: size,
        fillColor: '#60a5fa',
        fillOpacity: 0.7,
        color: '#3478f6',
        weight: 1.5
      });

      const netList = fac.networks.slice(0, 10).map(n => '<li>' + n + '</li>').join('');
      const more = fac.networks.length > 10 ? '<li>+' + (fac.networks.length - 10) + ' more</li>' : '';

      marker.bindPopup(
        '<div style="font-family:Instrument Sans,sans-serif;min-width:200px;">' +
        '<b style="color:#3478f6;">' + fac.name + '</b>' +
        (fac.city ? '<br><span style="color:#999;">' + fac.city + '</span>' : '') +
        '<br><b>' + fac.networks.length + ' networks</b>' +
        '<ul style="margin:4px 0;padding-left:16px;font-size:12px;max-height:150px;overflow-y:auto;">' +
        netList + more + '</ul></div>',
        { maxWidth: 300 }
      );

      networkLayer.addLayer(marker);
    });

    networkLayer.addTo(map);
    console.log('[DCHub] Network facilities loaded:', Object.keys(facilities).length, 'facilities');
  }

  // ═══════════════════════════════════════════════════════════
  // TOGGLE BUTTON
  // ═══════════════════════════════════════════════════════════

  function createToggleButton() {
    const btn = document.createElement('div');
    btn.className = 'dchub-pp-toggle';
    btn.innerHTML = '⚡ Pricing';
    btn.title = 'Toggle Energy Pricing Panel';
    btn.onclick = function () {
      const panel = document.getElementById('dchub-pricing-panel');
      if (!panel) return;
      if (panel.style.display === 'none' || !panel.style.display) {
        panel.style.display = 'block';
        // If we have a last-known location, refresh
        if (window._dchubLastSiteLatLng) {
          const { lat, lng, state } = window._dchubLastSiteLatLng;
          loadPricing(lat, lng, state);
        }
      } else {
        panel.style.display = 'none';
      }
    };
    // Position below the existing sidebar controls
    btn.style.bottom = '40px';
    document.body.appendChild(btn);
  }

  // ═══════════════════════════════════════════════════════════
  // INTEGRATION HOOKS
  // ═══════════════════════════════════════════════════════════

  // Hook into existing site analysis
  function hookSiteAnalysis() {
    // Override or wrap the existing site analysis function
    const origSiteAnalysis = window.runSiteAnalysis || window.evaluateSite;

    if (origSiteAnalysis) {
      const wrapper = function () {
        const result = origSiteAnalysis.apply(this, arguments);

        // Extract lat/lng from arguments or from the map
        let lat, lng, state;
        if (arguments[0] && typeof arguments[0] === 'object') {
          lat = arguments[0].lat || arguments[0].latitude;
          lng = arguments[0].lng || arguments[0].longitude;
          state = arguments[0].state;
        } else if (arguments.length >= 2) {
          lat = arguments[0];
          lng = arguments[1];
          state = arguments[2];
        }

        if (lat && lng) {
          window._dchubLastSiteLatLng = { lat, lng, state: state || guessState(lat, lng) };
          loadPricing(lat, lng, state || guessState(lat, lng));
          if (window.map) loadNetworkFacilities(window.map, lat, lng, 50);
        }

        return result;
      };

      if (window.runSiteAnalysis) window.runSiteAnalysis = wrapper;
      if (window.evaluateSite) window.evaluateSite = wrapper;
    }

    // Also hook right-click site evaluation
    if (window.map) {
      window.map.on('contextmenu', function (e) {
        const lat = e.latlng.lat;
        const lng = e.latlng.lng;
        const state = guessState(lat, lng);
        window._dchubLastSiteLatLng = { lat, lng, state };

        // Delay to let existing popup render first
        setTimeout(() => {
          loadPricing(lat, lng, state);
          loadNetworkFacilities(window.map, lat, lng, 50);
        }, 500);
      });
    }
  }

  // ═══════════════════════════════════════════════════════════
  // INIT
  // ═══════════════════════════════════════════════════════════

  function init() {
    console.log('[DCHub] Pricing & Connectivity Enhancement v1.0');
    createPricingPanel();
    createToggleButton();
    hookSiteAnalysis();
    console.log('[DCHub] ⚡ Pricing panel ready');
    console.log('[DCHub] 🌐 Network overlay ready (loads on site analysis)');
  }

  // Wait for map to be ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(init, 2000));
  } else {
    setTimeout(init, 2000);
  }

  // Export for manual use
  window.DCHubPricing = {
    loadPricing,
    loadNetworkFacilities,
    guessState
  };

})();
