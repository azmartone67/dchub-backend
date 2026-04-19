/**
 * DC Hub Alerts Frontend
 * =======================
 * Connects to /api/v1/simple-alerts API
 * 
 * Add to your HTML:
 * <script src="/static/js/dchub-alerts.js" data-cfasync="false"></script>
 */

(function() {
    'use strict';
    
    const API_BASE = 'https://dchub.cloud';
    
    // ==========================================================================
    // Alert System
    // ==========================================================================
    
    const AlertSystem = {
        userEmail: null,
        alerts: [],
        
        init() {
            this.userEmail = localStorage.getItem('dchub_alert_email');
            this.injectStyles();
            this.createAlertButton();
            console.log('🔔 DC Hub Alerts initialized');
        },
        
        injectStyles() {
            if (document.getElementById('dchub-alert-styles')) return;
            
            const style = document.createElement('style');
            style.id = 'dchub-alert-styles';
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
                    z-index: 9999;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    transition: all 0.3s ease;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                }
                .dchub-alert-btn:hover {
                    transform: translateY(-2px);
                    box-shadow: 0 6px 20px rgba(33, 150, 243, 0.5);
                }
                .dchub-alert-btn .badge {
                    background: #ff5722;
                    color: white;
                    border-radius: 50%;
                    min-width: 20px;
                    height: 20px;
                    font-size: 11px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 0 6px;
                }
                
                .dchub-modal-overlay {
                    position: fixed;
                    inset: 0;
                    background: rgba(0, 0, 0, 0.7);
                    backdrop-filter: blur(4px);
                    z-index: 10000;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    opacity: 0;
                    visibility: hidden;
                    transition: all 0.3s ease;
                }
                .dchub-modal-overlay.active { 
                    opacity: 1; 
                    visibility: visible;
                }
                
                .dchub-modal {
                    background: #1a1a2e;
                    border-radius: 16px;
                    width: 90%;
                    max-width: 550px;
                    max-height: 85vh;
                    overflow: hidden;
                    box-shadow: 0 25px 50px rgba(0, 0, 0, 0.5);
                    transform: scale(0.9) translateY(20px);
                    transition: transform 0.3s ease;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                }
                .dchub-modal-overlay.active .dchub-modal { 
                    transform: scale(1) translateY(0); 
                }
                
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
                    font-weight: 600;
                }
                .dchub-modal-close {
                    background: none;
                    border: none;
                    color: #888;
                    font-size: 28px;
                    cursor: pointer;
                    padding: 0;
                    line-height: 1;
                    transition: color 0.2s;
                }
                .dchub-modal-close:hover { color: white; }
                
                .dchub-modal-body {
                    padding: 20px;
                    overflow-y: auto;
                    max-height: calc(85vh - 80px);
                    color: #e0e0e0;
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
                    padding: 10px 20px;
                    border-radius: 8px;
                    cursor: pointer;
                    font-size: 14px;
                    transition: all 0.2s;
                }
                .dchub-tab:hover {
                    background: rgba(255, 255, 255, 0.1);
                }
                .dchub-tab.active {
                    background: #2196f3;
                    border-color: #2196f3;
                    color: white;
                }
                
                .dchub-alert-card {
                    background: rgba(255, 255, 255, 0.05);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    border-radius: 12px;
                    padding: 16px;
                    margin-bottom: 12px;
                }
                .dchub-alert-card-header {
                    display: flex;
                    justify-content: space-between;
                    align-items: flex-start;
                    margin-bottom: 8px;
                }
                .dchub-alert-card h4 {
                    margin: 0;
                    color: white;
                    font-size: 16px;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                }
                .dchub-alert-card p {
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
                    padding: 6px 12px;
                    border-radius: 6px;
                    cursor: pointer;
                    font-size: 12px;
                    transition: background 0.2s;
                }
                .dchub-alert-card .actions button:hover {
                    background: rgba(255, 255, 255, 0.2);
                }
                .dchub-alert-card .actions button.delete {
                    color: #ff5252;
                }
                .dchub-alert-card .actions button.delete:hover {
                    background: rgba(255, 82, 82, 0.2);
                }
                .dchub-alert-card .meta {
                    margin-top: 10px;
                    padding-top: 10px;
                    border-top: 1px solid rgba(255,255,255,0.05);
                    font-size: 11px;
                    color: #666;
                }
                .dchub-alert-card .frequency-badge {
                    display: inline-block;
                    background: rgba(33, 150, 243, 0.2);
                    color: #64b5f6;
                    padding: 2px 8px;
                    border-radius: 4px;
                    font-size: 11px;
                    margin-left: 8px;
                }
                
                .dchub-form-group {
                    margin-bottom: 16px;
                }
                .dchub-form-group label {
                    display: block;
                    margin-bottom: 6px;
                    color: #aaa;
                    font-size: 13px;
                    font-weight: 500;
                }
                .dchub-form-group input,
                .dchub-form-group select,
                .dchub-form-group textarea {
                    width: 100%;
                    padding: 12px 14px;
                    background: rgba(255, 255, 255, 0.05);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    border-radius: 8px;
                    color: white;
                    font-size: 14px;
                    box-sizing: border-box;
                    transition: border-color 0.2s;
                }
                .dchub-form-group input:focus,
                .dchub-form-group select:focus {
                    outline: none;
                    border-color: #2196f3;
                }
                .dchub-form-group input::placeholder {
                    color: #666;
                }
                .dchub-form-group .hint {
                    font-size: 11px;
                    color: #666;
                    margin-top: 4px;
                }
                
                .dchub-btn-primary {
                    background: linear-gradient(135deg, #2196f3 0%, #1976d2 100%);
                    color: white;
                    border: none;
                    padding: 14px 24px;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 600;
                    cursor: pointer;
                    width: 100%;
                    transition: all 0.2s;
                }
                .dchub-btn-primary:hover {
                    transform: translateY(-1px);
                    box-shadow: 0 4px 12px rgba(33, 150, 243, 0.4);
                }
                .dchub-btn-primary:disabled {
                    opacity: 0.6;
                    cursor: not-allowed;
                    transform: none;
                }
                
                .dchub-empty-state {
                    text-align: center;
                    padding: 40px 20px;
                    color: #888;
                }
                .dchub-empty-state .icon {
                    font-size: 48px;
                    margin-bottom: 16px;
                    opacity: 0.5;
                }
                .dchub-empty-state h3 {
                    margin: 0 0 8px 0;
                    color: #aaa;
                }
                
                .dchub-email-prompt {
                    text-align: center;
                    padding: 20px;
                }
                .dchub-email-prompt p {
                    margin-bottom: 16px;
                    color: #aaa;
                }
                
                .dchub-toast {
                    position: fixed;
                    bottom: 140px;
                    right: 20px;
                    background: #323232;
                    color: white;
                    padding: 12px 20px;
                    border-radius: 8px;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                    z-index: 10001;
                    animation: slideIn 0.3s ease;
                }
                .dchub-toast.success { background: #4caf50; }
                .dchub-toast.error { background: #f44336; }
                @keyframes slideIn {
                    from { opacity: 0; transform: translateX(20px); }
                    to { opacity: 1; transform: translateX(0); }
                }
            `;
            document.head.appendChild(style);
        },
        
        createAlertButton() {
            if (document.getElementById('dchub-alert-btn')) return;
            
            const btn = document.createElement('button');
            btn.id = 'dchub-alert-btn';
            btn.className = 'dchub-alert-btn';
            btn.innerHTML = '🔔 Alerts';
            btn.onclick = () => this.openModal();
            document.body.appendChild(btn);
            
            // Load alert count if email exists
            if (this.userEmail) {
                this.loadAlerts().then(() => this.updateButtonBadge());
            }
        },
        
        updateButtonBadge() {
            const btn = document.getElementById('dchub-alert-btn');
            if (btn && this.alerts.length > 0) {
                btn.innerHTML = `🔔 Alerts <span class="badge">${this.alerts.length}</span>`;
            } else if (btn) {
                btn.innerHTML = '🔔 Alerts';
            }
        },
        
        async loadAlerts() {
            if (!this.userEmail) return;
            
            try {
                const resp = await fetch(`${API_BASE}/api/v1/simple-alerts?email=${encodeURIComponent(this.userEmail)}`);
                const data = await resp.json();
                this.alerts = data.alerts || [];
            } catch (e) {
                console.error('Failed to load alerts:', e);
                this.alerts = [];
            }
        },
        
        openModal() {
            if (document.getElementById('dchub-alert-modal')) return;
            
            const overlay = document.createElement('div');
            overlay.id = 'dchub-alert-modal';
            overlay.className = 'dchub-modal-overlay';
            overlay.innerHTML = `
                <div class="dchub-modal">
                    <div class="dchub-modal-header">
                        <h2>🔔 Alert Center</h2>
                        <button class="dchub-modal-close">&times;</button>
                    </div>
                    <div class="dchub-modal-body">
                        <div id="dchub-alert-content"></div>
                    </div>
                </div>
            `;
            
            document.body.appendChild(overlay);
            
            // Trigger animation
            requestAnimationFrame(() => {
                overlay.classList.add('active');
            });
            
            // Event listeners
            overlay.querySelector('.dchub-modal-close').onclick = () => this.closeModal();
            overlay.onclick = (e) => {
                if (e.target === overlay) this.closeModal();
            };
            
            // Check if email is set
            if (!this.userEmail) {
                this.showEmailPrompt();
            } else {
                this.showMainContent();
            }
        },
        
        closeModal() {
            const overlay = document.getElementById('dchub-alert-modal');
            if (overlay) {
                overlay.classList.remove('active');
                setTimeout(() => overlay.remove(), 300);
            }
        },
        
        showEmailPrompt() {
            const content = document.getElementById('dchub-alert-content');
            content.innerHTML = `
                <div class="dchub-email-prompt">
                    <div style="font-size: 48px; margin-bottom: 16px;">📧</div>
                    <h3 style="color: white; margin-bottom: 8px;">Enter Your Email</h3>
                    <p>We'll use this to manage your alerts</p>
                    <div class="dchub-form-group">
                        <input type="email" id="dchub-email-input" placeholder="your@email.com">
                    </div>
                    <button class="dchub-btn-primary" onclick="DCHubAlerts.saveEmail()">Continue</button>
                </div>
            `;
            
            // Focus input
            setTimeout(() => {
                document.getElementById('dchub-email-input')?.focus();
            }, 100);
        },
        
        saveEmail() {
            const input = document.getElementById('dchub-email-input');
            const email = input?.value?.trim().toLowerCase();
            
            if (!email || !email.includes('@') || !email.includes('.')) {
                this.showToast('Please enter a valid email', 'error');
                return;
            }
            
            this.userEmail = email;
            localStorage.setItem('dchub_alert_email', email);
            this.showMainContent();
        },
        
        async showMainContent() {
            const content = document.getElementById('dchub-alert-content');
            content.innerHTML = '<div style="text-align: center; padding: 40px; color: #888;">Loading...</div>';
            
            await this.loadAlerts();
            
            content.innerHTML = `
                <div class="dchub-tabs">
                    <button class="dchub-tab active" data-tab="list">My Alerts (${this.alerts.length})</button>
                    <button class="dchub-tab" data-tab="create">+ Create Alert</button>
                </div>
                <div id="dchub-tab-content"></div>
            `;
            
            // Tab event listeners
            content.querySelectorAll('.dchub-tab').forEach(tab => {
                tab.onclick = () => {
                    content.querySelectorAll('.dchub-tab').forEach(t => t.classList.remove('active'));
                    tab.classList.add('active');
                    this.renderTab(tab.dataset.tab);
                };
            });
            
            this.renderTab('list');
            this.updateButtonBadge();
        },
        
        renderTab(tab) {
            const container = document.getElementById('dchub-tab-content');
            
            if (tab === 'list') {
                if (this.alerts.length === 0) {
                    container.innerHTML = `
                        <div class="dchub-empty-state">
                            <div class="icon">🔔</div>
                            <h3>No alerts yet</h3>
                            <p>Create your first alert to get notified about data center news.</p>
                        </div>
                    `;
                } else {
                    container.innerHTML = this.alerts.map(alert => `
                        <div class="dchub-alert-card" data-id="${alert.id}">
                            <div class="dchub-alert-card-header">
                                <div>
                                    <h4>${this.getTypeIcon(alert.alert_type)} ${alert.name}</h4>
                                    <p>${this.formatConfig(alert.alert_type, alert.config)}</p>
                                </div>
                                <div class="actions">
                                    <button class="delete" onclick="DCHubAlerts.deleteAlert(${alert.id})">🗑️</button>
                                </div>
                            </div>
                            <div class="meta">
                                <span class="frequency-badge">${alert.frequency}</span>
                                Created: ${new Date(alert.created_at).toLocaleDateString()}
                                ${alert.trigger_count > 0 ? ` • Triggered: ${alert.trigger_count}x` : ''}
                            </div>
                        </div>
                    `).join('');
                }
            } else {
                container.innerHTML = `
                    <form id="dchub-create-alert-form">
                        <div class="dchub-form-group">
                            <label>Alert Name</label>
                            <input type="text" id="alert-name" placeholder="e.g., Google Announcements" required>
                        </div>
                        <div class="dchub-form-group">
                            <label>Alert Type</label>
                            <select id="alert-type" onchange="DCHubAlerts.updateConfigFields()">
                                <option value="operator_watch">🏢 Operator Watch</option>
                                <option value="market_watch">📍 Market Watch</option>
                                <option value="capacity_threshold">⚡ Capacity Threshold</option>
                                <option value="keyword_watch">🔍 Keyword Watch</option>
                            </select>
                        </div>
                        <div id="dchub-config-fields">
                            <div class="dchub-form-group">
                                <label>Operators</label>
                                <input type="text" id="config-operators" placeholder="Google, Microsoft, AWS">
                                <div class="hint">Comma-separated list of operators to watch</div>
                            </div>
                        </div>
                        <div class="dchub-form-group">
                            <label>Notification Frequency</label>
                            <select id="alert-frequency">
                                <option value="immediate">Immediate</option>
                                <option value="daily">Daily Digest</option>
                                <option value="weekly">Weekly Digest</option>
                            </select>
                        </div>
                        <button type="submit" class="dchub-btn-primary">Create Alert</button>
                    </form>
                `;
                
                document.getElementById('dchub-create-alert-form').onsubmit = (e) => {
                    e.preventDefault();
                    this.createAlert();
                };
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
                    const min = config.min_mw || 0;
                    const max = config.max_mw ? `${config.max_mw}MW` : '∞';
                    return `Capacity: ${min}MW - ${max}`;
                case 'keyword_watch':
                    return `Keywords: ${(config.keywords || []).join(', ')}`;
                default:
                    return JSON.stringify(config);
            }
        },
        
        updateConfigFields() {
            const type = document.getElementById('alert-type').value;
            const container = document.getElementById('dchub-config-fields');
            
            const fields = {
                'operator_watch': `
                    <div class="dchub-form-group">
                        <label>Operators</label>
                        <input type="text" id="config-operators" placeholder="Google, Microsoft, AWS">
                        <div class="hint">Comma-separated list of operators to watch</div>
                    </div>
                `,
                'market_watch': `
                    <div class="dchub-form-group">
                        <label>Markets</label>
                        <input type="text" id="config-markets" placeholder="Phoenix, Dallas, Northern Virginia">
                        <div class="hint">Comma-separated list of markets to watch</div>
                    </div>
                `,
                'capacity_threshold': `
                    <div class="dchub-form-group">
                        <label>Minimum MW</label>
                        <input type="number" id="config-min-mw" placeholder="100" min="0">
                    </div>
                    <div class="dchub-form-group">
                        <label>Maximum MW (optional)</label>
                        <input type="number" id="config-max-mw" placeholder="Leave empty for no limit" min="0">
                    </div>
                `,
                'keyword_watch': `
                    <div class="dchub-form-group">
                        <label>Keywords</label>
                        <input type="text" id="config-keywords" placeholder="nuclear, renewable, expansion">
                        <div class="hint">Comma-separated list of keywords to watch</div>
                    </div>
                `
            };
            
            container.innerHTML = fields[type] || '';
        },
        
        async createAlert() {
            const name = document.getElementById('alert-name').value.trim();
            const alertType = document.getElementById('alert-type').value;
            const frequency = document.getElementById('alert-frequency').value;
            
            if (!name) {
                this.showToast('Please enter an alert name', 'error');
                return;
            }
            
            let config = {};
            
            switch (alertType) {
                case 'operator_watch':
                    const operators = document.getElementById('config-operators').value;
                    config.operators = operators.split(',').map(s => s.trim()).filter(Boolean);
                    if (config.operators.length === 0) {
                        this.showToast('Please enter at least one operator', 'error');
                        return;
                    }
                    break;
                case 'market_watch':
                    const markets = document.getElementById('config-markets').value;
                    config.markets = markets.split(',').map(s => s.trim()).filter(Boolean);
                    if (config.markets.length === 0) {
                        this.showToast('Please enter at least one market', 'error');
                        return;
                    }
                    break;
                case 'capacity_threshold':
                    const minMw = document.getElementById('config-min-mw').value;
                    const maxMw = document.getElementById('config-max-mw').value;
                    if (!minMw && !maxMw) {
                        this.showToast('Please enter a MW threshold', 'error');
                        return;
                    }
                    if (minMw) config.min_mw = parseInt(minMw);
                    if (maxMw) config.max_mw = parseInt(maxMw);
                    break;
                case 'keyword_watch':
                    const keywords = document.getElementById('config-keywords').value;
                    config.keywords = keywords.split(',').map(s => s.trim()).filter(Boolean);
                    if (config.keywords.length === 0) {
                        this.showToast('Please enter at least one keyword', 'error');
                        return;
                    }
                    break;
            }
            
            try {
                const resp = await fetch(`${API_BASE}/api/v1/simple-alerts`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        email: this.userEmail,
                        name: name,
                        alert_type: alertType,
                        config: config,
                        frequency: frequency
                    })
                });
                
                const data = await resp.json();
                
                if (data.success) {
                    this.showToast(`Alert "${name}" created!`, 'success');
                    this.showMainContent();
                } else {
                    this.showToast(data.error || 'Failed to create alert', 'error');
                }
            } catch (e) {
                this.showToast('Error creating alert', 'error');
                console.error(e);
            }
        },
        
        async deleteAlert(id) {
            if (!confirm('Delete this alert?')) return;
            
            try {
                const resp = await fetch(
                    `${API_BASE}/api/v1/simple-alerts/${id}?email=${encodeURIComponent(this.userEmail)}`,
                    { method: 'DELETE' }
                );
                
                const data = await resp.json();
                
                if (data.success) {
                    this.showToast('Alert deleted', 'success');
                    this.showMainContent();
                } else {
                    this.showToast(data.error || 'Failed to delete', 'error');
                }
            } catch (e) {
                this.showToast('Error deleting alert', 'error');
                console.error(e);
            }
        },
        
        showToast(message, type = 'info') {
            // Remove existing toast
            document.querySelectorAll('.dchub-toast').forEach(t => t.remove());
            
            const toast = document.createElement('div');
            toast.className = `dchub-toast ${type}`;
            toast.textContent = message;
            document.body.appendChild(toast);
            
            setTimeout(() => toast.remove(), 3000);
        }
    };
    
    // ==========================================================================
    // Initialize
    // ==========================================================================
    
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => AlertSystem.init());
    } else {
        AlertSystem.init();
    }
    
    // Export globally
    window.DCHubAlerts = AlertSystem;
    
})();
