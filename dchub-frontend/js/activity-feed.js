/**
 * DC Hub - Live Activity Feed
 * Real-time ticker showing platform activity
 */

const ACTIVITY_API_BASE = 'https://dchub.cloud';

// Activity feed state
let activityFeedRunning = false;
let activityItems = [];

// Initialize the activity feed
function initActivityFeed() {
    const container = document.getElementById('activity-feed-container');
    if (!container) {
        console.log('Activity feed container not found, creating floating feed');
        createFloatingFeed();
    }
    
    // Load initial activities
    loadActivityFeed();
    
    // Auto-refresh every 60 seconds
    setInterval(loadActivityFeed, 60000);
    
    // Start ticker animation
    activityFeedRunning = true;
    runTicker();
}

// Create floating activity feed if no container exists
function createFloatingFeed() {
    const feed = document.createElement('div');
    feed.id = 'activity-feed-floating';
    feed.innerHTML = `
        <div class="activity-feed-header">
            <span class="activity-feed-live">● LIVE</span>
            <span class="activity-feed-title">Activity Feed</span>
            <button class="activity-feed-toggle" onclick="toggleActivityFeed()">−</button>
        </div>
        <div class="activity-feed-body" id="activity-feed-container">
            <div class="activity-feed-loading">Loading activity...</div>
        </div>
    `;
    document.body.appendChild(feed);
    
    // Add styles
    const styles = document.createElement('style');
    styles.textContent = `
        #activity-feed-floating {
            position: fixed;
            bottom: 20px;
            right: 20px;
            width: 360px;
            max-height: 400px;
            background: var(--bg2, #12121a);
            border: 1px solid var(--border, #2a2a3a);
            border-radius: 12px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
            z-index: 1000;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            overflow: hidden;
            transition: all 0.3s ease;
        }
        #activity-feed-floating.minimized {
            max-height: 44px;
        }
        #activity-feed-floating.minimized .activity-feed-body {
            display: none;
        }
        .activity-feed-header {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 12px 16px;
            background: var(--bg3, #1a1a25);
            border-bottom: 1px solid var(--border, #2a2a3a);
        }
        .activity-feed-live {
            color: #22c55e;
            font-size: 11px;
            font-weight: 600;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .activity-feed-title {
            color: var(--text, #fff);
            font-size: 13px;
            font-weight: 600;
            flex: 1;
        }
        .activity-feed-toggle {
            background: none;
            border: none;
            color: var(--text2, #a0a0b0);
            font-size: 18px;
            cursor: pointer;
            padding: 0 4px;
        }
        .activity-feed-body {
            max-height: 350px;
            overflow-y: auto;
        }
        .activity-feed-loading {
            padding: 20px;
            text-align: center;
            color: var(--text3, #606070);
            font-size: 13px;
        }
        .activity-item {
            display: flex;
            gap: 12px;
            padding: 12px 16px;
            border-bottom: 1px solid var(--border, #2a2a3a);
            transition: background 0.2s;
            animation: slideIn 0.3s ease;
        }
        @keyframes slideIn {
            from { opacity: 0; transform: translateX(20px); }
            to { opacity: 1; transform: translateX(0); }
        }
        .activity-item:hover {
            background: var(--bg3, #1a1a25);
        }
        .activity-item:last-child {
            border-bottom: none;
        }
        .activity-icon {
            font-size: 18px;
            width: 24px;
            text-align: center;
        }
        .activity-content {
            flex: 1;
            min-width: 0;
        }
        .activity-text {
            color: var(--text, #fff);
            font-size: 13px;
            line-height: 1.4;
            margin-bottom: 4px;
        }
        .activity-text strong {
            color: var(--accent-light, #818cf8);
        }
        .activity-meta {
            display: flex;
            gap: 12px;
            font-size: 11px;
            color: var(--text3, #606070);
        }
        .activity-time {
            color: var(--text3, #606070);
        }
        .activity-tag {
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .activity-tag.capacity { background: rgba(99,102,241,0.2); color: #818cf8; }
        .activity-tag.news { background: rgba(34,197,94,0.2); color: #22c55e; }
        .activity-tag.facility { background: rgba(245,158,11,0.2); color: #f59e0b; }
        .activity-tag.deal { background: rgba(168,85,247,0.2); color: #a855f7; }
        
        @media (max-width: 768px) {
            #activity-feed-floating {
                width: calc(100% - 32px);
                right: 16px;
                bottom: 16px;
            }
        }
    `;
    document.head.appendChild(styles);
}

// Toggle feed minimized state
function toggleActivityFeed() {
    const feed = document.getElementById('activity-feed-floating');
    if (feed) {
        feed.classList.toggle('minimized');
        const btn = feed.querySelector('.activity-feed-toggle');
        btn.textContent = feed.classList.contains('minimized') ? '+' : '−';
    }
}

// Load activity from unified endpoint
async function loadActivityFeed() {
    try {
        const response = await fetch(ACTIVITY_API_BASE + '/api/activity-feed?limit=20');
        const data = await response.json();
        
        if (data.activities) {
            activityItems = data.activities.map(item => ({
                ...item,
                text: item.text.replace(/([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)/g, '<strong>$1</strong>')
            }));
            renderActivityFeed();
        }
    } catch (error) {
        console.error('Error loading activity feed:', error);
        // Fallback to multiple endpoints if unified fails
        loadActivityFeedFallback();
    }
}

// Fallback: Load activity from multiple sources
async function loadActivityFeedFallback() {
    try {
        const activities = [];
        
        // Fetch capacity updates
        try {
            const capRes = await fetch(ACTIVITY_API_BASE + '/api/autopilot/capacity-pipeline?limit=10');
            const capData = await capRes.json();
            if (capData.pipeline) {
                capData.pipeline.forEach(item => {
                    if (item.operator || item.location) {
                        activities.push({
                            type: 'capacity',
                            icon: '⚡',
                            text: `<strong>${item.operator || 'New project'}</strong> - ${item.capacity_mw}MW ${item.status === 'under_construction' ? 'under construction' : 'announced'}${item.location ? ' in ' + item.location : ''}`,
                            time: item.discovered_at,
                            tag: 'capacity'
                        });
                    }
                });
            }
        } catch (e) { console.log('Capacity fetch error:', e); }
        
        // Fetch recent news
        try {
            const newsRes = await fetch(ACTIVITY_API_BASE + '/api/news?limit=10');
            const newsData = await newsRes.json();
            if (newsData.articles) {
                newsData.articles.forEach(item => {
                    activities.push({
                        type: 'news',
                        icon: '📰',
                        text: `<strong>${item.title}</strong>`,
                        time: item.published_at || item.created_at,
                        tag: 'news',
                        url: item.url
                    });
                });
            }
        } catch (e) { console.log('News fetch error:', e); }
        
        // Fetch recent transactions
        try {
            const txRes = await fetch(ACTIVITY_API_BASE + '/api/autopilot/transactions?limit=5');
            const txData = await txRes.json();
            if (txData.transactions) {
                txData.transactions.forEach(item => {
                    activities.push({
                        type: 'deal',
                        icon: '🤝',
                        text: `<strong>${item.buyer || 'Unknown'}</strong> ${item.deal_type || 'deal'} with <strong>${item.target || 'Unknown'}</strong>${item.value_millions ? ' - $' + item.value_millions + 'M' : ''}`,
                        time: item.discovered_at,
                        tag: 'deal'
                    });
                });
            }
        } catch (e) { console.log('Transactions fetch error:', e); }
        
        // Fetch discovery status for facility updates
        try {
            const discRes = await fetch(ACTIVITY_API_BASE + '/api/discovery/status');
            const discData = await discRes.json();
            if (discData.total_facilities) {
                activities.push({
                    type: 'facility',
                    icon: '🔍',
                    text: `<strong>${discData.total_facilities.toLocaleString()}</strong> facilities tracked across <strong>${discData.total_companies || 325}+</strong> companies`,
                    time: new Date().toISOString(),
                    tag: 'facility'
                });
            }
        } catch (e) { console.log('Discovery fetch error:', e); }
        
        // Sort by time (newest first)
        activities.sort((a, b) => {
            const timeA = a.time ? new Date(a.time).getTime() : 0;
            const timeB = b.time ? new Date(b.time).getTime() : 0;
            return timeB - timeA;
        });
        
        // Store and render
        activityItems = activities.slice(0, 20);
        renderActivityFeed();
        
    } catch (error) {
        console.error('Error loading activity feed fallback:', error);
    }
}

// Render the activity feed
function renderActivityFeed() {
    const container = document.getElementById('activity-feed-container');
    if (!container) return;
    
    if (activityItems.length === 0) {
        container.innerHTML = '<div class="activity-feed-loading">No recent activity</div>';
        return;
    }
    
    container.innerHTML = activityItems.map(item => `
        <div class="activity-item">
            <div class="activity-icon">${item.icon}</div>
            <div class="activity-content">
                <div class="activity-text">${item.text}</div>
                <div class="activity-meta">
                    <span class="activity-time">${formatTimeAgo(item.time)}</span>
                    <span class="activity-tag ${item.tag}">${item.tag}</span>
                </div>
            </div>
        </div>
    `).join('');
}

// Format time ago
function formatTimeAgo(dateString) {
    if (!dateString) return 'Recently';
    const seconds = Math.floor((new Date() - new Date(dateString)) / 1000);
    if (seconds < 60) return 'Just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    if (seconds < 604800) return Math.floor(seconds / 86400) + 'd ago';
    return Math.floor(seconds / 604800) + 'w ago';
}

// Ticker animation for horizontal display (optional)
function runTicker() {
    // Reserved for horizontal ticker implementation
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initActivityFeed);
} else {
    initActivityFeed();
}

// Export for manual initialization
window.initActivityFeed = initActivityFeed;
window.toggleActivityFeed = toggleActivityFeed;
