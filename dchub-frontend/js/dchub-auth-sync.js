/**
 * DC Hub Auth Sync — v1.0
 * ========================
 * 
 * ONE LOGIN, EVERYWHERE. This script ensures that no matter which auth path
 * a user takes (login.html form, Google OAuth, Stripe checkout return, or
 * dchub-auth.js session), ALL localStorage keys stay in sync so every page
 * — including Land & Power — recognizes the user immediately.
 *
 * PROBLEM SOLVED:
 *   - login.html sets dchub_token + dchub_user but NOT dchub_session
 *   - dchub-auth.js sets dchub_token + dchub_session but NOT dchub_user
 *   - dchub-nav.js requires dchub_token + dchub_user
 *   - dchub-access-gate.js requires dchub_session with tier
 *   - land-power-app.js requires dchub_token + dchub_user
 *   - homepage-gate.js requires dchub_token + dchub_user
 *   → User logs in once but appears anonymous on some pages
 *
 * ALSO FIXES:
 *   - Backend plan:'free' → gate tier:'registered' mapping
 *   - Session validated lazily (every 15 min) not on every page load
 *   - 401/403 API responses handled with actionable UI
 *   - Stripe checkout return auto-refreshes session to pick up new plan
 *
 * DEPLOY: /js/dchub-auth-sync.js on Cloudflare Pages
 * LOAD: After dchub-auth.js, BEFORE access-gate, homepage-gate, nav
 *
 *   <script src="/js/dchub-auth.js"></script>
 *   <script src="/js/dchub-auth-sync.js"></script>
 *   <script src="/js/dchub-access-gate.js"></script>
 *   <script src="/js/homepage-gate.js"></script>
 *   <script src="/js/dchub-nav.js"></script>
 */

(function () {
  'use strict';

  // ─── Constants ────────────────────────────────────────────
  var TOKEN_KEY   = 'dchub_token';
  var USER_KEY    = 'dchub_user';
  var SESSION_KEY = 'dchub_session';
  var VALIDATE_KEY = 'dchub_last_validated';
  var VALIDATE_INTERVAL = 15 * 60 * 1000; // 15 minutes — don't re-validate more often than this
  var API_BASE = window.DCHUB_API_BASE || 'https://dchub.cloud';

  // ─── Plan → Tier Mapping ──────────────────────────────────
  // Backend returns: free, pro, founding, enterprise
  // Access gate expects: anonymous, registered, pro, enterprise
  function planToTier(plan) {
    if (!plan) return 'free';
    switch (plan.toLowerCase()) {
      case 'pro':        return 'pro';
      case 'founding':   return 'pro';
      case 'enterprise': return 'enterprise';
      case 'admin':      return 'enterprise';
      case 'free':       return 'registered'; // free account = registered (not anonymous, but not pro)
      default:           return 'free';
    }
  }

  // ─── Sync All Auth Keys ───────────────────────────────────
  // Call this after any auth event (login, OAuth, page load)
  // Ensures dchub_token, dchub_user, and dchub_session are all
  // populated and consistent with each other
  function syncAuth() {
    var token = localStorage.getItem(TOKEN_KEY);
    if (!token) return null; // Not logged in

    var user = safeParseJSON(localStorage.getItem(USER_KEY));
    var session = safeParseJSON(localStorage.getItem(SESSION_KEY));

    // Merge: build a canonical user record from whatever we have
    var email   = pick(user, 'email')   || pick(session, 'email')   || '';
    var name    = pick(user, 'name')    || pick(session, 'name')    || '';
    var company = pick(user, 'company') || pick(session, 'company') || '';
    var plan    = pick(user, 'plan')    || pick(session, 'plan')    || 'free';
    var role    = pick(user, 'role')    || pick(session, 'role')    || plan;
    var picture = pick(user, 'picture') || '';
    // If role suggests a paid tier but plan says free, trust the plan
    // (role may be stale from a previous subscription)
    var tier    = planToTier(plan);

    // ── Write dchub_user (for nav, land-power, homepage-gate) ──
    var userObj = {
      email: email,
      name: name,
      plan: plan,
      role: role,
      company: company,
      picture: picture
    };
    localStorage.setItem(USER_KEY, JSON.stringify(userObj));

    // ── Write dchub_session (for access-gate, dchub-auth) ──
    var sessionObj = {
      email: email,
      name: name,
      plan: plan,
      tier: tier,
      token: token,
      company: company,
      role: role,
      updated_at: new Date().toISOString()
    };
    localStorage.setItem(SESSION_KEY, JSON.stringify(sessionObj));

    // ── Set global tier for access-gate immediate pickup ──
    window.DCHUB_USER_TIER = tier;

    return { user: userObj, session: sessionObj, tier: tier };
  }

  // ─── Lazy Session Validation ──────────────────────────────
  // Only hits /api/auth/me if we haven't validated in the last 15 minutes.
  // This means: first page load validates, subsequent navigations skip it.
  function maybeValidateSession() {
    var token = localStorage.getItem(TOKEN_KEY);
    if (!token) return;

    var lastValidated = parseInt(localStorage.getItem(VALIDATE_KEY) || '0', 10);
    var now = Date.now();

    if (now - lastValidated < VALIDATE_INTERVAL) {
      // Recently validated — just sync keys and go
      syncAuth();
      return;
    }

    // Time to re-validate (non-blocking)
    fetch(API_BASE + '/api/auth/me', {
      headers: { 'Authorization': 'Bearer ' + token },
      credentials: 'include'
    })
    .then(function (r) {
      if (!r.ok) {
        // Only hard-clear on 401 with zero cached data.
        // 5xx / 429 / network blips are backend issues — never wipe the token.
        // Even on real 401: Railway cold starts return spurious 401s, so if we
        // have cached session data we keep the user in and re-validate later.
        if (r.status === 401) {
          var cached = localStorage.getItem(SESSION_KEY) || localStorage.getItem(USER_KEY);
          if (!cached) {
            clearAll();
            window.dispatchEvent(new CustomEvent('dchub:session-expired'));
          } else {
            console.warn('[DC Hub Auth Sync] 401 on /me — keeping cached session');
            syncAuth();
          }
        } else {
          console.warn('[DC Hub Auth Sync] /me returned', r.status, '— keeping session');
          syncAuth();
        }
        return null;
      }
      return r.json();
    })
    .then(function (data) {
      if (!data) return;
      var u = data.user || data;

      // Update localStorage with fresh server data
      var userObj = {
        email: u.email || '',
        name:  u.name || '',
        plan:  u.plan || 'free',
        role:  u.role || u.plan || 'free',
        company: u.company || '',
        picture: u.picture || ''
      };
      localStorage.setItem(USER_KEY, JSON.stringify(userObj));

      // Mark validated
      localStorage.setItem(VALIDATE_KEY, String(Date.now()));

      // Re-sync everything with fresh data
      syncAuth();

      window.dispatchEvent(new CustomEvent('dchub:session-valid', {
        detail: safeParseJSON(localStorage.getItem(SESSION_KEY))
      }));
    })
    .catch(function (e) {
      // Network error — don't clear session, might be offline
      console.warn('[DC Hub Auth Sync] Validation network error:', e.message);
      // Still sync what we have
      syncAuth();
    });
  }

  // ─── Stripe Checkout Return Detection ─────────────────────
  // When user returns from Stripe checkout, their plan may have changed.
  // Force an immediate re-validation to pick up the new tier.
  function checkStripeReturn() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('checkout') === 'success' || params.get('session_id') || params.get('welcome') === 'true') {
      // Force re-validation by clearing the timer
      localStorage.removeItem(VALIDATE_KEY);

      // Also force immediate /me call after short delay (Stripe webhook might still be processing)
      setTimeout(function () {
        localStorage.removeItem(VALIDATE_KEY);
        maybeValidateSession();
      }, 2000);
    }
  }

  // ─── 401/403 Response Handler ─────────────────────────────
  // Patches window.fetch to intercept API error responses
  function installFetchInterceptor() {
    var _origFetch = window.fetch;
    window.fetch = function () {
      return _origFetch.apply(this, arguments).then(function (response) {
        var urlStr = '';
        try { urlStr = typeof arguments[0] === 'string' ? arguments[0] : arguments[0].url || ''; } catch (e) {}
        if (!urlStr.includes('/api/')) return response;

        if (response.status === 401) {
          // Only trigger session expiry for auth endpoints.
          // Content endpoints (/api/news, /api/discovery, etc.) can 401/403
          // for plan-gating reasons — that should show an upgrade prompt, not
          // log the user out. The fetch interceptor on land-power-map will
          // also fire for HIFLD/ArcGIS calls — never treat those as auth failures.
          if (urlStr.includes('/api/auth/')) {
            handleExpired();
          }
        } else if (response.status === 403) {
          var cloned = response.clone();
          cloned.json().then(function (body) {
            if (body && (body.required_plan || body.upgrade_url)) {
              handleUpgrade(body);
            }
          }).catch(function () {});
        }
        return response;
      });
    };

    // Also intercept XHR for land-power-app.js which uses XMLHttpRequest
    var _origOpen = XMLHttpRequest.prototype.open;
    var _origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function (method, url) {
      this._dchubUrl = url;
      return _origOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function () {
      var xhr = this;
      var origOnload = xhr.onload;
      if (xhr._dchubUrl && xhr._dchubUrl.includes('/api/')) {
        xhr.onload = function () {
          if (xhr.status === 401 && xhr._dchubUrl && xhr._dchubUrl.includes('/api/auth/')) {
            handleExpired();
          }
          // Let original handler run
          if (origOnload) origOnload.apply(this, arguments);
        };
      }
      return _origSend.apply(this, arguments);
    };
  }

  var _expiredShown = false;
  function handleExpired() {
    if (_expiredShown) return;
    // Pages that allow anonymous access (e.g. land-power-map) set this flag
    // before loading this script. Never redirect or clear session on those pages.
    if (window.DCHUB_SKIP_AUTH_REDIRECT) return;
    _expiredShown = true;
    clearAll();
    window.dispatchEvent(new CustomEvent('dchub:session-expired'));
    showToast('Session expired — please sign in again.', 'Sign In',
      '/login.html?redirect=' + encodeURIComponent(window.location.pathname));
  }

  var _lastUpgradeTime = 0;
  function handleUpgrade(data) {
    var now = Date.now();
    if (now - _lastUpgradeTime < 30000) return; // Max once per 30s
    _lastUpgradeTime = now;

    var plan = data.required_plan || 'pro';
    if (window.DCHubGate && window.DCHubGate.show) {
      window.DCHubGate.show(plan);
    } else if (window.dchubGate && window.dchubGate.showModal) {
      window.dchubGate.showModal();
    } else {
      var label = plan.charAt(0).toUpperCase() + plan.slice(1);
      showToast('This feature requires ' + label + '.', 'View Plans', '/pricing');
    }
  }

  // ─── Toast Notification ───────────────────────────────────
  function showToast(msg, actionText, actionUrl) {
    var existing = document.getElementById('dchub-sync-toast');
    if (existing) existing.remove();

    if (!document.getElementById('dchub-sync-toast-style')) {
      var s = document.createElement('style');
      s.id = 'dchub-sync-toast-style';
      s.textContent = '@keyframes dchubToastSlide{from{transform:translateY(100%);opacity:0}to{transform:translateY(0);opacity:1}}';
      document.head.appendChild(s);
    }

    var t = document.createElement('div');
    t.id = 'dchub-sync-toast';
    t.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:99999;background:#141b2d;border:1px solid #1e293b;border-radius:12px;padding:14px 20px;display:flex;align-items:center;gap:14px;box-shadow:0 8px 32px rgba(0,0,0,0.5);animation:dchubToastSlide 0.3s ease-out;max-width:440px;';
    t.innerHTML = '<span style="color:#e2e8f0;font-size:13px;line-height:1.4;">' + msg + '</span>' +
      (actionText ? '<a href="' + actionUrl + '" style="white-space:nowrap;padding:7px 16px;background:#6366f1;color:#fff;border-radius:8px;font-size:12px;font-weight:600;text-decoration:none;">' + actionText + '</a>' : '') +
      '<span onclick="this.parentElement.remove()" style="color:#64748b;cursor:pointer;font-size:18px;padding:0 4px;line-height:1;">&times;</span>';
    document.body.appendChild(t);

    // Auto-dismiss after 8 seconds
    setTimeout(function () { if (t.parentElement) t.remove(); }, 8000);
  }

  // ─── Helpers ──────────────────────────────────────────────
  function safeParseJSON(str) {
    if (!str) return null;
    try { var o = JSON.parse(str); return typeof o === 'object' ? o : null; } catch (e) { return null; }
  }

  function pick(obj, key) {
    return obj && obj[key] ? obj[key] : '';
  }

  function clearAll() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(VALIDATE_KEY);
    window.DCHUB_USER_TIER = 'anonymous';
  }

  // ─── Listen for Auth Events ───────────────────────────────
  // When dchub-auth.js or login.html dispatches a login event, re-sync
  window.addEventListener('dchub:login', function () {
    syncAuth();
    localStorage.setItem(VALIDATE_KEY, String(Date.now())); // Just logged in = validated
  });

  window.addEventListener('dchub:logout', function () {
    clearAll();
  });

  window.addEventListener('dchub:session-valid', function () {
    syncAuth();
  });

  // ─── Expose Global API ────────────────────────────────────
  window.dchubSync = {
    sync: syncAuth,
    validate: maybeValidateSession,
    clearAll: clearAll,
    planToTier: planToTier,
    getTier: function () { return window.DCHUB_USER_TIER || 'anonymous'; },
    isLoggedIn: function () { return !!localStorage.getItem(TOKEN_KEY); },
    getPlan: function () {
      var s = safeParseJSON(localStorage.getItem(SESSION_KEY));
      return (s && s.plan) || 'free';
    }
  };

  // ─── Init ─────────────────────────────────────────────────
  // 1. Sync auth state immediately (synchronous — before other scripts run)
  syncAuth();

  // 2. Install fetch/XHR interceptors
  installFetchInterceptor();

  // 3. Check if returning from Stripe checkout
  checkStripeReturn();

  // 4. Lazy-validate session (async, non-blocking)
  if (localStorage.getItem(TOKEN_KEY)) {
    maybeValidateSession();
  }

  console.log('[DC Hub Auth Sync] v1.0 | Tier:', window.DCHUB_USER_TIER || 'anonymous',
    '| Token:', !!localStorage.getItem(TOKEN_KEY),
    '| User:', !!localStorage.getItem(USER_KEY),
    '| Session:', !!localStorage.getItem(SESSION_KEY));

})();
