/**
 * DC Hub Land & Power Map - Usage Limiter (Frontend)
 * ===================================================
 * Checks user's daily usage and shows upgrade modal when limit reached.
 * 
 * Add this to land-power.html or land-power-app.js
 */

const DCHubUsageLimiter = {
    // Configuration
    FREE_DAILY_LIMIT: 1,
    UPGRADE_URL: 'https://dchub.cloud/pricing',
    
    // State
    usageChecked: false,
    currentUsage: 0,
    
    /**
     * Check current usage from backend
     */
    async checkUsage() {
        try {
            const response = await fetch('/api/land-power/usage');
            const data = await response.json();
            
            if (data.success) {
                this.currentUsage = data.data.usage.used;
                this.usageChecked = true;
                return data.data;
            }
        } catch (e) {
            console.warn('Usage check failed:', e);
        }
        return null;
    },
    
    /**
     * Check if user can perform analysis
     */
    canAnalyze() {
        return this.currentUsage < this.FREE_DAILY_LIMIT;
    },
    
    /**
     * Show upgrade modal when limit reached
     */
    showUpgradeModal() {
        // Remove existing modal if any
        const existing = document.getElementById('dchub-upgrade-modal');
        if (existing) existing.remove();
        
        const modal = document.createElement('div');
        modal.id = 'dchub-upgrade-modal';
        modal.innerHTML = `
            <div style="
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0,0,0,0.8);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 10000;
            ">
                <div style="
                    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                    border-radius: 16px;
                    padding: 40px;
                    max-width: 500px;
                    width: 90%;
                    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
                    border: 1px solid rgba(99, 102, 241, 0.3);
                    text-align: center;
                ">
                    <div style="font-size: 48px; margin-bottom: 20px;">🔒</div>
                    
                    <h2 style="
                        color: #fff;
                        font-size: 28px;
                        margin-bottom: 16px;
                        font-weight: 700;
                    ">Daily Limit Reached</h2>
                    
                    <p style="
                        color: #a0aec0;
                        font-size: 16px;
                        line-height: 1.6;
                        margin-bottom: 24px;
                    ">
                        You've used your <strong style="color: #6366f1;">free daily analysis</strong>. 
                        Upgrade to DC Hub Pro for unlimited site analyses and premium features!
                    </p>
                    
                    <div style="
                        background: rgba(99, 102, 241, 0.1);
                        border-radius: 12px;
                        padding: 20px;
                        margin-bottom: 24px;
                        text-align: left;
                    ">
                        <div style="color: #6366f1; font-weight: 600; margin-bottom: 12px;">
                            ✨ DC Hub Pro includes:
                        </div>
                        <ul style="color: #e2e8f0; margin: 0; padding-left: 20px; line-height: 1.8;">
                            <li>Unlimited site analyses</li>
                            <li>Real-time power pricing data</li>
                            <li>PDF report exports</li>
                            <li>API access for integrations</li>
                            <li>Priority support</li>
                        </ul>
                    </div>
                    
                    <div style="display: flex; gap: 12px; justify-content: center;">
                        <a href="${this.UPGRADE_URL}" style="
                            background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
                            color: white;
                            padding: 14px 32px;
                            border-radius: 8px;
                            font-weight: 600;
                            font-size: 16px;
                            text-decoration: none;
                            display: inline-block;
                            transition: transform 0.2s;
                        " onmouseover="this.style.transform='scale(1.05)'" 
                           onmouseout="this.style.transform='scale(1)'">
                            🚀 Upgrade Now
                        </a>
                        
                        <button onclick="document.getElementById('dchub-upgrade-modal').remove()" style="
                            background: transparent;
                            color: #a0aec0;
                            padding: 14px 24px;
                            border-radius: 8px;
                            font-weight: 500;
                            font-size: 14px;
                            border: 1px solid #4a5568;
                            cursor: pointer;
                        ">
                            Maybe Later
                        </button>
                    </div>
                    
                    <p style="
                        color: #718096;
                        font-size: 12px;
                        margin-top: 20px;
                    ">
                        Your free analysis resets at midnight UTC
                    </p>
                </div>
            </div>
        `;
        
        document.body.appendChild(modal);
        
        // Close on background click
        modal.querySelector('div').addEventListener('click', (e) => {
            if (e.target === modal.querySelector('div')) {
                modal.remove();
            }
        });
    },
    
    /**
     * Wrap site analysis function to check limits
     */
    wrapAnalysisFunction(originalFunction) {
        const self = this;
        return async function(...args) {
            // Check usage first
            if (!self.usageChecked) {
                await self.checkUsage();
            }
            
            if (!self.canAnalyze()) {
                self.showUpgradeModal();
                return null;
            }
            
            // Call original function
            const result = await originalFunction.apply(this, args);
            
            // Increment local counter
            self.currentUsage++;
            
            // Check if this was their last free analysis
            if (!self.canAnalyze()) {
                // Show a toast notification
                self.showLastAnalysisToast();
            }
            
            return result;
        };
    },
    
    /**
     * Show toast when user has used their last free analysis
     */
    showLastAnalysisToast() {
        const toast = document.createElement('div');
        toast.innerHTML = `
            <div style="
                position: fixed;
                bottom: 20px;
                right: 20px;
                background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
                color: white;
                padding: 16px 24px;
                border-radius: 12px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.3);
                z-index: 9999;
                display: flex;
                align-items: center;
                gap: 12px;
                animation: slideIn 0.3s ease;
            ">
                <span style="font-size: 24px;">⚡</span>
                <div>
                    <div style="font-weight: 600;">That was your free analysis!</div>
                    <div style="font-size: 13px; opacity: 0.9;">
                        <a href="${this.UPGRADE_URL}" style="color: white; text-decoration: underline;">
                            Upgrade to Pro
                        </a> for unlimited access
                    </div>
                </div>
                <button onclick="this.parentElement.parentElement.remove()" style="
                    background: transparent;
                    border: none;
                    color: white;
                    font-size: 20px;
                    cursor: pointer;
                    padding: 0 0 0 12px;
                ">×</button>
            </div>
        `;
        
        document.body.appendChild(toast);
        
        // Auto-remove after 8 seconds
        setTimeout(() => {
            if (toast.parentElement) {
                toast.remove();
            }
        }, 8000);
    },
    
    /**
     * Initialize the usage limiter
     */
    async init() {
        console.log('🔒 DC Hub Usage Limiter initializing...');
        
        // Check usage on load
        const usage = await this.checkUsage();
        
        if (usage) {
            console.log(`📊 Usage: ${usage.usage.used}/${usage.usage.limit} analyses today`);
            
            if (usage.usage.remaining === 0 && !usage.is_premium) {
                console.log('⚠️ Daily limit reached - upgrade prompts active');
            }
        }
        
        // Add CSS animation
        const style = document.createElement('style');
        style.textContent = `
            @keyframes slideIn {
                from { transform: translateX(100%); opacity: 0; }
                to { transform: translateX(0); opacity: 1; }
            }
        `;
        document.head.appendChild(style);
        
        console.log('🔒 DC Hub Usage Limiter ready');
    }
};

// Auto-initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => DCHubUsageLimiter.init());
} else {
    DCHubUsageLimiter.init();
}

// Export for use in other scripts
window.DCHubUsageLimiter = DCHubUsageLimiter;
