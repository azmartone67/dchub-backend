/**
 * DC Hub Land & Power Map Enhancements v2.0
 * 
 * Features:
 * 1. EIA Electricity Prices - Real-time $/kWh by state and ISO region
 * 2. Power Plant Details - Name, MW capacity, fuel type in site analysis
 * 3. PDF Site Report Export - One-click downloadable reports
 * 4. Auto-Discovery API - Dynamic layer addition
 * 5. Enhanced Fiber/Gas/Power Data
 * 
 * Integration: Add to land-power-app.js or load as separate module
 */

// ============================================================================
// CONFIGURATION
// ============================================================================

const DCHUB_ENHANCEMENTS = {
    version: '2.1.0',
    
    // Use backend for all API calls (handles CORS, API keys, etc.)
    useBackend: true,
    
    // API Endpoints - Uses /api/v1/energy/* (existing backend routes)
    endpoints: {
        backend: 'https://dchub.cloud',
        // Direct APIs (fallback only)
        eia: 'https://api.eia.gov/v2',
        hifld: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services'
    },
    
    // State to ISO/RTO mapping
    stateToISO: {
        'TX': 'ERCOT',
        'CA': 'CAISO',
        'NY': 'NYISO',
        'PA': 'PJM', 'NJ': 'PJM', 'MD': 'PJM', 'VA': 'PJM', 'DE': 'PJM', 'WV': 'PJM', 'OH': 'PJM', 'NC': 'PJM',
        'IL': 'MISO', 'IN': 'MISO', 'MI': 'MISO', 'MN': 'MISO', 'WI': 'MISO', 'IA': 'MISO', 'MO': 'MISO',
        'OK': 'SPP', 'KS': 'SPP', 'NE': 'SPP', 'AR': 'SPP', 'NM': 'SPP',
        'MA': 'ISONE', 'CT': 'ISONE', 'RI': 'ISONE', 'NH': 'ISONE', 'VT': 'ISONE', 'ME': 'ISONE',
        'AZ': 'WECC', 'NV': 'WECC', 'UT': 'WECC', 'CO': 'WECC', 'WY': 'WECC', 'WA': 'WECC', 'OR': 'WECC', 'ID': 'WECC', 'MT': 'WECC'
    },
    
    // ISO region colors for visualization
    isoColors: {
        'ERCOT': '#FF6B35',
        'CAISO': '#FFD166',
        'PJM': '#06D6A0',
        'MISO': '#118AB2',
        'SPP': '#073B4C',
        'NYISO': '#EF476F',
        'ISONE': '#9B5DE5',
        'WECC': '#00F5D4'
    }
};

// ============================================================================
// 1. EIA ELECTRICITY PRICES MODULE
// ============================================================================

const EIAPrices = {
    cache: new Map(),
    cacheExpiry: 3600000, // 1 hour
    
    /**
     * Get electricity retail prices by state (via backend)
     * @param {string} state - Two-letter state code (e.g., 'AZ', 'TX')
     * @param {string} sector - 'COM' (commercial), 'IND' (industrial), 'RES' (residential), 'ALL'
     * @returns {Promise<Object>} Price data in cents/kWh
     */
    async getStatePrices(state, sector = 'IND') {
        const cacheKey = `${state}-${sector}`;
        const cached = this.cache.get(cacheKey);
        
        if (cached && Date.now() - cached.timestamp < this.cacheExpiry) {
            return cached.data;
        }
        
        try {
            // Use backend API v1 (handles EIA key server-side)
            const url = `${DCHUB_ENHANCEMENTS.endpoints.backend}/api/v1/energy/prices/electricity?state=${state}&sector=${sector}`;
            const response = await fetch(url);
            const json = await response.json();
            
            if (json.success && json.data) {
                this.cache.set(cacheKey, { data: json.data, timestamp: Date.now() });
                return json.data;
            }
            
            // Fallback to local data
            return this.getFallbackPrice(state, sector);
        } catch (error) {
            console.error('EIA API Error:', error);
            return this.getFallbackPrice(state, sector);
        }
    },
    
    /**
     * Calculate price trend from historical data
     */
    calculateTrend(data) {
        if (data.length < 2) return { direction: 'stable', change: 0 };
        
        const current = data[0].price;
        const previous = data[data.length - 1].price;
        const change = ((current - previous) / previous * 100).toFixed(1);
        
        return {
            direction: change > 2 ? 'up' : change < -2 ? 'down' : 'stable',
            change: parseFloat(change),
            period: `${data[data.length - 1].period} to ${data[0].period}`
        };
    },
    
    /**
     * Fallback prices when API unavailable (2024 averages)
     */
    getFallbackPrice(state, sector) {
        const fallbackPrices = {
            'AZ': { COM: 12.5, IND: 8.2, RES: 14.8 },
            'TX': { COM: 9.8, IND: 7.1, RES: 13.2 },
            'VA': { COM: 10.2, IND: 7.8, RES: 13.5 },
            'GA': { COM: 11.5, IND: 7.5, RES: 14.0 },
            'NV': { COM: 10.8, IND: 7.9, RES: 14.2 },
            'OR': { COM: 9.5, IND: 6.8, RES: 12.1 },
            'WA': { COM: 9.2, IND: 5.5, RES: 10.8 },
            'CA': { COM: 22.5, IND: 17.8, RES: 28.5 },
            'default': { COM: 11.5, IND: 8.0, RES: 14.0 }
        };
        
        const prices = fallbackPrices[state] || fallbackPrices['default'];
        return {
            state: state,
            sector: sector,
            price_cents_kwh: prices[sector] || prices.IND,
            price_dollars_mwh: ((prices[sector] || prices.IND) * 10).toFixed(2),
            period: 'fallback-2024',
            iso_region: DCHUB_ENHANCEMENTS.stateToISO[state] || 'Other',
            trend: { direction: 'stable', change: 0 },
            isFallback: true
        };
    },
    
    /**
     * Get prices for multiple states (for comparison)
     */
    async getMultiStatePrices(states, sector = 'IND') {
        const promises = states.map(state => this.getStatePrices(state, sector));
        const results = await Promise.all(promises);
        
        return results.filter(r => r !== null).sort((a, b) => a.price_cents_kwh - b.price_cents_kwh);
    },
    
    /**
     * Format price for display
     */
    formatPrice(priceData) {
        if (!priceData) return 'N/A';
        
        const trendIcon = priceData.trend.direction === 'up' ? '↑' : 
                         priceData.trend.direction === 'down' ? '↓' : '→';
        const trendColor = priceData.trend.direction === 'up' ? '#ef4444' : 
                          priceData.trend.direction === 'down' ? '#22c55e' : '#6b7280';
        
        return `
            <div class="price-display">
                <span class="price-value">${priceData.price_cents_kwh.toFixed(2)}¢/kWh</span>
                <span class="price-mwh">($${priceData.price_dollars_mwh}/MWh)</span>
                <span class="price-trend" style="color: ${trendColor}">${trendIcon} ${Math.abs(priceData.trend.change)}%</span>
                <span class="price-region">${priceData.iso_region}</span>
            </div>
        `;
    }
};

// ============================================================================
// 2. POWER PLANT DETAILS MODULE
// ============================================================================

const PowerPlantDetails = {
    cache: new Map(),
    
    /**
     * Query nearby power plants via backend API
     * @param {number} lat - Latitude
     * @param {number} lng - Longitude
     * @param {number} radiusMeters - Search radius in meters
     * @returns {Promise<Array>} Array of power plant objects
     */
    async getNearbyPlants(lat, lng, radiusMeters = 50000) {
        const cacheKey = `${lat.toFixed(3)}-${lng.toFixed(3)}-${radiusMeters}`;
        const cached = this.cache.get(cacheKey);
        
        if (cached && Date.now() - cached.timestamp < 3600000) {
            return cached.data;
        }
        
        // Try direct HIFLD query (backend route has conflicts)
        try {
            const plants = await this.getNearbyPlantsDirect(lat, lng, radiusMeters);
            if (plants.length > 0) {
                this.cache.set(cacheKey, { data: plants, timestamp: Date.now() });
                return plants;
            }
        } catch (error) {
            console.warn('Power Plants query failed (CORS expected):', error.message);
        }
        
        // Return empty - power plant data available via site-analysis
        console.log('💡 Power plant data available via EnhancedSiteAnalysis.analyze()');
        return [];
    },
    
    /**
     * Direct HIFLD query (fallback, may have CORS issues)
     */
    async getNearbyPlantsDirect(lat, lng, radiusMeters = 50000) {
        try {
            const url = new URL(`${DCHUB_ENHANCEMENTS.endpoints.hifld}/Power_Plants/FeatureServer/0/query`);
            url.searchParams.append('geometry', `${lng},${lat}`);
            url.searchParams.append('geometryType', 'esriGeometryPoint');
            url.searchParams.append('distance', radiusMeters);
            url.searchParams.append('units', 'esriSRUnit_Meter');
            url.searchParams.append('outFields', 'NAME,PRIMSOURCE,TOTAL_MW,STATUS,OPERATOR,COUNTY,STATE,NAICS_DESC');
            url.searchParams.append('returnGeometry', 'true');
            url.searchParams.append('f', 'json');
            
            const response = await fetch(url);
            const data = await response.json();
            
            if (data.features) {
                return data.features.map(f => ({
                    name: f.attributes.NAME || 'Unknown',
                    fuelType: this.normalizeFuelType(f.attributes.PRIMSOURCE),
                    capacity_mw: f.attributes.TOTAL_MW || 0,
                    status: f.attributes.STATUS || 'Unknown',
                    operator: f.attributes.OPERATOR || 'Unknown',
                    county: f.attributes.COUNTY,
                    state: f.attributes.STATE,
                    type: f.attributes.NAICS_DESC,
                    lat: f.geometry?.y,
                    lng: f.geometry?.x,
                    distance_km: this.calculateDistance(lat, lng, f.geometry?.y, f.geometry?.x)
                })).sort((a, b) => a.distance_km - b.distance_km);
            }
            
            return [];
        } catch (error) {
            console.error('Direct HIFLD Error:', error);
            return [];
        }
    },
    
    /**
     * Normalize fuel type names
     */
    normalizeFuelType(source) {
        const fuelMap = {
            'NATURAL GAS': 'Natural Gas',
            'NG': 'Natural Gas',
            'COAL': 'Coal',
            'NUCLEAR': 'Nuclear',
            'HYDRO': 'Hydro',
            'HYDROELECTRIC': 'Hydro',
            'WIND': 'Wind',
            'SOLAR': 'Solar',
            'PETROLEUM': 'Petroleum',
            'OIL': 'Petroleum',
            'BIOMASS': 'Biomass',
            'GEOTHERMAL': 'Geothermal',
            'OTHER': 'Other'
        };
        
        return fuelMap[source?.toUpperCase()] || source || 'Unknown';
    },
    
    /**
     * Calculate distance between two points (Haversine)
     */
    calculateDistance(lat1, lng1, lat2, lng2) {
        if (!lat2 || !lng2) return Infinity;
        
        const R = 6371; // Earth's radius in km
        const dLat = (lat2 - lat1) * Math.PI / 180;
        const dLng = (lng2 - lng1) * Math.PI / 180;
        const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                  Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                  Math.sin(dLng/2) * Math.sin(dLng/2);
        const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
        return R * c;
    },
    
    /**
     * Get fuel type icon and color
     */
    getFuelTypeStyle(fuelType) {
        const styles = {
            'Natural Gas': { icon: '🔥', color: '#3b82f6', label: 'Gas' },
            'Nuclear': { icon: '⚛️', color: '#8b5cf6', label: 'Nuclear' },
            'Coal': { icon: '🪨', color: '#6b7280', label: 'Coal' },
            'Wind': { icon: '💨', color: '#10b981', label: 'Wind' },
            'Solar': { icon: '☀️', color: '#f59e0b', label: 'Solar' },
            'Hydro': { icon: '💧', color: '#06b6d4', label: 'Hydro' },
            'Petroleum': { icon: '🛢️', color: '#ef4444', label: 'Oil' },
            'Biomass': { icon: '🌱', color: '#22c55e', label: 'Bio' },
            'Geothermal': { icon: '🌋', color: '#dc2626', label: 'Geo' },
            'Unknown': { icon: '⚡', color: '#9ca3af', label: 'Other' }
        };
        
        return styles[fuelType] || styles['Unknown'];
    },
    
    /**
     * Calculate total capacity by fuel type
     */
    summarizeByFuelType(plants) {
        const summary = {};
        
        plants.forEach(plant => {
            const fuel = plant.fuelType;
            if (!summary[fuel]) {
                summary[fuel] = { count: 0, total_mw: 0, plants: [] };
            }
            summary[fuel].count++;
            summary[fuel].total_mw += plant.capacity_mw || 0;
            summary[fuel].plants.push(plant.name);
        });
        
        return Object.entries(summary)
            .sort((a, b) => b[1].total_mw - a[1].total_mw)
            .map(([fuel, data]) => ({
                fuelType: fuel,
                ...this.getFuelTypeStyle(fuel),
                ...data
            }));
    },
    
    /**
     * Format plant details for site analysis display
     */
    formatForSiteAnalysis(plants) {
        if (!plants || plants.length === 0) {
            return '<div class="no-data">No power plants within search radius</div>';
        }
        
        const summary = this.summarizeByFuelType(plants);
        const totalMW = summary.reduce((acc, s) => acc + s.total_mw, 0);
        
        let html = `
            <div class="power-plants-summary">
                <div class="summary-header">
                    <span class="total-plants">${plants.length} Power Plants</span>
                    <span class="total-capacity">${totalMW.toLocaleString()} MW Total</span>
                </div>
                <div class="fuel-breakdown">
        `;
        
        summary.forEach(s => {
            html += `
                <div class="fuel-item" style="border-left: 3px solid ${s.color}">
                    <span class="fuel-icon">${s.icon}</span>
                    <span class="fuel-name">${s.fuelType}</span>
                    <span class="fuel-mw">${s.total_mw.toLocaleString()} MW</span>
                    <span class="fuel-count">(${s.count})</span>
                </div>
            `;
        });
        
        html += '</div><div class="nearest-plants"><h4>Nearest Plants:</h4>';
        
        plants.slice(0, 5).forEach(plant => {
            const style = this.getFuelTypeStyle(plant.fuelType);
            html += `
                <div class="plant-item">
                    <span class="plant-icon" style="color: ${style.color}">${style.icon}</span>
                    <div class="plant-info">
                        <span class="plant-name">${plant.name}</span>
                        <span class="plant-details">${plant.capacity_mw} MW ${plant.fuelType} • ${plant.distance_km.toFixed(1)} km</span>
                    </div>
                </div>
            `;
        });
        
        html += '</div></div>';
        return html;
    }
};

// ============================================================================
// 3. PDF SITE REPORT EXPORT MODULE
// ============================================================================

const PDFReportExport = {
    
    /**
     * Generate comprehensive site analysis PDF
     * @param {Object} siteData - Complete site analysis data
     * @returns {Promise<Blob>} PDF blob for download
     */
    async generateReport(siteData) {
        // Use jsPDF for client-side PDF generation
        // Load dynamically if not present
        if (!window.jspdf) {
            await this.loadJsPDF();
        }
        
        // Fetch enhanced data (Grid, Energy, FCC, EPA)
        if (window.DCHubPDFEnhancement && siteData.lat && siteData.lng) {
            try {
                console.log('📄 Fetching enhanced data for PDF...');
                siteData.enhancedData = await window.DCHubPDFEnhancement.fetchEnhancedSiteData(
                    siteData.lat,
                    siteData.lng,
                    siteData.state
                );
                console.log('✅ Enhanced data fetched:', siteData.enhancedData);
            } catch (e) {
                console.warn('⚠️ Could not fetch enhanced data:', e);
            }
        }
        
        const { jsPDF } = window.jspdf;
        const doc = new jsPDF('p', 'mm', 'letter');
        
        // Colors
        const primary = [15, 118, 110]; // Teal
        const secondary = [59, 130, 246]; // Blue
        const text = [31, 41, 55]; // Gray-800
        const lightBg = [249, 250, 251]; // Gray-50
        
        let y = 20;
        
        // Header
        doc.setFillColor(...primary);
        doc.rect(0, 0, 220, 35, 'F');
        
        doc.setTextColor(255, 255, 255);
        doc.setFontSize(24);
        doc.setFont('helvetica', 'bold');
        doc.text('DC Hub Site Analysis Report', 15, 18);
        
        doc.setFontSize(12);
        doc.setFont('helvetica', 'normal');
        doc.text(`Generated: ${new Date().toLocaleDateString('en-US', { 
            year: 'numeric', month: 'long', day: 'numeric' 
        })}`, 15, 28);
        
        y = 45;
        
        // Site Location
        doc.setTextColor(...text);
        doc.setFontSize(16);
        doc.setFont('helvetica', 'bold');
        doc.text('Site Location', 15, y);
        y += 8;
        
        doc.setFontSize(11);
        doc.setFont('helvetica', 'normal');
        doc.text(`Coordinates: ${siteData.lat?.toFixed(6)}, ${siteData.lng?.toFixed(6)}`, 15, y);
        y += 6;
        if (siteData.address) {
            doc.text(`Address: ${siteData.address}`, 15, y);
            y += 6;
        }
        doc.text(`State: ${siteData.state || 'N/A'} | ISO Region: ${siteData.iso_region || 'N/A'}`, 15, y);
        y += 12;
        
        // Site Score
        doc.setFillColor(...lightBg);
        doc.roundedRect(15, y, 180, 25, 3, 3, 'F');
        
        doc.setFontSize(14);
        doc.setFont('helvetica', 'bold');
        doc.text('Overall Site Score', 20, y + 10);
        
        const score = siteData.score || 0;
        const scoreColor = score >= 80 ? [34, 197, 94] : score >= 60 ? [234, 179, 8] : [239, 68, 68];
        doc.setTextColor(...scoreColor);
        doc.setFontSize(28);
        doc.text(`${score}/100`, 160, y + 17);
        
        y += 35;
        
        // Electricity Pricing
        doc.setTextColor(...text);
        doc.setFontSize(14);
        doc.setFont('helvetica', 'bold');
        doc.text('Electricity Pricing', 15, y);
        y += 8;
        
        if (siteData.pricing) {
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text(`Industrial Rate: ${siteData.pricing.price_cents_kwh?.toFixed(2) || 'N/A'}¢/kWh ($${siteData.pricing.price_dollars_mwh || 'N/A'}/MWh)`, 15, y);
            y += 6;
            doc.text(`Price Trend: ${siteData.pricing.trend?.change || 0}% ${siteData.pricing.trend?.direction || 'stable'} (${siteData.pricing.trend?.period || 'N/A'})`, 15, y);
            y += 12;
        }
        
        // Power Infrastructure
        doc.setFontSize(14);
        doc.setFont('helvetica', 'bold');
        doc.text('Power Infrastructure (50km radius)', 15, y);
        y += 8;
        
        if (siteData.powerPlants && siteData.powerPlants.length > 0) {
            const summary = PowerPlantDetails.summarizeByFuelType(siteData.powerPlants);
            const totalMW = summary.reduce((acc, s) => acc + s.total_mw, 0);
            
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text(`Total Nearby Capacity: ${totalMW.toLocaleString()} MW from ${siteData.powerPlants.length} plants`, 15, y);
            y += 8;
            
            // Fuel breakdown table
            doc.setFillColor(...secondary);
            doc.setTextColor(255, 255, 255);
            doc.rect(15, y, 180, 7, 'F');
            doc.setFontSize(10);
            doc.setFont('helvetica', 'bold');
            doc.text('Fuel Type', 20, y + 5);
            doc.text('Capacity (MW)', 80, y + 5);
            doc.text('Plants', 130, y + 5);
            doc.text('% of Total', 160, y + 5);
            y += 7;
            
            doc.setTextColor(...text);
            doc.setFont('helvetica', 'normal');
            
            summary.slice(0, 6).forEach((s, i) => {
                const bgColor = i % 2 === 0 ? [255, 255, 255] : lightBg;
                doc.setFillColor(...bgColor);
                doc.rect(15, y, 180, 6, 'F');
                
                doc.text(s.fuelType, 20, y + 4.5);
                doc.text(s.total_mw.toLocaleString(), 80, y + 4.5);
                doc.text(s.count.toString(), 130, y + 4.5);
                doc.text(`${(s.total_mw / totalMW * 100).toFixed(1)}%`, 160, y + 4.5);
                y += 6;
            });
            
            y += 8;
        } else {
            doc.setFontSize(11);
            doc.text('No power plants found within 50km radius', 15, y);
            y += 10;
        }
        
        // Substations
        if (siteData.substations && siteData.substations.length > 0) {
            doc.setFontSize(14);
            doc.setFont('helvetica', 'bold');
            doc.text('Substations', 15, y);
            y += 8;
            
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text(`${siteData.substations.length} substations within search radius`, 15, y);
            y += 6;
            
            const highVoltage = siteData.substations.filter(s => s.voltage >= 345);
            if (highVoltage.length > 0) {
                doc.text(`High-voltage (345kV+): ${highVoltage.length} substations`, 15, y);
                y += 10;
            }
        }
        
        // Gas Infrastructure
        if (siteData.gasInfra) {
            doc.setFontSize(14);
            doc.setFont('helvetica', 'bold');
            doc.text('Gas Infrastructure', 15, y);
            y += 8;
            
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text(`Pipelines within 25km: ${siteData.gasInfra.pipelineCount || 0}`, 15, y);
            y += 6;
            if (siteData.gasInfra.nearestPipeline) {
                doc.text(`Nearest pipeline: ${siteData.gasInfra.nearestPipeline.name || 'N/A'} (${siteData.gasInfra.nearestPipeline.distance_km?.toFixed(1) || 'N/A'} km)`, 15, y);
                y += 10;
            }
        }
        
        // Environmental Factors
        if (siteData.environmental) {
            if (y > 240) {
                doc.addPage();
                y = 20;
            }
            
            doc.setFontSize(14);
            doc.setFont('helvetica', 'bold');
            doc.text('Environmental Factors', 15, y);
            y += 8;
            
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            
            const envItems = [
                `Flood Zone: ${siteData.environmental.floodZone || 'Unknown'}`,
                `Seismic Risk: ${siteData.environmental.seismicRisk || 'Unknown'}`,
                `Wetlands: ${siteData.environmental.wetlands ? 'Present' : 'None detected'}`
            ];
            
            envItems.forEach(item => {
                doc.text(item, 15, y);
                y += 6;
            });
        }
        
        // ===========================================
        // ENHANCED DATA SECTIONS (Grid, Energy, FCC, EPA)
        // ===========================================
        if (window.DCHubPDFEnhancement && siteData.enhancedData) {
            y = window.DCHubPDFEnhancement.addEnhancedSectionsToPDF(doc, y + 8, siteData.enhancedData);
        } else if (siteData.lat && siteData.lng) {
            // Fetch and add enhanced data inline
            try {
                const API_BASE = 'https://dchub.cloud';
                const state = siteData.state;
                const stateToISO = {
                    'CA': 'CAISO', 'AZ': 'CAISO', 'NV': 'CAISO',
                    'TX': 'ERCOT',
                    'VA': 'PJM', 'MD': 'PJM', 'PA': 'PJM', 'NJ': 'PJM', 'OH': 'PJM',
                    'NY': 'NYISO',
                    'MN': 'MISO', 'WI': 'MISO', 'IA': 'MISO',
                    'OK': 'SPP', 'KS': 'SPP',
                    'MA': 'ISONE', 'CT': 'ISONE',
                    'GA': 'SERC', 'FL': 'SERC',
                    'WA': 'BPA', 'OR': 'BPA'
                };
                const iso = stateToISO[state] || null;
                
                // New page for enhanced data
                if (y > 180) {
                    doc.addPage();
                    y = 20;
                }
                
                // Grid & Energy Header
                doc.setFillColor(245, 158, 11);
                doc.rect(15, y, 180, 8, 'F');
                doc.setTextColor(255, 255, 255);
                doc.setFontSize(12);
                doc.setFont('helvetica', 'bold');
                doc.text('Real-Time Grid & Energy Data', 20, y + 6);
                y += 12;
                
                doc.setTextColor(...text);
                doc.setFontSize(11);
                doc.setFont('helvetica', 'normal');
                
                if (iso) {
                    doc.text(`ISO/RTO Region: ${iso}`, 15, y);
                    y += 6;
                }
                
                // Add note about real-time data
                doc.setFontSize(9);
                doc.setTextColor(100, 100, 100);
                doc.text('* Real-time grid and pricing data available at dchub.cloud/land-power', 15, y);
                y += 10;
                
            } catch (e) {
                console.warn('Could not add enhanced PDF sections:', e);
            }
        }
        
        // Footer
        doc.setFillColor(...primary);
        doc.rect(0, 267, 220, 15, 'F');
        doc.setTextColor(255, 255, 255);
        doc.setFontSize(10);
        doc.text('DC Hub - Data Center Intelligence Platform | dchub.cloud', 15, 275);
        doc.text('© 2026 DC Hub', 175, 275);
        
        return doc.output('blob');
    },
    
    /**
     * Load jsPDF library dynamically
     */
    async loadJsPDF() {
        return new Promise((resolve, reject) => {
            if (window.jspdf) {
                resolve();
                return;
            }
            
            const script = document.createElement('script');
            script.src = 'https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js';
            script.onload = resolve;
            script.onerror = reject;
            document.head.appendChild(script);
        });
    },
    
    /**
     * Trigger PDF download
     */
    async downloadReport(siteData, filename) {
        try {
            const blob = await this.generateReport(siteData);
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = filename || `DCHub-Site-Report-${Date.now()}.pdf`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
            return true;
        } catch (error) {
            console.error('PDF generation error:', error);
            return false;
        }
    }
};

// ============================================================================
// 4. AUTO-DISCOVERY API MODULE
// ============================================================================

const AutoDiscoveryAPI = {
    availableLayers: new Map(),
    
    /**
     * Register available data layers
     */
    registerLayers() {
        const layers = [
            {
                id: 'hifld-substations',
                name: 'Substations (HIFLD)',
                source: 'HIFLD',
                endpoint: `${DCHUB_ENHANCEMENTS.endpoints.hifld}/Electric_Substations/FeatureServer/0/query`,
                type: 'point',
                autoRefresh: false,
                enabled: true
            },
            {
                id: 'hifld-transmission',
                name: 'Transmission Lines (HIFLD)',
                source: 'HIFLD',
                endpoint: `${DCHUB_ENHANCEMENTS.endpoints.hifld}/Electric_Power_Transmission_Lines/FeatureServer/0/query`,
                type: 'line',
                autoRefresh: false,
                enabled: true
            },
            {
                id: 'hifld-power-plants',
                name: 'Power Plants (HIFLD)',
                source: 'HIFLD',
                endpoint: `${DCHUB_ENHANCEMENTS.endpoints.hifld}/Power_Plants/FeatureServer/0/query`,
                type: 'point',
                autoRefresh: false,
                enabled: true
            },
            {
                id: 'dot-pipelines',
                name: 'Gas Pipelines (DOT NPMS)',
                source: 'DOT',
                endpoint: 'https://services.arcgis.com/4lFYLJPggW6nWpYP/ArcGIS/rest/services/NPMS_Public_Viewer/FeatureServer/0/query',
                type: 'line',
                autoRefresh: false,
                enabled: true
            },
            {
                id: 'eia-prices',
                name: 'Electricity Prices (EIA)',
                source: 'EIA',
                endpoint: `${DCHUB_ENHANCEMENTS.endpoints.eia}/electricity/retail-sales/data`,
                type: 'data',
                autoRefresh: true,
                refreshInterval: 86400000, // 24 hours
                enabled: true
            },
            {
                id: 'texas-rrc-pipelines',
                name: 'Texas Pipelines (RRC)',
                source: 'Texas RRC',
                endpoint: 'https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer/0/query',
                type: 'line',
                region: 'TX',
                autoRefresh: false,
                enabled: true
            },
            {
                id: 'fema-flood',
                name: 'Flood Zones (FEMA)',
                source: 'FEMA',
                endpoint: 'https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query',
                type: 'polygon',
                autoRefresh: false,
                enabled: true
            },
            {
                id: 'nwi-wetlands',
                name: 'Wetlands (NWI)',
                source: 'USFWS',
                endpoint: 'https://fwspublicservices.wim.usgs.gov/server/rest/services/Wetlands/MapServer/0/query',
                type: 'polygon',
                autoRefresh: false,
                enabled: true
            }
        ];
        
        layers.forEach(layer => this.availableLayers.set(layer.id, layer));
        
        console.log(`📊 Auto-Discovery: ${layers.length} data layers registered`);
        return layers;
    },
    
    /**
     * Query a specific layer
     */
    async queryLayer(layerId, bounds, options = {}) {
        const layer = this.availableLayers.get(layerId);
        if (!layer) {
            console.warn(`Layer ${layerId} not found`);
            return null;
        }
        
        const params = new URLSearchParams({
            geometry: `${bounds.minLng},${bounds.minLat},${bounds.maxLng},${bounds.maxLat}`,
            geometryType: 'esriGeometryEnvelope',
            spatialRel: 'esriSpatialRelIntersects',
            outFields: options.outFields || '*',
            returnGeometry: options.returnGeometry !== false ? 'true' : 'false',
            f: 'json',
            resultRecordCount: options.limit || 2000
        });
        
        try {
            const response = await fetch(`${layer.endpoint}?${params}`);
            const data = await response.json();
            
            return {
                layerId,
                source: layer.source,
                features: data.features || [],
                count: data.features?.length || 0,
                exceededLimit: data.exceededTransferLimit || false
            };
        } catch (error) {
            console.error(`Error querying ${layerId}:`, error);
            return null;
        }
    },
    
    /**
     * Add new custom layer
     */
    addCustomLayer(layerConfig) {
        if (!layerConfig.id || !layerConfig.endpoint) {
            console.error('Layer must have id and endpoint');
            return false;
        }
        
        this.availableLayers.set(layerConfig.id, {
            ...layerConfig,
            custom: true,
            enabled: true
        });
        
        console.log(`✅ Custom layer added: ${layerConfig.name || layerConfig.id}`);
        return true;
    },
    
    /**
     * Get layer status
     */
    getLayerStatus() {
        const status = [];
        this.availableLayers.forEach((layer, id) => {
            status.push({
                id,
                name: layer.name,
                source: layer.source,
                type: layer.type,
                enabled: layer.enabled,
                custom: layer.custom || false
            });
        });
        return status;
    }
};

// ============================================================================
// 5. FIBER KMZ ENHANCEMENT MODULE
// ============================================================================

const FiberEnhancement = {
    carriers: new Map(),
    
    /**
     * Parse KMZ/KML fiber route data
     */
    async parseKMZ(file) {
        try {
            const JSZip = window.JSZip || (await this.loadJSZip());
            const zip = await JSZip.loadAsync(file);
            
            // Find KML file in KMZ
            const kmlFile = Object.keys(zip.files).find(name => name.endsWith('.kml'));
            if (!kmlFile) {
                throw new Error('No KML file found in KMZ');
            }
            
            const kmlContent = await zip.files[kmlFile].async('text');
            return this.parseKML(kmlContent);
        } catch (error) {
            console.error('KMZ parse error:', error);
            return null;
        }
    },
    
    /**
     * Parse KML content
     */
    parseKML(kmlContent) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(kmlContent, 'text/xml');
        
        const placemarks = doc.querySelectorAll('Placemark');
        const routes = [];
        
        placemarks.forEach(pm => {
            const name = pm.querySelector('name')?.textContent || 'Unknown';
            const description = pm.querySelector('description')?.textContent || '';
            
            // Get coordinates
            const coords = pm.querySelector('coordinates')?.textContent;
            if (coords) {
                const points = coords.trim().split(/\s+/).map(c => {
                    const [lng, lat, alt] = c.split(',').map(parseFloat);
                    return { lat, lng, alt: alt || 0 };
                }).filter(p => !isNaN(p.lat) && !isNaN(p.lng));
                
                if (points.length > 0) {
                    routes.push({
                        name,
                        description,
                        carrier: this.extractCarrier(name, description),
                        type: this.detectRouteType(name, description),
                        coordinates: points,
                        length_km: this.calculateRouteLength(points)
                    });
                }
            }
        });
        
        return routes;
    },
    
    /**
     * Extract carrier name from route data
     */
    extractCarrier(name, description) {
        const carriers = [
            'Lumen', 'CenturyLink', 'Level3', 'Zayo', 'Crown Castle', 'Uniti',
            'Windstream', 'Consolidated', 'Frontier', 'AT&T', 'Verizon', 'Sprint',
            'Cogent', 'GTT', 'NTT', 'Telia', 'Hurricane Electric', 'CoreSite',
            'Digital Realty', 'Equinix', 'QTS', 'CyrusOne', 'DataBank'
        ];
        
        const combined = `${name} ${description}`.toLowerCase();
        
        for (const carrier of carriers) {
            if (combined.includes(carrier.toLowerCase())) {
                return carrier;
            }
        }
        
        return 'Unknown';
    },
    
    /**
     * Detect fiber route type
     */
    detectRouteType(name, description) {
        const combined = `${name} ${description}`.toLowerCase();
        
        if (combined.includes('dark')) return 'Dark Fiber';
        if (combined.includes('lit')) return 'Lit Fiber';
        if (combined.includes('backbone') || combined.includes('long haul')) return 'Backbone';
        if (combined.includes('metro')) return 'Metro';
        if (combined.includes('submarine') || combined.includes('undersea')) return 'Submarine';
        
        return 'Unknown';
    },
    
    /**
     * Calculate route length
     */
    calculateRouteLength(points) {
        let total = 0;
        for (let i = 1; i < points.length; i++) {
            total += PowerPlantDetails.calculateDistance(
                points[i-1].lat, points[i-1].lng,
                points[i].lat, points[i].lng
            );
        }
        return total;
    },
    
    /**
     * Load JSZip library
     */
    async loadJSZip() {
        return new Promise((resolve, reject) => {
            if (window.JSZip) {
                resolve(window.JSZip);
                return;
            }
            
            const script = document.createElement('script');
            script.src = 'https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js';
            script.onload = () => resolve(window.JSZip);
            script.onerror = reject;
            document.head.appendChild(script);
        });
    }
};

// ============================================================================
// ENHANCED SITE ANALYSIS (COMBINES ALL MODULES)
// ============================================================================

const EnhancedSiteAnalysis = {
    
    /**
     * Perform comprehensive site analysis via backend
     */
    async analyze(lat, lng, options = {}) {
        const radius = options.radius || 50000;
        const state = options.state || await this.getStateFromCoords(lat, lng);
        
        console.log(`🔍 Starting enhanced site analysis for ${lat}, ${lng}`);
        
        try {
            // Use existing backend site-analysis endpoint (v1)
            const url = `${DCHUB_ENHANCEMENTS.endpoints.backend}/api/v1/energy/site-analysis?lat=${lat}&lng=${lng}&state=${state}&radius=${radius}`;
            const response = await fetch(url);
            const json = await response.json();
            
            if (json.success && json.data) {
                const backendData = json.data;
                
                // Get pricing separately
                let pricing = null;
                try {
                    const priceUrl = `${DCHUB_ENHANCEMENTS.endpoints.backend}/api/v1/energy/prices/electricity?state=${state}&sector=IND`;
                    const priceResp = await fetch(priceUrl);
                    const priceJson = await priceResp.json();
                    if (priceJson.success) pricing = priceJson.data;
                } catch (e) {
                    pricing = EIAPrices.getFallbackPrice(state, 'IND');
                }
                
                // Map power plants from backend structure
                const powerPlants = (backendData.infrastructure?.powerPlants || []).map(p => ({
                    name: p.attributes?.NAME || p.name || 'Unknown',
                    fuelType: p.attributes?.PRIMSOURCE || p.fuel_type || 'Unknown',
                    capacity_mw: p.attributes?.TOTAL_MW || p.capacity_mw || 0,
                    operator: p.attributes?.OPERATOR || p.operator || 'Unknown',
                    state: p.attributes?.STATE || state
                }));
                
                const analysis = {
                    lat,
                    lng,
                    state: state,
                    iso_region: DCHUB_ENHANCEMENTS.stateToISO[state] || 'Other',
                    pricing: pricing,
                    powerPlants: powerPlants,
                    powerSummary: PowerPlantDetails.summarizeByFuelType(powerPlants),
                    // Backend scores
                    gasScore: backendData.scores?.gasScore || 0,
                    powerScore: backendData.scores?.powerScore || 0,
                    score: backendData.scores?.overallScore || 50,
                    rating: backendData.scores?.rating || 'Unknown',
                    recommendations: backendData.scores?.recommendations || [],
                    // Pipeline data
                    pipelines: {
                        count: backendData.counts?.pipelines || 0,
                        nearest_km: backendData.scores?.details?.nearestPipelineKm,
                        operator: backendData.scores?.details?.nearestPipelineOperator,
                        hasInterstate: backendData.scores?.details?.hasInterstate || false
                    },
                    // Infrastructure counts
                    infrastructure: {
                        substations: backendData.counts?.substations || 0,
                        transmissionLines: backendData.counts?.transmissionLines || 0,
                        powerPlants: backendData.counts?.powerPlants || 0,
                        pipelines: backendData.counts?.pipelines || 0
                    },
                    timestamp: new Date().toISOString()
                };
                
                console.log(`✅ Analysis complete. Score: ${analysis.score}/100, Rating: ${analysis.rating}`);
                console.log(`   ⛽ Pipelines: ${analysis.pipelines.count}, Nearest: ${analysis.pipelines.nearest_km}km (${analysis.pipelines.operator})`);
                console.log(`   💰 Power: ${pricing?.price_cents_kwh || 'N/A'}¢/kWh (${analysis.iso_region})`);
                return analysis;
            }
        } catch (error) {
            console.warn('Backend analysis failed, using local fallback:', error);
        }
        
        // Fallback: Get pricing only
        const priceData = await EIAPrices.getStatePrices(state, 'IND');
        
        const analysis = {
            lat,
            lng,
            state,
            iso_region: DCHUB_ENHANCEMENTS.stateToISO[state] || 'Other',
            pricing: priceData,
            powerPlants: [],
            powerSummary: [],
            score: 50,
            timestamp: new Date().toISOString()
        };
        
        console.log(`✅ Fallback analysis. Score: ${analysis.score}/100`);
        return analysis;
    },
    
    /**
     * Calculate enhanced site score
     */
    calculateEnhancedScore(analysis) {
        let score = 50; // Base score
        
        // Pricing score (up to 20 points)
        if (analysis.pricing) {
            const price = analysis.pricing.price_cents_kwh;
            if (price < 7) score += 20;
            else if (price < 9) score += 15;
            else if (price < 11) score += 10;
            else if (price < 13) score += 5;
        }
        
        // Power availability score (up to 20 points)
        if (analysis.powerPlants && analysis.powerPlants.length > 0) {
            const totalMW = analysis.powerSummary.reduce((acc, s) => acc + s.total_mw, 0);
            if (totalMW > 5000) score += 20;
            else if (totalMW > 2000) score += 15;
            else if (totalMW > 1000) score += 10;
            else if (totalMW > 500) score += 5;
        }
        
        // Fuel diversity bonus (up to 10 points)
        if (analysis.powerSummary && analysis.powerSummary.length >= 3) {
            score += Math.min(10, analysis.powerSummary.length * 2);
        }
        
        return Math.min(100, Math.max(0, score));
    },
    
    /**
     * Get state from coordinates (reverse geocoding)
     */
    async getStateFromCoords(lat, lng) {
        // Simple bounding box check for major data center states
        const stateBounds = {
            'AZ': { minLat: 31.3, maxLat: 37.0, minLng: -114.8, maxLng: -109.0 },
            'TX': { minLat: 25.8, maxLat: 36.5, minLng: -106.6, maxLng: -93.5 },
            'VA': { minLat: 36.5, maxLat: 39.5, minLng: -83.7, maxLng: -75.2 },
            'GA': { minLat: 30.4, maxLat: 35.0, minLng: -85.6, maxLng: -80.8 },
            'NV': { minLat: 35.0, maxLat: 42.0, minLng: -120.0, maxLng: -114.0 },
            'CA': { minLat: 32.5, maxLat: 42.0, minLng: -124.4, maxLng: -114.1 },
            'OR': { minLat: 42.0, maxLat: 46.3, minLng: -124.6, maxLng: -116.5 },
            'WA': { minLat: 45.5, maxLat: 49.0, minLng: -124.8, maxLng: -116.9 }
        };
        
        for (const [state, bounds] of Object.entries(stateBounds)) {
            if (lat >= bounds.minLat && lat <= bounds.maxLat &&
                lng >= bounds.minLng && lng <= bounds.maxLng) {
                return state;
            }
        }
        
        return 'US'; // Default
    }
};

// ============================================================================
// UI INTEGRATION HELPERS
// ============================================================================

const UIIntegration = {
    
    /**
     * Add PDF export button to site analysis panel
     */
    addExportButton(container, siteData) {
        const btn = document.createElement('button');
        btn.className = 'pdf-export-btn';
        btn.innerHTML = '📄 Export PDF Report';
        btn.onclick = async () => {
            btn.disabled = true;
            btn.innerHTML = '⏳ Generating...';
            
            const success = await PDFReportExport.downloadReport(siteData, 
                `DCHub-Site-${siteData.state || 'Report'}-${Date.now()}.pdf`
            );
            
            btn.disabled = false;
            btn.innerHTML = success ? '✅ Downloaded!' : '❌ Error';
            setTimeout(() => { btn.innerHTML = '📄 Export PDF Report'; }, 2000);
        };
        
        container.appendChild(btn);
    },
    
    /**
     * Add pricing widget to map
     */
    createPricingWidget(priceData) {
        const widget = document.createElement('div');
        widget.className = 'pricing-widget';
        widget.innerHTML = EIAPrices.formatPrice(priceData);
        return widget;
    },
    
    /**
     * Get CSS styles for enhancement modules
     */
    getStyles() {
        return `
            .price-display {
                display: flex;
                flex-direction: column;
                gap: 4px;
                padding: 8px;
                background: #f8fafc;
                border-radius: 8px;
            }
            .price-value {
                font-size: 24px;
                font-weight: bold;
                color: #0f766e;
            }
            .price-mwh {
                font-size: 14px;
                color: #64748b;
            }
            .price-trend {
                font-size: 12px;
                font-weight: 500;
            }
            .price-region {
                font-size: 11px;
                color: #94a3b8;
                text-transform: uppercase;
            }
            .power-plants-summary {
                background: white;
                border-radius: 8px;
                padding: 12px;
            }
            .summary-header {
                display: flex;
                justify-content: space-between;
                margin-bottom: 12px;
                padding-bottom: 8px;
                border-bottom: 1px solid #e2e8f0;
            }
            .fuel-breakdown {
                display: flex;
                flex-direction: column;
                gap: 6px;
            }
            .fuel-item {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 6px 8px;
                background: #f8fafc;
                border-radius: 4px;
            }
            .plant-item {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 8px 0;
                border-bottom: 1px solid #f1f5f9;
            }
            .plant-name {
                font-weight: 500;
                display: block;
            }
            .plant-details {
                font-size: 12px;
                color: #64748b;
            }
            .pdf-export-btn {
                background: #0f766e;
                color: white;
                border: none;
                padding: 10px 16px;
                border-radius: 6px;
                font-weight: 500;
                cursor: pointer;
                transition: background 0.2s;
            }
            .pdf-export-btn:hover {
                background: #0d9488;
            }
            .pdf-export-btn:disabled {
                background: #94a3b8;
                cursor: not-allowed;
            }
        `;
    }
};

// ============================================================================
// INITIALIZATION & EXPORTS
// ============================================================================

// Initialize auto-discovery layers
AutoDiscoveryAPI.registerLayers();

// Export modules for use in land-power-app.js
window.DCHubEnhancements = {
    EIAPrices,
    PowerPlantDetails,
    PDFReportExport,
    AutoDiscoveryAPI,
    FiberEnhancement,
    EnhancedSiteAnalysis,
    UIIntegration,
    config: DCHUB_ENHANCEMENTS,
    version: DCHUB_ENHANCEMENTS.version
};

console.log(`
╔═══════════════════════════════════════════════════════════════╗
║  DC Hub Land & Power Enhancements v${DCHUB_ENHANCEMENTS.version}                     ║
╠═══════════════════════════════════════════════════════════════╣
║  ✅ EIA Electricity Prices Module                              ║
║  ✅ Power Plant Details Module                                 ║
║  ✅ PDF Site Report Export                                     ║
║  ✅ Auto-Discovery API (${AutoDiscoveryAPI.availableLayers.size} layers)                          ║
║  ✅ Fiber KMZ Enhancement                                      ║
║  📡 Using backend API: /api/v1/energy/*                        ║
╚═══════════════════════════════════════════════════════════════╝
`);

// Usage example (can be called from land-power-app.js):
// const analysis = await DCHubEnhancements.EnhancedSiteAnalysis.analyze(33.4484, -112.074, { state: 'AZ' });
// DCHubEnhancements.PDFReportExport.downloadReport(analysis);
