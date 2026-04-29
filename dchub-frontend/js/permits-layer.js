/**
 * DC Hub Construction Permits Layer
 * ==================================
 * Displays construction permits discovered by the autonomous brain
 * 
 * Features:
 * - Fetches permits from backend API
 * - Color-coded by status (approved, under review, pending)
 * - Sized by MW capacity
 * - Click for details and news source
 * 
 * Installation:
 * 1. Add to land-power.html: <script src="js/permits-layer.js?v=1"></script>
 * 2. Add button to layer panel in HTML
 * 3. Add 'permits' to layers object in land-power-app.js
 */

const DCHubPermits = {
    config: {
        backendUrl: 'https://dchub.cloud',
        cacheTimeout: 300000, // 5 minutes
        layer: null,
        visible: false
    },
    
    cache: {
        data: null,
        timestamp: null
    },
    
    // Status colors
    statusColors: {
        'approved': '#22c55e',           // Green
        'under_review': '#f59e0b',       // Amber
        'pending': '#3b82f6',            // Blue
        'denied': '#ef4444',             // Red
        'construction': '#8b5cf6',       // Purple
        'under_construction': '#8b5cf6', // Purple (alias)
        'planned': '#06b6d4',            // Cyan
        'announced': '#a855f7',          // Light purple
        'default': '#6b7280'             // Gray
    },
    
    // Status icons
    statusIcons: {
        'approved': '✅',
        'under_review': '🔄',
        'pending': '⏳',
        'denied': '❌',
        'construction': '🏗️',
        'under_construction': '🏗️',
        'planned': '📐',
        'announced': '📢',
        'default': '📋'
    },
    
    /**
     * Initialize the permits layer
     */
    init(map, layerGroup) {
        this.map = map;
        this.layerGroup = layerGroup || L.layerGroup();
        console.log('📋 DC Hub Permits Layer initialized');
        return this;
    },
    
    /**
     * Geocode city/state to coordinates (fallback when lat/lng missing)
     */
    cityCoordinates: {
        'Temple, TX': [31.0982, -97.3428],
        'Phoenix, AZ': [33.4484, -112.0740],
        'Columbus, OH': [39.9612, -82.9988],
        'Manassas, VA': [38.7509, -77.4753],
        'Goodyear, AZ': [33.4353, -112.3577],
        'Irving, TX': [32.8140, -96.9489],
        'Hillsboro, OR': [45.5229, -122.9898],
        'Leesburg, VA': [39.1157, -77.5636],
        'Ashburn, VA': [39.0438, -77.4874],
        'Dallas, TX': [32.7767, -96.7970],
        'Mesa, AZ': [33.4152, -111.8315],
        'Chandler, AZ': [33.3062, -111.8413],
        'Atlanta, GA': [33.7490, -84.3880],
        'Chicago, IL': [41.8781, -87.6298],
        'Denver, CO': [39.7392, -104.9903],
        'Las Vegas, NV': [36.1699, -115.1398],
        'Salt Lake City, UT': [40.7608, -111.8910],
        'Unknown, US': null
    },
    
    /**
     * Normalize permit data from backend format
     */
    normalizePermit(raw) {
        // Map backend field names to frontend expected names
        const permit = {
            id: raw.id,
            project: raw.project_name || raw.project || 'Unknown Project',
            developer: raw.owner || raw.developer || 'Unknown',
            mw: raw.estimated_power_mw || raw.mw || 0,
            status: raw.status || 'announced',
            lat: raw.lat,
            lng: raw.lng,
            date: raw.issue_date || raw.created_at?.split(' ')[0] || 'N/A',
            city: raw.city || 'Unknown',
            state: raw.state || '',
            source: raw.source || 'discovery',
            sqft: raw.square_feet || 0
        };
        
        // If no coordinates, try to geocode from city/state
        if (!permit.lat || !permit.lng) {
            const cityKey = `${permit.city}, ${permit.state}`;
            const coords = this.cityCoordinates[cityKey];
            if (coords) {
                // Add slight randomness to prevent stacking
                permit.lat = coords[0] + (Math.random() - 0.5) * 0.05;
                permit.lng = coords[1] + (Math.random() - 0.5) * 0.05;
                permit.geocoded = true;
            }
        }
        
        return permit;
    },
    
    /**
     * Filter out non-permit records (news articles, etc.)
     */
    isValidPermit(permit) {
        // Skip if no MW capacity and no city (likely a news article)
        if (permit.mw === 0 && permit.city === 'Unknown') return false;
        // Skip if project name looks like a news headline
        if (permit.project.includes('?') || permit.project.length > 80) return false;
        return true;
    },
    async fetchPermits(options = {}) {
        // Check cache
        if (this.cache.data && this.cache.timestamp && 
            Date.now() - this.cache.timestamp < this.config.cacheTimeout) {
            console.log('📋 Using cached permits data');
            return this.cache.data;
        }
        
        try {
            let url = `${this.config.backendUrl}/api/infrastructure/permits`;
            const params = new URLSearchParams();
            
            if (options.status) params.append('status', options.status);
            if (options.min_mw) params.append('min_mw', options.min_mw);
            if (options.developer) params.append('developer', options.developer);
            
            if (params.toString()) url += '?' + params.toString();
            
            const response = await fetch(url, {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            });
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            
            const result = await response.json();
            
            if (result.success && result.data) {
                this.cache.data = result.data;
                this.cache.timestamp = Date.now();
                console.log(`📋 Fetched ${result.count} permits from backend`);
                return result.data;
            }
            
            return [];
            
        } catch (error) {
            console.warn('📋 Failed to fetch permits from backend:', error.message);
            // Return sample data as fallback
            return this.getSamplePermits();
        }
    },
    
    /**
     * Sample permits data (fallback)
     */
    getSamplePermits() {
        return [
            {id: 'permit-1', project: 'Meta Mesa Campus', developer: 'Meta', mw: 300, status: 'approved', lat: 33.41, lng: -111.83, date: '2024-12-15', source: 'Phoenix Business Journal'},
            {id: 'permit-2', project: 'Microsoft Goodyear Phase 2', developer: 'Microsoft', mw: 200, status: 'under_review', lat: 33.44, lng: -112.39, date: '2025-01-10', source: 'Data Center Dynamics'},
            {id: 'permit-3', project: 'QTS Phoenix Expansion', developer: 'QTS', mw: 150, status: 'approved', lat: 33.45, lng: -112.07, date: '2024-11-20', source: 'QTS Press Release'},
            {id: 'permit-4', project: 'Google Mesa Data Center', developer: 'Google', mw: 400, status: 'construction', lat: 33.38, lng: -111.72, date: '2024-08-01', source: 'AZ Republic'},
            {id: 'permit-5', project: 'AWS West Phoenix', developer: 'Amazon', mw: 250, status: 'pending', lat: 33.52, lng: -112.25, date: '2025-01-15', source: 'Industry Rumor'},
            {id: 'permit-6', project: 'Aligned Energy Chandler', developer: 'Aligned', mw: 180, status: 'approved', lat: 33.30, lng: -111.84, date: '2024-10-05', source: 'Aligned Press Release'},
            {id: 'permit-7', project: 'Digital Realty PHX2', developer: 'Digital Realty', mw: 120, status: 'approved', lat: 33.47, lng: -112.02, date: '2024-09-18', source: 'BizJournals'},
            {id: 'permit-8', project: 'Vantage Phoenix Campus', developer: 'Vantage', mw: 220, status: 'under_review', lat: 33.55, lng: -112.15, date: '2025-01-05', source: 'Data Center Frontier'},
        ];
    },
    
    /**
     * Load and display permits on map
     */
    async loadPermits(options = {}) {
        if (!this.map || !this.layerGroup) {
            console.error('📋 Permits layer not initialized');
            return;
        }
        
        // Clear existing markers
        this.layerGroup.clearLayers();
        
        let rawPermits = await this.fetchPermits(options);
        
        if (!rawPermits || rawPermits.length === 0) {
            console.log('📋 No permits to display');
            return;
        }
        
        // Normalize and filter permits
        const permits = rawPermits
            .map(p => this.normalizePermit(p))
            .filter(p => this.isValidPermit(p));
        
        console.log(`📋 Filtered to ${permits.length} valid permits from ${rawPermits.length} raw records`);
        
        let totalMW = 0;
        let displayedCount = 0;
        
        permits.forEach(permit => {
            // Skip permits without valid coordinates
            if (!permit.lat || !permit.lng) {
                console.warn('📋 Skipping permit with missing coordinates:', permit.project);
                return;
            }
            
            const color = this.statusColors[permit.status] || this.statusColors.default;
            const icon = this.statusIcons[permit.status] || this.statusIcons.default;
            
            // Size based on MW capacity
            const radius = Math.max(8, Math.min(20, (permit.mw || 50) / 20));
            
            totalMW += permit.mw || 0;
            displayedCount++;
            
            const marker = L.circleMarker([permit.lat, permit.lng], {
                radius: radius,
                fillColor: color,
                color: '#fff',
                weight: 2,
                opacity: 1,
                fillOpacity: 0.8
            });
            
            const locationText = permit.city && permit.state ? `${permit.city}, ${permit.state}` : '';
            const geocodedNote = permit.geocoded ? ' (approx. location)' : '';
            
            const popupContent = `
                <div style="min-width:250px;">
                    <div style="font-size:14px;font-weight:700;color:#fff;margin-bottom:8px;">
                        ${icon} ${permit.project}
                    </div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px;">
                        <div style="color:#888;">Developer</div>
                        <div style="color:#fff;font-weight:500;">${permit.developer}</div>
                        
                        <div style="color:#888;">Capacity</div>
                        <div style="color:#22c55e;font-weight:700;">${permit.mw} MW</div>
                        
                        <div style="color:#888;">Status</div>
                        <div style="color:${color};font-weight:600;text-transform:capitalize;">${(permit.status || 'unknown').replace('_', ' ')}</div>
                        
                        <div style="color:#888;">Location</div>
                        <div style="color:#fff;">${locationText}${geocodedNote}</div>
                        
                        <div style="color:#888;">Date</div>
                        <div style="color:#fff;">${permit.date}</div>
                        
                        ${permit.sqft ? `
                        <div style="color:#888;">Size</div>
                        <div style="color:#fff;">${(permit.sqft / 1000000).toFixed(1)}M sqft</div>
                        ` : ''}
                    </div>
                    ${permit.source ? `
                        <div style="margin-top:10px;padding-top:8px;border-top:1px solid #333;">
                            <div style="font-size:10px;color:#888;">Source</div>
                            <div style="font-size:11px;color:#3b82f6;">${permit.source}</div>
                        </div>
                    ` : ''}
                    <div style="margin-top:8px;padding:6px;background:rgba(139,92,246,0.1);border-radius:4px;border-left:3px solid #8b5cf6;">
                        <div style="font-size:10px;color:#8b5cf6;">📡 Auto-discovered by DC Hub</div>
                    </div>
                </div>
            `;
            
            marker.bindPopup(popupContent);
            marker.addTo(this.layerGroup);
        });
        
        console.log(`📋 Displayed ${displayedCount} permits on map (${totalMW.toLocaleString()} MW total)`);
        
        // Update count display if element exists
        const countEl = document.getElementById('count-permits');
        if (countEl) {
            countEl.textContent = displayedCount;
        }
        
        return permits;
    },
    
    /**
     * Toggle layer visibility
     */
    toggle(map) {
        if (!this.layerGroup) {
            this.init(map);
        }
        
        if (this.config.visible) {
            map.removeLayer(this.layerGroup);
            this.config.visible = false;
            console.log('📋 Permits layer hidden');
        } else {
            this.layerGroup.addTo(map);
            this.loadPermits();
            this.config.visible = true;
            console.log('📋 Permits layer shown');
        }
        
        return this.config.visible;
    },
    
    /**
     * Filter permits by status
     */
    async filterByStatus(status) {
        await this.loadPermits({ status: status });
    },
    
    /**
     * Get permits summary
     */
    async getSummary() {
        const permits = await this.fetchPermits();
        
        const summary = {
            total: permits.length,
            total_mw: permits.reduce((sum, p) => sum + (p.mw || 0), 0),
            by_status: {},
            by_developer: {}
        };
        
        permits.forEach(p => {
            // Count by status
            summary.by_status[p.status] = (summary.by_status[p.status] || 0) + 1;
            
            // Count by developer
            summary.by_developer[p.developer] = (summary.by_developer[p.developer] || 0) + 1;
        });
        
        return summary;
    }
};

// Export globally
window.DCHubPermits = DCHubPermits;

console.log(`
╔═══════════════════════════════════════════════════════════════╗
║  DC Hub Construction Permits Layer v1.0                        ║
╠═══════════════════════════════════════════════════════════════╣
║  ✅ Auto-discovered permits from news                          ║
║  ✅ MW capacity visualization                                   ║
║  ✅ Status color coding                                         ║
║  📡 Data source: Autonomous Brain                              ║
╚═══════════════════════════════════════════════════════════════╝
`);
