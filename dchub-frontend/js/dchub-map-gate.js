/**
 * DC Hub — Map Free Tier Gate v5 (Zero Redirects)
 * /js/dchub-map-gate.js?v=5
 *
 * NO LOGIN REDIRECTS. NO DASHBOARD LOOPS. 
 * Works for anonymous, free, and paid users.
 *
 * - Anonymous/free: 5 layer toggles, then upgrade modal on the page
 * - Pro/Enterprise/Founding: unlimited, no restrictions
 * - Upgrade modal has sign-up and pricing links
 * - Server-side tracking for logged-in users (optional, non-blocking)
 */
(function () {
  'use strict';

  var FREE_LAYER_LIMIT = 5;
  var API_BASE = window.DCHUB_API_BASE || '';

  var layerToggles = 0;
  var locked = false;

  /* ── Auth helpers ── */
  function getUserPlan() {
    var PAID = ['pro', 'enterprise', 'founding'];
    try {
      var session = JSON.parse(localStorage.getItem('dchub_session') || '{}');
      var plan = (session.plan || '').toLowerCase();
      if (PAID.indexOf(plan) !== -1) return plan;
    } catch (e) {}
    try {
      var user = JSON.parse(localStorage.getItem('dchub_user') || '{}');
      var plan = (user.plan || '').toLowerCase();
      if (PAID.indexOf(plan) !== -1) return plan;
    } catch (e) {}
    return 'free';
  }

  function isLoggedIn() {
    return !!(localStorage.getItem('dchub_token') || localStorage.getItem('dchub_session'));
  }

  function getAuthToken() {
    var token = localStorage.getItem('dchub_token');
    if (token) return token;
    try {
      var session = JSON.parse(localStorage.getItem('dchub_session') || '{}');
      return session.token || null;
    } catch (e) { return null; }
  }

  /* ── Lock the map with upgrade modal ── */
  function lockMap(reason, message) {
    if (locked) return;
    locked = true;

    var banner = document.getElementById('lp-free-banner');
    if (banner) banner.style.display = 'none';

    if (message) {
      var titleEl = document.getElementById('lp-modal-title');
      var msgEl = document.getElementById('lp-modal-message');
      if (titleEl) titleEl.textContent = 'Upgrade to continue';
      if (msgEl) msgEl.innerHTML = message;
    }

    document.querySelectorAll('.layer-btn').forEach(function (btn) {
      btn.disabled = true;
      btn.style.opacity = '0.35';
      btn.style.cursor = 'not-allowed';
      btn.style.pointerEvents = 'none';
    });

    document.getElementById('lp-upgrade-modal').classList.add('show');
    console.log('[DCHub] Map locked. Reason:', reason);
  }

  function updateBanner() {
    var c = document.getElementById('lp-layer-count');
    if (c) c.textContent = layerToggles + ' / ' + FREE_LAYER_LIMIT;
  }

  /* ── Try to register usage server-side (non-blocking, best-effort) ── */
  function tryServerRegister(endpoint) {
    var token = getAuthToken();
    if (!token) return; // anonymous — no server tracking
    try {
      fetch(API_BASE + endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + token
        }
      }).catch(function () {}); // fire and forget
    } catch (e) {}
  }

  /* ── Main startup ── */
  function startGate() {

    /* Paid users — no restrictions at all */
    var plan = getUserPlan();
    if (plan !== 'free') {
      console.log('[DCHub] ' + plan + ' user. No restrictions.');
      return;
    }

    /* Free/anonymous user — show banner and enforce 5 layers */
    console.log('[DCHub] Free tier active. ' + FREE_LAYER_LIMIT + ' layers allowed.');

    /* Register session server-side if logged in */
    tryServerRegister('/api/v1/map/register-load');

    var banner = document.getElementById('lp-free-banner');
    if (banner) banner.style.display = 'flex';
    updateBanner();

    /* Track layer toggles */
    document.addEventListener('click', function (e) {
      if (locked) return;
      var btn = e.target.closest('.layer-btn');
      if (!btn) return;
      if (!btn.getAttribute('data-layer') && btn.id !== 'drought-btn' && btn.id !== 'risk-btn') return;

      layerToggles++;
      updateBanner();

      /* Report to server (non-blocking) */
      tryServerRegister('/api/v1/map/layer-toggle');

      if (layerToggles >= FREE_LAYER_LIMIT) {
        setTimeout(function () {
          var msg = isLoggedIn()
            ? 'You\'ve explored <strong>5 free layers</strong>.<br>' +
              'Upgrade to Pro for unlimited access to all <strong>40+ layers</strong>, ' +
              'site scoring, export tools, and API access.'
            : 'You\'ve explored <strong>5 free layers</strong>.<br>' +
              '<a href="/login?redirect=/land-power-map" style="color:#a5b4fc;text-decoration:underline;">Create a free account</a> ' +
              'or <a href="/pricing.html" style="color:#a5b4fc;text-decoration:underline;">upgrade to Pro</a> for unlimited access.';
          lockMap('layer_limit', msg);
        }, 150);
      }
    }, true);
  }

  /* ── Startup triggers ── */
  var started = false;
  function tryStart() {
    if (started) return;
    started = true;
    startGate();
  }

  window.addEventListener('dchub:session-valid', tryStart);
  var _fallback = setTimeout(tryStart, 4000);
  document.addEventListener('dchub:map-ready', function () {
    clearTimeout(_fallback);
    tryStart();
  }, { once: true });

})();
