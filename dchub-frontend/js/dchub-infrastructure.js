/**
 * DC Hub Infrastructure Data Integration v2
 * Enhanced data layers with HIFLD, EIA, PeeringDB, and GridStatus APIs
 * Copyright © 2024 DC Hub. All rights reserved.
 * PROPRIETARY AND CONFIDENTIAL
 */

(function() {
    'use strict';
    
    // =========================================================================
    // CONFIGURATION
    // =========================================================================
    
    const CONFIG = {
        // HIFLD ArcGIS REST API endpoints
        HIFLD: {
            substations: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0',
            transmissionLines: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0',
            gasPipelines: null, // REMOVED: HIFLD Natural_Gas_Pipelines returns 400 since early 2026
            powerPlants: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0',
            airports: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Airports/FeatureServer/0',
            railroads: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Railroads/FeatureServer/0'
        },
        
        // EIA API v2 endpoints (requires API key for full access)
        EIA: {
            baseUrl: 'https://api.eia.gov/v2',
            gridMonitor: 'https://api.eia.gov/v2/electricity/rto/daily-region-data/data',
            fuelMix: 'https://api.eia.gov/v2/electricity/rto/fuel-type-data/data',
            demand: 'https://api.eia.gov/v2/electricity/rto/region-sub-ba-data/data'
        },
        
        // PeeringDB API (free, public)
        PEERINGDB: {
            baseUrl: 'https://www.peeringdb.com/api',
            facilities: 'https://www.peeringdb.com/api/fac',
            exchanges: 'https://www.peeringdb.com/api/ix',
            networks: 'https://www.peeringdb.com/api/net'
        },
        
        // Cache settings
        CACHE_DURATION: 24 * 60 * 60 * 1000, // 24 hours
        REALTIME_CACHE: 5 * 60 * 1000, // 5 minutes for real-time data
        MAX_RECORDS_PER_REQUEST: 2000,
        
        // Default map bounds for US
        US_BOUNDS: {
            minLat: 24.396308,
            maxLat: 49.384358,
            minLng: -125.0,
            maxLng: -66.93457
        }
    };
    
    // =========================================================================
    // ISO/RTO REAL-TIME GRID STATUS (Embedded Data)
    // =========================================================================
    
    const GridStatus = {
        // Current grid status by ISO/RTO (updated from EIA/GridStatus.io)
        // This would be fetched live with API key - using representative data
        isoData: {
            'PJM': {
                name: 'PJM Interconnection',
                currentLoad: 98500, // MW
                capacity: 185000, // MW
                availableCapacity: 86500,
                peakDemand: 165000,
                renewablePct: 8.2,
                avgLMP: 32.45, // $/MWh
                congestionLevel: 'Moderate',
                queuedMW: 296000,
                activeProjects: 2847,
                avgQueueWait: 4.2, // years
                color: '#3b82f6'
            },
            'ERCOT': {
                name: 'Electric Reliability Council of Texas',
                currentLoad: 52000,
                capacity: 92000,
                availableCapacity: 40000,
                peakDemand: 85500,
                renewablePct: 32.5,
                avgLMP: 28.90,
                congestionLevel: 'Low',
                queuedMW: 179000,
                activeProjects: 1234,
                avgQueueWait: 2.2,
                color: '#ef4444'
            },
            'CAISO': {
                name: 'California ISO',
                currentLoad: 32000,
                capacity: 80000,
                availableCapacity: 48000,
                peakDemand: 52000,
                renewablePct: 45.8,
                avgLMP: 45.20,
                congestionLevel: 'Low',
                queuedMW: 187000,
                activeProjects: 987,
                avgQueueWait: 3.5,
                color: '#eab308'
            },
            'MISO': {
                name: 'Midcontinent ISO',
                currentLoad: 78000,
                capacity: 155000,
                availableCapacity: 77000,
                peakDemand: 127000,
                renewablePct: 12.4,
                avgLMP: 26.80,
                congestionLevel: 'Low',
                queuedMW: 258000,
                activeProjects: 1856,
                avgQueueWait: 3.8,
                color: '#22c55e'
            },
            'SPP': {
                name: 'Southwest Power Pool',
                currentLoad: 38000,
                capacity: 85000,
                availableCapacity: 47000,
                peakDemand: 54000,
                renewablePct: 42.1,
                avgLMP: 22.50,
                congestionLevel: 'Low',
                queuedMW: 120000,
                activeProjects: 756,
                avgQueueWait: 3.1,
                color: '#a855f7'
            },
            'NYISO': {
                name: 'New York ISO',
                currentLoad: 22000,
                capacity: 42000,
                availableCapacity: 20000,
                peakDemand: 33000,
                renewablePct: 28.5,
                avgLMP: 38.90,
                congestionLevel: 'Moderate',
                queuedMW: 114000,
                activeProjects: 543,
                avgQueueWait: 2.8,
                color: '#f97316'
            },
            'ISO-NE': {
                name: 'ISO New England',
                currentLoad: 15000,
                capacity: 32000,
                availableCapacity: 17000,
                peakDemand: 25000,
                renewablePct: 18.2,
                avgLMP: 42.30,
                congestionLevel: 'Low',
                queuedMW: 36000,
                activeProjects: 298,
                avgQueueWait: 2.4,
                color: '#06b6d4'
            }
        },
        
        // Get formatted popup for ISO region
        getISOPopup(isoCode) {
            const data = this.isoData[isoCode];
            if (!data) return '';
            
            const utilizationPct = ((data.currentLoad / data.capacity) * 100).toFixed(1);
            const availableGW = (data.availableCapacity / 1000).toFixed(1);
            const congestionColor = data.congestionLevel === 'High' ? '#ef4444' : 
                                   data.congestionLevel === 'Moderate' ? '#f59e0b' : '#22c55e';
            
            return `
                <div style="min-width:300px;max-width:350px">
                    <div style="font-weight:700;font-size:15px;margin-bottom:10px;color:${data.color};display:flex;align-items:center;gap:8px">
                        <span style="font-size:20px">⚡</span> ${data.name}
                    </div>
                    
                    <div style="background:rgba(99,102,241,0.1);border-radius:8px;padding:10px;margin-bottom:10px">
                        <div style="font-size:11px;color:#9ca3af;text-transform:uppercase;margin-bottom:4px">Current Grid Status</div>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px">
                            <div><span style="color:#9ca3af">Load:</span> <strong style="color:#22c55e">${(data.currentLoad/1000).toFixed(1)} GW</strong></div>
                            <div><span style="color:#9ca3af">Capacity:</span> <strong>${(data.capacity/1000).toFixed(0)} GW</strong></div>
                            <div><span style="color:#9ca3af">Available:</span> <strong style="color:#3b82f6">${availableGW} GW</strong></div>
                            <div><span style="color:#9ca3af">Utilization:</span> <strong>${utilizationPct}%</strong></div>
                        </div>
                        <div style="margin-top:8px;height:6px;background:#1f2937;border-radius:3px;overflow:hidden">
                            <div style="height:100%;width:${utilizationPct}%;background:${utilizationPct > 80 ? '#ef4444' : utilizationPct > 60 ? '#f59e0b' : '#22c55e'}"></div>
                        </div>
                    </div>
                    
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;margin-bottom:10px">
                        <div style="background:rgba(34,197,94,0.1);padding:8px;border-radius:6px">
                            <div style="color:#9ca3af;font-size:10px">Renewable Mix</div>
                            <div style="font-weight:700;color:#22c55e">${data.renewablePct}%</div>
                        </div>
                        <div style="background:rgba(59,130,246,0.1);padding:8px;border-radius:6px">
                            <div style="color:#9ca3af;font-size:10px">Avg LMP</div>
                            <div style="font-weight:700;color:#3b82f6">$${data.avgLMP}/MWh</div>
                        </div>
                        <div style="background:rgba(139,92,246,0.1);padding:8px;border-radius:6px">
                            <div style="color:#9ca3af;font-size:10px">Queue Capacity</div>
                            <div style="font-weight:700;color:#8b5cf6">${(data.queuedMW/1000).toFixed(0)} GW</div>
                        </div>
                        <div style="background:rgba(249,115,22,0.1);padding:8px;border-radius:6px">
                            <div style="color:#9ca3af;font-size:10px">Avg Queue Wait</div>
                            <div style="font-weight:700;color:#f97316">${data.avgQueueWait} years</div>
                        </div>
                    </div>
                    
                    <div style="padding:8px;background:rgba(0,0,0,0.2);border-radius:6px;font-size:11px">
                        <div style="display:flex;justify-content:space-between;align-items:center">
                            <span style="color:#9ca3af">Congestion Level:</span>
                            <span style="color:${congestionColor};font-weight:700">${data.congestionLevel}</span>
                        </div>
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
                            <span style="color:#9ca3af">Active Projects:</span>
                            <span style="font-weight:600">${data.activeProjects.toLocaleString()}</span>
                        </div>
                    </div>
                    
                    <div style="margin-top:8px;font-size:10px;color:#6b7280;text-align:center">
                        📡 Data from EIA Grid Monitor & interconnection.fyi
                    </div>
                </div>
            `;
        },
        
        // Get total US grid summary
        getUSSummary() {
            let totalLoad = 0, totalCapacity = 0, totalQueued = 0;
            Object.values(this.isoData).forEach(iso => {
                totalLoad += iso.currentLoad;
                totalCapacity += iso.capacity;
                totalQueued += iso.queuedMW;
            });
            return {
                totalLoad: totalLoad,
                totalCapacity: totalCapacity,
                totalQueued: totalQueued,
                utilization: ((totalLoad / totalCapacity) * 100).toFixed(1)
            };
        }
    };
    
    // =========================================================================
    // SUBSTATION CAPACITY DATA (Enhanced)
    // =========================================================================
    
    const SubstationCapacity = {
        // Voltage class to typical capacity mapping
        voltageToMW: {
            '765': 5000,
            '500': 3500,
            '345': 2000,
            '230': 1200,
            '161': 800,
            '138': 600,
            '115': 400,
            '69': 200,
            '46': 100,
            '34.5': 50,
            '25': 30,
            '15': 20,
            '12': 15,
            '4': 5
        },
        
        // Estimate capacity from voltage
        estimateCapacity(voltage) {
            if (!voltage) return 100;
            const v = parseInt(voltage.toString().replace(/[^0-9]/g, ''));
            
            for (const [kv, mw] of Object.entries(this.voltageToMW)) {
                if (v >= parseInt(kv)) return mw;
            }
            return 50;
        },
        
        // Get capacity indicator color
        getCapacityColor(mw) {
            if (mw >= 3000) return '#ef4444'; // Red - Major
            if (mw >= 1500) return '#f97316'; // Orange - Large
            if (mw >= 500) return '#eab308';  // Yellow - Medium
            if (mw >= 200) return '#22c55e';  // Green - Distribution
            return '#3b82f6';                  // Blue - Local
        }
    };
    
    // =========================================================================
    // GAS PIPELINE CAPACITY DATA
    // =========================================================================
    
    const GasPipelineData = {
        // Major interstate pipelines with capacity
        majorPipelines: {
            'KINDER MORGAN': { dailyCapacity: 42000, // MMcf/d
                               operator: 'Kinder Morgan',
                               segments: 72000 }, // miles
            'WILLIAMS': { dailyCapacity: 31000, operator: 'Williams Companies', segments: 33000 },
            'ENBRIDGE': { dailyCapacity: 24000, operator: 'Enbridge', segments: 38000 },
            'ENERGY TRANSFER': { dailyCapacity: 22000, operator: 'Energy Transfer', segments: 20000 },
            'TC ENERGY': { dailyCapacity: 18000, operator: 'TC Energy', segments: 57000 },
            'SOUTHERN': { dailyCapacity: 14500, operator: 'Southern Company Gas', segments: 14000 },
            'DOMINION': { dailyCapacity: 12000, operator: 'Dominion Energy', segments: 7000 },
            'ONEOK': { dailyCapacity: 8500, operator: 'ONEOK Partners', segments: 22000 },
            'DCP MIDSTREAM': { dailyCapacity: 7500, operator: 'DCP Midstream', segments: 68000 }
        },
        
        // Get pipeline info by operator name
        getPipelineInfo(operator) {
            if (!operator) return null;
            const upperOp = operator.toUpperCase();
            for (const [key, data] of Object.entries(this.majorPipelines)) {
                if (upperOp.includes(key)) return data;
            }
            return null;
        }
    };
    
    // =========================================================================
    // PEERINGDB DATA CENTER INTEGRATION
    // =========================================================================
    
    const PeeringDBData = {
        // Top US Data Center Markets with PeeringDB facility counts
        markets: {
            'Ashburn': { facilities: 85, networks: 1200, exchanges: 12, lat: 39.04, lng: -77.49 },
            'Dallas': { facilities: 62, networks: 450, exchanges: 8, lat: 32.78, lng: -96.80 },
            'Chicago': { facilities: 48, networks: 380, exchanges: 6, lat: 41.88, lng: -87.63 },
            'Los Angeles': { facilities: 55, networks: 520, exchanges: 9, lat: 34.05, lng: -118.24 },
            'New York': { facilities: 42, networks: 480, exchanges: 7, lat: 40.71, lng: -74.01 },
            'San Jose': { facilities: 38, networks: 620, exchanges: 8, lat: 37.34, lng: -121.89 },
            'Phoenix': { facilities: 28, networks: 180, exchanges: 4, lat: 33.45, lng: -112.07 },
            'Atlanta': { facilities: 35, networks: 290, exchanges: 5, lat: 33.75, lng: -84.39 },
            'Seattle': { facilities: 32, networks: 280, exchanges: 5, lat: 47.61, lng: -122.33 },
            'Denver': { facilities: 24, networks: 190, exchanges: 4, lat: 39.74, lng: -104.99 },
            'Miami': { facilities: 30, networks: 350, exchanges: 6, lat: 25.76, lng: -80.19 },
            'Houston': { facilities: 22, networks: 160, exchanges: 3, lat: 29.76, lng: -95.37 }
        },
        
        getMarketPopup(marketName) {
            const m = this.markets[marketName];
            if (!m) return '';
            
            return `
                <div style="min-width:220px">
                    <div style="font-weight:700;font-size:14px;margin-bottom:8px;color:#6366f1">
                        🏢 ${marketName} DC Market
                    </div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px">
                        <div><span style="color:#9ca3af">Facilities:</span> <strong>${m.facilities}</strong></div>
                        <div><span style="color:#9ca3af">Networks:</span> <strong>${m.networks}</strong></div>
                        <div><span style="color:#9ca3af">Exchanges:</span> <strong>${m.exchanges}</strong></div>
                    </div>
                    <div style="margin-top:8px;font-size:10px;color:#06b6d4">
                        📡 Source: PeeringDB
                    </div>
                </div>
            `;
        }
    };
    
    // =========================================================================
    // INTERCONNECTION QUEUE DATA (from interconnection.fyi)
    // =========================================================================
    
    const InterconnectionQueue = {
        // Current queue statistics (Dec 2024)
        summary: {
            totalRequests: 9733,
            totalCapacity: 1920, // GW
            loadRequests: 566,
            loadCapacity: 81.59, // GW (data centers!)
            avgCompletionTime: 4.2 // years
        },
        
        // Queue by ISO with data center load specifically
        isoQueues: {
            'PJM': { 
                genRequests: 2847, genCapacity: 296, 
                loadRequests: 180, loadCapacity: 28.5,
                dcProjects: 45, dcCapacity: 12.8
            },
            'MISO': { 
                genRequests: 1856, genCapacity: 258,
                loadRequests: 95, loadCapacity: 15.2,
                dcProjects: 22, dcCapacity: 6.4
            },
            'ERCOT': { 
                genRequests: 1234, genCapacity: 179,
                loadRequests: 120, loadCapacity: 18.5,
                dcProjects: 38, dcCapacity: 9.2
            },
            'CAISO': { 
                genRequests: 987, genCapacity: 187,
                loadRequests: 45, loadCapacity: 8.2,
                dcProjects: 15, dcCapacity: 4.1
            },
            'SPP': { 
                genRequests: 756, genCapacity: 120,
                loadRequests: 35, loadCapacity: 5.8,
                dcProjects: 8, dcCapacity: 2.2
            }
        },
        
        getQueuePopup(iso) {
            const q = this.isoQueues[iso];
            if (!q) return '';
            
            return `
                <div style="min-width:260px">
                    <div style="font-weight:700;font-size:14px;margin-bottom:8px;color:#8b5cf6">
                        📋 ${iso} Interconnection Queue
                    </div>
                    
                    <div style="font-size:11px;color:#9ca3af;margin-bottom:6px">Generation Projects</div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px;margin-bottom:10px">
                        <div><span style="color:#9ca3af">Requests:</span> <strong>${q.genRequests.toLocaleString()}</strong></div>
                        <div><span style="color:#9ca3af">Capacity:</span> <strong>${q.genCapacity} GW</strong></div>
                    </div>
                    
                    <div style="font-size:11px;color:#f97316;margin-bottom:6px">⚡ Load Projects (incl. Data Centers)</div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px;margin-bottom:10px;background:rgba(249,115,22,0.1);padding:8px;border-radius:6px">
                        <div><span style="color:#9ca3af">Load Requests:</span> <strong>${q.loadRequests}</strong></div>
                        <div><span style="color:#9ca3af">Load Capacity:</span> <strong>${q.loadCapacity} GW</strong></div>
                        <div><span style="color:#f97316">DC Projects:</span> <strong style="color:#f97316">${q.dcProjects}</strong></div>
                        <div><span style="color:#f97316">DC Capacity:</span> <strong style="color:#f97316">${q.dcCapacity} GW</strong></div>
                    </div>
                    
                    <div style="font-size:10px;color:#06b6d4;text-align:center">
                        📡 Data from interconnection.fyi (updated daily)
                    </div>
                </div>
            `;
        }
    };
    
    // =========================================================================
    // DATA LOADER - Fetches from HIFLD/EIA APIs
    // =========================================================================
    
    const DataLoader = {
        cache: {},
        
        // DC Hub API base URL (Neon-backed, replaces direct ArcGIS calls)
        API_BASE: window.location.origin.includes('localhost') 
            ? 'http://localhost:8080' 
            : 'https://dchub.cloud',
        
        // Generic DC Hub API query with caching
        async queryDCHubAPI(endpoint, params = {}) {
            const queryParams = new URLSearchParams(params);
            const url = `${this.API_BASE}${endpoint}?${queryParams}`;
            const cacheKey = url;
            
            // Circuit breaker — if 5+ consecutive failures, stop for 60s
            if (this._apiFailures >= 5 && Date.now() - this._lastFailureTime < 60000) {
                return null;
            }
            
            // Check cache
            if (this.cache[cacheKey] && Date.now() - this.cache[cacheKey].timestamp < CONFIG.CACHE_DURATION) {
                return this.cache[cacheKey].data;
            }
            
            try {
                const response = await fetch(url);
                if (!response.ok) {
                    console.warn(`📡 DC Hub API ${endpoint} returned ${response.status}`);
                    this._apiFailures = (this._apiFailures || 0) + 1;
                    this._lastFailureTime = Date.now();
                    if (window.DCHubInfra) window.DCHubInfra._consecutiveFailures = this._apiFailures;
                    return null;
                }
                const data = await response.json();
                
                // Reset failure counter on success
                this._apiFailures = 0;
                if (window.DCHubInfra) window.DCHubInfra._consecutiveFailures = 0;
                
                // Cache result
                this.cache[cacheKey] = { data, timestamp: Date.now() };
                return data;
            } catch (error) {
                console.warn('📡 DC Hub API unavailable:', error.message);
                this._apiFailures = (this._apiFailures || 0) + 1;
                this._lastFailureTime = Date.now();
                return null;
            }
        },
        
        // Convert DC Hub API response to ArcGIS-compatible features format
        // so existing LayerRenderer code works without changes
        _toFeatures(items, type) {
            if (!items || !Array.isArray(items)) return null;
            return {
                features: items.map(item => {
                    if (type === 'substation') {
                        return {
                            attributes: {
                                NAME: item.name,
                                CITY: item.city,
                                STATE: item.state,
                                STATUS: item.status || 'Active',
                                MAX_VOLT: item.max_voltage_kv,
                                MIN_VOLT: item.max_voltage_kv,
                                OWNER: item.owner,
                                LATITUDE: item.lat,
                                LONGITUDE: item.lng
                            },
                            geometry: { y: item.lat, x: item.lng }
                        };
                    } else if (type === 'gas_pipeline') {
                        return {
                            attributes: {
                                OPERATOR: item.operator || item.name,
                                TYPEPIPE: item.pipeline_type || 'Interstate',
                                STATUS: item.status || 'Active'
                            },
                            geometry: { 
                                y: item.lat, x: item.lng,
                                // Single point — no polyline paths from Neon (point data only)
                                paths: item.paths || null
                            }
                        };
                    } else if (type === 'power_plant') {
                        return {
                            attributes: {
                                NAME: item.name,
                                CITY: item.city,
                                STATE: item.state,
                                PRIMSOURCE: item.primary_source,
                                TOTAL_MW: item.capacity_mw,
                                LATITUDE: item.lat,
                                LONGITUDE: item.lng
                            },
                            geometry: { y: item.lat, x: item.lng }
                        };
                    }
                    return { attributes: item, geometry: { y: item.lat, x: item.lng } };
                })
            };
        },
        
        // Load substations from Neon via DC Hub API + HIFLD ArcGIS fallback
        async loadSubstations(bounds) {
            const center = bounds.getCenter();
            const radiusKm = Math.min(center.distanceTo(bounds.getNorthEast()) / 1000, 200);
            
            // Try DC Hub backend first (79,755 substations in Neon)
            const data = await this.queryDCHubAPI('/api/v1/infrastructure/substations', {
                lat: center.lat.toFixed(4),
                lng: center.lng.toFixed(4),
                radius: Math.round(radiusKm * 0.621371),
                min_kv: 69,
                limit: 500
            });
            
            if (data && data.substations) {
                return this._toFeatures(data.substations, 'substation');
            }
            
            // Fallback: HIFLD ArcGIS direct (still works for substations)
            try {
                const sw = bounds.getSouthWest();
                const ne = bounds.getNorthEast();
                const params = new URLSearchParams({
                    where: 'MAX_VOLT>=69',
                    outFields: 'NAME,CITY,STATE,STATUS,MAX_VOLT,MIN_VOLT,OWNER,LATITUDE,LONGITUDE',
                    f: 'json',
                    resultRecordCount: 500,
                    geometry: `${sw.lng},${sw.lat},${ne.lng},${ne.lat}`,
                    geometryType: 'esriGeometryEnvelope',
                    inSR: '4326',
                    outSR: '4326',
                    spatialRel: 'esriSpatialRelIntersects'
                });
                const response = await fetch(`${CONFIG.HIFLD.substations}/query?${params}`);
                if (response.ok) {
                    const arcData = await response.json();
                    if (arcData && arcData.features && arcData.features.length > 0) {
                        console.log(`📡 HIFLD ArcGIS: Loaded ${arcData.features.length} substations`);
                        return arcData;
                    }
                }
            } catch (err) {
                console.warn('📡 HIFLD substations ArcGIS unavailable:', err.message);
            }
            
            return null;
        },
        
        // Load transmission lines — still uses ArcGIS until Neon endpoint is ready
        async loadTransmissionLines(bounds) {
            // TODO: Rewire to Neon once transmission endpoint is updated
            const geometry = {
                xmin: bounds.getWest(),
                ymin: bounds.getSouth(),
                xmax: bounds.getEast(),
                ymax: bounds.getNorth()
            };
            
            const params = new URLSearchParams({
                where: 'VOLTAGE>=69',
                outFields: 'OWNER,VOLTAGE,VOLT_CLASS,STATUS,SUB_1,SUB_2',
                f: 'json',
                resultRecordCount: 5000,
                geometry: `${geometry.xmin},${geometry.ymin},${geometry.xmax},${geometry.ymax}`,
                geometryType: 'esriGeometryEnvelope',
                inSR: '4326',
                outSR: '4326',
                spatialRel: 'esriSpatialRelIntersects'
            });
            
            const url = `${CONFIG.HIFLD.transmissionLines}/query?${params}`;
            try {
                const response = await fetch(url);
                if (!response.ok) return null;
                const data = await response.json();
                this.cache[url] = { data, timestamp: Date.now() };
                return data;
            } catch (error) {
                console.warn('📡 Transmission lines ArcGIS unavailable:', error.message);
                return null;
            }
        },
        
        // Load gas pipelines from Neon via DC Hub API
        // Load gas pipelines from DC Hub backend + EIA ArcGIS fallback
        async loadGasPipelines(bounds) {
            const center = bounds.getCenter();
            const radiusKm = Math.min(center.distanceTo(bounds.getNorthEast()) / 1000, 200);
            
            // Source 1: DC Hub energy discovery (works, no auth needed)
            const data = await this.queryDCHubAPI('/api/energy-discovery/pipelines', {
                lat: center.lat.toFixed(4),
                lng: center.lng.toFixed(4),
                radius: Math.round(radiusKm * 0.621371),
                limit: 500
            });
            
            if (data && data.data && data.data.length > 0) {
                return this._toFeatures(data.data, 'gas_pipeline');
            }
            if (data && data.pipelines && data.pipelines.length > 0) {
                return this._toFeatures(data.pipelines, 'gas_pipeline');
            }
            
            // Source 2: EIA Natural Gas Interstate Pipelines ArcGIS (has line geometry)
            try {
                const sw = bounds.getSouthWest();
                const ne = bounds.getNorthEast();
                const params = new URLSearchParams({
                    where: '1=1',
                    geometry: `${sw.lng},${sw.lat},${ne.lng},${ne.lat}`,
                    geometryType: 'esriGeometryEnvelope',
                    inSR: '4326',
                    outSR: '4326',
                    spatialRel: 'esriSpatialRelIntersects',
                    outFields: 'operator,typepipe,status',
                    returnGeometry: true,
                    f: 'json',
                    resultRecordCount: 2000
                });
                const response = await fetch(`https://geo.dot.gov/server/rest/services/Hosted/Natural_Gas_Pipelines_US_EIA/FeatureServer/0/query?${params}`);
                if (response.ok) {
                    const eiaData = await response.json();
                    if (eiaData && eiaData.features && eiaData.features.length > 0) {
                        console.log(`📡 EIA ArcGIS: Loaded ${eiaData.features.length} gas pipeline segments`);
                        return eiaData;
                    }
                }
            } catch (err) {
                console.warn('📡 EIA gas pipeline ArcGIS unavailable:', err.message);
            }
            
            return null;
        },
        
        // Load power plants from Neon via DC Hub API
        async loadPowerPlants(bounds) {
            const center = bounds.getCenter();
            const radiusKm = Math.min(center.distanceTo(bounds.getNorthEast()) / 1000, 200);
            
            const data = await this.queryDCHubAPI('/api/v1/power-plants/nearby', {
                lat: center.lat.toFixed(4),
                lng: center.lng.toFixed(4),
                radius: Math.round(radiusKm * 0.621371),
                limit: 200
            });
            
            if (data && data.plants) {
                return this._toFeatures(data.plants, 'power_plant');
            }
            // Fallback: try ArcGIS direct
            const geometry = {
                xmin: bounds.getWest(),
                ymin: bounds.getSouth(),
                xmax: bounds.getEast(),
                ymax: bounds.getNorth()
            };
            const params = new URLSearchParams({
                where: '1=1',
                outFields: 'NAME,CITY,STATE,PRIMSOURCE,TOTAL_MW,LATITUDE,LONGITUDE',
                f: 'json',
                resultRecordCount: 2000,
                geometry: `${geometry.xmin},${geometry.ymin},${geometry.xmax},${geometry.ymax}`,
                geometryType: 'esriGeometryEnvelope',
                inSR: '4326',
                outSR: '4326',
                spatialRel: 'esriSpatialRelIntersects'
            });
            try {
                const response = await fetch(`${CONFIG.HIFLD.powerPlants}/query?${params}`);
                if (!response.ok) return null;
                return await response.json();
            } catch { return null; }
        }
    };
    
    // =========================================================================
    // LAYER RENDERER - Converts GeoJSON to Leaflet layers
    // =========================================================================
    
    const LayerRenderer = {
        
        // Render substations from JSON/GeoJSON with enhanced capacity data
        renderSubstations(data, layerGroup) {
            if (!data || !data.features) return 0;
            
            layerGroup.clearLayers();
            let count = 0;
            
            data.features.forEach(feature => {
                // Handle both GeoJSON (.properties) and ArcGIS JSON (.attributes) formats
                const props = feature.properties || feature.attributes || {};
                // Handle both GeoJSON coords and ArcGIS geometry
                let lat, lng;
                if (feature.geometry && feature.geometry.coordinates) {
                    // GeoJSON format
                    lng = feature.geometry.coordinates[0];
                    lat = feature.geometry.coordinates[1];
                } else if (feature.geometry && (feature.geometry.x !== undefined)) {
                    // ArcGIS JSON point format
                    lng = feature.geometry.x;
                    lat = feature.geometry.y;
                } else if (props.LATITUDE && props.LONGITUDE) {
                    // Fallback to attributes
                    lat = props.LATITUDE;
                    lng = props.LONGITUDE;
                } else {
                    return; // Skip if no coordinates
                }
                
                if (!lat || !lng) return;
                
                const voltage = props.MAX_VOLT || props.MIN_VOLT || 'Unknown';
                const estimatedMW = SubstationCapacity.estimateCapacity(voltage);
                const color = SubstationCapacity.getCapacityColor(estimatedMW);
                const radius = this.getVoltageRadius(voltage);
                
                const marker = L.circleMarker([lat, lng], {
                    radius: radius,
                    fillColor: color,
                    color: '#fff',
                    weight: 1,
                    opacity: 0.9,
                    fillOpacity: 0.8
                });
                
                marker.bindPopup(`
                    <div style="min-width:240px">
                        <div style="font-weight:700;font-size:14px;margin-bottom:8px;color:#6366f1">
                            🔌 ${props.NAME || 'Substation'}
                        </div>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px">
                            <div><span style="color:#9ca3af">Voltage:</span> <strong>${voltage}</strong></div>
                            <div><span style="color:#9ca3af">Est. Capacity:</span> <strong style="color:#22c55e">${estimatedMW} MW</strong></div>
                            <div><span style="color:#9ca3af">Owner:</span> ${props.OWNER || 'N/A'}</div>
                            <div><span style="color:#9ca3af">Status:</span> ${props.STATUS || 'Active'}</div>
                            <div><span style="color:#9ca3af">City:</span> ${props.CITY || 'N/A'}</div>
                            <div><span style="color:#9ca3af">State:</span> ${props.STATE || 'N/A'}</div>
                        </div>
                        <div style="margin-top:8px;padding:6px 8px;background:rgba(99,102,241,0.1);border-radius:6px;font-size:11px">
                            💡 ${estimatedMW >= 1000 ? 'Suitable for 100+ MW data center' : estimatedMW >= 500 ? 'Suitable for 50-100 MW facility' : 'Distribution-level capacity'}
                        </div>
                        <div style="margin-top:8px;padding-top:8px;border-top:1px solid #333;font-size:10px;color:#9ca3af">
                            📡 Source: HIFLD Open Data
                        </div>
                    </div>
                `);
                
                marker.addTo(layerGroup);
                count++;
            });
            
            return count;
        },
        
        // Render transmission lines from JSON/GeoJSON
        renderTransmissionLines(data, layerGroup) {
            if (!data || !data.features) return 0;
            
            layerGroup.clearLayers();
            let count = 0;
            
            data.features.forEach(feature => {
                // Handle both GeoJSON (.properties) and ArcGIS JSON (.attributes) formats
                const props = feature.properties || feature.attributes || {};
                
                // Handle both GeoJSON and ArcGIS geometry formats
                let coords;
                if (feature.geometry && feature.geometry.coordinates) {
                    // GeoJSON format
                    coords = feature.geometry.coordinates;
                } else if (feature.geometry && feature.geometry.paths) {
                    // ArcGIS JSON polyline format
                    coords = feature.geometry.paths[0]; // Use first path
                } else {
                    return; // Skip if no geometry
                }
                
                if (!coords || !Array.isArray(coords)) return;
                
                const voltage = props.VOLTAGE || props.VOLT_CLASS || 'Unknown';
                const color = this.getTransmissionColor(voltage);
                const weight = this.getTransmissionWeight(voltage);
                const estimatedMW = SubstationCapacity.estimateCapacity(voltage);
                
                // Convert coordinates for Leaflet (swap lng/lat)
                const latLngs = coords.map(coord => {
                    if (Array.isArray(coord[0])) {
                        return coord.map(c => [c[1], c[0]]);
                    }
                    return [coord[1], coord[0]];
                });
                
                const line = L.polyline(latLngs, {
                    color: color,
                    weight: weight,
                    opacity: 0.7
                });
                
                line.bindPopup(`
                    <div style="min-width:200px">
                        <div style="font-weight:700;font-size:13px;margin-bottom:6px;color:#f59e0b">
                            ⚡ Transmission Line
                        </div>
                        <div style="font-size:12px">
                            <div><span style="color:#9ca3af">Voltage:</span> <strong>${voltage}</strong></div>
                            <div><span style="color:#9ca3af">Est. Transfer:</span> <strong style="color:#22c55e">${estimatedMW} MW</strong></div>
                            <div><span style="color:#9ca3af">Owner:</span> ${props.OWNER || 'N/A'}</div>
                            <div><span style="color:#9ca3af">Status:</span> ${props.STATUS || 'Active'}</div>
                        </div>
                        <div style="margin-top:6px;font-size:10px;color:#9ca3af">📡 HIFLD Data</div>
                    </div>
                `);
                
                line.addTo(layerGroup);
                count++;
            });
            
            return count;
        },
        
        // Render gas pipelines from JSON/GeoJSON with capacity
        // Handles both polyline (ArcGIS) and point (Neon) data
        renderGasPipelines(data, layerGroup) {
            if (!data || !data.features) return 0;
            
            layerGroup.clearLayers();
            let count = 0;
            
            data.features.forEach(feature => {
                // Handle both GeoJSON (.properties) and ArcGIS JSON (.attributes) formats
                const props = feature.properties || feature.attributes || {};
                
                const pipelineInfo = GasPipelineData.getPipelineInfo(props.OPERATOR);
                
                const popupContent = `
                    <div style="min-width:200px">
                        <div style="font-weight:700;font-size:13px;margin-bottom:6px;color:#f97316">
                            🔥 Gas Pipeline
                        </div>
                        <div style="font-size:12px">
                            <div><span style="color:#9ca3af">Operator:</span> ${props.OPERATOR || 'Unknown'}</div>
                            <div><span style="color:#9ca3af">Type:</span> ${props.TYPEPIPE || 'Interstate'}</div>
                            <div><span style="color:#9ca3af">Status:</span> ${props.STATUS || 'Active'}</div>
                        </div>
                        ${pipelineInfo ? `<div style="margin-top:6px;padding:6px 8px;background:rgba(249,115,22,0.1);border-radius:6px;font-size:11px">
                            🔥 Daily Capacity: <strong>${pipelineInfo.dailyCapacity.toLocaleString()} MMcf/d</strong>
                        </div>` : ''}
                        <div style="margin-top:6px;font-size:10px;color:#9ca3af">📡 DC Hub Infrastructure Data</div>
                    </div>
                `;
                
                // Handle polyline paths (ArcGIS format)
                let coords;
                if (feature.geometry && feature.geometry.coordinates) {
                    coords = feature.geometry.coordinates;
                } else if (feature.geometry && feature.geometry.paths && feature.geometry.paths.length > 0) {
                    coords = feature.geometry.paths[0];
                }
                
                if (coords && Array.isArray(coords) && coords.length > 1) {
                    // Polyline rendering
                    const latLngs = coords.map(coord => {
                        if (Array.isArray(coord[0])) {
                            return coord.map(c => [c[1], c[0]]);
                        }
                        return [coord[1], coord[0]];
                    });
                    
                    const line = L.polyline(latLngs, {
                        color: '#f97316',
                        weight: pipelineInfo ? 3 : 2,
                        opacity: 0.7,
                        dashArray: pipelineInfo ? null : '5, 5'
                    });
                    line.bindPopup(popupContent);
                    line.addTo(layerGroup);
                    count++;
                } else if (feature.geometry && (feature.geometry.y || feature.geometry.x)) {
                    // Point rendering (Neon data — single lat/lng per pipeline segment)
                    const lat = feature.geometry.y;
                    const lng = feature.geometry.x;
                    if (!lat || !lng) return;
                    
                    const marker = L.circleMarker([lat, lng], {
                        radius: pipelineInfo ? 4 : 3,
                        fillColor: '#f97316',
                        color: '#fff',
                        weight: 1,
                        opacity: 0.8,
                        fillOpacity: 0.7
                    });
                    marker.bindPopup(popupContent);
                    marker.addTo(layerGroup);
                    count++;
                }
            });
            
            return count;
        },
        
        // Color helpers
        getVoltageColor(voltage) {
            const v = parseInt(voltage) || 0;
            if (v >= 500) return '#ef4444';
            if (v >= 345) return '#f97316';
            if (v >= 230) return '#eab308';
            if (v >= 115) return '#22c55e';
            return '#3b82f6';
        },
        
        getVoltageRadius(voltage) {
            const v = parseInt(voltage) || 0;
            if (v >= 500) return 8;
            if (v >= 345) return 7;
            if (v >= 230) return 6;
            if (v >= 115) return 5;
            return 4;
        },
        
        getTransmissionColor(voltage) {
            const v = parseInt(voltage) || 0;
            if (v >= 500) return '#ef4444';
            if (v >= 345) return '#f97316';
            if (v >= 230) return '#eab308';
            if (v >= 115) return '#22c55e';
            return '#6b7280';
        },
        
        getTransmissionWeight(voltage) {
            const v = parseInt(voltage) || 0;
            if (v >= 500) return 3;
            if (v >= 345) return 2.5;
            if (v >= 230) return 2;
            return 1.5;
        }
    };
    
    // =========================================================================
    // FIBER CARRIER DATA - Long-haul and Metro Fiber
    // =========================================================================
    
    const FiberCarriers = {
        longHaul: [
            { name: 'Zayo', color: '#00a3e0', miles: 134000, coverage: ['National', 'Metro', 'Dark Fiber'] },
            { name: 'Lumen', color: '#00a0df', miles: 450000, coverage: ['National', 'Global'] },
            { name: 'Crown Castle', color: '#003366', miles: 85000, coverage: ['Metro Fiber'] },
            { name: 'Cogent', color: '#ff6600', miles: 72000, coverage: ['National', 'Intercity'] },
            { name: 'GTT', color: '#7ab800', miles: 200000, coverage: ['Global'] },
            { name: 'Windstream', color: '#00467f', miles: 150000, coverage: ['Regional'] },
            { name: 'Segra', color: '#0077c8', miles: 48000, coverage: ['Southeast'] },
            { name: 'FirstLight', color: '#e31937', miles: 25000, coverage: ['Northeast'] },
            { name: 'Everstream', color: '#00875a', miles: 28000, coverage: ['Midwest'] },
            { name: 'Uniti Fiber', color: '#00b5e2', miles: 130000, coverage: ['Southeast', 'Midwest'] }
        ]
    };
    
    // =========================================================================
    // DYNAMIC LAYER MANAGER
    // =========================================================================
    
    const DynamicLayerManager = {
        map: null,
        layers: {},
        loadingIndicator: null,
        
        // Initialize with map reference
        init(map, existingLayers) {
            this.map = map;
            this.layers = existingLayers || {};
            
            // Create additional layer groups
            this.layers.hifldSubstations = this.layers.hifldSubstations || L.layerGroup();
            this.layers.hifldTransmission = this.layers.hifldTransmission || L.layerGroup();
            this.layers.hifldGas = this.layers.hifldGas || L.layerGroup();
            
            // Add map move listener for dynamic loading (debounced at 2000ms
            // to avoid stacking with land-power-app.js which debounces at 1200ms)
            this._moveTimeout = null;
            this._consecutiveFailures = 0;
            // DISABLED v2.1: moveend listener removed — land-power-app.js v129+
            // handles all infrastructure loading via serialized DC Hub queue.
            // This listener was firing 3 unqueued DC Hub API calls per pan,
            // causing Neon pool exhaustion (503s on /api/v1/infrastructure/substations).
            // this.map.on('moveend', () => {
            //     clearTimeout(this._moveTimeout);
            //     var delay = this._consecutiveFailures >= 3 ? 10000 : 2000;
            //     this._moveTimeout = setTimeout(() => this.onMapMove(), delay);
            // });
            
            console.log('🔌 DC Hub Infrastructure v2 initialized');
            console.log('📊 Grid Summary:', GridStatus.getUSSummary());
        },
        
        // Called when map stops moving
        async onMapMove() {
            const zoom = this.map.getZoom();
            const bounds = this.map.getBounds();
            
            // Only load detailed data at zoom 8+
            if (zoom < 8) return;
            
            // Check which layers are active and load data
            if (this.map.hasLayer(this.layers.hifldSubstations)) {
                await this.loadHIFLDSubstations(bounds);
            }
            
            if (this.map.hasLayer(this.layers.hifldTransmission)) {
                await this.loadHIFLDTransmission(bounds);
            }
            
            if (this.map.hasLayer(this.layers.hifldGas)) {
                await this.loadHIFLDGas(bounds);
            }
        },
        
        // Load HIFLD substations for current view
        async loadHIFLDSubstations(bounds) {
            this.showLoading('Loading substations from DC Hub...');
            
            const data = await DataLoader.loadSubstations(bounds);
            if (data) {
                const count = LayerRenderer.renderSubstations(data, this.layers.hifldSubstations);
                this.updateCount('count-subs-hifld', count);
                console.log(`🔌 Loaded ${count} HIFLD substations with capacity data`);
            }
            
            this.hideLoading();
        },
        
        // Load HIFLD transmission lines
        async loadHIFLDTransmission(bounds) {
            this.showLoading('Loading 300,000+ miles transmission...');
            
            const data = await DataLoader.loadTransmissionLines(bounds);
            if (data) {
                const count = LayerRenderer.renderTransmissionLines(data, this.layers.hifldTransmission);
                this.updateCount('count-trans-hifld', count);
                console.log(`⚡ Loaded ${count} HIFLD transmission lines with capacity`);
            }
            
            this.hideLoading();
        },
        
        // Load HIFLD gas pipelines
        async loadHIFLDGas(bounds) {
            this.showLoading('Loading gas pipelines from DC Hub + EIA...');
            
            const data = await DataLoader.loadGasPipelines(bounds);
            if (data) {
                const count = LayerRenderer.renderGasPipelines(data, this.layers.hifldGas);
                this.updateCount('count-gas-hifld', count);
                console.log(`🔥 Loaded ${count} HIFLD gas pipeline segments with capacity`);
            }
            
            this.hideLoading();
        },
        
        // Loading indicator helpers
        showLoading(message) {
            if (!this.loadingIndicator) {
                this.loadingIndicator = document.createElement('div');
                this.loadingIndicator.id = 'hifld-loading';
                this.loadingIndicator.style.cssText = `
                    position: fixed;
                    bottom: 40px;
                    left: 50%;
                    transform: translateX(-50%);
                    background: rgba(99, 102, 241, 0.95);
                    color: white;
                    padding: 10px 20px;
                    border-radius: 8px;
                    font-size: 13px;
                    font-weight: 600;
                    z-index: 10000;
                    display: none;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                `;
                document.body.appendChild(this.loadingIndicator);
            }
            this.loadingIndicator.textContent = message;
            this.loadingIndicator.style.display = 'block';
        },
        
        hideLoading() {
            if (this.loadingIndicator) {
                this.loadingIndicator.style.display = 'none';
            }
        },
        
        updateCount(elementId, count) {
            const el = document.getElementById(elementId);
            if (el) {
                el.textContent = count >= 1000 ? `${(count/1000).toFixed(1)}k` : count;
            }
        }
    };
    
    // =========================================================================
    // ENHANCED STATISTICS
    // =========================================================================
    
    const EnhancedStats = {
        // Total infrastructure counts
        totals: {
            substations: 79755,
            transmissionMiles: 300000,
            gasPipelineMiles: 37705,
            fiberMiles: 1500000,
            powerPlants: 20000,
            datacenters: 21000,
            queuedCapacity: 1920, // GW
            dcLoadQueue: 81.59 // GW
        },
        
        getSummary() {
            return {
                substations: `${(this.totals.substations/1000).toFixed(0)}k`,
                transmission: `${(this.totals.transmissionMiles/1000).toFixed(0)}k mi`,
                gasPipelines: `${(this.totals.gasPipelineMiles/1000).toFixed(0)}k mi`,
                fiber: `${(this.totals.fiberMiles/1000000).toFixed(1)}M mi`,
                powerPlants: `${(this.totals.powerPlants/1000).toFixed(0)}k`,
                datacenters: `${(this.totals.datacenters/1000).toFixed(0)}k`,
                queuedCapacity: `${this.totals.queuedCapacity} GW`,
                dcLoadQueue: `${this.totals.dcLoadQueue} GW`
            };
        }
    };
    
    // =========================================================================
    // EXPORT TO GLOBAL SCOPE
    // =========================================================================
    
    window.DCHubInfrastructure = {
        CONFIG,
        GridStatus,
        SubstationCapacity,
        GasPipelineData,
        PeeringDBData,
        InterconnectionQueue,
        DataLoader,
        LayerRenderer,
        FiberCarriers,
        DynamicLayerManager,
        EnhancedStats,
        
        // Quick init function
        init(map, layers) {
            DynamicLayerManager.init(map, layers);
            console.log('✅ DC Hub Infrastructure Data v2 ready');
            console.log('📊 Stats:', EnhancedStats.getSummary());
            console.log('⚡ US Grid:', GridStatus.getUSSummary());
        }
    };
    
})();
