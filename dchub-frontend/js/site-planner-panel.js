/**
 * DC Hub Site Planner — Frontend Integration
 * ============================================
 * Adds Site Planner panel to land-power-map.html
 * Pro-only feature with upgrade prompt for free users.
 * 
 * Usage: Add to land-power-map.html before closing </body>:
 *   <script src="/js/site-planner-panel.js?v=1"></script>
 * 
 * Also add the "Site Planner" button to your layer controls:
 *   <button id="site-planner-btn" class="layer-btn" onclick="toggleSitePlanner()">⚡ Site Planner</button>
 */

(function() {
    'use strict';

    var API_BASE = '';  // same-origin through Cloudflare Worker
    var panel = null;
    var isOpen = false;
    var sites = [];
    var currentMarkers = [];
    var map = null;

    // ─── Inject CSS ──────────────────────────────────────────────────────────
    var style = document.createElement('style');
    style.textContent = `
        #sp-panel {
            position: fixed;
            top: 0;
            right: -460px;
            width: 450px;
            height: 100vh;
            background: #0a0f1e;
            border-left: 1px solid rgba(255,255,255,0.08);
            z-index: 2000;
            transition: right 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            flex-direction: column;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            color: #e2e8f0;
            box-shadow: -8px 0 32px rgba(0,0,0,0.5);
            overflow: hidden;
            pointer-events: auto;
        }
        #sp-panel.open { right: 0; }
        #sp-panel * { box-sizing: border-box; }
        @media (max-width: 768px) {
            #sp-panel { width: 100vw; right: -100vw; }
        }

        .sp-header {
            padding: 16px 20px;
            background: linear-gradient(135deg, rgba(34,211,238,0.06), transparent);
            border-bottom: 1px solid rgba(255,255,255,0.06);
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
        }
        .sp-header-title {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .sp-header-title .sp-icon {
            width: 32px; height: 32px; border-radius: 8px;
            background: linear-gradient(135deg, #22d3ee, #8b5cf6);
            display: flex; align-items: center; justify-content: center;
            font-size: 16px;
        }
        .sp-header-title h3 {
            margin: 0; font-size: 15px; font-weight: 700; letter-spacing: -0.3px;
        }
        .sp-header-title small {
            display: block; font-size: 10px; color: rgba(255,255,255,0.35);
            font-family: 'JetBrains Mono', monospace; letter-spacing: 0.8px;
        }
        .sp-close {
            background: none; border: none; color: rgba(255,255,255,0.3);
            font-size: 20px; cursor: pointer; padding: 4px 8px; line-height: 1;
        }
        .sp-close:hover { color: #fff; }

        .sp-search {
            padding: 16px 20px;
            border-bottom: 1px solid rgba(255,255,255,0.06);
            flex-shrink: 0;
        }
        .sp-search-row {
            display: flex; gap: 8px;
        }
        .sp-search input {
            flex: 1; padding: 10px 14px; font-size: 13px;
            background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
            border-radius: 8px; color: #e2e8f0; outline: none;
            font-family: 'JetBrains Mono', monospace;
        }
        .sp-search input:focus { border-color: rgba(34,211,238,0.4); }
        .sp-search input::placeholder { color: rgba(255,255,255,0.25); }
        .sp-autocomplete {
            position: absolute; top: 100%; left: 0; right: 0;
            background: #0f1629; border: 1px solid rgba(255,255,255,0.1);
            border-radius: 0 0 8px 8px; max-height: 200px; overflow-y: auto;
            z-index: 10; display: none;
        }
        .sp-autocomplete.show { display: block; }
        .sp-ac-item {
            padding: 8px 12px; font-size: 12px; color: rgba(255,255,255,0.6);
            cursor: pointer; border-bottom: 1px solid rgba(255,255,255,0.03);
        }
        .sp-ac-item:hover { background: rgba(34,211,238,0.08); color: #e2e8f0; }
        .sp-search-btn {
            padding: 10px 18px; font-size: 12px; font-weight: 700;
            background: linear-gradient(135deg, #22d3ee, #06b6d4);
            border: none; border-radius: 8px; color: #0a0f1e; cursor: pointer;
            white-space: nowrap; font-family: 'JetBrains Mono', monospace;
        }
        .sp-search-btn:disabled { opacity: 0.3; cursor: not-allowed; }
        .sp-search-btn.loading {
            background: rgba(34,211,238,0.15); color: #22d3ee;
        }
        .sp-sites-count {
            font-size: 10px; color: rgba(255,255,255,0.3); margin-top: 8px;
            font-family: 'JetBrains Mono', monospace;
        }
        .sp-click-hint {
            font-size: 10px; color: rgba(34,211,238,0.5); margin-top: 6px;
            font-style: italic;
        }

        .sp-body {
            flex: 1; overflow-y: auto; padding: 0;
        }
        .sp-body::-webkit-scrollbar { width: 5px; }
        .sp-body::-webkit-scrollbar-track { background: transparent; }
        .sp-body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 3px; }

        .sp-empty {
            text-align: center; padding: 60px 32px; color: rgba(255,255,255,0.15);
        }
        .sp-empty-icon { font-size: 48px; margin-bottom: 12px; opacity: 0.3; }
        .sp-empty h4 { margin: 0 0 8px; font-size: 16px; font-weight: 600; }
        .sp-empty p { font-size: 12px; line-height: 1.5; margin: 0; }

        .sp-site-card {
            border-bottom: 1px solid rgba(255,255,255,0.04);
            animation: spSlideIn 0.3s ease;
        }
        @keyframes spSlideIn { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }

        .sp-card-header {
            padding: 14px 20px;
            display: flex; justify-content: space-between; align-items: flex-start;
            background: linear-gradient(135deg, rgba(34,211,238,0.02), transparent);
            cursor: pointer;
        }
        .sp-card-header:hover { background: rgba(34,211,238,0.04); }
        .sp-card-num {
            width: 20px; height: 20px; border-radius: 5px;
            background: rgba(34,211,238,0.15); color: #22d3ee;
            font-size: 10px; font-weight: 700; display: flex;
            align-items: center; justify-content: center;
            font-family: 'JetBrains Mono', monospace;
            flex-shrink: 0;
        }
        .sp-card-info { flex: 1; margin: 0 10px; }
        .sp-card-addr { font-size: 12px; font-weight: 600; color: #e2e8f0; margin-bottom: 2px; }
        .sp-card-meta { font-size: 10px; color: rgba(255,255,255,0.3); font-family: 'JetBrains Mono', monospace; }
        .sp-card-remove {
            background: none; border: none; color: rgba(255,255,255,0.15);
            cursor: pointer; font-size: 16px; padding: 2px 4px; line-height: 1;
        }
        .sp-card-remove:hover { color: #ef4444; }

        .sp-score-ring {
            position: relative; width: 56px; height: 56px; flex-shrink: 0;
        }
        .sp-score-ring svg { transform: rotate(-90deg); }
        .sp-score-val {
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            font-size: 16px; font-weight: 700; font-family: 'JetBrains Mono', monospace;
        }
        .sp-score-label {
            font-size: 7px; color: rgba(255,255,255,0.35); text-transform: uppercase;
            letter-spacing: 1px; text-align: center; margin-top: 2px;
        }

        .sp-stats-grid {
            display: grid; grid-template-columns: repeat(3, 1fr);
            border-top: 1px solid rgba(255,255,255,0.04);
        }
        .sp-stat {
            padding: 10px 14px;
            border-right: 1px solid rgba(255,255,255,0.04);
        }
        .sp-stat:last-child { border-right: none; }
        .sp-stat-label {
            font-size: 8px; color: rgba(255,255,255,0.3); text-transform: uppercase;
            letter-spacing: 1px; margin-bottom: 3px; font-family: 'JetBrains Mono', monospace;
        }
        .sp-stat-val {
            font-size: 14px; font-weight: 700; color: #e2e8f0;
            font-family: 'JetBrains Mono', monospace; letter-spacing: -0.5px;
        }
        .sp-stat-sub {
            font-size: 9px; color: rgba(255,255,255,0.25); margin-top: 1px;
            font-family: 'JetBrains Mono', monospace;
        }

        .sp-detail-section {
            padding: 12px 20px;
            border-top: 1px solid rgba(255,255,255,0.04);
            display: none;
        }
        .sp-detail-section.open { display: block; }
        .sp-section-title {
            font-size: 9px; color: rgba(255,255,255,0.35); text-transform: uppercase;
            letter-spacing: 1.5px; margin-bottom: 8px; font-family: 'JetBrains Mono', monospace;
        }
        .sp-sub-table { width: 100%; border-collapse: collapse; font-size: 11px; }
        .sp-sub-table th {
            text-align: left; padding: 4px 6px; color: rgba(255,255,255,0.3);
            font-size: 8px; text-transform: uppercase; letter-spacing: 1px;
            border-bottom: 1px solid rgba(255,255,255,0.06);
            font-family: 'JetBrains Mono', monospace; font-weight: 600;
        }
        .sp-sub-table td {
            padding: 6px; color: rgba(255,255,255,0.6);
            font-family: 'JetBrains Mono', monospace; font-size: 11px;
            border-bottom: 1px solid rgba(255,255,255,0.02);
        }
        .sp-badge {
            display: inline-block; padding: 1px 6px; border-radius: 3px;
            font-size: 9px; font-weight: 600; font-family: 'JetBrains Mono', monospace;
        }
        .sp-badge-cyan { background: rgba(34,211,238,0.12); color: #22d3ee; }
        .sp-badge-yellow { background: rgba(245,158,11,0.12); color: #f59e0b; }
        .sp-badge-red { background: rgba(239,68,68,0.12); color: #ef4444; }
        .sp-badge-green { background: rgba(16,185,129,0.12); color: #10b981; }
        .sp-badge-purple { background: rgba(139,92,246,0.12); color: #a78bfa; }

        .sp-detail-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
        }
        .sp-detail-item {
            padding: 8px 10px; background: rgba(255,255,255,0.02);
            border-radius: 6px; border: 1px solid rgba(255,255,255,0.03);
        }
        .sp-detail-item-label {
            font-size: 8px; color: rgba(255,255,255,0.3); text-transform: uppercase;
            letter-spacing: 1px; margin-bottom: 3px; font-family: 'JetBrains Mono', monospace;
        }
        .sp-detail-item-val {
            font-size: 13px; font-weight: 600; color: #e2e8f0;
            font-family: 'JetBrains Mono', monospace;
        }

        .sp-tabs {
            display: flex; gap: 4px; padding: 10px 20px; flex-wrap: wrap;
            border-top: 1px solid rgba(255,255,255,0.04);
        }
        .sp-tab {
            padding: 4px 10px; font-size: 10px; border-radius: 5px;
            background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.05);
            color: rgba(255,255,255,0.4); cursor: pointer;
            font-family: 'JetBrains Mono', monospace; font-weight: 600;
        }
        .sp-tab.active {
            background: rgba(34,211,238,0.12); border-color: rgba(34,211,238,0.25);
            color: #22d3ee;
        }

        .sp-upgrade {
            text-align: center; padding: 40px 24px;
        }
        .sp-upgrade h4 { color: #f59e0b; margin: 0 0 8px; }
        .sp-upgrade p { font-size: 12px; color: rgba(255,255,255,0.4); margin: 0 0 16px; line-height: 1.5; }
        .sp-upgrade-btn {
            padding: 10px 24px; background: linear-gradient(135deg, #f59e0b, #d97706);
            border: none; border-radius: 8px; color: #0a0f1e; font-weight: 700;
            font-size: 13px; cursor: pointer;
        }
        .sp-actions {
            display: flex; gap: 8px; padding: 10px 20px;
            border-top: 1px solid rgba(255,255,255,0.06); flex-shrink: 0;
        }
        .sp-action-btn {
            flex: 1; padding: 8px 12px; font-size: 11px; font-weight: 700;
            border: 1px solid rgba(255,255,255,0.08); border-radius: 6px;
            background: rgba(255,255,255,0.03); color: rgba(255,255,255,0.5);
            cursor: pointer; font-family: 'JetBrains Mono', monospace;
            text-align: center;
        }
        .sp-action-btn:hover { background: rgba(34,211,238,0.08); color: #22d3ee; border-color: rgba(34,211,238,0.2); }
        .sp-action-btn.primary {
            background: linear-gradient(135deg, rgba(34,211,238,0.15), rgba(139,92,246,0.15));
            border-color: rgba(34,211,238,0.3); color: #22d3ee;
        }
    `;
    document.head.appendChild(style);

    // ─── Build Panel HTML ────────────────────────────────────────────────────
    function buildPanel() {
        panel = document.createElement('div');
        panel.id = 'sp-panel';
        panel.innerHTML = `
            <div class="sp-header">
                <div class="sp-header-title">
                    <div class="sp-icon">⚡</div>
                    <div>
                        <h3>Site Planner</h3>
                        <small>GRID INTERCONNECTION ANALYZER</small>
                    </div>
                </div>
                <button class="sp-close" onclick="toggleSitePlanner()">✕</button>
            </div>
            <div class="sp-search">
                <div class="sp-search-row" style="position:relative">
                    <input type="text" id="sp-input" placeholder="Enter address or click map..."
                           onkeydown="if(event.key==='Enter')spAnalyze()" oninput="spAutocomplete(this.value)">
                    <button class="sp-search-btn" id="sp-btn" onclick="spAnalyze()">⚡ Analyze</button>
                    <div class="sp-autocomplete" id="sp-ac"></div>
                </div>
                <div class="sp-sites-count" id="sp-count"></div>
                <div class="sp-click-hint">💡 Or click anywhere on the map to drop a pin</div>
            </div>
            <div class="sp-body" id="sp-body">
                <div class="sp-empty">
                    <div class="sp-empty-icon">⚡</div>
                    <h4>Grid Interconnection Analyzer</h4>
                    <p>Search an address or click the map to analyze grid interconnection viability. Up to 3 sites.</p>
                </div>
            </div>
            <div class="sp-actions" id="sp-actions" style="display:none">
                <button class="sp-action-btn" onclick="spExportPDF()">📄 Export PDF</button>
                <button class="sp-action-btn" onclick="spCompare()">⚖ Compare Sites</button>
                <button class="sp-action-btn" onclick="spClearAll()">🗑 Clear All</button>
            </div>
        `;
        document.body.appendChild(panel);

        // CRITICAL: Stop Leaflet from intercepting clicks/input on this panel
        if (window.L && L.DomEvent) {
            L.DomEvent.disableClickPropagation(panel);
            L.DomEvent.disableScrollPropagation(panel);
        }
        // Also stop native event bubbling to map
        panel.addEventListener('mousedown', function(e) { e.stopPropagation(); });
        panel.addEventListener('dblclick', function(e) { e.stopPropagation(); });
        panel.addEventListener('wheel', function(e) { e.stopPropagation(); });
        panel.addEventListener('touchstart', function(e) { e.stopPropagation(); });
    }

    // ─── Score Ring SVG ──────────────────────────────────────────────────────
    function scoreRingSVG(score) {
        var r = 22, circ = 2 * Math.PI * r;
        var offset = circ - (score / 100) * circ;
        var color = score >= 70 ? '#22d3ee' : score >= 45 ? '#f59e0b' : '#ef4444';
        return '<div class="sp-score-ring">' +
            '<svg width="56" height="56">' +
            '<circle cx="28" cy="28" r="' + r + '" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="4"/>' +
            '<circle cx="28" cy="28" r="' + r + '" fill="none" stroke="' + color + '" stroke-width="4" ' +
            'stroke-dasharray="' + circ + '" stroke-dashoffset="' + offset + '" stroke-linecap="round"/>' +
            '</svg>' +
            '<div class="sp-score-val" style="color:' + color + '">' + score + '</div>' +
            '</div>' +
            '<div class="sp-score-label">Score</div>';
    }

    // ─── Risk Badge ──────────────────────────────────────────────────────────
    function riskBadge(level) {
        var cls = level === 'Low' ? 'cyan' : level === 'Moderate' ? 'yellow' : level === 'High' ? 'red' : 'purple';
        return '<span class="sp-badge sp-badge-' + cls + '">' + (level || 'N/A') + '</span>';
    }

    // ─── Render Site Card ────────────────────────────────────────────────────
    function renderSite(site, idx) {
        var a = site.analysis || site;
        var loc = a.location || {};
        var subs = a.substations || [];
        var tx = a.transmission || {};
        var iso = a.iso || {};
        var q = a.queue || {};
        var cong = a.congestion || {};
        var env = a.environmental || {};
        var gen = a.generation_mix || {};
        var dcs = a.nearby_data_centers || {};
        var fiber = a.fiber_connectivity || {};
        var pricing = a.power_pricing || {};
        var water = a.water_risk || {};
        var gas = a.gas_infrastructure || {};
        var majorPipes = a.major_pipelines || {};
        var capacity = a.capacity_pipeline || {};
        var score = (a.suitability_score || {}).score || 0;
        var nearestSub = subs[0] || {};
        var nearestGas = (gas.nearest_pipeline || {});
        var cardId = 'sp-card-' + idx;

        var html = '<div class="sp-site-card" id="' + cardId + '">';
        
        // Header
        html += '<div class="sp-card-header" onclick="spToggleDetail(' + idx + ')">';
        html += '<div class="sp-card-num">' + (idx + 1) + '</div>';
        html += '<div class="sp-card-info">';
        html += '<div class="sp-card-addr">' + truncate(loc.address || 'Unknown', 45) + '</div>';
        html += '<div class="sp-card-meta">' + (loc.lat || 0).toFixed(4) + '°N • ' + iso.name + ' • ' + (site.meta || {}).elapsed_seconds + 's</div>';
        html += '</div>';
        html += scoreRingSVG(score);
        html += '<button class="sp-card-remove" onclick="event.stopPropagation();spRemoveSite(' + idx + ')" title="Remove">✕</button>';
        html += '</div>';

        // Quick stats
        html += '<div class="sp-stats-grid">';
        html += statCell('Nearest Sub', (nearestSub.distance_miles || 0).toFixed(1) + ' mi', (nearestSub.voltage_kv || 0) + ' kV');
        html += statCell('Queue', (q.queue_mw || 0).toLocaleString() + ' MW', '~' + (q.estimated_wait_years || 'N/A') + 'yr');
        html += statCell('Congestion', cong.level || 'N/A', (cong.substations_within_radius || 0) + ' subs nearby');
        html += '</div>';

        // Enhanced stats row
        html += '<div class="sp-stats-grid">';
        html += statCell('Gas Access', gas.gas_access || 'N/A', nearestGas.distance_miles ? nearestGas.distance_miles + ' mi' : '');
        html += statCell('Power Price', '$' + (pricing.avg_wholesale_price_mwh || 0) + '/MWh', pricing.price_trend || '');
        html += statCell('DC Pipeline', (capacity.total_pipeline_mw || 0).toLocaleString() + ' MW', capacity.demand_signal || '');
        html += '</div>';

        // Third stats row
        html += '<div class="sp-stats-grid">';
        html += statCell('Data Centers', (dcs.count || 0) + ' nearby', dcs.corridor_signal || '');
        html += statCell('Connectivity', fiber.connectivity_rating || 'N/A', (fiber.connected_facilities_nearby || 0) + ' facilities');
        html += statCell('Water Risk', water.water_stress_level || 'N/A', '');
        html += '</div>';

        // Tabs
        html += '<div class="sp-tabs">';
        ['subs', 'tx', 'env', 'gen', 'dcs', 'gas', 'capacity', 'pricing'].forEach(function(t) {
            var labels = { subs: '⚡ Subs', tx: '🔌 Tx', env: '🌿 Env', gen: '⚙ Gen', dcs: '🏢 DCs', gas: '🔥 Gas', capacity: '📊 Pipeline', pricing: '💰 Price' };
            html += '<div class="sp-tab" onclick="spShowSection(' + idx + ',\'' + t + '\')" data-sp-tab="' + idx + '-' + t + '">' + labels[t] + '</div>';
        });
        html += '</div>';

        // Substations section
        html += '<div class="sp-detail-section" data-sp-section="' + idx + '-subs">';
        html += '<table class="sp-sub-table"><thead><tr><th>Substation</th><th>kV</th><th>Dist</th><th>Operator</th></tr></thead><tbody>';
        subs.forEach(function(s) {
            html += '<tr><td style="color:#e2e8f0">' + truncate(s.name || '', 20) + '</td>';
            html += '<td><span class="sp-badge sp-badge-purple">' + (s.voltage_kv || 0) + '</span></td>';
            html += '<td style="color:' + ((s.distance_miles || 99) < 5 ? '#22d3ee' : 'rgba(255,255,255,0.5)') + '">' + (s.distance_miles || 0).toFixed(1) + '</td>';
            html += '<td>' + truncate(s.operator || '', 15) + '</td></tr>';
        });
        html += '</tbody></table></div>';

        // Transmission section
        html += '<div class="sp-detail-section" data-sp-section="' + idx + '-tx">';
        html += '<div class="sp-detail-grid">';
        html += detailItem('Line', tx.line_name || 'N/A');
        html += detailItem('Voltage', (tx.voltage_kv || 0) + ' kV');
        html += detailItem('Distance', (tx.distance_miles || 'N/A') + ' mi');
        html += detailItem('Owner', tx.owner || 'N/A');
        html += detailItem('Status', tx.status || 'N/A');
        html += detailItem('Class', tx.volt_class || 'N/A');
        html += '</div></div>';

        // Environmental section
        html += '<div class="sp-detail-section" data-sp-section="' + idx + '-env">';
        html += '<div class="sp-detail-grid">';
        html += '<div class="sp-detail-item"><div class="sp-detail-item-label">Flood Risk</div>' + riskBadge(env.flood_risk) + '</div>';
        html += '<div class="sp-detail-item"><div class="sp-detail-item-label">Wetlands</div>' + riskBadge(env.wetland_risk) + '</div>';
        html += '<div class="sp-detail-item"><div class="sp-detail-item-label">Species</div>' + riskBadge(env.species_risk) + '</div>';
        html += '<div class="sp-detail-item"><div class="sp-detail-item-label">Env Score</div><div class="sp-detail-item-val">' + (env.env_score || 'N/A') + '</div></div>';
        html += '</div>';
        if (env.risks_identified) {
            html += '<div style="margin-top:8px;font-size:10px;color:rgba(255,255,255,0.3);font-family:monospace">' + (env.risks_identified || []).join(' • ') + '</div>';
        }
        html += '</div>';

        // Generation mix section
        html += '<div class="sp-detail-section" data-sp-section="' + idx + '-gen">';
        html += '<div class="sp-section-title">Generation within 25 mi — ' + (gen.total_mw || 0).toLocaleString() + ' MW total</div>';
        var mix = gen.mix || {};
        Object.keys(mix).sort(function(a, b) { return (mix[b].mw || 0) - (mix[a].mw || 0); }).forEach(function(fuel) {
            var m = mix[fuel];
            var pct = m.percentage || 0;
            var barColor = { 'Natural Gas': '#f59e0b', 'Nuclear': '#8b5cf6', 'Wind': '#22d3ee', 'Solar': '#fbbf24', 'Coal': '#6b7280', 'Water': '#3b82f6' }[fuel] || '#22d3ee';
            html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">';
            html += '<span style="width:100px;text-align:right;font-size:10px;color:rgba(255,255,255,0.4);font-family:monospace">' + truncate(fuel, 14) + '</span>';
            html += '<div style="flex:1;height:6px;background:rgba(255,255,255,0.03);border-radius:3px;overflow:hidden">';
            html += '<div style="width:' + pct + '%;height:100%;background:' + barColor + ';border-radius:3px"></div></div>';
            html += '<span style="width:40px;font-size:10px;color:rgba(255,255,255,0.5);font-family:monospace">' + pct + '%</span>';
            html += '</div>';
        });
        html += '</div>';

        // Nearby DCs section
        html += '<div class="sp-detail-section" data-sp-section="' + idx + '-dcs">';
        html += '<div class="sp-section-title">Nearby Data Centers — ' + (dcs.count || 0) + ' facilities, ' + (dcs.corridor_signal || '') + ' corridor</div>';
        (dcs.facilities || []).slice(0, 8).forEach(function(f) {
            html += '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.02);font-size:11px">';
            html += '<span style="color:#e2e8f0">' + truncate(f.name || '', 28) + '</span>';
            html += '<span style="color:rgba(255,255,255,0.4);font-family:monospace">' + (f.distance_miles || 0).toFixed(1) + ' mi</span>';
            html += '</div>';
        });
        html += '</div>';

        // Gas Infrastructure section
        html += '<div class="sp-detail-section" data-sp-section="' + idx + '-gas">';
        html += '<div class="sp-section-title">Gas Infrastructure — ' + (gas.count || 0) + ' pipelines nearby, ' + (gas.gas_access || 'N/A') + ' access</div>';
        if (nearestGas.name) {
            html += '<div class="sp-detail-grid">';
            html += detailItem('Nearest', nearestGas.name || 'N/A');
            html += detailItem('Operator', nearestGas.operator || 'N/A');
            html += detailItem('Distance', (nearestGas.distance_miles || 'N/A') + ' mi');
            html += detailItem('Type', nearestGas.type || 'N/A');
            html += detailItem('Diameter', (nearestGas.diameter || 0) + '"');
            html += detailItem('Capacity', (nearestGas.capacity_mcf || 0).toLocaleString() + ' MCF');
            html += '</div>';
        }
        // Pipeline type breakdown
        var ptypes = gas.pipeline_types || {};
        if (Object.keys(ptypes).length > 0) {
            html += '<div style="margin-top:10px">';
            html += '<div class="sp-section-title">Pipeline Types</div>';
            Object.keys(ptypes).forEach(function(t) {
                html += '<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:11px">';
                html += '<span style="color:rgba(255,255,255,0.5)">' + t + '</span>';
                html += '<span class="sp-badge sp-badge-yellow">' + ptypes[t] + '</span>';
                html += '</div>';
            });
            html += '</div>';
        }
        // Major interstate pipelines
        var majors = (majorPipes.major_pipelines || []);
        if (majors.length > 0) {
            html += '<div style="margin-top:10px">';
            html += '<div class="sp-section-title">Major Interstate Pipelines</div>';
            majors.forEach(function(mp) {
                html += '<div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.03)">';
                html += '<div style="display:flex;justify-content:space-between;font-size:11px">';
                html += '<span style="color:#e2e8f0;font-weight:600">' + truncate(mp.name || '', 24) + '</span>';
                html += '<span style="color:rgba(255,255,255,0.4);font-family:monospace">' + (mp.distance_miles || 0) + ' mi</span>';
                html += '</div>';
                html += '<div style="font-size:9px;color:rgba(255,255,255,0.3);margin-top:2px;font-family:monospace">';
                html += (mp.operator || '') + ' • ' + (mp.capacity_mdth_per_day || 0).toLocaleString() + ' MDth/d • ' + (mp.states_served || '');
                html += '</div></div>';
            });
            html += '</div>';
        }
        html += '</div>';

        // Capacity Pipeline section (DC projects planned/under construction)
        html += '<div class="sp-detail-section" data-sp-section="' + idx + '-capacity">';
        html += '<div class="sp-section-title">DC Capacity Pipeline — ' + (capacity.total_pipeline_mw || 0).toLocaleString() + ' MW • ' + (capacity.project_count || 0) + ' projects • ' + (capacity.demand_signal || 'Unknown') + ' demand</div>';
        // Phase breakdown
        var phases = capacity.phase_breakdown || {};
        if (Object.keys(phases).length > 0) {
            html += '<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">';
            Object.keys(phases).forEach(function(ph) {
                var p = phases[ph];
                html += '<div style="padding:6px 10px;background:rgba(255,255,255,0.02);border-radius:6px;border:1px solid rgba(255,255,255,0.04)">';
                html += '<div style="font-size:8px;color:rgba(255,255,255,0.3);text-transform:uppercase;letter-spacing:1px">' + ph + '</div>';
                html += '<div style="font-size:13px;font-weight:700;color:#e2e8f0;font-family:monospace">' + (p.mw || 0).toLocaleString() + ' MW</div>';
                html += '<div style="font-size:9px;color:rgba(255,255,255,0.25)">' + (p.count || 0) + ' projects</div>';
                html += '</div>';
            });
            html += '</div>';
        }
        // Project list
        (capacity.projects || []).slice(0, 6).forEach(function(p) {
            html += '<div style="padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.02)">';
            html += '<div style="display:flex;justify-content:space-between;font-size:11px">';
            html += '<span style="color:#e2e8f0">' + truncate(p.operator || '', 22) + '</span>';
            html += '<span class="sp-badge sp-badge-cyan">' + (p.capacity_mw || 0).toLocaleString() + ' MW</span>';
            html += '</div>';
            html += '<div style="font-size:9px;color:rgba(255,255,255,0.25);margin-top:1px;font-family:monospace">';
            html += (p.market || '') + ' • ' + (p.phase || '') + ' • ' + (p.status || '');
            if (p.completion_date) html += ' • ETA: ' + p.completion_date;
            html += '</div></div>';
        });
        html += '</div>';

        // Pricing section
        html += '<div class="sp-detail-section" data-sp-section="' + idx + '-pricing">';
        html += '<div class="sp-detail-grid">';
        html += detailItem('Avg Price', '$' + (pricing.avg_wholesale_price_mwh || 0) + '/MWh');
        html += detailItem('Peak Price', '$' + (pricing.peak_price_mwh || 0) + '/MWh');
        html += detailItem('Trend', pricing.price_trend || 'N/A');
        html += detailItem('Renewable %', (pricing.renewable_percentage || 0) + '%');
        html += detailItem('Annual/MW', '$' + ((pricing.estimated_annual_cost_per_mw || 0) / 1000).toFixed(0) + 'K');
        html += detailItem('Connectivity', fiber.connectivity_rating || 'N/A');
        html += '</div>';
        if (water.recommendation) {
            html += '<div style="margin-top:8px;padding:8px;background:rgba(255,255,255,0.02);border-radius:6px;font-size:10px;color:rgba(255,255,255,0.35)">' + water.recommendation + '</div>';
        }
        html += '</div>';

        html += '</div>';
        return html;
    }

    function statCell(label, val, sub) {
        return '<div class="sp-stat"><div class="sp-stat-label">' + label + '</div><div class="sp-stat-val">' + val + '</div>' + (sub ? '<div class="sp-stat-sub">' + sub + '</div>' : '') + '</div>';
    }

    function detailItem(label, val) {
        return '<div class="sp-detail-item"><div class="sp-detail-item-label">' + label + '</div><div class="sp-detail-item-val">' + val + '</div></div>';
    }

    function truncate(str, len) { return str.length > len ? str.substring(0, len) + '...' : str; }

    // ─── API Call ────────────────────────────────────────────────────────────
    function callAPI(endpoint, body, callback) {
        var token = localStorage.getItem('dchub_token') || '';
        var apiKey = localStorage.getItem('dchub_api_key') || '';
        
        var headers = { 'Content-Type': 'application/json' };
        if (token) headers['Authorization'] = 'Bearer ' + token;
        if (apiKey) headers['X-API-Key'] = apiKey;

        fetch(API_BASE + endpoint, {
            method: 'POST',
            headers: headers,
            body: JSON.stringify(body),
        })
        .then(function(r) {
            if (r.status === 401 || r.status === 403) {
                // Check if user is actually logged in — if so, this is a backend auth bug, not a plan issue
                var hasToken = !!localStorage.getItem('dchub_token');
                if (hasToken) {
                    console.error('⚡ Site Planner: 401 but user HAS token — backend require_plan not recognizing JWT');
                    console.error('  Token present:', localStorage.getItem('dchub_token').substring(0, 20) + '...');
                    // Show a helpful error instead of upgrade prompt
                    var body = document.getElementById('sp-body');
                    if (body) {
                        body.innerHTML = '<div class="sp-upgrade">' +
                            '<h4 style="color:#ef4444">⚠️ Authentication Issue</h4>' +
                            '<p>You are logged in but the backend did not recognize your session.<br><br>' +
                            'Try: <strong>Sign out and sign back in</strong> to refresh your auth token.<br><br>' +
                            'If this persists, the backend require_plan decorator may need to be updated to recognize JWT tokens from dchub-api-base.js.</p>' +
                            '<button class="sp-upgrade-btn" onclick="location.reload()" style="background:linear-gradient(135deg,#6366f1,#4f46e5)">🔄 Refresh Page</button>' +
                            '</div>';
                    }
                } else {
                    showUpgradePrompt();
                }
                return null;
            }
            return r.json();
        })
        .then(function(data) {
            if (data) callback(null, data);
        })
        .catch(function(err) {
            callback(err, null);
        });
    }

    // ─── Analyze ─────────────────────────────────────────────────────────────
    window.spAnalyze = function(lat, lng) {
        if (sites.length >= 3) return;

        var input = document.getElementById('sp-input');
        var btn = document.getElementById('sp-btn');
        var body = {};

        if (lat && lng) {
            body = { lat: lat, lng: lng };
        } else if (input && input.value.trim()) {
            body = { address: input.value.trim() };
        } else {
            return;
        }

        btn.disabled = true;
        btn.className = 'sp-search-btn loading';
        btn.textContent = '◌ Analyzing...';

        callAPI('/api/v1/site-planner/analyze', body, function(err, data) {
            btn.disabled = false;
            btn.className = 'sp-search-btn';
            btn.textContent = '⚡ Analyze';

            if (err || !data || !data.success) {
                console.error('Site Planner error:', err || data);
                return;
            }

            sites.push(data);
            if (input) input.value = '';
            renderAllSites();
            addMarker(data);
            updateCount();
        });
    };

    window.spRemoveSite = function(idx) {
        sites.splice(idx, 1);
        removeMarker(idx);
        renderAllSites();
        updateCount();
    };

    window.spToggleDetail = function(idx) {
        // Toggle first section
        spShowSection(idx, 'subs');
    };

    window.spShowSection = function(idx, section) {
        // Hide all sections for this card
        document.querySelectorAll('[data-sp-section^="' + idx + '-"]').forEach(function(el) {
            el.classList.remove('open');
        });
        document.querySelectorAll('[data-sp-tab^="' + idx + '-"]').forEach(function(el) {
            el.classList.remove('active');
        });
        // Show selected
        var sec = document.querySelector('[data-sp-section="' + idx + '-' + section + '"]');
        var tab = document.querySelector('[data-sp-tab="' + idx + '-' + section + '"]');
        if (sec) sec.classList.toggle('open');
        if (tab) tab.classList.toggle('active');
    };

    function renderAllSites() {
        var body = document.getElementById('sp-body');
        if (!body) return;
        if (sites.length === 0) {
            body.innerHTML = '<div class="sp-empty"><div class="sp-empty-icon">⚡</div><h4>Grid Interconnection Analyzer</h4><p>Search an address or click the map to analyze grid interconnection viability. Up to 3 sites.</p></div>';
            return;
        }
        var html = '';
        sites.forEach(function(s, i) { html += renderSite(s, i); });
        body.innerHTML = html;
    }

    function updateCount() {
        var el = document.getElementById('sp-count');
        if (el) el.textContent = sites.length + '/3 sites analyzed' + (sites.length >= 3 ? ' (max reached)' : '');
        var actions = document.getElementById('sp-actions');
        if (actions) actions.style.display = sites.length > 0 ? 'flex' : 'none';
    }

    function showUpgradePrompt() {
        var body = document.getElementById('sp-body');
        if (body) {
            body.innerHTML = '<div class="sp-upgrade">' +
                '<h4>⚡ Pro Feature</h4>' +
                '<p>Site Planner requires a DC Hub Pro subscription. Get instant grid interconnection analysis, substation proximity, environmental screening, and suitability scoring.</p>' +
                '<a href="https://dchub.cloud/pricing" class="sp-upgrade-btn">Upgrade to Pro — $199/mo</a>' +
                '</div>';
        }
    }

    // ─── Map Integration ─────────────────────────────────────────────────────
    function getMap() {
        if (map) return map;
        // Try common map variable names
        if (window.map) { map = window.map; return map; }
        if (window.leafletMap) { map = window.leafletMap; return map; }
        // Try finding Leaflet map instance
        var containers = document.querySelectorAll('.leaflet-container');
        if (containers.length > 0) {
            // Access internal Leaflet map
            for (var key in containers[0]) {
                if (key.startsWith('_leaflet_id')) {
                    // Found it
                }
            }
        }
        return null;
    }

    function addMarker(data) {
        var m = getMap();
        if (!m || !window.L) return;
        var a = data.analysis || {};
        var loc = a.location || {};
        if (!loc.lat || !loc.lng) return;

        var score = (a.suitability_score || {}).score || 0;
        var color = score >= 70 ? '#22d3ee' : score >= 45 ? '#f59e0b' : '#ef4444';

        var icon = L.divIcon({
            className: 'sp-marker',
            html: '<div style="width:32px;height:32px;border-radius:50%;background:' + color + '20;border:2px solid ' + color + ';display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:' + color + ';font-family:monospace">' + score + '</div>',
            iconSize: [32, 32],
            iconAnchor: [16, 16],
        });

        var marker = L.marker([loc.lat, loc.lng], { icon: icon })
            .addTo(m)
            .bindPopup('<b>' + truncate(loc.address || '', 40) + '</b><br>Score: ' + score + '/100<br>ISO: ' + (a.iso || {}).name);

        currentMarkers.push(marker);
        m.setView([loc.lat, loc.lng], 12);
    }

    function removeMarker(idx) {
        if (currentMarkers[idx]) {
            var m = getMap();
            if (m) m.removeLayer(currentMarkers[idx]);
            currentMarkers.splice(idx, 1);
        }
    }

    // ─── Map Click Handler ───────────────────────────────────────────────────
    function enableMapClick() {
        var m = getMap();
        if (!m) {
            setTimeout(enableMapClick, 2000);
            return;
        }
        m.on('click', function(e) {
            if (!isOpen || sites.length >= 3) return;
            spAnalyze(e.latlng.lat, e.latlng.lng);
        });
    }

    // ─── Fix #4: Address Autocomplete ──────────────────────────────────────
    var acTimer = null;
    window.spAutocomplete = function(val) {
        clearTimeout(acTimer);
        var dropdown = document.getElementById('sp-ac');
        if (!dropdown) return;
        if (!val || val.length < 3) { dropdown.classList.remove('show'); return; }
        
        acTimer = setTimeout(function() {
            fetch('https://nominatim.openstreetmap.org/search?q=' + encodeURIComponent(val) + '&format=json&limit=5&countrycodes=us', {
                headers: {'User-Agent': 'DCHub-SitePlanner/1.0'}
            })
            .then(function(r) { return r.json(); })
            .then(function(results) {
                if (!results || results.length === 0) { dropdown.classList.remove('show'); return; }
                dropdown.innerHTML = '';
                results.forEach(function(r) {
                    var item = document.createElement('div');
                    item.className = 'sp-ac-item';
                    item.textContent = r.display_name.substring(0, 80);
                    item.onclick = function() {
                        document.getElementById('sp-input').value = r.display_name;
                        dropdown.classList.remove('show');
                        spAnalyze();
                    };
                    dropdown.appendChild(item);
                });
                dropdown.classList.add('show');
            })
            .catch(function() { dropdown.classList.remove('show'); });
        }, 400);
    };

    // Hide autocomplete when clicking outside
    document.addEventListener('click', function(e) {
        var dd = document.getElementById('sp-ac');
        if (dd && !e.target.closest('.sp-search-row')) dd.classList.remove('show');
    });

    // ─── Fix #7: PDF Export ─────────────────────────────────────────────────
    window.spExportPDF = function() {
        if (sites.length === 0) return;
        
        var site = sites[0];
        var a = site.analysis || {};
        var loc = a.location || {};
        var subs = a.substations || [];
        var iso = a.iso || {};
        var gas = a.gas_infrastructure || {};
        var dcs = a.nearby_data_centers || {};
        var pricing = a.power_pricing || {};
        var water = a.water_risk || {};
        var cap = a.capacity_pipeline || {};
        var score = (a.suitability_score || {}).score || 0;
        var breakdown = (a.suitability_score || {}).breakdown || {};
        
        // Build text report
        var lines = [];
        lines.push('DC HUB SITE PLANNER — INTERCONNECTION ANALYSIS REPORT');
        lines.push('═'.repeat(55));
        lines.push('Generated: ' + new Date().toISOString().split('T')[0]);
        lines.push('');
        lines.push('LOCATION: ' + (loc.address || 'Unknown'));
        lines.push('Coordinates: ' + (loc.lat || 0).toFixed(4) + '°N, ' + (loc.lng || 0).toFixed(4) + '°W');
        lines.push('ISO/RTO: ' + (iso.name || 'Unknown'));
        lines.push('');
        lines.push('SUITABILITY SCORE: ' + score + '/100');
        lines.push('─'.repeat(40));
        Object.keys(breakdown).forEach(function(k) {
            var b = breakdown[k];
            lines.push('  ' + k.replace(/_/g,' ').toUpperCase() + ': ' + b.points + ' pts (' + b.value + ')');
        });
        lines.push('');
        lines.push('NEAREST SUBSTATIONS');
        lines.push('─'.repeat(40));
        subs.forEach(function(s, i) {
            lines.push('  ' + (i+1) + '. ' + (s.name || 'Unknown') + ' — ' + (s.voltage_kv || 0) + ' kV, ' + (s.distance_miles || 0).toFixed(1) + ' mi, ' + (s.operator || ''));
        });
        lines.push('');
        lines.push('GAS INFRASTRUCTURE');
        lines.push('─'.repeat(40));
        lines.push('  Access Rating: ' + (gas.gas_access || 'N/A'));
        lines.push('  Nearest: ' + ((gas.nearest_pipeline || {}).name || 'N/A') + ' — ' + ((gas.nearest_pipeline || {}).distance_miles || 0) + ' mi');
        lines.push('  Pipelines within 25mi: ' + (gas.count || 0));
        lines.push('');
        lines.push('POWER PRICING (' + (iso.name || 'Unknown') + ')');
        lines.push('─'.repeat(40));
        lines.push('  Avg Wholesale: $' + (pricing.avg_wholesale_price_mwh || 0) + '/MWh');
        lines.push('  Peak: $' + (pricing.peak_price_mwh || 0) + '/MWh');
        lines.push('  Trend: ' + (pricing.price_trend || 'N/A'));
        lines.push('  Annual/MW: $' + ((pricing.estimated_annual_cost_per_mw || 0) / 1000).toFixed(0) + 'K');
        lines.push('');
        lines.push('DATA CENTER CORRIDOR');
        lines.push('─'.repeat(40));
        lines.push('  Nearby DCs: ' + (dcs.count || 0) + ' (' + (dcs.corridor_signal || '') + ')');
        lines.push('  DC Pipeline: ' + (cap.total_pipeline_mw || 0).toLocaleString() + ' MW (' + (cap.project_count || 0) + ' projects)');
        lines.push('');
        lines.push('WATER RISK');
        lines.push('─'.repeat(40));
        lines.push('  Stress Level: ' + (water.water_stress_level || 'Unknown'));
        lines.push('  ' + (water.recommendation || ''));
        lines.push('');
        lines.push('─'.repeat(55));
        lines.push('Report generated by DC Hub Site Planner (dchub.cloud)');
        lines.push('Data sources: HIFLD, FEMA, FWS, NWI, EIA, DC Hub DB');
        
        var text = lines.join('\n');
        
        // Download as text file (works without jsPDF dependency)
        var blob = new Blob([text], {type: 'text/plain'});
        var url = URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = url;
        link.download = 'DCHub-SitePlanner-' + (loc.state || 'US') + '-' + new Date().toISOString().split('T')[0] + '.txt';
        link.click();
        URL.revokeObjectURL(url);
    };

    // ─── Fix #8: Compare Mode ───────────────────────────────────────────────
    window.spCompare = function() {
        if (sites.length < 2) {
            alert('Add at least 2 sites to compare (click the map or search addresses)');
            return;
        }
        
        var body = document.getElementById('sp-body');
        if (!body) return;
        
        // Build comparison table
        var html = '<div style="padding:16px 20px">';
        html += '<div class="sp-section-title">SITE COMPARISON — ' + sites.length + ' SITES</div>';
        html += '<table class="sp-sub-table" style="width:100%"><thead><tr><th>#</th>';
        
        var metrics = [
            {key: 'score', label: 'Score', fn: function(s) { return ((s.analysis||{}).suitability_score||{}).score || 0; }},
            {key: 'sub', label: 'Sub Dist', fn: function(s) { var subs = (s.analysis||{}).substations||[]; return subs[0] ? subs[0].distance_miles.toFixed(1)+'mi' : 'N/A'; }},
            {key: 'kv', label: 'Voltage', fn: function(s) { var subs = (s.analysis||{}).substations||[]; return subs[0] ? subs[0].voltage_kv+'kV' : 'N/A'; }},
            {key: 'iso', label: 'ISO', fn: function(s) { return ((s.analysis||{}).iso||{}).name || 'N/A'; }},
            {key: 'gas', label: 'Gas', fn: function(s) { return ((s.analysis||{}).gas_infrastructure||{}).gas_access || 'N/A'; }},
            {key: 'dcs', label: 'DCs', fn: function(s) { return ((s.analysis||{}).nearby_data_centers||{}).count || 0; }},
            {key: 'price', label: '$/MWh', fn: function(s) { return '$'+((s.analysis||{}).power_pricing||{}).avg_wholesale_price_mwh; }},
            {key: 'water', label: 'Water', fn: function(s) { return ((s.analysis||{}).water_risk||{}).water_stress_level || 'N/A'; }},
        ];
        
        metrics.forEach(function(m) { html += '<th>' + m.label + '</th>'; });
        html += '</tr></thead><tbody>';
        
        // Find best score
        var bestScore = Math.max.apply(null, sites.map(function(s) { return ((s.analysis||{}).suitability_score||{}).score || 0; }));
        
        sites.forEach(function(s, i) {
            var isBest = (((s.analysis||{}).suitability_score||{}).score || 0) === bestScore;
            html += '<tr style="' + (isBest ? 'background:rgba(34,211,238,0.05)' : '') + '">';
            html += '<td>' + (i+1) + '</td>';
            metrics.forEach(function(m) {
                var val = m.fn(s);
                var style = '';
                if (m.key === 'score') {
                    var color = val >= 70 ? '#22d3ee' : val >= 45 ? '#f59e0b' : '#ef4444';
                    style = 'color:' + color + ';font-weight:700';
                }
                html += '<td style="' + style + '">' + val + '</td>';
            });
            html += '</tr>';
        });
        
        html += '</tbody></table>';
        
        // Recommendation
        var bestIdx = sites.findIndex(function(s) { return ((s.analysis||{}).suitability_score||{}).score === bestScore; });
        var bestLoc = ((sites[bestIdx]||{}).analysis||{}).location||{};
        html += '<div style="margin-top:16px;padding:12px;background:rgba(34,211,238,0.05);border:1px solid rgba(34,211,238,0.15);border-radius:8px">';
        html += '<div style="font-size:10px;color:#22d3ee;font-weight:700;margin-bottom:4px">⚡ RECOMMENDED SITE</div>';
        html += '<div style="font-size:13px;color:#e2e8f0">Site #' + (bestIdx+1) + ': ' + truncate(bestLoc.address || 'Unknown', 50) + ' (Score: ' + bestScore + ')</div>';
        html += '</div>';
        
        html += '<button class="sp-action-btn" style="margin-top:12px;width:100%" onclick="renderAllSites();document.getElementById(\'sp-actions\').style.display=\'flex\'">← Back to Details</button>';
        html += '</div>';
        
        body.innerHTML = html;
    };

    // ─── Clear All ──────────────────────────────────────────────────────────
    window.spClearAll = function() {
        sites = [];
        currentMarkers.forEach(function(m) { var mp = getMap(); if (mp) mp.removeLayer(m); });
        currentMarkers = [];
        renderAllSites();
        updateCount();
        document.getElementById('sp-actions').style.display = 'none';
    };

    // ─── Toggle Panel ────────────────────────────────────────────────────────
    window.toggleSitePlanner = function() {
        if (!panel) buildPanel();
        isOpen = !isOpen;
        panel.classList.toggle('open', isOpen);
        // Expose state so site-scoring-integration.js can suppress its click handler
        window._sitePlannerOpen = isOpen;

        var btn = document.getElementById('site-planner-btn');
        if (btn) btn.classList.toggle('active', isOpen);

        if (isOpen) enableMapClick();
    };

    // ─── Auto-inject button if not present ───────────────────────────────────
    document.addEventListener('DOMContentLoaded', function() {
        // Button is now in the sidebar tool grid — just pre-build the panel
        setTimeout(function() {
            // Wire up the sidebar button if it exists
            var btn = document.getElementById('site-planner-btn');
            if (btn && !btn._spWired) {
                btn._spWired = true;
                // Already wired via onclick="toggleSitePlanner()" in HTML
            }
        }, 500);

        // Pre-build panel
        buildPanel();
    });

    console.log('⚡ DC Hub Site Planner v2.0 loaded — 8 tabs, PDF export, compare mode');
})();
