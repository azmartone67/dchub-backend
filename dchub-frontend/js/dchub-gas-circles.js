/**
 * DC Hub — Gas Pipeline Circle Markers v2.0
 * Restores the orange circle markers for gas pipelines from DC Hub Neon DB.
 * Uses /api/v1/gas-pipelines endpoint.
 * 
 * Load after land-power-app.js:
 *   <script src="/js/dchub-gas-circles.js?v=2"></script>
 */

(function () {
  'use strict';

  var gasCircleLayer = null;
  var gasCircleCache = {};
  var CACHE_TTL = 120000; // 2 min

  function getCacheKey(bounds, z) {
    var b = bounds;
    return z + ':' + b.getSouth().toFixed(2) + ',' + b.getWest().toFixed(2) + ',' +
           b.getNorth().toFixed(2) + ',' + b.getEast().toFixed(2);
  }

  function loadDCHubGasPipelineCircles() {
    if (!window.map || !window.L) return;
    var map = window.map;
    var zoom = map.getZoom();

    // Only load at zoom 7+
    if (zoom < 7) {
      if (gasCircleLayer) {
        map.removeLayer(gasCircleLayer);
        gasCircleLayer = null;
      }
      return;
    }

    var bounds = map.getBounds();
    var key = getCacheKey(bounds, zoom);

    // Check cache
    if (gasCircleCache[key] && Date.now() - gasCircleCache[key].ts < CACHE_TTL) {
      renderCircles(gasCircleCache[key].data);
      return;
    }

    var lat = bounds.getCenter().lat;
    var lng = bounds.getCenter().lng;
    // Radius based on zoom: wider at lower zoom
    var radius = zoom <= 8 ? 100 : zoom <= 10 ? 50 : 30;

    var url = 'https://dchub.cloud/api/v1/gas-pipelines?lat=' + lat + '&lng=' + lng +
              '&radius=' + radius + '&limit=500';

    fetch(url, { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.data && d.data.length > 0) {
          gasCircleCache[key] = { data: d.data, ts: Date.now() };
          renderCircles(d.data);
          console.log('🟠 Gas circles: ' + d.data.length + ' from DC Hub Neon');
        }
      })
      .catch(function (e) {
        console.warn('[Gas Circles] Error:', e.message);
      });
  }

  function renderCircles(pipelines) {
    if (!window.map || !window.L) return;
    var map = window.map;

    // Remove old layer
    if (gasCircleLayer) {
      map.removeLayer(gasCircleLayer);
    }

    gasCircleLayer = L.layerGroup();

    pipelines.forEach(function (p) {
      var lat = p.latitude || p.lat;
      var lng = p.longitude || p.lng || p.lon;
      if (!lat || !lng) return;

      var name = p.name || p.pipeline_name || 'Gas Pipeline';
      var operator = p.operator || '';
      var diameter = p.diameter_inches || p.diameter || '';
      var capacity = p.capacity_mdth || '';
      var states = p.states_served || p.state || '';

      var marker = L.circleMarker([lat, lng], {
        radius: 5,
        fillColor: '#ff8c00',
        fillOpacity: 0.75,
        color: '#cc6600',
        weight: 1.5
      });

      var popup = '<div style="font-family:Instrument Sans,sans-serif;min-width:180px;">' +
        '<b style="color:#ff8c00;">🔥 ' + name + '</b>';
      if (operator) popup += '<br><span style="color:#ccc;">Operator:</span> ' + operator;
      if (diameter) popup += '<br><span style="color:#ccc;">Diameter:</span> ' + diameter + '"';
      if (capacity) popup += '<br><span style="color:#ccc;">Capacity:</span> ' + capacity + ' MDth/d';
      if (states) popup += '<br><span style="color:#ccc;">States:</span> ' + states;
      popup += '</div>';

      marker.bindPopup(popup, { maxWidth: 280 });
      gasCircleLayer.addLayer(marker);
    });

    gasCircleLayer.addTo(map);
  }

  // Hook into map movement
  function init() {
    if (!window.map) {
      setTimeout(init, 2000);
      return;
    }

    console.log('🟠 Gas Pipeline Circles v2.0 — loading from DC Hub Neon');

    // Load on zoom/move
    window.map.on('moveend', function () {
      loadDCHubGasPipelineCircles();
    });

    // Initial load
    loadDCHubGasPipelineCircles();
  }

  // Export globally so other scripts can call it
  window.loadDCHubGasPipelineCircles = loadDCHubGasPipelineCircles;

  // Wait for DOM + map
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(init, 3000); });
  } else {
    setTimeout(init, 3000);
  }

})();
