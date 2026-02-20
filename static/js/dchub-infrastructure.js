/**
 * DC Hub Infrastructure v2.0
 * Comprehensive infrastructure layer management with HIFLD, EIA, and Overpass data
 */
(function() {
    'use strict';
    
    const CONFIG = {
        API_BASE: window.location.origin,
        HIFLD_BASE: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services',
        EIA_BASE: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services',
        OVERPASS_ENDPOINTS: [
            'https://overpass-api.de/api/interpreter',
            'https://overpass.kumi.systems/api/interpreter',
            'https://overpass.openstreetmap.ru/api/interpreter'
        ],
        CACHE_DURATION: 10 * 60 * 1000,
        REQUEST_TIMEOUT: 30000
    };
    
    const DataCache = {
        _cache: {},
        get(key) {
            const item = this._cache[key];
            if (item && Date.now() - item.timestamp < CONFIG.CACHE_DURATION) {
                return item.data;
            }
            return null;
        },
        set(key, data) {
            this._cache[key] = { data, timestamp: Date.now() };
        }
    };
    
    async function queryArcGIS(url, params = {}) {
        const defaultParams = {
            f: 'json',
            inSR: '4326',
            outSR: '4326',
            returnGeometry: 'true',
            resultRecordCount: '500'
        };
        
        const queryParams = new URLSearchParams({ ...defaultParams, ...params });
        
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), CONFIG.REQUEST_TIMEOUT);
            
            const response = await fetch(`${url}?${queryParams}`, {
                signal: controller.signal
            });
            clearTimeout(timeoutId);
            
            if (!response.ok) {
                console.warn(`ArcGIS query failed: HTTP ${response.status}`);
                return { features: [], error: `HTTP ${response.status}` };
            }
            return await response.json();
        } catch (error) {
            if (error.name === 'AbortError') {
                console.warn('ArcGIS query timed out');
            } else {
                console.warn(`ArcGIS query error: ${error.message}`);
            }
            return { features: [], error: error.message };
        }
    }
    
    async function queryOverpass(query) {
        for (const endpoint of CONFIG.OVERPASS_ENDPOINTS) {
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), CONFIG.REQUEST_TIMEOUT);
                
                const response = await fetch(endpoint, {
                    method: 'POST',
                    body: `data=${encodeURIComponent(query)}`,
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    signal: controller.signal
                });
                clearTimeout(timeoutId);
                
                if (response.ok) {
                    return await response.json();
                }
                console.warn(`Overpass ${endpoint} returned ${response.status}, trying next...`);
            } catch (error) {
                if (error.name === 'AbortError') {
                    console.warn(`Overpass ${endpoint} timed out, trying next...`);
                } else {
                    console.warn(`Overpass ${endpoint} failed: ${error.message}, trying next...`);
                }
            }
        }
        console.warn('All Overpass endpoints failed, using fallback data');
        return { elements: [] };
    }
    
    function getBboxFromBounds(bounds) {
        const west = bounds.getWest();
        const south = bounds.getSouth();
        const east = bounds.getEast();
        const north = bounds.getNorth();
        return `${west},${south},${east},${north}`;
    }
    
    async function loadSubstations(map, bounds, layerGroup) {
        const cacheKey = `substations-${getBboxFromBounds(bounds)}`;
        let cached = DataCache.get(cacheKey);
        
        if (!cached) {
            const bbox = getBboxFromBounds(bounds);
            const result = await queryArcGIS(
                `${CONFIG.HIFLD_BASE}/Electric_Substations/FeatureServer/0/query`,
                {
                    where: '1=1',
                    geometry: bbox,
                    geometryType: 'esriGeometryEnvelope',
                    spatialRel: 'esriSpatialRelIntersects',
                    outFields: 'NAME,CITY,STATE,STATUS,MAX_VOLT,MIN_VOLT,LATITUDE,LONGITUDE'
                }
            );
            cached = result.features || [];
            if (cached.length > 0) DataCache.set(cacheKey, cached);
        }
        
        layerGroup.clearLayers();
        cached.forEach(f => {
            const attr = f.attributes || {};
            const lat = attr.LATITUDE || (f.geometry?.y);
            const lng = attr.LONGITUDE || (f.geometry?.x);
            if (!lat || !lng) return;
            
            const voltage = attr.MAX_VOLT || 0;
            let color = '#a855f7';
            if (voltage >= 345) color = '#ef4444';
            else if (voltage >= 230) color = '#f59e0b';
            else if (voltage >= 115) color = '#10b981';
            
            const marker = L.circleMarker([lat, lng], {
                radius: 5,
                fillColor: color,
                color: '#fff',
                weight: 1,
                fillOpacity: 0.8
            });
            
            marker.bindPopup(`
                <div style="min-width:180px">
                    <strong style="color:#a855f7;">⚡ ${attr.NAME || 'Substation'}</strong><br>
                    <small>${attr.CITY || ''}, ${attr.STATE || ''}</small><br>
                    <hr style="margin:6px 0;border-color:#333;">
                    <div style="font-size:12px;">
                        <div><b>Max Voltage:</b> ${attr.MAX_VOLT ? attr.MAX_VOLT + ' kV' : 'N/A'}</div>
                        <div><b>Status:</b> ${attr.STATUS || 'Active'}</div>
                    </div>
                </div>
            `);
            layerGroup.addLayer(marker);
        });
        
        console.log(`✅ Loaded ${cached.length} substations`);
        return cached.length;
    }
    
    async function loadPowerPlants(map, bounds, layerGroup) {
        const cacheKey = `powerplants-${getBboxFromBounds(bounds)}`;
        let cached = DataCache.get(cacheKey);
        
        if (!cached) {
            const bbox = getBboxFromBounds(bounds);
            const result = await queryArcGIS(
                `${CONFIG.HIFLD_BASE}/Power_Plants/FeatureServer/0/query`,
                {
                    where: '1=1',
                    geometry: bbox,
                    geometryType: 'esriGeometryEnvelope',
                    spatialRel: 'esriSpatialRelIntersects',
                    outFields: 'NAME,PRIM_FUEL,TOTAL_MW,LATITUDE,LONGITUDE,OPER_STAT,STATE'
                }
            );
            cached = result.features || [];
            if (cached.length > 0) DataCache.set(cacheKey, cached);
        }
        
        layerGroup.clearLayers();
        const fuelColors = {
            'NG': '#f59e0b', 'Nuclear': '#10b981', 'Coal': '#6b7280',
            'Hydro': '#06b6d4', 'Wind': '#22c55e', 'Solar': '#eab308',
            'Oil': '#1f2937', 'Geothermal': '#ef4444'
        };
        
        cached.forEach(f => {
            const attr = f.attributes || {};
            const lat = attr.LATITUDE || (f.geometry?.y);
            const lng = attr.LONGITUDE || (f.geometry?.x);
            if (!lat || !lng) return;
            
            const fuel = attr.PRIM_FUEL || 'Unknown';
            const color = fuelColors[fuel] || '#6366f1';
            const mw = attr.TOTAL_MW || 0;
            const radius = Math.max(4, Math.min(12, Math.sqrt(mw) / 3));
            
            const marker = L.circleMarker([lat, lng], {
                radius: radius,
                fillColor: color,
                color: '#fff',
                weight: 1,
                fillOpacity: 0.8
            });
            
            marker.bindPopup(`
                <div style="min-width:180px">
                    <strong style="color:#f59e0b;">🏭 ${attr.NAME || 'Power Plant'}</strong><br>
                    <small>${attr.STATE || ''}</small><br>
                    <hr style="margin:6px 0;border-color:#333;">
                    <div style="font-size:12px;">
                        <div><b>Fuel:</b> ${fuel}</div>
                        <div><b>Capacity:</b> ${mw.toLocaleString()} MW</div>
                        <div><b>Status:</b> ${attr.OPER_STAT || 'Operating'}</div>
                    </div>
                </div>
            `);
            layerGroup.addLayer(marker);
        });
        
        console.log(`✅ Loaded ${cached.length} power plants`);
        return cached.length;
    }
    
    async function loadTransmissionLines(map, bounds, layerGroup) {
        const cacheKey = `transmission-${getBboxFromBounds(bounds)}`;
        let cached = DataCache.get(cacheKey);
        
        if (!cached) {
            const bbox = getBboxFromBounds(bounds);
            const result = await queryArcGIS(
                `${CONFIG.HIFLD_BASE}/Electric_Power_Transmission_Lines/FeatureServer/0/query`,
                {
                    where: '1=1',
                    geometry: bbox,
                    geometryType: 'esriGeometryEnvelope',
                    spatialRel: 'esriSpatialRelIntersects',
                    outFields: 'VOLTAGE,OWNER,STATUS',
                    resultRecordCount: '200'
                }
            );
            cached = result.features || [];
            if (cached.length > 0) DataCache.set(cacheKey, cached);
        }
        
        layerGroup.clearLayers();
        cached.forEach(f => {
            const attr = f.attributes || {};
            const paths = f.geometry?.paths || [];
            if (!paths.length) return;
            
            const voltage = attr.VOLTAGE || 0;
            let color = '#f59e0b';
            let weight = 2;
            if (voltage >= 500) { color = '#ef4444'; weight = 4; }
            else if (voltage >= 345) { color = '#f97316'; weight = 3; }
            else if (voltage >= 230) { color = '#eab308'; weight = 2; }
            
            paths.forEach(path => {
                const latLngs = path.map(([lng, lat]) => [lat, lng]);
                const line = L.polyline(latLngs, {
                    color: color,
                    weight: weight,
                    opacity: 0.7
                });
                line.bindPopup(`
                    <div style="min-width:150px">
                        <strong style="color:#f59e0b;">⚡ Transmission Line</strong><br>
                        <div style="font-size:12px;">
                            <div><b>Voltage:</b> ${voltage} kV</div>
                            <div><b>Owner:</b> ${attr.OWNER || 'Unknown'}</div>
                        </div>
                    </div>
                `);
                layerGroup.addLayer(line);
            });
        });
        
        console.log(`✅ Loaded ${cached.length} transmission lines`);
        return cached.length;
    }
    
    async function loadGasPipelines(map, bounds, layerGroup) {
        const cacheKey = `gas-${getBboxFromBounds(bounds)}`;
        let cached = DataCache.get(cacheKey);
        
        if (!cached) {
            const bbox = getBboxFromBounds(bounds);
            const result = await queryArcGIS(
                'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Pipelines/FeatureServer/0/query',
                {
                    where: '1=1',
                    geometry: bbox,
                    geometryType: 'esriGeometryEnvelope',
                    spatialRel: 'esriSpatialRelIntersects',
                    outFields: 'TYPEPIPE,OPERATOR,STATUS',
                    resultRecordCount: '200'
                }
            );
            cached = result.features || [];
            if (cached.length > 0) DataCache.set(cacheKey, cached);
        }
        
        layerGroup.clearLayers();
        cached.forEach(f => {
            const attr = f.attributes || {};
            const paths = f.geometry?.paths || [];
            if (!paths.length) return;
            
            const type = attr.TYPEPIPE || '';
            let color = '#ef4444';
            if (type.toLowerCase().includes('interstate')) color = '#dc2626';
            else if (type.toLowerCase().includes('intrastate')) color = '#f97316';
            
            paths.forEach(path => {
                const latLngs = path.map(([lng, lat]) => [lat, lng]);
                const line = L.polyline(latLngs, {
                    color: color,
                    weight: 2,
                    opacity: 0.7,
                    dashArray: '8,4'
                });
                line.bindPopup(`
                    <div style="min-width:150px">
                        <strong style="color:#ef4444;">🔥 Gas Pipeline</strong><br>
                        <div style="font-size:12px;">
                            <div><b>Type:</b> ${type || 'Unknown'}</div>
                            <div><b>Operator:</b> ${attr.OPERATOR || 'Unknown'}</div>
                        </div>
                    </div>
                `);
                layerGroup.addLayer(line);
            });
        });
        
        console.log(`✅ Loaded ${cached.length} gas pipelines`);
        return cached.length;
    }
    
    function createStaticGasStorageLayer(layerGroup) {
        const gasStorageFacilities = [
            { name: "Leidy Hub", lat: 41.35, lng: -77.45, type: "Hub", capacity: "3.5 Bcf/d" },
            { name: "Henry Hub", lat: 30.25, lng: -91.82, type: "Hub", capacity: "4.0 Bcf/d" },
            { name: "Dominion South", lat: 39.90, lng: -77.35, type: "Hub", capacity: "2.8 Bcf/d" },
            { name: "Katy Hub", lat: 29.78, lng: -95.82, type: "Hub", capacity: "2.5 Bcf/d" },
            { name: "Waha Hub", lat: 31.40, lng: -103.20, type: "Hub", capacity: "3.0 Bcf/d" },
            { name: "Opal Hub", lat: 41.77, lng: -110.32, type: "Hub", capacity: "2.0 Bcf/d" }
        ];
        
        layerGroup.clearLayers();
        gasStorageFacilities.forEach(f => {
            const marker = L.circleMarker([f.lat, f.lng], {
                radius: 8,
                fillColor: '#dc2626',
                color: '#fff',
                weight: 2,
                fillOpacity: 0.9
            });
            marker.bindPopup(`
                <div style="min-width:150px">
                    <strong style="color:#dc2626;">⛽ ${f.name}</strong><br>
                    <div style="font-size:12px;">
                        <div><b>Type:</b> ${f.type}</div>
                        <div><b>Capacity:</b> ${f.capacity}</div>
                    </div>
                </div>
            `);
            layerGroup.addLayer(marker);
        });
        console.log(`✅ Loaded ${gasStorageFacilities.length} gas storage/hub facilities`);
    }
    
    function createStaticGasProcessingLayer(layerGroup) {
        const processingPlants = [
            { name: "Midland Processing", lat: 31.99, lng: -102.08, capacity: "200 MMcf/d" },
            { name: "Delaware Basin Plant", lat: 31.85, lng: -103.95, capacity: "250 MMcf/d" },
            { name: "Permian Gateway", lat: 32.10, lng: -102.50, capacity: "300 MMcf/d" },
            { name: "Eagle Ford Processing", lat: 28.75, lng: -98.50, capacity: "180 MMcf/d" },
            { name: "Marcellus Plant", lat: 41.20, lng: -76.80, capacity: "400 MMcf/d" }
        ];
        
        layerGroup.clearLayers();
        processingPlants.forEach(p => {
            const marker = L.circleMarker([p.lat, p.lng], {
                radius: 7,
                fillColor: '#f97316',
                color: '#fff',
                weight: 2,
                fillOpacity: 0.9
            });
            marker.bindPopup(`
                <div style="min-width:150px">
                    <strong style="color:#f97316;">🏭 ${p.name}</strong><br>
                    <div style="font-size:12px;">
                        <div><b>Type:</b> Gas Processing</div>
                        <div><b>Capacity:</b> ${p.capacity}</div>
                    </div>
                </div>
            `);
            layerGroup.addLayer(marker);
        });
        console.log(`✅ Loaded ${processingPlants.length} gas processing plants`);
    }
    
    function createStaticNGLFractionatorsLayer(layerGroup) {
        const fractionators = [
            { name: "Mont Belvieu Complex", lat: 29.85, lng: -94.88, capacity: "1.2 MMBbl/d" },
            { name: "Sweeny Hub", lat: 29.05, lng: -95.70, capacity: "400 MBbl/d" },
            { name: "Appalachia NGL", lat: 40.25, lng: -80.15, capacity: "200 MBbl/d" },
            { name: "Conway Hub", lat: 38.05, lng: -97.65, capacity: "350 MBbl/d" }
        ];
        
        layerGroup.clearLayers();
        fractionators.forEach(f => {
            const marker = L.circleMarker([f.lat, f.lng], {
                radius: 8,
                fillColor: '#a855f7',
                color: '#fff',
                weight: 2,
                fillOpacity: 0.9
            });
            marker.bindPopup(`
                <div style="min-width:150px">
                    <strong style="color:#a855f7;">🧪 ${f.name}</strong><br>
                    <div style="font-size:12px;">
                        <div><b>Type:</b> NGL Fractionator</div>
                        <div><b>Capacity:</b> ${f.capacity}</div>
                    </div>
                </div>
            `);
            layerGroup.addLayer(marker);
        });
        console.log(`✅ Loaded ${fractionators.length} NGL fractionators`);
    }
    
    async function loadDataCenters(map, bounds, layerGroup) {
        const cacheKey = `datacenters-${getBboxFromBounds(bounds)}`;
        let cached = DataCache.get(cacheKey);
        
        if (!cached) {
            const south = bounds.getSouth();
            const west = bounds.getWest();
            const north = bounds.getNorth();
            const east = bounds.getEast();
            
            const query = `
                [out:json][timeout:25];
                (
                    node["building"="data_centre"](${south},${west},${north},${east});
                    way["building"="data_centre"](${south},${west},${north},${east});
                    node["building"="data_center"](${south},${west},${north},${east});
                    way["building"="data_center"](${south},${west},${north},${east});
                );
                out center 100;
            `;
            
            const result = await queryOverpass(query);
            cached = result.elements || [];
            if (cached.length > 0) DataCache.set(cacheKey, cached);
        }
        
        layerGroup.clearLayers();
        cached.forEach(el => {
            const lat = el.lat || el.center?.lat;
            const lng = el.lon || el.center?.lon;
            if (!lat || !lng) return;
            
            const name = el.tags?.name || 'Data Center';
            const operator = el.tags?.operator || '';
            
            const marker = L.circleMarker([lat, lng], {
                radius: 7,
                fillColor: '#10b981',
                color: '#fff',
                weight: 2,
                fillOpacity: 0.9
            });
            marker.bindPopup(`
                <div style="min-width:150px">
                    <strong style="color:#10b981;">🖥️ ${name}</strong><br>
                    ${operator ? `<small>Operator: ${operator}</small>` : ''}
                </div>
            `);
            layerGroup.addLayer(marker);
        });
        
        console.log(`✅ Loaded ${cached.length} data centers from OSM`);
        return cached.length;
    }
    
    function createFallbackUtilityTerritories(layerGroup) {
        const territories = [
            { name: "Dominion Energy", coords: [[39.5, -78.5], [38.5, -78.5], [38.5, -76.5], [39.5, -76.5]], color: '#6366f1' },
            { name: "Duke Energy", coords: [[36.5, -82], [34.5, -82], [34.5, -79], [36.5, -79]], color: '#10b981' },
            { name: "Georgia Power", coords: [[34.5, -85], [31.5, -85], [31.5, -81], [34.5, -81]], color: '#f59e0b' },
            { name: "PG&E", coords: [[40, -124], [36, -124], [36, -119], [40, -119]], color: '#ef4444' },
            { name: "ERCOT", coords: [[36, -106], [26, -106], [26, -93], [36, -93]], color: '#06b6d4' }
        ];
        
        layerGroup.clearLayers();
        territories.forEach(t => {
            const polygon = L.polygon(t.coords, {
                color: t.color,
                fillColor: t.color,
                fillOpacity: 0.1,
                weight: 2
            });
            polygon.bindPopup(`<strong>${t.name}</strong><br>Utility Service Territory`);
            layerGroup.addLayer(polygon);
        });
        console.log(`✅ Loaded ${territories.length} utility territory outlines (fallback)`);
    }
    
    function safeToggleLayer(layerName, layers, map, item) {
        if (!layerName) {
            console.warn('toggleLayer called with null/undefined layer name');
            return;
        }
        
        if (!layers[layerName]) {
            console.warn(`Layer '${layerName}' not found in layers object`);
            return;
        }
        
        if (item && item.classList.contains('active')) {
            map.addLayer(layers[layerName]);
        } else if (item) {
            map.removeLayer(layers[layerName]);
        }
    }
    
    async function loadFromBackendAPI(endpoint, layerGroup, markerOptions = {}) {
        try {
            const response = await fetch(`${CONFIG.API_BASE}${endpoint}`);
            if (!response.ok) return [];
            const data = await response.json();
            
            if (!data.success || !data.features) return [];
            
            layerGroup.clearLayers();
            data.features.forEach(f => {
                if (!f.lat || !f.lng) return;
                const marker = L.circleMarker([f.lat, f.lng], {
                    radius: markerOptions.radius || 6,
                    fillColor: markerOptions.color || '#6366f1',
                    color: '#fff',
                    weight: 1,
                    fillOpacity: 0.8
                });
                
                const popupContent = Object.entries(f)
                    .filter(([k]) => k !== 'lat' && k !== 'lng' && k !== 'paths' && k !== 'rings')
                    .map(([k, v]) => `<b>${k}:</b> ${v}`)
                    .join('<br>');
                marker.bindPopup(`<div style="max-width:200px">${popupContent}</div>`);
                layerGroup.addLayer(marker);
            });
            
            console.log(`✅ Loaded ${data.features.length} ${endpoint.split('/').pop()}`);
            return data.features;
        } catch (e) {
            console.warn(`Failed to load ${endpoint}:`, e.message);
            return [];
        }
    }
    
    async function loadLNGTerminals(layerGroup) {
        return loadFromBackendAPI('/api/v2/infrastructure/static/lng_terminals', layerGroup, {
            color: '#f59e0b', radius: 8
        });
    }
    
    async function loadGasMarketHubs(layerGroup) {
        return loadFromBackendAPI('/api/v2/infrastructure/static/gas_market_hubs', layerGroup, {
            color: '#ef4444', radius: 7
        });
    }
    
    async function loadISORTORegions(layerGroup) {
        return loadFromBackendAPI('/api/v2/infrastructure/static/iso_rto', layerGroup, {
            color: '#8b5cf6', radius: 10
        });
    }
    
    async function loadSubmarineCables(layerGroup) {
        return loadFromBackendAPI('/api/v2/infrastructure/static/submarine_cables', layerGroup, {
            color: '#06b6d4', radius: 6
        });
    }
    
    async function loadRailroads(map, bounds, layerGroup) {
        const bbox = getBboxFromBounds(bounds);
        try {
            const response = await fetch(`${CONFIG.API_BASE}/api/v2/infrastructure/railroads?lat=${bounds.getCenter().lat}&lng=${bounds.getCenter().lng}&radius=50`);
            if (!response.ok) return 0;
            const data = await response.json();
            
            layerGroup.clearLayers();
            (data.railroads || []).forEach(r => {
                if (r.paths && r.paths.length) {
                    r.paths.forEach(path => {
                        const coords = path.map(p => [p[1], p[0]]);
                        const line = L.polyline(coords, {
                            color: '#78716c',
                            weight: 2,
                            opacity: 0.7
                        });
                        line.bindPopup(`<b>Railroad:</b> ${r.owner || 'Unknown'}<br><b>Tracks:</b> ${r.tracks || 'N/A'}`);
                        layerGroup.addLayer(line);
                    });
                }
            });
            console.log(`✅ Loaded ${data.count} railroad segments`);
            return data.count;
        } catch (e) {
            console.warn('Failed to load railroads:', e.message);
            return 0;
        }
    }
    
    async function loadAirports(map, bounds, layerGroup) {
        try {
            const response = await fetch(`${CONFIG.API_BASE}/api/v2/infrastructure/airports?lat=${bounds.getCenter().lat}&lng=${bounds.getCenter().lng}&radius=100`);
            if (!response.ok) return 0;
            const data = await response.json();
            
            layerGroup.clearLayers();
            (data.airports || []).forEach(a => {
                if (!a.lat || !a.lng) return;
                const marker = L.circleMarker([a.lat, a.lng], {
                    radius: a.type === 'AIRPORT' ? 6 : 4,
                    fillColor: '#3b82f6',
                    color: '#fff',
                    weight: 1,
                    fillOpacity: 0.8
                });
                marker.bindPopup(`
                    <strong>✈️ ${a.name}</strong><br>
                    <b>ICAO:</b> ${a.ident}<br>
                    <b>Type:</b> ${a.type}<br>
                    <b>City:</b> ${a.city}, ${a.state}
                `);
                layerGroup.addLayer(marker);
            });
            console.log(`✅ Loaded ${data.count} airports`);
            return data.count;
        } catch (e) {
            console.warn('Failed to load airports:', e.message);
            return 0;
        }
    }
    
    async function loadInternetExchanges(map, bounds, layerGroup) {
        try {
            const response = await fetch(`${CONFIG.API_BASE}/api/v2/infrastructure/internet-exchanges?lat=${bounds.getCenter().lat}&lng=${bounds.getCenter().lng}&radius=100`);
            if (!response.ok) return 0;
            const data = await response.json();
            
            layerGroup.clearLayers();
            (data.internet_exchanges || []).forEach(ix => {
                if (!ix.lat || !ix.lng) return;
                const marker = L.circleMarker([ix.lat, ix.lng], {
                    radius: 7,
                    fillColor: '#10b981',
                    color: '#fff',
                    weight: 1,
                    fillOpacity: 0.9
                });
                marker.bindPopup(`
                    <strong>🌐 ${ix.name}</strong><br>
                    <b>Location:</b> ${ix.city}, ${ix.state}
                `);
                layerGroup.addLayer(marker);
            });
            console.log(`✅ Loaded ${data.count} internet exchanges`);
            return data.count;
        } catch (e) {
            console.warn('Failed to load internet exchanges:', e.message);
            return 0;
        }
    }
    
    async function loadAquifers(map, bounds, layerGroup) {
        try {
            const response = await fetch(`${CONFIG.API_BASE}/api/v2/infrastructure/aquifers?lat=${bounds.getCenter().lat}&lng=${bounds.getCenter().lng}&radius=100`);
            if (!response.ok) return 0;
            const data = await response.json();
            console.log(`✅ Loaded ${data.count} aquifers`);
            return data.count;
        } catch (e) {
            console.warn('Failed to load aquifers:', e.message);
            return 0;
        }
    }
    
    async function getInfrastructureSummary(lat, lng, radius = 50) {
        try {
            const response = await fetch(`${CONFIG.API_BASE}/api/v2/infrastructure/summary?lat=${lat}&lng=${lng}&radius=${radius}`);
            if (!response.ok) return null;
            return await response.json();
        } catch (e) {
            console.warn('Failed to get infrastructure summary:', e.message);
            return null;
        }
    }
    
    const WaterDroughtLayer = {
        async fetchDroughtStatus(lat, lng) {
            try {
                const response = await fetch(`${CONFIG.API_BASE}/api/v1/water/drought-status?lat=${lat}&lng=${lng}`);
                if (!response.ok) {
                    console.warn('Drought API returned:', response.status);
                    return null;
                }
                return await response.json();
            } catch (e) {
                console.warn('Failed to fetch drought status:', e.message);
                return null;
            }
        },
        
        async fetchDroughtHistory(state) {
            try {
                const response = await fetch(`${CONFIG.API_BASE}/api/v1/water/drought-history?state=${encodeURIComponent(state)}`);
                if (!response.ok) return null;
                return await response.json();
            } catch (e) {
                console.warn('Failed to fetch drought history:', e.message);
                return null;
            }
        },
        
        async fetchStateComparison(states) {
            try {
                const stateParam = Array.isArray(states) ? states.join(',') : states;
                const response = await fetch(`${CONFIG.API_BASE}/api/v1/water/state-comparison?states=${encodeURIComponent(stateParam)}`);
                if (!response.ok) return null;
                return await response.json();
            } catch (e) {
                console.warn('Failed to fetch state comparison:', e.message);
                return null;
            }
        },
        
        async getSiteWaterAssessment(lat, lng) {
            try {
                const response = await fetch(`${CONFIG.API_BASE}/api/v1/water/site-water-assessment?lat=${lat}&lng=${lng}`);
                if (!response.ok) return null;
                return await response.json();
            } catch (e) {
                console.warn('Failed to fetch site water assessment:', e.message);
                return null;
            }
        }
    };
    
    window.WaterDroughtLayer = WaterDroughtLayer;
    
    window.DCHubInfrastructure = {
        CONFIG,
        DataCache,
        queryArcGIS,
        queryOverpass,
        getBboxFromBounds,
        loadSubstations,
        loadPowerPlants,
        loadTransmissionLines,
        loadGasPipelines,
        createStaticGasStorageLayer,
        createStaticGasProcessingLayer,
        createStaticNGLFractionatorsLayer,
        loadDataCenters,
        createFallbackUtilityTerritories,
        safeToggleLayer,
        loadFromBackendAPI,
        loadLNGTerminals,
        loadGasMarketHubs,
        loadISORTORegions,
        loadSubmarineCables,
        loadRailroads,
        loadAirports,
        loadInternetExchanges,
        loadAquifers,
        getInfrastructureSummary,
        WaterDroughtLayer,
        version: '2.2.0'
    };
    
    console.log('⚡ DC Hub Infrastructure v2.2.0 loaded');
})();
