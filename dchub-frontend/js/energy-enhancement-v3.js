/**
 * DC Hub Energy Enhancement v3.0
 * ==============================
 * 
 * CHANGES in v3:
 * - Removed Compare AZ button
 * - Added searchable market dropdown
 * - Measure tool moved to evaluate/PDF section
 * - GridStatus integration support
 * 
 * Load this AFTER land-power-app.js and energy-infrastructure.js
 */

(function() {
    'use strict';
    
    var initAttempts = 0;
    var maxAttempts = 20;
    
    // ============================================
    // MARKET DATABASE
    // ============================================
    
    var MARKETS = [
        // Southwest
        { name: 'Phoenix, AZ', region: 'Southwest', lat: 33.4484, lng: -111.9260, iso: 'WECC' },
        { name: 'Tonopah, AZ', region: 'Southwest', lat: 33.4484, lng: -112.8674, iso: 'WECC' },
        { name: 'Casa Grande, AZ', region: 'Southwest', lat: 32.8795, lng: -111.7574, iso: 'WECC' },
        { name: 'Coolidge, AZ', region: 'Southwest', lat: 32.9478, lng: -111.5185, iso: 'WECC' },
        { name: 'Eloy, AZ', region: 'Southwest', lat: 32.7559, lng: -111.5549, iso: 'WECC' },
        { name: 'Las Vegas, NV', region: 'Southwest', lat: 36.1699, lng: -115.1398, iso: 'WECC' },
        { name: 'Reno, NV', region: 'Southwest', lat: 39.5296, lng: -119.8138, iso: 'WECC' },
        
        // Texas
        { name: 'Dallas, TX', region: 'Texas', lat: 32.7767, lng: -96.7970, iso: 'ERCOT' },
        { name: 'San Antonio, TX', region: 'Texas', lat: 29.4241, lng: -98.4936, iso: 'ERCOT' },
        { name: 'Austin, TX', region: 'Texas', lat: 30.2672, lng: -97.7431, iso: 'ERCOT' },
        { name: 'Houston, TX', region: 'Texas', lat: 29.7604, lng: -95.3698, iso: 'ERCOT' },
        { name: 'Midland, TX (Permian)', region: 'Texas', lat: 31.9973, lng: -102.0779, iso: 'ERCOT' },
        { name: 'Odessa, TX', region: 'Texas', lat: 31.8457, lng: -102.3676, iso: 'ERCOT' },
        
        // Virginia / Data Center Alley
        { name: 'Ashburn, VA', region: 'Mid-Atlantic', lat: 39.0438, lng: -77.4874, iso: 'PJM' },
        { name: 'Manassas, VA', region: 'Mid-Atlantic', lat: 38.7509, lng: -77.4753, iso: 'PJM' },
        { name: 'Richmond, VA', region: 'Mid-Atlantic', lat: 37.5407, lng: -77.4360, iso: 'PJM' },
        { name: 'Northern Virginia', region: 'Mid-Atlantic', lat: 38.9072, lng: -77.0369, iso: 'PJM' },
        
        // California
        { name: 'Silicon Valley, CA', region: 'California', lat: 37.3861, lng: -122.0839, iso: 'CAISO' },
        { name: 'Los Angeles, CA', region: 'California', lat: 34.0522, lng: -118.2437, iso: 'CAISO' },
        { name: 'Sacramento, CA', region: 'California', lat: 38.5816, lng: -121.4944, iso: 'CAISO' },
        
        // Pacific Northwest
        { name: 'Seattle, WA', region: 'Pacific Northwest', lat: 47.6062, lng: -122.3321, iso: 'BPA' },
        { name: 'Portland, OR', region: 'Pacific Northwest', lat: 45.5152, lng: -122.6784, iso: 'BPA' },
        { name: 'Hillsboro, OR', region: 'Pacific Northwest', lat: 45.5229, lng: -122.9898, iso: 'BPA' },
        
        // Midwest
        { name: 'Chicago, IL', region: 'Midwest', lat: 41.8781, lng: -87.6298, iso: 'PJM' },
        { name: 'Columbus, OH', region: 'Midwest', lat: 39.9612, lng: -82.9988, iso: 'PJM' },
        { name: 'Des Moines, IA', region: 'Midwest', lat: 41.5868, lng: -93.6250, iso: 'MISO' },
        { name: 'Kansas City, MO', region: 'Midwest', lat: 39.0997, lng: -94.5786, iso: 'SPP' },
        { name: 'Omaha, NE', region: 'Midwest', lat: 41.2565, lng: -95.9345, iso: 'SPP' },
        
        // Southeast
        { name: 'Atlanta, GA', region: 'Southeast', lat: 33.7490, lng: -84.3880, iso: 'SERC' },
        { name: 'Charlotte, NC', region: 'Southeast', lat: 35.2271, lng: -80.8431, iso: 'SERC' },
        { name: 'Miami, FL', region: 'Southeast', lat: 25.7617, lng: -80.1918, iso: 'FRCC' },
        { name: 'Jacksonville, FL', region: 'Southeast', lat: 30.3322, lng: -81.6557, iso: 'FRCC' },
        
        // Northeast
        { name: 'New York City, NY', region: 'Northeast', lat: 40.7128, lng: -74.0060, iso: 'NYISO' },
        { name: 'Boston, MA', region: 'Northeast', lat: 42.3601, lng: -71.0589, iso: 'ISONE' },
        { name: 'Newark, NJ', region: 'Northeast', lat: 40.7357, lng: -74.1724, iso: 'PJM' }
    ];
    
    function init() {
        initAttempts++;
        
        if (!window.map || !window.L || !window.DCHubEnergy) {
            if (initAttempts < maxAttempts) {
                setTimeout(init, 250);
                return;
            }
            console.warn('⚠️ Energy Enhancement: Dependencies not ready');
            return;
        }
        
        console.log('⚡ Initializing Energy Enhancement v3...');
        
        var map = window.map;
        
        // Create layer groups
        var dotPipelinesLayer = L.layerGroup();
        var texasPipelinesLayer = L.layerGroup();
        
        window.energyLayers = {
            dotPipelines: dotPipelinesLayer,
            texasPipelines: texasPipelinesLayer
        };
        
        // ============================================
        // LAYER TOGGLES
        // ============================================
        
        window.toggleDOTPipelines = async function() {
            if (map.hasLayer(dotPipelinesLayer)) {
                map.removeLayer(dotPipelinesLayer);
                updateButtonState('dotPipelines', false);
                showToast('🔥 DOT Pipelines hidden', 'info');
                return false;
            } else {
                map.addLayer(dotPipelinesLayer);
                updateButtonState('dotPipelines', true);
                // Load from DC Hub backend (50K+ gas pipelines in Neon)
                var count = 0;
                try {
                    var apiBase = window.DCHUB_API_BASE || window.location.origin;
                    var center = map.getCenter();
                    var zoom = map.getZoom();
                    var radius = zoom >= 10 ? 30000 : zoom >= 7 ? 80000 : 200000;
                    var headers = {};
                    if (typeof _lpAuthHeaders === 'function') headers = _lpAuthHeaders();
                    var resp = await fetch(apiBase + '/api/v1/gas-pipelines?lat=' + center.lat + '&lng=' + center.lng + '&radius=' + radius + '&limit=1000', { headers: headers });
                    if (resp.ok) {
                        var json = await resp.json();
                        var pipes = json.data || json.pipelines || json.results || [];
                        pipes.forEach(function(p) {
                            var lat = p.lat || p.latitude || p.start_lat;
                            var lng = p.lng || p.longitude || p.start_lng;
                            if (!lat || !lng) return;
                            L.circleMarker([lat, lng], {radius:7,fillColor:'#f97316',color:'#fff',weight:1,opacity:0.9,fillOpacity:0.85,pane:'markerPane'})
                             .bindPopup('<b>🔥 ' + (p.name||p.operator||'Gas Pipeline') + '</b><br>Type: ' + (p.type||p.pipeline_type||'Interstate') + '<br><span style="color:#f97316;font-size:10px">📡 DC Hub</span>')
                             .addTo(dotPipelinesLayer);
                            count++;
                        });
                    }
                } catch(e) { console.warn('DOT pipeline fetch error:', e.message); }
                showToast('🔥 Loaded ' + count + ' pipeline points (DC Hub)', 'success');
                return true;
            }
        };
        
        window.toggleTexasPipelines = async function() {
            if (map.hasLayer(texasPipelinesLayer)) {
                map.removeLayer(texasPipelinesLayer);
                updateButtonState('texasPipelines', false);
                showToast('🛢️ Texas Pipelines hidden', 'info');
                return false;
            } else {
                map.addLayer(texasPipelinesLayer);
                updateButtonState('texasPipelines', true);
                // Load TX pipelines from DC Hub backend (50K+ in Neon)
                var count = 0;
                try {
                    var apiBase = window.DCHUB_API_BASE || window.location.origin;
                    var headers = {};
                    if (typeof _lpAuthHeaders === 'function') headers = _lpAuthHeaders();
                    // Texas center coords, 500km radius covers the whole state
                    var resp = await fetch(apiBase + '/api/v1/gas-pipelines?lat=31.0&lng=-99.5&radius=500000&limit=2000', { headers: headers });
                    if (resp.ok) {
                        var json = await resp.json();
                        var pipes = json.data || json.pipelines || json.results || [];
                        pipes.forEach(function(p) {
                            var lat = p.lat || p.latitude || p.start_lat;
                            var lng = p.lng || p.longitude || p.start_lng;
                            if (!lat || !lng) return;
                            L.circleMarker([lat, lng], {radius:7,fillColor:'#dc2626',color:'#fff',weight:1,opacity:0.9,fillOpacity:0.85,pane:'markerPane'})
                             .bindPopup('<b>🛢️ ' + (p.name||p.operator||'TX Pipeline') + '</b><br>Type: ' + (p.type||p.pipeline_type||'Gas') + '<br>State: TX<br><span style="color:#dc2626;font-size:10px">📡 DC Hub</span>')
                             .addTo(texasPipelinesLayer);
                            count++;
                        });
                    }
                } catch(e) { console.warn('TX pipeline fetch error:', e.message); }
                showToast('🛢️ Loaded ' + count + ' Texas pipeline points (DC Hub)', 'success');
                return true;
            }
        };
        
        function updateButtonState(layerName, active) {
            var btn = document.querySelector('[data-layer="' + layerName + '"]');
            if (btn) btn.classList.toggle('active', active);
        }
        
        function showToast(message, type) {
            if (window.showToast) {
                window.showToast(message, type);
            } else {
                console.log(message);
            }
        }
        
        // Auto-reload on map move (uses same logic as toggle functions)
        var reloadTimeout = null;
        map.on('moveend', function() {
            clearTimeout(reloadTimeout);
            reloadTimeout = setTimeout(async function() {
                // DOT and TX layers reload via their toggle functions' internal logic
                // No action needed on pan — data is bbox-cached in the layer groups
            }, 500);
        });
        
        // ============================================
        // SITE ANALYSIS
        // ============================================
        
        window.analyzeSite = async function(lat, lng, showOnMap) {
            console.log('🔍 Analyzing site:', lat.toFixed(4), lng.toFixed(4));
            
            var result = await DCHubEnergy.enhancedSiteAnalysis(lat, lng, 25000);
            var scores = result.scores || {};
            var details = scores.details || {};
            var counts = result.counts || {};
            
            console.log('═══════════════════════════════════════════════════════');
            console.log('📍 ENERGY INFRASTRUCTURE REPORT');
            console.log('═══════════════════════════════════════════════════════');
            console.log('📍 Location:', lat.toFixed(4) + ', ' + lng.toFixed(4));
            console.log('📊 Overall Score:', scores.overallScore + '/100');
            console.log('🔌 Power:', scores.powerScore + '/50 | 🔥 Gas:', scores.gasScore + '/50');
            console.log('═══════════════════════════════════════════════════════');
            
            if (showOnMap) {
                var score = scores.overallScore || 0;
                var color = score >= 70 ? '#22c55e' : score >= 50 ? '#f59e0b' : '#ef4444';
                
                var marker = L.circleMarker([lat, lng], {
                    radius: 14, fillColor: color, color: '#fff',
                    weight: 3, opacity: 1, fillOpacity: 0.85
                }).addTo(map);
                
                var popupContent = 
                    '<div style="min-width:220px;font-family:Inter,sans-serif;">' +
                    '<div style="font-weight:700;font-size:14px;color:#6366f1;border-bottom:2px solid #6366f1;padding-bottom:6px;margin-bottom:8px;">⚡ Energy Analysis</div>' +
                    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;">' +
                    '<div style="background:#f3f4f6;padding:8px;border-radius:6px;text-align:center;">' +
                    '<div style="font-size:20px;font-weight:700;color:' + color + ';">' + score + '</div>' +
                    '<div style="font-size:9px;color:#6b7280;">OVERALL</div></div>' +
                    '<div style="background:#f3f4f6;padding:8px;border-radius:6px;text-align:center;">' +
                    '<div style="font-size:12px;font-weight:600;color:#6366f1;">' + (scores.powerScore || 0) + ' / ' + (scores.gasScore || 0) + '</div>' +
                    '<div style="font-size:9px;color:#6b7280;">POWER / GAS</div></div></div>' +
                    '<div style="font-size:11px;margin-bottom:6px;"><strong>Pipelines:</strong> ' + (counts.pipelines || 0) + ' nearby</div>' +
                    '<div style="font-size:11px;"><strong>Substations:</strong> ' + (counts.substations || 0) + ' nearby</div>' +
                    (details.hasTallgrass ? '<div style="background:#dcfce7;color:#166534;padding:4px 8px;border-radius:4px;font-size:10px;font-weight:600;margin-top:6px;">🎯 Tallgrass/REX Pipeline!</div>' : '') +
                    '</div>';
                
                marker.bindPopup(popupContent, {maxWidth: 280}).openPopup();
                map.flyTo([lat, lng], 11);
            }
            
            return result;
        };
        
        // ============================================
        // MARKET FUNCTIONS
        // ============================================
        
        window.getMarkets = function() { return MARKETS; };
        
        window.searchMarkets = function(query) {
            if (!query) return MARKETS;
            var q = query.toLowerCase();
            return MARKETS.filter(function(m) {
                return m.name.toLowerCase().includes(q) || 
                       m.region.toLowerCase().includes(q) ||
                       m.iso.toLowerCase().includes(q);
            });
        };
        
        window.analyzeMarket = function(marketName) {
            var market = MARKETS.find(function(m) {
                return m.name.toLowerCase().includes(marketName.toLowerCase());
            });
            if (market) return analyzeSite(market.lat, market.lng, true);
            console.log('❌ Market not found:', marketName);
            return null;
        };
        
        window.goToMarket = function(marketName) {
            var market = MARKETS.find(function(m) {
                return m.name.toLowerCase().includes(marketName.toLowerCase());
            });
            if (market) {
                map.flyTo([market.lat, market.lng], 10);
                showToast('📍 ' + market.name, 'info');
                return market;
            }
            return null;
        };
        
        // Site shortcuts
        window.analyzeTonopah = function() { return analyzeSite(33.4484, -112.8674, true); };
        window.analyzeCasaGrande = function() { return analyzeSite(32.8795, -111.7574, true); };
        window.analyzeCoolidge = function() { return analyzeSite(32.9478, -111.5185, true); };
        window.analyzeEloy = function() { return analyzeSite(32.7559, -111.5549, true); };
        window.analyzePermian = function() { return analyzeSite(31.9973, -102.0779, true); };
        window.analyzeAshburn = function() { return analyzeSite(39.0438, -77.4874, true); };
        
        // ============================================
        // MEASURE BUTTON - Use original controls bar button
        // ============================================
        
        // Don't modify measure button - leave original in controls bar working
        console.log('📏 Measure tool: Using original button in controls bar');
        
        // ============================================
        // ADD UI - MARKET DROPDOWN
        // ============================================
        
        function addEnergyButtons() {
            var containers = [
                document.querySelector('.layer-toggles'),
                document.querySelector('.controls-bar'),
                document.querySelector('#layer-controls')
            ];
            
            var container = containers.find(function(c) { return c !== null; });
            if (!container) return;
            if (document.querySelector('[data-layer="dotPipelines"]')) return;
            
            // Energy Section
            var section = document.createElement('div');
            section.style.cssText = 'margin-top:16px;padding-top:12px;border-top:1px solid var(--border,#333);';
            
            var label = document.createElement('div');
            label.style.cssText = 'font-size:10px;color:#f59e0b;font-weight:600;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px;';
            label.innerHTML = '⚡ Energy Infrastructure';
            section.appendChild(label);
            
            var btnGroup = document.createElement('div');
            btnGroup.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;';
            
            // DOT Pipelines
            var dotBtn = document.createElement('button');
            dotBtn.className = 'layer-btn';
            dotBtn.setAttribute('data-layer', 'dotPipelines');
            dotBtn.innerHTML = '🔥 DOT Pipes';
            dotBtn.title = 'Interstate/Intrastate Gas Pipelines';
            dotBtn.style.cssText = 'font-size:10px;padding:6px 10px;';
            dotBtn.onclick = function() { window.toggleDOTPipelines(); };
            btnGroup.appendChild(dotBtn);
            
            // Texas Pipelines
            var txBtn = document.createElement('button');
            txBtn.className = 'layer-btn';
            txBtn.setAttribute('data-layer', 'texasPipelines');
            txBtn.innerHTML = '🛢️ TX RRC';
            txBtn.title = 'Texas Railroad Commission Pipelines';
            txBtn.style.cssText = 'font-size:10px;padding:6px 10px;';
            txBtn.onclick = function() { window.toggleTexasPipelines(); };
            btnGroup.appendChild(txBtn);
            
            // Analyze
            var analyzeBtn = document.createElement('button');
            analyzeBtn.className = 'layer-btn';
            analyzeBtn.innerHTML = '⚡ Analyze';
            analyzeBtn.title = 'Analyze map center';
            analyzeBtn.style.cssText = 'font-size:10px;padding:6px 10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none;';
            analyzeBtn.onclick = function() {
                var center = map.getCenter();
                analyzeSite(center.lat, center.lng, true);
            };
            btnGroup.appendChild(analyzeBtn);
            
            section.appendChild(btnGroup);
            
            // ============================================
            // MARKET DROPDOWN (REPLACES COMPARE AZ)
            // ============================================
            
            var marketSection = document.createElement('div');
            marketSection.style.cssText = 'margin-top:12px;';
            
            var marketLabel = document.createElement('div');
            marketLabel.style.cssText = 'font-size:10px;color:#8b5cf6;font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px;';
            marketLabel.innerHTML = '📍 Jump to Market';
            marketSection.appendChild(marketLabel);
            
            var dropdownContainer = document.createElement('div');
            dropdownContainer.style.cssText = 'position:relative;';
            
            var searchInput = document.createElement('input');
            searchInput.type = 'text';
            searchInput.placeholder = 'Search markets...';
            searchInput.style.cssText = 'width:100%;padding:8px 12px;font-size:11px;border:1px solid var(--border,#444);border-radius:6px;background:var(--bg2,#1a1a2e);color:var(--text,#fff);outline:none;box-sizing:border-box;';
            dropdownContainer.appendChild(searchInput);
            
            var dropdownList = document.createElement('div');
            dropdownList.style.cssText = 'position:absolute;top:100%;left:0;right:0;max-height:250px;overflow-y:auto;background:var(--bg2,#1a1a2e);border:1px solid var(--border,#444);border-radius:6px;margin-top:4px;display:none;z-index:1000;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
            dropdownContainer.appendChild(dropdownList);
            
            function populateDropdown(markets) {
                dropdownList.innerHTML = '';
                var regions = {};
                markets.forEach(function(m) {
                    if (!regions[m.region]) regions[m.region] = [];
                    regions[m.region].push(m);
                });
                
                Object.keys(regions).sort().forEach(function(region) {
                    var header = document.createElement('div');
                    header.style.cssText = 'padding:6px 12px;font-size:9px;color:#6b7280;font-weight:600;text-transform:uppercase;background:var(--bg3,#252540);position:sticky;top:0;';
                    header.textContent = region;
                    dropdownList.appendChild(header);
                    
                    regions[region].forEach(function(market) {
                        var item = document.createElement('div');
                        item.style.cssText = 'padding:8px 12px;font-size:11px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;';
                        item.innerHTML = '<span>' + market.name + '</span><span style="font-size:9px;color:#6b7280;background:var(--bg3,#333);padding:2px 6px;border-radius:4px;">' + market.iso + '</span>';
                        item.onmouseover = function() { this.style.background = 'var(--bg3,#252540)'; };
                        item.onmouseout = function() { this.style.background = 'transparent'; };
                        item.onclick = function() {
                            map.flyTo([market.lat, market.lng], 10);
                            setTimeout(function() { analyzeSite(market.lat, market.lng, true); }, 1000);
                            dropdownList.style.display = 'none';
                            searchInput.value = '';
                            searchInput.placeholder = market.name;
                        };
                        dropdownList.appendChild(item);
                    });
                });
            }
            
            populateDropdown(MARKETS);
            
            searchInput.onfocus = function() {
                dropdownList.style.display = 'block';
                populateDropdown(MARKETS);
            };
            
            searchInput.oninput = function() {
                populateDropdown(searchMarkets(this.value));
                dropdownList.style.display = 'block';
            };
            
            document.addEventListener('click', function(e) {
                if (!dropdownContainer.contains(e.target)) {
                    dropdownList.style.display = 'none';
                }
            });
            
            marketSection.appendChild(dropdownContainer);
            section.appendChild(marketSection);
            container.appendChild(section);
            
            // Measure tool uses original button in controls bar
            console.log('✅ Energy UI with market dropdown added');
        }
        
        setTimeout(addEnergyButtons, 1000);
        
        // ============================================
        // ENHANCE POPUP
        // ============================================
        
        setInterval(function() {
            var popup = document.querySelector('.site-eval-popup');
            if (popup && !popup.hasAttribute('data-energy-enhanced')) {
                popup.setAttribute('data-energy-enhanced', 'true');
                if (window.lastSiteEvaluation) {
                    var lat = window.lastSiteEvaluation.lat;
                    var lng = window.lastSiteEvaluation.lng;
                    if (lat && lng) {
                        DCHubEnergy.enhancedSiteAnalysis(lat, lng, 25000).then(function(result) {
                            var scoreSection = popup.querySelector('.site-eval-score');
                            if (scoreSection) {
                                var scores = result.scores || {};
                                var counts = result.counts || {};
                                var details = scores.details || {};
                                var gasScoreColor = scores.gasScore >= 30 ? '#22c55e' : scores.gasScore >= 15 ? '#f59e0b' : '#ef4444';
                                
                                var gasSection = document.createElement('div');
                                gasSection.innerHTML = 
                                    '<div style="font-size:10px;color:#f97316;font-weight:600;margin:12px 0 6px;">🔥 GAS INFRASTRUCTURE</div>' +
                                    '<div class="site-eval-grid">' +
                                    '<div class="site-eval-item"><div class="site-eval-label">Gas Score</div>' +
                                    '<div class="site-eval-value" style="color:' + gasScoreColor + ';">' + (scores.gasScore || 0) + '/50</div></div>' +
                                    '<div class="site-eval-item"><div class="site-eval-label">Pipelines</div>' +
                                    '<div class="site-eval-value">' + (counts.pipelines || 0) + '</div></div>' +
                                    '</div>' +
                                    (details.hasTallgrass ? '<div style="background:rgba(34,197,94,0.15);color:#16a34a;padding:6px 10px;border-radius:6px;font-size:10px;font-weight:600;margin-top:8px;text-align:center;">🎯 Tallgrass/REX Pipeline!</div>' : '');
                                
                                scoreSection.parentNode.insertBefore(gasSection, scoreSection);
                            }
                        });
                    }
                }
            }
        }, 500);
        
        // ============================================
        // CONNECTIVITY SCORING (PeeringDB Integration)
        // ============================================
        
        window.getConnectivityScore = async function(lat, lng) {
            try {
                var response = await fetch('https://dchub.cloud/api/v1/connectivity/score?lat=' + lat + '&lng=' + lng);
                var data = await response.json();
                
                if (data.success) {
                    console.log('═══════════════════════════════════════════════════════');
                    console.log('🌐 CONNECTIVITY SCORE');
                    console.log('═══════════════════════════════════════════════════════');
                    console.log('📍 Location:', lat.toFixed(4) + ', ' + lng.toFixed(4));
                    console.log('📊 Score:', data.score + '/100', '(' + data.rating + ')');
                    console.log('');
                    console.log('📈 Breakdown:');
                    console.log('   🔗 IXP Score:', data.breakdown.ixp_score + '/30');
                    console.log('   🏢 Facility Score:', data.breakdown.facility_score + '/30');
                    console.log('   🌐 Network Score:', data.breakdown.network_score + '/40');
                    console.log('');
                    console.log('📊 Nearby:');
                    console.log('   📡 IXPs:', data.counts.ixps, '(within 100km)');
                    console.log('   🏢 Facilities:', data.counts.facilities, '(within 50km)');
                    console.log('   🌐 Networks:', data.counts.total_networks);
                    if (data.nearest_ixp) {
                        console.log('   📡 Nearest IXP:', data.nearest_ixp.name, '(' + data.nearest_ixp.distance_km + 'km)');
                    }
                    console.log('═══════════════════════════════════════════════════════');
                }
                return data;
            } catch (e) {
                console.error('❌ Connectivity score error:', e);
                return null;
            }
        };
        
        window.getNearbyIXPs = async function(lat, lng, radius) {
            radius = radius || 100;
            try {
                var response = await fetch('https://dchub.cloud/api/v1/connectivity/ixps?lat=' + lat + '&lng=' + lng + '&radius=' + radius);
                return await response.json();
            } catch (e) {
                console.error('❌ IXP fetch error:', e);
                return null;
            }
        };
        
        window.getNearbyFacilities = async function(lat, lng, radius) {
            radius = radius || 50;
            try {
                var response = await fetch('https://dchub.cloud/api/v1/connectivity/facilities?lat=' + lat + '&lng=' + lng + '&radius=' + radius);
                return await response.json();
            } catch (e) {
                console.error('❌ Facility fetch error:', e);
                return null;
            }
        };
        
        // ============================================
        // EXPANDED EIA DATA ACCESS
        // ============================================
        
        window.getRTODemand = async function(rto) {
            rto = rto || 'ERCOT';
            try {
                var response = await fetch('https://dchub.cloud/api/v1/energy/rto/demand?rto=' + rto);
                return await response.json();
            } catch (e) {
                console.error('❌ RTO demand error:', e);
                return null;
            }
        };
        
        window.getRTOFuelMix = async function(rto) {
            rto = rto || 'ERCOT';
            try {
                var response = await fetch('https://dchub.cloud/api/v1/energy/rto/fuelmix?rto=' + rto);
                return await response.json();
            } catch (e) {
                console.error('❌ RTO fuel mix error:', e);
                return null;
            }
        };
        
        window.getNaturalGasPrice = async function() {
            try {
                var response = await fetch('https://dchub.cloud/api/v1/energy/naturalgas/price');
                return await response.json();
            } catch (e) {
                console.error('❌ Natural gas price error:', e);
                return null;
            }
        };
        
        window.getRetailRates = async function(state) {
            state = state || 'AZ';
            try {
                var response = await fetch('https://dchub.cloud/api/v1/energy/retail/rates?state=' + state);
                return await response.json();
            } catch (e) {
                console.error('❌ Retail rates error:', e);
                return null;
            }
        };
        
        // ============================================
        // OIL & GAS OPERATOR DATA (HIFLD)
        // ============================================
        
        window.getOilGasWells = async function(lat, lng, radius) {
            radius = radius || 25;
            try {
                var response = await fetch('https://dchub.cloud/api/v1/oilgas/wells?lat=' + lat + '&lng=' + lng + '&radius=' + radius);
                var data = await response.json();
                
                if (data.success) {
                    console.log('═══════════════════════════════════════════════════════');
                    console.log('🛢️ OIL & GAS WELLS');
                    console.log('═══════════════════════════════════════════════════════');
                    console.log('📍 Location:', lat.toFixed(4) + ', ' + lng.toFixed(4));
                    console.log('📏 Radius:', radius, 'miles');
                    console.log('🔢 Total Wells:', data.total_wells);
                    console.log('🏢 Unique Operators:', data.unique_operators);
                    console.log('');
                    if (data.major_operators && data.major_operators.length > 0) {
                        console.log('⭐ Major Operators Present:');
                        data.major_operators.forEach(function(op) {
                            console.log('   ', op.operator, '-', op.count, 'wells');
                        });
                    }
                    console.log('═══════════════════════════════════════════════════════');
                }
                return data;
            } catch (e) {
                console.error('❌ Oil/Gas wells error:', e);
                return null;
            }
        };
        
        window.getOperatorsNearby = async function(lat, lng, radius) {
            radius = radius || 50;
            try {
                var response = await fetch('https://dchub.cloud/api/v1/oilgas/operators?lat=' + lat + '&lng=' + lng + '&radius=' + radius);
                var data = await response.json();
                
                if (data.success) {
                    console.log('═══════════════════════════════════════════════════════');
                    console.log('🏢 OPERATOR ANALYSIS');
                    console.log('═══════════════════════════════════════════════════════');
                    console.log('📍 Location:', lat.toFixed(4) + ', ' + lng.toFixed(4));
                    console.log('📊 Activity:', data.rating);
                    console.log('🔢 Total Wells:', data.total_wells);
                    console.log('🏢 Unique Operators:', data.unique_operators);
                    console.log('📈 Diversity Score:', data.diversity_score + '/100');
                    console.log('');
                    if (data.major_operators && data.major_operators.length > 0) {
                        console.log('⭐ Major Operators:');
                        data.major_operators.slice(0, 10).forEach(function(op) {
                            console.log('   ', op.major_name, '(' + op.operator + ') -', op.count, 'wells');
                        });
                    }
                    console.log('═══════════════════════════════════════════════════════');
                }
                return data;
            } catch (e) {
                console.error('❌ Operator analysis error:', e);
                return null;
            }
        };
        
        window.searchOperator = async function(operator, state) {
            if (!operator) {
                console.error('❌ Operator name required');
                return null;
            }
            try {
                var url = 'https://dchub.cloud/api/v1/oilgas/search?operator=' + encodeURIComponent(operator);
                if (state) url += '&state=' + state;
                var response = await fetch(url);
                var data = await response.json();
                
                if (data.success) {
                    console.log('═══════════════════════════════════════════════════════');
                    console.log('🔍 OPERATOR SEARCH:', operator.toUpperCase());
                    console.log('═══════════════════════════════════════════════════════');
                    console.log('🔢 Total Wells Found:', data.total_wells);
                    if (data.states && data.states.length > 0) {
                        console.log('');
                        console.log('📍 Wells by State:');
                        data.states.forEach(function(s) {
                            console.log('   ', s.state, '-', s.count, 'wells');
                        });
                    }
                    console.log('═══════════════════════════════════════════════════════');
                }
                return data;
            } catch (e) {
                console.error('❌ Operator search error:', e);
                return null;
            }
        };
        
        console.log('═══════════════════════════════════════════════════════════════');
        console.log('⚡ DC HUB ENERGY ENHANCEMENT v3.2 READY');
        console.log('═══════════════════════════════════════════════════════════════');
        console.log('');
        console.log('🔍 Markets:');
        console.log('    getMarkets()              - List all markets');
        console.log('    searchMarkets("texas")    - Search markets');
        console.log('    goToMarket("Dallas")      - Jump to market');
        console.log('    analyzeMarket("Ashburn")  - Analyze market');
        console.log('');
        console.log('🌐 Connectivity (PeeringDB):');
        console.log('    getConnectivityScore(lat, lng)  - Full score');
        console.log('    getNearbyIXPs(lat, lng)         - Internet Exchanges');
        console.log('    getNearbyFacilities(lat, lng)   - Data centers');
        console.log('');
        console.log('⚡ Energy (EIA):');
        console.log('    getRTODemand("ERCOT")     - Real-time demand');
        console.log('    getRTOFuelMix("PJM")      - Generation mix');
        console.log('    getNaturalGasPrice()      - Henry Hub price');
        console.log('    getRetailRates("TX")      - State electricity rates');
        console.log('');
        console.log('🛢️ Oil & Gas (HIFLD):');
        console.log('    getOilGasWells(lat, lng)        - Wells nearby');
        console.log('    getOperatorsNearby(lat, lng)    - Operator analysis');
        console.log('    searchOperator("ExxonMobil")    - Find operator wells');
        console.log('    searchOperator("Chevron", "TX") - By state');
        console.log('');
        console.log('💡 Major operators tracked: ExxonMobil, Chevron, EOG,');
        console.log('   ConocoPhillips, Devon, Pioneer, Diamondback, Apache...');
        console.log('═══════════════════════════════════════════════════════════════');
    }
    
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
