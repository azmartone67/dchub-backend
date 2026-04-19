/**
 * DC Hub Unified Auth Module v2
 * ==============================
 * Single source of truth for authentication state.
 * 
 * CANONICAL KEYS (the only ones that matter):
 *   dchub_token  — JWT token from /api/auth/login or /api/auth/google
 *   dchub_user   — JSON user object { id, email, name, plan, ... }
 *   dchub_session — Compat key for dchub-access-gate.js and Land & Power
 *
 * LEGACY KEYS (migrated on first load, then deleted):
 *   dc_hub_token, dchub-token, dc_hub_user, dchub-user
 *
 * USAGE:
 *   Include this script BEFORE any other DC Hub JS:
 *   <script src="/js/dchub-auth-v2.js"><\/script>
 *
 *   Then use:
 *     DCHUB_AUTH.getToken()       → string | null
 *     DCHUB_AUTH.getUser()        → object | null
 *     DCHUB_AUTH.setUser(obj)     → saves + syncs session key
 *     DCHUB_AUTH.isLoggedIn()     → boolean
 *     DCHUB_AUTH.getPlan()        → 'free' | 'pro' | 'founding' | 'enterprise' | 'developer'
 *     DCHUB_AUTH.logout()         → clears all keys, redirects to /login.html
 *     DCHUB_AUTH.requireAuth()    → redirects to login if not authenticated
 */

(function() {
    'use strict';

    const TOKEN_KEY   = 'dchub_token';
    const USER_KEY    = 'dchub_user';
    const SESSION_KEY = 'dchub_session';

    // Legacy keys to migrate from
    const LEGACY_TOKEN_KEYS = ['dc_hub_token', 'dchub-token'];
    const LEGACY_USER_KEYS  = ['dc_hub_user', 'dchub-user'];

    // ---- Migration (runs once per page load, fast) ----
    function migrateLegacy() {
        for (const k of LEGACY_TOKEN_KEYS) {
            const v = localStorage.getItem(k);
            if (v && !localStorage.getItem(TOKEN_KEY)) {
                localStorage.setItem(TOKEN_KEY, v);
            }
            localStorage.removeItem(k);
        }
        for (const k of LEGACY_USER_KEYS) {
            const v = localStorage.getItem(k);
            if (v && !localStorage.getItem(USER_KEY)) {
                localStorage.setItem(USER_KEY, v);
            }
            localStorage.removeItem(k);
        }
    }

    // ---- Session sync (for access gate compat) ----
    function syncSession(user) {
        const token = localStorage.getItem(TOKEN_KEY);
        if (!user || !token) return;
        localStorage.setItem(SESSION_KEY, JSON.stringify({
            token: token,
            plan: user.plan || 'free',
            tier: user.plan || 'free',
            email: user.email || '',
            name: user.name || (user.email ? user.email.split('@')[0] : '')
        }));
    }

    // ---- Public API ----
    window.DCHUB_AUTH = {
        TOKEN_KEY: TOKEN_KEY,
        USER_KEY: USER_KEY,
        SESSION_KEY: SESSION_KEY,

        init() { migrateLegacy(); },

        getToken() {
            return localStorage.getItem(TOKEN_KEY);
        },

        getUser() {
            try {
                return JSON.parse(localStorage.getItem(USER_KEY) || 'null');
            } catch (e) {
                return null;
            }
        },

        setUser(user) {
            localStorage.setItem(USER_KEY, JSON.stringify(user));
            syncSession(user);
        },

        setToken(token) {
            localStorage.setItem(TOKEN_KEY, token);
        },

        isLoggedIn() {
            return !!localStorage.getItem(TOKEN_KEY);
        },

        getPlan() {
            const user = this.getUser();
            return (user && user.plan) ? user.plan.toLowerCase() : 'free';
        },

        syncSession: syncSession,

        logout() {
            localStorage.removeItem(TOKEN_KEY);
            localStorage.removeItem(USER_KEY);
            localStorage.removeItem(SESSION_KEY);
            localStorage.removeItem('dchub_dashboard_data');
            localStorage.removeItem('dchub_user_id');
            localStorage.removeItem('dchub_page_views');
            window.location.href = '/login.html?logged_out=true';
        },

        /**
         * Call at top of any authenticated page.
         * Redirects to login if no token found.
         * @param {string} [redirectPath] - Where to send user after login
         */
        requireAuth(redirectPath) {
            if (!this.isLoggedIn()) {
                const path = redirectPath || window.location.pathname;
                window.location.href = '/login.html?redirect=' + encodeURIComponent(path) + '&reason=auth_required';
                return false;
            }
            return true;
        },

        /**
         * Verify token with backend and update cached user.
         * Falls back to cached data on network error.
         * @returns {Promise<object|null>} user object or null
         */
        async verify() {
            const token = this.getToken();
            if (!token) return null;

            const apiBase = window.DCHUB_API_BASE || '';

            try {
                const res = await fetch(apiBase + '/api/auth/me', {
                    headers: { 'Authorization': 'Bearer ' + token }
                });

                if (res.ok) {
                    const data = await res.json();
                    if (data.success && data.user) {
                        this.setUser(data.user);
                        return data.user;
                    }
                }

                // Backend returned error — try cached user
                const cached = this.getUser();
                if (cached && cached.email) {
                    console.warn('[DCHUB_AUTH] Backend auth failed, using cached user');
                    syncSession(cached);
                    return cached;
                }

                // No cached data — force logout
                this.logout();
                return null;

            } catch (e) {
                // Network error — try cached
                const cached = this.getUser();
                if (cached && cached.email) {
                    console.warn('[DCHUB_AUTH] Network error, using cached user');
                    syncSession(cached);
                    return cached;
                }
                return null;
            }
        }
    };

    // Run migration on load
    migrateLegacy();

    // ---- Handle Google OAuth callback ----
    // After Google OAuth, backend redirects to /?token=xxx&email=yyy&plan=zzz
    // This picks up the token from the URL and writes it to localStorage
    (function handleOAuthCallback() {
        var params = new URLSearchParams(window.location.search);
        var hashParams = new URLSearchParams(window.location.hash.replace('#', ''));

        var token = params.get('token') || params.get('jwt') ||
                    hashParams.get('token') || hashParams.get('jwt');

        if (token) {
            localStorage.setItem(TOKEN_KEY, token);

            var user = {
                email: params.get('email') || hashParams.get('email') || '',
                name: params.get('name') || hashParams.get('name') || '',
                plan: params.get('plan') || hashParams.get('plan') || 'free'
            };

            localStorage.setItem(USER_KEY, JSON.stringify(user));
            syncSession(user);

            // Clean URL
            window.history.replaceState({}, '', window.location.pathname);

            // Fire event
            window.dispatchEvent(new CustomEvent('dchub:login', { detail: user }));

            // Handle redirect
            var redirect = params.get('redirect') || sessionStorage.getItem('dchub_redirect');
            if (redirect) {
                sessionStorage.removeItem('dchub_redirect');
                window.location.href = redirect;
            }

            console.log('[DCHUB_AUTH] OAuth callback: token captured, plan=' + user.plan);
        }
    })();

})();
