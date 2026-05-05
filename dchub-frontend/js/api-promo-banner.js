/**
 * DC Hub - API Promotion Banner
 * Adds signup CTA to pages to drive API adoption
 * 
 * Add to any page:
 * <script src="js/api-promo-banner.js?v=1"></script>
 */

(function() {
    'use strict';

    const CONFIG = {
        showAfterSeconds: 30,      // Show banner after 30 seconds
        showOnScroll: true,        // Or show after scrolling
        scrollThreshold: 0.3,      // 30% down the page
        dismissDuration: 7 * 24 * 60 * 60 * 1000,  // 7 days
        storageKey: 'dchub_api_promo_dismissed'
    };

    // Check if already dismissed
    function isDismissed() {
        const dismissed = localStorage.getItem(CONFIG.storageKey);
        if (!dismissed) return false;
        return Date.now() < parseInt(dismissed);
    }

    // Dismiss banner
    function dismiss() {
        localStorage.setItem(CONFIG.storageKey, Date.now() + CONFIG.dismissDuration);
        const banner = document.getElementById('dchub-api-promo');
        if (banner) {
            banner.style.transform = 'translateY(100%)';
            setTimeout(() => banner.remove(), 300);
        }
    }

    // Create and show banner
    function showBanner() {
        if (isDismissed()) return;
        if (document.getElementById('dchub-api-promo')) return;

        const banner = document.createElement('div');
        banner.id = 'dchub-api-promo';
        banner.innerHTML = `
            <style>
                #dchub-api-promo {
                    position: fixed;
                    bottom: 0;
                    left: 0;
                    right: 0;
                    background: linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #4c1d95 100%);
                    border-top: 1px solid #6366f1;
                    padding: 16px 24px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    gap: 24px;
                    z-index: 9999;
                    transform: translateY(100%);
                    transition: transform 0.3s ease;
                    box-shadow: 0 -4px 20px rgba(99, 102, 241, 0.3);
                }
                #dchub-api-promo.show {
                    transform: translateY(0);
                }
                #dchub-api-promo .promo-icon {
                    font-size: 32px;
                    animation: bounce 2s infinite;
                }
                @keyframes bounce {
                    0%, 100% { transform: translateY(0); }
                    50% { transform: translateY(-5px); }
                }
                #dchub-api-promo .promo-text {
                    flex: 1;
                    max-width: 600px;
                }
                #dchub-api-promo .promo-title {
                    font-size: 16px;
                    font-weight: 700;
                    color: #fff;
                    margin-bottom: 4px;
                }
                #dchub-api-promo .promo-subtitle {
                    font-size: 13px;
                    color: #c7d2fe;
                }
                #dchub-api-promo .promo-stats {
                    display: flex;
                    gap: 16px;
                    font-size: 11px;
                    color: #a5b4fc;
                    margin-top: 6px;
                }
                #dchub-api-promo .promo-stat {
                    display: flex;
                    align-items: center;
                    gap: 4px;
                }
                #dchub-api-promo .promo-btn {
                    padding: 12px 28px;
                    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                    border: none;
                    border-radius: 8px;
                    color: #fff;
                    font-size: 14px;
                    font-weight: 700;
                    cursor: pointer;
                    transition: all 0.2s;
                    text-decoration: none;
                    white-space: nowrap;
                }
                #dchub-api-promo .promo-btn:hover {
                    transform: scale(1.05);
                    box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4);
                }
                #dchub-api-promo .promo-close {
                    position: absolute;
                    top: 8px;
                    right: 12px;
                    background: none;
                    border: none;
                    color: #9ca3af;
                    font-size: 20px;
                    cursor: pointer;
                    padding: 4px 8px;
                    border-radius: 4px;
                    transition: all 0.2s;
                }
                #dchub-api-promo .promo-close:hover {
                    background: rgba(255,255,255,0.1);
                    color: #fff;
                }
                @media (max-width: 768px) {
                    #dchub-api-promo {
                        flex-direction: column;
                        text-align: center;
                        padding: 20px 16px 16px;
                        gap: 12px;
                    }
                    #dchub-api-promo .promo-stats {
                        justify-content: center;
                    }
                }
            </style>
            <button class="promo-close" onclick="window.DCHubPromo.dismiss()" title="Dismiss">×</button>
            <div class="promo-icon">🚀</div>
            <div class="promo-text">
                <div class="promo-title">Build with DC Hub API — Free Tier Available</div>
                <div class="promo-subtitle">Access 50,000+ data center facilities, real-time grid data, and site scoring via REST API</div>
                <div class="promo-stats">
                    <span class="promo-stat">✓ 100 requests/day free</span>
                    <span class="promo-stat">✓ Instant API key</span>
                    <span class="promo-stat">✓ No credit card</span>
                </div>
            </div>
            <a href="/signup" class="promo-btn">Get Free API Key →</a>
        `;

        document.body.appendChild(banner);

        // Trigger animation
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                banner.classList.add('show');
            });
        });
    }

    // Initialize
    function init() {
        if (isDismissed()) return;

        // Show after delay
        setTimeout(showBanner, CONFIG.showAfterSeconds * 1000);

        // Or show on scroll
        if (CONFIG.showOnScroll) {
            let shown = false;
            window.addEventListener('scroll', () => {
                if (shown) return;
                const scrollPercent = window.scrollY / (document.body.scrollHeight - window.innerHeight);
                if (scrollPercent > CONFIG.scrollThreshold) {
                    shown = true;
                    showBanner();
                }
            });
        }
    }

    // Expose API
    window.DCHubPromo = {
        show: showBanner,
        dismiss: dismiss,
        reset: () => localStorage.removeItem(CONFIG.storageKey)
    };

    // Auto-init
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
