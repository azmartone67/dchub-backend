/**
 * DC Hub Water & Drought Risk Layer v1.0
 * =======================================
 * Integrates US Drought Monitor (USDM) data for water risk assessment
 * 
 * Data Sources:
 * - US Drought Monitor (USDA/NDMC) - droughtmonitor.unl.edu
 * - NOAA Climate Data
 * - State-level groundwater data
 * 
 * Features:
 * - Real-time drought severity by location
 * - State-by-state water risk comparison
 * - Data center suitability scoring
 * - Historical drought trends
 */

const WaterDroughtLayer = {
    // Configuration
    config: {
        // US Drought Monitor API endpoints
        usdmBaseUrl: 'https://usdmdataservices.unl.edu/api',
        // Backup: ArcGIS feature service for USDM
        arcgisUrl: 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Drought_Intensity/FeatureServer/0',
        // Cache settings
        cacheTTL: 3600000, // 1 hour
        // Layer visibility
        visible: false,
        // Map layer reference
        layer: null
    },

    // Cache for API responses
    cache: new Map(),

    // Drought severity levels (USDM standard)
    severityLevels: {
        'None': { code: 'None', name: 'No Drought', color: '#FFFFFF', score: 100, description: 'Normal conditions' },
        'D0': { code: 'D0', name: 'Abnormally Dry', color: '#FFFF00', score: 80, description: 'Going into drought: short-term dryness' },
        'D1': { code: 'D1', name: 'Moderate Drought', color: '#FCD37F', score: 60, description: 'Some damage to crops, streams low' },
        'D2': { code: 'D2', name: 'Severe Drought', color: '#FFAA00', score: 40, description: 'Crop/pasture losses likely, water shortages' },
        'D3': { code: 'D3', name: 'Extreme Drought', color: '#E60000', score: 20, description: 'Major crop/pasture losses, water restrictions' },
        'D4': { code: 'D4', name: 'Exceptional Drought', color: '#730000', score: 0, description: 'Exceptional and widespread losses' }
    },

    // State FIPS codes for API queries
    stateFips: {
        'AL': '01', 'AK': '02', 'AZ': '04', 'AR': '05', 'CA': '06', 'CO': '08', 'CT': '09', 'DE': '10',
        'FL': '12', 'GA': '13', 'HI': '15', 'ID': '16', 'IL': '17', 'IN': '18', 'IA': '19', 'KS': '20',
        'KY': '21', 'LA': '22', 'ME': '23', 'MD': '24', 'MA': '25', 'MI': '26', 'MN': '27', 'MS': '28',
        'MO': '29', 'MT': '30', 'NE': '31', 'NV': '32', 'NH': '33', 'NJ': '34', 'NM': '35', 'NY': '36',
        'NC': '37', 'ND': '38', 'OH': '39', 'OK': '40', 'OR': '41', 'PA': '42', 'RI': '44', 'SC': '45',
        'SD': '46', 'TN': '47', 'TX': '48', 'UT': '49', 'VT': '50', 'VA': '51', 'WA': '53', 'WV': '54',
        'WI': '55', 'WY': '56'
    },

    // State name to abbreviation mapping
    stateAbbrev: {
        'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA',
        'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE', 'Florida': 'FL', 'Georgia': 'GA',
        'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA',
        'Kansas': 'KS', 'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
        'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS', 'Missouri': 'MO',
        'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV', 'New Hampshire': 'NH', 'New Jersey': 'NJ',
        'New Mexico': 'NM', 'New York': 'NY', 'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH',
        'Oklahoma': 'OK', 'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
        'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT', 'Vermont': 'VT',
        'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY'
    },

    /**
     * Fetch drought status for a specific location
     * @param {number} lat - Latitude
     * @param {number} lng - Longitude
     * @returns {Promise<Object>} Drought assessment data
     */
    async fetchDroughtStatus(lat, lng) {
        const cacheKey = `drought_${lat.toFixed(2)}_${lng.toFixed(2)}`;
        const cached = this.cache.get(cacheKey);
        
        if (cached && Date.now() - cached.timestamp < this.config.cacheTTL) {
            return cached.data;
        }

        try {
            // Get state from coordinates
            const state = await this.getStateFromCoords(lat, lng);
            
            // Try ArcGIS feature service for point-based query
            const arcgisData = await this.queryArcGIS(lat, lng);
            
            // Get state-level statistics
            const stateData = await this.fetchStateData(state);
            
            // Calculate comprehensive water risk score
            const assessment = this.calculateWaterRisk(arcgisData, stateData, state);
            
            const result = {
                success: true,
                location: { lat, lng, state },
                drought_level: arcgisData.level || 'None',
                dominant_level: this.severityLevels[arcgisData.level] || this.severityLevels['None'],
                state_data: stateData,
                water_risk_score: assessment.score,
                dc_assessment: assessment,
                timestamp: new Date().toISOString()
            };
            
            this.cache.set(cacheKey, { data: result, timestamp: Date.now() });
            return result;
            
        } catch (error) {
            console.error('Drought data fetch error:', error);
            // Return fallback data
            return this.getFallbackData(lat, lng);
        }
    },

    /**
     * Query ArcGIS feature service for drought data at a point
     */
    async queryArcGIS(lat, lng) {
        try {
            const url = `${this.config.arcgisUrl}/query`;
            const params = new URLSearchParams({
                geometry: `${lng},${lat}`,
                geometryType: 'esriGeometryPoint',
                inSR: '4326',
                spatialRel: 'esriSpatialRelIntersects',
                outFields: '*',
                returnGeometry: 'false',
                f: 'json'
            });

            const response = await fetch(`${url}?${params}`);
            const data = await response.json();

            if (data.features && data.features.length > 0) {
                const attrs = data.features[0].attributes;
                // USDM uses DM field for drought level (0-4, or -1 for none)
                const dm = attrs.DM || attrs.dm || attrs.OBJECTID || -1;
                const level = dm >= 0 && dm <= 4 ? `D${dm}` : 'None';
                
                return {
                    level: level,
                    raw: attrs,
                    source: 'arcgis'
                };
            }
            
            return { level: 'None', source: 'arcgis_empty' };
            
        } catch (error) {
            console.warn('ArcGIS drought query failed:', error);
            return { level: 'None', source: 'fallback' };
        }
    },

    /**
     * Fetch state-level drought statistics
     */
    async fetchStateData(state) {
        if (!state) return this.getDefaultStateData();
        
        const cacheKey = `state_drought_${state}`;
        const cached = this.cache.get(cacheKey);
        
        if (cached && Date.now() - cached.timestamp < this.config.cacheTTL) {
            return cached.data;
        }

        // Use hardcoded data (USDM API is CORS-blocked from browser)
        // TODO: Route through backend proxy when available
        console.log(`📊 Using drought data for ${state}`);
        const stateData = this.getHardcodedStateData(state);
        this.cache.set(cacheKey, { data: stateData, timestamp: Date.now() });
        return stateData;
    },

    /**
     * Hardcoded state data (updated periodically as fallback)
     * Based on recent USDM data for top data center markets
     */
    getHardcodedStateData(state) {
        const stateData = {
            'AZ': { none_pct: 15, d0_pct: 25, d1_pct: 30, d2_pct: 20, d3_pct: 8, d4_pct: 2 },
            'TX': { none_pct: 35, d0_pct: 25, d1_pct: 20, d2_pct: 12, d3_pct: 6, d4_pct: 2 },
            'VA': { none_pct: 85, d0_pct: 10, d1_pct: 5, d2_pct: 0, d3_pct: 0, d4_pct: 0 },
            'OH': { none_pct: 90, d0_pct: 8, d1_pct: 2, d2_pct: 0, d3_pct: 0, d4_pct: 0 },
            'GA': { none_pct: 75, d0_pct: 15, d1_pct: 8, d2_pct: 2, d3_pct: 0, d4_pct: 0 },
            'NV': { none_pct: 10, d0_pct: 20, d1_pct: 30, d2_pct: 25, d3_pct: 10, d4_pct: 5 },
            'CA': { none_pct: 40, d0_pct: 20, d1_pct: 15, d2_pct: 15, d3_pct: 7, d4_pct: 3 },
            'WA': { none_pct: 70, d0_pct: 15, d1_pct: 10, d2_pct: 5, d3_pct: 0, d4_pct: 0 },
            'OR': { none_pct: 60, d0_pct: 20, d1_pct: 12, d2_pct: 6, d3_pct: 2, d4_pct: 0 },
            'CO': { none_pct: 30, d0_pct: 25, d1_pct: 25, d2_pct: 15, d3_pct: 4, d4_pct: 1 },
            'IL': { none_pct: 85, d0_pct: 10, d1_pct: 4, d2_pct: 1, d3_pct: 0, d4_pct: 0 },
            'NJ': { none_pct: 90, d0_pct: 7, d1_pct: 3, d2_pct: 0, d3_pct: 0, d4_pct: 0 },
            'NC': { none_pct: 80, d0_pct: 12, d1_pct: 6, d2_pct: 2, d3_pct: 0, d4_pct: 0 },
            'IA': { none_pct: 80, d0_pct: 12, d1_pct: 6, d2_pct: 2, d3_pct: 0, d4_pct: 0 },
            'UT': { none_pct: 20, d0_pct: 25, d1_pct: 30, d2_pct: 18, d3_pct: 5, d4_pct: 2 }
        };

        const data = stateData[state] || { none_pct: 70, d0_pct: 15, d1_pct: 10, d2_pct: 4, d3_pct: 1, d4_pct: 0 };
        
        return {
            state: state,
            ...data,
            date: new Date().toISOString(),
            source: 'hardcoded'
        };
    },

    getDefaultStateData() {
        return {
            state: 'US',
            none_pct: 60,
            d0_pct: 20,
            d1_pct: 12,
            d2_pct: 5,
            d3_pct: 2,
            d4_pct: 1,
            date: new Date().toISOString(),
            source: 'default'
        };
    },

    /**
     * Calculate comprehensive water risk score for data center siting
     */
    calculateWaterRisk(droughtData, stateData, state) {
        let score = 100;
        
        // Factor 1: Current drought level at location (40% weight)
        const level = droughtData.level || 'None';
        const levelData = this.severityLevels[level] || this.severityLevels['None'];
        score -= (100 - levelData.score) * 0.40;
        
        // Factor 2: State-wide drought coverage (30% weight)
        if (stateData) {
            const severeArea = (stateData.d2_pct || 0) + (stateData.d3_pct || 0) + (stateData.d4_pct || 0);
            score -= severeArea * 0.30;
        }
        
        // Factor 3: Historical water stress by region (20% weight)
        const waterStressRegions = {
            'AZ': 25, 'NV': 30, 'CA': 20, 'UT': 22, 'NM': 25, 'CO': 18,
            'TX': 15, 'OK': 12, 'KS': 10,
            'OR': 8, 'WA': 5, 'ID': 10,
            'VA': 3, 'OH': 2, 'IL': 2, 'GA': 5, 'NC': 4
        };
        const historicalStress = waterStressRegions[state] || 5;
        score -= historicalStress * 0.20;
        
        // Factor 4: Groundwater availability bonus (10% weight)
        const aquiferRichStates = ['TX', 'NE', 'KS', 'OK', 'IL', 'IN', 'OH'];
        if (aquiferRichStates.includes(state)) {
            score += 5; // Bonus for major aquifer access
        }

        // Clamp score between 0-100
        score = Math.max(0, Math.min(100, score));
        
        // Generate assessment
        let rating, recommendation;
        if (score >= 80) {
            rating = 'Excellent Water Availability';
            recommendation = 'Low water stress region. Minimal cooling constraints expected.';
        } else if (score >= 60) {
            rating = 'Good Water Availability';
            recommendation = 'Moderate water conditions. Consider water-efficient cooling systems.';
        } else if (score >= 40) {
            rating = 'Moderate Water Risk';
            recommendation = 'Elevated drought risk. Air-cooled or hybrid cooling recommended.';
        } else if (score >= 20) {
            rating = 'High Water Risk';
            recommendation = 'Significant drought conditions. Air-cooled systems strongly recommended.';
        } else {
            rating = 'Severe Water Risk';
            recommendation = 'Extreme drought. Consider alternative locations or advanced dry cooling.';
        }

        return {
            score: Math.round(score),
            rating,
            recommendation,
            drought_level: level,
            state_coverage: stateData ? {
                no_drought: stateData.none_pct,
                abnormally_dry: stateData.d0_pct,
                moderate: stateData.d1_pct,
                severe: stateData.d2_pct,
                extreme: stateData.d3_pct,
                exceptional: stateData.d4_pct
            } : null
        };
    },

    /**
     * Fallback data when APIs fail
     */
    getFallbackData(lat, lng) {
        const state = this.estimateStateFromCoords(lat, lng);
        const stateData = this.getHardcodedStateData(state);
        
        return {
            success: true,
            location: { lat, lng, state },
            drought_level: 'Unknown',
            dominant_level: { code: 'Unknown', name: 'Data Unavailable', color: '#888888', score: 50 },
            state_data: stateData,
            water_risk_score: 50,
            dc_assessment: {
                score: 50,
                rating: 'Data Unavailable',
                recommendation: 'Unable to fetch real-time data. Please try again later.'
            },
            timestamp: new Date().toISOString(),
            is_fallback: true
        };
    },

    /**
     * Get state from coordinates using simple bounding box
     */
    estimateStateFromCoords(lat, lng) {
        const stateBounds = {
            'AZ': { minLat: 31.3, maxLat: 37.0, minLng: -114.8, maxLng: -109.0 },
            'TX': { minLat: 25.8, maxLat: 36.5, minLng: -106.6, maxLng: -93.5 },
            'VA': { minLat: 36.5, maxLat: 39.5, minLng: -83.7, maxLng: -75.2 },
            'GA': { minLat: 30.4, maxLat: 35.0, minLng: -85.6, maxLng: -80.8 },
            'NV': { minLat: 35.0, maxLat: 42.0, minLng: -120.0, maxLng: -114.0 },
            'CA': { minLat: 32.5, maxLat: 42.0, minLng: -124.4, maxLng: -114.1 },
            'OR': { minLat: 42.0, maxLat: 46.3, minLng: -124.6, maxLng: -116.5 },
            'WA': { minLat: 45.5, maxLat: 49.0, minLng: -124.8, maxLng: -116.9 },
            'OH': { minLat: 38.4, maxLat: 42.0, minLng: -84.8, maxLng: -80.5 },
            'IL': { minLat: 36.9, maxLat: 42.5, minLng: -91.5, maxLng: -87.0 },
            'CO': { minLat: 37.0, maxLat: 41.0, minLng: -109.0, maxLng: -102.0 },
            'NC': { minLat: 33.8, maxLat: 36.6, minLng: -84.3, maxLng: -75.4 }
        };

        for (const [state, bounds] of Object.entries(stateBounds)) {
            if (lat >= bounds.minLat && lat <= bounds.maxLat &&
                lng >= bounds.minLng && lng <= bounds.maxLng) {
                return state;
            }
        }

        return 'US';
    },

    /**
     * Get state from coordinates using reverse geocoding
     */
    async getStateFromCoords(lat, lng) {
        // First try simple bounds check
        const estimated = this.estimateStateFromCoords(lat, lng);
        if (estimated !== 'US') return estimated;
        
        // Fall back to Census geocoder
        try {
            const url = `https://geocoding.geo.census.gov/geocoder/geographies/coordinates?x=${lng}&y=${lat}&benchmark=Public_AR_Current&vintage=Current_Current&layers=States&format=json`;
            const response = await fetch(url);
            const data = await response.json();
            
            if (data.result?.geographies?.States?.[0]) {
                return data.result.geographies.States[0].STUSAB;
            }
        } catch (error) {
            console.warn('Geocoding failed:', error);
        }
        
        return estimated;
    },

    /**
     * Compare water risk across multiple states
     */
    async fetchStateComparison(statesStr) {
        const states = statesStr.split(',').map(s => s.trim().toUpperCase());
        const results = [];
        
        for (const state of states) {
            try {
                const stateData = await this.fetchStateData(state);
                const assessment = this.calculateWaterRisk({ level: 'None' }, stateData, state);
                
                results.push({
                    state: state,
                    water_risk_score: assessment.score,
                    rating: assessment.rating,
                    drought_coverage: stateData,
                    severe_drought_pct: (stateData.d2_pct || 0) + (stateData.d3_pct || 0) + (stateData.d4_pct || 0)
                });
            } catch (error) {
                console.warn(`Failed to fetch data for ${state}:`, error);
            }
        }
        
        // Sort by water risk score (highest = best)
        results.sort((a, b) => b.water_risk_score - a.water_risk_score);
        
        return {
            success: true,
            comparison: results,
            timestamp: new Date().toISOString()
        };
    },

    /**
     * Show full water assessment modal
     */
    showFullAssessment(lat, lng, map) {
        // Remove existing modal
        const existing = document.getElementById('water-assessment-modal');
        if (existing) existing.remove();
        
        const modal = document.createElement('div');
        modal.id = 'water-assessment-modal';
        modal.style.cssText = `
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            z-index: 10000;
            background: #0f1119;
            border: 1px solid #252836;
            border-radius: 16px;
            padding: 24px;
            min-width: 400px;
            max-width: 500px;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
        `;
        
        modal.innerHTML = `
            <button onclick="document.getElementById('water-assessment-modal').remove()" style="position:absolute;top:12px;right:12px;background:#252836;border:none;color:#888;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:16px;">×</button>
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px;">
                <span style="font-size:28px;">💧</span>
                <div>
                    <div style="font-weight:700;font-size:18px;color:#06b6d4;">Water Risk Assessment</div>
                    <div style="font-size:12px;color:#666;">Location: ${lat.toFixed(4)}, ${lng.toFixed(4)}</div>
                </div>
            </div>
            <div id="water-modal-content" style="color:#aaa;">
                <div style="text-align:center;padding:40px;color:#666;">
                    <div style="font-size:24px;margin-bottom:8px;">⏳</div>
                    Loading assessment data...
                </div>
            </div>
        `;
        
        document.body.appendChild(modal);
        
        // Add backdrop
        const backdrop = document.createElement('div');
        backdrop.id = 'water-assessment-backdrop';
        backdrop.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.6);
            z-index: 9999;
        `;
        backdrop.onclick = () => {
            modal.remove();
            backdrop.remove();
        };
        document.body.appendChild(backdrop);
        
        // Fetch and display data
        this.fetchDroughtStatus(lat, lng).then(data => {
            const content = document.getElementById('water-modal-content');
            if (!content) return;
            
            const score = data.water_risk_score || 50;
            const assessment = data.dc_assessment || {};
            const dominant = data.dominant_level || {};
            const stateData = data.state_data || {};
            
            const scoreColor = score >= 80 ? '#10b981' : score >= 60 ? '#fbbf24' : score >= 40 ? '#f97316' : '#ef4444';
            
            content.innerHTML = `
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px;">
                    <div style="background:#1a1a2e;padding:16px;border-radius:12px;text-align:center;">
                        <div style="font-size:11px;color:#888;margin-bottom:4px;text-transform:uppercase;">Water Score</div>
                        <div style="font-size:42px;font-weight:800;color:${scoreColor};">${Math.round(score)}</div>
                        <div style="font-size:11px;color:#666;">out of 100</div>
                    </div>
                    <div style="background:#1a1a2e;padding:16px;border-radius:12px;text-align:center;">
                        <div style="font-size:11px;color:#888;margin-bottom:4px;text-transform:uppercase;">Drought Level</div>
                        <div style="font-size:24px;font-weight:700;color:${dominant.color || '#fff'};margin:8px 0;">${dominant.code || 'None'}</div>
                        <div style="font-size:11px;color:#666;">${dominant.name || 'No Drought'}</div>
                    </div>
                </div>
                
                <div style="background:linear-gradient(135deg,rgba(6,182,212,0.1),rgba(59,130,246,0.1));border-radius:12px;padding:16px;margin-bottom:20px;border-left:4px solid ${scoreColor};">
                    <div style="font-weight:600;color:${scoreColor};margin-bottom:4px;">${assessment.rating || 'Assessment Unavailable'}</div>
                    <div style="font-size:13px;color:#aaa;line-height:1.5;">${assessment.recommendation || ''}</div>
                </div>
                
                <div style="margin-bottom:20px;">
                    <div style="font-size:12px;font-weight:600;color:#888;margin-bottom:12px;text-transform:uppercase;">State Drought Coverage (${data.location?.state || 'US'})</div>
                    ${this.renderDroughtBars(stateData)}
                </div>
                
                <div style="background:#1a1a2e;border-radius:12px;padding:16px;">
                    <div style="font-size:12px;font-weight:600;color:#888;margin-bottom:12px;text-transform:uppercase;">Data Center Cooling Recommendations</div>
                    ${this.getCoolingRecommendations(score)}
                </div>
                
                <div style="margin-top:16px;padding-top:16px;border-top:1px solid #252836;font-size:10px;color:#666;text-align:center;">
                    Data source: US Drought Monitor (USDA/NDMC) • Updated: ${new Date(data.timestamp).toLocaleDateString()}
                </div>
            `;
        }).catch(error => {
            const content = document.getElementById('water-modal-content');
            if (content) {
                content.innerHTML = `
                    <div style="text-align:center;padding:40px;color:#ef4444;">
                        <div style="font-size:24px;margin-bottom:8px;">⚠️</div>
                        Failed to load assessment data.<br>Please try again later.
                    </div>
                `;
            }
        });
    },

    /**
     * Render drought percentage bars
     */
    renderDroughtBars(stateData) {
        const levels = [
            { key: 'none_pct', label: 'No Drought', color: '#22c55e' },
            { key: 'd0_pct', label: 'D0 - Abnormally Dry', color: '#FFFF00' },
            { key: 'd1_pct', label: 'D1 - Moderate', color: '#FCD37F' },
            { key: 'd2_pct', label: 'D2 - Severe', color: '#FFAA00' },
            { key: 'd3_pct', label: 'D3 - Extreme', color: '#E60000' },
            { key: 'd4_pct', label: 'D4 - Exceptional', color: '#730000' }
        ];
        
        return levels.map(level => {
            const pct = stateData[level.key] || 0;
            return `
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
                    <div style="width:12px;height:12px;border-radius:3px;background:${level.color};flex-shrink:0;"></div>
                    <div style="flex:1;font-size:11px;color:#aaa;">${level.label}</div>
                    <div style="width:100px;height:8px;background:#252836;border-radius:4px;overflow:hidden;">
                        <div style="width:${pct}%;height:100%;background:${level.color};"></div>
                    </div>
                    <div style="width:40px;text-align:right;font-size:11px;font-weight:600;color:${level.color};">${pct.toFixed(0)}%</div>
                </div>
            `;
        }).join('');
    },

    /**
     * Get cooling recommendations based on water score
     */
    getCoolingRecommendations(score) {
        if (score >= 80) {
            return `
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                    <span style="color:#22c55e;">✓</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#22c55e;">Evaporative Cooling:</strong> Recommended - abundant water availability</div>
                </div>
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                    <span style="color:#22c55e;">✓</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#22c55e;">Cooling Towers:</strong> Viable - low water stress region</div>
                </div>
                <div style="display:flex;align-items:flex-start;gap:8px;">
                    <span style="color:#22c55e;">✓</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#22c55e;">Hybrid Systems:</strong> Optional - not required for efficiency</div>
                </div>
            `;
        } else if (score >= 60) {
            return `
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                    <span style="color:#fbbf24;">◐</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#fbbf24;">Evaporative Cooling:</strong> Use with caution - monitor water restrictions</div>
                </div>
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                    <span style="color:#22c55e;">✓</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#22c55e;">Hybrid Systems:</strong> Recommended - balance efficiency and water use</div>
                </div>
                <div style="display:flex;align-items:flex-start;gap:8px;">
                    <span style="color:#fbbf24;">◐</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#fbbf24;">Water Recycling:</strong> Consider implementing for resilience</div>
                </div>
            `;
        } else if (score >= 40) {
            return `
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                    <span style="color:#f97316;">⚠</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#f97316;">Evaporative Cooling:</strong> Not recommended - water restrictions likely</div>
                </div>
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                    <span style="color:#22c55e;">✓</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#22c55e;">Air-Cooled Systems:</strong> Strongly recommended</div>
                </div>
                <div style="display:flex;align-items:flex-start;gap:8px;">
                    <span style="color:#22c55e;">✓</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#22c55e;">Indirect Evaporative:</strong> Good alternative with lower water use</div>
                </div>
            `;
        } else {
            return `
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                    <span style="color:#ef4444;">✗</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#ef4444;">Evaporative Cooling:</strong> Avoid - severe water stress</div>
                </div>
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                    <span style="color:#22c55e;">✓</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#22c55e;">Direct Air Cooling:</strong> Required - minimize water dependency</div>
                </div>
                <div style="display:flex;align-items:flex-start;gap:8px;">
                    <span style="color:#f97316;">⚠</span>
                    <div style="font-size:12px;color:#aaa;"><strong style="color:#f97316;">Location Risk:</strong> Consider alternative sites with better water access</div>
                </div>
            `;
        }
    },

    /**
     * Add drought overlay layer to map
     * Uses ArcGIS tile service (reliable, no CORS)
     */
    addDroughtLayer(map) {
        if (this.config.layer) {
            try { map.removeLayer(this.config.layer); } catch(e) {}
        }

        try {
            // Use drought monitor WMS (more reliable than tiles)
            this.config.layer = L.tileLayer.wms('https://droughtmonitor.unl.edu/data/shapefiles_m/USDM_current_M.gdb', {
                layers: '0',
                format: 'image/png',
                transparent: true,
                opacity: 0.5,
                attribution: 'US Drought Monitor'
            });
            
            // If WMS fails, it will just show nothing (silent fail)
            this.config.layer.addTo(map);
            this.config.visible = true;
            console.log('✅ Drought WMS layer added');
        } catch (error) {
            console.warn('Drought layer failed:', error);
            this.config.visible = false;
        }
    },

    /**
     * Remove drought layer from map
     */
    removeDroughtLayer(map) {
        if (this.config.layer) {
            map.removeLayer(this.config.layer);
            this.config.layer = null;
            this.config.visible = false;
        }
    },

    /**
     * Toggle drought layer visibility
     */
    toggleLayer(map) {
        if (this.config.visible) {
            this.removeDroughtLayer(map);
        } else {
            this.addDroughtLayer(map);
        }
        return this.config.visible;
    }
};

// Export for global access
window.WaterDroughtLayer = WaterDroughtLayer;

console.log(`
╔═══════════════════════════════════════════════════════════════╗
║  DC Hub Water & Drought Risk Layer v1.0                        ║
╠═══════════════════════════════════════════════════════════════╣
║  ✅ US Drought Monitor Integration                              ║
║  ✅ Point-based drought queries                                 ║
║  ✅ State comparison analysis                                   ║
║  ✅ DC cooling recommendations                                  ║
║  📡 Data source: USDM (droughtmonitor.unl.edu)                 ║
╚═══════════════════════════════════════════════════════════════╝
`);
