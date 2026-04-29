/**
 * DC Hub Frontend Enhancements
 * ============================
 * 
 * Integrates with Replit backend APIs:
 * 1. Alert System UI
 * 2. Image Matching for News
 * 3. Decision Logging
 * 4. Land & Power Site Evaluation
 * 
 * Usage: Include in index.html after main app.js
 * <script src="/static/js/dchub-enhancements.js"></script>
 */

(function() {
    'use strict';
    
    // ==========================================================================
    // Configuration
    // ==========================================================================
    
    const CONFIG = {
        API_BASE: 'https://dchub.cloud',
        CACHE_TTL: 300000, // 5 minutes
        MAX_RETRIES: 3
    };
    
    // Simple cache
    const cache = new Map();
    
    // ==========================================================================
    // API Helper
    // ==========================================================================
    
    async function apiCall(endpoint, options = {}) {
        const url = `${CONFIG.API_BASE}${endpoint}`;
        const cacheKey = `${options.method || 'GET'}:${url}:${JSON.stringify(options.body || '')}`;
        
        // Check cache for GET requests
        if (!options.method || options.method === 'GET') {
            const cached = cache.get(cacheKey);
            if (cached && Date.now() - cached.timestamp < CONFIG.CACHE_TTL) {
                return cached.data;
            }
        }
        
        const fetchOptions = {
            method: options.method || 'GET',
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            },
            ...options
        };
        
        if (options.body && typeof options.body === 'object') {
            fetchOptions.body = JSON.stringify(options.body);
        }
        
        try {
            const response = await fetch(url, fetchOptions);
            const data = await response.json();
            
            // Cache successful GET responses
            if (!options.method || options.method === 'GET') {
                cache.set(cacheKey, { data, timestamp: Date.now() });
            }
            
            return data;
        } catch (error) {
            console.error(`API Error [${endpoint}]:`, error);
            throw error;
        }
    }
    
    // ==========================================================================
    // 1. ALERT SYSTEM UI
    // ==========================================================================
    
    const AlertSystem = {
        userEmail: null,
        alerts: [],
        
        init() {
            // Try to get email from localStorage or prompt
            this.userEmail = localStorage.getItem('dchub_email');
            this.injectStyles();
            this.createAlertButton();
        },
        
        injectStyles() {
            const style = document.createElement('style');
            style.textContent = `
                .dchub-alert-btn {
                    position: fixed;
                    bottom: 80px;
                    right: 20px;
                    background: linear-gradient(135deg, #2196f3 0%, #1976d2 100%);
                    color: white;
                    border: none;
                    border-radius: 50px;
                    padding: 12px 20px;
                    font-size: 14px;
                    font-weight: 600;
                    cursor: pointer;
                    box-shadow: 0 4px 15px rgba(33, 150, 243, 0.4);
                    z-index: 1000;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    transition: all 0.3s ease;
                }
                .dchub-alert-btn:hover {
                    transform: translateY(-2px);
                    box-shadow: 0 6px 20px rgba(33, 150, 243, 0.5);
                }
                .dchub-alert-btn .badge {
                    background: #ff5722;
                    color: white;
                    border-radius: 50%;
                    width: 20px;
                    height: 20px;
                    font-size: 11px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                
                .dchub-modal-overlay {
                    position: fixed;
                    inset: 0;
                    background: rgba(0, 0, 0, 0.6);
                    backdrop-filter: blur(4px);
                    z-index: 10000;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    opacity: 0;
                    transition: opacity 0.3s ease;
                }
                .dchub-modal-overlay.active { opacity: 1; }
                
                .dchub-modal {
                    background: #1a1a2e;
                    border-radius: 16px;
                    width: 90%;
                    max-width: 600px;
                    max-height: 80vh;
                    overflow: hidden;
                    box-shadow: 0 25px 50px rgba(0, 0, 0, 0.5);
                    transform: scale(0.9);
                    transition: transform 0.3s ease;
                }
                .dchub-modal-overlay.active .dchub-modal { transform: scale(1); }
                
                .dchub-modal-header {
                    background: linear-gradient(135deg, #16213e 0%, #1a1a2e 100%);
                    padding: 20px;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }
                .dchub-modal-header h2 {
                    margin: 0;
                    color: white;
                    font-size: 20px;
                }
                .dchub-modal-close {
                    background: none;
                    border: none;
                    color: #888;
                    font-size: 24px;
                    cursor: pointer;
                    padding: 5px;
                    line-height: 1;
                }
                .dchub-modal-close:hover { color: white; }
                
                .dchub-modal-body {
                    padding: 20px;
                    overflow-y: auto;
                    max-height: calc(80vh - 140px);
                    color: #e0e0e0;
                }
                
                .dchub-alert-card {
                    background: rgba(255, 255, 255, 0.05);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    border-radius: 12px;
                    padding: 16px;
                    margin-bottom: 12px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }
                .dchub-alert-card .info h4 {
                    margin: 0 0 4px 0;
                    color: white;
                }
                .dchub-alert-card .info p {
                    margin: 0;
                    color: #888;
                    font-size: 13px;
                }
                .dchub-alert-card .actions {
                    display: flex;
                    gap: 8px;
                }
                .dchub-alert-card .actions button {
                    background: rgba(255, 255, 255, 0.1);
                    border: none;
                    color: white;
                    padding: 8px 12px;
                    border-radius: 6px;
                    cursor: pointer;
                    font-size: 12px;
                }
                .dchub-alert-card .actions button:hover {
                    background: rgba(255, 255, 255, 0.2);
                }
                .dchub-alert-card .actions button.delete {
                    color: #ff5252;
                }
                
                .dchub-form-group {
                    margin-bottom: 16px;
                }
                .dchub-form-group label {
                    display: block;
                    margin-bottom: 6px;
                    color: #aaa;
                    font-size: 13px;
                }
                .dchub-form-group input,
                .dchub-form-group select,
                .dchub-form-group textarea {
                    width: 100%;
                    padding: 10px 14px;
                    background: rgba(255, 255, 255, 0.05);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    border-radius: 8px;
                    color: white;
                    font-size: 14px;
                    box-sizing: border-box;
                }
                .dchub-form-group input:focus,
                .dchub-form-group select:focus,
                .dchub-form-group textarea:focus {
                    outline: none;
                    border-color: #2196f3;
                }
                
                .dchub-btn-primary {
                    background: linear-gradient(135deg, #2196f3 0%, #1976d2 100%);
                    color: white;
                    border: none;
                    padding: 12px 24px;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 600;
                    cursor: pointer;
                    width: 100%;
                }
                .dchub-btn-primary:hover {
                    background: linear-gradient(135deg, #1e88e5 0%, #1565c0 100%);
                }
                
                .dchub-tabs {
                    display: flex;
                    gap: 8px;
                    margin-bottom: 20px;
                }
                .dchub-tab {
                    background: rgba(255, 255, 255, 0.05);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    color: #aaa;
                    padding: 10px 16px;
                    border-radius: 8px;
                    cursor: pointer;
                    font-size: 13px;
                }
                .dchub-tab.active {
                    background: #2196f3;
                    border-color: #2196f3;
                    color: white;
                }
                
                .dchub-empty-state {
                    text-align: center;
                    padding: 40px 20px;
                    color: #888;
                }
                .dchub-empty-state svg {
                    width: 64px;
                    height: 64px;
                    margin-bottom: 16px;
                    opacity: 0.5;
                }
            `;
            document.head.appendChild(style);
        },
        
        createAlertButton() {
            const btn = document.createElement('button');
            btn.className = 'dchub-alert-btn';
            btn.innerHTML = '🔔 Alerts';
            btn.onclick = () => this.openModal();
            document.body.appendChild(btn);
            
            // Load alerts count
            this.loadAlerts().then(() => {
                if (this.alerts.length > 0) {
                    btn.innerHTML = `🔔 Alerts <span class="badge">${this.alerts.length}</span>`;
                }
            });
        },
        
        async loadAlerts() {
            if (!this.userEmail) return;
            
            try {
                const data = await apiCall(`/api/v1/alerts?email=${encodeURIComponent(this.userEmail)}`);
                this.alerts = data.alerts || [];
            } catch (e) {
                console.error('Failed to load alerts:', e);
            }
        },
        
        openModal() {
            // Check if email is set
            if (!this.userEmail) {
                this.promptEmail();
                return;
            }
            
            const overlay = document.createElement('div');
            overlay.className = 'dchub-modal-overlay';
            overlay.innerHTML = `
                <div class="dchub-modal">
                    <div class="dchub-modal-header">
                        <h2>🔔 Alert Center</h2>
                        <button class="dchub-modal-close">&times;</button>
                    </div>
                    <div class="dchub-modal-body">
                        <div class="dchub-tabs">
                            <button class="dchub-tab active" data-tab="list">My Alerts</button>
                            <button class="dchub-tab" data-tab="create">Create Alert</button>
                        </div>
                        <div id="alert-content"></div>
                    </div>
                </div>
            `;
            
            document.body.appendChild(overlay);
            requestAnimationFrame(() => overlay.classList.add('active'));
            
            // Event listeners
            overlay.querySelector('.dchub-modal-close').onclick = () => this.closeModal(overlay);
            overlay.onclick = (e) => {
                if (e.target === overlay) this.closeModal(overlay);
            };
            
            overlay.querySelectorAll('.dchub-tab').forEach(tab => {
                tab.onclick = () => {
                    overlay.querySelectorAll('.dchub-tab').forEach(t => t.classList.remove('active'));
                    tab.classList.add('active');
                    this.renderTab(tab.dataset.tab, overlay);
                };
            });
            
            this.renderTab('list', overlay);
        },
        
        closeModal(overlay) {
            overlay.classList.remove('active');
            setTimeout(() => overlay.remove(), 300);
        },
        
        async renderTab(tab, overlay) {
            const content = overlay.querySelector('#alert-content');
            
            if (tab === 'list') {
                await this.loadAlerts();
                
                if (this.alerts.length === 0) {
                    content.innerHTML = `
                        <div class="dchub-empty-state">
                            <svg viewBox="0 0 24 24" fill="currentColor">
                                <path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.89 2 2 2zm6-6v-5c0-3.07-1.64-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.63 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/>
                            </svg>
                            <h3>No alerts yet</h3>
                            <p>Create your first alert to get notified about data center news and developments.</p>
                        </div>
                    `;
                } else {
                    content.innerHTML = this.alerts.map(alert => `
                        <div class="dchub-alert-card" data-id="${alert.id}">
                            <div class="info">
                                <h4>${this.getTypeIcon(alert.alert_type)} ${alert.name}</h4>
                                <p>${this.formatConfig(alert.alert_type, JSON.parse(alert.config || '{}'))}</p>
                            </div>
                            <div class="actions">
                                <button onclick="DCHubEnhancements.AlertSystem.testAlert(${alert.id})">Test</button>
                                <button class="delete" onclick="DCHubEnhancements.AlertSystem.deleteAlert(${alert.id})">Delete</button>
                            </div>
                        </div>
                    `).join('');
                }
            } else {
                content.innerHTML = `
                    <form id="create-alert-form">
                        <div class="dchub-form-group">
                            <label>Alert Name</label>
                            <input type="text" name="name" placeholder="e.g., Google Announcements" required>
                        </div>
                        <div class="dchub-form-group">
                            <label>Alert Type</label>
                            <select name="alert_type" onchange="DCHubEnhancements.AlertSystem.updateConfigFields(this.value)">
                                <option value="operator_watch">🏢 Operator Watch</option>
                                <option value="market_watch">📍 Market Watch</option>
                                <option value="capacity_threshold">⚡ Capacity Threshold</option>
                                <option value="keyword_watch">🔍 Keyword Watch</option>
                            </select>
                        </div>
                        <div id="config-fields">
                            <div class="dchub-form-group">
                                <label>Operators (comma-separated)</label>
                                <input type="text" name="operators" placeholder="Google, Microsoft, AWS">
                            </div>
                        </div>
                        <div class="dchub-form-group">
                            <label>Notification Frequency</label>
                            <select name="frequency">
                                <option value="immediate">Immediate</option>
                                <option value="daily">Daily Digest</option>
                                <option value="weekly">Weekly Digest</option>
                            </select>
                        </div>
                        <button type="submit" class="dchub-btn-primary">Create Alert</button>
                    </form>
                `;
                
                document.getElementById('create-alert-form').onsubmit = (e) => this.handleCreateAlert(e, overlay);
            }
        },
        
        getTypeIcon(type) {
            const icons = {
                'operator_watch': '🏢',
                'market_watch': '📍',
                'capacity_threshold': '⚡',
                'keyword_watch': '🔍'
            };
            return icons[type] || '🔔';
        },
        
        formatConfig(type, config) {
            switch (type) {
                case 'operator_watch':
                    return `Watching: ${(config.operators || []).join(', ')}`;
                case 'market_watch':
                    return `Markets: ${(config.markets || []).join(', ')}`;
                case 'capacity_threshold':
                    return `Capacity: ${config.min_mw || 0}MW - ${config.max_mw || '∞'}MW`;
                case 'keyword_watch':
                    return `Keywords: ${(config.keywords || []).join(', ')}`;
                default:
                    return JSON.stringify(config);
            }
        },
        
        updateConfigFields(type) {
            const container = document.getElementById('config-fields');
            
            const fields = {
                'operator_watch': `
                    <div class="dchub-form-group">
                        <label>Operators (comma-separated)</label>
                        <input type="text" name="operators" placeholder="Google, Microsoft, AWS">
                    </div>
                `,
                'market_watch': `
                    <div class="dchub-form-group">
                        <label>Markets (comma-separated)</label>
                        <input type="text" name="markets" placeholder="Phoenix, Dallas, Northern Virginia">
                    </div>
                `,
                'capacity_threshold': `
                    <div class="dchub-form-group">
                        <label>Minimum MW</label>
                        <input type="number" name="min_mw" placeholder="100">
                    </div>
                    <div class="dchub-form-group">
                        <label>Maximum MW (leave empty for no limit)</label>
                        <input type="number" name="max_mw" placeholder="500">
                    </div>
                `,
                'keyword_watch': `
                    <div class="dchub-form-group">
                        <label>Keywords (comma-separated)</label>
                        <input type="text" name="keywords" placeholder="nuclear, renewable, expansion">
                    </div>
                `
            };
            
            container.innerHTML = fields[type] || '';
        },
        
        async handleCreateAlert(e, overlay) {
            e.preventDefault();
            const form = e.target;
            const formData = new FormData(form);
            
            const alertType = formData.get('alert_type');
            let config = {};
            
            switch (alertType) {
                case 'operator_watch':
                    config.operators = formData.get('operators').split(',').map(s => s.trim()).filter(Boolean);
                    break;
                case 'market_watch':
                    config.markets = formData.get('markets').split(',').map(s => s.trim()).filter(Boolean);
                    break;
                case 'capacity_threshold':
                    config.min_mw = parseFloat(formData.get('min_mw')) || 0;
                    if (formData.get('max_mw')) {
                        config.max_mw = parseFloat(formData.get('max_mw'));
                    }
                    break;
                case 'keyword_watch':
                    config.keywords = formData.get('keywords').split(',').map(s => s.trim()).filter(Boolean);
                    break;
            }
            
            try {
                const result = await apiCall('/api/v1/alerts', {
                    method: 'POST',
                    body: {
                        email: this.userEmail,
                        name: formData.get('name'),
                        alert_type: alertType,
                        config: config,
                        frequency: formData.get('frequency')
                    }
                });
                
                if (result.success) {
                    alert('Alert created successfully!');
                    this.renderTab('list', overlay);
                    overlay.querySelector('.dchub-tab[data-tab="list"]').click();
                } else {
                    alert('Failed to create alert: ' + (result.error || 'Unknown error'));
                }
            } catch (error) {
                alert('Error creating alert: ' + error.message);
            }
        },
        
        async testAlert(id) {
            try {
                const result = await apiCall(`/api/v1/alerts/test/${id}`, {
                    method: 'POST',
                    body: { email: this.userEmail }
                });
                alert(result.success ? `Test email sent to ${result.email}` : `Failed: ${result.message}`);
            } catch (error) {
                alert('Error: ' + error.message);
            }
        },
        
        async deleteAlert(id) {
            if (!confirm('Delete this alert?')) return;
            
            try {
                const result = await apiCall(`/api/v1/alerts/${id}?email=${encodeURIComponent(this.userEmail)}`, {
                    method: 'DELETE'
                });
                
                if (result.success) {
                    document.querySelector(`.dchub-alert-card[data-id="${id}"]`)?.remove();
                    this.alerts = this.alerts.filter(a => a.id !== id);
                } else {
                    alert('Failed to delete: ' + (result.error || 'Unknown error'));
                }
            } catch (error) {
                alert('Error: ' + error.message);
            }
        },
        
        promptEmail() {
            const email = prompt('Enter your email to manage alerts:');
            if (email && email.includes('@')) {
                this.userEmail = email;
                localStorage.setItem('dchub_email', email);
                this.openModal();
            }
        }
    };
    
    // ==========================================================================
    // 2. IMAGE MATCHING FOR NEWS
    // ==========================================================================
    
    const ImageMatcher = {
        async getImage(title, category = null) {
            try {
                const result = await apiCall('/api/v1/images/match', {
                    method: 'POST',
                    body: { title, category }
                });
                return result.image_url || result.fallback_url;
            } catch (e) {
                console.error('Image matching error:', e);
                return '/static/images/default-news.jpg';
            }
        },
        
        async enhanceNewsFeed() {
            // Find news items without images
            const newsItems = document.querySelectorAll('.news-item:not(.image-matched)');
            
            for (const item of newsItems) {
                const title = item.querySelector('.news-title')?.textContent;
                if (title) {
                    const imageUrl = await this.getImage(title);
                    const imgEl = item.querySelector('img');
                    if (imgEl) {
                        imgEl.src = imageUrl;
                    }
                    item.classList.add('image-matched');
                }
            }
        }
    };
    
    // ==========================================================================
    // 3. DECISION LOGGING
    // ==========================================================================
    
    const DecisionLogger = {
        async log(decision) {
            try {
                await apiCall('/api/v1/decisions', {
                    method: 'POST',
                    body: {
                        type: decision.type || 'user_action',
                        action: decision.action,
                        context: decision.context || {},
                        timestamp: new Date().toISOString()
                    }
                });
            } catch (e) {
                console.error('Decision logging error:', e);
            }
        },
        
        // Track common user actions
        trackClick(element, action) {
            this.log({
                type: 'click',
                action: action,
                context: {
                    element: element.tagName,
                    text: element.textContent?.substring(0, 100),
                    page: window.location.pathname
                }
            });
        },
        
        trackSearch(query, results_count) {
            this.log({
                type: 'search',
                action: 'facility_search',
                context: { query, results_count }
            });
        },
        
        trackFilter(filter_type, value) {
            this.log({
                type: 'filter',
                action: `filter_${filter_type}`,
                context: { filter_type, value }
            });
        }
    };
    
    // ==========================================================================
    // 4. LAND & POWER ENHANCEMENTS
    // ==========================================================================
    
    const LandPower = {
        async evaluateSite(lat, lng) {
            try {
                const result = await apiCall(`/api/v1/energy/site-evaluation?lat=${lat}&lng=${lng}`);
                return result;
            } catch (e) {
                console.error('Site evaluation error:', e);
                return null;
            }
        },
        
        async getElectricityPrices(state) {
            try {
                const result = await apiCall(`/api/v1/energy/prices/electricity?state=${state}`);
                return result;
            } catch (e) {
                console.error('Pricing error:', e);
                return null;
            }
        },
        
        async getPowerPlants(lat, lng, radius = 50) {
            try {
                const result = await apiCall(`/api/v1/energy/power-plants?lat=${lat}&lng=${lng}&radius=${radius}`);
                return result.power_plants || [];
            } catch (e) {
                console.error('Power plants error:', e);
                return [];
            }
        },
        
        async getSubstations(lat, lng, radius = 25) {
            try {
                const result = await apiCall(`/api/v1/energy/substations?lat=${lat}&lng=${lng}&radius=${radius}`);
                return result.substations || [];
            } catch (e) {
                console.error('Substations error:', e);
                return [];
            }
        },
        
        // Generate PDF report (requires jsPDF to be loaded)
        async generatePDFReport(lat, lng) {
            if (typeof jspdf === 'undefined') {
                console.error('jsPDF not loaded. Include jspdf library first.');
                return;
            }
            
            const evaluation = await this.evaluateSite(lat, lng);
            if (!evaluation) {
                alert('Failed to evaluate site');
                return;
            }
            
            const { jsPDF } = jspdf;
            const doc = new jsPDF();
            
            // Header
            doc.setFillColor(26, 26, 46);
            doc.rect(0, 0, 210, 40, 'F');
            doc.setTextColor(255, 255, 255);
            doc.setFontSize(24);
            doc.text('DC Hub Site Evaluation Report', 20, 25);
            
            // Reset text color
            doc.setTextColor(0, 0, 0);
            
            // Location
            doc.setFontSize(12);
            doc.text(`Location: ${lat.toFixed(4)}, ${lng.toFixed(4)}`, 20, 55);
            doc.text(`State: ${evaluation.location?.state || 'N/A'}`, 20, 62);
            doc.text(`Generated: ${new Date().toLocaleString()}`, 20, 69);
            
            // Overall Score
            doc.setFontSize(20);
            doc.setTextColor(33, 150, 243);
            doc.text(`Overall Score: ${evaluation.overall_score}/100`, 20, 85);
            
            // Individual Scores
            doc.setFontSize(14);
            doc.setTextColor(0, 0, 0);
            let y = 100;
            
            const scores = [
                { name: 'Power Infrastructure', key: 'power', icon: '⚡' },
                { name: 'Energy Cost', key: 'cost', icon: '💰' },
                { name: 'Renewable Energy', key: 'renewable', icon: '🌱' },
                { name: 'Grid Reliability', key: 'reliability', icon: '🔌' }
            ];
            
            for (const score of scores) {
                doc.text(`${score.icon} ${score.name}: ${evaluation.scores[score.key]}/100`, 20, y);
                y += 10;
            }
            
            // Pricing section
            y += 10;
            doc.setFontSize(16);
            doc.text('Electricity Pricing', 20, y);
            y += 10;
            doc.setFontSize(12);
            
            const pricing = evaluation.pricing?.prices || {};
            doc.text(`Industrial: ${pricing.industrial?.price || 'N/A'} ${pricing.industrial?.unit || ''}`, 25, y);
            y += 7;
            doc.text(`Commercial: ${pricing.commercial?.price || 'N/A'} ${pricing.commercial?.unit || ''}`, 25, y);
            
            // Nearby Infrastructure
            y += 15;
            doc.setFontSize(16);
            doc.text('Nearby Power Infrastructure', 20, y);
            y += 10;
            doc.setFontSize(10);
            
            const plants = evaluation.power_plants || [];
            for (let i = 0; i < Math.min(5, plants.length); i++) {
                const p = plants[i];
                doc.text(`• ${p.name} - ${p.capacity_mw}MW ${p.fuel_type} (${p.distance_miles}mi)`, 25, y);
                y += 6;
            }
            
            // Footer
            doc.setFontSize(10);
            doc.setTextColor(128, 128, 128);
            doc.text('Generated by DC Hub - dchub.cloud', 20, 280);
            
            // Save
            doc.save(`site-report-${lat.toFixed(2)}-${lng.toFixed(2)}.pdf`);
        },
        
        // Add site evaluation panel to map
        addEvaluationPanel(map) {
            const panel = document.createElement('div');
            panel.id = 'site-evaluation-panel';
            panel.innerHTML = `
                <div style="position: absolute; bottom: 20px; left: 20px; background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.2); max-width: 300px; z-index: 1000;">
                    <h4 style="margin: 0 0 10px 0;">📍 Click map to evaluate site</h4>
                    <div id="evaluation-results" style="display: none;">
                        <div id="score-display"></div>
                        <button onclick="DCHubEnhancements.LandPower.generatePDFReport(window.lastEvalLat, window.lastEvalLng)" style="margin-top: 10px; padding: 8px 16px; background: #2196f3; color: white; border: none; border-radius: 4px; cursor: pointer;">
                            📄 Download PDF Report
                        </button>
                    </div>
                </div>
            `;
            document.body.appendChild(panel);
            
            // Listen for map clicks
            if (map && map.on) {
                map.on('click', async (e) => {
                    const { lat, lng } = e.latlng;
                    window.lastEvalLat = lat;
                    window.lastEvalLng = lng;
                    
                    document.getElementById('score-display').innerHTML = '<p>Evaluating...</p>';
                    document.getElementById('evaluation-results').style.display = 'block';
                    
                    const result = await this.evaluateSite(lat, lng);
                    
                    if (result) {
                        document.getElementById('score-display').innerHTML = `
                            <p><strong>Overall Score: ${result.overall_score}/100</strong></p>
                            <p>⚡ Power: ${result.scores.power}</p>
                            <p>💰 Cost: ${result.scores.cost}</p>
                            <p>🌱 Renewable: ${result.scores.renewable}</p>
                            <p>🔌 Reliability: ${result.scores.reliability}</p>
                            <p style="color: #666; font-size: 12px;">State: ${result.location?.state || 'N/A'}</p>
                        `;
                    } else {
                        document.getElementById('score-display').innerHTML = '<p style="color: red;">Evaluation failed</p>';
                    }
                });
            }
        }
    };
    
    // ==========================================================================
    // Initialize & Export
    // ==========================================================================
    
    // Auto-initialize on DOM ready
    document.addEventListener('DOMContentLoaded', () => {
        AlertSystem.init();
        
        // Auto-enhance news feed periodically
        setInterval(() => ImageMatcher.enhanceNewsFeed(), 5000);
        
        console.log('✅ DC Hub Enhancements loaded');
    });
    
    // Export for global access
    window.DCHubEnhancements = {
        AlertSystem,
        ImageMatcher,
        DecisionLogger,
        LandPower,
        apiCall,
        CONFIG
    };
    
})();
