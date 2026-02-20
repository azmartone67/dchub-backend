// ========================================
// DC HUB v49 - LIVE API + M&A DEALS + MARKET GROWTH
// ========================================

const API_BASE = '';  // Use relative URLs for both dev and production

// State
let facilities = [];
let filteredFacilities = [];
let news = [];
let map = null;
let markers = null;
let currentRegion = 'all';
let facilitiesLoaded = 0;

// ========================================
// API FUNCTIONS
// ========================================

async function fetchAPI(endpoint) {
    try {
        const response = await fetch(API_BASE + endpoint);
        if (!response.ok) throw new Error('API error');
        return await response.json();
    } catch (error) {
        console.error('API Error:', error);
        return null;
    }
}

// ========================================
// INITIALIZATION
// ========================================

async function init() {
    console.log('🚀 DC Hub v49 initializing...');
    
    initMap();
    
    await Promise.all([
        loadStats(),
        loadFacilities(),
        loadNews(),
        loadTransactions(),
        loadMarketGrowth()
    ]);
    
    renderMarkets();
    
    setupSearch();
    setupFilters();
    
    loadAITracking();
    
    console.log('✅ DC Hub v49 ready');
}

// ========================================
// STATS
// ========================================

async function loadStats() {
    const result = await fetchAPI('/api/v1/stats');
    
    if (result && result.data) {
        const stats = result.data;
        
        document.getElementById('stat-facilities').textContent = (stats.total_facilities || 0).toLocaleString();
        document.getElementById('hero-facilities').textContent = (stats.total_facilities || 0).toLocaleString();
        const pipelineMW = stats.pipeline_mw || 0;
        const pipelineGW = stats.pipeline_gw || (pipelineMW / 1000);
        document.getElementById('stat-power').textContent = pipelineGW >= 1 
            ? pipelineGW.toFixed(1) + ' GW' 
            : pipelineMW.toLocaleString() + ' MW';
        document.getElementById('stat-news').textContent = (stats.total_announcements || 0).toLocaleString();
        document.getElementById('stat-sources').textContent = Object.keys(stats.by_source || {}).length;
        
        document.getElementById('api-status').textContent = 'LIVE DATA';
        console.log('📊 Stats loaded:', stats.total_facilities, 'facilities');
    } else {
        document.getElementById('api-status').textContent = 'OFFLINE';
    }
}

// ========================================
// AI PLATFORM TRACKING
// ========================================

async function loadAITracking() {
    try {
        const result = await fetchAPI('/ai/tracking');
        if (result) {
            const activePlatforms = result.platforms_active || 3;
            const totalRequests = result.total_requests_all_time || 0;
            
            const statEl = document.getElementById('stat-ai-platforms');
            if (statEl) statEl.textContent = activePlatforms;
            
            const reqEl = document.getElementById('ai-total-requests');
            if (reqEl) reqEl.textContent = totalRequests;
            
            console.log('🤖 AI tracking loaded:', activePlatforms, 'platforms,', totalRequests, 'requests');
        }
    } catch (err) {
        const statEl = document.getElementById('stat-ai-platforms');
        if (statEl) statEl.textContent = '3';
        console.log('AI tracking not available');
    }
}

// ========================================
// FACILITIES
// ========================================

async function loadFacilities() {
    // Use lightweight map endpoint to load all facilities (increased limit)
    const result = await fetchAPI('/api/v1/map?limit=5000');
    
    if (result && result.data) {
        facilities = result.data;
        filteredFacilities = [...facilities];
        
        console.log('🏢 Loaded', facilities.length, 'facilities for map');
        
        // Update map with real data
        updateMapMarkers();
        
        // Render facilities grid
        renderFacilities();
    } else {
        console.warn('⚠️ Using fallback facilities data');
        document.getElementById('facilities-grid').innerHTML = '<div class="loading">Unable to load facilities. Check API connection.</div>';
    }
}

function renderFacilities() {
    const container = document.getElementById('facilities-grid');
    const toShow = filteredFacilities.slice(0, facilitiesLoaded + 50);
    facilitiesLoaded = toShow.length;
    
    if (toShow.length === 0) {
        container.innerHTML = '<div class="loading">No facilities found.</div>';
        return;
    }
    
    container.innerHTML = toShow.map(f => `
        <div class="facility-card" data-id="${f.id}">
            <div class="facility-header">
                <div>
                    <div class="facility-name">${escapeHtml(f.name || 'Unknown')}</div>
                    <div class="facility-provider">${escapeHtml(f.provider || 'Unknown Provider')}</div>
                </div>
                <span class="facility-status ${f.status || 'active'}">${(f.status || 'active').toUpperCase()}</span>
            </div>
            <div class="facility-meta">
                <span>📍 ${escapeHtml(f.city || '')} ${escapeHtml(f.country || '')}</span>
                ${f.power_mw ? `<span>⚡ ${f.power_mw} MW</span>` : ''}
            </div>
        </div>
    `).join('');
}

// ========================================
// NEWS
// ========================================

async function loadNews() {
    // Try to load from API first
    const result = await fetchAPI('/api/news?limit=20');
    
    if (result && result.articles && result.articles.length > 0) {
        news = result.articles;
        renderNews();
    } else {
        // Fallback to hardcoded recent news
        news = getDefaultNews();
        renderNews();
    }
}

function getDefaultNews() {
    return [
        {title: "Meta Plans $65B Data Center Investment in 2025", source: "Data Center Frontier", summary: "Meta has announced plans to spend up to $65 billion on capital expenditures in 2025, with a significant portion dedicated to AI data center infrastructure.", categories: ["investment", "hyperscale"], companies: ["Meta"], locations: ["Multiple US Markets"], published_at: "2025-01-24"},
        {title: "Microsoft Commits $80B for AI Data Centers", source: "Data Center Knowledge", summary: "Microsoft has committed approximately $80 billion for fiscal year 2025 data center investments, focusing on AI infrastructure across North America and Europe.", categories: ["investment", "hyperscale", "ai"], companies: ["Microsoft"], locations: ["Global"], published_at: "2025-01-20"},
        {title: "OpenAI Stargate: $500B Data Center Initiative", source: "Reuters", summary: "OpenAI, Oracle, and Softbank announce Stargate, a $500 billion initiative to build 20 large AI data centers across Texas, Louisiana, and Indiana.", categories: ["ai", "construction"], companies: ["OpenAI", "Oracle"], locations: ["Texas", "Louisiana", "Indiana"], published_at: "2025-01-15"},
        {title: "Atlanta Leads US in Data Center Absorption", source: "CBRE", summary: "Atlanta led all primary US markets for net absorption in 2024 with 705.8 MW, surpassing Northern Virginia's 451.7 MW for the first time.", categories: ["market"], companies: [], locations: ["Atlanta"], published_at: "2025-01-10"},
        {title: "Google Partners with Kairos Power for Nuclear", source: "Data Center Dynamics", summary: "Google has partnered with Kairos Power to deploy small modular nuclear reactors for powering AI data centers, targeting first reactor by 2030.", categories: ["power", "hyperscale"], companies: ["Google"], locations: [], published_at: "2025-01-05"},
        {title: "QTS Announces $1.3B Atlanta Expansion", source: "Data Center Frontier", summary: "QTS Data Centers announces a $1.3 billion new data center in the Atlanta metropolitan area, adding significant capacity to the growing market.", categories: ["expansion", "construction"], companies: ["QTS"], locations: ["Atlanta"], published_at: "2024-12-20"},
        {title: "Equinix Expands Singapore Campus", source: "Capacity Media", summary: "Equinix announces SG5 Singapore expansion, a 65 MW interconnection hub serving the Asia-Pacific region with $250M investment.", categories: ["expansion"], companies: ["Equinix"], locations: ["Singapore"], published_at: "2024-12-15"},
        {title: "AWS Expands Phoenix Presence", source: "Data Center Knowledge", summary: "Amazon Web Services continues Arizona expansion with new facilities in the Phoenix West corridor to support AI/ML workloads.", categories: ["expansion", "hyperscale"], companies: ["AWS", "Amazon"], locations: ["Phoenix"], published_at: "2024-12-10"}
    ];
}

const NEWS_ICONS = {
    'Data Center Frontier': '🏗️', 'Data Center Knowledge': '📡', 'Data Center Dynamics': '⚡',
    'Reuters': '🌐', 'CBRE': '📊', 'TechCrunch': '💻', 'Capacity Media': '🔌',
    'SiliconANGLE': '💎', 'Ars Technica': '🖥️', 'The Register': '📰'
};

function getNewsIcon(source, title) {
    if (NEWS_ICONS[source]) return NEWS_ICONS[source];
    const t = (title || '').toLowerCase();
    if (t.includes('invest') || t.includes('billion') || t.includes('$')) return '💰';
    if (t.includes('ai') || t.includes('artificial')) return '🤖';
    if (t.includes('power') || t.includes('energy') || t.includes('nuclear')) return '⚡';
    if (t.includes('expan') || t.includes('build') || t.includes('construct')) return '🏗️';
    if (t.includes('acqui') || t.includes('merger') || t.includes('deal')) return '🤝';
    return '📰';
}

function renderNews() {
    const container = document.getElementById('news-grid');
    
    if (news.length === 0) {
        container.innerHTML = '<div class="loading">No news available.</div>';
        return;
    }
    
    const featured = news[0];
    const rest = news.slice(1, 12);
    
    container.innerHTML = `
        <div class="news-card news-featured" style="grid-column: 1 / -1; display:grid; grid-template-columns: 1fr 1fr; gap:24px;">
            <div class="news-featured-img" style="background:linear-gradient(135deg, var(--bg3), var(--bg4)); border-radius:12px; display:flex; align-items:center; justify-content:center; min-height:200px; font-size:4rem;">
                ${getNewsIcon(featured.source, featured.title)}
            </div>
            <div style="display:flex;flex-direction:column;justify-content:center;">
                <span class="news-source">${escapeHtml(featured.source || 'Industry News')}</span>
                <h3 class="news-title" style="-webkit-line-clamp:3;font-size:1.3rem;">
                    <a href="${featured.url || '#'}" target="_blank" rel="noopener">${escapeHtml(featured.title)}</a>
                </h3>
                <div class="news-meta">
                    ${(featured.companies || []).map(c => `<span class="news-tag company">${escapeHtml(c)}</span>`).join('')}
                    ${(featured.locations || []).map(l => `<span class="news-tag location">${escapeHtml(l)}</span>`).join('')}
                    ${(featured.categories || []).slice(0, 2).map(c => `<span class="news-tag">${escapeHtml(c)}</span>`).join('')}
                </div>
                <p class="news-summary" style="-webkit-line-clamp:4;">${escapeHtml(featured.summary || '')}</p>
                <div class="news-date">${formatDate(featured.published_at)}</div>
            </div>
        </div>
        ${rest.map(article => `
            <div class="news-card">
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
                    <span style="font-size:1.5rem;">${getNewsIcon(article.source, article.title)}</span>
                    <span class="news-source" style="margin-bottom:0;">${escapeHtml(article.source || 'Industry News')}</span>
                </div>
                <h3 class="news-title">
                    <a href="${article.url || '#'}" target="_blank" rel="noopener">${escapeHtml(article.title)}</a>
                </h3>
                <div class="news-meta">
                    ${(article.companies || []).map(c => `<span class="news-tag company">${escapeHtml(c)}</span>`).join('')}
                    ${(article.locations || []).map(l => `<span class="news-tag location">${escapeHtml(l)}</span>`).join('')}
                    ${(article.categories || []).slice(0, 2).map(c => `<span class="news-tag">${escapeHtml(c)}</span>`).join('')}
                </div>
                <p class="news-summary">${escapeHtml(article.summary || '')}</p>
                <div class="news-date">${formatDate(article.published_at)}</div>
            </div>
        `).join('')}
    `;
}

// ========================================
// TRANSACTIONS (M&A DEALS)
// ========================================

async function loadTransactions() {
    const result = await fetchAPI('/api/transactions/public?limit=8');
    const container = document.getElementById('transactions-grid');
    if (!container) return;

    if (result && result.transactions && result.transactions.length > 0) {
        container.innerHTML = result.transactions.map(t => {
            const typeColors = {
                'Acquisition': 'var(--red)', 'ma': 'var(--red)',
                'capex': 'var(--green)', 'ai_infra': 'var(--purple)',
                'jv': 'var(--cyan)', 'equity': 'var(--orange)',
                'debt': 'var(--text3)', 'land': 'var(--orange)',
                'ai_contract': 'var(--pink)'
            };
            const typeLabels = {
                'Acquisition': 'M&A', 'ma': 'M&A',
                'capex': 'CapEx', 'ai_infra': 'AI Infra',
                'jv': 'JV', 'equity': 'Equity',
                'debt': 'Debt', 'land': 'Land',
                'ai_contract': 'AI Contract', 'JV Investment': 'JV'
            };
            const color = typeColors[t.type] || 'var(--accent)';
            const label = typeLabels[t.type] || (t.type || 'Deal');

            return `
                <div class="transaction-card">
                    <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:12px;">
                        <span style="padding:3px 8px;border-radius:4px;font-size:11px;font-weight:600;background:${color}22;color:${color};">${escapeHtml(label)}</span>
                        <span style="font-size:12px;color:var(--text3);">${formatDate(t.date)}</span>
                    </div>
                    <div style="font-weight:600;margin-bottom:6px;font-size:15px;">${escapeHtml(t.buyer || 'Unknown')}</div>
                    <div style="font-size:13px;color:var(--text2);margin-bottom:12px;">${escapeHtml(t.seller ? (t.type === 'Acquisition' || t.type === 'ma' ? 'Acquires ' : 'Invests in ') + t.seller : '')}</div>
                    <div style="display:flex;gap:16px;font-size:13px;">
                        <span style="color:var(--green);font-weight:600;font-family:'JetBrains Mono',monospace;">${escapeHtml(t.value_display || 'Undisclosed')}</span>
                        ${t.power_mw > 0 ? `<span style="color:var(--orange);">${t.power_mw.toLocaleString()} MW</span>` : ''}
                        ${t.market ? `<span style="color:var(--text3);">${escapeHtml(t.market)}</span>` : ''}
                    </div>
                </div>
            `;
        }).join('');
        console.log('💰 Loaded', result.transactions.length, 'M&A deals');
    } else {
        container.innerHTML = '<div class="loading">No transaction data available.</div>';
    }
}

// ========================================
// MARKET GROWTH CHART
// ========================================

async function loadMarketGrowth() {
    const result = await fetchAPI('/api/v1/market-growth');
    const container = document.getElementById('market-growth-chart');
    if (!container || !result || !result.success) return;

    const years = result.years;
    const facilities = result.facilities;
    const investment = result.investment_billions;
    const maxFacilities = Math.max(...facilities);
    const maxInvestment = Math.max(...investment);

    container.innerHTML = `
        <div style="display:flex;gap:24px;align-items:flex-end;height:220px;padding:0 10px;">
            ${years.map((year, i) => {
                const facHeight = (facilities[i] / maxFacilities) * 180;
                const invHeight = (investment[i] / maxInvestment) * 180;
                const isProjection = year >= 2026;
                return `
                    <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;">
                        <div style="display:flex;gap:3px;align-items:flex-end;height:180px;">
                            <div title="${facilities[i].toLocaleString()} facilities" style="width:18px;height:${facHeight}px;background:${isProjection ? 'repeating-linear-gradient(135deg,var(--accent),var(--accent) 3px,transparent 3px,transparent 6px)' : 'var(--accent)'};border-radius:3px 3px 0 0;transition:height 0.5s;opacity:${isProjection ? 0.7 : 1};"></div>
                            <div title="$${investment[i]}B investment" style="width:18px;height:${invHeight}px;background:${isProjection ? 'repeating-linear-gradient(135deg,var(--green),var(--green) 3px,transparent 3px,transparent 6px)' : 'var(--green)'};border-radius:3px 3px 0 0;transition:height 0.5s;opacity:${isProjection ? 0.7 : 1};"></div>
                        </div>
                        <span style="font-size:12px;color:${isProjection ? 'var(--orange)' : 'var(--text3)'};font-weight:${isProjection ? '600' : '400'};">${year}${isProjection ? '*' : ''}</span>
                    </div>
                `;
            }).join('')}
        </div>
        <div style="display:flex;gap:20px;justify-content:center;margin-top:16px;font-size:12px;">
            <span style="display:flex;align-items:center;gap:6px;"><span style="width:12px;height:12px;background:var(--accent);border-radius:2px;"></span>Facilities</span>
            <span style="display:flex;align-items:center;gap:6px;"><span style="width:12px;height:12px;background:var(--green);border-radius:2px;"></span>Investment ($B)</span>
            <span style="color:var(--orange);">* Projected</span>
        </div>
    `;
    console.log('📈 Market growth chart loaded with', years.length, 'years');
}

// ========================================
// MAP
// ========================================

function initMap() {
    map = L.map('map', {
        center: [30, 0],
        zoom: 2,
        minZoom: 2,
        maxZoom: 18,
        worldCopyJump: true
    });
    
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '© OpenStreetMap contributors, © CARTO',
        subdomains: 'abcd',
        maxZoom: 19
    }).addTo(map);
    
    markers = L.markerClusterGroup({
        chunkedLoading: true,
        maxClusterRadius: 50,
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
        zoomToBoundsOnClick: true,
        iconCreateFunction: function(cluster) {
            const count = cluster.getChildCount();
            let size = 'small';
            if (count > 100) size = 'large';
            else if (count > 50) size = 'medium';
            
            return L.divIcon({
                html: `<div>${count}</div>`,
                className: `marker-cluster marker-cluster-${size}`,
                iconSize: L.point(40, 40)
            });
        }
    });
    
    map.addLayer(markers);
}

function updateMapMarkers() {
    markers.clearLayers();
    
    let displayed = 0;
    const filtered = filteredFacilities.filter(f => {
        if (!f.latitude || !f.longitude) return false;
        if (currentRegion === 'all') return true;
        return (f.region || '').toLowerCase().includes(currentRegion.replace('-', ''));
    });
    
    filtered.forEach(f => {
        const statusColor = f.status === 'construction' ? '#f59e0b' : 
                           f.status === 'planned' ? '#6366f1' : '#10b981';
        
        const marker = L.circleMarker([f.latitude, f.longitude], {
            radius: 6,
            fillColor: statusColor,
            color: '#fff',
            weight: 1,
            opacity: 1,
            fillOpacity: 0.8
        });
        
        marker.bindPopup(`
            <div style="min-width: 200px;">
                <strong style="font-size: 14px;">${escapeHtml(f.name)}</strong><br>
                <span style="color: #a8a8b3;">${escapeHtml(f.provider || 'Unknown')}</span><br>
                <div style="margin-top: 8px; font-size: 13px;">
                    📍 ${escapeHtml(f.city || '')} ${escapeHtml(f.country || '')}<br>
                    ${f.power_mw ? `⚡ ${f.power_mw} MW` : ''}
                </div>
            </div>
        `);
        
        markers.addLayer(marker);
        displayed++;
    });
    
    document.getElementById('map-displayed').textContent = displayed.toLocaleString();
    document.getElementById('map-filtered').textContent = filteredFacilities.length.toLocaleString();
    
    console.log('🗺️ Map updated:', displayed, 'markers');
}

// ========================================
// MARKETS DATA
// ========================================

const marketData = [
    // Tier 1 - Primary Markets
    {name: "Northern Virginia", rank: 1, region: "NA", vacancy: 0.8, vacancyChange: "+0.1", pricing: 220, pricingChange: "+15%", inventory: "5.6 GW", construction: "1.8 GW", tier: 1},
    {name: "Dallas-Fort Worth", rank: 2, region: "NA", vacancy: 1.2, vacancyChange: "-0.3", pricing: 165, pricingChange: "+12%", inventory: "1.5 GW", construction: "1.1 GW", tier: 1},
    {name: "Phoenix", rank: 3, region: "NA", vacancy: 1.8, vacancyChange: "-0.5", pricing: 185, pricingChange: "+18%", inventory: "1.2 GW", construction: "1.3 GW", tier: 1},
    {name: "Chicago", rank: 4, region: "NA", vacancy: 2.1, vacancyChange: "+0.2", pricing: 155, pricingChange: "+15%", inventory: "1.1 GW", construction: "1.18 GW", tier: 1},
    {name: "Silicon Valley", rank: 5, region: "NA", vacancy: 1.4, vacancyChange: "+0.3", pricing: 245, pricingChange: "+19%", inventory: "2.9 GW", construction: "0.4 GW", tier: 1},
    {name: "Atlanta", rank: 6, region: "NA", vacancy: 2.8, vacancyChange: "-1.2", pricing: 145, pricingChange: "+26%", inventory: "0.9 GW", construction: "1.11 GW", tier: 1},
    
    // Tier 2 - Secondary Markets
    {name: "Austin", rank: 7, region: "NA", vacancy: 3.2, vacancyChange: "-0.8", pricing: 135, pricingChange: "+14%", inventory: "0.9 GW", construction: "0.34 GW", tier: 2},
    {name: "New York Metro", rank: 8, region: "NA", vacancy: 2.5, vacancyChange: "+0.4", pricing: 195, pricingChange: "+8%", inventory: "1.1 GW", construction: "0.5 GW", tier: 2},
    {name: "Portland/Hillsboro", rank: 9, region: "NA", vacancy: 3.5, vacancyChange: "-0.6", pricing: 125, pricingChange: "+10%", inventory: "0.8 GW", construction: "0.3 GW", tier: 2},
    {name: "Denver", rank: 10, region: "NA", vacancy: 4.2, vacancyChange: "-0.3", pricing: 115, pricingChange: "+9%", inventory: "0.5 GW", construction: "0.2 GW", tier: 2},
    {name: "Columbus", rank: 11, region: "NA", vacancy: 2.9, vacancyChange: "-2.1", pricing: 120, pricingChange: "+22%", inventory: "0.4 GW", construction: "0.6 GW", tier: 2},
    {name: "Salt Lake City", rank: 12, region: "NA", vacancy: 5.1, vacancyChange: "+0.2", pricing: 105, pricingChange: "+7%", inventory: "0.3 GW", construction: "0.15 GW", tier: 2},
    {name: "Seattle", rank: 13, region: "NA", vacancy: 2.9, vacancyChange: "-0.7", pricing: 165, pricingChange: "+13%", inventory: "0.6 GW", construction: "0.35 GW", tier: 2},
    {name: "Los Angeles", rank: 14, region: "NA", vacancy: 3.8, vacancyChange: "-0.4", pricing: 175, pricingChange: "+11%", inventory: "0.7 GW", construction: "0.25 GW", tier: 2},
    
    // Tier 3 - Emerging Markets
    {name: "Nashville", rank: 15, region: "NA", vacancy: 6.2, vacancyChange: "-1.8", pricing: 95, pricingChange: "+28%", inventory: "0.25 GW", construction: "0.45 GW", tier: 3},
    {name: "Kansas City", rank: 16, region: "NA", vacancy: 5.8, vacancyChange: "-0.9", pricing: 85, pricingChange: "+15%", inventory: "0.2 GW", construction: "0.18 GW", tier: 3},
    {name: "Charlotte", rank: 17, region: "NA", vacancy: 4.9, vacancyChange: "-0.5", pricing: 110, pricingChange: "+12%", inventory: "0.3 GW", construction: "0.22 GW", tier: 3},
    {name: "Reno", rank: 18, region: "NA", vacancy: 6.5, vacancyChange: "-1.2", pricing: 90, pricingChange: "+20%", inventory: "0.15 GW", construction: "0.25 GW", tier: 3},
    {name: "Las Vegas", rank: 19, region: "NA", vacancy: 5.5, vacancyChange: "-0.8", pricing: 100, pricingChange: "+16%", inventory: "0.35 GW", construction: "0.3 GW", tier: 3},
    {name: "Richmond", rank: 20, region: "NA", vacancy: 7.0, vacancyChange: "-1.5", pricing: 95, pricingChange: "+25%", inventory: "0.1 GW", construction: "0.2 GW", tier: 3},
    {name: "Indianapolis", rank: 21, region: "NA", vacancy: 8.0, vacancyChange: "-2.0", pricing: 80, pricingChange: "+18%", inventory: "0.08 GW", construction: "0.15 GW", tier: 3},
    
    // EMEA
    {name: "London", rank: 22, region: "EMEA", vacancy: 3.2, vacancyChange: "+0.4", pricing: 195, pricingChange: "+8%", inventory: "1.2 GW", construction: "0.35 GW", tier: 1},
    {name: "Frankfurt", rank: 23, region: "EMEA", vacancy: 2.8, vacancyChange: "-0.6", pricing: 175, pricingChange: "+14%", inventory: "1.0 GW", construction: "0.42 GW", tier: 1},
    {name: "Amsterdam", rank: 24, region: "EMEA", vacancy: 4.1, vacancyChange: "+0.2", pricing: 155, pricingChange: "+6%", inventory: "0.8 GW", construction: "0.18 GW", tier: 1},
    {name: "Dublin", rank: 25, region: "EMEA", vacancy: 3.5, vacancyChange: "-0.8", pricing: 165, pricingChange: "+12%", inventory: "0.6 GW", construction: "0.28 GW", tier: 2},
    {name: "Paris", rank: 26, region: "EMEA", vacancy: 4.8, vacancyChange: "-0.3", pricing: 145, pricingChange: "+9%", inventory: "0.5 GW", construction: "0.2 GW", tier: 2},
    {name: "Madrid", rank: 27, region: "EMEA", vacancy: 5.5, vacancyChange: "-1.0", pricing: 120, pricingChange: "+15%", inventory: "0.3 GW", construction: "0.25 GW", tier: 3},
    {name: "Milan", rank: 28, region: "EMEA", vacancy: 6.0, vacancyChange: "-0.8", pricing: 115, pricingChange: "+12%", inventory: "0.25 GW", construction: "0.18 GW", tier: 3},
    
    // APAC
    {name: "Singapore", rank: 29, region: "APAC", vacancy: 1.9, vacancyChange: "+0.5", pricing: 210, pricingChange: "+16%", inventory: "0.9 GW", construction: "0.15 GW", tier: 1},
    {name: "Tokyo", rank: 30, region: "APAC", vacancy: 2.4, vacancyChange: "-0.2", pricing: 225, pricingChange: "+10%", inventory: "1.1 GW", construction: "0.32 GW", tier: 1},
    {name: "Sydney", rank: 31, region: "APAC", vacancy: 3.6, vacancyChange: "-0.4", pricing: 185, pricingChange: "+14%", inventory: "0.7 GW", construction: "0.25 GW", tier: 2},
    {name: "Hong Kong", rank: 32, region: "APAC", vacancy: 2.1, vacancyChange: "+0.3", pricing: 235, pricingChange: "+7%", inventory: "0.5 GW", construction: "0.1 GW", tier: 2},
    {name: "Mumbai", rank: 33, region: "APAC", vacancy: 5.2, vacancyChange: "-1.5", pricing: 95, pricingChange: "+22%", inventory: "0.4 GW", construction: "0.35 GW", tier: 3},
    {name: "Seoul", rank: 34, region: "APAC", vacancy: 3.0, vacancyChange: "-0.5", pricing: 180, pricingChange: "+12%", inventory: "0.6 GW", construction: "0.2 GW", tier: 2}
];

function renderMarkets(regionFilter = 'all') {
    const container = document.getElementById('market-grid');
    const filtered = regionFilter === 'all' ? marketData : marketData.filter(m => m.region === regionFilter);
    
    container.innerHTML = filtered.map(m => {
        const vacancyClass = m.vacancyChange.startsWith('+') ? 'up' : 'down';
        const pricingClass = m.pricingChange.startsWith('+') ? 'up' : 'down';
        const tierBadge = m.tier === 1 ? '🔥' : m.tier === 2 ? '📈' : '🌱';
        
        return `
            <div class="market-card" data-market="${m.name}">
                <div class="market-header">
                    <div class="market-name">${tierBadge} ${m.name}</div>
                    <span class="market-rank">#${m.rank}</span>
                </div>
                <div class="market-stats">
                    <div class="market-stat">
                        <div class="market-stat-value">${m.vacancy}%</div>
                        <div class="market-stat-label">Vacancy</div>
                        <div class="market-stat-change ${vacancyClass}">${m.vacancyChange} YoY</div>
                    </div>
                    <div class="market-stat">
                        <div class="market-stat-value">$${m.pricing}</div>
                        <div class="market-stat-label">$/kW/mo</div>
                        <div class="market-stat-change ${pricingClass}">${m.pricingChange} YoY</div>
                    </div>
                </div>
                <div style="margin-top: 12px; font-size: 12px; color: var(--text2);">
                    📦 ${m.inventory} inventory • 🏗️ ${m.construction} building
                </div>
            </div>
        `;
    }).join('');
    
    // Add click handlers for market filtering
    document.querySelectorAll('.market-card').forEach(card => {
        card.addEventListener('click', () => {
            const marketName = card.dataset.market;
            filterByMarket(marketName);
        });
    });
}

const MARKET_CITY_ALIASES = {
    'phoenix': ['phoenix', 'mesa', 'tempe', 'scottsdale', 'chandler', 'gilbert', 'goodyear', 'az', 'arizona'],
    'dallas': ['dallas', 'fort worth', 'plano', 'irving', 'arlington', 'carrollton', 'richardson'],
    'northern virginia': ['ashburn', 'loudoun', 'sterling', 'reston', 'herndon', 'manassas'],
    'silicon valley': ['san jose', 'santa clara', 'sunnyvale', 'milpitas', 'fremont', 'palo alto'],
    'chicago': ['chicago', 'aurora', 'elk grove', 'schaumburg'],
    'atlanta': ['atlanta', 'marietta', 'alpharetta', 'duluth'],
    'los angeles': ['los angeles', 'el segundo', 'downtown la'],
    'new york': ['new york', 'nyc', 'manhattan', 'secaucus'],
    'seattle': ['seattle', 'tukwila', 'kent'],
    'denver': ['denver', 'aurora', 'centennial'],
    'austin': ['austin', 'round rock'],
    'houston': ['houston'],
    'miami': ['miami', 'boca raton'],
    'columbus': ['columbus', 'new albany', 'dublin'],
    'salt lake': ['salt lake city', 'west valley'],
    'portland': ['portland', 'hillsboro'],
    'reno': ['reno', 'sparks'],
    'las vegas': ['las vegas', 'henderson']
};

async function filterByMarket(marketName) {
    console.log('🔍 Searching for:', marketName);
    document.getElementById('search-input').value = marketName;
    
    const searchLower = marketName.toLowerCase();
    
    // Check if we have a market alias
    let cityMatches = [];
    for (const [market, cities] of Object.entries(MARKET_CITY_ALIASES)) {
        if (searchLower.includes(market) || market.includes(searchLower)) {
            cityMatches = cities;
            break;
        }
    }
    
    // If no alias found, use the search term directly
    if (cityMatches.length === 0) {
        cityMatches = [searchLower];
    }
    
    // Call API for better results - use facilities endpoint with q parameter
    const result = await fetchAPI(`/api/v1/facilities?q=${encodeURIComponent(marketName)}&limit=100`);
    
    console.log('📡 API response:', result);
    
    if (result && result.data && result.data.length > 0) {
        filteredFacilities = result.data;
        console.log('✅ Found', filteredFacilities.length, 'facilities');
        
        // Zoom map to show results
        const withCoords = filteredFacilities.filter(f => f.latitude && f.longitude);
        if (withCoords.length > 0 && map) {
            const bounds = L.latLngBounds(withCoords.map(f => [f.latitude, f.longitude]));
            map.fitBounds(bounds, { padding: [50, 50], maxZoom: 10 });
        }
    } else {
        // Fallback to local filter
        console.log('⚠️ API returned no results, using local filter');
        filteredFacilities = facilities.filter(f => {
            const city = (f.city || '').toLowerCase();
            const state = (f.state || '').toLowerCase();
            
            return cityMatches.some(c => city.includes(c) || state.includes(c));
        });
    }
    
    updateMapMarkers();
    renderFacilities();
    
    // Scroll to map
    document.getElementById('map-section').scrollIntoView({ behavior: 'smooth' });
}

// ========================================
// SEARCH
// ========================================

function setupSearch() {
    const input = document.getElementById('search-input');
    const dropdown = document.getElementById('search-dropdown');
    let timeout;
    
    input.addEventListener('input', () => {
        clearTimeout(timeout);
        timeout = setTimeout(() => performSearch(input.value), 200);
    });
    
    // Handle Enter key - trigger search immediately
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && input.value.length >= 2) {
            e.preventDefault();
            dropdown.classList.remove('active');
            filterByMarket(input.value);
        }
    });
    
    input.addEventListener('focus', () => {
        if (input.value.length >= 2) performSearch(input.value);
    });
    
    document.addEventListener('click', (e) => {
        if (!input.contains(e.target) && !dropdown.contains(e.target)) {
            dropdown.classList.remove('active');
        }
    });
}

function performSearch(query) {
    const dropdown = document.getElementById('search-dropdown');
    
    if (!query || query.length < 2) {
        dropdown.classList.remove('active');
        return;
    }
    
    const q = query.toLowerCase();
    const results = [];
    
    // Check if query matches a known market/city
    const knownCities = Object.keys(MARKET_CITY_ALIASES);
    for (const city of knownCities) {
        if (city.includes(q) || q.includes(city)) {
            results.push({ type: 'market', name: city.charAt(0).toUpperCase() + city.slice(1), meta: 'City/Market • Click to search' });
            break;
        }
    }
    
    // Search markets
    marketData.forEach(m => {
        if (m.name.toLowerCase().includes(q)) {
            results.push({ type: 'market', name: m.name, meta: `Market • Rank #${m.rank} • ${m.inventory} capacity` });
        }
    });
    
    // Search providers
    const providers = new Set();
    facilities.forEach(f => {
        if (f.provider && f.provider.toLowerCase().includes(q) && !providers.has(f.provider)) {
            providers.add(f.provider);
            results.push({ type: 'provider', name: f.provider, meta: 'Provider' });
        }
    });
    
    // Search facilities
    let facilityCount = 0;
    facilities.forEach(f => {
        if (facilityCount >= 5) return;
        if (f.name && f.name.toLowerCase().includes(q)) {
            results.push({ type: 'facility', name: f.name, meta: `${f.provider || 'Unknown'} • ${f.city || ''}` });
            facilityCount++;
        }
    });
    
    if (results.length === 0) {
        dropdown.classList.remove('active');
        return;
    }
    
    dropdown.innerHTML = results.slice(0, 10).map(r => `
        <div class="search-result" data-type="${r.type}" data-name="${escapeHtml(r.name)}">
            <div class="search-result-name">${escapeHtml(r.name)}</div>
            <div class="search-result-meta">
                <span class="search-result-type ${r.type}">${r.type}</span>
                ${escapeHtml(r.meta)}
            </div>
        </div>
    `).join('');
    
    dropdown.classList.add('active');
    
    // Add click handlers
    dropdown.querySelectorAll('.search-result').forEach(el => {
        el.addEventListener('click', () => {
            const name = el.dataset.name;
            const type = el.dataset.type;
            
            document.getElementById('search-input').value = name;
            dropdown.classList.remove('active');
            
            if (type === 'market') {
                filterByMarket(name);
            } else if (type === 'provider') {
                filteredFacilities = facilities.filter(f => f.provider === name);
                updateMapMarkers();
                renderFacilities();
                document.getElementById('map-section').scrollIntoView({ behavior: 'smooth' });
            } else {
                filteredFacilities = facilities.filter(f => f.name === name);
                updateMapMarkers();
                renderFacilities();
                document.getElementById('map-section').scrollIntoView({ behavior: 'smooth' });
            }
        });
    });
}

// ========================================
// FILTERS
// ========================================

function setupFilters() {
    // Map region filters
    document.querySelectorAll('.map-filter-btn[data-region]').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.map-filter-btn[data-region]').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            
            currentRegion = btn.dataset.region;
            filteredFacilities = currentRegion === 'all' ? [...facilities] : 
                facilities.filter(f => (f.region || '').toLowerCase().includes(currentRegion.replace('-', '')));
            
            updateMapMarkers();
            renderFacilities();
        });
    });
    
    // Market region filters
    document.querySelectorAll('.map-filter-btn[data-market-region]').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.map-filter-btn[data-market-region]').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderMarkets(btn.dataset.marketRegion);
        });
    });
    
    // Load more facilities
    document.getElementById('load-more-facilities').addEventListener('click', () => {
        facilitiesLoaded += 50;
        renderFacilities();
    });
    
    // Refresh news
    document.getElementById('refresh-news').addEventListener('click', loadNews);
}

// ========================================
// UTILITIES
// ========================================

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    try {
        const date = new Date(dateStr);
        return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    } catch {
        return dateStr;
    }
}

// ========================================
// START
// ========================================

document.addEventListener('DOMContentLoaded', init);

// Update data every 5 minutes
setInterval(() => {
    loadStats();
}, 300000);
