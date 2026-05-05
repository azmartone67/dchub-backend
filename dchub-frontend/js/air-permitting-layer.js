/**
 * DC Hub Air Permitting Layer v1.0
 * ===================================
 * Renders EPA nonattainment areas, AQS monitors, Federal Class I areas,
 * and parcel-level permitting scores on the Land & Power Map.
 *
 * Follows the same pattern as permits-layer.js and water-drought-layer.js.
 *
 * Installation:
 * 1. Add to land-power-map.html (near bottom, with other layer scripts):
 *    <script src="js/air-permitting-layer.js?v=1"></script>
 *
 * 2. Add button to Environmental & Risk panel:
 *    <button class="layer-btn" id="air-permitting-btn"
 *            style="background:linear-gradient(135deg,rgba(239,68,68,0.1),rgba(245,158,11,0.1));border-color:rgba(239,68,68,0.3)">
 *      🏭 Air Permitting
 *      <span class="count" style="background:rgba(239,68,68,0.3);color:#ef4444">EPA</span>
 *    </button>
 *
 * 3. Add click handler in land-power-app.js (inside the layer-btn forEach):
 *    if (this.id === 'air-permitting-btn') {
 *        if (window.DCHubAirPermitting) {
 *            window.DCHubAirPermitting.toggle(map);
 *            this.classList.toggle('active');
 *        }
 *        return;
 *    }
 *
 * Backend: Python/FastAPI on Replit at `config.backendUrl`.
 * Endpoints used:
 *   GET  /api/infrastructure/air-permitting/nonattainment
 *   GET  /api/infrastructure/air-permitting/monitors
 *   GET  /api/infrastructure/air-permitting/class1
 *   POST /api/infrastructure/air-permitting/score
 */

const DCHubAirPermitting = {
    config: {
        // Endpoints live inside main.py on your existing backend (dchub.cloud)
        backendUrl: (window.location.hostname === 'localhost' ? 'http://localhost:5000' : 'https://dchub.cloud'),
        fallbackUrl: '',  // no separate fallback — single source of truth
        cacheTimeout: 600000,  // 10 min
        visible: false,
        layerGroups: {
            ozone: null,
            pm25: null,
            pm10: null,
            monitors: null,
            class1: null
        },
        mapClickHandler: null
    },

    cache: {
        nonattainment: null,
        monitors: null,
        class1: null,
        timestamps: {}
    },

    // Styling — matches DC Hub dark-theme palette
    styles: {
        nonattainment: {
            ozone: { color: '#EF4444', fillColor: '#EF4444', fillOpacity: 0.28, weight: 1 },
            pm25:  { color: '#F59E0B', fillColor: '#F59E0B', fillOpacity: 0.24, weight: 1 },
            pm10:  { color: '#B45309', fillColor: '#B45309', fillOpacity: 0.36, weight: 1 }
        },
        monitor: {
            safe:      '#22c55e',
            caution:   '#f59e0b',
            exceed:    '#ef4444'
        },
        class1: {
            color: '#2DD4BF', weight: 1, dashArray: '4,4',
            fillColor: '#2DD4BF', fillOpacity: 0.05
        },
        classification_colors: {
            'Marginal':    '#FDE68A',
            'Moderate':    '#FBBF24',
            'Serious':     '#F97316',
            'Severe':      '#EF4444',
            'Extreme':     '#991B1B',
            'Maintenance': '#A3E635'
        }
    },

    /**
     * Initialize the layer (idempotent)
     */
    init(map) {
        if (!map) { console.error('🏭 Air Permitting: no map passed to init'); return this; }
        this.map = map;
        this.config.layerGroups.ozone     = this.config.layerGroups.ozone     || L.layerGroup();
        this.config.layerGroups.pm25      = this.config.layerGroups.pm25      || L.layerGroup();
        this.config.layerGroups.pm10      = this.config.layerGroups.pm10      || L.layerGroup();
        this.config.layerGroups.monitors  = this.config.layerGroups.monitors  || L.layerGroup();
        this.config.layerGroups.class1    = this.config.layerGroups.class1    || L.layerGroup();
        console.log('🏭 DC Hub Air Permitting Layer initialized');
        return this;
    },

    /**
     * Cached fetch helper with fallback to sample data
     */
    async _fetchCached(key, endpoint, options) {
        if (this.cache[key] && this.cache.timestamps[key] &&
            Date.now() - this.cache.timestamps[key] < this.config.cacheTimeout) {
            return this.cache[key];
        }

        const urls = [
            `${this.config.backendUrl}${endpoint}`,
            this.config.fallbackUrl ? `${this.config.fallbackUrl}${endpoint}` : null
        ].filter(Boolean);

        for (const url of urls) {
            try {
                const r = await fetch(url, options || { headers: { 'Accept': 'application/json' } });
                if (!r.ok) continue;
                const j = await r.json();
                if (j.success && j.data) {
                    this.cache[key] = j.data;
                    this.cache.timestamps[key] = Date.now();
                    return j.data;
                }
            } catch (e) {
                console.warn('🏭 Backend fetch failed:', url, e.message);
            }
        }

        // Fall back to sample data
        console.warn('🏭 Using fallback sample data for', key);
        return this._getSampleData(key);
    },

    /**
     * Sample data used when backend is unreachable
     */
    _getSampleData(key) {
        if (key === 'nonattainment') {
            return { type: 'FeatureCollection', features: [
                { type:'Feature', geometry:{ type:'Polygon', coordinates:[[[-112.8,32.9],[-111.4,32.9],[-111.4,34.0],[-112.8,34.0],[-112.8,32.9]]] }, properties:{ pollutant:'ozone', name:'Phoenix-Mesa', classification:'Moderate' } },
                { type:'Feature', geometry:{ type:'Polygon', coordinates:[[[-97.8,32.2],[-96.1,32.2],[-96.1,33.7],[-97.8,33.7],[-97.8,32.2]]] }, properties:{ pollutant:'ozone', name:'Dallas-Fort Worth', classification:'Moderate' } },
                { type:'Feature', geometry:{ type:'Polygon', coordinates:[[[-77.8,38.3],[-76.8,38.3],[-76.8,39.3],[-77.8,39.3],[-77.8,38.3]]] }, properties:{ pollutant:'ozone', name:'Northern Virginia', classification:'Marginal' } },
                { type:'Feature', geometry:{ type:'Polygon', coordinates:[[[-113.0,33.2],[-112.3,33.2],[-112.3,33.8],[-113.0,33.8],[-113.0,33.2]]] }, properties:{ pollutant:'pm10', name:'Phoenix West', classification:'Serious' } }
            ]};
        }
        if (key === 'monitors') {
            return [
                { id:'AQS-04-013-4003', pol:'PM10',  dv:165,   lat:33.42, lon:-112.09, naaqs:150,   pct_of_naaqs:110 },
                { id:'AQS-48-113-0069', pol:'O3',    dv:0.076, lat:32.82, lon:-96.83,  naaqs:0.070, pct_of_naaqs:108 },
                { id:'AQS-51-107-1005', pol:'PM2.5', dv:8.6,   lat:38.95, lon:-77.45,  naaqs:9,     pct_of_naaqs:96  }
            ];
        }
        if (key === 'class1') {
            return [
                { name:'Grand Canyon NP', lat:36.10, lon:-112.10 },
                { name:'Shenandoah NP', lat:38.50, lon:-78.40 },
                { name:'Big Bend NP', lat:29.30, lon:-103.30 }
            ];
        }
        return [];
    },

    /**
     * Load and render nonattainment polygons
     */
    async loadNonattainment() {
        const geojson = await this._fetchCached(
            'nonattainment', '/api/infrastructure/air-permitting/nonattainment'
        );

        ['ozone', 'pm25', 'pm10'].forEach(k => this.config.layerGroups[k].clearLayers());

        (geojson.features || []).forEach(f => {
            const pol = f.properties.pollutant;
            const style = this.styles.nonattainment[pol] || this.styles.nonattainment.pm10;
            L.geoJSON(f, { style })
              .bindTooltip(
                `<b>${f.properties.name}</b><br>${pol.toUpperCase()} · ${f.properties.classification}<br><i>EPA Green Book</i>`,
                { sticky: true }
              )
              .addTo(this.config.layerGroups[pol]);
        });
    },

    /**
     * Load and render AQS monitors
     */
    async loadMonitors() {
        const monitors = await this._fetchCached(
            'monitors', '/api/infrastructure/air-permitting/monitors'
        );
        this.config.layerGroups.monitors.clearLayers();
        (monitors || []).forEach(m => {
            const pct = m.pct_of_naaqs || (m.dv / m.naaqs * 100);
            const color = pct > 100 ? this.styles.monitor.exceed
                        : pct > 85  ? this.styles.monitor.caution
                        : this.styles.monitor.safe;
            L.circleMarker([m.lat, m.lon], {
                radius: 5, color: '#fff', weight: 1.5,
                fillColor: color, fillOpacity: 0.95
            })
            .bindPopup(this._monitorPopup(m, pct))
            .addTo(this.config.layerGroups.monitors);
        });
    },

    /**
     * Load and render Class I areas + consultation buffers
     */
    async loadClass1() {
        const areas = await this._fetchCached(
            'class1', '/api/infrastructure/air-permitting/class1'
        );
        this.config.layerGroups.class1.clearLayers();
        (areas || []).forEach(a => {
            L.circle([a.lat, a.lon], {
                ...this.styles.class1,
                radius: 300000  // 300 km
            })
            .bindTooltip(`<b>${a.name}</b><br><i>300 km FLM consultation buffer</i>`, { sticky: true })
            .addTo(this.config.layerGroups.class1);

            L.circleMarker([a.lat, a.lon], {
                radius: 4, color: '#065F46', weight: 1,
                fillColor: this.styles.class1.fillColor, fillOpacity: 0.9
            })
            .bindTooltip(`<b>${a.name}</b>`)
            .addTo(this.config.layerGroups.class1);
        });
    },

    /**
     * Register a click handler so users can drop a pin and get a score
     */
    _bindMapClick() {
        if (this.config.mapClickHandler) return;
        const self = this;
        this.config.mapClickHandler = function (e) {
            if (!self.config.visible) return;
            self.scoreLocation(e.latlng.lat, e.latlng.lng);
        };
        this.map.on('click', this.config.mapClickHandler);
    },

    _unbindMapClick() {
        if (!this.config.mapClickHandler) return;
        this.map.off('click', this.config.mapClickHandler);
        this.config.mapClickHandler = null;
    },

    /**
     * POST to scoring endpoint and render result in a popup
     */
    async scoreLocation(lat, lon, capacityMW) {
        const payload = { lat, lon, capacity_mw: capacityMW || 100 };
        try {
            const r = await fetch(
                `${this.config.backendUrl}/api/infrastructure/air-permitting/score`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                    body: JSON.stringify(payload)
                }
            );
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const j = await r.json();
            if (!j.success) throw new Error('Score endpoint error');
            this._showScorePopup(lat, lon, j.data);
        } catch (e) {
            console.warn('🏭 Score fetch failed:', e.message);
            this._showScorePopup(lat, lon, this._mockScore(lat, lon));
        }
    },

    _mockScore(lat, lon) {
        // Very crude fallback so UI still works without backend
        return {
            score: 75,
            verdict_short: '(offline) Score unavailable — using demo fallback.',
            pathway: 'Minor Source Permit',
            offset_estimate_usd: 'N/A',
            pollutants: {
                'PM10':{s:'green',d:'Attainment'},
                'PM2.5':{s:'green',d:'Attainment'},
                'O3':{s:'yellow',d:'Attainment (approx)'},
                'NO2':{s:'green',d:'Attainment'},
                'GHG':{s:'yellow',d:'Above 75k tpy PSD threshold'}
            }
        };
    },

    /**
     * Rendered popup for a parcel score — matches dchub dark theme
     */
    _showScorePopup(lat, lon, data) {
        const scoreColor = data.score >= 75 ? '#22c55e' : data.score >= 50 ? '#f59e0b' : '#ef4444';
        const pathwayColor = data.pathway.startsWith('NNSR') ? '#ef4444'
                           : data.pathway.startsWith('PSD')  ? '#8b5cf6'
                           : data.pathway.startsWith('Syn')  ? '#f59e0b'
                           : '#22c55e';

        const chips = Object.entries(data.pollutants || {}).map(([p, v]) => {
            const bg = v.s === 'green' ? 'rgba(34,197,94,0.15)'
                     : v.s === 'yellow' ? 'rgba(245,158,11,0.15)'
                     : 'rgba(239,68,68,0.15)';
            const fg = v.s === 'green' ? '#22c55e'
                     : v.s === 'yellow' ? '#f59e0b'
                     : '#ef4444';
            return `<span style="display:inline-block;padding:2px 8px;margin:2px;background:${bg};color:${fg};border-radius:3px;font-size:10px;font-weight:700;" title="${v.d}">${p}</span>`;
        }).join('');

        const html = `
            <div style="min-width:320px;font-family:-apple-system,sans-serif;">
              <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;">
                <div style="font-size:32px;font-weight:800;color:${scoreColor};line-height:1;">${data.score}</div>
                <div>
                  <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:#888;font-weight:700;">Air Permitting Score</div>
                  <div style="font-size:11px;color:#ccc;margin-top:2px;">${lat.toFixed(4)}°, ${lon.toFixed(4)}°</div>
                </div>
              </div>
              <div style="font-size:12px;color:#fff;line-height:1.4;margin-bottom:10px;">${data.verdict_short || ''}</div>
              <div style="padding:8px 10px;background:rgba(0,0,0,0.3);border-radius:4px;margin-bottom:10px;display:flex;align-items:center;gap:8px;">
                <span style="font-size:10px;text-transform:uppercase;color:#888;letter-spacing:0.5px;">Pathway</span>
                <span style="margin-left:auto;color:${pathwayColor};font-weight:700;font-size:11px;">${data.pathway}</span>
              </div>
              <div style="font-size:10px;text-transform:uppercase;color:#888;letter-spacing:0.8px;margin-bottom:4px;">Pollutants</div>
              <div style="margin-bottom:10px;">${chips}</div>
              ${data.offset_estimate_usd ? `
                <div style="padding:8px;background:rgba(139,92,246,0.1);border-left:3px solid #8b5cf6;border-radius:3px;font-size:11px;">
                  <div style="color:#8b5cf6;font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;">Offset Est.</div>
                  <div style="color:#fff;margin-top:2px;">${data.offset_estimate_usd}</div>
                </div>
              ` : ''}
              ${data.state_context ? `
                <div style="font-size:11px;color:#aaa;margin-top:10px;padding-top:8px;border-top:1px solid #333;line-height:1.45;">
                  <b style="color:#fff;">${data.state || ''} context:</b> ${data.state_context}
                </div>
              ` : ''}
              <div style="margin-top:10px;padding-top:8px;border-top:1px solid #333;font-size:10px;color:#666;">
                📡 Data: EPA Green Book · AQS · NPS FLM · NEI
              </div>
            </div>
        `;

        L.popup({ maxWidth: 380, className: 'dchub-air-popup' })
         .setLatLng([lat, lon])
         .setContent(html)
         .openOn(this.map);
    },

    _monitorPopup(m, pct) {
        const color = pct > 100 ? '#ef4444' : pct > 85 ? '#f59e0b' : '#22c55e';
        const unit = m.pol === 'O3' ? 'ppm' : 'µg/m³';
        return `
            <div style="min-width:200px;font-family:-apple-system,sans-serif;">
              <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">AQS Monitor</div>
              <div style="font-size:13px;font-weight:700;color:#fff;margin:2px 0 8px;">${m.pol} · ${m.id}</div>
              <div style="display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12px;">
                <span style="color:#888;">Design Value</span><span style="color:${color};font-weight:700;">${m.dv} ${unit}</span>
                <span style="color:#888;">NAAQS</span><span style="color:#fff;">${m.naaqs} ${unit}</span>
                <span style="color:#888;">% of NAAQS</span><span style="color:${color};font-weight:700;">${pct.toFixed(0)}%</span>
                ${m.year ? `<span style="color:#888;">Year</span><span style="color:#ccc;">${m.year}</span>` : ''}
              </div>
            </div>
        `;
    },

    /**
     * Public: toggle the layer on/off
     */
    async toggle(map) {
        if (!this.map) this.init(map);

        if (this.config.visible) {
            Object.values(this.config.layerGroups).forEach(g => this.map.removeLayer(g));
            this._unbindMapClick();
            this.config.visible = false;
            console.log('🏭 Air Permitting layer hidden');
            return false;
        }

        // Show
        Object.values(this.config.layerGroups).forEach(g => g.addTo(this.map));
        this.config.visible = true;

        // Lazy-load data in parallel
        await Promise.all([
            this.loadNonattainment(),
            this.loadMonitors(),
            this.loadClass1()
        ]);

        this._bindMapClick();
        console.log('🏭 Air Permitting layer shown — click any parcel to score');

        // Update count chip if present
        const countEl = document.getElementById('count-airpermitting');
        if (countEl) countEl.textContent = 'EPA';

        return true;
    },

    /**
     * Public: filter which sub-layers are visible
     */
    setSubLayer(key, visible) {
        const g = this.config.layerGroups[key];
        if (!g) return;
        if (visible) g.addTo(this.map); else this.map.removeLayer(g);
    }
};

// Export globally, matching DC Hub convention
window.DCHubAirPermitting = DCHubAirPermitting;

console.log(`
╔═══════════════════════════════════════════════════════════════╗
║  DC Hub Air Permitting Layer v1.0                              ║
╠═══════════════════════════════════════════════════════════════╣
║  ✅ EPA Green Book nonattainment overlay (O3 / PM2.5 / PM10)  ║
║  ✅ AQS monitor design values                                 ║
║  ✅ Federal Class I 300 km consultation buffer                ║
║  ✅ Parcel-level permitting score (click map)                 ║
║  📡 Backend: /api/infrastructure/air-permitting/*              ║
╚═══════════════════════════════════════════════════════════════╝
`);
