/**
 * DC Hub Energy Infrastructure Enhancement v1
 * ============================================
 * 
 * INSTALLATION:
 * 1. Copy this file to your Cloudflare project: js/energy-infrastructure.js
 * 2. Add this script tag to land-power.html BEFORE land-power-app.js:
 *    <script src="js/energy-infrastructure.js"></script>
 * 
 * This module adds:
 * - Enhanced site scoring with gas pipeline proximity
 * - DOT/EIA pipeline layer
 * - Texas RRC pipeline layer
 * - State oil/gas well layers
 * - Tallgrass pipeline visibility
 * 
 * All features work alongside existing HIFLD integration.
 */

(function() {
    'use strict';
    
    // ==========================================================================
    // CONFIGURATION
    // ==========================================================================
    
    const CONFIG = {
        // API base - your Replit backend
        API_BASE: 'https://dchub.cloud',
        
        // Fallback to direct API if backend unavailable
        DIRECT_API: {
            dotPipelines: 'https://geo.dot.gov/server/rest/services/Hosted/Natural_Gas_Pipelines_US_EIA/FeatureServer/0/query',
            texasRRC: 'https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer/0/query'
        },
        
        // Cache duration in ms
        CACHE_DURATION: 5 * 60 * 1000, // 5 minutes
        
        // Colors for pipeline display
        COLORS: {
            interstate: '#ef4444',      // Red for interstate gas
            intrastate: '#f97316',      // Orange for intrastate
            tallgrass: '#22c55e',       // Green for Tallgrass
            gas: '#eab308',             // Yellow for generic gas
            oil: '#1f2937',             // Dark for oil
            wells: '#6366f1'            // Purple for wells
        }
    };
    
    // ==========================================================================
    // CACHE MANAGEMENT
    // ==========================================================================
    
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
        },
        
        clear() {
            this._cache = {};
        }
    };
    
    // ==========================================================================
    // API FUNCTIONS
    // ==========================================================================
    
    /**
     * Fetch from backend API with fallback
     */
    async function fetchAPI(endpoint, params = {}) {
        const url = new URL(`${CONFIG.API_BASE}${endpoint}`);
        Object.entries(params).forEach(([k, v]) => url.searchParams.append(k, v));
        
        try {
            const response = await fetch(url.toString(), { timeout: 15000 });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return await response.json();
        } catch (error) {
            console.warn(`⚠️ Backend API failed: ${error.message}`);
            return null;
        }
    }
    
    /**
     * Direct ArcGIS query (fallback)
     */
    async function queryArcGIS(url, params = {}) {
        const defaultParams = {
            f: 'json',
            inSR: '4326',
            outSR: '4326',
            returnGeometry: 'true',
            resultRecordCount: '1000'
        };
        
        const queryParams = new URLSearchParams({ ...defaultParams, ...params });
        
        try {
            const response = await fetch(`${url}?${queryParams}`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return await response.json();
        } catch (error) {
            console.warn(`⚠️ ArcGIS query failed: ${error.message}`);
            return { features: [] };
        }
    }
    
    // ==========================================================================
    // ENHANCED SITE ANALYSIS
    // ==========================================================================
    
    /**
     * Enhanced site analysis with gas infrastructure scoring
     * Call this after the standard evaluateSite for additional insights
     */
    async function enhancedSiteAnalysis(lat, lng, radius = 25000) {
        console.log('🔌 Running enhanced energy infrastructure analysis...');
        
        const cacheKey = `site-${lat.toFixed(4)}-${lng.toFixed(4)}-${radius}`;
        const cached = DataCache.get(cacheKey);
        if (cached) {
            console.log('📦 Using cached analysis');
            return cached;
        }
        
        // Try backend first
        let result = await fetchAPI('/api/v1/energy/site-analysis', { lat, lng, radius });
        
        if (result && result.success) {
            console.log('✅ Backend analysis complete');
            DataCache.set(cacheKey, result.data);
            return result.data;
        }
        
        // Fallback: direct API queries
        console.log('⚠️ Using direct API fallback...');
        
        const bounds = getBoundsFromPoint(lat, lng, radius);
        const envelope = `${bounds.minLng},${bounds.minLat},${bounds.maxLng},${bounds.maxLat}`;
        
        // Query DOT pipelines
        const pipelineData = await queryArcGIS(CONFIG.DIRECT_API.dotPipelines, {
            where: '1=1',
            geometry: envelope,
            geometryType: 'esriGeometryEnvelope',
            spatialRel: 'esriSpatialRelIntersects',
            outFields: 'typepipe,operator,status'
        });
        
        // Calculate gas score
        const gasScore = calculateGasScore(lat, lng, pipelineData.features || []);
        
        const analysisResult = {
            location: { lat, lng },
            radius,
            scores: {
                gasScore: gasScore.score,
                overallScore: gasScore.score, // Will be combined with existing power score
                recommendations: gasScore.recommendations
            },
            counts: {
                pipelines: (pipelineData.features || []).length
            },
            infrastructure: {
                pipelines: (pipelineData.features || []).slice(0, 20)
            }
        };
        
        DataCache.set(cacheKey, analysisResult);
        return analysisResult;
    }
    
    /**
     * Calculate gas infrastructure score
     */
    function calculateGasScore(lat, lng, pipelines) {
        let score = 0;
        const recommendations = [];
        
        if (!pipelines || pipelines.length === 0) {
            recommendations.push('ℹ️ No gas pipelines found in search area');
            return { score: 0, recommendations };
        }
        
        // Find nearest pipeline
        let nearestDist = Infinity;
        let nearestPipeline = null;
        let hasTallgrass = false;
        let hasInterstate = false;
        
        pipelines.forEach(p => {
            const attrs = p.attributes || {};
            const paths = p.geometry?.paths || [];
            
            // Check for Tallgrass
            const operator = (attrs.operator || '').toLowerCase();
            if (operator.includes('tallgrass') || operator.includes('rockies express') || operator.includes('rex')) {
                hasTallgrass = true;
            }
            
            // Check type
            if (attrs.typepipe === 'interstate') {
                hasInterstate = true;
            }
            
            // Calculate distance to first point of pipeline
            if (paths[0] && paths[0][0]) {
                const [pLng, pLat] = paths[0][0];
                const dist = getDistanceMeters(lat, lng, pLat, pLng);
                if (dist < nearestDist) {
                    nearestDist = dist;
                    nearestPipeline = p;
                }
            }
        });
        
        const nearestKm = nearestDist / 1000;
        
        // Score based on distance
        if (nearestKm < 5) {
            score += 40;
            recommendations.push(`✅ Gas pipeline within ${nearestKm.toFixed(1)}km - excellent for gas-powered DC`);
        } else if (nearestKm < 10) {
            score += 30;
            recommendations.push(`✅ Gas pipeline within ${nearestKm.toFixed(1)}km - good gas access`);
        } else if (nearestKm < 25) {
            score += 15;
            recommendations.push(`⚠️ Nearest gas pipeline is ${nearestKm.toFixed(1)}km away`);
        } else if (nearestKm < 50) {
            score += 5;
            recommendations.push(`⚠️ Gas pipeline ${nearestKm.toFixed(1)}km away - may need extension`);
        } else {
            recommendations.push(`❌ Nearest gas pipeline is ${nearestKm.toFixed(1)}km away - limited gas access`);
        }
        
        // Bonus for Tallgrass
        if (hasTallgrass) {
            score += 10;
            recommendations.push('🎯 Tallgrass/REX pipeline in area - potential partnership opportunity');
        }
        
        // Bonus for interstate
        if (hasInterstate) {
            score += 5;
            recommendations.push('📍 Interstate pipeline access available');
        }
        
        return { score: Math.min(100, score), recommendations };
    }
    
    // ==========================================================================
    // MAP LAYER FUNCTIONS
    // ==========================================================================
    
    /**
     * Load DOT gas pipelines layer
     */
    async function loadDOTPipelines(map, bounds, layerGroup) {
        console.log('📡 Loading DOT gas pipelines...');
        
        const envelope = `${bounds.getWest()},${bounds.getSouth()},${bounds.getEast()},${bounds.getNorth()}`;
        
        // Try backend first
        let data = await fetchAPI('/api/v1/energy/pipelines', {
            minLat: bounds.getSouth(),
            maxLat: bounds.getNorth(),
            minLng: bounds.getWest(),
            maxLng: bounds.getEast()
        });
        
        let features = [];
        if (data && data.success) {
            features = data.data || [];
        } else {
            // Fallback
            const result = await queryArcGIS(CONFIG.DIRECT_API.dotPipelines, {
                where: '1=1',
                geometry: envelope,
                geometryType: 'esriGeometryEnvelope',
                spatialRel: 'esriSpatialRelIntersects',
                outFields: 'typepipe,operator,status'
            });
            features = result.features || [];
        }
        
        // Clear existing
        layerGroup.clearLayers();
        
        let addedCount = 0;
        features.forEach(f => {
            const attrs = f.attributes || {};
            const paths = f.geometry?.paths || [];
            
            if (!paths.length) return;
            
            // Determine color
            let color = CONFIG.COLORS.gas;
            const operator = (attrs.operator || '').toLowerCase();
            
            if (operator.includes('tallgrass') || operator.includes('rockies express')) {
                color = CONFIG.COLORS.tallgrass;
            } else if (attrs.typepipe === 'interstate') {
                color = CONFIG.COLORS.interstate;
            } else if (attrs.typepipe === 'intrastate') {
                color = CONFIG.COLORS.intrastate;
            }
            
            // Create polyline
            paths.forEach(path => {
                const latLngs = path.map(([lng, lat]) => [lat, lng]);
                
                const polyline = L.polyline(latLngs, {
                    color: color,
                    weight: 3,
                    opacity: 0.8
                });
                
                polyline.bindPopup(`
                    <div style="min-width:200px">
                        <div style="font-weight:600;color:#6366f1;border-bottom:1px solid #e5e7eb;padding-bottom:4px;margin-bottom:8px;">
                            🔥 Gas Pipeline
                        </div>
                        <div style="font-size:12px;">
                            <div><strong>Operator:</strong> ${attrs.operator || 'Unknown'}</div>
                            <div><strong>Type:</strong> ${attrs.typepipe || 'Unknown'}</div>
                            <div><strong>Status:</strong> ${attrs.status || 'Active'}</div>
                        </div>
                        <div style="font-size:10px;color:#6b7280;margin-top:8px;">
                            📡 DOT/EIA Pipeline Data
                        </div>
                    </div>
                `);
                
                layerGroup.addLayer(polyline);
                addedCount++;
            });
        });
        
        console.log(`✅ Added ${addedCount} DOT pipeline segments`);
        return addedCount;
    }
    
    /**
     * Load Texas RRC pipelines
     */
    async function loadTexasPipelines(map, bounds, layerGroup) {
        console.log('📡 Loading Texas RRC pipelines...');
        
        // Try backend
        let data = await fetchAPI('/api/v1/energy/texas-pipelines', {
            minLat: bounds.getSouth(),
            maxLat: bounds.getNorth(),
            minLng: bounds.getWest(),
            maxLng: bounds.getEast()
        });
        
        let features = [];
        if (data && data.success) {
            features = data.data || [];
        } else {
            // Fallback
            const envelope = `${bounds.getWest()},${bounds.getSouth()},${bounds.getEast()},${bounds.getNorth()}`;
            const result = await queryArcGIS(CONFIG.DIRECT_API.texasRRC, {
                where: '1=1',
                geometry: envelope,
                geometryType: 'esriGeometryEnvelope',
                spatialRel: 'esriSpatialRelIntersects',
                outFields: 'P5_NUM,OPERATOR_NAME,PIPELINE_TYPE,COMMODITY'
            });
            features = result.features || [];
        }
        
        layerGroup.clearLayers();
        
        let addedCount = 0;
        features.forEach(f => {
            const attrs = f.attributes || {};
            const paths = f.geometry?.paths || [];
            
            if (!paths.length) return;
            
            // Determine color by commodity
            const commodity = (attrs.COMMODITY || '').toLowerCase();
            let color = CONFIG.COLORS.gas;
            if (commodity.includes('oil') || commodity.includes('crude')) {
                color = CONFIG.COLORS.oil;
            }
            
            paths.forEach(path => {
                const latLngs = path.map(([lng, lat]) => [lat, lng]);
                
                const polyline = L.polyline(latLngs, {
                    color: color,
                    weight: 2,
                    opacity: 0.7,
                    dashArray: '5, 5'
                });
                
                polyline.bindPopup(`
                    <div style="min-width:200px">
                        <div style="font-weight:600;color:#ef4444;border-bottom:1px solid #e5e7eb;padding-bottom:4px;margin-bottom:8px;">
                            🛢️ Texas Pipeline
                        </div>
                        <div style="font-size:12px;">
                            <div><strong>P5:</strong> ${attrs.P5_NUM || 'N/A'}</div>
                            <div><strong>Operator:</strong> ${attrs.OPERATOR_NAME || 'Unknown'}</div>
                            <div><strong>Type:</strong> ${attrs.PIPELINE_TYPE || 'Unknown'}</div>
                            <div><strong>Commodity:</strong> ${attrs.COMMODITY || 'Unknown'}</div>
                        </div>
                        <div style="font-size:10px;color:#6b7280;margin-top:8px;">
                            📡 Texas RRC Data
                        </div>
                    </div>
                `);
                
                layerGroup.addLayer(polyline);
                addedCount++;
            });
        });
        
        console.log(`✅ Added ${addedCount} Texas pipeline segments`);
        return addedCount;
    }
    
    // ==========================================================================
    // UTILITY FUNCTIONS
    // ==========================================================================
    
    function getBoundsFromPoint(lat, lng, radiusMeters) {
        const latDelta = radiusMeters / 111000;
        const lngDelta = radiusMeters / (111000 * Math.cos(lat * Math.PI / 180));
        return {
            minLat: lat - latDelta,
            maxLat: lat + latDelta,
            minLng: lng - lngDelta,
            maxLng: lng + lngDelta
        };
    }
    
    function getDistanceMeters(lat1, lng1, lat2, lng2) {
        const R = 6371000;
        const dLat = (lat2 - lat1) * Math.PI / 180;
        const dLng = (lng2 - lng1) * Math.PI / 180;
        const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                  Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                  Math.sin(dLng/2) * Math.sin(dLng/2);
        const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
        return R * c;
    }
    
    // ==========================================================================
    // EXPORT TO GLOBAL SCOPE
    // ==========================================================================
    
    window.DCHubEnergy = {
        // Core functions
        enhancedSiteAnalysis,
        loadDOTPipelines,
        loadTexasPipelines,
        
        // Utilities
        fetchAPI,
        queryArcGIS,
        calculateGasScore,
        
        // Cache
        cache: DataCache,
        
        // Config
        config: CONFIG,
        
        // Version
        version: '1.0.0'
    };
    
    console.log('⚡ DC Hub Energy Infrastructure v1.0.0 loaded');
    console.log('   Use: DCHubEnergy.enhancedSiteAnalysis(lat, lng, radius)');
    console.log('   Use: DCHubEnergy.loadDOTPipelines(map, bounds, layerGroup)');
    
})();
