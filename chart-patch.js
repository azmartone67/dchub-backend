/*
 * DC Hub AI Chart Patch — All-Time View + Cumulative Data
 * 
 * HOW TO INSTALL:
 * Add this right before </body> in static/ai.html:
 *   <script src="/static/js/chart-patch.js"></script>
 *
 * Also change the chart heading in the HTML from:
 *   <h3>📊 Requests by Platform (7 Days)</h3>
 * To:
 *   <h3 id="chartTitle">📊 Requests by Platform (All Time)</h3>
 *   <div style="display:flex;gap:4px;margin:-0.5rem 0 0.75rem;">
 *     <button onclick="setChartRange('7d')" id="btn7d" style="padding:3px 10px;border:1px solid var(--border);border-radius:6px;background:transparent;color:var(--text-muted);font-size:0.65rem;font-family:var(--font-mono);cursor:pointer;">7 Days</button>
 *     <button onclick="setChartRange('30d')" id="btn30d" style="padding:3px 10px;border:1px solid var(--border);border-radius:6px;background:transparent;color:var(--text-muted);font-size:0.65rem;font-family:var(--font-mono);cursor:pointer;">30 Days</button>
 *     <button onclick="setChartRange('all')" id="btnAll" style="padding:3px 10px;border:1px solid var(--cyan);border-radius:6px;background:rgba(6,182,212,0.15);color:var(--cyan);font-size:0.65rem;font-family:var(--font-mono);cursor:pointer;">All Time</button>
 *   </div>
 */
(function() {
    'use strict';

    const API_URLS = [
        'https://api.dchub.cloud',
        'https://dchub.cloud',
        'https://dchub-backend-production.up.railway.app'
    ];

    let _chartRange = 'all';
    let _cumulativeData = {};
    const _$ = id => document.getElementById(id);

    // ── Fetch cumulative all-time data from Neon ──
    async function fetchCumulativeData() {
        for (const base of API_URLS) {
            try {
                const r = await fetch(base + '/api/v1/ai-tracking/cumulative', {
                    mode: 'cors',
                    headers: { 'Accept': 'application/json' },
                    signal: AbortSignal.timeout(8000)
                });
                if (!r.ok) continue;
                const data = await r.json();
                const platforms = data.platforms || (Array.isArray(data) ? data : []);
                platforms.forEach(pl => {
                    if (pl.platform) {
                        _cumulativeData[pl.platform.toLowerCase()] = {
                            total_requests: pl.total_requests || 0,
                            first_seen: pl.first_seen,
                            last_seen: pl.last_seen
                        };
                    }
                });
                // Update hero stat
                const allTimeEl = _$('statAllTime');
                if (data.all_time_total && allTimeEl) {
                    allTimeEl.textContent = data.all_time_total.toLocaleString();
                }
                console.log('[DC Hub Patch] Cumulative loaded:', Object.keys(_cumulativeData).length, 'platforms');
                // Now re-render chart with cumulative data
                reRenderChart();
                return;
            } catch(e) { continue; }
        }
    }

    // ── Chart range toggle ──
    window.setChartRange = function(range) {
        _chartRange = range;
        ['btn7d', 'btn30d', 'btnAll'].forEach(id => {
            const el = _$(id);
            if (!el) return;
            const isActive = (id === 'btn7d' && range === '7d') ||
                             (id === 'btn30d' && range === '30d') ||
                             (id === 'btnAll' && range === 'all');
            el.style.border = isActive ? '1px solid var(--cyan)' : '1px solid var(--border)';
            el.style.background = isActive ? 'rgba(6,182,212,0.15)' : 'transparent';
            el.style.color = isActive ? 'var(--cyan)' : 'var(--text-muted)';
        });
        const title = _$('chartTitle');
        if (title) {
            const labels = { '7d': '7 Days', '30d': '30 Days', 'all': 'All Time' };
            title.textContent = '\u{1f4ca} Requests by Platform (' + (labels[range] || 'All Time') + ')';
        }
        reRenderChart();
    };

    // ── Platform colors/names reference ──
    const PLAT_META = {
        claude:      { name: 'Claude',     color: '#d97706' },
        chatgpt:     { name: 'ChatGPT',    color: '#10b981' },
        gemini:      { name: 'Gemini',     color: '#4285f4' },
        perplexity:  { name: 'Perplexity', color: '#06b6d4' },
        grok:        { name: 'Grok',       color: '#ef4444' },
        copilot:     { name: 'Copilot',    color: '#8b5cf6' },
        deepseek:    { name: 'DeepSeek',   color: '#6366f1' },
        mistral:     { name: 'Mistral',    color: '#f43f5e' },
        groq:        { name: 'Groq',       color: '#f97316' },
        meta:        { name: 'Meta AI',    color: '#3b82f6' },
        poe:         { name: 'Poe',        color: '#a855f7' },
        cohere:      { name: 'Cohere',     color: '#14b8a6' },
        youcom:      { name: 'You.com',    color: '#eab308' },
        huggingface: { name: 'HuggingFace',color: '#fbbf24' },
        mcp:         { name: 'MCP',        color: '#8b5cf6' },
        glama:       { name: 'Glama',      color: '#22d3ee' },
    };

    // ── Re-render the chart with cumulative data ──
    function reRenderChart() {
        const wrapper = _$('platformChart');
        if (!wrapper) return;

        const range = _chartRange;
        const noise = new Set(['direct', 'unknown_ai', 'seo_bot', 'media_crawler', 'unknown', 'test', 'mcp-remote-fallback-test']);

        // Gather tracking data from the last fetch (stored in window by the main script)
        // We intercept by reading what's currently displayed or from the global
        let trackingPlatforms = {};
        let trackingChart = {};

        // Try to access the main script's currentData via the global scope
        // The main IIFE stores data in `currentData` which is scoped. 
        // We'll read from the DOM + cumulative instead.
        
        // Build items from cumulative data (all-time source of truth)
        let items = [];
        const allKeys = new Set([...Object.keys(_cumulativeData)]);

        // Also try to extract 7d data from the existing chart bars
        const existingBars = wrapper.querySelectorAll('.chart-col');
        const existing7d = {};
        existingBars.forEach(col => {
            const label = col.querySelector('.chart-label');
            const val = col.querySelector('.chart-bar-value');
            if (label && val) {
                const name = label.textContent.trim();
                const count = parseInt(val.textContent.replace(/[^0-9]/g, '')) || 0;
                // Find key by name
                const key = Object.entries(PLAT_META).find(([k, v]) => v.name === name)?.[0];
                if (key) existing7d[key] = count;
            }
        });

        allKeys.forEach(key => {
            if (noise.has(key)) return;
            const cu = _cumulativeData[key] || {};
            const totalReqs = cu.total_requests || 0;
            const reqs7d = existing7d[key] || 0;

            let count;
            if (range === '7d') {
                count = reqs7d;
            } else if (range === '30d') {
                count = totalReqs > 0 ? Math.min(totalReqs, Math.max(reqs7d * 4, totalReqs)) : reqs7d * 4;
            } else {
                count = totalReqs;
            }

            if (count > 0) {
                const meta = PLAT_META[key] || {};
                items.push({
                    key,
                    name: meta.name || key.charAt(0).toUpperCase() + key.slice(1),
                    color: meta.color || '#64748b',
                    count
                });
            }
        });

        // If no cumulative data yet but we're on 7d, keep existing chart
        if (items.length === 0 && range === '7d') return;

        items.sort((a, b) => b.count - a.count);
        const display = items.slice(0, 10);
        const maxCount = Math.max(...display.map(d => d.count), 1);

        if (display.length === 0) {
            wrapper.innerHTML = '<div style="padding:2rem;text-align:center;color:var(--text-muted);font-size:0.8rem;">No data for this period</div>';
            return;
        }

        const fmt = n => n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : n.toLocaleString();

        wrapper.innerHTML = `
            <div class="chart-bars">
                ${display.map(d => {
                    const pct = Math.max((d.count / maxCount) * 100, 3);
                    return `
                        <div class="chart-col">
                            <div class="chart-bar-wrap">
                                <div class="chart-bar" style="height:${pct}%; background:${d.color};">
                                    <span class="chart-bar-value">${fmt(d.count)}</span>
                                </div>
                                <div class="chart-bar-accent" style="background:${d.color};"></div>
                            </div>
                            <div class="chart-label">${d.name}</div>
                        </div>
                    `;
                }).join('')}
            </div>
        `;
    }

    // ── Init: fetch cumulative and override chart ──
    // Wait for the main script to finish its init, then overlay
    setTimeout(async () => {
        await fetchCumulativeData();
        // Refresh every 30s
        setInterval(fetchCumulativeData, 30000);
    }, 3000); // Wait 3s for main script init to complete

})();
