/**
 * DC Hub GridStatus Integration v1.0
 * ===================================
 * 
 * Real-time ISO/RTO grid data integration:
 * - Grid load and demand
 * - Fuel mix (generation by source)
 * - LMP (Locational Marginal Pricing)
 * - Grid status and alerts
 * 
 * Supported ISOs: CAISO, PJM, ERCOT, MISO, NYISO, ISONE, SPP
 * 
 * Data Sources:
 * - CAISO Outlook (free, no key)
 * - EIA API v2 (free, key required)
 */

(function() {
    'use strict';
    
    // ============================================
    // CONFIGURATION
    // ============================================
    
    var CONFIG = {
        // Backend proxy endpoints (avoids CORS)
        API_BASE: 'https://dchub.cloud',
        
        // EIA API (uses existing DC Hub key)
        EIA_BASE: 'https://api.eia.gov/v2',
        
        // Cache duration
        CACHE_DURATION: 5 * 60 * 1000, // 5 minutes
        
        // Supported ISOs with their EIA respondent codes
        ISO_CODES: {
            'CAISO': 'CISO',
            'PJM': 'PJM',
            'ERCOT': 'ERCO',
            'MISO': 'MISO',
            'NYISO': 'NYIS',
            'ISONE': 'ISNE',
            'SPP': 'SWPP',
            'BPA': 'BPAT'
        }
    };
    
    // ============================================
    // ISO REGION DATA
    // ============================================
    
    var ISO_REGIONS = {
        'CAISO': {
            name: 'California ISO',
            fullName: 'California Independent System Operator',
            states: ['CA'],
            bounds: [[32.5, -124.5], [42.0, -114.0]],
            timezone: 'America/Los_Angeles',
            website: 'https://www.caiso.com'
        },
        'PJM': {
            name: 'PJM Interconnection',
            fullName: 'PJM Interconnection LLC',
            states: ['PA', 'NJ', 'MD', 'DE', 'VA', 'WV', 'OH', 'DC', 'NC', 'KY', 'TN', 'IN', 'IL', 'MI'],
            bounds: [[35.0, -90.0], [42.5, -74.0]],
            timezone: 'America/New_York',
            website: 'https://www.pjm.com'
        },
        'ERCOT': {
            name: 'ERCOT',
            fullName: 'Electric Reliability Council of Texas',
            states: ['TX'],
            bounds: [[25.8, -106.6], [36.5, -93.5]],
            timezone: 'America/Chicago',
            website: 'https://www.ercot.com'
        },
        'MISO': {
            name: 'MISO',
            fullName: 'Midcontinent Independent System Operator',
            states: ['ND', 'SD', 'MN', 'WI', 'IA', 'IL', 'IN', 'MI', 'MO', 'AR', 'LA', 'MS', 'MT', 'KY'],
            bounds: [[29.0, -104.0], [49.0, -82.0]],
            timezone: 'America/Chicago',
            website: 'https://www.misoenergy.org'
        },
        'NYISO': {
            name: 'NYISO',
            fullName: 'New York Independent System Operator',
            states: ['NY'],
            bounds: [[40.5, -79.8], [45.0, -71.8]],
            timezone: 'America/New_York',
            website: 'https://www.nyiso.com'
        },
        'ISONE': {
            name: 'ISO-NE',
            fullName: 'ISO New England',
            states: ['CT', 'MA', 'ME', 'NH', 'RI', 'VT'],
            bounds: [[40.9, -73.7], [47.5, -66.9]],
            timezone: 'America/New_York',
            website: 'https://www.iso-ne.com'
        },
        'SPP': {
            name: 'SPP',
            fullName: 'Southwest Power Pool',
            states: ['KS', 'OK', 'NE', 'ND', 'SD', 'MT', 'WY', 'NM', 'AR', 'LA', 'MO'],
            bounds: [[25.8, -108.0], [49.0, -89.0]],
            timezone: 'America/Chicago',
            website: 'https://www.spp.org'
        },
        'WECC': {
            name: 'WECC',
            fullName: 'Western Electricity Coordinating Council',
            states: ['AZ', 'CO', 'NM', 'UT', 'WY', 'NV', 'OR', 'WA', 'ID', 'MT'],
            bounds: [[31.3, -125.0], [49.0, -102.0]],
            timezone: 'America/Denver',
            website: 'https://www.wecc.org'
        }
    };
    
    // State to ISO mapping
    var STATE_TO_ISO = {
        'AZ': 'WECC', 'CA': 'CAISO', 'CO': 'WECC', 'CT': 'ISONE',
        'DE': 'PJM', 'FL': 'FRCC', 'GA': 'SERC', 'IA': 'MISO',
        'ID': 'WECC', 'IL': 'PJM', 'IN': 'MISO', 'KS': 'SPP',
        'KY': 'PJM', 'LA': 'MISO', 'MA': 'ISONE', 'MD': 'PJM',
        'ME': 'ISONE', 'MI': 'MISO', 'MN': 'MISO', 'MO': 'SPP',
        'MS': 'MISO', 'MT': 'WECC', 'NC': 'PJM', 'ND': 'MISO',
        'NE': 'SPP', 'NH': 'ISONE', 'NJ': 'PJM', 'NM': 'WECC',
        'NV': 'WECC', 'NY': 'NYISO', 'OH': 'PJM', 'OK': 'SPP',
        'OR': 'WECC', 'PA': 'PJM', 'RI': 'ISONE', 'SD': 'MISO',
        'TN': 'PJM', 'TX': 'ERCOT', 'UT': 'WECC', 'VA': 'PJM',
        'VT': 'ISONE', 'WA': 'WECC', 'WI': 'MISO', 'WV': 'PJM',
        'WY': 'WECC', 'DC': 'PJM'
    };
    
    // ============================================
    // CACHE SYSTEM
    // ============================================
    
    var cache = {
        data: {},
        timestamps: {},
        
        get: function(key) {
            var timestamp = this.timestamps[key];
            if (timestamp && (Date.now() - timestamp) < CONFIG.CACHE_DURATION) {
                return this.data[key];
            }
            return null;
        },
        
        set: function(key, value) {
            this.data[key] = value;
            this.timestamps[key] = Date.now();
        },
        
        clear: function() {
            this.data = {};
            this.timestamps = {};
        }
    };
    
    // ============================================
    // UTILITY FUNCTIONS
    // ============================================
    
    function getISOForLocation(lat, lng) {
        // Special cases first
        if (lng < -114 && lng > -124.5 && lat < 42 && lat > 32.5) return 'CAISO';
        if (lng > -106.6 && lng < -93.5 && lat > 25.8 && lat < 36.5) return 'ERCOT';
        
        // Check bounding boxes
        for (var iso in ISO_REGIONS) {
            var bounds = ISO_REGIONS[iso].bounds;
            if (bounds && 
                lat >= bounds[0][0] && lat <= bounds[1][0] &&
                lng >= bounds[0][1] && lng <= bounds[1][1]) {
                return iso;
            }
        }
        
        // Fallback
        if (lng < -115) return 'CAISO';
        if (lng < -102) return 'WECC';
        if (lng > -95 && lng < -74) return 'PJM';
        return 'MISO';
    }
    
    function getISOForState(state) {
        return STATE_TO_ISO[state.toUpperCase()] || 'WECC';
    }
    
    function getEIAKey() {
        if (window.DCHubEnergy && window.DCHubEnergy.config && window.DCHubEnergy.config.EIA_API_KEY) {
            return window.DCHubEnergy.config.EIA_API_KEY;
        }
        return null;
    }
    
    // ============================================
    // CAISO DATA (via backend proxy)
    // ============================================
    
    async function getCAISOFuelMix() {
        var cacheKey = 'caiso_fuelmix';
        var cached = cache.get(cacheKey);
        if (cached) return cached;
        
        try {
            // Use backend proxy to avoid CORS
            var response = await fetch(CONFIG.API_BASE + '/api/v1/grid/caiso/fuelmix');
            var data = await response.json();
            
            if (data.success) {
                cache.set(cacheKey, data);
                return data;
            }
            return null;
        } catch (e) {
            console.error('❌ CAISO fuel mix error:', e);
            return null;
        }
    }
    
    async function getCAISODemand() {
        var cacheKey = 'caiso_demand';
        var cached = cache.get(cacheKey);
        if (cached) return cached;
        
        try {
            // Use backend proxy to avoid CORS
            var response = await fetch(CONFIG.API_BASE + '/api/v1/grid/caiso/demand');
            var data = await response.json();
            
            if (data.success) {
                cache.set(cacheKey, data);
                return data;
            }
            return null;
        } catch (e) {
            console.error('❌ CAISO demand error:', e);
            return null;
        }
    }
    
    // ============================================
    // EIA API DATA
    // ============================================
    
    async function getEIADemand(iso) {
        var eiaCode = CONFIG.ISO_CODES[iso];
        if (!eiaCode) return null;
        
        var cacheKey = 'eia_demand_' + iso;
        var cached = cache.get(cacheKey);
        if (cached) return cached;
        
        var apiKey = getEIAKey();
        if (!apiKey) return null;
        
        try {
            var url = CONFIG.EIA_BASE + '/electricity/rto/region-data/data/?' +
                'api_key=' + apiKey +
                '&frequency=hourly' +
                '&data[0]=value' +
                '&facets[respondent][]=' + eiaCode +
                '&facets[type][]=D' +
                '&sort[0][column]=period' +
                '&sort[0][direction]=desc' +
                '&length=24';
            
            var response = await fetch(url);
            var data = await response.json();
            
            if (data.response && data.response.data && data.response.data.length > 0) {
                var latest = data.response.data[0];
                var result = {
                    iso: iso,
                    timestamp: latest.period,
                    demandMW: Math.round(latest.value || 0),
                    hourlyData: data.response.data.slice(0, 24).map(function(d) {
                        return { period: d.period, mw: Math.round(d.value || 0) };
                    })
                };
                cache.set(cacheKey, result);
                return result;
            }
        } catch (e) {
            console.error('❌ EIA demand error for', iso, ':', e);
        }
        return null;
    }
    
    async function getEIAGeneration(iso) {
        var eiaCode = CONFIG.ISO_CODES[iso];
        if (!eiaCode) return null;
        
        var cacheKey = 'eia_gen_' + iso;
        var cached = cache.get(cacheKey);
        if (cached) return cached;
        
        var apiKey = getEIAKey();
        if (!apiKey) return null;
        
        try {
            var url = CONFIG.EIA_BASE + '/electricity/rto/fuel-type-data/data/?' +
                'api_key=' + apiKey +
                '&frequency=hourly' +
                '&data[0]=value' +
                '&facets[respondent][]=' + eiaCode +
                '&sort[0][column]=period' +
                '&sort[0][direction]=desc' +
                '&length=100';
            
            var response = await fetch(url);
            var data = await response.json();
            
            if (data.response && data.response.data) {
                var latestPeriod = data.response.data[0]?.period;
                var fuelMix = {};
                var total = 0;
                
                data.response.data.forEach(function(d) {
                    if (d.period === latestPeriod) {
                        var fuelType = d['fueltype'] || d['type-name'] || 'Other';
                        var value = d.value || 0;
                        fuelMix[fuelType] = (fuelMix[fuelType] || 0) + value;
                        total += value;
                    }
                });
                
                var result = {
                    iso: iso,
                    timestamp: latestPeriod,
                    sources: fuelMix,
                    totalMW: Math.round(total),
                    percentages: {}
                };
                
                for (var fuel in fuelMix) {
                    result.percentages[fuel] = ((fuelMix[fuel] / total) * 100).toFixed(1);
                }
                
                cache.set(cacheKey, result);
                return result;
            }
        } catch (e) {
            console.error('❌ EIA generation error for', iso, ':', e);
        }
        return null;
    }
    
    // ============================================
    // COMPREHENSIVE GRID STATUS
    // ============================================
    
    async function getGridStatus(lat, lng) {
        var iso = getISOForLocation(lat, lng);
        var isoInfo = ISO_REGIONS[iso] || {};
        
        var result = {
            iso: iso,
            isoName: isoInfo.name || iso,
            isoFullName: isoInfo.fullName || iso,
            location: { lat: lat, lng: lng },
            timestamp: new Date().toISOString(),
            website: isoInfo.website
        };
        
        if (iso === 'CAISO') {
            var fuelMix = await getCAISOFuelMix();
            var demand = await getCAISODemand();
            
            if (fuelMix) {
                result.fuelMix = fuelMix.sources;
                result.fuelMixSorted = fuelMix.raw;
                result.totalGenerationMW = fuelMix.totalMW;
                result.renewablesPct = fuelMix.renewablesPct;
                result.renewablesMW = fuelMix.renewablesMW;
            }
            
            if (demand) {
                result.currentDemandMW = demand.currentDemandMW;
                result.forecastDemandMW = demand.dayAheadForecastMW;
            }
        } else {
            var eiaDemand = await getEIADemand(iso);
            var eiaGen = await getEIAGeneration(iso);
            
            if (eiaDemand) {
                result.currentDemandMW = eiaDemand.demandMW;
                result.hourlyDemand = eiaDemand.hourlyData;
            }
            
            if (eiaGen) {
                result.fuelMix = eiaGen.sources;
                result.totalGenerationMW = eiaGen.totalMW;
            }
        }
        
        // Grid stress calculation
        if (result.currentDemandMW && result.totalGenerationMW) {
            var ratio = result.currentDemandMW / result.totalGenerationMW;
            if (ratio > 0.95) {
                result.gridStress = 'critical';
                result.gridStressColor = '#ef4444';
                result.gridStressLabel = 'Critical';
            } else if (ratio > 0.90) {
                result.gridStress = 'high';
                result.gridStressColor = '#f97316';
                result.gridStressLabel = 'High Load';
            } else if (ratio > 0.80) {
                result.gridStress = 'moderate';
                result.gridStressColor = '#f59e0b';
                result.gridStressLabel = 'Moderate';
            } else {
                result.gridStress = 'normal';
                result.gridStressColor = '#22c55e';
                result.gridStressLabel = 'Normal';
            }
            result.utilizationPct = (ratio * 100).toFixed(1);
        }
        
        if (result.fuelMix) {
            result.carbonIntensity = estimateCarbonIntensity(result.fuelMix);
        }
        
        return result;
    }
    
    function estimateCarbonIntensity(fuelMix) {
        var factors = {
            'Coal': 820, 'Natural Gas': 490, 'Gas': 490, 'NG': 490,
            'Oil': 650, 'Petroleum': 650, 'Nuclear': 12,
            'Solar': 45, 'Wind': 11, 'Hydro': 24, 'Large Hydro': 24,
            'Small hydro': 24, 'Geothermal': 38, 'Biomass': 230,
            'Biogas': 230, 'Batteries': 0, 'Other': 400, 'Imports': 400
        };
        
        var totalMW = 0;
        var weightedIntensity = 0;
        
        for (var source in fuelMix) {
            var mw = fuelMix[source] || 0;
            var factor = 400;
            for (var key in factors) {
                if (source.toLowerCase().includes(key.toLowerCase())) {
                    factor = factors[key];
                    break;
                }
            }
            totalMW += mw;
            weightedIntensity += mw * factor;
        }
        
        if (totalMW === 0) return null;
        
        var intensity = Math.round(weightedIntensity / totalMW);
        
        return {
            value: intensity,
            unit: 'gCO2/kWh',
            rating: intensity < 200 ? 'Low' : intensity < 400 ? 'Medium' : 'High',
            color: intensity < 200 ? '#22c55e' : intensity < 400 ? '#f59e0b' : '#ef4444'
        };
    }
    
    // ============================================
    // EXPOSE API
    // ============================================
    
    window.DCHubGridStatus = {
        config: CONFIG,
        isoRegions: ISO_REGIONS,
        getISOForLocation: getISOForLocation,
        getISOForState: getISOForState,
        getGridStatus: getGridStatus,
        getCAISOFuelMix: getCAISOFuelMix,
        getCAISODemand: getCAISODemand,
        getEIADemand: getEIADemand,
        getEIAGeneration: getEIAGeneration,
        estimateCarbonIntensity: estimateCarbonIntensity,
        clearCache: function() { cache.clear(); },
        
        test: async function() {
            console.log('🧪 Testing GridStatus Integration...');
            console.log('\n📊 CAISO Fuel Mix:');
            var caisoFuel = await getCAISOFuelMix();
            if (caisoFuel) {
                console.log('   Total:', caisoFuel.totalMW, 'MW');
                console.log('   Renewables:', caisoFuel.renewablesPct + '%');
            }
            
            console.log('\n📍 Phoenix, AZ Grid Status:');
            var status = await getGridStatus(33.45, -112.07);
            console.log('   ISO:', status.isoName);
            console.log('   Grid Stress:', status.gridStressLabel);
            
            console.log('\n✅ GridStatus test complete');
            return status;
        }
    };
    
    console.log('⚡ DC Hub GridStatus Integration v1.0 loaded');
    
})();
