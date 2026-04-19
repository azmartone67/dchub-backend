/**
 * DC Hub Universal Navigation v1.0
 * ══════════════════════════════════════════════════════
 * Drop-in <script src="/js/dchub-nav.js"></script> for ANY page.
 *
 * Injects: top nav, founding banner, mobile bottom nav,
 *          mobile drawer, user auth state, sign-in/out.
 *
 * USAGE:
 *   <script src="/js/dchub-nav.js"></script>
 *   That's it. Works on every page.
 *
 * CUSTOMIZATION (optional, set before loading):
 *   window.DCHUB_NAV_CONFIG = {
 *     hideFoundingBanner: true,  // hide the founding banner
 *     activePage: 'news',        // override auto-detect
 *     minimal: true              // just logo + back button
 *   };
 *
 * DYNAMIC: Reads user auth from localStorage (dchub_token, dchub_user)
 *          and shows Sign In vs user avatar/menu automatically.
 *
 * SINGLE SOURCE OF TRUTH: Edit NAV_LINKS below to change nav everywhere.
 */
(function () {
  'use strict';

  // ── API CONFIG ──────────────────────────────────────────────
  // Fetches nav config from Replit backend. Falls back to defaults below.
  var API_BASE = (window.DCHUB_NAV_CONFIG && window.DCHUB_NAV_CONFIG.apiBase) || 'https://dchub.cloud';
  var NAV_API_ENDPOINT = API_BASE + '/api/nav-config';
  var NAV_CACHE_KEY = 'dchub_nav_config';
  var NAV_CACHE_TTL = 5 * 60 * 1000; // 5 min cache

  // ── NAV LINK DEFINITIONS (defaults — overridden by API) ───
  // These are the fallback if the API is unreachable.
  // To update nav dynamically: change the /api/nav-config response in Replit.
  var NAV_LINKS = [
    { id: 'home', label: 'Home', href: '/', type: 'link' },
    {
      id: 'markets', label: 'Markets', type: 'dropdown',
      items: [
        { icon: '\uD83D\uDDFA\uFE0F', label: 'Global Markets', desc: 'Explore 140+ countries', href: '/markets/' },
        { icon: '\uD83D\uDCCA', label: 'Market Analysis', desc: 'Trends & insights', href: '/market-intelligence' },
        { icon: '\uD83D\uDCC8', label: 'Analytics', desc: 'Data dashboards', href: '/analytics' }
      ]
    },
    {
      id: 'intelligence', label: 'Intelligence', type: 'dropdown',
      items: [
        { icon: '\uD83D\uDE80', label: 'AI Pipeline', desc: 'Capacity projects', href: '/ai-pipeline', badge: 'Live' },
        { icon: '\uD83D\uDCB0', label: 'AI Deals', desc: 'M&A tracker', href: '/ai-deals', badge: 'Live' },
        { icon: '\uD83C\uDFD7\uFE0F', label: 'Asset Explorer', desc: '20,000+ facilities', href: '/assets' },
        { icon: '\uD83D\uDCE6', label: 'AI Inventory', desc: 'Supply analysis', href: '/ai-inventory' }
      ]
    },
    {
      id: 'tools', label: 'Tools', type: 'dropdown',
      items: [
        { icon: '\u26A1', label: 'Land & Power', desc: 'Site selection', href: '/land-power', badge: 'New' },
        { icon: '\uD83D\uDCB5', label: 'Transactions', desc: 'Deal flow', href: '/transactions' },
        { icon: '\u2696\uFE0F', label: 'Comps', desc: 'Side-by-side analysis', href: '/transaction-comps' },
        { icon: '\uD83D\uDCB0', label: 'Tax Incentives', desc: '50-state programs', href: '/tax-incentives', badge: 'New' },
        { icon: '\uD83D\uDD0D', label: 'Map', desc: 'Interactive map', href: '/#map-section' }
      ]
    },
    { id: 'news', label: 'News', href: '/news', type: 'link' },
    { id: 'ai-wars', label: '\u2694\uFE0F AI Wars', href: '/ai-wars', type: 'link', style: 'color:#a78bfa;font-weight:600' },
    { id: 'developers', label: 'Developers', href: '/developers', type: 'link' },
    { id: 'pricing', label: 'Pricing', href: '/pricing', type: 'link', style: 'color:var(--accent,#6366f1)' },
    {
      id: 'about', label: 'About', type: 'dropdown',
      items: [
        { icon: '\u2B50', label: 'AI Validation', desc: 'What AI says about us', href: '/testimonials', badge: 'New' },
        { icon: '\u2139\uFE0F', label: 'About DC Hub', desc: 'Our mission', href: '/about' },
        { icon: '\uD83E\uDD16', label: 'AI Agents', desc: 'Research assistant', href: '/ai-agents' },
        { icon: '\u2694\uFE0F', label: 'AI Wars', desc: 'Live AI showdowns', href: '/ai-wars', badge: 'New' },
        { icon: '\uD83C\uDF10', label: 'Ecosystem', desc: 'Vendors & partners', href: '/ecosystem' },
        { icon: '\uD83D\uDCE2', label: 'Advertise', desc: 'Sponsorship & media kit', href: '/advertise' }
      ]
    }
  ];

  // Mobile bottom nav items
  var MOBILE_NAV = [
    { label: 'Home', href: '/', icon: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>' },
    { label: 'Markets', href: '/markets/', icon: '<path d="M18 20V10M12 20V4M6 20v-6"/>' },
    { label: 'Tools', href: '/land-power', icon: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10"/>' },
    { label: 'News', href: '/news', icon: '<path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/>' },
    { label: 'More', href: '#', icon: '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/>', action: 'drawer' }
  ];

  // Mobile drawer links (full list)
  var DRAWER_LINKS = [
    { icon: '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>', label: 'Dashboard', href: '/' },
    { icon: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>', label: 'AI Validation', href: '/testimonials' },
    { icon: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/>', label: 'Land & Power', href: '/land-power' },
    { icon: '<rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/>', label: 'Transactions', href: '/transactions' },
    { icon: '<path d="M18 20V10M12 20V4M6 20v-6"/>', label: 'Analytics', href: '/analytics' },
    { icon: '<path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16"/>', label: 'News', href: '/news' },
    { icon: '<path d="M16 18l6-6-6-6"/><path d="M8 6l-6 6 6 6"/>', label: 'Developers', href: '/developers' },
    { icon: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>', label: 'API Docs', href: '/api-docs' }
  ];

  var LOGO_SVG = '<svg class="dchub-nav-logo" viewBox="0 0 24 32" fill="none"><path d="M13.5 1L2 18H11L9.5 31L22 13H12.5L13.5 1Z" fill="url(#navLogoGrad)" stroke="#818cf8" stroke-width="1"/><defs><linearGradient id="navLogoGrad" x1="12" y1="1" x2="12" y2="31" gradientUnits="userSpaceOnUse"><stop stop-color="#a5b4fc"/><stop offset="1" stop-color="#6366f1"/></linearGradient></defs></svg>';

  var CONFIG = window.DCHUB_NAV_CONFIG || {};

  // ── Detect active page ────────────────────────────────────
  function getActivePage() {
    if (CONFIG.activePage) return CONFIG.activePage;
    var path = window.location.pathname.replace(/\.html$/, '').replace(/\/$/, '') || '/';
    for (var i = 0; i < NAV_LINKS.length; i++) {
      var link = NAV_LINKS[i];
      if (link.href && link.href.replace(/\/$/, '') === path) return link.id;
      if (link.items) {
        for (var j = 0; j < link.items.length; j++) {
          if (link.items[j].href && link.items[j].href.replace(/\/$/, '') === path) return link.id;
        }
      }
    }
    return '';
  }

  // ── Auth state ────────────────────────────────────────────
  function getUser() {
    try {
      var raw = localStorage.getItem('dchub_user');
      if (!raw) return null;
      var u = JSON.parse(raw);
      return typeof u === 'object' ? u : { name: String(raw) };
    } catch (e) { return localStorage.getItem('dchub_user') ? { name: 'User' } : null; }
  }
  function isLoggedIn() { return !!localStorage.getItem('dchub_token') && !!localStorage.getItem('dchub_user'); }
  function getInitials(user) {
    if (!user || !user.name) return '\uD83D\uDC64';
    return user.name.split(' ').map(function (n) { return n[0]; }).join('').toUpperCase().slice(0, 2);
  }

  // ── Build nav HTML ────────────────────────────────────────
  function buildNavHTML() {
    var active = getActivePage();
    var user = getUser();
    var loggedIn = isLoggedIn();

    // Desktop nav links
    var linksHTML = '';
    NAV_LINKS.forEach(function (link) {
      var isActive = link.id === active;
      if (link.type === 'link') {
        var style = link.style ? ' style="' + link.style + '"' : '';
        linksHTML += '<a href="' + link.href + '" class="dchub-nav-link' + (isActive ? ' active' : '') + '"' + style + '>' + link.label + '</a>';
      } else {
        linksHTML += '<div class="dchub-nav-dropdown' + (isActive ? ' active' : '') + '">';
        linksHTML += '<button type="button" class="dchub-nav-link dchub-dropdown-trigger">' + link.label + ' <span class="dchub-caret">\u25BE</span></button>';
        linksHTML += '<div class="dchub-dropdown-menu">';
        link.items.forEach(function (item) {
          linksHTML += '<a href="' + item.href + '" class="dchub-dropdown-item">';
          linksHTML += '<span class="dchub-dd-icon">' + item.icon + '</span>';
          linksHTML += '<div><div class="dchub-dd-label">' + item.label;
          if (item.badge) linksHTML += '<span class="dchub-dd-badge">' + item.badge + '</span>';
          linksHTML += '</div><div class="dchub-dd-desc">' + item.desc + '</div></div></a>';
        });
        linksHTML += '</div></div>';
      }
    });

    // User/auth section
    var authHTML = '';
    if (loggedIn && user) {
      var avatarStyle = user.picture ? 'background-image:url(' + user.picture + ');background-size:cover;' : '';
      var avatarText = user.picture ? '' : getInitials(user);
      authHTML =
        '<div class="dchub-user-menu" id="dchub-nav-usermenu">' +
          '<div class="dchub-user-avatar" id="dchub-nav-avatar" style="' + avatarStyle + '">' + avatarText + '</div>' +
          '<div class="dchub-user-dropdown" id="dchub-nav-dropdown">' +
            '<div class="dchub-udditem" data-action="profile">\uD83D\uDC64 Profile</div>' +
            '<div class="dchub-udditem" data-action="saved">\uD83D\uDD16 Saved Searches</div>' +
            '<div class="dchub-udditem" data-action="alerts">\uD83D\uDD14 Alerts</div>' +
            '<div class="dchub-udditem" data-action="settings">\u2699\uFE0F Settings</div>' +
            '<div class="dchub-udd-divider"></div>' +
            '<div class="dchub-udditem" data-action="signout">\uD83D\uDEAA Sign Out</div>' +
          '</div>' +
        '</div>';
    } else {
      authHTML =
        '<button type="button" class="dchub-nav-signin" id="dchub-nav-signin">Sign In</button>';
    }

    // Full nav
    return (
      '<nav class="dchub-nav" id="dchub-nav">' +
        '<a href="/" class="dchub-nav-brand">' + LOGO_SVG + '<span class="dchub-nav-title">DC HUB</span></a>' +
        '<button type="button" class="dchub-hamburger" id="dchub-hamburger" aria-label="Toggle menu"><span></span><span></span><span></span></button>' +
        '<div class="dchub-nav-links" id="dchub-nav-links">' + linksHTML + '</div>' +
        '<div class="dchub-nav-right">' +
          authHTML +
          '<a href="/pricing" class="dchub-nav-cta">\uD83D\uDCB0 Get Pricing</a>' +
        '</div>' +
      '</nav>'
    );
  }

  // ── Build founding banner ─────────────────────────────────
  function buildFoundingBanner() {
    if (CONFIG.hideFoundingBanner) return '';
    return (
      '<div class="dchub-founding-banner" id="dchub-founding-banner">' +
        '<div class="dchub-founding-content">' +
          '<h4>\uD83D\uDE80 Founding Member Offer</h4>' +
          '<p>Lock in <strong>$99/month</strong> for life (normally $299)</p>' +
          '<span class="dchub-founding-timer">Limited spots remaining</span>' +
          '<a href="/pricing#founding" class="dchub-founding-cta">Claim Your Spot \u2192</a>' +
        '</div>' +
        '<button type="button" class="dchub-founding-close" id="dchub-founding-close" aria-label="Dismiss">\u00D7</button>' +
      '</div>'
    );
  }

  // ── Build mobile bottom nav ───────────────────────────────
  function buildMobileNav() {
    var path = window.location.pathname;
    var html = '<nav class="dchub-mobile-nav" id="dchub-mobile-nav"><div class="dchub-mobile-nav-inner">';
    MOBILE_NAV.forEach(function (item) {
      var isActive = item.href !== '#' && path.indexOf(item.href.replace(/\/$/, '')) === 0 && item.href !== '/';
      if (item.href === '/' && (path === '/' || path === '/index.html')) isActive = true;
      var tag = item.action ? 'button type="button"' : 'a href="' + item.href + '"';
      var closeTag = item.action ? 'button' : 'a';
      html += '<' + tag + ' class="dchub-mobnav-item' + (isActive ? ' active' : '') + '"' + (item.action ? ' data-action="' + item.action + '"' : '') + '>';
      html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' + item.icon + '</svg>';
      html += '<span>' + item.label + '</span>';
      html += '</' + closeTag + '>';
    });
    html += '</div></nav>';
    return html;
  }

  // ── Build mobile drawer ───────────────────────────────────
  function buildMobileDrawer() {
    var path = window.location.pathname;
    var html =
      '<div class="dchub-drawer-backdrop" id="dchub-drawer-backdrop"></div>' +
      '<div class="dchub-drawer" id="dchub-drawer">' +
        '<div class="dchub-drawer-header">' +
          '<div class="dchub-drawer-logo">' + LOGO_SVG + ' <span>DC HUB</span></div>' +
          '<button type="button" class="dchub-drawer-close" id="dchub-drawer-close">\u00D7</button>' +
        '</div>' +
        '<nav class="dchub-drawer-links">';
    DRAWER_LINKS.forEach(function (item) {
      var isActive = path.indexOf(item.href.replace(/\/$/, '')) === 0 && item.href !== '/';
      if (item.href === '/' && (path === '/' || path === '/index.html')) isActive = true;
      html += '<a href="' + item.href + '"' + (isActive ? ' class="active"' : '') + '>';
      html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' + item.icon + '</svg> ' + item.label + '</a>';
    });
    html += '</nav>';
    html += '<div class="dchub-drawer-section"><div class="dchub-drawer-section-title">Quick Actions</div><nav class="dchub-drawer-links">';
    html += '<a href="/pricing">\uD83D\uDCB0 Get Pricing</a>';
    if (!isLoggedIn()) {
      html += '<a href="/login.html">\uD83D\uDC64 Sign In</a>';
    } else {
      html += '<a href="#" id="dchub-drawer-signout">\uD83D\uDEAA Sign Out</a>';
    }
    html += '</nav></div></div>';
    return html;
  }

  // ── Inject CSS ────────────────────────────────────────────
  function injectStyles() {
    if (document.getElementById('dchub-nav-styles')) return;
    var s = document.createElement('style');
    s.id = 'dchub-nav-styles';
    s.textContent = [
      /* Top nav */
      '.dchub-nav{position:fixed;top:0;left:0;right:0;z-index:1000;background:rgba(10,10,18,.95);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,.06);height:64px;display:flex;align-items:center;padding:0 20px;font-family:"Inter","SF Pro Display",-apple-system,sans-serif}',
      '.dchub-nav-brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:1.25rem;text-decoration:none;color:#fff;flex-shrink:0}',
      '.dchub-nav-logo{width:32px;height:32px}',
      '.dchub-nav-title{color:#6366f1;font-weight:800}',
      '.dchub-nav-links{display:flex;gap:2px;margin-left:16px;flex:1}',
      '.dchub-nav-link{color:#94a3b8;padding:8px 12px;border-radius:6px;font-size:13px;font-weight:500;text-decoration:none;transition:.2s;border:none;background:none;cursor:pointer;font-family:inherit;white-space:nowrap}',
      '.dchub-nav-link:hover,.dchub-nav-link.active{color:#f0f4f8;background:rgba(255,255,255,.06)}',
      '.dchub-nav-right{margin-left:auto;display:flex;align-items:center;gap:8px;flex-shrink:0}',
      '.dchub-nav-signin{padding:8px 16px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:8px;color:#f0f4f8;font-size:13px;font-weight:600;cursor:pointer;transition:.2s;font-family:inherit}',
      '.dchub-nav-signin:hover{background:rgba(99,102,241,.15);border-color:#6366f1}',
      '.dchub-nav-cta{padding:8px 16px;background:#6366f1;border-radius:8px;color:#fff;font-size:13px;font-weight:600;text-decoration:none;transition:.2s;white-space:nowrap}',
      '.dchub-nav-cta:hover{background:#818cf8}',
      '.dchub-caret{font-size:10px;margin-left:2px;opacity:.5}',

      /* Dropdowns */
      '.dchub-nav-dropdown{position:relative}',
      '.dchub-dropdown-menu{display:none;position:absolute;top:100%;left:0;margin-top:8px;background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:8px;min-width:260px;box-shadow:0 16px 48px rgba(0,0,0,.4);z-index:1001}',
      '.dchub-nav-dropdown:hover .dchub-dropdown-menu,.dchub-dropdown-menu:hover{display:block}',
      '.dchub-dropdown-item{display:flex;align-items:flex-start;gap:12px;padding:10px 12px;border-radius:8px;text-decoration:none;color:#d1d5db;transition:.15s}',
      '.dchub-dropdown-item:hover{background:rgba(99,102,241,.1);color:#f0f4f8}',
      '.dchub-dd-icon{font-size:1.1rem;margin-top:2px;flex-shrink:0}',
      '.dchub-dd-label{font-size:13px;font-weight:600;display:flex;align-items:center;gap:6px}',
      '.dchub-dd-desc{font-size:11px;color:#6b7280;margin-top:2px}',
      '.dchub-dd-badge{font-size:9px;font-weight:700;padding:1px 6px;border-radius:4px;background:rgba(16,185,129,.15);color:#10b981;text-transform:uppercase;letter-spacing:.5px}',

      /* User menu */
      '.dchub-user-menu{position:relative}',
      '.dchub-user-avatar{width:36px;height:36px;border-radius:50%;background:#6366f1;display:flex;align-items:center;justify-content:center;cursor:pointer;font-weight:600;font-size:13px;color:#fff;transition:.2s}',
      '.dchub-user-avatar:hover{box-shadow:0 0 0 2px #818cf8}',
      '.dchub-user-dropdown{display:none;position:absolute;top:100%;right:0;margin-top:8px;background:#0f1119;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:8px;min-width:200px;box-shadow:0 8px 32px rgba(0,0,0,.4);z-index:1001}',
      '.dchub-user-dropdown.open{display:block}',
      '.dchub-udditem{padding:10px 12px;border-radius:6px;cursor:pointer;font-size:13px;color:#d1d5db;transition:.15s}',
      '.dchub-udditem:hover{background:rgba(99,102,241,.1);color:#f0f4f8}',
      '.dchub-udd-divider{height:1px;background:rgba(255,255,255,.06);margin:6px 0}',

      /* Hamburger */
      '.dchub-hamburger{display:none;flex-direction:column;gap:5px;background:none;border:none;cursor:pointer;padding:8px;margin-left:auto}',
      '.dchub-hamburger span{display:block;width:24px;height:2px;background:#f0f4f8;border-radius:2px;transition:.3s}',
      '.dchub-hamburger.active span:nth-child(1){transform:rotate(45deg) translate(5px,5px)}',
      '.dchub-hamburger.active span:nth-child(2){opacity:0}',
      '.dchub-hamburger.active span:nth-child(3){transform:rotate(-45deg) translate(5px,-5px)}',

      /* Founding banner */
      '.dchub-founding-banner{background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#a855f7 100%);color:#fff;padding:4px 24px;text-align:center;position:fixed;top:0;left:0;right:0;z-index:1001;height:24px;display:flex;align-items:center;justify-content:center;overflow:hidden}',
      '.dchub-founding-content{display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:nowrap}',
      '.dchub-founding-banner h4{font-size:11px;font-weight:700;margin:0;white-space:nowrap}',
      '.dchub-founding-banner p{font-size:10px;opacity:.9;margin:0;white-space:nowrap}',
      '.dchub-founding-timer{font-family:"JetBrains Mono",monospace;background:rgba(0,0,0,.2);padding:1px 8px;border-radius:4px;font-size:9px;white-space:nowrap}',
      '.dchub-founding-cta{padding:2px 10px;background:#fff;color:#6366f1;border:none;border-radius:4px;font-weight:600;cursor:pointer;white-space:nowrap;font-size:10px;text-decoration:none;transition:.2s}',
      '.dchub-founding-cta:hover{transform:scale(1.05);box-shadow:0 4px 12px rgba(0,0,0,.2)}',
      '.dchub-founding-close{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;color:#fff;font-size:1rem;cursor:pointer;opacity:.7;padding:0;line-height:1}',
      '.dchub-founding-close:hover{opacity:1}',

      /* Body offset for fixed nav */
      'body.dchub-has-banner .dchub-nav{top:24px}',
      'body.dchub-has-banner{padding-top:88px !important}',
      'body:not(.dchub-has-banner){padding-top:64px !important}',

      /* Mobile bottom nav */
      '.dchub-mobile-nav{display:none;position:fixed;bottom:0;left:0;right:0;height:64px;background:rgba(10,10,18,.98);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border-top:1px solid rgba(255,255,255,.06);z-index:999}',
      '.dchub-mobile-nav-inner{display:flex;width:100%;height:100%}',
      '.dchub-mobnav-item{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;color:#6b7280;text-decoration:none;font-size:10px;background:none;border:none;cursor:pointer;font-family:inherit;transition:color .2s}',
      '.dchub-mobnav-item svg{width:22px;height:22px}',
      '.dchub-mobnav-item.active{color:#818cf8}',

      /* Drawer */
      '.dchub-drawer-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);opacity:0;transition:opacity .3s;z-index:1100}',
      '.dchub-drawer-backdrop.visible{display:block;opacity:1}',
      '.dchub-drawer{display:none;position:fixed;top:0;left:0;width:85%;max-width:320px;height:100%;background:#0a0a12;transform:translateX(-100%);transition:transform .3s cubic-bezier(.4,0,.2,1);z-index:1101;overflow-y:auto}',
      '.dchub-drawer.open{display:block;transform:translateX(0)}',
      '.dchub-drawer-header{display:flex;align-items:center;justify-content:space-between;padding:20px}',
      '.dchub-drawer-logo{display:flex;align-items:center;gap:10px;font-size:1.25rem;font-weight:700;color:#818cf8}',
      '.dchub-drawer-logo svg{width:28px;height:28px}',
      '.dchub-drawer-close{width:40px;height:40px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:20px;color:#f0f4f8;font-size:24px;cursor:pointer}',
      '.dchub-drawer-links{padding:8px 16px}',
      '.dchub-drawer-links a{display:flex;align-items:center;gap:14px;padding:14px 16px;border-radius:12px;text-decoration:none;color:#94a3b8;font-size:15px;transition:.2s}',
      '.dchub-drawer-links a:hover,.dchub-drawer-links a.active{background:rgba(99,102,241,.15);color:#818cf8}',
      '.dchub-drawer-links a svg{width:20px;height:20px;opacity:.7;flex-shrink:0}',
      '.dchub-drawer-section{padding:16px;border-top:1px solid rgba(255,255,255,.06)}',
      '.dchub-drawer-section-title{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#4b5563;margin-bottom:8px;padding:0 16px}',

      /* Mobile responsive */
      '@media(max-width:768px){',
        '.dchub-hamburger{display:flex}',
        '.dchub-nav-links{display:none;position:absolute;top:64px;left:0;right:0;background:rgba(10,10,18,.98);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);flex-direction:column;padding:16px;border-bottom:1px solid rgba(255,255,255,.06);gap:0}',
        '.dchub-nav-links.open{display:flex}',
        '.dchub-nav-link{padding:14px 16px;font-size:16px;border-radius:8px}',
        '.dchub-dropdown-menu{position:static;margin:0;border:none;box-shadow:none;background:transparent;padding-left:16px;display:none}',
        '.dchub-nav-dropdown.open .dchub-dropdown-menu{display:block}',
        '.dchub-nav-right{display:none}',
        '.dchub-mobile-nav{display:flex !important}',
        '.dchub-drawer,.dchub-drawer-backdrop{display:block}',
        'body{padding-bottom:64px !important}',
        '.dchub-founding-banner p{display:none}',
        '.dchub-founding-banner{height:20px;padding:2px 16px}',
        'body.dchub-has-banner .dchub-nav{top:20px}',
        'body.dchub-has-banner{padding-top:84px !important}',
      '}',
      '@media(min-width:769px){.dchub-mobile-nav,.dchub-drawer,.dchub-drawer-backdrop{display:none !important}}',

      /* Hide old page navs — JS adds .dchub-old-nav to detected conflicts */
      '.dchub-old-nav{display:none !important}',

      /* Profile modal */
      '.dchub-profile-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:2000;animation:dchub-fade-in .2s ease}',
      '.dchub-profile-backdrop.active{display:flex;align-items:center;justify-content:center}',
      '@keyframes dchub-fade-in{from{opacity:0}to{opacity:1}}',
      '.dchub-profile-modal{background:#12121a;border:1px solid rgba(255,255,255,.08);border-radius:16px;width:90%;max-width:420px;box-shadow:0 24px 64px rgba(0,0,0,.5);overflow:hidden;animation:dchub-slide-up .25s ease}',
      '@keyframes dchub-slide-up{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}',
      '.dchub-profile-header{display:flex;align-items:center;justify-content:space-between;padding:20px 24px 0;margin-bottom:4px}',
      '.dchub-profile-header h3{margin:0;font-size:16px;font-weight:700;color:#f0f4f8}',
      '.dchub-profile-close{background:none;border:none;color:#6b7280;font-size:22px;cursor:pointer;padding:4px 8px;line-height:1;border-radius:6px;transition:.15s}',
      '.dchub-profile-close:hover{color:#f0f4f8;background:rgba(255,255,255,.06)}',
      '.dchub-profile-body{padding:16px 24px 24px}',
      '.dchub-profile-avatar{width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;color:#fff;margin:0 auto 16px}',
      '.dchub-profile-name{text-align:center;font-size:18px;font-weight:700;color:#f0f4f8;margin-bottom:4px}',
      '.dchub-profile-email{text-align:center;font-size:13px;color:#6b7280;margin-bottom:20px}',
      '.dchub-profile-rows{display:flex;flex-direction:column;gap:0}',
      '.dchub-profile-row{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-top:1px solid rgba(255,255,255,.06)}',
      '.dchub-profile-label{font-size:13px;color:#6b7280;font-weight:500}',
      '.dchub-profile-value{font-size:13px;color:#f0f4f8;font-weight:600}',
      '.dchub-profile-plan-badge{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:700;letter-spacing:.5px}',
      '.dchub-plan-free{background:rgba(107,114,128,.15);color:#9ca3af}',
      '.dchub-plan-pro{background:rgba(99,102,241,.15);color:#818cf8}',
      '.dchub-plan-founding{background:linear-gradient(135deg,rgba(99,102,241,.2),rgba(168,85,247,.2));color:#a78bfa}',
      '.dchub-plan-enterprise{background:rgba(16,185,129,.15);color:#34d399}',
      '.dchub-plan-loading{background:rgba(107,114,128,.1);color:#6b7280}',
      '.dchub-plan-error{background:rgba(239,68,68,.1);color:#f87171}',
      '.dchub-profile-spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.1);border-top-color:#818cf8;border-radius:50%;animation:dchub-spin .6s linear infinite;vertical-align:middle;margin-right:6px}',
      '@keyframes dchub-spin{to{transform:rotate(360deg)}}'
    ].join('\n');
    document.head.appendChild(s);
  }

  // ── Profile modal ────────────────────────────────────────
  function formatPlanLabel(plan) {
    if (!plan) return 'Unknown';
    if (plan === 'founding') return 'Founding Member';
    return plan.charAt(0).toUpperCase() + plan.slice(1);
  }

  function getPlanBadgeClass(plan) {
    if (!plan) return 'dchub-plan-loading';
    var map = { free: 'dchub-plan-free', pro: 'dchub-plan-pro', founding: 'dchub-plan-founding', enterprise: 'dchub-plan-enterprise' };
    return map[plan] || 'dchub-plan-free';
  }

  function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str || ''));
    return div.innerHTML;
  }

  function openProfileModal() {
    var existing = document.getElementById('dchub-profile-backdrop');
    if (existing) existing.remove();

    var user = getUser();
    var initials = user && user.name ? user.name.charAt(0).toUpperCase() : '\uD83D\uDC64';
    var name = (user && user.name) || 'Loading...';
    var email = (user && user.email) || '';

    var backdrop = document.createElement('div');
    backdrop.id = 'dchub-profile-backdrop';
    backdrop.className = 'dchub-profile-backdrop active';
    backdrop.innerHTML =
      '<div class="dchub-profile-modal">' +
        '<div class="dchub-profile-header"><h3>Your Profile</h3><button class="dchub-profile-close" id="dchub-profile-close-btn">&times;</button></div>' +
        '<div class="dchub-profile-body">' +
          '<div class="dchub-profile-avatar" id="dchub-profile-avatar"></div>' +
          '<div class="dchub-profile-name" id="dchub-profile-name"></div>' +
          '<div class="dchub-profile-email" id="dchub-profile-email"></div>' +
          '<div class="dchub-profile-rows">' +
            '<div class="dchub-profile-row"><span class="dchub-profile-label">Plan</span><span class="dchub-profile-value" id="dchub-profile-plan"><span class="dchub-profile-spinner"></span> Verifying...</span></div>' +
            '<div class="dchub-profile-row"><span class="dchub-profile-label">Role</span><span class="dchub-profile-value" id="dchub-profile-role">--</span></div>' +
            '<div class="dchub-profile-row"><span class="dchub-profile-label">Member Since</span><span class="dchub-profile-value" id="dchub-profile-since">--</span></div>' +
          '</div>' +
        '</div>' +
      '</div>';

    document.body.appendChild(backdrop);
    document.getElementById('dchub-profile-avatar').textContent = initials;
    document.getElementById('dchub-profile-name').textContent = name;
    document.getElementById('dchub-profile-email').textContent = email;

    document.getElementById('dchub-profile-close-btn').addEventListener('click', function () {
      backdrop.classList.remove('active');
      setTimeout(function () { backdrop.remove(); }, 200);
    });
    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop) {
        backdrop.classList.remove('active');
        setTimeout(function () { backdrop.remove(); }, 200);
      }
    });

    var token = localStorage.getItem('dchub_token');
    if (!token) {
      document.getElementById('dchub-profile-plan').innerHTML = '<span class="dchub-profile-plan-badge dchub-plan-error">Not signed in</span>';
      return;
    }

    var controller = new AbortController();
    var timeoutId = setTimeout(function () { controller.abort(); }, 8000);

    fetch(API_BASE + '/api/auth/me', {
      headers: { 'Authorization': 'Bearer ' + token },
      signal: controller.signal
    })
    .then(function (r) { clearTimeout(timeoutId); return r.json(); })
    .then(function (data) {
      if (!data || !data.success || !data.user) {
        document.getElementById('dchub-profile-plan').innerHTML = '<span class="dchub-profile-plan-badge dchub-plan-error">Unable to verify plan</span>';
        return;
      }
      var u = data.user;
      var planLabel = formatPlanLabel(u.plan);
      var planClass = getPlanBadgeClass(u.plan);
      document.getElementById('dchub-profile-plan').innerHTML = '<span class="dchub-profile-plan-badge ' + planClass + '">' + escapeHtml(planLabel) + '</span>';
      document.getElementById('dchub-profile-role').textContent = (u.role || 'user').charAt(0).toUpperCase() + (u.role || 'user').slice(1);
      document.getElementById('dchub-profile-name').textContent = u.name || u.email.split('@')[0];
      document.getElementById('dchub-profile-email').textContent = u.email || '';
      document.getElementById('dchub-profile-avatar').textContent = (u.name || u.email || 'U').charAt(0).toUpperCase();

      if (u.member_since || u.created_at) {
        var d = new Date(u.member_since || u.created_at);
        document.getElementById('dchub-profile-since').textContent = d.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
      }

      var storedUser = getUser() || {};
      if (storedUser.plan !== u.plan || storedUser.role !== u.role || storedUser.name !== u.name) {
        var merged = Object.assign({}, storedUser, u);
        localStorage.setItem('dchub_user', JSON.stringify(merged));
        console.log('[DC Hub] Profile: localStorage updated with fresh plan:', u.plan);

        var planEl = document.getElementById('user-plan');
        if (planEl) {
          planEl.textContent = u.plan === 'founding' ? 'FOUNDING MEMBER' : (u.plan || 'UNKNOWN').toUpperCase();
        }
      }
    })
    .catch(function (err) {
      clearTimeout(timeoutId);
      console.log('[DC Hub] Profile fetch failed:', err);
      document.getElementById('dchub-profile-plan').innerHTML = '<span class="dchub-profile-plan-badge dchub-plan-error">Unable to verify plan</span>';
    });
  }

  // ── Wire up events ────────────────────────────────────────
  function wireEvents() {
    // Hamburger toggle
    var hamburger = document.getElementById('dchub-hamburger');
    var navLinks = document.getElementById('dchub-nav-links');
    if (hamburger && navLinks) {
      hamburger.addEventListener('click', function () {
        hamburger.classList.toggle('active');
        navLinks.classList.toggle('open');
      });
    }

    // Mobile dropdown toggles
    document.querySelectorAll('.dchub-dropdown-trigger').forEach(function (trigger) {
      trigger.addEventListener('click', function (e) {
        if (window.innerWidth <= 768) {
          e.preventDefault();
          this.parentElement.classList.toggle('open');
        }
      });
    });

    // User avatar dropdown
    var avatar = document.getElementById('dchub-nav-avatar');
    var dropdown = document.getElementById('dchub-nav-dropdown');
    if (avatar && dropdown) {
      avatar.addEventListener('click', function (e) {
        e.stopPropagation();
        dropdown.classList.toggle('open');
      });
      document.addEventListener('click', function () { dropdown.classList.remove('open'); });
    }

    // User menu actions
    document.querySelectorAll('.dchub-udditem').forEach(function (item) {
      item.addEventListener('click', function () {
        var action = this.getAttribute('data-action');
        if (dropdown) dropdown.classList.remove('open');
        if (action === 'signout') {
          localStorage.removeItem('dchub_user');
          localStorage.removeItem('dchub_token');
          localStorage.removeItem('dchub_session');
          if (typeof google !== 'undefined' && google.accounts) google.accounts.id.disableAutoSelect();
          location.reload();
        } else if (action === 'profile') {
          openProfileModal();
        } else if (action === 'settings') {
          var sModal = document.getElementById('settings-modal') || document.getElementById('modal-settings');
          if (sModal) sModal.classList.add('active');
          else window.location.href = '/dashboard.html';
        } else if (action === 'saved') {
          var svModal = document.getElementById('saved-modal');
          if (svModal) svModal.classList.add('active');
          else window.location.href = '/dashboard.html';
        } else if (action === 'alerts') {
          var aModal = document.getElementById('alerts-modal');
          if (aModal) aModal.classList.add('active');
          else window.location.href = '/dashboard.html';
        }
      });
    });

    // Sign in button
    var signinBtn = document.getElementById('dchub-nav-signin');
    if (signinBtn) {
      signinBtn.addEventListener('click', function () {
        // Try to open existing auth modal, else redirect
        var authModal = document.getElementById('auth-modal');
        if (authModal) {
          authModal.classList.add('active');
        } else if (typeof DCHubGate !== 'undefined' && DCHubGate.showSignup) {
          DCHubGate.showSignup();
        } else {
          window.location.href = '/login.html?redirect=' + encodeURIComponent(window.location.pathname);
        }
      });
    }

    // Founding banner close
    var foundingClose = document.getElementById('dchub-founding-close');
    if (foundingClose) {
      foundingClose.addEventListener('click', function () {
        var banner = document.getElementById('dchub-founding-banner');
        if (banner) banner.style.display = 'none';
        document.body.classList.remove('dchub-has-banner');
        localStorage.setItem('dchub-founding-closed', 'true');
      });
    }

    // Mobile drawer
    var backdrop = document.getElementById('dchub-drawer-backdrop');
    var drawer = document.getElementById('dchub-drawer');
    var drawerClose = document.getElementById('dchub-drawer-close');
    function openDrawer() {
      if (drawer) drawer.classList.add('open');
      if (backdrop) backdrop.classList.add('visible');
      document.body.style.overflow = 'hidden';
    }
    function closeDrawer() {
      if (drawer) drawer.classList.remove('open');
      if (backdrop) backdrop.classList.remove('visible');
      document.body.style.overflow = '';
    }
    // "More" button in mobile nav
    document.querySelectorAll('[data-action="drawer"]').forEach(function (btn) {
      btn.addEventListener('click', openDrawer);
    });
    if (backdrop) backdrop.addEventListener('click', closeDrawer);
    if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
    // Close drawer on link click
    if (drawer) {
      drawer.querySelectorAll('a').forEach(function (link) {
        link.addEventListener('click', closeDrawer);
      });
    }
    // Drawer sign out
    var drawerSignout = document.getElementById('dchub-drawer-signout');
    if (drawerSignout) {
      drawerSignout.addEventListener('click', function (e) {
        e.preventDefault();
        localStorage.removeItem('dchub_user');
        localStorage.removeItem('dchub_token');
        localStorage.removeItem('dchub_session');
        location.reload();
      });
    }
  }

  // ── Remove existing nav if present ────────────────────────
  function removeExistingNav() {
    // Don't clobber if page opts out
    if (CONFIG.skipNavInjection) return true;
    // Remove old navs that conflict
    var existingNavs = document.querySelectorAll('nav:not(.dchub-nav):not(.dchub-mobile-nav):not(.dchub-drawer-links):not(.mobile-nav-drawer-links)');
    // We DON'T remove them — pages may have additional nav-like elements we shouldn't touch.
    // Instead, we'll just prepend ours. The old nav may still be visible on pages that
    // previously had their own nav — the page owner can remove it when ready.
    return false;
  }

  // ── Fetch nav config from Replit backend ───────────────────
  function getCachedNavConfig() {
    try {
      var raw = localStorage.getItem(NAV_CACHE_KEY);
      if (!raw) return null;
      var cached = JSON.parse(raw);
      if (Date.now() - cached.ts > NAV_CACHE_TTL) return null;
      return cached.data;
    } catch (e) { return null; }
  }

  function setCachedNavConfig(data) {
    try {
      localStorage.setItem(NAV_CACHE_KEY, JSON.stringify({ ts: Date.now(), data: data }));
    } catch (e) { /* quota exceeded, ignore */ }
  }

  function fetchNavConfig(callback) {
    // 1. Try cache first (instant)
    var cached = getCachedNavConfig();
    if (cached) {
      if (cached.links) NAV_LINKS = cached.links;
      if (cached.mobile) MOBILE_NAV = cached.mobile;
      if (cached.drawer) DRAWER_LINKS = cached.drawer;
      if (cached.founding) window._dchubFoundingData = cached.founding;
      if (cached.stats) window._dchubNavStats = cached.stats;
      callback();
      // Still refresh in background
      fetchFromAPI(false);
      return;
    }
    // 2. No cache — init with defaults immediately, then update async
    callback();
    fetchFromAPI(true);
  }

  function fetchFromAPI(shouldRefresh) {
    var controller = new AbortController();
    var timeoutId = setTimeout(function () { controller.abort(); }, 3000);
    fetch(NAV_API_ENDPOINT, { signal: controller.signal })
      .then(function (r) { clearTimeout(timeoutId); return r.json(); })
      .then(function (data) {
        if (data && data.success) {
          var config = {};
          if (data.links) { NAV_LINKS = data.links; config.links = data.links; }
          if (data.mobile) { MOBILE_NAV = data.mobile; config.mobile = data.mobile; }
          if (data.drawer) { DRAWER_LINKS = data.drawer; config.drawer = data.drawer; }
          if (data.founding) { config.founding = data.founding; window._dchubFoundingData = data.founding; }
          if (data.stats) { config.stats = data.stats; window._dchubNavStats = data.stats; }
          setCachedNavConfig(config);
          // Refresh nav if we got new data and UI is already rendered
          if (shouldRefresh && document.getElementById('dchub-nav')) {
            var wrapper = document.getElementById('dchub-nav-wrapper');
            if (wrapper) wrapper.remove();
            document.body.classList.remove('dchub-has-banner');
            initDOM();
          }
        }
      })
      .catch(function () { clearTimeout(timeoutId); /* API down, use defaults */ });
  }

  // ── Hide old page-specific navs ─────────────────────────────
  function hideOldNavs() {
    // Target: any <nav> or <header> that is a direct child of body or a top-level wrapper,
    // is NOT part of our injected nav, and appears to be a page-level navigation.
    var ours = document.getElementById('dchub-nav-wrapper');
    if (!ours) return;

    // Direct body children: nav, header
    Array.prototype.forEach.call(document.body.children, function (el) {
      if (el === ours) return;
      if (el.id === 'dchub-nav-wrapper') return;
      var tag = el.tagName;
      if (tag === 'NAV' || tag === 'HEADER') {
        el.classList.add('dchub-old-nav');
      }
    });

    // Also find navs with position:fixed/sticky that are near the top (competing navs)
    document.querySelectorAll('nav, header, [role="navigation"]').forEach(function (el) {
      if (el.closest('#dchub-nav-wrapper')) return;  // skip ours
      if (el.closest('.dchub-drawer')) return;  // skip our drawer
      var style = window.getComputedStyle(el);
      if (style.position === 'fixed' || style.position === 'sticky') {
        var rect = el.getBoundingClientRect();
        // If it's at the top of the page (nav bar position), hide it
        if (rect.top < 100) {
          el.classList.add('dchub-old-nav');
        }
      }
    });

    // Also hide old mobile bottom navs (not ours)
    document.querySelectorAll('.mobile-bottom-nav, #mobile-bottom-nav').forEach(function (el) {
      if (!el.closest('#dchub-nav-wrapper')) el.classList.add('dchub-old-nav');
    });

    // Hide old mobile drawers (not ours)
    document.querySelectorAll('.mobile-nav-drawer, #mobile-nav-drawer, .mobile-nav-drawer-backdrop, #mobile-nav-backdrop').forEach(function (el) {
      if (!el.closest('#dchub-nav-wrapper')) el.classList.add('dchub-old-nav');
    });

    var hidden = document.querySelectorAll('.dchub-old-nav').length;
    if (hidden > 0) console.log('[DC Hub] Nav: hid ' + hidden + ' old nav element(s)');
  }

  // ── Init DOM (separated for re-render) ────────────────────
  function initDOM() {
    if (document.getElementById('dchub-nav')) return;
    var navHTML = buildNavHTML();
    var bannerHTML = buildFoundingBanner();
    var mobileHTML = buildMobileNav();
    var drawerHTML = buildMobileDrawer();
    var wrapper = document.createElement('div');
    wrapper.id = 'dchub-nav-wrapper';
    wrapper.innerHTML = bannerHTML + navHTML + mobileHTML + drawerHTML;
    document.body.insertBefore(wrapper, document.body.firstChild);
    var showBanner = !localStorage.getItem('dchub-founding-closed') && !CONFIG.hideFoundingBanner;
    if (showBanner) {
      document.body.classList.add('dchub-has-banner');
      // Update founding spots from API data
      if (window._dchubFoundingData) {
        var timer = document.querySelector('.dchub-founding-timer');
        if (timer && window._dchubFoundingData.remaining) {
          timer.textContent = window._dchubFoundingData.remaining + ' of ' + (window._dchubFoundingData.total || 50) + ' spots left';
        }
      }
    } else {
      var banner = document.getElementById('dchub-founding-banner');
      if (banner) banner.style.display = 'none';
    }
    wireEvents();

    // Hide old page-specific navs that conflict
    hideOldNavs();
  }

  // ── Init ──────────────────────────────────────────────────
  function init() {
    // Don't inject on login/signup pages
    var path = window.location.pathname;
    if (path.indexOf('login') > -1 || path.indexOf('signup') > -1 || path.indexOf('forgot-password') > -1 || path.indexOf('reset-password') > -1) {
      return;
    }

    injectStyles();

    // Check if we already injected
    if (document.getElementById('dchub-nav')) return;

    // Fetch nav config from Replit (or use cache/defaults), then render
    fetchNavConfig(function () {
      initDOM();
      console.log('[DC Hub] Nav v1.0 loaded | Page: ' + getActivePage() + ' | Logged in: ' + isLoggedIn() + ' | API: ' + NAV_API_ENDPOINT);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  // Public API
  window.DCHubNav = {
    refresh: function () {
      var wrapper = document.getElementById('dchub-nav-wrapper');
      if (wrapper) wrapper.remove();
      init();
    },
    getActivePage: getActivePage,
    isLoggedIn: isLoggedIn
  };
})();
