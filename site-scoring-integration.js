/**
 * DC Hub - Site Scoring Integration
 * Adds real-time site scoring popup on map click
 * Integrates with /api/site-score, /api/carbon/intensity, /api/renewable/solar endpoints
 * 
 * Add to land-power.html:
 * <script src="js/site-scoring-integration.js?v=1"></script>
 */

(function() {
    'use strict';
    
    // Configuration
    const CONFIG = {
        apiBase: window.location.origin,
        enableOnClick: true,
        showInExistingPopup: true,  // Add to existing popups vs separate popup
        cacheResults: true,
        cacheDuration: 5 * 60 * 1000  // 5 minutes
    };
    
    // Cache for site scores
    const scoreCache = new Map();
    
    // State lookup for coordinates (simplified US coverage)
    const STATE_BOUNDARIES = {
        'VA': { minLat: 36.5, maxLat: 39.5, minLon: -83.7, maxLon: -75.2 },
        'TX': { minLat: 25.8, maxLat: 36.5, minLon: -106.6, maxLon: -93.5 },
        'CA': { minLat: 32.5, maxLat: 42.0, minLon: -124.4, maxLon: -114.1 },
        'AZ': { minLat: 31.3, maxLat: 37.0, minLon: -114.8, maxLon: -109.0 },
        'NV': { minLat: 35.0, maxLat: 42.0, minLon: -120.0, maxLon: -114.0 },
        'GA': { minLat: 30.4, maxLat: 35.0, minLon: -85.6, maxLon: -80.8 },
        'NC': { minLat: 33.8, maxLat: 36.6, minLon: -84.3, maxLon: -75.5 },
        'OH': { minLat: 38.4, maxLat: 42.0, minLon: -84.8, maxLon: -80.5 },
        'IL': { minLat: 36.9, maxLat: 42.5, minLon: -91.5, maxLon: -87.0 },
        'NY': { minLat: 40.5, maxLat: 45.0, minLon: -79.8, maxLon: -71.8 },
        'NJ': { minLat: 38.9, maxLat: 41.4, minLon: -75.6, maxLon: -73.9 },
        'PA': { minLat: 39.7, maxLat: 42.3, minLon: -80.5, maxLon: -74.7 },
        'WA': { minLat: 45.5, maxLat: 49.0, minLon: -124.8, maxLon: -116.9 },
        'OR': { minLat: 41.9, maxLat: 46.3, minLon: -124.6, maxLon: -116.5 },
        'CO': { minLat: 36.9, maxLat: 41.0, minLon: -109.0, maxLon: -102.0 },
        'UT': { minLat: 36.9, maxLat: 42.0, minLon: -114.0, maxLon: -109.0 },
        'FL': { minLat: 24.5, maxLat: 31.0, minLon: -87.6, maxLon: -80.0 },
        'SC': { minLat: 32.0, maxLat: 35.2, minLon: -83.4, maxLon: -78.5 },
        'TN': { minLat: 35.0, maxLat: 36.7, minLon: -90.3, maxLon: -81.6 },
        'IN': { minLat: 37.8, maxLat: 41.8, minLon: -88.1, maxLon: -84.8 },
        'IA': { minLat: 40.4, maxLat: 43.5, minLon: -96.6, maxLon: -90.1 },
        'NE': { minLat: 40.0, maxLat: 43.0, minLon: -104.0, maxLon: -95.3 },
        'OK': { minLat: 33.6, maxLat: 37.0, minLon: -103.0, maxLon: -94.4 }
    };
    
    /**
     * Estimate state from coordinates
     */
    function getStateFromCoords(lat, lon) {
        for (const [state, bounds] of Object.entries(STATE_BOUNDARIES)) {
            if (lat >= bounds.minLat && lat <= bounds.maxLat &&
                lon >= bounds.minLon && lon <= bounds.maxLon) {
                return state;
            }
        }
        // Default fallback based on general region
        if (lon < -115) return 'CA';
        if (lon < -100) return 'TX';
        if (lon < -85) return 'IL';
        if (lat > 40) return 'NY';
        return 'VA'; // Default to VA
    }
    
    /**
     * Fetch site score from API
     */
    async function fetchSiteScore(lat, lon) {
        const cacheKey = `${lat.toFixed(4)},${lon.toFixed(4)}`;
        
        // Check cache
        if (CONFIG.cacheResults && scoreCache.has(cacheKey)) {
            const cached = scoreCache.get(cacheKey);
            if (Date.now() - cached.timestamp < CONFIG.cacheDuration) {
                return cached.data;
            }
        }
        
        const state = getStateFromCoords(lat, lon);
        
        try {
            const response = await fetch(
                `${CONFIG.apiBase}/api/site-score?lat=${lat}&lon=${lon}&state=${state}`
            );
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            
            const data = await response.json();
            
            // Cache result
            if (CONFIG.cacheResults) {
                scoreCache.set(cacheKey, {
                    data: data,
                    timestamp: Date.now()
                });
            }
            
            return data;
        } catch (error) {
            console.error('Site score fetch error:', error);
            return null;
        }
    }
    
    /**
     * Fetch renewable potential
     */
    async function fetchRenewablePotential(lat, lon) {
        try {
            const [solarRes, windRes] = await Promise.all([
                fetch(`${CONFIG.apiBase}/api/renewable/solar?lat=${lat}&lon=${lon}`),
                fetch(`${CONFIG.apiBase}/api/renewable/wind?lat=${lat}&lon=${lon}`)
            ]);
            
            const solar = solarRes.ok ? await solarRes.json() : null;
            const wind = windRes.ok ? await windRes.json() : null;
            
            return { solar, wind };
        } catch (error) {
            console.error('Renewable fetch error:', error);
            return { solar: null, wind: null };
        }
    }
    
    /**
     * Get score color based on value
     */
    function getScoreColor(score) {
        if (score >= 80) return '#10b981'; // Green
        if (score >= 60) return '#fbbf24'; // Yellow
        if (score >= 40) return '#f97316'; // Orange
        return '#ef4444'; // Red
    }
    
    /**
     * Get score grade
     */
    function getScoreGrade(score) {
        if (score >= 90) return 'A+';
        if (score >= 80) return 'A';
        if (score >= 70) return 'B';
        if (score >= 60) return 'C';
        if (score >= 50) return 'D';
        return 'F';
    }
    
    /**
     * Format price rating
     */
    function formatPriceRating(rating) {
        const ratings = {
            'excellent': '⭐ Excellent',
            'good': '✓ Good',
            'average': '• Average',
            'above_average': '△ Above Avg',
            'expensive': '⚠ Expensive'
        };
        return ratings[rating] || rating;
    }
    
    /**
     * Format carbon rating
     */
    function formatCarbonRating(rating) {
        const ratings = {
            'excellent': '🌱 Very Clean',
            'good': '✓ Clean',
            'average': '• Average',
            'below_average': '△ Below Avg',
            'poor': '⚠ High Carbon'
        };
        return ratings[rating] || rating;
    }
    
    /**
     * Generate site score popup HTML
     */
    function generateScorePopupHTML(score, lat, lon) {
        if (!score) {
            return `
                <div style="padding:20px;text-align:center;">
                    <div style="font-size:24px;margin-bottom:8px;">⚠️</div>
                    <div style="color:#f97316;">Could not calculate site score</div>
                    <div style="font-size:11px;color:#666;margin-top:8px;">${lat.toFixed(4)}, ${lon.toFixed(4)}</div>
                </div>
            `;
        }
        
        const overallColor = getScoreColor(score.overall_score);
        const grade = getScoreGrade(score.overall_score);
        const state = getStateFromCoords(lat, lon);
        
        return `
            <div style="min-width:320px;font-family:Inter,system-ui,sans-serif;">
                <!-- Header -->
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid #333;">
                    <div style="width:56px;height:56px;border-radius:12px;background:linear-gradient(135deg,${overallColor}22,${overallColor}44);border:2px solid ${overallColor};display:flex;flex-direction:column;align-items:center;justify-content:center;">
                        <div style="font-size:20px;font-weight:800;color:${overallColor};">${Math.round(score.overall_score)}</div>
                        <div style="font-size:9px;color:#888;">/ 100</div>
                    </div>
                    <div style="flex:1;">
                        <div style="font-size:16px;font-weight:700;color:#fff;">Site Score: ${grade}</div>
                        <div style="font-size:11px;color:#888;margin-top:2px;">📍 ${lat.toFixed(4)}, ${lon.toFixed(4)}</div>
                        <div style="font-size:10px;color:#666;margin-top:2px;">${state} • ${new Date().toLocaleTimeString()}</div>
                    </div>
                </div>
                
                <!-- Score Breakdown -->
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px;">
                    ${generateScoreItem('⚡ Energy', score.energy_score, score.energy_details?.price_rating)}
                    ${generateScoreItem('🌱 Carbon', score.carbon_score, score.carbon_details?.rating)}
                    ${generateScoreItem('🔌 Infra', score.infrastructure_score)}
                    ${generateScoreItem('🌐 Connect', score.connectivity_score)}
                    ${generateScoreItem('🛡️ Risk', score.risk_score)}
                    ${generateScoreItem('💰 Cost', score.cost_score)}
                </div>
                
                <!-- Energy Details -->
                <div style="background:#1a1a2e;border-radius:8px;padding:10px;margin-bottom:10px;">
                    <div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Energy Details</div>
                    <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
                        <span style="color:#aaa;">Industrial Rate</span>
                        <span style="color:#10b981;font-weight:600;font-family:'JetBrains Mono',monospace;">$${(score.energy_details?.industrial_price_cents_kwh / 100).toFixed(3)}/kWh</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
                        <span style="color:#aaa;">Est. Monthly (1MW)</span>
                        <span style="color:#fff;font-family:'JetBrains Mono',monospace;">$${(score.energy_details?.estimated_monthly_cost_per_mw || 0).toLocaleString()}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;font-size:12px;">
                        <span style="color:#aaa;">Price Rating</span>
                        <span style="color:#888;">${formatPriceRating(score.energy_details?.price_rating)}</span>
                    </div>
                </div>
                
                <!-- Carbon Details -->
                <div style="background:#1a1a2e;border-radius:8px;padding:10px;margin-bottom:10px;">
                    <div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Carbon Intensity</div>
                    <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
                        <span style="color:#aaa;">Grid Intensity</span>
                        <span style="color:#fbbf24;font-weight:600;font-family:'JetBrains Mono',monospace;">${score.carbon_details?.carbon_intensity_gco2_kwh || 'N/A'} gCO₂/kWh</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
                        <span style="color:#aaa;">eGRID Region</span>
                        <span style="color:#fff;">${score.carbon_details?.egrid_subregion || 'N/A'}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;font-size:12px;">
                        <span style="color:#aaa;">Carbon Rating</span>
                        <span style="color:#888;">${formatCarbonRating(score.carbon_details?.rating)}</span>
                    </div>
                </div>
                
                <!-- Actions -->
                <div style="display:flex;gap:8px;margin-top:12px;">
                    <button onclick="SiteScoring.loadRenewables(${lat}, ${lon})" style="flex:1;padding:8px;background:linear-gradient(135deg,#10b981,#059669);border:none;border-radius:6px;color:#fff;font-size:11px;font-weight:600;cursor:pointer;">
                        ☀️ Solar/Wind Potential
                    </button>
                    <button onclick="SiteScoring.addToComparison(${lat}, ${lon}, ${score.overall_score})" style="flex:1;padding:8px;background:#6366f1;border:none;border-radius:6px;color:#fff;font-size:11px;font-weight:600;cursor:pointer;">
                        📊 Add to Compare
                    </button>
                </div>
                
                <div style="margin-top:10px;font-size:9px;color:#666;text-align:center;">
                    Data: EIA, EPA eGRID • ${score.energy_details?.source || 'DC Hub'}
                </div>
            </div>
        `;
    }
    
    /**
     * Generate individual score item
     */
    function generateScoreItem(label, score, subtext) {
        const color = getScoreColor(score);
        return `
            <div style="background:#1a1a2e;padding:8px 10px;border-radius:6px;display:flex;justify-content:space-between;align-items:center;">
                <div>
                    <div style="font-size:11px;color:#888;">${label}</div>
                    ${subtext ? `<div style="font-size:9px;color:#666;">${subtext}</div>` : ''}
                </div>
                <div style="font-size:16px;font-weight:700;color:${color};font-family:'JetBrains Mono',monospace;">${Math.round(score)}</div>
            </div>
        `;
    }
    
    /**
     * Generate renewable popup HTML
     */
    function generateRenewablePopupHTML(data, lat, lon) {
        const { solar, wind } = data;
        
        let html = `
            <div style="min-width:300px;font-family:Inter,system-ui,sans-serif;">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid #333;">
                    <span style="font-size:20px;">🌱</span>
                    <span style="font-size:14px;font-weight:700;color:#10b981;">Renewable Energy Potential</span>
                </div>
        `;
        
        if (solar && !solar.error) {
            const solarColor = getScoreColor(solar.capacity_factor_pct * 4); // Scale 0-25% to 0-100
            html += `
                <div style="background:#1a1a2e;border-radius:8px;padding:12px;margin-bottom:10px;border-left:3px solid #fbbf24;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <span style="font-size:12px;font-weight:600;color:#fbbf24;">☀️ Solar PV Potential</span>
                        <span style="padding:3px 8px;background:${solarColor}22;color:${solarColor};border-radius:4px;font-size:10px;font-weight:600;">${solar.solar_rating?.toUpperCase() || 'N/A'}</span>
                    </div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:11px;">
                        <div><span style="color:#888;">Capacity Factor:</span> <span style="color:#fff;font-weight:600;">${solar.capacity_factor_pct?.toFixed(1)}%</span></div>
                        <div><span style="color:#888;">Annual GHI:</span> <span style="color:#fff;">${solar.annual_ghi_kwh_m2} kWh/m²</span></div>
                        <div><span style="color:#888;">1MW Annual:</span> <span style="color:#10b981;font-weight:600;">${solar.annual_production_mwh?.toLocaleString()} MWh</span></div>
                    </div>
                </div>
            `;
        }
        
        if (wind && !wind.error) {
            const windColor = getScoreColor(wind.estimated_capacity_factor_pct * 2.5); // Scale 0-40% to 0-100
            html += `
                <div style="background:#1a1a2e;border-radius:8px;padding:12px;margin-bottom:10px;border-left:3px solid #06b6d4;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <span style="font-size:12px;font-weight:600;color:#06b6d4;">💨 Wind Potential</span>
                        <span style="padding:3px 8px;background:${windColor}22;color:${windColor};border-radius:4px;font-size:10px;font-weight:600;">${wind.wind_rating?.toUpperCase() || 'N/A'}</span>
                    </div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:11px;">
                        <div><span style="color:#888;">Wind Speed:</span> <span style="color:#fff;font-weight:600;">${wind.mean_wind_speed_ms?.toFixed(1)} m/s</span></div>
                        <div><span style="color:#888;">Wind Class:</span> <span style="color:#fff;">Class ${wind.wind_class}</span></div>
                        <div><span style="color:#888;">Power Density:</span> <span style="color:#fff;">${wind.power_density_w_m2} W/m²</span></div>
                        <div><span style="color:#888;">Est. CF:</span> <span style="color:#10b981;font-weight:600;">${wind.estimated_capacity_factor_pct}%</span></div>
                    </div>
                </div>
            `;
        }
        
        html += `
                <div style="font-size:9px;color:#666;text-align:center;margin-top:8px;">
                    Data: NREL PVWatts & Wind Toolkit
                </div>
            </div>
        `;
        
        return html;
    }
    
    /**
     * Show site score popup on map
     */
    async function showSiteScorePopup(lat, lon, latlng) {
        if (typeof map === 'undefined' || typeof L === 'undefined') {
            console.warn('Map not available');
            return;
        }
        
        // Show loading popup
        const loadingPopup = L.popup({ maxWidth: 400 })
            .setLatLng(latlng)
            .setContent(`
                <div style="padding:30px;text-align:center;">
                    <div style="font-size:32px;margin-bottom:12px;">📊</div>
                    <div style="color:#6366f1;font-weight:600;">Calculating Site Score...</div>
                    <div style="font-size:11px;color:#888;margin-top:8px;">Analyzing energy, carbon, infrastructure</div>
                </div>
            `)
            .openOn(map);
        
        // Fetch score
        const score = await fetchSiteScore(lat, lon);
        
        // Update popup with results
        loadingPopup.setContent(generateScorePopupHTML(score, lat, lon));
    }
    
    /**
     * Load renewable potential (called from popup button)
     */
    async function loadRenewables(lat, lon) {
        if (typeof map === 'undefined' || typeof L === 'undefined') return;
        
        // Show loading
        const popup = L.popup({ maxWidth: 380 })
            .setLatLng([lat, lon])
            .setContent(`
                <div style="padding:20px;text-align:center;">
                    <div style="font-size:24px;margin-bottom:8px;">🌱</div>
                    <div style="color:#10b981;">Loading renewable potential...</div>
                </div>
            `)
            .openOn(map);
        
        const data = await fetchRenewablePotential(lat, lon);
        popup.setContent(generateRenewablePopupHTML(data, lat, lon));
    }
    
    // Site comparison storage
    const comparisonSites = [];
    
    /**
     * Add site to comparison list
     */
    function addToComparison(lat, lon, score) {
        const state = getStateFromCoords(lat, lon);
        comparisonSites.push({
            lat,
            lon,
            state,
            score,
            timestamp: Date.now()
        });
        
        // Show notification
        showNotification(`📊 Added to comparison (${comparisonSites.length} sites)`);
        
        // Update comparison panel if it exists
        updateComparisonPanel();
    }
    
    /**
     * Show notification toast
     */
    function showNotification(message) {
        const toast = document.createElement('div');
        toast.style.cssText = `
            position: fixed;
            bottom: 80px;
            left: 50%;
            transform: translateX(-50%);
            background: #10b981;
            color: #fff;
            padding: 12px 24px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            z-index: 10000;
            animation: slideUp 0.3s ease;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        `;
        toast.textContent = message;
        document.body.appendChild(toast);
        
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transition = 'opacity 0.3s';
            setTimeout(() => toast.remove(), 300);
        }, 2000);
    }
    
    /**
     * Update comparison panel (placeholder)
     */
    function updateComparisonPanel() {
        // This can be expanded to show a side panel with comparison
        console.log('Comparison sites:', comparisonSites);
    }
    
    /**
     * Initialize site scoring integration
     */
    function init() {
        console.log('🎯 Site Scoring Integration initializing...');
        
        // Wait for map to be ready
        const checkMap = setInterval(() => {
            if (typeof map !== 'undefined' && typeof L !== 'undefined') {
                clearInterval(checkMap);
                
                // Add click handler for site scoring
                map.on('click', function(e) {
                    // Check if site scoring mode is enabled (you can add a toggle button)
                    const scoringBtn = document.getElementById('site-scoring-btn');
                    
                    // If no specific button, check if shift key is held or use default behavior
                    if (e.originalEvent.shiftKey) {
                        showSiteScorePopup(e.latlng.lat, e.latlng.lng, e.latlng);
                    }
                });
                
                // Add keyboard shortcut info
                console.log('✅ Site Scoring ready! Hold SHIFT + Click to see site scores');
                
                // Add site scoring button to controls if layer-toggles exists
                addSiteScoreButton();
            }
        }, 500);
    }
    
    /**
     * Add site scoring toggle button
     */
    function addSiteScoreButton() {
        const layerToggles = document.querySelector('.layer-toggles');
        if (!layerToggles) return;
        
        const btn = document.createElement('button');
        btn.id = 'site-scoring-btn';
        btn.className = 'layer-btn';
        btn.innerHTML = '📊 Site Score';
        btn.title = 'Click to enable site scoring mode, then click map';
        btn.style.background = 'linear-gradient(135deg, #6366f1, #4f46e5)';
        btn.style.color = '#fff';
        btn.style.borderColor = '#6366f1';
        
        btn.addEventListener('click', function() {
            this.classList.toggle('active');
            
            if (this.classList.contains('active')) {
                this.innerHTML = '📊 Score Mode ON';
                this.style.background = 'linear-gradient(135deg, #10b981, #059669)';
                this.style.borderColor = '#10b981';
                
                // Enable click-to-score mode
                window.siteScoreMode = true;
                showNotification('📊 Site Score Mode: Click anywhere on map');
            } else {
                this.innerHTML = '📊 Site Score';
                this.style.background = 'linear-gradient(135deg, #6366f1, #4f46e5)';
                this.style.borderColor = '#6366f1';
                
                window.siteScoreMode = false;
            }
        });
        
        layerToggles.appendChild(btn);
        
        // Add map click handler for score mode
        if (typeof map !== 'undefined') {
            map.on('click', function(e) {
                if (window.siteScoreMode) {
                    showSiteScorePopup(e.latlng.lat, e.latlng.lng, e.latlng);
                }
            });
        }
    }
    
    // Expose API
    window.SiteScoring = {
        init,
        fetchSiteScore,
        showSiteScorePopup,
        loadRenewables,
        addToComparison,
        getComparisonSites: () => comparisonSites,
        clearComparison: () => { comparisonSites.length = 0; }
    };
    
    // Auto-initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
    
})();
