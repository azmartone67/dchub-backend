// ============================================================================
// DC Hub - Capacity Headroom Heatmap Layer v1.0
// Integrates with land-power-app.js layer system
// Shows market readiness scores as color-coded zones on the map
// ============================================================================

(function() {
    'use strict';

    // Wait for map to be ready
    var initAttempts = 0;
    var initInterval = setInterval(function() {
        initAttempts++;
        if (typeof window.map !== 'undefined' && typeof L !== 'undefined') {
            clearInterval(initInterval);
            initCapacityHeatmap();
        } else if (initAttempts > 50) {
            clearInterval(initInterval);
            console.warn('⚡ Capacity Heatmap: Map not found after 10s');
        }
    }, 200);

    function initCapacityHeatmap() {
        console.log('🔥 Capacity Heatmap Layer: Initializing...');

        // Use the layer group already defined in land-power-app.js layers object
        var heatmapLayer = window.layers ? window.layers.capacityHeatmap : L.layerGroup();
        
        // If layers wasn't ready yet, register it
        if (window.layers && !window.layers.capacityHeatmap) {
            window.layers.capacityHeatmap = heatmapLayer;
        }

        // ============================================
        // MARKET DATA (from Capacity Headroom API)
        // Updated by fetchHeadroomData() every 30 min
        // ============================================
        var BACKEND_URL = 'https://dchub.cloud';
        
        // Fallback data from latest API response (always available even if API is down)
        var marketData = [
            {
                market: 'phoenix', name: 'Phoenix, AZ', iso: 'CAISO',
                lat: 33.4484, lng: -112.0740,
                readiness: { score: 74.4, grade: 'B', label: 'Good Capacity' },
                grid: { spare_capacity_pct: 75.8, spare_capacity_mw: 64461, signal: 'green' },
                gas: { pipeline_count: 1, headroom_mdth: 2184, signal: 'green' },
                power: { local_plants: 189, local_capacity_mw: 48962 },
                fiber: { route_count: 2 },
                cost: { electricity_rate_cents_kwh: 7.52 }
            },
            {
                market: 'dallas', name: 'Dallas, TX', iso: 'ERCOT',
                lat: 32.7767, lng: -96.7970,
                readiness: { score: 71.3, grade: 'B', label: 'Good Capacity' },
                grid: { spare_capacity_pct: 70.1, spare_capacity_mw: 98165, signal: 'green' },
                gas: { pipeline_count: 8, headroom_mdth: 18340, signal: 'yellow' },
                power: { local_plants: 994, local_capacity_mw: 239019 },
                fiber: { route_count: 2 },
                cost: { electricity_rate_cents_kwh: 6.63 }
            },
            {
                market: 'northern_virginia', name: 'Northern Virginia', iso: 'PJM',
                lat: 39.0438, lng: -77.4874,
                readiness: { score: 61.8, grade: 'C', label: 'Moderate Capacity' },
                grid: { spare_capacity_pct: 35.2, spare_capacity_mw: 66953, signal: 'green' },
                gas: { pipeline_count: 3, headroom_mdth: 4550, signal: 'green' },
                power: { local_plants: 241, local_capacity_mw: 37057 },
                fiber: { route_count: 2 },
                cost: { electricity_rate_cents_kwh: 9.92 }
            },
            {
                market: 'atlanta', name: 'Atlanta, GA', iso: 'MISO',
                lat: 33.7490, lng: -84.3880,
                readiness: { score: 77.8, grade: 'B', label: 'Good Capacity' },
                grid: { spare_capacity_pct: 62.3, spare_capacity_mw: 121432, signal: 'green' },
                gas: { pipeline_count: 1, headroom_mdth: 1008, signal: 'green' },
                power: { local_plants: 251, local_capacity_mw: 52155 },
                fiber: { route_count: 2 },
                cost: { electricity_rate_cents_kwh: 6.83 }
            },
            {
                market: 'las_vegas', name: 'Las Vegas, NV', iso: 'CAISO',
                lat: 36.1699, lng: -115.1398,
                readiness: { score: 77.3, grade: 'B', label: 'Good Capacity' },
                grid: { spare_capacity_pct: 75.8, spare_capacity_mw: 64461, signal: 'green' },
                gas: { pipeline_count: 2, headroom_mdth: 1710, signal: 'green' },
                power: { local_plants: 119, local_capacity_mw: 20213 },
                fiber: { route_count: 2 },
                cost: { electricity_rate_cents_kwh: 7.59 }
            },
            {
                market: 'salt_lake_city', name: 'Salt Lake City, UT', iso: 'CAISO',
                lat: 40.7608, lng: -111.8910,
                readiness: { score: 79.3, grade: 'B', label: 'Good Capacity' },
                grid: { spare_capacity_pct: 72.0, spare_capacity_mw: 58000, signal: 'green' },
                gas: { pipeline_count: 2, headroom_mdth: 1400, signal: 'green' },
                power: { local_plants: 125, local_capacity_mw: 18500 },
                fiber: { route_count: 2 },
                cost: { electricity_rate_cents_kwh: 6.10 }
            },
            {
                market: 'columbus', name: 'Columbus, OH', iso: 'PJM',
                lat: 39.9612, lng: -82.9988,
                readiness: { score: 64.4, grade: 'C', label: 'Moderate Capacity' },
                grid: { spare_capacity_pct: 35.2, spare_capacity_mw: 66953, signal: 'green' },
                gas: { pipeline_count: 1, headroom_mdth: 555, signal: 'green' },
                power: { local_plants: 196, local_capacity_mw: 38633 },
                fiber: { route_count: 2 },
                cost: { electricity_rate_cents_kwh: 9.33 }
            },
            {
                market: 'des_moines', name: 'Des Moines, IA', iso: 'MISO',
                lat: 41.5868, lng: -93.6250,
                readiness: { score: 82.0, grade: 'B', label: 'Good Capacity' },
                grid: { spare_capacity_pct: 62.3, spare_capacity_mw: 121432, signal: 'green' },
                gas: { pipeline_count: 1, headroom_mdth: 720, signal: 'green' },
                power: { local_plants: 259, local_capacity_mw: 37701 },
                fiber: { route_count: 2 },
                cost: { electricity_rate_cents_kwh: 6.36 }
            }
        ];

        // ============================================
        // COLOR SCALE - Score to Color Mapping
        // ============================================
        function scoreToColor(score) {
            if (score >= 80) return { fill: '#10b981', border: '#059669', text: '#ecfdf5', glow: 'rgba(16,185,129,0.35)' };  // Green - Excellent
            if (score >= 70) return { fill: '#3b82f6', border: '#2563eb', text: '#eff6ff', glow: 'rgba(59,130,246,0.30)' };  // Blue - Good
            if (score >= 60) return { fill: '#f59e0b', border: '#d97706', text: '#fffbeb', glow: 'rgba(245,158,11,0.30)' };  // Amber - Moderate
            return { fill: '#ef4444', border: '#dc2626', text: '#fef2f2', glow: 'rgba(239,68,68,0.30)' };                    // Red - Tight
        }

        function signalToIcon(signal) {
            if (signal === 'green') return '🟢';
            if (signal === 'yellow') return '🟡';
            return '🔴';
        }

        // ============================================
        // RENDER HEATMAP ZONES
        // ============================================
        function renderHeatmap() {
            heatmapLayer.clearLayers();

            marketData.forEach(function(m) {
                var colors = scoreToColor(m.readiness.score);
                var radiusKm = 80; // ~50 miles

                // Gradient zone circle
                var zone = L.circle([m.lat, m.lng], {
                    radius: radiusKm * 1000,
                    fillColor: colors.fill,
                    fillOpacity: 0.12,
                    color: colors.border,
                    weight: 2,
                    opacity: 0.6,
                    dashArray: '6 4'
                });
                zone.addTo(heatmapLayer);

                // Inner glow circle
                var innerGlow = L.circle([m.lat, m.lng], {
                    radius: radiusKm * 500,
                    fillColor: colors.fill,
                    fillOpacity: 0.18,
                    color: 'transparent',
                    weight: 0
                });
                innerGlow.addTo(heatmapLayer);

                // Score marker (custom divIcon)
                var markerSize = m.readiness.score >= 75 ? 56 : 48;
                var scoreIcon = L.divIcon({
                    className: 'capacity-score-marker',
                    html: '<div style="' +
                        'width:' + markerSize + 'px;height:' + markerSize + 'px;' +
                        'background:' + colors.fill + ';' +
                        'border:3px solid ' + colors.border + ';' +
                        'border-radius:50%;' +
                        'display:flex;align-items:center;justify-content:center;' +
                        'font-family:JetBrains Mono,monospace;' +
                        'font-size:16px;font-weight:800;' +
                        'color:#fff;' +
                        'box-shadow:0 0 20px ' + colors.glow + ', 0 4px 12px rgba(0,0,0,0.4);' +
                        'cursor:pointer;' +
                        'transition:transform 0.2s, box-shadow 0.2s;' +
                        '">' +
                        Math.round(m.readiness.score) +
                    '</div>',
                    iconSize: [markerSize, markerSize],
                    iconAnchor: [markerSize/2, markerSize/2]
                });

                var marker = L.marker([m.lat, m.lng], { icon: scoreIcon, zIndexOffset: 1000 });

                // Build popup content
                var spareMW = m.grid.spare_capacity_mw;
                var spareFormatted = spareMW >= 1000 ? (spareMW / 1000).toFixed(1) + ' GW' : spareMW.toLocaleString() + ' MW';
                var capacityFormatted = m.power.local_capacity_mw >= 1000 
                    ? (m.power.local_capacity_mw / 1000).toFixed(1) + ' GW' 
                    : m.power.local_capacity_mw.toLocaleString() + ' MW';

                var popupHTML = 
                    '<div style="min-width:300px;font-family:Inter,sans-serif;">' +
                        // Header
                        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid #333;">' +
                            '<div>' +
                                '<div style="font-size:15px;font-weight:700;color:#fff;">' + m.name + '</div>' +
                                '<div style="font-size:11px;color:#888;margin-top:2px;">' + m.iso + ' • Readiness: ' + m.readiness.label + '</div>' +
                            '</div>' +
                            '<div style="' +
                                'width:48px;height:48px;' +
                                'background:' + colors.fill + ';' +
                                'border:2px solid ' + colors.border + ';' +
                                'border-radius:50%;' +
                                'display:flex;align-items:center;justify-content:center;' +
                                'font-family:JetBrains Mono,monospace;' +
                                'font-size:18px;font-weight:800;color:#fff;' +
                            '">' + Math.round(m.readiness.score) + '</div>' +
                        '</div>' +

                        // Grid section
                        '<div style="margin-bottom:10px;">' +
                            '<div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">⚡ Grid Capacity</div>' +
                            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">' +
                                '<div style="background:#1a1a2e;padding:8px 10px;border-radius:6px;">' +
                                    '<div style="font-size:9px;color:#666;">Spare Capacity</div>' +
                                    '<div style="font-size:14px;font-weight:700;color:' + colors.fill + ';font-family:JetBrains Mono,monospace;">' + spareFormatted + '</div>' +
                                '</div>' +
                                '<div style="background:#1a1a2e;padding:8px 10px;border-radius:6px;">' +
                                    '<div style="font-size:9px;color:#666;">Headroom</div>' +
                                    '<div style="font-size:14px;font-weight:700;color:#fff;font-family:JetBrains Mono,monospace;">' + m.grid.spare_capacity_pct.toFixed(1) + '%</div>' +
                                '</div>' +
                            '</div>' +
                        '</div>' +

                        // Infrastructure row
                        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px;">' +
                            '<div style="background:#1a1a2e;padding:8px;border-radius:6px;text-align:center;">' +
                                '<div style="font-size:9px;color:#666;">Power Plants</div>' +
                                '<div style="font-size:13px;font-weight:700;color:#f59e0b;font-family:JetBrains Mono,monospace;">' + m.power.local_plants + '</div>' +
                                '<div style="font-size:9px;color:#555;">' + capacityFormatted + '</div>' +
                            '</div>' +
                            '<div style="background:#1a1a2e;padding:8px;border-radius:6px;text-align:center;">' +
                                '<div style="font-size:9px;color:#666;">Gas Pipelines</div>' +
                                '<div style="font-size:13px;font-weight:700;color:#f97316;font-family:JetBrains Mono,monospace;">' + m.gas.pipeline_count + '</div>' +
                                '<div style="font-size:9px;color:#555;">' + signalToIcon(m.gas.signal) + ' ' + m.gas.headroom_mdth.toLocaleString() + ' MDth</div>' +
                            '</div>' +
                            '<div style="background:#1a1a2e;padding:8px;border-radius:6px;text-align:center;">' +
                                '<div style="font-size:9px;color:#666;">Fiber Routes</div>' +
                                '<div style="font-size:13px;font-weight:700;color:#06b6d4;font-family:JetBrains Mono,monospace;">' + m.fiber.route_count + '</div>' +
                                '<div style="font-size:9px;color:#555;">Networks</div>' +
                            '</div>' +
                        '</div>' +

                        // Cost row
                        '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(16,185,129,0.1));border-radius:6px;margin-bottom:8px;">' +
                            '<div style="font-size:11px;color:#aaa;">⚡ Electricity Rate</div>' +
                            '<div style="font-size:14px;font-weight:700;color:' + (m.cost.electricity_rate_cents_kwh < 7.5 ? '#10b981' : m.cost.electricity_rate_cents_kwh < 9 ? '#f59e0b' : '#ef4444') + ';font-family:JetBrains Mono,monospace;">' + 
                                m.cost.electricity_rate_cents_kwh.toFixed(2) + '¢/kWh</div>' +
                        '</div>' +

                        // Footer
                        '<div style="font-size:9px;color:#555;text-align:center;padding-top:6px;border-top:1px solid #333;">Data: EIA Live • Updated every 30 min</div>' +
                    '</div>';

                marker.bindPopup(popupHTML, { maxWidth: 360, className: 'capacity-popup' });
                marker.addTo(heatmapLayer);

                // Market name label
                var labelIcon = L.divIcon({
                    className: 'capacity-label',
                    html: '<div style="' +
                        'font-family:Inter,sans-serif;' +
                        'font-size:10px;font-weight:600;' +
                        'color:' + colors.fill + ';' +
                        'text-shadow:0 1px 4px rgba(0,0,0,0.8);' +
                        'white-space:nowrap;' +
                        'text-align:center;' +
                        'pointer-events:none;' +
                        '">' + m.name + '<br>' +
                        '<span style="font-family:JetBrains Mono,monospace;font-size:9px;color:#aaa;">' + m.readiness.grade + ' • ' + m.iso + '</span>' +
                    '</div>',
                    iconSize: [120, 30],
                    iconAnchor: [60, -20]
                });
                L.marker([m.lat, m.lng], { icon: labelIcon, interactive: false, zIndexOffset: 999 })
                    .addTo(heatmapLayer);
            });

            console.log('🔥 Capacity Heatmap: Rendered ' + marketData.length + ' markets');
        }

        // ============================================
        // LIVE DATA FETCH (updates from backend)
        // ============================================
        function fetchHeadroomData() {
            fetch(BACKEND_URL + '/api/v1/capacity/heatmap', {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            })
            .then(function(resp) {
                if (!resp.ok) throw new Error('API ' + resp.status);
                return resp.json();
            })
            .then(function(data) {
                if (data.success && data.markets && data.markets.length > 0) {
                    // Update market data with live API response
                    data.markets.forEach(function(apiMarket) {
                        var existing = marketData.find(function(m) { return m.market === apiMarket.market; });
                        if (existing) {
                            if (apiMarket.readiness) existing.readiness = apiMarket.readiness;
                            if (apiMarket.grid) existing.grid = apiMarket.grid;
                            if (apiMarket.gas) existing.gas = apiMarket.gas;
                            if (apiMarket.power) existing.power = apiMarket.power;
                            if (apiMarket.fiber) existing.fiber = apiMarket.fiber;
                            if (apiMarket.cost) existing.cost = apiMarket.cost;
                        }
                    });
                    // Re-render if layer is active
                    if (window.map.hasLayer(heatmapLayer)) {
                        renderHeatmap();
                    }
                    console.log('🔥 Capacity Heatmap: Updated from live API (' + data.markets.length + ' markets)');
                }
            })
            .catch(function(err) {
                console.log('🔥 Capacity Heatmap: Using cached data (' + err.message + ')');
            });
        }

        // ============================================
        // LAYER TOGGLE INTEGRATION
        // ============================================
        
        // The generic handler in land-power-app.js handles add/remove via data-layer="capacityHeatmap"
        // We just need to render content when the layer is shown
        window.map.on('layeradd', function(e) {
            if (e.layer === heatmapLayer) {
                renderHeatmap();
                fetchHeadroomData();
                console.log('🔥 Capacity Heatmap: Shown');
            }
        });

        // Render once on init so the layer has content when toggled on
        renderHeatmap();

        // Expose for external use
        window.CapacityHeatmap = {
            render: renderHeatmap,
            refresh: fetchHeadroomData,
            getData: function() { return marketData; },
            layer: heatmapLayer
        };

        // Initial render if layer is meant to be on by default (it's not, user toggles it)
        // renderHeatmap();

        // Try fetching live data on load (updates cache silently)
        setTimeout(fetchHeadroomData, 5000);

        // Auto-refresh every 30 minutes
        setInterval(fetchHeadroomData, 30 * 60 * 1000);

        console.log('🔥 Capacity Heatmap Layer: Ready (8 markets, click button to show)');
    }
})();
