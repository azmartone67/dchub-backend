/**
 * DC Hub Site Scoring Integration
 * Frontend module for site scoring and analysis
 */

const SiteScoring = {
    baseUrl: '',

    _getAuthHeaders() {
        try {
            const session = JSON.parse(localStorage.getItem('dchub_session') || '{}');
            const key = session.api_key || session.apiKey || '';
            if (key) return { 'X-API-Key': key };
        } catch(e) {}
        return {};
    },

    async scoreSite(lat, lng, options = {}) {
        const params = new URLSearchParams({
            lat: lat,
            lng: lng,
            radius: options.radius || 50,
            ...options
        });
        
        try {
            const response = await fetch(`${this.baseUrl}/api/site-score?${params}`, {
                headers: this._getAuthHeaders()
            });
            const data = await response.json();
            return data;
        } catch (error) {
            console.error('Site scoring error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async getEnergyPrices(state) {
        try {
            const response = await fetch(`${this.baseUrl}/api/energy/prices/${state}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Energy prices error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async getCarbonIntensity(lat, lng) {
        const params = new URLSearchParams({ lat, lng });
        try {
            const response = await fetch(`${this.baseUrl}/api/carbon/intensity?${params}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Carbon intensity error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async getSolarPotential(lat, lng) {
        const params = new URLSearchParams({ lat, lng });
        try {
            const response = await fetch(`${this.baseUrl}/api/renewable/solar?${params}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Solar potential error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async getWindPotential(lat, lng) {
        const params = new URLSearchParams({ lat, lng });
        try {
            const response = await fetch(`${this.baseUrl}/api/renewable/wind?${params}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Wind potential error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async getRenewableCombined(lat, lng) {
        const params = new URLSearchParams({ lat, lng });
        try {
            const response = await fetch(`${this.baseUrl}/api/renewable/combined?${params}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Renewable combined error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async getNearbyInfrastructure(lat, lng, radius = 50) {
        const params = new URLSearchParams({ lat, lng, radius });
        try {
            const response = await fetch(`${this.baseUrl}/api/v1/infrastructure/nearby?${params}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Infrastructure error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async getGasPipelines(state, options = {}) {
        const params = new URLSearchParams({
            state: state,
            limit: options.limit || 100,
            ...options
        });
        try {
            const response = await fetch(`${this.baseUrl}/api/v1/gas-pipelines?${params}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Gas pipelines error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async getSubstations(state, options = {}) {
        const params = new URLSearchParams({
            state: state,
            limit: options.limit || 100,
            ...options
        });
        try {
            const response = await fetch(`${this.baseUrl}/api/v1/energy/substations?${params}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Substations error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async compareSites(sites) {
        try {
            const response = await fetch(`${this.baseUrl}/api/v1/energy/compare-sites`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...this._getAuthHeaders() },
                body: JSON.stringify({ sites })
            });
            return await response.json();
        } catch (error) {
            console.error('Site comparison error:', error);
            return { success: false, error: error.message };
        }
    },
    
    async fullSiteAnalysis(lat, lng, options = {}) {
        const params = new URLSearchParams({
            lat: lat,
            lng: lng,
            radius: options.radius || 50
        });
        try {
            const response = await fetch(`${this.baseUrl}/api/v1/energy/site-analysis?${params}`, {
                headers: this._getAuthHeaders()
            });
            return await response.json();
        } catch (error) {
            console.error('Site analysis error:', error);
            return { success: false, error: error.message };
        }
    },
    
    formatScore(score) {
        if (score >= 80) return { label: 'Excellent', color: '#22c55e', icon: '🟢' };
        if (score >= 60) return { label: 'Good', color: '#84cc16', icon: '🟡' };
        if (score >= 40) return { label: 'Fair', color: '#eab308', icon: '🟠' };
        return { label: 'Poor', color: '#ef4444', icon: '🔴' };
    },
    
    createScoreCard(data) {
        const scoreInfo = this.formatScore(data.overall_score || 0);
        return `
            <div class="score-card" style="border-left: 4px solid ${scoreInfo.color}">
                <div class="score-header">
                    <span class="score-icon">${scoreInfo.icon}</span>
                    <span class="score-value">${data.overall_score || 0}</span>
                    <span class="score-label">${scoreInfo.label}</span>
                </div>
                <div class="score-details">
                    <div class="score-item">Power: ${data.power_score || 'N/A'}</div>
                    <div class="score-item">Fiber: ${data.fiber_score || 'N/A'}</div>
                    <div class="score-item">Water: ${data.water_score || 'N/A'}</div>
                    <div class="score-item">Risk: ${data.risk_score || 'N/A'}</div>
                </div>
            </div>
        `;
    }
};

if (typeof module !== 'undefined' && module.exports) {
    module.exports = SiteScoring;
}
