/* DC Hub Energy Patch v1.0 — fixes missing DCHubEnergy methods */
(function patchDCHubEnergy() {
    'use strict';

    function applyPatch() {
        if (typeof window.DCHubEnergy === 'undefined') {
            setTimeout(applyPatch, 500);
            return;
        }

        let patched = 0;

        // ── loadDOTPipelines ─────────────────────────────────────────────
        if (typeof DCHubEnergy.loadDOTPipelines !== 'function') {
            DCHubEnergy.loadDOTPipelines = async function (map, layerGroup) {
                try {
                    const resp = await fetch('/api/energy-discovery/pipelines?source=dot&limit=500');
                    if (!resp.ok) return [];
                    const data = await resp.json();
                    const pipelines = data.pipelines || data.features || [];
                    pipelines.forEach(p => {
                        const coords = p.coordinates || p.geometry?.coordinates || [];
                        if (!coords.length) return;
                        const latlngs = coords.map(c =>
                            Array.isArray(c[0]) ? c.map(pt => [pt[1], pt[0]]) : [c[1], c[0]]
                        );
                        const line = L.polyline(latlngs, { color: '#ff6600', weight: 2, opacity: 0.7 });
                        line.bindPopup(`<b>${p.name || 'DOT Pipeline'}</b><br>${p.operator || ''}`);
                        layerGroup ? layerGroup.addLayer(line) : line.addTo(map);
                    });
                    console.log(`[DCHubEnergy] loadDOTPipelines: ${pipelines.length} segments`);
                    return pipelines;
                } catch (e) {
                    console.warn('[DCHubEnergy] loadDOTPipelines failed:', e.message);
                    return [];
                }
            };
            patched++;
        }

        // ── loadTexasPipelines ───────────────────────────────────────────
        if (typeof DCHubEnergy.loadTexasPipelines !== 'function') {
            DCHubEnergy.loadTexasPipelines = async function (map, layerGroup) {
                try {
                    const resp = await fetch('/api/energy-discovery/pipelines?state=TX&limit=500');
                    if (!resp.ok) return [];
                    const data = await resp.json();
                    const pipelines = data.pipelines || data.features || [];
                    pipelines.forEach(p => {
                        const coords = p.coordinates || p.geometry?.coordinates || [];
                        if (!coords.length) return;
                        const latlngs = coords.map(c =>
                            Array.isArray(c[0]) ? c.map(pt => [pt[1], pt[0]]) : [c[1], c[0]]
                        );
                        const line = L.polyline(latlngs, { color: '#8B4513', weight: 2, opacity: 0.75, dashArray: '4 4' });
                        line.bindPopup(`<b>${p.name || 'TX Pipeline'}</b><br>${p.operator || 'Texas RRC'}`);
                        layerGroup ? layerGroup.addLayer(line) : line.addTo(map);
                    });
                    console.log(`[DCHubEnergy] loadTexasPipelines: ${pipelines.length} segments`);
                    return pipelines;
                } catch (e) {
                    console.warn('[DCHubEnergy] loadTexasPipelines failed:', e.message);
                    return [];
                }
            };
            patched++;
        }

        // ── enhancedSiteAnalysis ─────────────────────────────────────────
        if (typeof DCHubEnergy.enhancedSiteAnalysis !== 'function') {
            DCHubEnergy.enhancedSiteAnalysis = async function (lat, lng, options = {}) {
                try {
                    const params = new URLSearchParams({
                        lat: lat.toFixed(6), lng: lng.toFixed(6),
                        radius: options.radius || 25,
                        include_energy: true, include_grid: true
                    });
                    const resp = await fetch(`/api/v1/energy/site-analysis?${params}`);
                    if (!resp.ok) {
                        return { success: false, lat, lng, power_score: null, grid_score: null, source: 'fallback' };
                    }
                    const data = await resp.json();
                    return { success: true, ...data, source: 'api' };
                } catch (e) {
                    return { success: false, lat, lng, error: e.message, source: 'error' };
                }
            };
            patched++;
        }

        // ── Wrap window toggle fns ONLY if not already defined ──────
        // energy-enhancement-v3.js defines the full toggle logic (layer add/remove,
        // button state, toast). Only set these as fallback if that script didn't load.
        if (typeof window.toggleDOTPipelines !== 'function') {
            window.toggleDOTPipelines = async function (...args) {
                return DCHubEnergy.loadDOTPipelines?.(...args) ?? [];
            };
        }
        if (typeof window.toggleTexasPipelines !== 'function') {
            window.toggleTexasPipelines = async function (...args) {
                return DCHubEnergy.loadTexasPipelines?.(...args) ?? [];
            };
        }

        console.log(`✅ [Energy Patch] ${patched} methods patched on DCHubEnergy`);
    }

    document.readyState === 'loading'
        ? document.addEventListener('DOMContentLoaded', applyPatch)
        : applyPatch();
})();
