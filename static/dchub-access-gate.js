/**
 * DC Hub Access Gate v3.1.1
 * Three-tier gating: anonymous -> registered (free) -> pro -> enterprise
 * v3.1.0: Session sync from backend — fixes death spiral after Stripe upgrade
 * v3.1.1: Fixed /api/auth/me response parsing (nested user object) + founding→pro mapping
 *
 * USAGE:
 *   data-gate="registered"  — requires free account (blurs + lock badge)
 *   data-gate="pro"         — requires Pro subscription
 *   data-gate-preview="6" data-gate="registered"  — show 6 items, gradient-blur the rest
 *
 * DETECTION:
 *   anonymous = no dchub_token in localStorage
 *   registered = has dchub_token + dchub_user
 *   pro/enterprise = has dchub_session with valid tier + expiry
 */
(function () {
  'use strict';
  var LOGIN_URL = '/login.html';
  var PRICING_URL = '/pricing';
  var API_BASE = 'https://dchub.cloud';
  var TIER_HIERARCHY = { anonymous: 0, free: 0, registered: 1, pro: 2, enterprise: 3 };
  var FOUNDING_TOTAL = 50;
  var FOUNDING_SEATS_CLAIMED = 3;
  var _seatsRemaining = FOUNDING_TOTAL - FOUNDING_SEATS_CLAIMED;

  try {
    fetch(API_BASE + '/api/founding-members', { mode: 'cors' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && typeof d.claimed === 'number') {
          FOUNDING_SEATS_CLAIMED = d.claimed;
          _seatsRemaining = FOUNDING_TOTAL - d.claimed;
        }
      }).catch(function () {});
  } catch (e) {}

  // ─── SESSION SYNC: Refresh tier from backend if token exists but session is stale ───
  var _tierSynced = false;
  var _tierSyncPromise = null;

  function syncSessionFromBackend() {
    if (_tierSynced || _tierSyncPromise) return _tierSyncPromise;
    var token = localStorage.getItem('dchub_token');
    if (!token) return Promise.resolve();
    _tierSyncPromise = fetch(API_BASE + '/api/auth/me', {
      headers: { 'Authorization': 'Bearer ' + token },
      mode: 'cors'
    }).then(function (r) {
      if (!r.ok) throw new Error('auth/me failed: ' + r.status);
      return r.json();
    }).then(function (data) {
      _tierSynced = true;
      // /api/auth/me returns { success: true, user: { plan: "pro", email: "..." } }
      var user = (data && data.user) ? data.user : data;
      var plan = user && (user.plan || user.tier);
      if (plan) {
        var session = {};
        try { session = JSON.parse(localStorage.getItem('dchub_session') || '{}'); } catch (e) {}
        var backendTier = (plan === 'free') ? 'registered' : (plan === 'founding') ? 'pro' : plan;
        if (session.tier !== backendTier) {
          console.log('[DC Hub] Session sync: ' + (session.tier || 'none') + ' → ' + backendTier);
          session.tier = backendTier;
          session.expires = new Date(Date.now() + 30 * 86400000).toISOString();
          session.email = user.email || session.email;
          localStorage.setItem('dchub_session', JSON.stringify(session));
          window.DCHUB_USER_TIER = backendTier;
        }
      }
    }).catch(function (e) {
      console.warn('[DC Hub] Session sync failed:', e.message);
      _tierSynced = true;
    });
    return _tierSyncPromise;
  }

  // Kick off sync immediately if user has a token
  if (localStorage.getItem('dchub_token')) {
    syncSessionFromBackend();
  }

  function getUserTier() {
    if (window.DCHUB_USER_TIER) return window.DCHUB_USER_TIER;
    try {
      var session = JSON.parse(localStorage.getItem('dchub_session') || '{}');
      // Check tier from session (dchub-auth.js stores tier here)
      if (session.tier) {
        // If session has expires field, check it; otherwise trust the session
        if (!session.expires || new Date(session.expires) > new Date()) return session.tier;
      }
    } catch (e) {}
    if (localStorage.getItem('dchub_token')) return 'registered';
    return 'anonymous';
  }
  function isLoggedIn() {
    // Check either dchub_user OR dchub_session — auth stores data in session
    return !!localStorage.getItem('dchub_token') && (!!localStorage.getItem('dchub_user') || !!localStorage.getItem('dchub_session'));
  }
  function hasAccess(t) { return (TIER_HIERARCHY[getUserTier()] || 0) >= (TIER_HIERARCHY[t] || 0); }

  var TIER_FEATURES = {
    registered: {
      name: 'Free', color: '#6366f1', tagline: 'Create a free account to unlock full data',
      features: ['All 28 market intelligence cards', 'Construction pipeline tracker', 'News feed (150+ articles, 30+ sources)', 'Facility browser (20,000+)', 'Market growth charts', 'Announcements archive', '5 searches per day', 'Save up to 3 searches']
    },
    pro: {
      name: 'Pro', foundingPrice: '$99', regularPrice: '$199', period: '/mo', color: '#00c9a7',
      tagline: 'Unlock premium intelligence tools',
      features: ['Land & Power map (free preview + full Pro access)', 'Transaction comps & $/MW data', 'Competitive Intelligence panel', '50+ infrastructure data layers', 'Real-time energy pricing', 'Fiber & pipeline mapping', 'Export reports (PDF/CSV)', '10,000 API calls/day', 'Market reports (quarterly)', 'Unlimited searches & alerts']
    },
    enterprise: {
      name: 'Enterprise', foundingPrice: '$349', regularPrice: '$699', period: '/mo', color: '#845ef7',
      tagline: 'Full platform access + AI intelligence',
      features: ['Everything in Pro', 'AI Expert Brain assistant', 'Predictive M&A signals', 'Custom market reports', 'Dedicated account manager', 'White-label options', 'Unlimited API access', 'Priority data updates']
    }
  };

  function injectStyles() {
    if (document.getElementById('gate-styles')) return;
    var s = document.createElement('style'); s.id = 'gate-styles';
    s.textContent = [
      '[data-gate-locked]{position:relative;pointer-events:none;user-select:none}',
      '[data-gate-locked]>*{filter:blur(12px);opacity:.25;transition:filter .3s}',
      '[data-gate-locked]::after{content:"";position:absolute;inset:0;z-index:50;cursor:pointer;pointer-events:all}',
      '[data-gate-preview-active]{position:relative}',
      '.gate-preview-overlay{position:absolute;bottom:0;left:0;right:0;height:80%;background:linear-gradient(to bottom,transparent 0%,rgba(4,8,14,0.92) 35%,rgba(4,8,14,1) 100%);z-index:50;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;padding-bottom:32px;pointer-events:all;cursor:pointer}',
      '.gate-preview-cta{display:flex;flex-direction:column;align-items:center;gap:12px;pointer-events:all}',
      '.gate-preview-text{font-size:15px;font-weight:700;color:#f0f4f8;text-align:center;max-width:360px;line-height:1.4}',
      '.gate-preview-sub{font-size:12px;color:#5d7590;text-align:center}',
      '.gate-preview-btn{padding:12px 28px;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;transition:all .25s;letter-spacing:.3px}',
      '.gate-preview-btn:hover{transform:translateY(-1px);box-shadow:0 8px 25px rgba(99,102,241,.3)}',
      '#gate-overlay{position:fixed;inset:0;z-index:9999;background:rgba(4,8,14,.88);backdrop-filter:blur(20px);display:flex;align-items:center;justify-content:center;opacity:0;animation:gate-fade .5s cubic-bezier(.22,1,.36,1) forwards}',
      '@keyframes gate-fade{to{opacity:1}}',
      '#gate-modal{width:min(540px,92vw);background:#0a1220;border:1px solid #1a2d44;border-radius:20px;overflow:hidden;box-shadow:0 40px 100px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.04);animation:gate-rise .6s cubic-bezier(.22,1,.36,1) forwards;transform:translateY(30px);opacity:0}',
      '@keyframes gate-rise{to{transform:translateY(0);opacity:1}}',
      '.gate-head{padding:32px 36px 0;text-align:center}',
      '.gate-icon{width:68px;height:68px;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:30px;margin:0 auto 14px;border:1px solid rgba(255,255,255,.06)}',
      '.gate-title{font-family:"Instrument Sans","SF Pro Display",-apple-system,sans-serif;font-size:22px;font-weight:800;color:#f0f4f8;letter-spacing:-.3px;margin-bottom:6px}',
      '.gate-sub{font-size:13px;color:#5d7590;line-height:1.5;max-width:380px;margin:0 auto}',
      '.gate-price{text-align:center;padding:20px 36px 4px}',
      '.gate-price-row{display:flex;align-items:baseline;justify-content:center;gap:14px}',
      '.gate-price-old{font-size:28px;font-weight:700;color:#3d556e;text-decoration:line-through;text-decoration-color:#ff4757;text-decoration-thickness:2.5px}',
      '.gate-price-new{font-size:52px;font-weight:800;letter-spacing:-2px}',
      '.gate-price-new span{font-size:16px;font-weight:500;color:#5d7590;letter-spacing:0}',
      '.gate-free-price{text-align:center;padding:16px 36px 8px}',
      '.gate-free-price-text{font-size:48px;font-weight:800;color:#6366f1}',
      '.gate-free-price-sub{font-size:13px;color:#5d7590;margin-top:4px}',
      '.gate-scarcity{padding:8px 36px 16px;text-align:center}',
      '.gate-seats-bar{height:6px;background:#1a2d44;border-radius:3px;overflow:hidden;margin:8px auto 0;max-width:300px}',
      '.gate-seats-fill{height:100%;border-radius:3px;transition:width 1s ease}',
      '.gate-seats-text{font-size:12px;font-weight:700;letter-spacing:.3px;margin-top:6px}',
      '.gate-badge{display:inline-block;font-size:10px;font-weight:800;padding:3px 10px;border-radius:20px;letter-spacing:.8px;text-transform:uppercase;margin-bottom:6px}',
      '.gate-features{padding:0 36px 20px;display:grid;grid-template-columns:1fr 1fr;gap:7px}',
      '.gate-feat{display:flex;align-items:flex-start;gap:7px;font-size:11.5px;color:#8fa3b8;line-height:1.4}',
      '.gate-check{color:#00c9a7;font-size:13px;flex-shrink:0;margin-top:1px}',
      '.gate-cta{padding:0 36px 12px;display:flex;flex-direction:column;gap:10px}',
      '.gate-btn-primary{display:block;width:100%;padding:14px;border:none;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;text-align:center;text-decoration:none;letter-spacing:.3px;transition:all .25s}',
      '.gate-btn-primary:hover{transform:translateY(-1px);box-shadow:0 8px 30px rgba(0,201,167,.3)}',
      '.gate-btn-secondary{display:block;width:100%;padding:11px;background:transparent;border:1px solid #1a2d44;border-radius:12px;color:#5d7590;font-size:13px;font-weight:600;cursor:pointer;text-align:center;text-decoration:none;transition:all .25s}',
      '.gate-btn-secondary:hover{border-color:#2a4060;color:#8fa3b8;text-decoration:none}',
      '.gate-dismiss{padding:14px;text-align:center;border-top:1px solid rgba(255,255,255,.04)}',
      '.gate-dismiss a{font-size:12px;color:#3d556e;text-decoration:none;cursor:pointer}',
      '.gate-dismiss a:hover{color:#5d7590}',
      '.gate-lock-badge{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:55;background:#0a1220;border:1px solid #1a2d44;border-radius:12px;padding:10px 20px;display:flex;align-items:center;gap:8px;pointer-events:all;cursor:pointer;box-shadow:0 8px 30px rgba(0,0,0,.4);transition:all .25s}',
      '.gate-lock-badge:hover{border-color:#6366f1;background:#0d1822;box-shadow:0 8px 30px rgba(99,102,241,.15)}',
      '.gate-lock-badge .lock-icon{font-size:16px}',
      '.gate-lock-badge .lock-text{font-size:12px;font-weight:700;color:#8fa3b8;letter-spacing:.5px;text-transform:uppercase}',
      '.gate-lock-badge .lock-tier{font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;text-transform:uppercase;letter-spacing:.5px}',
      '.gate-google-btn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:13px;background:#fff;border:1px solid #dadce0;border-radius:12px;font-size:14px;font-weight:600;color:#3c4043;cursor:pointer;transition:all .2s}',
      '.gate-google-btn:hover{background:#f8f9fa;box-shadow:0 1px 3px rgba(0,0,0,.1)}',
      '.gate-google-btn svg{width:18px;height:18px}',
      '.gate-divider{display:flex;align-items:center;gap:12px;padding:0 36px;margin:4px 0}',
      '.gate-divider::before,.gate-divider::after{content:"";flex:1;height:1px;background:#1a2d44}',
      '.gate-divider span{font-size:11px;color:#3d556e;font-weight:600;text-transform:uppercase;letter-spacing:1px}',
      '@media(max-width:580px){#gate-modal{border-radius:16px}.gate-head{padding:24px 20px 0}.gate-features{grid-template-columns:1fr;padding:0 20px 16px}.gate-cta{padding:0 20px 12px}.gate-price{padding:16px 20px 4px}.gate-price-new{font-size:40px}.gate-free-price-text{font-size:36px}}'
    ].join('\n');
    document.head.appendChild(s);
  }

  var GOOGLE_SVG = '<svg viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>';

  function buildScarcityHTML() {
    var r = _seatsRemaining, pct = Math.round((FOUNDING_SEATS_CLAIMED / FOUNDING_TOTAL) * 100);
    var c = r <= 10 ? '#ff4757' : r <= 25 ? '#ffa502' : '#00c9a7';
    var t = r <= 10 ? 'Almost gone!' : r <= 25 ? 'Going fast' : 'Available';
    return '<div class="gate-scarcity"><div class="gate-badge" style="background:' + c + '18;color:' + c + '">\uD83D\uDD25 FOUNDING MEMBER \u2014 Locked for life</div><div class="gate-seats-bar"><div class="gate-seats-fill" style="width:' + pct + '%;background:' + c + '"></div></div><div class="gate-seats-text" style="color:' + c + '"><span class="seat-count">' + r + '</span> of ' + FOUNDING_TOTAL + ' spots remaining \u2014 ' + t + '</div></div>';
  }

  function closeOverlay() {
    var o = document.getElementById('gate-overlay');
    if (!o) return;
    o.style.opacity = '0';
    setTimeout(function () { if (o.parentNode) o.remove(); document.body.style.overflow = ''; }, 300);
  }

  function wireClose(overlay) {
    var closeBtn = document.getElementById('gate-close');
    if (closeBtn) closeBtn.addEventListener('click', closeOverlay);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) closeOverlay(); });
    document.addEventListener('keydown', function h(e) { if (e.key === 'Escape') { closeOverlay(); document.removeEventListener('keydown', h); } });
  }

  function showRegistrationModal() {
    var existing = document.getElementById('gate-overlay');
    if (existing) existing.remove();
    var cfg = TIER_FEATURES.registered;
    var overlay = document.createElement('div');
    overlay.id = 'gate-overlay';
    var featHTML = cfg.features.map(function (f) { return '<div class="gate-feat"><span class="gate-check">\u2713</span>' + f + '</div>'; }).join('');
    var redir = encodeURIComponent(window.location.pathname);
    overlay.innerHTML =
      '<div id="gate-modal"><div class="gate-head">' +
        '<div class="gate-icon" style="background:#6366f115;color:#6366f1">\uD83D\uDD13</div>' +
        '<div class="gate-title">' + cfg.tagline + '</div>' +
        '<div class="gate-sub">Join 2,400+ data center professionals. Free forever \u2014 no credit card required.</div>' +
      '</div>' +
      '<div class="gate-free-price"><div class="gate-free-price-text">Free</div><div class="gate-free-price-sub">Instant access \u2022 No credit card</div></div>' +
      '<div class="gate-features">' + featHTML + '</div>' +
      '<div class="gate-cta"><button type="button" class="gate-google-btn" id="gate-google-signup">' + GOOGLE_SVG + ' Continue with Google</button></div>' +
      '<div class="gate-divider"><span>or</span></div>' +
      '<div class="gate-cta"><a href="' + LOGIN_URL + '?redirect=' + redir + '" class="gate-btn-primary" style="background:#6366f1;color:#fff">Sign up with email \u2192</a><a href="' + PRICING_URL + '" class="gate-btn-secondary">View Pro plans instead</a></div>' +
      '<div class="gate-dismiss"><a id="gate-close">Continue browsing with limited access</a></div></div>';
    document.body.appendChild(overlay);

    var gb = document.getElementById('gate-google-signup');
    if (gb) gb.addEventListener('click', function () {
      // Store redirect in localStorage as backup (survives backend redirect flow)
      localStorage.setItem('dchub_last_page', redir);
      sessionStorage.setItem('dchub_redirect', redir);
      // Use server-side OAuth redirect flow (works in incognito, no third-party cookies needed)
      window.location.href = '/api/auth/google/redirect?redirect=' + redir;
    });
    wireClose(overlay);
    if (typeof gtag !== 'undefined') gtag('event', 'signup_prompt_shown', { event_category: 'Conversion' });
  }

  function showProModal(tier) {
    var existing = document.getElementById('gate-overlay');
    if (existing) existing.remove();
    var cfg = TIER_FEATURES[tier] || TIER_FEATURES.pro;
    var overlay = document.createElement('div');
    overlay.id = 'gate-overlay';
    var featHTML = cfg.features.map(function (f) { return '<div class="gate-feat"><span class="gate-check">\u2713</span>' + f + '</div>'; }).join('');
    overlay.innerHTML =
      '<div id="gate-modal"><div class="gate-head">' +
        '<div class="gate-icon" style="background:' + cfg.color + '15;color:' + cfg.color + '">\uD83D\uDD12</div>' +
        '<div class="gate-title">' + cfg.tagline + '</div>' +
        '<div class="gate-sub">This feature requires DC Hub ' + cfg.name + '. Join professionals at CBRE, JLL, and EdgeConneX.</div>' +
      '</div>' +
      '<div class="gate-price"><div class="gate-price-row"><span class="gate-price-old">' + cfg.regularPrice + '</span><span class="gate-price-new" style="color:' + cfg.color + '">' + cfg.foundingPrice + '<span>' + cfg.period + '</span></span></div></div>' +
      buildScarcityHTML() +
      '<div class="gate-features">' + featHTML + '</div>' +
      '<div class="gate-cta"><a href="' + PRICING_URL + '#' + tier + '" class="gate-btn-primary" style="background:' + cfg.color + ';color:#000">Claim Founding Member Spot \u2192</a><a href="' + PRICING_URL + '" class="gate-btn-secondary">Compare All Plans</a></div>' +
      '<div class="gate-dismiss"><a id="gate-close">Maybe later</a></div></div>';
    document.body.appendChild(overlay);
    wireClose(overlay);
  }

  function showGateModal(tier) {
    if (tier === 'registered' && !isLoggedIn()) { showRegistrationModal(); return; }
    showProModal(tier);
  }

  function gateElements() {
    document.querySelectorAll('[data-gate]:not([data-gate-locked]):not([data-gate-preview])').forEach(function (el) {
      var required = el.getAttribute('data-gate');
      if (hasAccess(required)) return;
      el.setAttribute('data-gate-locked', '');
      var isReg = required === 'registered';
      var tierColor = isReg ? '#6366f1' : (TIER_FEATURES[required] || TIER_FEATURES.pro).color;
      var badge = document.createElement('div');
      badge.className = 'gate-lock-badge';
      badge.innerHTML = '<span class="lock-icon">' + (isReg ? '\uD83D\uDD13' : '\uD83D\uDD12') + '</span><span class="lock-text">' + (isReg ? 'Free Account' : cfg_name(required)) + '</span><span class="lock-tier" style="background:' + tierColor + '20;color:' + tierColor + '">' + (isReg ? 'Sign Up' : 'Upgrade') + '</span>';
      badge.addEventListener('click', function (e) { e.stopPropagation(); showGateModal(required); });
      el.style.position = 'relative';
      el.appendChild(badge);
      el.addEventListener('click', function () { showGateModal(required); });
    });
  }

  function cfg_name(tier) { return tier === 'enterprise' ? 'Enterprise' : 'Pro'; }

  function gatePreviewElements() {
    document.querySelectorAll('[data-gate-preview]:not([data-gate-preview-active])').forEach(function (el) {
      var required = el.getAttribute('data-gate') || 'registered';
      if (hasAccess(required)) return;
      var showCount = parseInt(el.getAttribute('data-gate-preview')) || 3;
      el.setAttribute('data-gate-preview-active', '');
      var children = el.children;
      for (var i = showCount; i < children.length; i++) {
        children[i].style.filter = 'blur(14px)';
        children[i].style.opacity = '0.15';
        children[i].style.pointerEvents = 'none';
      }
      var isReg = required === 'registered';
      var btnColor = isReg ? '#6366f1' : '#00c9a7';
      var msgs = { 'market-intel-grid': '28 markets available', 'facilities-grid': '20,000+ facilities in database', 'news-feed-content': '150+ articles from 30+ sources', 'pipeline-timeline': '7.8 GW pipeline data available', 'announcements-container': 'Full announcements archive available' };
      var totalMsg = msgs[el.id] || 'Full dataset available';
      var overlay = document.createElement('div');
      overlay.className = 'gate-preview-overlay';
      overlay.innerHTML = '<div class="gate-preview-cta"><div class="gate-preview-text">' + (isReg ? '\uD83D\uDD13 Create a free account to see all data' : '\uD83D\uDD12 Upgrade to Pro for full access') + '</div><div class="gate-preview-sub">' + totalMsg + '</div><button type="button" class="gate-preview-btn" style="background:' + btnColor + ';color:#fff">' + (isReg ? 'Sign Up Free \u2192' : 'View Pro Plans \u2192') + '</button></div>';
      overlay.addEventListener('click', function (e) { e.stopPropagation(); showGateModal(required); });
      el.style.position = 'relative';
      el.appendChild(overlay);
    });
  }

  function gateFullPage(tier) {
    // If user has a token, wait for backend session sync before gating
    if (localStorage.getItem('dchub_token') && _tierSyncPromise) {
      _tierSyncPromise.then(function () {
        if (hasAccess(tier)) {
          console.log('[DC Hub] Page gate: access granted after sync (' + getUserTier() + ' >= ' + tier + ')');
          document.body.style.overflow = '';
          return;
        }
        console.log('[DC Hub] Page gate: blocked (' + getUserTier() + ' < ' + tier + ')');
        document.body.style.overflow = 'hidden';
        setTimeout(function () { showGateModal(tier); }, 300);
      });
      // Temporarily hide body while we verify (prevents flash of gated content)
      if (!hasAccess(tier)) document.body.style.overflow = 'hidden';
      return;
    }
    if (hasAccess(tier)) return;
    document.body.style.overflow = 'hidden';
    setTimeout(function () { showGateModal(tier); }, 800);
  }

  function init() {
    injectStyles();
    if (window.DCHUB_PAGE_GATE && window.DCHUB_PAGE_GATE !== 'free') gateFullPage(window.DCHUB_PAGE_GATE);
    gateElements();
    gatePreviewElements();
    if (window.MutationObserver) {
      new MutationObserver(function (muts) {
        var needs = false;
        muts.forEach(function (m) { m.addedNodes.forEach(function (n) {
          if (n.nodeType === 1 && (n.getAttribute && (n.getAttribute('data-gate') || n.getAttribute('data-gate-preview')) || n.querySelectorAll && n.querySelectorAll('[data-gate],[data-gate-preview]').length)) needs = true;
        }); });
        if (needs) { gateElements(); gatePreviewElements(); }
      }).observe(document.body, { childList: true, subtree: true });
    }
    console.log('[DC Hub] Access Gate v3.1.1 | Tier: ' + getUserTier() + ' | Logged in: ' + isLoggedIn() + ' | Founding spots: ' + _seatsRemaining + '/' + FOUNDING_TOTAL + ' | Synced: ' + _tierSynced);

    // ─── AGGRESSIVE GATING FOR ANONYMOUS USERS ───────────────
    if (getUserTier() === 'anonymous') {

      // Force-gate sections by ID with max-height + gradient overlay
      var sectionsToGate = [
        { id: 'facilities-grid', maxHeight: '520px', msg: '20,000+ facilities — Create a free account to browse all', type: 'registered' },
        { id: 'market-intel-grid', maxHeight: '450px', msg: '28 markets with live vacancy & pricing data', type: 'registered' },
        { id: 'news-feed-content', maxHeight: '280px', msg: '150+ articles from 30+ sources updated every 3 minutes', type: 'registered' },
        { id: 'announcements-container', maxHeight: '350px', msg: 'Full announcements archive', type: 'registered' },
        { id: 'chart-bars', maxHeight: '200px', msg: 'Market growth data across 10 markets', type: 'registered' }
      ];

      function forceGateSections() {
        sectionsToGate.forEach(function (cfg) {
          var el = document.getElementById(cfg.id);
          if (!el || el.getAttribute('data-force-gated')) return;
          el.setAttribute('data-force-gated', '1');
          el.style.maxHeight = cfg.maxHeight;
          el.style.overflow = 'hidden';
          el.style.position = 'relative';
          
          var overlay = document.createElement('div');
          overlay.style.cssText = 'position:absolute;bottom:0;left:0;right:0;height:75%;background:linear-gradient(to bottom,transparent 0%,rgba(4,8,14,0.92) 35%,rgba(4,8,14,1) 100%);z-index:50;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;padding-bottom:32px;cursor:pointer';
          var isReg = cfg.type === 'registered';
          var btnColor = isReg ? '#6366f1' : '#00c9a7';
          overlay.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;gap:12px;pointer-events:all">' +
            '<div style="font-size:15px;font-weight:700;color:#f0f4f8;text-align:center;max-width:360px;line-height:1.4">' + (isReg ? '🔓 Create a free account to see all data' : '🔒 Upgrade to Pro for full access') + '</div>' +
            '<div style="font-size:12px;color:#5d7590;text-align:center">' + cfg.msg + '</div>' +
            '<button style="padding:12px 28px;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;background:' + btnColor + ';color:#fff;letter-spacing:.3px">' + (isReg ? 'Sign Up Free →' : 'View Pro Plans →') + '</button></div>';
          overlay.addEventListener('click', function (e) { e.stopPropagation(); showGateModal(cfg.type); });
          el.appendChild(overlay);
        });
      }

      // Run immediately and again after dynamic content loads
      forceGateSections();
      setTimeout(forceGateSections, 1500);
      setTimeout(forceGateSections, 4000);

      // Full-lock sections (transactions, reports, pipeline)
      var fullLockSections = [
        { id: 'transactions', type: 'pro' },
        { id: 'reports-section', type: 'pro' },
        { id: 'pipeline', type: 'registered' }
      ];
      fullLockSections.forEach(function (cfg) {
        var section = document.getElementById(cfg.id);
        if (!section || section.getAttribute('data-force-locked')) return;
        section.setAttribute('data-force-locked', '1');
        section.style.position = 'relative';
        section.style.maxHeight = '300px';
        section.style.overflow = 'hidden';
        // Apply blur to all child elements
        Array.from(section.children).forEach(function (child) {
          child.style.filter = 'blur(10px)';
          child.style.opacity = '0.2';
          child.style.pointerEvents = 'none';
        });
        var lockOverlay = document.createElement('div');
        lockOverlay.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(to bottom,rgba(4,8,14,0.5) 0%,rgba(4,8,14,0.95) 60%,rgba(4,8,14,1) 100%);z-index:55;display:flex;align-items:center;justify-content:center;cursor:pointer';
        var isPro = cfg.type === 'pro';
        lockOverlay.innerHTML = '<div style="text-align:center;padding:20px">' +
          '<div style="font-size:2rem;margin-bottom:8px">' + (isPro ? '🔒' : '🔓') + '</div>' +
          '<div style="color:#fff;font-size:1.1rem;font-weight:700;margin-bottom:6px">' + (isPro ? 'Pro Feature' : 'Free Account Required') + '</div>' +
          '<div style="color:#5d7590;font-size:0.85rem;margin-bottom:16px">' + (isPro ? 'Transaction comps, market reports & more' : 'Create a free account to access this section') + '</div>' +
          '<button style="padding:10px 24px;background:' + (isPro ? '#00c9a7' : '#6366f1') + ';color:#fff;border:none;border-radius:8px;font-weight:600;cursor:pointer">' + (isPro ? 'View Pro Plans →' : 'Sign Up Free →') + '</button></div>';
        lockOverlay.addEventListener('click', function () { showGateModal(cfg.type); });
        section.appendChild(lockOverlay);
      });

      // Sticky signup bar at bottom
      if (!sessionStorage.getItem('dchub_sticky_dismissed')) {
        setTimeout(function () {
          if (document.getElementById('dchub-sticky-gate')) return;
          var bar = document.createElement('div');
          bar.id = 'dchub-sticky-gate';
          bar.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:linear-gradient(135deg,#0a1628,#0d1f3c);border-top:1px solid rgba(99,102,241,0.3);padding:14px 24px;display:flex;align-items:center;justify-content:center;gap:16px;z-index:9998;box-shadow:0 -4px 30px rgba(0,0,0,0.5);animation:stickySlide 0.4s ease-out';
          var styleEl = document.createElement('style');
          styleEl.textContent = '@keyframes stickySlide{from{transform:translateY(100%)}to{transform:translateY(0)}}';
          document.head.appendChild(styleEl);
          bar.innerHTML = '<span style="color:rgba(255,255,255,0.9);font-size:0.9rem"><strong style="color:#00c8ff">80,000+ professionals</strong> use DC Hub for data center intelligence. Join them — it\'s free.</span>' +
            '<button onclick="window.DCHubGate.showSignup()" style="padding:8px 20px;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;border:none;border-radius:6px;font-weight:600;font-size:0.85rem;cursor:pointer;white-space:nowrap">Create Free Account</button>' +
            '<span onclick="this.parentElement.remove();sessionStorage.setItem(\'dchub_sticky_dismissed\',\'1\')" style="color:rgba(255,255,255,0.4);cursor:pointer;font-size:1.2rem;padding:0 8px">×</span>';
          document.body.appendChild(bar);
        }, 5000);
      }

      // Map overlay after 3 seconds
      setTimeout(function () {
        var mapSection = document.getElementById('map-section');
        if (!mapSection) return;
        if (mapSection.querySelector('.gate-map-wall')) return;
        
        // Create a full wall over the entire map section
        var wall = document.createElement('div');
        wall.className = 'gate-map-wall';
        wall.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;background:rgba(10,15,30,0.92);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:200;cursor:pointer';
        wall.innerHTML = '<div style="text-align:center;max-width:420px;padding:24px">' +
          '<div style="font-size:3rem;margin-bottom:16px">🗺️</div>' +
          '<h3 style="color:#fff;font-size:1.4rem;margin:0 0 10px">Interactive Data Center Map</h3>' +
          '<p style="color:rgba(255,255,255,0.6);font-size:0.95rem;margin:0 0 8px">13,000+ facilities across 140+ countries</p>' +
          '<p style="color:rgba(255,255,255,0.5);font-size:0.85rem;margin:0 0 24px">Filter by tier, capacity, provider, and region</p>' +
          '<button style="padding:14px 36px;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;border:none;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;letter-spacing:.3px">Create Free Account to Explore →</button>' +
          '<div style="margin-top:12px"><a href="/login.html" style="color:#6366f1;font-size:13px;text-decoration:none">Already have an account? Sign in</a></div>' +
          '</div>';
        wall.addEventListener('click', function (e) { 
          e.stopPropagation();
          showRegistrationModal(); 
        });
        
        // Force the section to be the positioning parent
        mapSection.style.position = 'relative';
        mapSection.style.overflow = 'hidden';
        mapSection.appendChild(wall);
      }, 2000);

      // Intercept nav links to gated pages
      var gatedPaths = ['land-power', 'transactions', 'transaction-comps', 'ai-deals', 'ai-pipeline', 'ai-inventory', 'analytics', 'assets', 'gdci'];
      document.addEventListener('click', function (e) {
        var link = e.target.closest('a');
        if (!link) return;
        var href = link.getAttribute('href') || '';
        var isGated = gatedPaths.some(function (p) {
          return href === '/' + p || href === p || href.indexOf('/' + p) === 0;
        });
        if (isGated) {
          e.preventDefault();
          e.stopPropagation();
          showRegistrationModal();
        }
      }, true);

      // Gate export/download/API buttons
      document.querySelectorAll('a, button').forEach(function (btn) {
        var text = (btn.textContent || '').toLowerCase();
        if (text.indexOf('export') !== -1 || text.indexOf('download pdf') !== -1 || text.indexOf('csv') !== -1 || text.indexOf('get api key') !== -1) {
          btn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            showProModal('pro');
          }, true);
        }
      });

      // Auto-show signup modal after 45 seconds of browsing
      setTimeout(function () {
        if (getUserTier() === 'anonymous' && !sessionStorage.getItem('dchub_modal_shown')) {
          sessionStorage.setItem('dchub_modal_shown', '1');
          showRegistrationModal();
        }
      }, 45000);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.DCHubGate = {
    show: showGateModal, showSignup: showRegistrationModal, showPro: function () { showProModal('pro'); },
    getUserTier: getUserTier, isLoggedIn: isLoggedIn, hasAccess: hasAccess,
    getSeatsRemaining: function () { return _seatsRemaining; },
    setTier: function (tier, days) { localStorage.setItem('dchub_session', JSON.stringify({ tier: tier, expires: new Date(Date.now() + (days || 30) * 86400000).toISOString() })); location.reload(); },
    clearTier: function () { localStorage.removeItem('dchub_session'); location.reload(); }
  };
})();
