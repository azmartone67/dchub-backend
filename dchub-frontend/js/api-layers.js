/**
 * DC Hub API Layers Integration
 * ==============================
 * Adds new layer buttons for EIA, GridStatus, and EPA data
 * 
 * Dependencies: Leaflet, land-power-app.js (for map variable)
 * 
 * v2 Changes:
 * - Grid demand fetches handle 401 gracefully (shows ISO regions without data)
 * - EPA and EIA fetches have proper error handling
 * - Removed redundant individual ISO demand calls when summary provides data
 */

(function() {
    'use strict';
    
    const API_BASE = 'https://dchub.cloud';
    
    // Layer groups
    let epaFacilitiesLayer = null;
    let gridDemandLayer = null;
    let energyPricesLayer = null;
    let isInitialized = false;
    
    // Safe initialization - won't break if something fails
    function safeInit() {
        try {
            // Wait for map to be ready
            waitForMap(function() {
                try {
                    console.log('🔌 API Layers Integration loading...');
                    
                    epaFacilitiesLayer = L.layerGroup();
                    gridDemandLayer = L.layerGroup();
                    energyPricesLayer = L.layerGroup();
                    
                    // Add button click handlers
                    setupButtonHandlers();
                    
                    isInitialized = true;
                    console.log('✅ API Layers Integration ready');
                    console.log('   🌿 EPA Facilities Layer');
                    console.log('   ⚡ Grid Demand Layer');
                    console.log('   💰 Energy Prices Layer');
                } catch (err) {
                    console.warn('⚠️ API Layers init error (non-fatal):', err.message);
                }
            });
        } catch (err) {
            console.warn('⚠️ API Layers setup error (non-fatal):', err.message);
        }
    }
    
    // Wait for map to be ready
    function waitForMap(callback) {
        if (typeof map !== 'undefined' && map && typeof L !== 'undefined') {
            callback();
        } else {
            setTimeout(() => waitForMap(callback), 100);
        }
    }
    
    // Initialize after DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', safeInit);
    } else {
        safeInit();
    }
    
    function setupButtonHandlers() {
        // EPA Facilities button
        const epaBtn = document.getElementById('epa-btn');
        if (epaBtn) {
            epaBtn.addEventListener('click', function() {
                toggleEPALayer(this);
            });
        }
        
        // Grid Demand button
        const gridBtn = document.getElementById('grid-demand-btn');
        if (gridBtn) {
            gridBtn.addEventListener('click', function() {
                toggleGridDemandLayer(this);
            });
        }
        
        // Energy Prices button
        const pricesBtn = document.getElementById('energy-prices-btn');
        if (pricesBtn) {
            pricesBtn.addEventListener('click', function() {
                toggleEnergyPricesLayer(this);
            });
        }
    }
    
    // ===========================================
    // EPA FACILITIES LAYER
    // ===========================================
    async function toggleEPALayer(btn) {
        try {
            if (!epaFacilitiesLayer) {
                console.warn('⚠️ EPA layer not initialized');
                return;
            }
            
            if (map.hasLayer(epaFacilitiesLayer)) {
                map.removeLayer(epaFacilitiesLayer);
                btn.classList.remove('active');
                console.log('🌿 EPA Facilities hidden');
                return;
            }
            
            btn.classList.add('active');
            map.addLayer(epaFacilitiesLayer);
            
            // Get current map bounds state
            const center = map.getCenter();
            
            // Determine state from center (simplified)
            console.log(`🌿 Loading EPA facilities near ${center.lat.toFixed(2)}, ${center.lng.toFixed(2)}...`);
            
            const response = await fetch(`${API_BASE}/api/epa/facilities?lat=${center.lat}&lng=${center.lng}&radius=50`);
            
            if (!response.ok) {
                console.warn(`⚠️ EPA API returned ${response.status}`);
                btn.classList.remove('active');
                return;
            }
            
            const data = await response.json();
            
            if (data.success && data.data) {
                epaFacilitiesLayer.clearLayers();
                
                // Use facilities_in_state if no facilities with coords in radius
                const facilities = data.data.facilities_in_radius && data.data.facilities_in_radius.length > 0 
                    ? data.data.facilities_in_radius 
                    : (data.data.facilities_in_state || []);
                
                let count = 0;
                facilities.forEach(facility => {
                    // Try multiple coordinate fields
                    const lat = facility.fac_latitude || facility.pref_latitude || facility.latitude;
                    const lng = facility.fac_longitude || facility.pref_longitude || facility.longitude;
                    
                    // Skip if no valid coords or coords are 0
                    if (!lat || !lng || (lat === 0 && lng === 0)) return;
                    
                    const marker = L.circleMarker([lat, lng], {
                        radius: 6,
                        fillColor: '#10b981',
                        color: '#059669',
                        weight: 2,
                        opacity: 1,
                        fillOpacity: 0.7
                    });
                    
                    marker.bindPopup(`
                        <div style="min-width:200px;">
                            <div style="font-weight:700;color:#10b981;margin-bottom:8px;">🌿 EPA Facility</div>
                            <div><strong>${facility.facility_name || 'Unknown'}</strong></div>
                            <div style="color:#888;font-size:12px;">${facility.city_name || ''}, ${facility.state_abbr || ''}</div>
                            <div style="color:#666;font-size:11px;margin-top:4px;">${facility.street_address || ''}</div>
                            ${facility.parent_co_name && facility.parent_co_name !== 'NA' ? `<div style="margin-top:8px;font-size:11px;">Parent: ${facility.parent_co_name}</div>` : ''}
                        </div>
                    `);
                    
                    epaFacilitiesLayer.addLayer(marker);
                    count++;
                });
                
                // Show info about facilities without coords
                const totalInState = data.data.total_in_state || facilities.length;
                
                if (count === 0 && totalInState > 0) {
                    console.log(`⚠️ ${totalInState} EPA facilities in state but none have coordinates`);
                    // Add info marker at center
                    const infoMarker = L.marker([center.lat, center.lng], {
                        icon: L.divIcon({
                            className: 'epa-info-marker',
                            html: `<div style="background:#10b981;color:#fff;padding:8px 12px;border-radius:8px;font-size:12px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,0.3);">🌿 ${totalInState} EPA facilities in ${data.data.state || 'area'}<br><span style="font-size:10px;opacity:0.8;">(coordinates unavailable)</span></div>`,
                            iconSize: [150, 50],
                            iconAnchor: [75, 25]
                        })
                    });
                    epaFacilitiesLayer.addLayer(infoMarker);
                    console.log(`✅ Showing EPA summary for ${totalInState} facilities`);
                } else {
                    console.log(`✅ Loaded ${count} EPA facilities with coordinates`);
                }
            } else {
                console.log('⚠️ No EPA facility data returned');
            }
        } catch (err) {
            console.error('❌ EPA API error:', err);
            btn.classList.remove('active');
        }
    }
    
    // ===========================================
    // GRID DEMAND LAYER (ISO/RTO Real-time)
    // ===========================================
    async function toggleGridDemandLayer(btn) {
        try {
            if (!gridDemandLayer) {
                console.warn('⚠️ Grid demand layer not initialized');
                return;
            }
            
            if (map.hasLayer(gridDemandLayer)) {
                map.removeLayer(gridDemandLayer);
                btn.classList.remove('active');
                console.log('⚡ Grid Demand hidden');
                return;
            }
            
            btn.classList.add('active');
            map.addLayer(gridDemandLayer);
            
            console.log('⚡ Loading real-time grid demand...');
            
            // ISO/RTO approximate center coordinates
            const isoLocations = {
                'CAISO': { lat: 37.5, lng: -119.5, name: 'California ISO' },
                'ERCOT': { lat: 31.0, lng: -99.0, name: 'Texas (ERCOT)' },
                'PJM': { lat: 40.0, lng: -77.0, name: 'PJM Interconnection' },
                'NYISO': { lat: 42.5, lng: -75.5, name: 'New York ISO' },
                'MISO': { lat: 41.0, lng: -90.0, name: 'Midcontinent ISO' },
                'SPP': { lat: 36.0, lng: -98.0, name: 'Southwest Power Pool' },
                'ISONE': { lat: 42.5, lng: -71.5, name: 'ISO New England' }
            };
            
            gridDemandLayer.clearLayers();
            
            // Grid demand API not available — show static ISO markers only (no API calls)
            let isos = Object.keys(isoLocations);
            
            // Render ISO regions without making API calls
            for (const iso of isos) {
                const loc = isoLocations[iso];
                
                let demandGW = null;
                // Grid demand API disabled — no wasted 503 calls
                
                const color = demandGW ? '#f59e0b' : '#6b7280';
                const radius = demandGW ? Math.min(30, 10 + demandGW / 5) : 15;
                
                const marker = L.circleMarker([loc.lat, loc.lng], {
                    radius: radius,
                    fillColor: color,
                    color: '#fff',
                    weight: 2,
                    opacity: 1,
                    fillOpacity: 0.8
                });
                
                // Create popup content
                const popupContent = document.createElement('div');
                popupContent.style.minWidth = '220px';
                popupContent.innerHTML = `
                    <div style="font-weight:700;color:#f59e0b;margin-bottom:8px;">⚡ ${loc.name}</div>
                    <div style="font-size:14px;"><strong>${iso}</strong></div>
                    ${demandGW ? `
                        <div style="margin-top:8px;padding:8px;background:#1a1a2e;border-radius:6px;text-align:center;">
                            <div style="font-size:24px;font-weight:700;color:#f59e0b;">${demandGW.toFixed(1)} GW</div>
                            <div style="font-size:10px;color:#888;">Current Demand</div>
                        </div>
                    ` : `
                        <div style="margin-top:8px;color:#888;font-size:12px;">Real-time data requires subscription</div>
                    `}
                    <div id="fuel-mix-${iso}" style="margin-top:10px;">
                        <button onclick="window.DCHubAPILayers.loadFuelMix('${iso}')" style="width:100%;padding:8px;background:#f59e0b;color:#000;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:12px;">
                            🔥 Load Fuel Mix
                        </button>
                    </div>
                `;
                
                marker.bindPopup(popupContent);
                gridDemandLayer.addLayer(marker);
            }
            
            console.log(`✅ Loaded ${isos.length} ISO/RTO regions`);
            
        } catch (err) {
            console.error('❌ Grid API error:', err);
            btn.classList.remove('active');
        }
    }
    
    // ===========================================
    // ENERGY PRICES LAYER (State-level)
    // ===========================================
    async function toggleEnergyPricesLayer(btn) {
        try {
            if (!energyPricesLayer) {
                console.warn('⚠️ Energy prices layer not initialized');
                return;
            }
            
            if (map.hasLayer(energyPricesLayer)) {
                map.removeLayer(energyPricesLayer);
                btn.classList.remove('active');
                console.log('💰 Energy Prices hidden');
                return;
            }
            
            btn.classList.add('active');
            map.addLayer(energyPricesLayer);
            
            console.log('💰 Loading energy prices by state...');
            
            // Major data center states to show
            const dcStates = [
                { code: 'VA', lat: 37.4, lng: -78.5, name: 'Virginia' },
                { code: 'TX', lat: 31.0, lng: -99.0, name: 'Texas' },
                { code: 'AZ', lat: 34.0, lng: -111.5, name: 'Arizona' },
                { code: 'CA', lat: 36.8, lng: -119.5, name: 'California' },
                { code: 'GA', lat: 32.7, lng: -83.5, name: 'Georgia' },
                { code: 'OH', lat: 40.4, lng: -82.9, name: 'Ohio' },
                { code: 'IL', lat: 40.0, lng: -89.0, name: 'Illinois' },
                { code: 'NC', lat: 35.5, lng: -79.5, name: 'North Carolina' },
                { code: 'NV', lat: 38.8, lng: -116.5, name: 'Nevada' },
                { code: 'OR', lat: 44.0, lng: -120.5, name: 'Oregon' },
                { code: 'WA', lat: 47.4, lng: -120.5, name: 'Washington' },
                { code: 'NJ', lat: 40.1, lng: -74.5, name: 'New Jersey' }
            ];
            
            energyPricesLayer.clearLayers();
            
            for (const state of dcStates) {
                try {
                    const response = await fetch(`${API_BASE}/api/v1/energy/electricity-rates?state=${state.code}&sector=IND&months=1`);
                    
                    if (!response.ok) continue; // Skip states with errors
                    
                    const data = await response.json();
                    
                    let price = null;
                    let year = '2024';
                    
                    if (data.success && data.data && data.data.length > 0) {
                        const latest = data.data[0];
                        price = parseFloat(latest.price_cents_kwh);
                        year = latest.period || '2025';
                    }
                    
                    // Color based on price (green = cheap, red = expensive)
                    let color = '#6b7280';
                    if (price) {
                        if (price < 8) color = '#10b981';      // Green - cheap
                        else if (price < 10) color = '#84cc16'; // Lime
                        else if (price < 12) color = '#fbbf24'; // Yellow
                        else if (price < 15) color = '#f97316'; // Orange
                        else color = '#ef4444';                 // Red - expensive
                    }
                    
                    const marker = L.circleMarker([state.lat, state.lng], {
                        radius: 20,
                        fillColor: color,
                        color: '#fff',
                        weight: 2,
                        opacity: 1,
                        fillOpacity: 0.85
                    });
                    
                    // Add price label
                    const label = L.divIcon({
                        className: 'price-label',
                        html: `<div style="
                            background:${color};
                            color:#fff;
                            padding:4px 8px;
                            border-radius:12px;
                            font-size:11px;
                            font-weight:700;
                            white-space:nowrap;
                            box-shadow:0 2px 4px rgba(0,0,0,0.3);
                        ">${price ? price.toFixed(1) + '¢' : 'N/A'}</div>`,
                        iconSize: [50, 20],
                        iconAnchor: [25, 10]
                    });
                    
                    const labelMarker = L.marker([state.lat, state.lng], { icon: label });
                    
                    marker.bindPopup(`
                        <div style="min-width:200px;">
                            <div style="font-weight:700;color:${color};margin-bottom:8px;">💰 ${state.name}</div>
                            <div style="padding:12px;background:#1a1a2e;border-radius:8px;text-align:center;">
                                ${price ? `
                                    <div style="font-size:28px;font-weight:700;color:${color};">${price.toFixed(2)}¢</div>
                                    <div style="font-size:11px;color:#888;">per kWh (${year})</div>
                                ` : `
                                    <div style="color:#888;">Price data unavailable</div>
                                `}
                            </div>
                            <div style="margin-top:8px;font-size:10px;color:#666;">
                                Source: EIA Retail Electricity Prices
                            </div>
                        </div>
                    `);
                    
                    energyPricesLayer.addLayer(marker);
                    energyPricesLayer.addLayer(labelMarker);
                    
                } catch (err) {
                    console.log(`⚠️ Could not load price for ${state.code}`);
                }
            }
            
            console.log(`✅ Loaded energy prices for ${dcStates.length} states`);
            
        } catch (err) {
            console.error('❌ Energy Prices API error:', err);
            btn.classList.remove('active');
        }
    }
    
    // ===========================================
    // HELPER FUNCTIONS
    // ===========================================
    async function getStateFromCoords(lat, lng) {
        // Simplified state detection based on coords
        if (lng > -115 && lng < -109 && lat > 31 && lat < 37) return 'AZ';
        if (lng > -125 && lng < -114 && lat > 32 && lat < 42) return 'CA';
        if (lng > -107 && lng < -93 && lat > 25 && lat < 37) return 'TX';
        if (lng > -84 && lng < -75 && lat > 36 && lat < 40) return 'VA';
        if (lng > -85 && lng < -80 && lat > 30 && lat < 35) return 'GA';
        if (lng > -92 && lng < -87 && lat > 36 && lat < 42) return 'IL';
        if (lng > -85 && lng < -80 && lat > 38 && lat < 42) return 'OH';
        return 'AZ'; // Default
    }
    
    // Load fuel mix for an ISO
    async function loadFuelMix(iso) {
        const container = document.getElementById(`fuel-mix-${iso}`);
        if (!container) return;
        
        container.innerHTML = '<div style="text-align:center;color:#888;font-size:11px;">Loading fuel mix...</div>';
        
        try {
            const response = await fetch(`${API_BASE}/api/grid/fuel-mix?iso=${iso}`);
            
            if (!response.ok) {
                container.innerHTML = '<div style="color:#888;font-size:10px;text-align:center;">Fuel mix requires Pro subscription</div>';
                return;
            }
            
            const data = await response.json();
            
            if (data.success && data.data && data.data.fuel_mix) {
                const fuelMix = data.data.fuel_mix;
                const fuelIcons = {
                    'Natural Gas': '🔥',
                    'Gas': '🔥',
                    'Coal': '🪨',
                    'Nuclear': '☢️',
                    'Wind': '💨',
                    'Solar': '☀️',
                    'Hydro': '💧',
                    'Large Hydro': '💧',
                    'Imports': '📥',
                    'Other': '⚡',
                    'Batteries': '🔋',
                    'Geothermal': '🌋',
                    'Biomass': '🌿',
                    'Small Hydro': '💧'
                };
                
                let html = '<div style="background:#1a1a2e;border-radius:6px;padding:8px;margin-top:8px;">';
                html += '<div style="font-weight:600;font-size:11px;color:#f59e0b;margin-bottom:6px;">Fuel Mix</div>';
                
                // Sort by percentage
                const sorted = Object.entries(fuelMix).sort((a, b) => (b[1].percentage || 0) - (a[1].percentage || 0));
                
                for (const [fuel, info] of sorted) {
                    const pct = info.percentage || 0;
                    if (pct < 1) continue; // Skip tiny amounts
                    
                    const icon = fuelIcons[fuel] || '⚡';
                    const barWidth = Math.max(5, pct);
                    
                    html += `
                        <div style="margin:4px 0;font-size:10px;">
                            <div style="display:flex;justify-content:space-between;margin-bottom:2px;">
                                <span>${icon} ${fuel}</span>
                                <span style="color:#f59e0b;font-weight:600;">${pct.toFixed(1)}%</span>
                            </div>
                            <div style="background:#2a2a3e;border-radius:3px;height:6px;overflow:hidden;">
                                <div style="background:linear-gradient(90deg,#f59e0b,#fbbf24);width:${barWidth}%;height:100%;"></div>
                            </div>
                        </div>
                    `;
                }
                
                html += '</div>';
                container.innerHTML = html;
                
            } else {
                container.innerHTML = '<div style="color:#888;font-size:10px;text-align:center;">Fuel mix data unavailable for this ISO</div>';
            }
        } catch (err) {
            console.error('Fuel mix error:', err);
            container.innerHTML = '<div style="color:#ef4444;font-size:10px;text-align:center;">Error loading fuel mix</div>';
        }
    }
    
    // Expose functions globally
    window.DCHubAPILayers = {
        toggleEPALayer,
        toggleGridDemandLayer,
        toggleEnergyPricesLayer,
        loadFuelMix
    };
    
})();
