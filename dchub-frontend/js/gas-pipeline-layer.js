/**
 * DC Hub - Gas Pipeline Layer Integration v2.0
 * Loads gas pipeline infrastructure from Energy Auto-Discovery API
 * Primary: /api/energy-discovery/pipelines (no auth required)
 * Fallback: /api/v1/gas-pipelines (requires plan)
 */

(function() {
    'use strict';

    const CONFIG = {
        apiBase: window.location.origin,
        primaryEndpoint: '/api/energy-discovery/pipelines',
        fallbackEndpoint: '/api/v1/gas-pipelines',
        colors: {
            transmission: '#f97316',
            distribution: '#fbbf24',
            gathering: '#a855f7',
            default: '#f97316'
        },
        lineWidths: {
            transmission: 3,
            distribution: 2,
            gathering: 1.5,
            default: 2
        }
    };

    let pipelineLayer = null;
    let pipelinesData = [];
    let isLayerVisible = false;

    /**
     * Normalize pipeline data from different API formats
     */
    function normalizePipeline(raw) {
        return {
            id: raw.id || '',
            operator: raw.operator || 'Unknown',
            name: raw.name || raw.operator || 'Gas Pipeline',
            type: raw.pipeline_type || raw.type || 'Interstate Transmission',
            status: raw.status || 'Active',
            diameter: raw.diameter_inches || raw.diameter || null,
            commodity: raw.commodity || 'Natural Gas',
            capacity: raw.capacity_mdth ? (raw.capacity_mdth.toLocaleString() + ' MDth/d') : (raw.capacity || null),
            capacity_mdth: raw.capacity_mdth || 0,
            lat: raw.lat || null,
            lng: raw.lng || null,
            state: raw.state || '',
            states_served: raw.states_served || raw.states || '',
            market: raw.market || '',
            source: raw.source || 'DC Hub Auto-Discovery',
            // Line geometry if available
            coordinates: raw.coordinates || null,
            geometry: raw.geometry || null,
            start_lat: raw.start_lat || null,
            start_lon: raw.start_lon || null,
            end_lat: raw.end_lat || null,
            end_lon: raw.end_lon || null
        };
    }

    /**
     * Fetch pipelines — tries discovery endpoint first, falls back to original
     */
    async function fetchPipelines(params = {}) {
        // Try energy discovery endpoint first (no auth needed)
        try {
            const queryParams = new URLSearchParams();
            if (params.state) queryParams.append('state', params.state);
            if (params.operator) queryParams.append('operator', params.operator);
            if (params.limit) queryParams.append('limit', params.limit);

            const url = `${CONFIG.apiBase}${CONFIG.primaryEndpoint}${queryParams.toString() ? '?' + queryParams.toString() : ''}`;
            console.log('🔵 Fetching pipelines from discovery API:', url);
            const response = await fetch(url);

            if (response.ok) {
                const json = await response.json();
                if (json.success && json.data && json.data.length > 0) {
                    console.log('✅ Discovery API: Loaded ' + json.data.length + ' pipelines');
                    return { pipelines: json.data.map(normalizePipeline), source: 'discovery' };
                }
            }
        } catch (e) {
            console.warn('⚠️ Discovery API unavailable:', e.message);
        }

        // Fallback to original endpoint
        try {
            const queryParams = new URLSearchParams();
            if (params.state) queryParams.append('state', params.state);
            if (params.operator) queryParams.append('operator', params.operator);
            if (params.type) queryParams.append('type', params.type);
            if (params.limit) queryParams.append('limit', params.limit);

            const url = `${CONFIG.apiBase}${CONFIG.fallbackEndpoint}${queryParams.toString() ? '?' + queryParams.toString() : ''}`;
            console.log('🔵 Fallback: Fetching from', url);
            const response = await fetch(url);

            if (response.ok) {
                const json = await response.json();
                const pipelines = json.pipelines || json.data || (Array.isArray(json) ? json : []);
                if (pipelines.length > 0) {
                    console.log('✅ Fallback API: Loaded ' + pipelines.length + ' pipelines');
                    return { pipelines: pipelines.map(normalizePipeline), source: 'fallback' };
                }
            }
        } catch (e) {
            console.warn('⚠️ Fallback API also unavailable:', e.message);
        }

        console.error('❌ No pipeline data available from any source');
        return null;
    }

    function getPipelineColor(type) {
        if (!type) return CONFIG.colors.default;
        var t = type.toLowerCase();
        if (t.includes('transmission') || t.includes('interstate')) return CONFIG.colors.transmission;
        if (t.includes('distribution')) return CONFIG.colors.distribution;
        if (t.includes('gathering')) return CONFIG.colors.gathering;
        return CONFIG.colors.default;
    }

    function getPipelineWidth(type, diameter) {
        var diamNum = 0;
        if (diameter) {
            var match = String(diameter).match(/(\d+)/);
            if (match) diamNum = parseInt(match[1]);
        }
        var baseWidth = CONFIG.lineWidths.default;
        if (type) {
            var t = type.toLowerCase();
            if (t.includes('transmission') || t.includes('interstate')) baseWidth = CONFIG.lineWidths.transmission;
            if (t.includes('distribution')) baseWidth = CONFIG.lineWidths.distribution;
            if (t.includes('gathering')) baseWidth = CONFIG.lineWidths.gathering;
        }
        if (diamNum >= 30) return baseWidth + 2;
        if (diamNum >= 20) return baseWidth + 1;
        if (diamNum >= 10) return baseWidth + 0.5;
        return baseWidth;
    }

    function generatePipelinePopup(p) {
        var typeColor = getPipelineColor(p.type);
        return '<div style="min-width:260px;font-family:Inter,system-ui,sans-serif;">' +
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #333;">' +
                '<span style="font-size:20px;">🔥</span>' +
                '<div>' +
                    '<div style="font-size:14px;font-weight:700;color:#f97316;">' + (p.name || 'Gas Pipeline') + '</div>' +
                    '<div style="font-size:11px;color:#888;">' + (p.operator || 'Unknown Operator') + '</div>' +
                '</div>' +
            '</div>' +
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;">' +
                '<div style="background:#1a1a2e;padding:8px;border-radius:6px;">' +
                    '<div style="font-size:9px;color:#888;text-transform:uppercase;">Type</div>' +
                    '<div style="font-size:12px;font-weight:600;color:' + typeColor + ';">' + (p.type || 'N/A') + '</div>' +
                '</div>' +
                '<div style="background:#1a1a2e;padding:8px;border-radius:6px;">' +
                    '<div style="font-size:9px;color:#888;text-transform:uppercase;">Diameter</div>' +
                    '<div style="font-size:12px;font-weight:600;color:#fff;">' + (p.diameter ? p.diameter + '"' : 'N/A') + '</div>' +
                '</div>' +
                '<div style="background:#1a1a2e;padding:8px;border-radius:6px;">' +
                    '<div style="font-size:9px;color:#888;text-transform:uppercase;">States Served</div>' +
                    '<div style="font-size:11px;font-weight:600;color:#fff;">' + (p.states_served || p.state || 'N/A') + '</div>' +
                '</div>' +
                '<div style="background:#1a1a2e;padding:8px;border-radius:6px;">' +
                    '<div style="font-size:9px;color:#888;text-transform:uppercase;">Status</div>' +
                    '<div style="font-size:12px;font-weight:600;color:#10b981;">' + (p.status || 'Active') + '</div>' +
                '</div>' +
            '</div>' +
            (p.capacity ? '<div style="background:linear-gradient(135deg,rgba(249,115,22,0.15),rgba(251,191,36,0.15));padding:10px;border-radius:6px;border-left:3px solid #f97316;margin-bottom:10px;">' +
                '<div style="font-size:10px;color:#888;">Capacity</div>' +
                '<div style="font-size:16px;font-weight:700;color:#f97316;">' + p.capacity + '</div>' +
            '</div>' : '') +
            '<div style="margin-top:8px;font-size:9px;color:#666;text-align:center;">' +
                'Source: ' + (p.source || 'DC Hub Auto-Discovery') + ' • DC Hub Infrastructure' +
            '</div>' +
        '</div>';
    }

    /**
     * Add pipelines to map — handles both line geometry and point markers
     */
    function addPipelinesToMap(pipelines) {
        if (typeof map === 'undefined' || typeof L === 'undefined') {
            console.warn('Map not available');
            return 0;
        }

        if (pipelineLayer) {
            map.removeLayer(pipelineLayer);
        }

        var features = [];

        pipelines.forEach(function(p) {
            // Option 1: Full line geometry (GeoJSON coordinates)
            if (p.coordinates || (p.geometry && p.geometry.coordinates)) {
                var coords = p.coordinates || p.geometry.coordinates;
                if (Array.isArray(coords) && Array.isArray(coords[0])) {
                    var latLngs = coords.map(function(c) { return [c[1], c[0]]; });
                    var polyline = L.polyline(latLngs, {
                        color: getPipelineColor(p.type),
                        weight: getPipelineWidth(p.type, p.diameter),
                        opacity: 0.8,
                        dashArray: (p.type && p.type.toLowerCase().includes('gathering')) ? '5, 5' : null
                    });
                    polyline.bindPopup(generatePipelinePopup(p), { maxWidth: 320 });
                    features.push(polyline);
                }
            }
            // Option 2: Start/end point format
            else if (p.start_lat && p.start_lon && p.end_lat && p.end_lon) {
                var latLngs2 = [[p.start_lat, p.start_lon], [p.end_lat, p.end_lon]];
                var polyline2 = L.polyline(latLngs2, {
                    color: getPipelineColor(p.type),
                    weight: getPipelineWidth(p.type, p.diameter),
                    opacity: 0.8
                });
                polyline2.bindPopup(generatePipelinePopup(p), { maxWidth: 320 });
                features.push(polyline2);
            }
            // Option 3: Single point (discovery data) — show as circle marker
            else if (p.lat && p.lng) {
                var marker = L.circleMarker([p.lat, p.lng], {
                    radius: Math.min(8 + (p.capacity_mdth || 0) / 3000, 16),
                    fillColor: getPipelineColor(p.type),
                    color: '#fff',
                    weight: 1.5,
                    opacity: 0.9,
                    fillOpacity: 0.7
                });
                marker.bindPopup(generatePipelinePopup(p), { maxWidth: 320 });
                features.push(marker);
            }
        });

        if (features.length > 0) {
            pipelineLayer = L.featureGroup(features).addTo(map);
            isLayerVisible = true;
            showNotification('🔥 Loaded ' + features.length + ' gas pipelines');
        } else {
            console.warn('No pipeline features with displayable geometry');
            showNotification('⚠️ No pipeline geometry data available');
        }

        return features.length;
    }

    function togglePipelineLayer() {
        if (pipelineLayer) {
            if (isLayerVisible) {
                map.removeLayer(pipelineLayer);
                isLayerVisible = false;
            } else {
                pipelineLayer.addTo(map);
                isLayerVisible = true;
            }
        }
        return isLayerVisible;
    }

    async function loadStatePipelines(state) {
        var result = await fetchPipelines({ state: state, limit: 500 });
        if (result && result.pipelines) {
            pipelinesData = result.pipelines;
            return addPipelinesToMap(result.pipelines);
        }
        return 0;
    }

    async function loadAllPipelines() {
        var result = await fetchPipelines({ limit: 1000 });
        if (result && result.pipelines) {
            pipelinesData = result.pipelines;
            return addPipelinesToMap(result.pipelines);
        }
        return 0;
    }

    function showNotification(message) {
        var toast = document.createElement('div');
        toast.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);' +
            'background:#f97316;color:#fff;padding:12px 24px;border-radius:8px;font-size:13px;' +
            'font-weight:600;z-index:10000;box-shadow:0 4px 20px rgba(0,0,0,0.3);';
        toast.textContent = message;
        document.body.appendChild(toast);
        setTimeout(function() {
            toast.style.opacity = '0';
            toast.style.transition = 'opacity 0.3s';
            setTimeout(function() { toast.remove(); }, 300);
        }, 2500);
    }

    function addPipelineButton() {
        var layerToggles = document.querySelector('.layer-toggles');
        if (!layerToggles) return;
        if (document.getElementById('gas-pipeline-btn')) return;

        var btn = document.createElement('button');
        btn.id = 'gas-pipeline-btn';
        btn.className = 'layer-btn';
        btn.innerHTML = '🔥 Gas Pipelines';
        btn.title = 'Toggle gas pipeline infrastructure layer';

        btn.addEventListener('click', async function() {
            this.classList.toggle('active');
            if (this.classList.contains('active')) {
                this.innerHTML = '🔥 Loading...';
                this.disabled = true;
                if (pipelinesData.length === 0) {
                    await loadAllPipelines();
                } else {
                    togglePipelineLayer();
                }
                this.innerHTML = '🔥 Pipelines <span class="count">' + pipelinesData.length + '</span>';
                this.disabled = false;
            } else {
                togglePipelineLayer();
                this.innerHTML = '🔥 Gas Pipelines';
            }
        });

        layerToggles.appendChild(btn);
        console.log('✅ Gas Pipeline button added');
    }

    function addStateFilter() {
        var layerToggles = document.querySelector('.layer-toggles');
        if (!layerToggles) return;
        if (document.getElementById('pipeline-state-filter')) return;

        var select = document.createElement('select');
        select.id = 'pipeline-state-filter';
        select.style.cssText = 'padding:8px 12px;background:#181a25;border:1px solid #252836;border-radius:8px;color:#fff;font-size:12px;cursor:pointer;display:none;';
        select.innerHTML = '<option value="">All States</option>' +
            '<option value="AZ">Arizona</option><option value="TX">Texas</option>' +
            '<option value="VA">Virginia</option><option value="GA">Georgia</option>' +
            '<option value="NV">Nevada</option><option value="UT">Utah</option>' +
            '<option value="OH">Ohio</option><option value="IA">Iowa</option>' +
            '<option value="PA">Pennsylvania</option><option value="LA">Louisiana</option>';

        select.addEventListener('change', async function() {
            if (this.value) {
                await loadStatePipelines(this.value);
            } else {
                await loadAllPipelines();
            }
        });

        layerToggles.appendChild(select);
    }

    function init() {
        console.log('🔥 Gas Pipeline Layer v2.0 initializing...');
        var checkMap = setInterval(function() {
            if (typeof map !== 'undefined' && typeof L !== 'undefined') {
                clearInterval(checkMap);
                addPipelineButton();
                addStateFilter();
                console.log('✅ Gas Pipeline Layer v2.0 ready (discovery + fallback)');
            }
        }, 500);
    }

    window.GasPipelineLayer = {
        init: init,
        loadAllPipelines: loadAllPipelines,
        loadStatePipelines: loadStatePipelines,
        togglePipelineLayer: togglePipelineLayer,
        fetchPipelines: fetchPipelines,
        getData: function() { return pipelinesData; },
        isVisible: function() { return isLayerVisible; }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
