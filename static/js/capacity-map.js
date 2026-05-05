(function() {
    'use strict';
    
    const FUEL_COLORS = {
        'Natural Gas': '#f97316',
        'Solar': '#eab308',
        'Wind': '#22c55e',
        'Nuclear': '#a855f7',
        'Coal': '#64748b',
        'Petroleum': '#78716c',
        'Hydro': '#06b6d4',
        'Hydroelectric': '#06b6d4',
        'Biomass': '#84cc16',
        'Geothermal': '#ec4899',
        'Other': '#94a3b8',
        'Storage': '#60a5fa'
    };
    
    const FUEL_LAYER_MAP = {
        'Natural Gas': 'natgas',
        'Solar': 'solar',
        'Wind': 'wind',
        'Nuclear': 'nuclear',
        'Coal': 'coal'
    };
    
    const MARKET_COORDS = {
        phoenix:            { lat: 33.45,  lng: -112.07 },
        dallas:             { lat: 32.78,  lng: -96.80 },
        northern_virginia:  { lat: 38.95,  lng: -77.45 },
        atlanta:            { lat: 33.75,  lng: -84.39 },
        las_vegas:          { lat: 36.17,  lng: -115.14 },
        salt_lake:          { lat: 40.76,  lng: -111.89 },
        columbus:           { lat: 39.96,  lng: -82.99 },
        des_moines:         { lat: 41.59,  lng: -93.62 }
    };
    
    let map, mapData, fiberData;
    const layers = {
        markets: L.layerGroup(),
        gas: L.layerGroup(),
        natgas: L.layerGroup(),
        solar: L.layerGroup(),
        wind: L.layerGroup(),
        nuclear: L.layerGroup(),
        coal: L.layerGroup(),
        fiber: L.layerGroup(),
        bead: L.layerGroup()
    };
    
    function initMap() {
        map = L.map('map', {
            center: [38.5, -98],
            zoom: 5,
            zoomControl: true,
            attributionControl: false
        });
        
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            maxZoom: 18
        }).addTo(map);
        
        Object.values(layers).forEach(l => l.addTo(map));
        
        setupLayerToggles();
    }
    
    function setupLayerToggles() {
        const layerMap = {
            'layer-markets': 'markets',
            'layer-gas': 'gas',
            'layer-natgas': 'natgas',
            'layer-solar': 'solar',
            'layer-wind': 'wind',
            'layer-nuclear': 'nuclear',
            'layer-coal': 'coal',
            'layer-fiber': 'fiber',
            'layer-bead': 'bead'
        };
        
        Object.entries(layerMap).forEach(([checkboxId, layerKey]) => {
            const cb = document.getElementById(checkboxId);
            if (!cb) return;
            cb.addEventListener('change', () => {
                if (cb.checked) {
                    map.addLayer(layers[layerKey]);
                } else {
                    map.removeLayer(layers[layerKey]);
                }
            });
            if (!cb.checked) {
                map.removeLayer(layers[layerKey]);
            }
        });
    }
    
    async function loadData() {
        try {
            const resp = await fetch('/api/capacity-map/data');
            const json = await resp.json();
            if (!json.success) throw new Error(json.error || 'Failed to load data');
            mapData = json;
            return json;
        } catch (err) {
            console.error('Failed to load capacity data:', err);
            return null;
        }
    }
    
    function updateSummary(data) {
        const s = data.summary;
        document.getElementById('total-capacity').textContent = s.total_capacity_gw.toLocaleString();
        document.getElementById('total-plants').textContent = s.total_power_plants.toLocaleString();
        document.getElementById('total-pipelines').textContent = s.total_pipelines;
    }
    
    function renderMarketCards(data) {
        const container = document.getElementById('market-list');
        container.innerHTML = '<h3>Markets (8 tracked)</h3>';
        
        const sorted = Object.entries(data.markets).sort((a, b) =>
            (b[1].power.total_capacity_mw || 0) - (a[1].power.total_capacity_mw || 0)
        );
        
        sorted.forEach(([key, market]) => {
            const card = document.createElement('div');
            card.className = 'market-card';
            card.dataset.market = key;
            
            const fuelBar = buildFuelBar(market.power.fuel_breakdown, market.power.total_capacity_mw);
            
            card.innerHTML = `
                <div class="market-name">${market.name}</div>
                <div class="market-stats">
                    <div class="market-stat"><strong>${market.power.total_capacity_gw} GW</strong> power</div>
                    <div class="market-stat"><strong>${market.power.total_plants}</strong> plants</div>
                    <div class="market-stat"><strong>${market.gas.total_pipelines}</strong> pipelines</div>
                    <div class="market-stat"><strong>${formatMdth(market.gas.total_capacity_mdth)}</strong> gas</div>
                </div>
                ${fuelBar}
            `;
            
            card.addEventListener('click', () => {
                document.querySelectorAll('.market-card').forEach(c => c.classList.remove('active'));
                card.classList.add('active');
                zoomToMarket(key, market);
            });
            
            container.appendChild(card);
        });
    }
    
    function buildFuelBar(breakdown, total) {
        if (!breakdown || !breakdown.length || !total) return '';
        let html = '<div class="fuel-bar">';
        breakdown.forEach(f => {
            const pct = (f.capacity_mw / total * 100);
            if (pct < 1) return;
            const color = getFuelColor(f.fuel);
            html += `<div class="fuel-segment" style="width:${pct}%;background:${color}" title="${f.fuel}: ${f.capacity_mw.toLocaleString()} MW (${pct.toFixed(0)}%)"></div>`;
        });
        html += '</div>';
        return html;
    }
    
    function getFuelColor(fuel) {
        if (!fuel) return FUEL_COLORS['Other'];
        for (const [key, color] of Object.entries(FUEL_COLORS)) {
            if (fuel.toLowerCase().includes(key.toLowerCase())) return color;
        }
        return FUEL_COLORS['Other'];
    }
    
    function getFuelLayer(fuel) {
        if (!fuel) return null;
        const fl = fuel.toLowerCase();
        if (fl.includes('natural gas') || fl.includes('gas')) return 'natgas';
        if (fl.includes('solar')) return 'solar';
        if (fl.includes('wind')) return 'wind';
        if (fl.includes('nuclear')) return 'nuclear';
        if (fl.includes('coal')) return 'coal';
        return null;
    }
    
    function formatMdth(val) {
        if (!val) return '0';
        if (val >= 10000) return (val / 1000).toFixed(1) + ' Bcf';
        return val.toLocaleString() + ' MDth';
    }
    
    function formatMW(mw) {
        if (!mw) return '0 MW';
        if (mw >= 1000) return (mw / 1000).toFixed(1) + ' GW';
        return Math.round(mw) + ' MW';
    }
    
    function renderMapLayers(data) {
        const fuelCounts = { natgas: 0, solar: 0, wind: 0, nuclear: 0, coal: 0 };
        let gasCount = 0;
        
        Object.entries(data.markets).forEach(([key, market]) => {
            const coords = MARKET_COORDS[key] || { lat: market.center[0], lng: market.center[1] };
            
            const circle = L.circle([coords.lat, coords.lng], {
                radius: getMarketRadius(market.power.total_capacity_mw),
                fillColor: '#3b82f6',
                fillOpacity: 0.08,
                color: '#3b82f6',
                weight: 2,
                opacity: 0.4,
                dashArray: '6,4'
            });
            
            circle.bindPopup(buildMarketPopup(market));
            layers.markets.addLayer(circle);
            
            const label = L.marker([coords.lat, coords.lng], {
                icon: L.divIcon({
                    className: 'market-label',
                    html: `<div style="
                        background:rgba(59,130,246,0.9);
                        color:#fff;
                        padding:4px 10px;
                        border-radius:12px;
                        font-size:11px;
                        font-weight:600;
                        white-space:nowrap;
                        text-align:center;
                        box-shadow:0 2px 8px rgba(0,0,0,0.3);
                        font-family:Inter,sans-serif;
                    ">${market.name}<br><span style="font-size:10px;opacity:0.8">${market.power.total_capacity_gw} GW | ${market.gas.total_pipelines} pipes</span></div>`,
                    iconSize: null,
                    iconAnchor: [60, 12]
                })
            });
            layers.markets.addLayer(label);
            
            if (market.gas.pipelines) {
                market.gas.pipelines.forEach(pipe => {
                    if (!pipe.lat || !pipe.lng) return;
                    
                    const pipeMarker = L.circleMarker([pipe.lat, pipe.lng], {
                        radius: Math.max(6, Math.min(14, (pipe.capacity_mdth || 0) / 1500)),
                        fillColor: '#ef4444',
                        color: '#fff',
                        weight: 1.5,
                        fillOpacity: 0.85
                    });
                    
                    pipeMarker.bindPopup(`
                        <div style="min-width:200px;font-family:Inter,sans-serif">
                            <div style="font-size:14px;font-weight:700;color:#dc2626;margin-bottom:6px">&#x1f525; ${pipe.name || 'Gas Pipeline'}</div>
                            <table style="width:100%;font-size:12px">
                                <tr><td style="color:#64748b">Operator</td><td style="text-align:right;font-weight:500">${pipe.operator}</td></tr>
                                <tr><td style="color:#64748b">Capacity</td><td style="text-align:right;font-weight:500">${(pipe.capacity_mdth || 0).toLocaleString()} MDth/day</td></tr>
                                <tr><td style="color:#64748b">Diameter</td><td style="text-align:right;font-weight:500">${pipe.diameter_inches || '?'}" pipe</td></tr>
                                <tr><td style="color:#64748b">States</td><td style="text-align:right;font-weight:500">${pipe.states_served || pipe.state}</td></tr>
                                <tr><td style="color:#64748b">Status</td><td style="text-align:right;font-weight:500">${pipe.status || 'Active'}</td></tr>
                            </table>
                        </div>
                    `);
                    layers.gas.addLayer(pipeMarker);
                    gasCount++;
                });
            }
            
            if (market.top_plants) {
                market.top_plants.forEach(plant => {
                    const fuelLayerKey = getFuelLayer(plant.fuel_type);
                    if (!fuelLayerKey) return;
                    
                    const plantCoords = getPlantCoords(plant, coords, market.top_plants.indexOf(plant));
                    if (!plantCoords) return;
                    
                    const color = getFuelColor(plant.fuel_type);
                    const radius = Math.max(4, Math.min(10, Math.sqrt((plant.capacity_mw || 0) / 50)));
                    
                    const marker = L.circleMarker(plantCoords, {
                        radius: radius,
                        fillColor: color,
                        color: '#fff',
                        weight: 1,
                        fillOpacity: 0.8
                    });
                    
                    marker.bindPopup(`
                        <div style="min-width:200px;font-family:Inter,sans-serif">
                            <div style="font-size:13px;font-weight:700;color:${color};margin-bottom:6px">&#x26A1; ${plant.name || 'Power Plant'}</div>
                            <table style="width:100%;font-size:12px">
                                <tr><td style="color:#64748b">Operator</td><td style="text-align:right;font-weight:500">${plant.operator || 'Unknown'}</td></tr>
                                <tr><td style="color:#64748b">Capacity</td><td style="text-align:right;font-weight:500">${formatMW(plant.capacity_mw)}</td></tr>
                                <tr><td style="color:#64748b">Fuel</td><td style="text-align:right;font-weight:500">${plant.fuel_type || 'Unknown'}</td></tr>
                                <tr><td style="color:#64748b">Sector</td><td style="text-align:right;font-weight:500">${plant.sector || 'Unknown'}</td></tr>
                                <tr><td style="color:#64748b">State</td><td style="text-align:right;font-weight:500">${plant.state}</td></tr>
                            </table>
                        </div>
                    `);
                    
                    if (layers[fuelLayerKey]) {
                        layers[fuelLayerKey].addLayer(marker);
                        if (fuelCounts[fuelLayerKey] !== undefined) fuelCounts[fuelLayerKey]++;
                    }
                });
            }
        });
        
        document.getElementById('count-gas').textContent = gasCount;
        document.getElementById('count-natgas').textContent = fuelCounts.natgas;
        document.getElementById('count-solar').textContent = fuelCounts.solar;
        document.getElementById('count-wind').textContent = fuelCounts.wind;
        document.getElementById('count-nuclear').textContent = fuelCounts.nuclear;
        document.getElementById('count-coal').textContent = fuelCounts.coal;
    }
    
    function getPlantCoords(plant, marketCenter, index) {
        if (plant.lat && plant.lng) return [plant.lat, plant.lng];
        
        const angle = (index * 137.508) * Math.PI / 180;
        const r = 0.3 + (index * 0.05);
        return [
            marketCenter.lat + r * Math.cos(angle),
            marketCenter.lng + r * Math.sin(angle) * 1.2
        ];
    }
    
    function getMarketRadius(totalMW) {
        if (!totalMW) return 30000;
        if (totalMW > 100000) return 120000;
        if (totalMW > 50000) return 90000;
        if (totalMW > 20000) return 70000;
        return 50000;
    }
    
    function buildMarketPopup(market) {
        let fuelRows = '';
        if (market.power.fuel_breakdown) {
            market.power.fuel_breakdown.slice(0, 6).forEach(f => {
                const color = getFuelColor(f.fuel);
                fuelRows += `
                    <div class="stat-row">
                        <span class="stat-label"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:4px"></span>${f.fuel}</span>
                        <span class="stat-value">${formatMW(f.capacity_mw)} (${f.count})</span>
                    </div>`;
            });
        }
        
        let pipeRows = '';
        if (market.gas.pipelines) {
            market.gas.pipelines.slice(0, 5).forEach(p => {
                pipeRows += `
                    <div class="stat-row">
                        <span class="stat-label">${p.name || p.operator}</span>
                        <span class="stat-value">${(p.capacity_mdth || 0).toLocaleString()} MDth</span>
                    </div>`;
            });
        }
        
        return `
            <div class="market-popup">
                <h3>${market.name}</h3>
                <div class="stat-row">
                    <span class="stat-label">Total Power Capacity</span>
                    <span class="stat-value">${market.power.total_capacity_gw} GW</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Power Plants</span>
                    <span class="stat-value">${market.power.total_plants}</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Gas Pipelines</span>
                    <span class="stat-value">${market.gas.total_pipelines}</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Gas Capacity</span>
                    <span class="stat-value">${formatMdth(market.gas.total_capacity_mdth)}</span>
                </div>
                <hr style="border:none;border-top:1px solid #e2e8f0;margin:8px 0">
                <div style="font-size:11px;font-weight:600;color:#64748b;margin-bottom:4px">FUEL MIX</div>
                ${fuelRows}
                ${pipeRows ? `
                    <hr style="border:none;border-top:1px solid #e2e8f0;margin:8px 0">
                    <div style="font-size:11px;font-weight:600;color:#64748b;margin-bottom:4px">GAS PIPELINES</div>
                    ${pipeRows}
                ` : ''}
            </div>
        `;
    }
    
    function zoomToMarket(key, market) {
        const coords = MARKET_COORDS[key] || { lat: market.center[0], lng: market.center[1] };
        map.flyTo([coords.lat, coords.lng], 8, { duration: 1 });
        
        const panel = document.getElementById('info-panel');
        const title = document.getElementById('info-title');
        const content = document.getElementById('info-content');
        
        title.textContent = market.name;
        
        let html = '<table>';
        html += `<tr><td>Power Capacity</td><td>${market.power.total_capacity_gw} GW</td></tr>`;
        html += `<tr><td>Power Plants</td><td>${market.power.total_plants}</td></tr>`;
        html += `<tr><td>Gas Pipelines</td><td>${market.gas.total_pipelines}</td></tr>`;
        html += `<tr><td>Gas Capacity</td><td>${formatMdth(market.gas.total_capacity_mdth)}</td></tr>`;
        html += '</table>';
        
        if (market.power.fuel_breakdown && market.power.fuel_breakdown.length) {
            html += '<div style="margin-top:10px;font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Fuel Breakdown</div>';
            html += '<table>';
            market.power.fuel_breakdown.forEach(f => {
                const color = getFuelColor(f.fuel);
                html += `<tr><td><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:6px"></span>${f.fuel}</td><td>${formatMW(f.capacity_mw)}</td></tr>`;
            });
            html += '</table>';
        }
        
        if (market.gas.pipelines && market.gas.pipelines.length) {
            html += '<div style="margin-top:10px;font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Pipelines</div>';
            html += '<table>';
            market.gas.pipelines.forEach(p => {
                html += `<tr><td>${p.name || p.operator}</td><td>${(p.capacity_mdth || 0).toLocaleString()} MDth</td></tr>`;
            });
            html += '</table>';
        }
        
        content.innerHTML = html;
        panel.classList.add('visible');
    }
    
    async function loadFiberData() {
        try {
            const resp = await fetch('/api/fiber/map-data');
            const json = await resp.json();
            if (!json.success) return null;
            fiberData = json;
            return json;
        } catch (err) {
            console.error('Failed to load fiber data:', err);
            return null;
        }
    }

    function renderFiberLayers(data) {
        if (!data) return;

        const providerColors = {
            'Zayo': '#06b6d4',
            'Lumen': '#0ea5e9',
            'Crown Castle': '#38bdf8',
            'Cogent': '#67e8f9',
            'Uniti': '#22d3ee',
            'Segra': '#a5f3fc',
            'Windstream': '#7dd3fc',
            'Lightpath': '#bae6fd',
        };

        let fiberCount = 0;
        if (data.fiber_routes) {
            data.fiber_routes.forEach(route => {
                const color = providerColors[route.provider] || '#06b6d4';
                const weight = route.dark_fiber ? 3 : 2;

                const line = L.polyline(route.coords, {
                    color: color,
                    weight: weight,
                    opacity: 0.7,
                    dashArray: route.dark_fiber ? null : '8,6',
                });

                line.bindPopup(`
                    <div style="min-width:220px;font-family:Inter,sans-serif">
                        <div style="font-size:14px;font-weight:700;color:#06b6d4;margin-bottom:6px">&#x1F4E1; ${route.name}</div>
                        <table style="width:100%;font-size:12px">
                            <tr><td style="color:#64748b">Provider</td><td style="text-align:right;font-weight:500">${route.provider}</td></tr>
                            <tr><td style="color:#64748b">Route Miles</td><td style="text-align:right;font-weight:500">${route.miles.toLocaleString()}</td></tr>
                            <tr><td style="color:#64748b">Fiber Count</td><td style="text-align:right;font-weight:500">${route.fiber_count}</td></tr>
                            <tr><td style="color:#64748b">Dark Fiber</td><td style="text-align:right;font-weight:500">${route.dark_fiber ? 'Available' : 'No'}</td></tr>
                        </table>
                    </div>
                `);

                layers.fiber.addLayer(line);
                fiberCount++;
            });
        }

        let beadCount = 0;
        if (data.bead_grants) {
            data.bead_grants.forEach(grant => {
                const radius = Math.max(6, Math.min(16, Math.sqrt(grant.amount / 100000000)));

                const marker = L.circleMarker([grant.lat, grant.lng], {
                    radius: radius,
                    fillColor: '#10b981',
                    color: '#fff',
                    weight: 1.5,
                    fillOpacity: 0.75,
                });

                marker.bindPopup(`
                    <div style="min-width:220px;font-family:Inter,sans-serif">
                        <div style="font-size:14px;font-weight:700;color:#10b981;margin-bottom:6px">&#x1F4B0; BEAD Grant - ${grant.state}</div>
                        <table style="width:100%;font-size:12px">
                            <tr><td style="color:#64748b">Amount</td><td style="text-align:right;font-weight:500">${grant.amount_formatted}</td></tr>
                            <tr><td style="color:#64748b">Status</td><td style="text-align:right;font-weight:500">${grant.status}</td></tr>
                            <tr><td style="color:#64748b">Priority Tech</td><td style="text-align:right;font-weight:500">${grant.priority}</td></tr>
                            <tr><td style="color:#64748b">Unserved Locations</td><td style="text-align:right;font-weight:500">${grant.unserved.toLocaleString()}</td></tr>
                        </table>
                    </div>
                `);

                layers.bead.addLayer(marker);
                beadCount++;
            });
        }

        document.getElementById('count-fiber').textContent = fiberCount;
        document.getElementById('count-bead').textContent = beadCount;
        document.getElementById('total-fiber').textContent = fiberCount;
    }

    async function init() {
        initMap();

        const [data, fiber] = await Promise.all([loadData(), loadFiberData()]);
        if (!data) {
            document.getElementById('loading').innerHTML = '<div class="loading-spinner"><p style="color:#ef4444">Failed to load capacity data</p></div>';
            return;
        }

        updateSummary(data);
        renderMarketCards(data);
        renderMapLayers(data);
        renderFiberLayers(fiber);

        document.getElementById('loading').style.display = 'none';
    }

    document.addEventListener('DOMContentLoaded', init);
})();
