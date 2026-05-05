/**
 * DC Hub Alerts - Simple Alerts UI Component
 * Adds a bell icon for managing alerts via /api/v1/simple-alerts
 */

(function() {
    'use strict';

    const API_BASE = '/api/v1/simple-alerts';
    
    const AlertsUI = {
        isOpen: false,
        alerts: [],
        userEmail: localStorage.getItem('dchub_email') || '',

        init() {
            this.injectStyles();
            this.createBellIcon();
            this.createModal();
            this.loadAlerts();
        },

        injectStyles() {
            const style = document.createElement('style');
            style.textContent = `
                .alerts-bell {
                    position: fixed;
                    top: 20px;
                    right: 80px;
                    width: 44px;
                    height: 44px;
                    background: linear-gradient(135deg, #3b82f6, #1d4ed8);
                    border-radius: 50%;
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    box-shadow: 0 4px 15px rgba(59, 130, 246, 0.4);
                    z-index: 9998;
                    transition: transform 0.2s, box-shadow 0.2s;
                }
                .alerts-bell:hover {
                    transform: scale(1.1);
                    box-shadow: 0 6px 20px rgba(59, 130, 246, 0.5);
                }
                .alerts-bell svg {
                    width: 24px;
                    height: 24px;
                    fill: white;
                }
                .alerts-bell .badge {
                    position: absolute;
                    top: -4px;
                    right: -4px;
                    background: #ef4444;
                    color: white;
                    font-size: 11px;
                    font-weight: bold;
                    padding: 2px 6px;
                    border-radius: 10px;
                    min-width: 18px;
                    text-align: center;
                }
                .alerts-modal {
                    display: none;
                    position: fixed;
                    top: 0;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    background: rgba(0,0,0,0.6);
                    z-index: 9999;
                    align-items: center;
                    justify-content: center;
                }
                .alerts-modal.open {
                    display: flex;
                }
                .alerts-content {
                    background: #1a1a2e;
                    border-radius: 16px;
                    width: 90%;
                    max-width: 500px;
                    max-height: 80vh;
                    overflow: hidden;
                    box-shadow: 0 25px 50px rgba(0,0,0,0.5);
                }
                .alerts-header {
                    padding: 20px;
                    background: linear-gradient(135deg, #3b82f6, #1d4ed8);
                    color: white;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }
                .alerts-header h2 {
                    margin: 0;
                    font-size: 18px;
                }
                .alerts-close {
                    background: none;
                    border: none;
                    color: white;
                    font-size: 24px;
                    cursor: pointer;
                    padding: 0;
                    line-height: 1;
                }
                .alerts-body {
                    padding: 20px;
                    overflow-y: auto;
                    max-height: 60vh;
                }
                .alert-item {
                    background: #252540;
                    border-radius: 10px;
                    padding: 15px;
                    margin-bottom: 10px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }
                .alert-info h4 {
                    margin: 0 0 5px 0;
                    color: #fff;
                    font-size: 14px;
                }
                .alert-info p {
                    margin: 0;
                    color: #888;
                    font-size: 12px;
                }
                .alert-actions {
                    display: flex;
                    gap: 8px;
                }
                .alert-actions button {
                    padding: 6px 12px;
                    border: none;
                    border-radius: 6px;
                    cursor: pointer;
                    font-size: 12px;
                }
                .btn-toggle {
                    background: #10b981;
                    color: white;
                }
                .btn-toggle.inactive {
                    background: #6b7280;
                }
                .btn-delete {
                    background: #ef4444;
                    color: white;
                }
                .alerts-form {
                    background: #252540;
                    border-radius: 10px;
                    padding: 15px;
                    margin-top: 15px;
                }
                .alerts-form h4 {
                    margin: 0 0 15px 0;
                    color: #fff;
                    font-size: 14px;
                }
                .form-group {
                    margin-bottom: 12px;
                }
                .form-group label {
                    display: block;
                    color: #888;
                    font-size: 12px;
                    margin-bottom: 5px;
                }
                .form-group input, .form-group select {
                    width: 100%;
                    padding: 10px;
                    border: 1px solid #333;
                    border-radius: 6px;
                    background: #1a1a2e;
                    color: white;
                    font-size: 14px;
                    box-sizing: border-box;
                }
                .btn-create {
                    width: 100%;
                    padding: 12px;
                    background: linear-gradient(135deg, #3b82f6, #1d4ed8);
                    color: white;
                    border: none;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 600;
                    cursor: pointer;
                    transition: opacity 0.2s;
                }
                .btn-create:hover {
                    opacity: 0.9;
                }
                .no-alerts {
                    text-align: center;
                    color: #888;
                    padding: 20px;
                }
            `;
            document.head.appendChild(style);
        },

        createBellIcon() {
            const bell = document.createElement('div');
            bell.className = 'alerts-bell';
            bell.innerHTML = `
                <svg viewBox="0 0 24 24">
                    <path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2zm-2 1H8v-6c0-2.48 1.51-4.5 4-4.5s4 2.02 4 4.5v6z"/>
                </svg>
                <span class="badge" style="display:none;">0</span>
            `;
            bell.onclick = () => this.toggleModal();
            document.body.appendChild(bell);
            this.bellEl = bell;
        },

        createModal() {
            const modal = document.createElement('div');
            modal.className = 'alerts-modal';
            modal.innerHTML = `
                <div class="alerts-content">
                    <div class="alerts-header">
                        <h2>My Alerts</h2>
                        <button class="alerts-close">&times;</button>
                    </div>
                    <div class="alerts-body">
                        <div class="alerts-list"></div>
                        <div class="alerts-form">
                            <h4>Create New Alert</h4>
                            <div class="form-group">
                                <label>Email</label>
                                <input type="email" id="alert-email" placeholder="your@email.com" value="${this.userEmail}">
                            </div>
                            <div class="form-group">
                                <label>Alert Type</label>
                                <select id="alert-type">
                                    <option value="operator_watch">Operator Watch</option>
                                    <option value="market_watch">Market Watch</option>
                                    <option value="deal_alert">Deal Alert</option>
                                    <option value="capacity_alert">Capacity Alert</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Alert Name</label>
                                <input type="text" id="alert-name" placeholder="e.g., Google News">
                            </div>
                            <div class="form-group">
                                <label>Keywords (comma-separated)</label>
                                <input type="text" id="alert-keywords" placeholder="e.g., Google, Microsoft, AWS">
                            </div>
                            <div class="form-group">
                                <label>Frequency</label>
                                <select id="alert-frequency">
                                    <option value="immediate">Immediate</option>
                                    <option value="daily">Daily Digest</option>
                                    <option value="weekly">Weekly Summary</option>
                                </select>
                            </div>
                            <button class="btn-create" onclick="AlertsUI.createAlert()">Create Alert</button>
                        </div>
                    </div>
                </div>
            `;
            modal.querySelector('.alerts-close').onclick = () => this.toggleModal();
            modal.onclick = (e) => {
                if (e.target === modal) this.toggleModal();
            };
            document.body.appendChild(modal);
            this.modalEl = modal;
            this.listEl = modal.querySelector('.alerts-list');
        },

        toggleModal() {
            this.isOpen = !this.isOpen;
            this.modalEl.classList.toggle('open', this.isOpen);
            if (this.isOpen) this.loadAlerts();
        },

        async loadAlerts() {
            const email = document.getElementById('alert-email')?.value || this.userEmail;
            if (!email) {
                this.renderAlerts([]);
                return;
            }
            
            try {
                const res = await fetch(`${API_BASE}?email=${encodeURIComponent(email)}`);
                const data = await res.json();
                this.alerts = data.alerts || [];
                this.renderAlerts(this.alerts);
                this.updateBadge();
            } catch (err) {
                console.error('Failed to load alerts:', err);
            }
        },

        renderAlerts(alerts) {
            if (!alerts.length) {
                this.listEl.innerHTML = '<div class="no-alerts">No alerts yet. Create one below!</div>';
                return;
            }

            this.listEl.innerHTML = alerts.map(a => `
                <div class="alert-item" data-id="${a.id}">
                    <div class="alert-info">
                        <h4>${a.name || a.alert_type}</h4>
                        <p>${a.alert_type} - ${a.frequency}</p>
                    </div>
                    <div class="alert-actions">
                        <button class="btn-toggle ${a.is_active ? '' : 'inactive'}" onclick="AlertsUI.toggleAlert(${a.id}, ${a.is_active})">
                            ${a.is_active ? 'Active' : 'Paused'}
                        </button>
                        <button class="btn-delete" onclick="AlertsUI.deleteAlert(${a.id})">Delete</button>
                    </div>
                </div>
            `).join('');
        },

        updateBadge() {
            const badge = this.bellEl.querySelector('.badge');
            const activeCount = this.alerts.filter(a => a.is_active).length;
            if (activeCount > 0) {
                badge.textContent = activeCount;
                badge.style.display = 'block';
            } else {
                badge.style.display = 'none';
            }
        },

        async createAlert() {
            const email = document.getElementById('alert-email').value;
            const alertType = document.getElementById('alert-type').value;
            const name = document.getElementById('alert-name').value;
            const keywords = document.getElementById('alert-keywords').value;
            const frequency = document.getElementById('alert-frequency').value;

            if (!email) {
                alert('Please enter your email');
                return;
            }

            localStorage.setItem('dchub_email', email);
            this.userEmail = email;

            const config = {};
            if (alertType === 'operator_watch') {
                config.operators = keywords.split(',').map(k => k.trim()).filter(k => k);
            } else if (alertType === 'market_watch') {
                config.markets = keywords.split(',').map(k => k.trim()).filter(k => k);
            } else {
                config.keywords = keywords.split(',').map(k => k.trim()).filter(k => k);
            }

            try {
                const res = await fetch(API_BASE, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        email,
                        alert_type: alertType,
                        name: name || alertType,
                        config,
                        frequency
                    })
                });
                const data = await res.json();
                if (data.success) {
                    document.getElementById('alert-name').value = '';
                    document.getElementById('alert-keywords').value = '';
                    this.loadAlerts();
                } else {
                    alert('Failed to create alert: ' + (data.error || 'Unknown error'));
                }
            } catch (err) {
                alert('Error creating alert');
                console.error(err);
            }
        },

        async toggleAlert(id, currentState) {
            try {
                const res = await fetch(`${API_BASE}/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ is_active: !currentState })
                });
                if (res.ok) this.loadAlerts();
            } catch (err) {
                console.error('Failed to toggle alert:', err);
            }
        },

        async deleteAlert(id) {
            if (!confirm('Delete this alert?')) return;
            try {
                const res = await fetch(`${API_BASE}/${id}`, { method: 'DELETE' });
                if (res.ok) this.loadAlerts();
            } catch (err) {
                console.error('Failed to delete alert:', err);
            }
        }
    };

    window.AlertsUI = AlertsUI;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => AlertsUI.init());
    } else {
        AlertsUI.init();
    }
})();
