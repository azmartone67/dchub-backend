/**
 * ============================================================================
 * DC Hub - Data Center Intelligence Platform
 * Copyright © 2025 DC Hub. All rights reserved.
 * 
 * PROPRIETARY AND CONFIDENTIAL
 * 
 * This software and its documentation are proprietary to DC Hub.
 * Unauthorized copying, modification, distribution, reverse engineering,
 * decompilation, or any other use of this software is strictly prohibited
 * and may result in civil and criminal penalties.
 * 
 * This code is protected by:
 * - U.S. Copyright Law (17 U.S.C.)
 * - International Copyright Treaties
 * - Trade Secret Protection
 * 
 * License: Proprietary - All Rights Reserved
 * Contact: legal@dchub.cloud
 * ============================================================================
 */

(function() {
    'use strict';
    
    // =========================================================================
    // DC HUB PROTECTION SYSTEM
    // =========================================================================
    
    const DCHubProtection = {
        
        // Configuration
        config: {
            enableDevToolsDetection: true,
            enableConsoleWarning: true,
            enableRightClickProtection: false, // Set to true for stricter protection
            enableTextSelectionProtection: false,
            debugMode: false
        },
        
        // Initialize protection
        init: function() {
            if (this.config.enableConsoleWarning) {
                this.consoleWarning();
            }
            if (this.config.enableDevToolsDetection) {
                this.detectDevTools();
            }
            this.addWatermark();
        },
        
        // Console warning message
        consoleWarning: function() {
            // Large warning banner
            console.log('%c⛔ STOP!', 'color: #ef4444; font-size: 60px; font-weight: bold; text-shadow: 2px 2px 0 #000;');
            console.log('%cThis is a protected application.', 'color: #f59e0b; font-size: 20px; font-weight: bold;');
            console.log('%c' + [
                '════════════════════════════════════════════════════════════════════',
                '',
                '  DC Hub - Data Center Intelligence Platform',
                '  Copyright © 2025 DC Hub. All rights reserved.',
                '',
                '  ⚠️  WARNING: UNAUTHORIZED ACCESS PROHIBITED',
                '',
                '  This software is protected by copyright law and international',
                '  treaties. Unauthorized reproduction, reverse engineering, or',
                '  distribution is strictly prohibited and will be prosecuted.',
                '',
                '  Violations may result in:',
                '  • Civil penalties up to $150,000 per infringement',
                '  • Criminal prosecution',
                '  • Immediate account termination',
                '',
                '  If you are seeing this message while trying to copy our code,',
                '  please reconsider. We\'ve invested thousands of hours building',
                '  this platform. Respect intellectual property.',
                '',
                '  Contact: legal@dchub.cloud',
                '',
                '════════════════════════════════════════════════════════════════════'
            ].join('\n'), 'color: #9ca3af; font-family: monospace; font-size: 12px;');
            
            // Watermark in console
            console.log('%cDC Hub v89 | dchub.cloud', 'color: #6366f1; font-size: 10px;');
        },
        
        // Detect developer tools
        detectDevTools: function() {
            const threshold = 160;
            let devToolsOpen = false;
            
            const checkDevTools = function() {
                const widthThreshold = window.outerWidth - window.innerWidth > threshold;
                const heightThreshold = window.outerHeight - window.innerHeight > threshold;
                
                if (widthThreshold || heightThreshold) {
                    if (!devToolsOpen) {
                        devToolsOpen = true;
                        // console.clear(); // Disabled for debugging
                        DCHubProtection.consoleWarning();
                    }
                } else {
                    devToolsOpen = false;
                }
            };
            
            // Check periodically
            setInterval(checkDevTools, 1000);
            
            // Debugger detection disabled for development
            // const detectDebugger = function() {
            //     const start = performance.now();
            //     debugger;
            //     const end = performance.now();
            //     if (end - start > 100) {
            //         console.clear();
            //         DCHubProtection.consoleWarning();
            //     }
            // };
            
            // Run debugger detection occasionally (not too frequently)
            // if (this.config.debugMode === false) {
            //     setInterval(detectDebugger, 5000);
            // }
        },
        
        // Add invisible watermark to page
        addWatermark: function() {
            // Add hidden watermark element
            const watermark = document.createElement('div');
            watermark.id = 'dchub-wm';
            watermark.setAttribute('data-copyright', 'DC Hub © 2024');
            watermark.setAttribute('data-license', 'Proprietary');
            watermark.setAttribute('data-contact', 'legal@dchub.cloud');
            watermark.style.cssText = 'position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);';
            watermark.innerHTML = '<!-- DC Hub © 2024 | dchub.cloud | All Rights Reserved -->';
            
            if (document.body) {
                document.body.appendChild(watermark);
            } else {
                document.addEventListener('DOMContentLoaded', function() {
                    document.body.appendChild(watermark);
                });
            }
            
            // Add meta tags
            const addMeta = function(name, content) {
                const meta = document.createElement('meta');
                meta.name = name;
                meta.content = content;
                document.head.appendChild(meta);
            };
            
            addMeta('copyright', 'DC Hub © 2024');
            addMeta('author', 'DC Hub');
            addMeta('application-name', 'DC Hub');
        },
        
        // Protect against right-click (optional)
        enableRightClickProtection: function() {
            document.addEventListener('contextmenu', function(e) {
                e.preventDefault();
                console.log('%c⚠️ Right-click is disabled on this page.', 'color: #f59e0b;');
                return false;
            });
        },
        
        // Protect text selection (optional)
        enableTextSelectionProtection: function() {
            document.addEventListener('selectstart', function(e) {
                e.preventDefault();
                return false;
            });
            document.body.style.userSelect = 'none';
            document.body.style.webkitUserSelect = 'none';
        },
        
        // Log suspicious activity
        logSuspiciousActivity: function(activity) {
            // In production, this could send to a logging endpoint
            console.warn('[DC Hub Security]', activity);
            
            // Could integrate with analytics
            if (typeof gtag !== 'undefined') {
                gtag('event', 'security_alert', {
                    'event_category': 'Security',
                    'event_label': activity
                });
            }
        },
        
        // Verify integrity (basic)
        verifyIntegrity: function() {
            // Check if critical functions exist
            const criticalFunctions = [
                'DCHubChat',
                'initDCHubAnalytics'
            ];
            
            criticalFunctions.forEach(function(fn) {
                if (typeof window[fn] === 'undefined') {
                    DCHubProtection.logSuspiciousActivity('Missing critical function: ' + fn);
                }
            });
        }
    };
    
    // =========================================================================
    // RATE LIMITING FOR API/DATA ACCESS
    // =========================================================================
    
    const DCHubRateLimiter = {
        limits: {
            search: { max: 100, window: 60000 }, // 100 searches per minute
            export: { max: 10, window: 3600000 }, // 10 exports per hour
            api: { max: 1000, window: 86400000 }  // 1000 API calls per day
        },
        
        counts: {},
        
        check: function(action) {
            const now = Date.now();
            const limit = this.limits[action];
            
            if (!limit) return true;
            
            if (!this.counts[action]) {
                this.counts[action] = { count: 0, resetTime: now + limit.window };
            }
            
            if (now > this.counts[action].resetTime) {
                this.counts[action] = { count: 0, resetTime: now + limit.window };
            }
            
            if (this.counts[action].count >= limit.max) {
                DCHubProtection.logSuspiciousActivity('Rate limit exceeded: ' + action);
                return false;
            }
            
            this.counts[action].count++;
            return true;
        }
    };
    
    // =========================================================================
    // INITIALIZATION
    // =========================================================================
    
    // =========================================================================
    // VISITOR TRACKING
    // =========================================================================
    
    const DCHubVisitorTracking = {
        apiBase: 'https://dchub.cloud',
        
        init: function() {
            // Generate or retrieve session ID
            if (!localStorage.getItem('dchub_session_id')) {
                localStorage.setItem('dchub_session_id', this.generateSessionId());
            }
            
            // Track this page visit
            this.trackVisit();
        },
        
        generateSessionId: function() {
            return 'sess_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
        },
        
        trackVisit: function() {
            const sessionId = localStorage.getItem('dchub_session_id');
            
            fetch(this.apiBase + '/api/track/visit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    page: window.location.pathname,
                    referrer: document.referrer,
                    session_id: sessionId
                })
            }).catch(function() {
                // Silently fail - don't break the site if tracking fails
            });
        }
    };
    
    // =========================================================================
    // INITIALIZATION
    // =========================================================================

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            DCHubProtection.init();
            DCHubVisitorTracking.init();
        });
    } else {
        DCHubProtection.init();
        DCHubVisitorTracking.init();
    }
    
    // Expose rate limiter for use in other scripts
    window.DCHubRateLimiter = DCHubRateLimiter;
    window.DCHubVisitorTracking = DCHubVisitorTracking;
    
})();

// ============================================================================
// Additional obfuscation note for build process:
// 
// For production deployment, run this file through:
// 1. JavaScript Obfuscator: https://obfuscator.io/
//    Settings: High obfuscation, self-defending, debug protection
// 
// 2. Terser/UglifyJS for minification
// 
// 3. Integrity hash (SRI) for script tags
// ============================================================================
