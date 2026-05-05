/**
 * DC Hub - Energy Discovery Integration v2.0
 * ============================================
 * Supplements Land & Power map with auto-discovered infrastructure
 * from /api/energy-discovery/* endpoints.
 *
 * v2.0 Changes:
 *   - 23 monitored markets (up from 8)
 *   - 4 layer toggles: Plants, Transmission, Wind, Pipelines
 *   - Market filter dropdown
 *   - Live status badge with counts
 *   - Lazy-load per layer (fetch on first toggle)
 *   - KMZ export button
 *   - Improved popups for all asset types
 *
 * Add to land-power.html AFTER land-power-app.js:
 * <script src="js/energy-discovery-integration.js?v=2.0"></script>
 */

(function () {
    'use strict';

    // =========================================================================
    // CONFIG
    // =========================================================================

    var API_BASE = window.location.origin;

    // All 23 monitored markets — must match backend MONITORED_MARKETS keys
    var MARKETS = {
        '': { label: 'All Markets' },
        'phoenix': { label: 'Phoenix, AZ', tier: 1 },
        'dallas': { label: 'Dallas, TX', tier: 1 },
        'northern_virginia': { label: 'Northern Virginia', tier: 1 },
        'atlanta': { label: 'Atlanta, GA', tier: 1 },
        'las_vegas': { label: 'Las Vegas, NV', tier: 1 },
        'salt_lake': { label: 'Salt Lake City, UT', tier: 1 },
        'columbus': { label: 'Columbus, OH', tier: 1 },
        'des_moines': { label: 'Des Moines, IA', tier: 1 },
        'chicago': { label: 'Chicago, IL', tier: 1 },
        'silicon_valley': { label: 'Silicon Valley, CA', tier: 1 },
        'new_york_nj': { label: 'New York / NJ', tier: 1 },
        'seattle_quincy': { label: 'Seattle / Quincy, WA', tier: 1 },
        'portland_hillsboro': { label: 'Portland / Hillsboro, OR', tier: 1 },
        'denver': { label: 'Denver, CO', tier: 2 },
        'san_antonio': { label: 'San Antonio, TX', tier: 2 },
        'houston': { label: 'Houston, TX', tier: 2 },
        'miami': { label: 'Miami, FL', tier: 2 },
        'reno': { label: 'Reno, NV', tier: 2 },
        'sacramento': { label: 'Sacramento, CA', tier: 2 },
        'minneapolis': { label: 'Minneapolis, MN', tier: 3 },
        'kansas_city': { label: 'Kansas City, MO', tier: 3 },
        'richmond': { label: 'Richmond, VA', tier: 3 },
        'nashville': { label: 'Nashville, TN', tier: 3 }
    };

    // =========================================================================
    // STATE
    // =========================================================================

    var discoveredLayers = {
        powerPlants: null,
        transmissionLines: null,
        windProjects: null,
        pipelines: null
    };
    var discoveryData = {
        powerPlants: [],
        transmissionLines: [],
        windProjects: [],
        pipelines: [],
        status: null
    };
    var activeMarket = '';   // '' = all
    var layerActive = {
        powerPlants: false,
        transmissionLines: false,
        windProjects: false,
        pipelines: false
    };

    // =========================================================================
    // API HELPERS
    // =========================================================================

    async function discoveryFetch(endpoint) {
        try {
            var resp = await fetch(API_BASE + '/api/energy-discovery/' + endpoint);
            if (!resp.ok) return null;
            var json = await resp.json();
            return json.success ? json : null;
        } catch (e) {
            console.warn('⚡ Discovery API /' + endpoint + ':', e.message);
            return null;
        }
    }

    function marketParam() {
        return activeMarket ? '&market=' + activeMarket : '';
    }

    // =========================================================================
    // STYLING
    // =========================================================================

    function getFuelStyle(fuelType) {
        if (!fuelType) return { icon: '⚡', color: '#888' };
        var f = fuelType.toLowerCase();
        if (f.includes('nuclear'))                        return { icon: '☢️', color: '#a855f7' };
        if (f.includes('solar'))                          return { icon: '☀️', color: '#facc15' };
        if (f.includes('wind'))                           return { icon: '🌬️', color: '#06b6d4' };
        if (f.includes('gas') || f.includes('natural'))   return { icon: '🔥', color: '#f97316' };
        if (f.includes('coal'))                           return { icon: '🏭', color: '#6b7280' };
        if (f.includes('hydro') || f.includes('water') || f.includes('pumped')) return { icon: '💧', color: '#3b82f6' };
        if (f.includes('geothermal'))                     return { icon: '🌋', color: '#ef4444' };
        if (f.includes('biomass') || f.includes('wood'))  return { icon: '🌿', color: '#22c55e' };
        if (f.includes('petroleum') || f.includes('oil')) return { icon: '🛢️', color: '#d97706' };
        if (f.includes('battery') || f.includes('storage')) return { icon: '🔋', color: '#8b5cf6' };
        return { icon: '⚡', color: '#888' };
    }

    function voltageColor(kv) {
        if (kv >= 500) return '#ef4444';
        if (kv >= 345) return '#f97316';
        if (kv >= 230) return '#facc15';
        if (kv >= 115) return '#22c55e';
        return '#6b7280';
    }

    // =========================================================================
    // POPUPS
    // =========================================================================

    function popupGrid(pairs) {
        var cells = pairs.map(function (p) {
            return '<div style="background:#1a1a2e;padding:8px;border-radius:6px;">' +
                '<div style="font-size:9px;color:#888;text-transform:uppercase;">' + p[0] + '</div>' +
                '<div style="font-size:12px;font-weight:600;color:' + (p[2] || '#fff') + ';">' + p[1] + '</div>' +
            '</div>';
        }).join('');
        return '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;">' + cells + '</div>';
    }

    function popupWrap(headerHtml, gridHtml) {
        return '<div style="min-width:260px;font-family:Inter,system-ui,sans-serif;">' +
            headerHtml + gridHtml +
            '<div style="font-size:9px;color:#555;text-align:center;margin-top:6px;">DC Hub Energy Auto-Discovery</div>' +
        '</div>';
    }

    function powerPlantPopup(plant) {
        var s = getFuelStyle(plant.fuel_type);
        var header = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #333;">' +
            '<span style="font-size:22px;">' + s.icon + '</span>' +
            '<div><div style="font-size:14px;font-weight:700;color:' + s.color + ';">' + (plant.name || 'Power Plant') + '</div>' +
            '<div style="font-size:11px;color:#888;">' + (plant.operator || '') + '</div></div></div>';
        var grid = popupGrid([
            ['Capacity', plant.capacity_mw ? plant.capacity_mw.toLocaleString() + ' MW' : 'N/A', '#10b981'],
            ['Fuel Type', plant.fuel_type || 'N/A', s.color],
            ['State', plant.state || 'N/A', '#fff'],
            ['Source', plant.source || 'EIA', '#06b6d4']
        ]);
        return popupWrap(header, grid);
    }

    function transmissionPopup(line) {
        var kv = line.voltage_kv || 0;
        var header = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #333;">' +
            '<span style="font-size:22px;">⚡</span>' +
            '<div><div style="font-size:14px;font-weight:700;color:' + voltageColor(kv) + ';">' + (line.owner || 'Transmission Line') + '</div>' +
            '<div style="font-size:11px;color:#888;">' + (line.volt_class || '') + '</div></div></div>';
        var grid = popupGrid([
            ['Voltage', kv ? kv.toLocaleString() + ' kV' : 'N/A', voltageColor(kv)],
            ['Class', line.volt_class || 'N/A', '#fff'],
            ['From', line.sub_1 || 'N/A', '#888'],
            ['To', line.sub_2 || 'N/A', '#888']
        ]);
        return popupWrap(header, grid);
    }

    function windPopup(project) {
        var header = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #333;">' +
            '<span style="font-size:22px;">🌬️</span>' +
            '<div><div style="font-size:14px;font-weight:700;color:#06b6d4;">' + (project.project_name || 'Wind Project') + '</div>' +
            '<div style="font-size:11px;color:#888;">' + (project.manufacturer || '') + ' ' + (project.model || '') + '</div></div></div>';
        var grid = popupGrid([
            ['Project MW', project.project_capacity_mw ? project.project_capacity_mw.toLocaleString() + ' MW' : 'N/A', '#06b6d4'],
            ['Turbine kW', project.turbine_capacity_kw ? project.turbine_capacity_kw.toLocaleString() + ' kW' : 'N/A', '#fff'],
            ['State', project.state || 'N/A', '#fff'],
            ['County', project.county || 'N/A', '#888']
        ]);
        return popupWrap(header, grid);
    }

    function pipelinePopup(pipe) {
        var header = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #333;">' +
            '<span style="font-size:22px;">⛽</span>' +
            '<div><div style="font-size:14px;font-weight:700;color:#f97316;">' + (pipe.name || pipe.operator || 'Pipeline') + '</div>' +
            '<div style="font-size:11px;color:#888;">' + (pipe.operator || '') + '</div></div></div>';
        var grid = popupGrid([
            ['Capacity', pipe.capacity_mdth ? pipe.capacity_mdth.toLocaleString() + ' MDth/d' : 'N/A', '#f97316'],
            ['Diameter', pipe.diameter_inches ? pipe.diameter_inches + '"' : 'N/A', '#fff'],
            ['Commodity', pipe.commodity || 'Natural Gas', '#fff'],
            ['States', pipe.states_served || pipe.state || 'N/A', '#888']
        ]);
        return popupWrap(header, grid);
    }

    // =========================================================================
    // LAYER LOADERS
    // =========================================================================

    async function loadPowerPlants() {
        var result = await discoveryFetch('power-plants?limit=2000' + marketParam());
        if (!result || !result.data) return 0;
        if (typeof map === 'undefined' || typeof L === 'undefined') return 0;

        if (discoveredLayers.powerPlants) map.removeLayer(discoveredLayers.powerPlants);

        var markers = [];
        discoveryData.powerPlants = result.data;

        result.data.forEach(function (plant) {
            if (!plant.lat || !plant.lng) return;
            var s = getFuelStyle(plant.fuel_type);
            var radius = Math.min(5 + Math.sqrt(plant.capacity_mw || 0) / 8, 22);
            var marker = L.circleMarker([plant.lat, plant.lng], {
                radius: radius,
                fillColor: s.color,
                color: '#fff',
                weight: 1,
                opacity: 0.9,
                fillOpacity: 0.7
            });
            marker.bindPopup(powerPlantPopup(plant), { maxWidth: 340 });
            markers.push(marker);
        });

        if (markers.length > 0) {
            discoveredLayers.powerPlants = L.featureGroup(markers);
            if (layerActive.powerPlants) discoveredLayers.powerPlants.addTo(map);
        }
        return markers.length;
    }

    async function loadTransmissionLines() {
        var result = await discoveryFetch('transmission-lines?limit=2000' + marketParam());
        if (!result || !result.data) return 0;
        if (typeof map === 'undefined' || typeof L === 'undefined') return 0;

        if (discoveredLayers.transmissionLines) map.removeLayer(discoveredLayers.transmissionLines);

        // Transmission lines don't have individual lat/lng in our DB (hash-based IDs).
        // We show them as a summary badge — or if sub coords exist, plot those.
        // For now, show count badge and let the HIFLD layer handle geometry.
        discoveryData.transmissionLines = result.data;

        // If any lines have geometry/coords in the future, we'd plot them here.
        // For now, transmission lines are counted for the status panel.
        console.log('⚡ Transmission lines loaded: ' + result.data.length);
        return result.data.length;
    }

    async function loadWindProjects() {
        var result = await discoveryFetch('wind-projects?limit=2000' + (activeMarket ? '&market=' + activeMarket : ''));
        if (!result || !result.data) return 0;
        if (typeof map === 'undefined' || typeof L === 'undefined') return 0;

        if (discoveredLayers.windProjects) map.removeLayer(discoveredLayers.windProjects);

        var markers = [];
        discoveryData.windProjects = result.data;

        result.data.forEach(function (project) {
            if (!project.lat || !project.lng) return;
            var marker = L.circleMarker([project.lat, project.lng], {
                radius: Math.min(5 + Math.sqrt(project.project_capacity_mw || 0) / 6, 18),
                fillColor: '#06b6d4',
                color: '#fff',
                weight: 1,
                opacity: 0.85,
                fillOpacity: 0.6
            });
            marker.bindPopup(windPopup(project), { maxWidth: 340 });
            markers.push(marker);
        });

        if (markers.length > 0) {
            discoveredLayers.windProjects = L.featureGroup(markers);
            if (layerActive.windProjects) discoveredLayers.windProjects.addTo(map);
        }
        return markers.length;
    }

    async function loadPipelines() {
        var result = await discoveryFetch('pipelines?limit=200' + marketParam());
        if (!result || !result.data) return 0;
        if (typeof map === 'undefined' || typeof L === 'undefined') return 0;

        if (discoveredLayers.pipelines) map.removeLayer(discoveredLayers.pipelines);

        var markers = [];
        discoveryData.pipelines = result.data;

        result.data.forEach(function (pipe) {
            if (!pipe.lat || !pipe.lng) return;
            var radius = Math.min(6 + Math.sqrt(pipe.capacity_mdth || 0) / 30, 16);
            var marker = L.circleMarker([pipe.lat, pipe.lng], {
                radius: radius,
                fillColor: '#f97316',
                color: '#fff',
                weight: 1.5,
                opacity: 0.9,
                fillOpacity: 0.65
            });
            marker.bindPopup(pipelinePopup(pipe), { maxWidth: 340 });
            markers.push(marker);
        });

        if (markers.length > 0) {
            discoveredLayers.pipelines = L.featureGroup(markers);
            if (layerActive.pipelines) discoveredLayers.pipelines.addTo(map);
        }
        return markers.length;
    }

    // =========================================================================
    // STATUS
    // =========================================================================

    async function loadDiscoveryStatus() {
        var result = await discoveryFetch('status');
        if (result && result.data) {
            discoveryData.status = result.data;
            console.log('⚡ Energy Discovery Status:');
            console.log('   🏭 Power Plants: ' + (result.data.total_power_plants || 0));
            console.log('   ⚡ Transmission Lines: ' + (result.data.total_transmission_lines || 0));
            console.log('   🌬️ Wind Projects: ' + (result.data.total_wind_projects || 0));
            console.log('   ⛽ Pipelines: ' + (result.data.total_pipelines || 0));
            console.log('   📊 Total Capacity: ' + ((result.data.total_capacity_mw || 0) / 1000).toFixed(1) + ' GW');
            console.log('   🔄 Scheduler: ' + (result.data.running ? 'Running' : 'Stopped'));
            console.log('   📍 Markets: ' + (result.data.markets_monitored || 0));
            updateStatusBadge(result.data);
            if (window.dcHubAudit) {
                window.dcHubAudit.discoveredPowerPlants = result.data.total_power_plants || 0;
                window.dcHubAudit.discoveredTransmission = result.data.total_transmission_lines || 0;
                window.dcHubAudit.discoveredPipelines = result.data.total_pipelines || 0;
                window.dcHubAudit.discoveredWind = result.data.total_wind_projects || 0;
            }
        }
        return result;
    }

    // =========================================================================
    // UI — PANEL, BUTTONS, STATUS BADGE
    // =========================================================================

    var CSS = `
/* Energy Discovery Panel */
#energy-discovery-panel {
    position: absolute;
    top: 80px;
    right: 12px;
    z-index: 1200;
    background: rgba(10,10,26,0.92);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(99,102,241,0.3);
    border-radius: 12px;
    padding: 0;
    width: 240px;
    font-family: 'JetBrains Mono', 'SF Mono', monospace;
    color: #e2e8f0;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    transition: opacity .2s;
}
#energy-discovery-panel.collapsed {
    width: auto;
    padding: 0;
}
#energy-discovery-panel.collapsed .edp-body { display: none; }

.edp-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid rgba(99,102,241,0.2);
    cursor: pointer;
    user-select: none;
}
.edp-header:hover { background: rgba(99,102,241,0.08); }
.edp-title {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #818cf8;
}
.edp-collapse-icon {
    font-size: 12px;
    color: #666;
    transition: transform .2s;
}
#energy-discovery-panel.collapsed .edp-collapse-icon { transform: rotate(180deg); }

.edp-body { padding: 10px 12px 12px; }

/* Status badge */
.edp-status {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    margin-bottom: 10px;
}
.edp-stat {
    background: rgba(30,30,60,0.6);
    padding: 6px 8px;
    border-radius: 6px;
    text-align: center;
}
.edp-stat-val {
    font-size: 13px;
    font-weight: 700;
    color: #10b981;
}
.edp-stat-label {
    font-size: 8px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-top: 2px;
}

/* Market select */
.edp-market-select {
    width: 100%;
    padding: 6px 8px;
    margin-bottom: 10px;
    background: #111128;
    border: 1px solid rgba(99,102,241,0.25);
    border-radius: 6px;
    color: #e2e8f0;
    font-size: 11px;
    font-family: inherit;
    cursor: pointer;
    outline: none;
}
.edp-market-select:focus { border-color: #818cf8; }
.edp-market-select option { background: #111128; color: #e2e8f0; }

/* Layer buttons */
.edp-layers { display: flex; flex-direction: column; gap: 5px; margin-bottom: 10px; }
.edp-layer-btn {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 7px 10px;
    background: rgba(30,30,60,0.5);
    border: 1px solid rgba(99,102,241,0.15);
    border-radius: 6px;
    color: #94a3b8;
    font-size: 11px;
    font-family: inherit;
    cursor: pointer;
    transition: all .15s;
}
.edp-layer-btn:hover { background: rgba(99,102,241,0.1); color: #e2e8f0; }
.edp-layer-btn.active {
    background: rgba(99,102,241,0.15);
    border-color: rgba(99,102,241,0.4);
    color: #818cf8;
}
.edp-layer-btn .edp-count {
    font-size: 10px;
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 10px;
    background: rgba(16,185,129,0.15);
    color: #10b981;
}
.edp-layer-btn.active .edp-count {
    background: rgba(99,102,241,0.2);
    color: #a5b4fc;
}

/* Export button */
.edp-export-btn {
    width: 100%;
    padding: 7px;
    background: rgba(6,182,212,0.12);
    border: 1px solid rgba(6,182,212,0.25);
    border-radius: 6px;
    color: #06b6d4;
    font-size: 10px;
    font-family: inherit;
    cursor: pointer;
    text-align: center;
    transition: all .15s;
}
.edp-export-btn:hover { background: rgba(6,182,212,0.2); }

/* Capacity badge */
.edp-capacity {
    text-align: center;
    font-size: 9px;
    color: #555;
    margin-top: 8px;
}
.edp-capacity strong { color: #10b981; font-size: 11px; }
`;

    function injectCSS() {
        if (document.getElementById('edp-styles')) return;
        var style = document.createElement('style');
        style.id = 'edp-styles';
        style.textContent = CSS;
        document.head.appendChild(style);
    }

    function buildPanel() {
        if (document.getElementById('energy-discovery-panel')) return;
        injectCSS();

        var panel = document.createElement('div');
        panel.id = 'energy-discovery-panel';
        panel.innerHTML =
            '<div class="edp-header" id="edp-toggle-header">' +
                '<span class="edp-title">⚡ Energy Discovery</span>' +
                '<span class="edp-collapse-icon">▼</span>' +
            '</div>' +
            '<div class="edp-body">' +
                // Status
                '<div class="edp-status" id="edp-status">' +
                    '<div class="edp-stat"><div class="edp-stat-val" id="edp-plants">—</div><div class="edp-stat-label">Plants</div></div>' +
                    '<div class="edp-stat"><div class="edp-stat-val" id="edp-txlines">—</div><div class="edp-stat-label">TX Lines</div></div>' +
                    '<div class="edp-stat"><div class="edp-stat-val" id="edp-wind">—</div><div class="edp-stat-label">Wind</div></div>' +
                    '<div class="edp-stat"><div class="edp-stat-val" id="edp-pipes">—</div><div class="edp-stat-label">Pipelines</div></div>' +
                '</div>' +
                // Market selector
                '<select class="edp-market-select" id="edp-market-select">' +
                    buildMarketOptions() +
                '</select>' +
                // Layer toggles
                '<div class="edp-layers">' +
                    '<button class="edp-layer-btn" data-layer="powerPlants">🏭 Power Plants <span class="edp-count" id="edp-cnt-plants">—</span></button>' +
                    '<button class="edp-layer-btn" data-layer="windProjects">🌬️ Wind Projects <span class="edp-count" id="edp-cnt-wind">—</span></button>' +
                    '<button class="edp-layer-btn" data-layer="pipelines">⛽ Pipelines <span class="edp-count" id="edp-cnt-pipes">—</span></button>' +
                    '<button class="edp-layer-btn" data-layer="transmissionLines">⚡ TX Lines <span class="edp-count" id="edp-cnt-tx">—</span></button>' +
                '</div>' +
                // Export
                '<button class="edp-export-btn" id="edp-export">📦 Export KMZ</button>' +
                // Capacity
                '<div class="edp-capacity" id="edp-capacity"></div>' +
            '</div>';

        // Insert into map container or body
        var mapContainer = document.getElementById('map') || document.querySelector('.map-container');
        if (mapContainer && mapContainer.parentElement) {
            mapContainer.parentElement.style.position = 'relative';
            mapContainer.parentElement.appendChild(panel);
        } else {
            document.body.appendChild(panel);
        }

        // Collapse toggle
        document.getElementById('edp-toggle-header').addEventListener('click', function () {
            panel.classList.toggle('collapsed');
        });

        // Market selector
        document.getElementById('edp-market-select').addEventListener('change', function () {
            activeMarket = this.value;
            refreshActiveLayers();
        });

        // Layer buttons
        panel.querySelectorAll('.edp-layer-btn').forEach(function (btn) {
            btn.addEventListener('click', function () { toggleLayer(this); });
        });

        // Export
        document.getElementById('edp-export').addEventListener('click', function () {
            var url = API_BASE + '/api/energy-discovery/export/kmz?type=all';
            if (activeMarket) url += '&market=' + activeMarket;
            window.open(url, '_blank');
        });
    }

    function buildMarketOptions() {
        var html = '';
        var tiers = { 0: [], 1: [], 2: [], 3: [] };
        Object.keys(MARKETS).forEach(function (key) {
            var t = MARKETS[key].tier || 0;
            tiers[t].push(key);
        });
        // "All" first
        html += '<option value="">All Markets (23)</option>';
        // Then by tier
        var tierLabels = { 1: 'Primary Hubs', 2: 'Fast-Growing', 3: 'Emerging' };
        [1, 2, 3].forEach(function (t) {
            html += '<optgroup label="' + tierLabels[t] + '">';
            tiers[t].forEach(function (key) {
                html += '<option value="' + key + '">' + MARKETS[key].label + '</option>';
            });
            html += '</optgroup>';
        });
        return html;
    }

    function updateStatusBadge(status) {
        var el = function (id) { return document.getElementById(id); };
        if (el('edp-plants')) el('edp-plants').textContent = formatNum(status.total_power_plants);
        if (el('edp-txlines')) el('edp-txlines').textContent = formatNum(status.total_transmission_lines);
        if (el('edp-wind')) el('edp-wind').textContent = formatNum(status.total_wind_projects);
        if (el('edp-pipes')) el('edp-pipes').textContent = formatNum(status.total_pipelines);
        if (el('edp-capacity')) {
            var gw = ((status.total_capacity_mw || 0) / 1000).toFixed(1);
            el('edp-capacity').innerHTML = 'Total Capacity: <strong>' + gw + ' GW</strong> · ' +
                (status.markets_monitored || 23) + ' markets · ' +
                (status.running ? '🟢 Live' : '🔴 Stopped');
        }
    }

    function formatNum(n) {
        if (n === null || n === undefined) return '—';
        if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
        return String(n);
    }

    // =========================================================================
    // LAYER TOGGLE LOGIC
    // =========================================================================

    var loaders = {
        powerPlants: loadPowerPlants,
        windProjects: loadWindProjects,
        pipelines: loadPipelines,
        transmissionLines: loadTransmissionLines
    };
    var countEls = {
        powerPlants: 'edp-cnt-plants',
        windProjects: 'edp-cnt-wind',
        pipelines: 'edp-cnt-pipes',
        transmissionLines: 'edp-cnt-tx'
    };
    var dataKeys = {
        powerPlants: 'powerPlants',
        windProjects: 'windProjects',
        pipelines: 'pipelines',
        transmissionLines: 'transmissionLines'
    };

    async function toggleLayer(btn) {
        var layerKey = btn.getAttribute('data-layer');
        layerActive[layerKey] = !layerActive[layerKey];
        btn.classList.toggle('active');

        if (layerActive[layerKey]) {
            btn.style.opacity = '0.6';
            btn.style.pointerEvents = 'none';
            var count = await loaders[layerKey]();
            btn.style.opacity = '';
            btn.style.pointerEvents = '';
            var countEl = document.getElementById(countEls[layerKey]);
            if (countEl) countEl.textContent = formatNum(count);
            // Add layer to map if not already
            if (discoveredLayers[layerKey]) discoveredLayers[layerKey].addTo(map);
        } else {
            if (discoveredLayers[layerKey]) map.removeLayer(discoveredLayers[layerKey]);
        }
    }

    async function refreshActiveLayers() {
        var keys = Object.keys(layerActive);
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i];
            if (layerActive[k]) {
                var count = await loaders[k]();
                var countEl = document.getElementById(countEls[k]);
                if (countEl) countEl.textContent = formatNum(count);
            }
        }
    }

    // =========================================================================
    // ALSO ADD LEGACY BUTTON to .layer-toggles (backward compat)
    // =========================================================================

    function addLegacyButton() {
        var layerToggles = document.querySelector('.layer-toggles');
        if (!layerToggles || document.getElementById('discovery-power-btn')) return;

        var btn = document.createElement('button');
        btn.id = 'discovery-power-btn';
        btn.className = 'layer-btn';
        btn.setAttribute('data-layer', 'discoveredPower');
        btn.innerHTML = '⚡ Energy Discovery';
        btn.title = 'Open Energy Discovery panel';
        btn.addEventListener('click', function () {
            var panel = document.getElementById('energy-discovery-panel');
            if (panel) {
                panel.classList.remove('collapsed');
                panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        });
        layerToggles.appendChild(btn);
    }

    // =========================================================================
    // INIT
    // =========================================================================

    function init() {
        var attempts = 0;
        var checkMap = setInterval(function () {
            attempts++;
            if (typeof map !== 'undefined' && typeof L !== 'undefined') {
                clearInterval(checkMap);
                buildPanel();
                addLegacyButton();
                loadDiscoveryStatus();
                console.log('⚡ Energy Discovery Integration v2.0 ready — 23 markets');
            } else if (attempts > 30) {
                clearInterval(checkMap);
                console.warn('⚡ Energy Discovery: map not found after 30s, panel-only mode');
                buildPanel();
                loadDiscoveryStatus();
            }
        }, 1000);
    }

    // Expose API
    window.EnergyDiscovery = {
        loadStatus: loadDiscoveryStatus,
        loadPowerPlants: loadPowerPlants,
        loadWindProjects: loadWindProjects,
        loadPipelines: loadPipelines,
        loadTransmissionLines: loadTransmissionLines,
        getData: function () { return discoveryData; },
        getLayers: function () { return discoveredLayers; },
        setMarket: function (m) {
            activeMarket = m;
            var sel = document.getElementById('edp-market-select');
            if (sel) sel.value = m;
            refreshActiveLayers();
        },
        getMarkets: function () { return MARKETS; },
        refresh: refreshActiveLayers
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
