/**
 * DC Hub - AI Intelligence Sections
 * Connects to deep_learning_engine.py backend endpoints
 */

const AI_API_BASE = 'https://dchub.cloud';

// Fetch AI-detected transactions
async function fetchAITransactions() {
    try {
        const response = await fetch(AI_API_BASE + '/api/autopilot/transactions?limit=10');
        if (!response.ok) throw new Error('Failed to fetch');
        const data = await response.json();
        return data.transactions || [];
    } catch (error) {
        console.error('AI Transactions error:', error);
        return [];
    }
}

// Fetch capacity pipeline
async function fetchCapacityPipeline() {
    try {
        const response = await fetch(AI_API_BASE + '/api/autopilot/capacity-pipeline?limit=15');
        if (!response.ok) throw new Error('Failed to fetch');
        const data = await response.json();
        return data.pipeline || [];
    } catch (error) {
        console.error('Capacity Pipeline error:', error);
        return [];
    }
}

// Format time ago
function formatTimeAgo(dateString) {
    if (!dateString) return 'Unknown';
    const seconds = Math.floor((new Date() - new Date(dateString)) / 1000);
    if (seconds < 60) return 'Just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return Math.floor(seconds / 86400) + 'd ago';
}

// Render AI transactions
function renderAITransactions(transactions) {
    const container = document.getElementById('ai-transactions-container');
    if (!container) return;

    if (!transactions || !transactions.length) {
        container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text2)">' +
            '<div style="font-size:2rem;margin-bottom:12px">🔍</div>' +
            '<p>AI is scanning for M&A activity...</p>' +
            '<p style="font-size:12px;margin-top:8px;color:var(--text3)">Deals will appear here automatically</p></div>';
        return;
    }

    container.innerHTML = transactions.map(function(tx) {
        var confidence = Math.round((tx.confidence || 0.5) * 100);
        var confColor = confidence >= 70 ? 'var(--green)' : (confidence >= 50 ? 'var(--orange)' : 'var(--text3)');
        var value = tx.value_millions ? '$' + (tx.value_millions >= 1000 ? (tx.value_millions / 1000).toFixed(1) + 'B' : tx.value_millions + 'M') : 'Undisclosed';
        var dealType = tx.deal_type || 'deal';

        return '<div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:12px;transition:all 0.2s" onmouseover="this.style.borderColor=\'var(--accent)\'" onmouseout="this.style.borderColor=\'var(--border)\'">' +
            '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">' +
            '<div>' +
            '<span style="display:inline-block;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;background:rgba(99,102,241,.2);color:var(--accent-light)">' + dealType + '</span>' +
            '<span style="margin-left:8px;font-size:11px;color:' + confColor + '">🤖 ' + confidence + '% confidence</span>' +
            '</div>' +
            '<div style="font-family:\'JetBrains Mono\',monospace;font-weight:700;color:var(--green)">' + value + '</div>' +
            '</div>' +
            '<div style="font-weight:600;margin-bottom:4px">' + (tx.buyer || 'Unknown') + ' → ' + (tx.target || 'Unknown') + '</div>' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:12px;font-size:12px;color:var(--text3)">' +
            '<span>Detected: ' + formatTimeAgo(tx.discovered_at) + '</span>' +
            (tx.source_url ? '<a href="' + tx.source_url + '" target="_blank" style="color:var(--accent-light)">Source →</a>' : '') +
            '</div></div>';
    }).join('');
}

// Render capacity pipeline
function renderCapacityPipeline(pipeline) {
    var container = document.getElementById('ai-capacity-container');
    if (!container) return;

    if (!pipeline || !pipeline.length) {
        container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text2)">' +
            '<div style="font-size:2rem;margin-bottom:12px">⚡</div>' +
            '<p>AI is analyzing capacity announcements...</p>' +
            '<p style="font-size:12px;margin-top:8px;color:var(--text3)">MW updates will appear here automatically</p></div>';
        return;
    }

    // Filter out entries with no useful data (no operator AND no location)
    var validPipeline = pipeline.filter(function(p) {
        return p.operator || p.location || p.capacity_mw >= 50;
    });

    // If no valid entries, show a different message
    if (!validPipeline.length) {
        validPipeline = pipeline.slice(0, 12); // Just show top 12 by MW
    }

    // Calculate totals from ALL pipeline data
    var totalMW = pipeline.reduce(function(sum, p) { return sum + (p.capacity_mw || 0); }, 0);
    var constructionMW = pipeline.filter(function(p) { return p.status === 'under_construction'; })
        .reduce(function(sum, p) { return sum + (p.capacity_mw || 0); }, 0);

    // Stats section
    var statsHtml = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px">' +
        '<div style="background:var(--bg3);border-radius:8px;padding:16px;text-align:center">' +
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:1.5rem;font-weight:700;color:var(--accent-light)">' + Math.round(totalMW).toLocaleString() + ' MW</div>' +
        '<div style="font-size:12px;color:var(--text3);margin-top:4px">Total Detected</div></div>' +
        '<div style="background:var(--bg3);border-radius:8px;padding:16px;text-align:center">' +
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:1.5rem;font-weight:700;color:var(--orange)">' + Math.round(constructionMW).toLocaleString() + ' MW</div>' +
        '<div style="font-size:12px;color:var(--text3);margin-top:4px">Under Construction</div></div>' +
        '<div style="background:var(--bg3);border-radius:8px;padding:16px;text-align:center">' +
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:1.5rem;font-weight:700;color:var(--green)">' + pipeline.length + '</div>' +
        '<div style="font-size:12px;color:var(--text3);margin-top:4px">Projects Detected</div></div></div>';

    // Sort by MW descending and take top 12
    var sortedPipeline = validPipeline.sort(function(a, b) { return (b.capacity_mw || 0) - (a.capacity_mw || 0); }).slice(0, 12);

    // Projects grid
    var projectsHtml = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px">' +
        sortedPipeline.map(function(p) {
            var statusConfig = {
                'operational': { bg: 'rgba(34,197,94,.2)', color: '#22c55e', label: 'Online' },
                'under_construction': { bg: 'rgba(234,179,8,.2)', color: '#eab308', label: 'Construction' },
                'planned': { bg: 'rgba(99,102,241,.2)', color: 'var(--accent-light)', label: 'Planned' }
            };
            var st = statusConfig[p.status] || statusConfig['planned'];
            var conf = Math.round((p.confidence || 0.5) * 100);
            var operatorName = p.operator || 'Unknown Operator';
            var locationName = p.location || 'Location TBD';

            return '<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:14px">' +
                '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
                '<span style="font-weight:600;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + operatorName + '</span>' +
                '<span style="font-family:\'JetBrains Mono\',monospace;color:var(--accent-light);font-weight:600">' + (p.capacity_mw || '?') + ' MW</span></div>' +
                '<div style="color:var(--text2);font-size:13px;margin-bottom:8px">📍 ' + locationName + '</div>' +
                '<div style="display:flex;justify-content:space-between;align-items:center">' +
                '<span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;background:' + st.bg + ';color:' + st.color + '">' + st.label + '</span>' +
                '<span style="font-size:11px;color:var(--text3)">🤖 ' + conf + '%</span></div></div>';
        }).join('') + '</div>';

    container.innerHTML = statsHtml + projectsHtml;
}

// Initialize AI sections
async function initAISections() {
    console.log('🤖 Loading AI Intelligence sections...');
    
    // Load transactions
    var transactions = await fetchAITransactions();
    renderAITransactions(transactions);
    
    // Load capacity pipeline
    var pipeline = await fetchCapacityPipeline();
    renderCapacityPipeline(pipeline);
    
    console.log('✅ AI sections loaded');
    
    // Auto-refresh every 5 minutes
    setInterval(async function() {
        var txData = await fetchAITransactions();
        renderAITransactions(txData);
        
        var capData = await fetchCapacityPipeline();
        renderCapacityPipeline(capData);
    }, 300000);
}

// Run on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAISections);
} else {
    initAISections();
}
