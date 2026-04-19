/**
 * DC Hub Land & Power Button Fixes v1.0
 * ======================================
 * Wires up Score Location and Energy Discovery buttons
 * without modifying land-power-map.html structure.
 * 
 * Load AFTER land-power-app.js and energy-discovery-integration.js
 */
(function() {
    'use strict';

    function init() {
        if (typeof map === 'undefined' || typeof L === 'undefined') {
            setTimeout(init, 500);
            return;
        }

        // ═══ FIX 1: Score Location button ═══
        // The button fires map.fire('click') but doesn't switch to Score tab
        var scoreBtn = document.querySelector('.layer-btn[onclick*="map.fire"]');
        if (scoreBtn && scoreBtn.textContent.indexOf('Score Location') !== -1) {
            scoreBtn.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();
                // Switch to Score tab in right panel
                if (typeof switchRightTab === 'function') switchRightTab('score');
                // Fire scoring at map center
                if (typeof map !== 'undefined') {
                    var c = map.getCenter();
                    map.fire('click', { latlng: c });
                }
            };
            console.log('📊 Score Location button wired (+ Score tab switch)');
        }

        // ═══ FIX 2: Energy Discovery button ═══
        // Button has no onclick — wire it to the energy discovery module or inline panel
        var energyBtn = document.getElementById('grid-demand-btn');
        if (energyBtn && !energyBtn.onclick) {
            energyBtn.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();

                // Try external module first
                if (typeof window.DCHubEnergyDiscovery !== 'undefined') {
                    window.DCHubEnergyDiscovery.toggle();
                    return;
                }
                if (typeof window.toggleEnergyDiscovery === 'function') {
                    window.toggleEnergyDiscovery();
                    return;
                }

                // Fallback: build inline panel
                var existing = document.getElementById('energy-discovery-panel');
                if (existing) {
                    existing.style.display = existing.style.display === 'none' ? 'block' : 'none';
                    return;
                }

                var c = map.getCenter();
                var lat = c.lat.toFixed(4);
                var lng = c.lng.toFixed(4);

                var panel = document.createElement('div');
                panel.id = 'energy-discovery-panel';
                panel.style.cssText = 'position:fixed;top:60px;right:460px;width:380px;max-height:80vh;overflow-y:auto;background:#0a0f1e;border:1px solid rgba(255,255,255,0.08);border-radius:12px;z-index:2001;padding:0;box-shadow:-4px 0 24px rgba(0,0,0,0.5)';

                panel.innerHTML =
                    '<div style="padding:16px 20px;border-bottom:1px solid rgba(255,255,255,0.06);display:flex;justify-content:space-between;align-items:center">' +
                        '<div style="display:flex;align-items:center;gap:8px">' +
                            '<span style="font-size:18px">⚡</span>' +
                            '<div><div style="font-size:14px;font-weight:700;color:#e2e8f0">Energy Discovery</div>' +
                            '<div style="font-size:10px;color:rgba(255,255,255,0.35)">LIVE MARKET DATA</div></div>' +
                        '</div>' +
                        '<button onclick="document.getElementById(\'energy-discovery-panel\').style.display=\'none\'" style="background:none;border:none;color:rgba(255,255,255,0.3);font-size:20px;cursor:pointer">\u00d7</button>' +
                    '</div>' +
                    '<div id="energy-disc-content" style="padding:16px 20px">' +
                        '<div style="text-align:center;padding:24px;color:rgba(255,255,255,0.3)">' +
                            '<div style="font-size:32px;margin-bottom:8px">⚡</div>' +
                            '<div style="font-size:12px">Loading energy data for<br>' + lat + ', ' + lng + '...</div>' +
                        '</div>' +
                    '</div>';

                document.body.appendChild(panel);

                fetch('/api/v1/energy/site-analysis?lat=' + lat + '&lng=' + lng + '&state=US&radius=50')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        var el = document.getElementById('energy-disc-content');
                        if (!el) return;
                        if (data.success && data.data) {
                            var d = data.data;
                            el.innerHTML =
                                '<div style="font-size:11px;color:#94a3b8;margin-bottom:12px">' + lat + ', ' + lng + '</div>' +
                                '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">' +
                                    '<div style="background:rgba(59,130,246,0.08);padding:10px;border-radius:8px;border-left:3px solid #3b82f6">' +
                                        '<div style="font-size:10px;color:#94a3b8">Power Score</div>' +
                                        '<div style="font-size:20px;font-weight:700;color:#3b82f6">' + (d.scores ? d.scores.powerScore || '--' : '--') + '</div>' +
                                    '</div>' +
                                    '<div style="background:rgba(245,158,11,0.08);padding:10px;border-radius:8px;border-left:3px solid #f59e0b">' +
                                        '<div style="font-size:10px;color:#94a3b8">Gas Score</div>' +
                                        '<div style="font-size:20px;font-weight:700;color:#f59e0b">' + (d.scores ? d.scores.gasScore || '--' : '--') + '</div>' +
                                    '</div>' +
                                '</div>' +
                                '<div style="background:rgba(16,185,129,0.08);padding:12px;border-radius:8px;margin-bottom:12px;text-align:center;border-left:3px solid #10b981">' +
                                    '<div style="font-size:10px;color:#94a3b8">Overall Rating</div>' +
                                    '<div style="font-size:18px;font-weight:700;color:#10b981">' + (d.scores ? d.scores.rating || 'N/A' : 'N/A') + ' (' + (d.scores ? d.scores.overallScore || '--' : '--') + '/100)</div>' +
                                '</div>' +
                                '<div style="font-size:10px;color:#64748b;text-align:center;margin-top:8px">Source: DC Hub Energy API</div>';
                        } else {
                            el.innerHTML = '<div style="color:#f59e0b;padding:16px;text-align:center;font-size:12px">Energy data unavailable for this location.<br><span style="font-size:10px;color:#64748b">Try zooming into a US location.</span></div>';
                        }
                    })
                    .catch(function() {
                        var el = document.getElementById('energy-disc-content');
                        if (el) el.innerHTML = '<div style="color:#ef4444;padding:16px;text-align:center;font-size:12px">Failed to load energy data.<br><span style="font-size:10px;color:#64748b">Check network connection.</span></div>';
                    });
            };
            console.log('⚡ Energy Discovery button wired (module + fallback panel)');
        }

        console.log('🔧 Land & Power button fixes v1.0 applied');
    }

    // Wait for DOM + map
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() { setTimeout(init, 1000); });
    } else {
        setTimeout(init, 1000);
    }
})();
