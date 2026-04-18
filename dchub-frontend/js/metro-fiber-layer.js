/**
 * DC Hub Metro Dark Fiber Layer v1.0
 * ═══════════════════════════════════════════════════════════════
 * Renders metro dark fiber intelligence on the Land & Power map.
 * Data source: /api/v1/fiber/metro (19 markets, 12 carriers, 67K route miles)
 *
 * Features:
 *   - Proportional circles per market sized by route miles
 *   - Color-coded by fiber density score (green=high, amber=medium, red=low)
 *   - Click popup shows carrier count, on-net buildings, route miles, tier
 *   - Legend with tier breakdown
 *   - Integrates with existing layer toggle system (data-layer="metrofiber")
 *
 * Usage: Include via <script src="js/metro-fiber-layer.js?v=1"></script>
 *        after land-power-app.js (needs `map` global)
 * ═══════════════════════════════════════════════════════════════
 */
(function () {
  'use strict';

  // ── Market center coordinates (approximate metro centroids) ──
  var MARKET_COORDS = {
    'Northern Virginia':  { lat: 38.95, lng: -77.45 },
    'Dallas-Fort Worth':  { lat: 32.80, lng: -96.80 },
    'Chicago':            { lat: 41.88, lng: -87.63 },
    'New York Metro':     { lat: 40.75, lng: -74.00 },
    'Phoenix':            { lat: 33.45, lng: -112.07 },
    'Silicon Valley':     { lat: 37.39, lng: -122.04 },
    'Los Angeles':        { lat: 34.05, lng: -118.24 },
    'Atlanta':            { lat: 33.75, lng: -84.39 },
    'Columbus':           { lat: 39.96, lng: -82.99 },
    'Boston':             { lat: 42.36, lng: -71.06 },
    'Houston':            { lat: 29.76, lng: -95.37 },
    'Denver':             { lat: 39.74, lng: -104.99 },
    'Seattle':            { lat: 47.61, lng: -122.33 },
    'Miami':              { lat: 25.76, lng: -80.19 },
    'Portland':           { lat: 45.52, lng: -122.68 },
    'Charlotte':          { lat: 35.23, lng: -80.84 },
    'Salt Lake City':     { lat: 40.76, lng: -111.89 },
    'San Antonio':        { lat: 29.42, lng: -98.49 },
    'Richmond':           { lat: 37.54, lng: -77.44 },
  };

  // ── State ──
  var metroFiberGroup = null;
  var metroFiberData = null;
  var metroFiberLoaded = false;
  var metroFiberVisible = false;

  // ── Color scale based on fiber density score ──
  function densityColor(score) {
    if (score >= 90) return '#10b981'; // green — Tier 1 saturated
    if (score >= 70) return '#22c55e'; // light green
    if (score >= 50) return '#f59e0b'; // amber
    if (score >= 30) return '#f97316'; // orange
    return '#ef4444';                  // red — low density
  }

  function tierBadgeColor(tier) {
    if (tier === 'Tier 1') return 'background:rgba(16,185,129,0.2);color:#10b981;';
    if (tier === 'Tier 2') return 'background:rgba(59,130,246,0.2);color:#3b82f6;';
    return 'background:rgba(168,85,247,0.2);color:#a855f7;';
  }

  // ── Radius scale: route_miles → pixel radius ──
  function circleRadius(routeMiles) {
    // Scale: 475 mi (Richmond) → 12px, 17150 mi (NYC) → 45px
    var min = 12, max = 45;
    var minMi = 400, maxMi = 18000;
    var clamped = Math.max(minMi, Math.min(maxMi, routeMiles));
    var ratio = (clamped - minMi) / (maxMi - minMi);
    return min + ratio * (max - min);
  }

  // ── Format number with commas ──
  function fmt(n) {
    return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  // ── Build popup HTML for a market ──
  function buildPopup(m) {
    var color = densityColor(m.fiber_density_score);
    var tierStyle = tierBadgeColor(m.tier);

    return '<div style="font-family:Inter,system-ui,sans-serif;min-width:280px;max-width:320px;">' +
      // Header
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #333;">' +
        '<div style="width:36px;height:36px;border-radius:50%;background:' + color + ';display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:800;color:#000;">' + m.fiber_density_score + '</div>' +
        '<div>' +
          '<div style="font-size:14px;font-weight:700;color:#fff;">' + m.market + '</div>' +
          '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;' + tierStyle + '">' + m.tier + '</span>' +
          ' <span style="font-size:10px;color:#9ca3af;">' + m.state + '</span>' +
        '</div>' +
      '</div>' +
      // Stats grid
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:10px;">' +
        '<div style="background:#1a1a2e;padding:8px 10px;border-radius:6px;">' +
          '<div style="font-size:9px;color:#9ca3af;text-transform:uppercase;">Route Miles</div>' +
          '<div style="font-size:15px;font-weight:700;color:#f59e0b;font-family:JetBrains Mono,monospace;">' + fmt(m.total_route_miles) + '</div>' +
        '</div>' +
        '<div style="background:#1a1a2e;padding:8px 10px;border-radius:6px;">' +
          '<div style="font-size:9px;color:#9ca3af;text-transform:uppercase;">Carriers</div>' +
          '<div style="font-size:15px;font-weight:700;color:#3b82f6;font-family:JetBrains Mono,monospace;">' + m.total_carriers + '</div>' +
        '</div>' +
        '<div style="background:#1a1a2e;padding:8px 10px;border-radius:6px;">' +
          '<div style="font-size:9px;color:#9ca3af;text-transform:uppercase;">On-Net Buildings</div>' +
          '<div style="font-size:15px;font-weight:700;color:#10b981;font-family:JetBrains Mono,monospace;">' + fmt(m.total_on_net_buildings) + '</div>' +
        '</div>' +
        '<div style="background:#1a1a2e;padding:8px 10px;border-radius:6px;">' +
          '<div style="font-size:9px;color:#9ca3af;text-transform:uppercase;">Density Score</div>' +
          '<div style="font-size:15px;font-weight:700;color:' + color + ';font-family:JetBrains Mono,monospace;">' + m.fiber_density_score + '/100</div>' +
        '</div>' +
      '</div>' +
      // Density bar
      '<div style="background:#1a1a2e;border-radius:6px;padding:6px 10px;margin-bottom:8px;">' +
        '<div style="font-size:9px;color:#9ca3af;margin-bottom:4px;">FIBER DENSITY</div>' +
        '<div style="height:6px;background:#333;border-radius:3px;overflow:hidden;">' +
          '<div style="height:100%;width:' + m.fiber_density_score + '%;background:linear-gradient(90deg,' + color + ',#f59e0b);border-radius:3px;transition:width .3s;"></div>' +
        '</div>' +
      '</div>' +
      // Source
      '<div style="font-size:9px;color:#555;text-align:center;">DC Hub Metro Dark Fiber Intelligence · dchub.cloud</div>' +
    '</div>';
  }

  // ── Load and render ──
  function loadMetroFiber() {
    if (metroFiberLoaded && metroFiberData) {
      showMetroFiber();
      return;
    }

    var countEl = document.getElementById('count-metrofiber');
    if (countEl) countEl.textContent = '...';

    fetch('https://dchub.cloud/api/v1/fiber/metro')
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (!data.success || !data.markets) {
          console.error('[DC Hub] Metro fiber API error:', data);
          if (countEl) countEl.textContent = 'ERR';
          return;
        }

        metroFiberData = data;
        metroFiberLoaded = true;

        if (countEl) countEl.textContent = data.total_markets;

        renderMetroFiber();
      })
      .catch(function (err) {
        console.error('[DC Hub] Metro fiber fetch failed:', err);
        if (countEl) countEl.textContent = 'ERR';
      });
  }

  function renderMetroFiber() {
    if (!metroFiberData || typeof L === 'undefined' || typeof map === 'undefined') return;

    // Clean up existing
    if (metroFiberGroup) {
      map.removeLayer(metroFiberGroup);
    }
    metroFiberGroup = L.layerGroup();

    metroFiberData.markets.forEach(function (m) {
      var coords = MARKET_COORDS[m.market];
      if (!coords) {
        console.warn('[DC Hub] No coords for metro fiber market:', m.market);
        return;
      }

      var color = densityColor(m.fiber_density_score);
      var radius = circleRadius(m.total_route_miles);

      // Outer glow circle
      var glow = L.circleMarker([coords.lat, coords.lng], {
        radius: radius + 6,
        color: color,
        fillColor: color,
        fillOpacity: 0.08,
        weight: 0,
        interactive: false,
      });

      // Main circle
      var circle = L.circleMarker([coords.lat, coords.lng], {
        radius: radius,
        color: color,
        fillColor: color,
        fillOpacity: 0.25,
        weight: 2,
        opacity: 0.9,
      });

      // Label
      var label = L.marker([coords.lat, coords.lng], {
        icon: L.divIcon({
          className: 'metro-fiber-label',
          html: '<div style="' +
            'font-family:JetBrains Mono,monospace;' +
            'font-size:10px;font-weight:700;' +
            'color:' + color + ';' +
            'text-align:center;' +
            'text-shadow:0 0 4px rgba(0,0,0,0.8),0 0 8px rgba(0,0,0,0.6);' +
            'white-space:nowrap;pointer-events:none;' +
          '">' + m.fiber_density_score + '</div>',
          iconSize: [40, 16],
          iconAnchor: [20, 8],
        }),
        interactive: false,
      });

      // Popup
      circle.bindPopup(buildPopup(m), {
        maxWidth: 340,
        className: 'metro-fiber-popup',
      });

      // Hover effect
      circle.on('mouseover', function () {
        this.setStyle({ fillOpacity: 0.45, weight: 3 });
      });
      circle.on('mouseout', function () {
        this.setStyle({ fillOpacity: 0.25, weight: 2 });
      });

      metroFiberGroup.addLayer(glow);
      metroFiberGroup.addLayer(circle);
      metroFiberGroup.addLayer(label);
    });

    // Add summary legend
    var legend = L.control({ position: 'bottomright' });
    legend.onAdd = function () {
      var div = L.DomUtil.create('div', 'metro-fiber-legend');
      div.innerHTML =
        '<div style="background:rgba(10,10,18,0.92);border:1px solid #333;border-radius:8px;padding:10px 12px;font-family:Inter,system-ui,sans-serif;min-width:160px;backdrop-filter:blur(8px);">' +
          '<div style="font-size:11px;font-weight:700;color:#f59e0b;margin-bottom:6px;display:flex;align-items:center;gap:6px;">📡 Metro Dark Fiber</div>' +
          '<div style="font-size:10px;color:#9ca3af;margin-bottom:6px;">' +
            fmt(metroFiberData.total_route_miles) + ' route miles · ' +
            metroFiberData.total_markets + ' markets' +
          '</div>' +
          '<div style="display:flex;flex-direction:column;gap:3px;">' +
            '<div style="display:flex;align-items:center;gap:6px;font-size:10px;">' +
              '<span style="width:10px;height:10px;border-radius:50%;background:#10b981;display:inline-block;"></span>' +
              '<span style="color:#ccc;">90+ Tier 1 Dense</span>' +
            '</div>' +
            '<div style="display:flex;align-items:center;gap:6px;font-size:10px;">' +
              '<span style="width:10px;height:10px;border-radius:50%;background:#f59e0b;display:inline-block;"></span>' +
              '<span style="color:#ccc;">50-89 Emerging</span>' +
            '</div>' +
            '<div style="display:flex;align-items:center;gap:6px;font-size:10px;">' +
              '<span style="width:10px;height:10px;border-radius:50%;background:#ef4444;display:inline-block;"></span>' +
              '<span style="color:#ccc;">&lt;50 Developing</span>' +
            '</div>' +
          '</div>' +
        '</div>';
      return div;
    };
    legend._metroFiber = true;

    metroFiberGroup.addLayer = (function (origAdd) {
      return function (layer) { return origAdd.call(metroFiberGroup, layer); };
    })(metroFiberGroup.addLayer);

    // Store legend ref for cleanup
    metroFiberGroup._legend = legend;

    showMetroFiber();
    console.log('[DC Hub] Metro fiber layer loaded:', metroFiberData.total_markets, 'markets,', fmt(metroFiberData.total_route_miles), 'route miles');
  }

  function showMetroFiber() {
    if (!metroFiberGroup || typeof map === 'undefined') return;
    metroFiberGroup.addTo(map);
    if (metroFiberGroup._legend) {
      metroFiberGroup._legend.addTo(map);
    }
    metroFiberVisible = true;
  }

  function hideMetroFiber() {
    if (!metroFiberGroup || typeof map === 'undefined') return;
    map.removeLayer(metroFiberGroup);
    if (metroFiberGroup._legend) {
      map.removeControl(metroFiberGroup._legend);
    }
    metroFiberVisible = false;
  }

  function toggleMetroFiber() {
    if (metroFiberVisible) {
      hideMetroFiber();
    } else {
      loadMetroFiber();
    }
  }

  // ── Wire into existing layer toggle system ──
  // The land-power-app.js dispatches click events on .layer-btn[data-layer]
  // We intercept clicks on the metrofiber button
  function wireLayerToggle() {
    var btn = document.querySelector('.layer-btn[data-layer="metrofiber"]');
    if (!btn) {
      console.warn('[DC Hub] Metro fiber button not found');
      return;
    }

    btn.addEventListener('click', function (e) {
      // Toggle active class (land-power-app.js may also do this)
      if (!btn.classList.contains('active')) {
        btn.classList.add('active');
        loadMetroFiber();
      } else {
        btn.classList.remove('active');
        hideMetroFiber();
      }
    });
  }

  // ── Expose globals for integration with fiber panel ──
  window.loadMetroFiber = loadMetroFiber;
  window.hideMetroFiber = hideMetroFiber;
  window.toggleMetroFiber = toggleMetroFiber;
  window.metroFiberLayerReady = true;

  // ── Init: wait for map to be ready ──
  function init() {
    if (typeof map !== 'undefined' && typeof L !== 'undefined') {
      wireLayerToggle();
      console.log('[DC Hub] Metro fiber layer v1.0 ready');
    } else {
      setTimeout(init, 500);
    }
  }

  // Start after DOM + map init
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(init, 1000); });
  } else {
    setTimeout(init, 1000);
  }
})();
