/**
 * DC Hub Auth Helper — v1.0
 * 
 * For static Cloudflare Pages frontend.
 * - Wraps fetch() to auto-attach Bearer token
 * - Validates session on page load via /api/auth/me
 * - Manages localStorage session state
 * - Exposes dchubAuth global for use by other scripts
 * 
 * Deploy to: static/js/dchub-auth.js on Cloudflare Pages
 * Load BEFORE homepage-gate.js and any page-specific scripts
 * 
 * Usage:
 *   <script src="/js/dchub-auth.js"></script>
 *   <script src="/js/homepage-gate.js"></script>
 *   
 *   // In any page script:
 *   const data = await dchubAuth.fetch('/api/news/live');
 *   const json = await data.json();
 */

(function () {
  'use strict';

  const API_BASE = ''; // Same origin — Cloudflare proxies to Replit
  const SESSION_KEY = 'dchub_session';
  const TOKEN_KEY = 'dchub_token';

  // ─── OAuth Callback Token Capture ────────────────────────────
  // Runs first: if URL has ?token=JWT from Google OAuth callback, store it immediately
  // Then redirect to the page the user originally came from (if stored)
  (function () {
    var params = new URLSearchParams(window.location.search);
    var token = params.get('token');
    if (!token) return;
    localStorage.setItem(TOKEN_KEY, token);

    // Check for redirect from multiple sources (in priority order):
    // 1. ?redirect= param in current URL (backend passed it through)
    // 2. sessionStorage (set by login.html before OAuth)
    // 3. localStorage last_page (set by pages before navigating to login)
    var redirect = params.get('redirect')
                || sessionStorage.getItem('dchub_redirect')
                || localStorage.getItem('dchub_last_page');

    // Clean up
    sessionStorage.removeItem('dchub_redirect');
    localStorage.removeItem('dchub_last_page');

    if (redirect && redirect !== '/' && redirect !== window.location.pathname) {
      // Redirect to the original page (e.g. /land-power.html)
      window.location.href = redirect;
      return; // Stop execution — page is navigating
    }

    // No redirect — just clean the URL and stay on this page
    var cleanUrl = window.location.origin + window.location.pathname;
    window.history.replaceState({}, document.title, cleanUrl);
  })();

  // ─── Session Management ─────────────────────────────────────

  function getToken() {
    // Check dedicated token key first, then fall back to session object
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) return token;

    try {
      const session = JSON.parse(localStorage.getItem(SESSION_KEY));
      return session?.token || session?.jwt || null;
    } catch (e) {
      return null;
    }
  }

  function getSession() {
    try {
      const raw = localStorage.getItem(SESSION_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function setSession(data) {
    // Store token separately for easy access
    if (data.token) {
      localStorage.setItem(TOKEN_KEY, data.token);
    }

    // Store full session
    const session = {
      email: data.email || '',
      name: data.name || '',
      plan: data.plan || data.tier || 'free',
      tier: data.tier || data.plan || 'free',
      token: data.token || getToken(),
      company: data.company || '',
      role: data.role || '',
      expires: data.expires || null,
      updated_at: new Date().toISOString()
    };
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
    return session;
  }

  function clearSession() {
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(TOKEN_KEY);
  }

  function isLoggedIn() {
    return !!getToken();
  }

  // ─── Authenticated Fetch Wrapper ────────────────────────────

  async function authFetch(url, options = {}) {
    const token = getToken();

    // Build headers
    const headers = new Headers(options.headers || {});

    // Always set content type for JSON bodies
    if (options.body && typeof options.body === 'string' && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }

    // Attach Bearer token if available
    if (token) {
      headers.set('Authorization', `Bearer ${token}`);
    }

    // Build full URL
    const fullUrl = url.startsWith('http') ? url : `${API_BASE}${url}`;

    const response = await fetch(fullUrl, {
      ...options,
      headers,
      credentials: 'include' // Include cookies as fallback
    });

    // Handle 401 — token may be expired, but don't clear session here.
    // authFetch is used for many API calls (news, facilities, etc.) — a 401
    // on /api/news/live should NOT wipe the user's login session.
    // Let validateSession() be the sole authority on clearing credentials.
    if (response.status === 401) {
      // Just notify — let page-level handlers decide what to do
      window.dispatchEvent(new CustomEvent('dchub:session-expired'));
    }

    return response;
  }

  // ─── Session Validation ─────────────────────────────────────

  async function validateSession() {
    const token = getToken();
    if (!token) return null;

    try {
      const response = await fetch(`${API_BASE}/api/auth/me`, {
        headers: { 'Authorization': `Bearer ${token}` },
        credentials: 'include'
      });

      if (!response.ok) {
        // Only clear session on definitive 401 Unauthorized.
        // Do NOT clear on 500/502/503/429 or any other server/network error —
        // those are backend issues, not invalid tokens, and clearing here
        // causes the login loop (token gone → auth-sync redirects → loop).
        if (response.status === 401) {
          const cached = getSession();
          if (cached) {
            // Even on 401: if we have cached user data, keep the session alive
            // and let the user continue. The token may still be valid — Railway
            // can return spurious 401s during cold starts.
            console.warn('DC Hub: /api/auth/me returned 401 — using cached session');
            window.dispatchEvent(new CustomEvent('dchub:session-valid', { detail: cached }));
            return cached;
          }
          // No cache at all — token is genuinely invalid, clear it
          clearSession();
          return null;
        }
        // 5xx, 429, etc. — backend problem, not our token's fault
        console.warn('DC Hub: /api/auth/me returned', response.status, '— keeping session');
        const cached = getSession();
        if (cached) {
          window.dispatchEvent(new CustomEvent('dchub:session-valid', { detail: cached }));
        }
        return cached;
      }

      const data = await response.json();

      // Update local session with fresh data from server
      const session = setSession({
        email: data.email || data.user?.email,
        name: data.name || data.user?.name,
        plan: data.plan || data.user?.plan || data.tier,
        tier: data.tier || data.user?.tier || data.plan,
        company: data.company || data.user?.company,
        role: data.role || data.user?.role,
        token: token // Keep existing token
      });

      // Dispatch event so other scripts know the session is valid
      window.dispatchEvent(new CustomEvent('dchub:session-valid', { detail: session }));

      return session;
    } catch (e) {
      // Network error — never clear session on network failures
      console.warn('DC Hub: session validation failed (network)', e.message);
      const cached = getSession();
      if (cached) {
        window.dispatchEvent(new CustomEvent('dchub:session-valid', { detail: cached }));
      }
      return cached;
    }
  }

  // ─── Login Helper ───────────────────────────────────────────

  async function login(email, password) {
    try {
      const response = await fetch(`${API_BASE}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
        credentials: 'include'
      });

      const data = await response.json();

      if (!response.ok) {
        return { success: false, error: data.error || data.message || 'Login failed' };
      }

      // Store session + token
      const session = setSession({
        token: data.token || data.jwt || data.access_token,
        email: data.email || data.user?.email || email,
        name: data.name || data.user?.name,
        plan: data.plan || data.user?.plan || data.tier || 'free',
        tier: data.tier || data.user?.tier || data.plan || 'free',
        company: data.company || data.user?.company,
        role: data.role || data.user?.role
      });

      window.dispatchEvent(new CustomEvent('dchub:login', { detail: session }));

      return { success: true, session };
    } catch (e) {
      return { success: false, error: 'Network error — please try again' };
    }
  }

  // ─── Register Helper ────────────────────────────────────────

  async function register(email, password, name, company) {
    try {
      const response = await fetch(`${API_BASE}/api/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, name, company }),
        credentials: 'include'
      });

      const data = await response.json();

      if (!response.ok) {
        return { success: false, error: data.error || data.message || 'Registration failed' };
      }

      // If registration returns a token, auto-login
      if (data.token || data.jwt) {
        const session = setSession({
          token: data.token || data.jwt,
          email: data.email || email,
          name: data.name || name,
          plan: 'free',
          tier: 'free',
          company: company || ''
        });

        window.dispatchEvent(new CustomEvent('dchub:login', { detail: session }));
        return { success: true, session, autoLogin: true };
      }

      return { success: true, message: data.message || 'Account created — please log in' };
    } catch (e) {
      return { success: false, error: 'Network error — please try again' };
    }
  }

  // ─── Google OAuth Handler ───────────────────────────────────

  function handleGoogleCallback() {
    // After Google OAuth, the backend redirects to something like:
    // /login.html?token=xxx&email=yyy&plan=zzz
    // or the token might be in a hash fragment
    const params = new URLSearchParams(window.location.search);
    const hashParams = new URLSearchParams(window.location.hash.replace('#', ''));

    const token = params.get('token') || params.get('jwt') ||
                  hashParams.get('token') || hashParams.get('jwt');

    if (token) {
      const session = setSession({
        token: token,
        email: params.get('email') || hashParams.get('email') || '',
        name: params.get('name') || hashParams.get('name') || '',
        plan: params.get('plan') || hashParams.get('plan') || 'free',
        tier: params.get('tier') || hashParams.get('tier') || 'free'
      });

      // Clean URL
      const cleanUrl = window.location.pathname;
      window.history.replaceState({}, '', cleanUrl);

      window.dispatchEvent(new CustomEvent('dchub:login', { detail: session }));

      // If we were redirected from a specific page, go back there
      const redirect = params.get('redirect') || sessionStorage.getItem('dchub_redirect');
      if (redirect) {
        sessionStorage.removeItem('dchub_redirect');
        window.location.href = redirect;
      }

      return session;
    }

    return null;
  }

  // ─── Logout ─────────────────────────────────────────────────

  function logout() {
    clearSession();
    window.dispatchEvent(new CustomEvent('dchub:logout'));
    // Optionally call backend logout
    fetch(`${API_BASE}/api/auth/logout`, {
      method: 'POST',
      credentials: 'include'
    }).catch(() => {}); // Fire and forget
  }

  // ─── Expose Global API ──────────────────────────────────────

  window.dchubAuth = {
    // Core
    fetch: authFetch,
    getToken,
    getSession,
    setSession,
    clearSession,
    isLoggedIn,

    // Auth actions
    login,
    register,
    logout,
    validateSession,
    handleGoogleCallback,

    // Convenience
    getPlan() {
      const session = getSession();
      return session?.plan || session?.tier || 'anonymous';
    },
    isPro() {
      const plan = this.getPlan();
      return ['pro', 'enterprise'].includes(plan);
    },
    isEnterprise() {
      return this.getPlan() === 'enterprise';
    }
  };

  // ─── Auto-Init ──────────────────────────────────────────────

  // 1. Check for Google OAuth callback tokens in URL
  handleGoogleCallback();

  // 2. Validate existing session on page load (non-blocking)
  //    Pages that allow anonymous access (e.g. land-power-map) set
  //    window.DCHUB_SKIP_AUTH_REDIRECT = true BEFORE loading this script.
  //    We still validate the session (to get the plan) but we never redirect.
  if (isLoggedIn()) {
    validateSession().then(session => {
      if (session) {
        console.log('DC Hub: session valid —', session.plan, 'plan');
      } else {
        console.log('DC Hub: session expired — cleared');
      }
    });
  }

})();
