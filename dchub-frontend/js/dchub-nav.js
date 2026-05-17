/**
 * DC Hub Universal Navigation v2.6
 * ══════════════════════════════════════════════════════
 * Single source of truth for nav across all DC Hub pages.
 * Drop in with: <script src="/js/dchub-nav.js"></script>
 *
 * v2.6 changes:
 * - Research: +Grid Intelligence (NEW badge) — /research/grid-intelligence
 * - Active path detection: grid-intelligence + ercot-batch-zero → Research
 * - Mobile drawer: Grid Intelligence added as first Research item
 *
 * v2.5 changes:
 * - Restored 11 missing pages dropped during v2.2 restructure
 * - Maps: +Compare, +Land & Power landing
 * - Intelligence: +AI Pipeline, +AI Deals, +AI Inventory, +Comps, +Construction Pipeline, +Analytics
 * - Platform: +Developers
 * - Research: +Testimonials
 * - About dropdown added: About, Advertise
 *
 * v2.2 changes:
 * - Maps is now the FIRST nav item (most important for new visitors)
 * - Facility Map correctly links to /map (not #map-section anchor)
 * - Land & Power correctly links to /land-power-map (full tool, not lite)
 * - NEW badge on Facility Map, PRO badge on Land & Power, LIVE on AI Wars
 * - Intelligence dropdown cleaned up (no duplicates)
 * - Tools renamed to Platform — cleaner for API/AI/ecosystem group
 * - AI Wars removed from duplicate About dropdown
 * - Mobile bottom nav updated: Home/Facility/L&P/Rankings/News
 */
(function () {
  'use strict';

  // ── NAV LINKS ─────────────────────────────────────────────
  // Single source of truth — edit here, updates all pages.
  // Map links corrected: /map = Facility Map, /land-power-map = full L&P tool
  var NAV_LINKS = [

    // ── MAPS — top-level, most important for new visitors ─────
    {
      id: 'maps', label: 'Maps', type: 'dropdown',
      items: [
        {
          label: 'Facility Map',
          href: '/map',
          desc: '11,361 global facilities · satellite view',
          badge: 'NEW'
        },
        {
          label: 'Land & Power',
          href: '/land-power-map',
          desc: 'Substations · fiber · gas · nuclear · full infrastructure',
          badge: 'PRO'
        },
        { label: 'Compare',            href: '/compare',              desc: 'Side-by-side facility comparison' },
        { label: 'Site Selection',      href: '/land-power',           desc: 'Greenfield site selection guide' },
      ]
    },

    // ── INTELLIGENCE — data & market analysis ─────────────────
    {
      id: 'intelligence', label: 'Intelligence', type: 'dropdown',
      items: [
        { label: 'Daily Report',         href: '/daily',                  desc: 'State-by-state infographic · updated daily', badge: 'NEW' },
        { label: 'Market Analytics',     href: '/market-intelligence',    desc: '44 markets · vacancy & pricing' },
        { label: 'Rankings',             href: '/rankings',               desc: 'Power · fiber · gas · construction' },
        { label: 'Capacity Pipeline',    href: '/capacity-pipeline',      desc: '540 projects · 369 GW tracked' },
        { label: 'Construction',         href: '/construction-pipeline',  desc: 'Active builds & permits' },
        { label: 'Transactions',         href: '/transactions',           desc: 'M&A deal database · $70B+ volume' },
        { label: 'Total Power',          href: '/dcpi/totals',            desc: 'Operating + being-built MW',         badge: 'NEW' },
        { label: 'Comps',                href: '/transaction-comps',      desc: 'Side-by-side deal analysis' },
        { label: 'AI Pipeline',          href: '/ai-pipeline',            desc: 'AI/ML capacity projects',          badge: 'LIVE' },
        { label: 'AI Deals',             href: '/ai-deals',               desc: 'AI infrastructure M&A',            badge: 'LIVE' },
        { label: 'AI Inventory',         href: '/ai-inventory',           desc: 'GPU & capacity supply' },
        { label: 'Powered Shell',        href: '/powered-shell',          desc: '$/sf/yr lease rates + pipeline',   badge: 'NEW' },
        { label: 'Analytics',            href: '/analytics',              desc: 'Data dashboards & trends' },
        { label: 'Markets',              href: '/markets/',               desc: '60 global market profiles' },
        { label: 'Tax Incentives',       href: '/tax-incentives',         desc: '50-state incentive programs' },
        { label: 'News',                 href: '/news',                   desc: 'Live industry news feed' },
      ]
    },

    // ── PLATFORM — API, AI, ecosystem ────────────────────────
    {
      id: 'platform', label: 'Platform', type: 'dropdown',
      items: [
        { label: 'API & MCP',          href: '/api-docs',             desc: 'REST API · MCP server · AI agents' },
        { label: 'Developers',         href: '/developers',           desc: 'SDKs, guides & integration docs' },
        { label: 'AI Integrations',    href: '/ai-integrations',      desc: 'MCP status · platform connections' },
        { label: 'AI Hub',             href: '/ai',                   desc: 'Claude · ChatGPT · Gemini landing' },
        { label: 'AI Wars',            href: '/ai-wars',              desc: 'Live AI platform benchmarks',       badge: 'LIVE' },
        { label: 'Ecosystem',          href: '/ecosystem',            desc: '76 vendors & partners' },
        { label: 'Assets Explorer',    href: '/assets',               desc: '20,000+ searchable facilities' },
      ]
    },

    // ── RESEARCH — press, GDCI, benchmarks, resources ────────
    {
      id: 'research', label: 'Research', type: 'dropdown',
      items: [
        { label: 'Grid Intelligence', href: '/research/grid-intelligence', desc: 'ISO transmission & site intel', badge: 'NEW' },
        { label: 'Press Releases',    href: '/press',           desc: 'Media kit & company news',     badge: 'NEW' },
        { label: 'GDCI',              href: '/gdci',            desc: 'Global Data Center Index',     badge: 'NEW' },
        { label: 'AI Validation',     href: '/testimonials',    desc: 'What AI says about DC Hub' },
        { label: 'vs Competitors',    href: '/vs',              desc: 'Static research vs live MCP',    badge: 'NEW' },
        { label: 'Live Pulse',        href: '/intelligence',    desc: 'Platform health · auto-refresh 60s', badge: 'NEW' },
        { label: 'Announcements',     href: '/announcements',   desc: 'Product updates & releases' },
        { label: 'Architecture',      href: '/architecture',    desc: 'Platform design & roadmap' },
      ]
    },

    // ── TOP-LEVEL LINKS ───────────────────────────────────────
    { id: 'pricing', label: 'Pricing', type: 'link', href: '/pricing' },

    // ── ABOUT — company info ────────────────────────────────
    {
      id: 'about', label: 'About', type: 'dropdown',
      items: [
        { label: 'About DC Hub',       href: '/about',            desc: 'Our mission & team' },
        { label: 'Advertise',          href: '/advertise',        desc: 'Sponsorship & media kit' },
        { label: 'FAQ',                href: '/faq',              desc: 'Frequently asked questions' },
        { label: 'Glossary',           href: '/glossary',         desc: 'Industry terminology' },
      ]
    },

    { id: 'news',    label: 'News',    type: 'link', href: '/news',    style: 'display:none' }, // hidden on desktop, in mobile nav
  ];

  // ── MOBILE BOTTOM NAV — 5 most important destinations ───────
  var MOBILE_NAV = [
    { icon: 'M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',           label: 'Home',      href: '/' },
    { icon: 'M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z',            label: 'Facility',  href: '/map' },
    { icon: 'M1 6l5 5 5-5M1 6h12M7 1v10',                                 label: 'L&P Map',   href: '/land-power-map' },
    { icon: 'M18 20V10M12 20V4M6 20v-6',                                   label: 'Rankings',  href: '/rankings' },
    { icon: 'M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z',label: 'News',     href: '/news' },
  ];

  var CONFIG = window.DCHUB_NAV_CONFIG || {};

  // ── AUTH ─────────────────────────────────────────────────
  function getUser() {
    try {
      var raw = localStorage.getItem('dchub_user');
      if (!raw) return null;
      var u = JSON.parse(raw);
      return typeof u === 'object' ? u : { name: String(raw) };
    } catch(e) { return null; }
  }
  function isLoggedIn() {
    return !!localStorage.getItem('dchub_token') && !!localStorage.getItem('dchub_user');
  }
  function getUserPlan() {
    try {
      var s = JSON.parse(localStorage.getItem('dchub_session') || '{}');
      return (s.plan || s.tier || 'free').toLowerCase();
    } catch(e) { return 'free'; }
  }
  function planRank(p) {
    return {free:0,developer:1,pro:2,enterprise:3}[(p||'free').toLowerCase()] || 0;
  }
  function getInitials(user) {
    if (!user || !user.name) return '?';
    return user.name.split(' ').map(function(n){ return n[0]; }).join('').toUpperCase().slice(0,2);
  }

  // ── ACTIVE PAGE ──────────────────────────────────────────
  function getActivePage() {
    if (CONFIG.activePage) return CONFIG.activePage;
    var path = window.location.pathname.replace(/\.html$/, '').replace(/\/$/, '') || '/';
    for (var i = 0; i < NAV_LINKS.length; i++) {
      var link = NAV_LINKS[i];
      if (link.href && link.href === path) return link.id;
      if (link.items) {
        for (var j = 0; j < link.items.length; j++) {
          if (link.items[j].href && link.items[j].href === path) return link.id;
        }
      }
    }
    // Special cases
    if (path === '/map')                   return 'maps';
    if (path.includes('land-power'))       return 'maps';
    if (path.includes('ranking'))          return 'intelligence';
    if (path.includes('pipeline'))         return 'intelligence';
    if (path.includes('transaction'))      return 'intelligence';
    if (path.includes('tax'))              return 'intelligence';
    if (path.includes('api') || path.includes('ai') || path.includes('ecosystem') || path.includes('assets') || path.includes('developer')) return 'platform';
    if (path.includes('press') || path.includes('gdci') || path.includes('announcement') || path.includes('architecture') || path.includes('testimonial') || path.includes('grid-intelligence') || path.includes('ercot-batch-zero')) return 'research';
    if (path.includes('about') || path.includes('advertise') || path.includes('faq') || path.includes('glossary')) return 'about';
    if (path.includes('compare')) return 'maps';
    if (path.includes('analytic') || path.includes('inventory') || path.includes('deal') || path.includes('construction')) return 'intelligence';
    return '';
  }

  // ── BUILD NAV HTML ───────────────────────────────────────
  function buildNavHTML() {
    var active = getActivePage();
    var user = getUser();
    var loggedIn = isLoggedIn();
    var plan = getUserPlan();
    var rank = planRank(plan);
    var isDev = rank >= 1;

    // Desktop links
    var linksHTML = '';
    NAV_LINKS.forEach(function(link) {
      var isActive = link.id === active;
      if (link.type === 'link') {
        linksHTML += '<a href="' + link.href + '" class="dchub-nav-link' + (isActive ? ' active' : '') + '">' + link.label + '</a>';
      } else {
        linksHTML += '<div class="dchub-nav-dropdown' + (isActive ? ' active' : '') + '">';
        linksHTML += '<button class="dchub-nav-link dchub-nav-dropbtn">' + link.label;
        if (link.id === 'maps') linksHTML += ' <span class="dchub-nav-new">NEW</span>';
        linksHTML += ' <svg width="10" height="6" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg></button>';
        linksHTML += '<div class="dchub-nav-dropmenu">';
        link.items.forEach(function(item) {
          var badgeHtml = '';
          if (item.badge === 'LIVE') badgeHtml = '<span class="dchub-nav-badge live">LIVE</span>';
          if (item.badge === 'PRO')  badgeHtml = '<span class="dchub-nav-badge pro">PRO</span>';
          if (item.badge === 'NEW')  badgeHtml = '<span class="dchub-nav-badge new-badge">NEW</span>';
          linksHTML += '<a href="' + item.href + '" class="dchub-nav-dropitem">';
          linksHTML += '<span class="dchub-nav-dropitem-label">' + item.label + badgeHtml + '</span>';
          if (item.desc) linksHTML += '<span class="dchub-nav-dropitem-desc">' + item.desc + '</span>';
          linksHTML += '</a>';
        });
        linksHTML += '</div></div>';
      }
    });

    // Right side — plan badge + auth
    var planColors = {free:'#3d4a5c',developer:'#00c8f0',pro:'#00d98a',enterprise:'#9f7afa'};
    var planLabels = {free:'Free',developer:'Developer',pro:'Pro',enterprise:'Enterprise'};
    var planColor = planColors[plan] || planColors.free;
    var planLabel = planLabels[plan] || 'Free';

    var rightHTML = '';

    // Plan badge (always shown when logged in)
    if (loggedIn) {
      rightHTML += '<span class="dchub-plan-badge" style="color:' + planColor + ';border-color:' + planColor + '33;background:' + planColor + '11">' + planLabel + '</span>';
    }

    // Upgrade CTA for free users
    if (loggedIn && !isDev) {
      rightHTML += '<a href="/pricing" class="dchub-nav-upgrade">Upgrade $99/mo</a>';
    }

    if (!loggedIn) {
      rightHTML += '<a href="/pricing" class="dchub-nav-link" style="color:#6b7a94">Pricing</a>';
      rightHTML += '<a href="/login.html" class="dchub-nav-signin">Sign in</a>';
      rightHTML += '<a href="/pricing" class="dchub-nav-upgrade">Get started</a>';
    } else {
      var initials = getInitials(user);
      rightHTML += '<div class="dchub-nav-user-wrap">';
      rightHTML += '<button class="dchub-nav-avatar" id="dchub-avatar-btn">' + initials + '</button>';
      rightHTML += '<div class="dchub-nav-user-menu" id="dchub-user-menu">';
      if (user && user.email) rightHTML += '<div class="dchub-user-email">' + user.email + '</div>';
      rightHTML += '<a href="/dashboard.html">Dashboard</a>';
      rightHTML += '<a href="/dashboard.html#api-keys">API Keys</a>';
      rightHTML += '<a href="/pricing">Plan: ' + planLabel + '</a>';
      rightHTML += '<div class="dchub-user-divider"></div>';
      rightHTML += '<a href="#" id="dchub-signout-btn">Sign out</a>';
      rightHTML += '</div></div>';
    }

    // Hamburger
    rightHTML += '<button class="dchub-nav-hamburger" id="dchub-hamburger" aria-label="Menu">';
    rightHTML += '<svg width="20" height="20" viewBox="0 0 20 20"><path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
    rightHTML += '</button>';

    return '<nav class="dchub-nav" id="dchub-nav">' +
      '<a class="dchub-nav-brand" href="/">' +
        '<svg class="dchub-nav-logo" viewBox="0 0 24 32" fill="none">' +
          '<path d="M13.5 1L2 18H11L9.5 31L22 13H12.5L13.5 1Z" fill="url(#navBoltGrad)" stroke="#00c8f0" stroke-width="1"/>' +
          '<defs><linearGradient id="navBoltGrad" x1="12" y1="1" x2="12" y2="31" gradientUnits="userSpaceOnUse"><stop stop-color="#5ce0ff"/><stop offset="1" stop-color="#0077aa"/></linearGradient></defs>' +
        '</svg>' +
        '<span class="dchub-nav-title">DC Hub</span>' +
        '<span class="dchub-nav-sub">Intelligence</span>' +
      '</a>' +
      '<div class="dchub-nav-links">' + linksHTML + '</div>' +
      '<div class="dchub-nav-right">' + rightHTML + '</div>' +
    '</nav>';
  }

  // ── MOBILE DRAWER ────────────────────────────────────────
  function buildDrawerHTML() {
    var loggedIn = isLoggedIn();
    var plan = getUserPlan();
    var isDev = planRank(plan) >= 1;

    var html = '<div class="dchub-drawer-overlay" id="dchub-drawer-overlay"></div>';
    html += '<div class="dchub-drawer" id="dchub-drawer">';
    html += '<div class="dchub-drawer-head">';
    html += '<a href="/" class="dchub-nav-brand" style="text-decoration:none">';
    html += '<span style="font-weight:700;font-size:15px;color:#fff">DC Hub</span>';
    html += '<span style="font-size:11px;color:#3d4a5c;margin-left:3px">Intelligence</span>';
    html += '</a>';
    html += '<button class="dchub-drawer-close" id="dchub-drawer-close">×</button>';
    html += '</div>';

    // Maps section — prominent
    // Maps — first and most prominent
    html += '<div class="dchub-drawer-section">';
    html += '<div class="dchub-drawer-section-title">Maps</div>';
    html += '<a href="/map" class="dchub-drawer-item"><span>Facility Map</span><span class="dchub-nav-badge new-badge">NEW</span></a>';
    html += '<a href="/land-power-map" class="dchub-drawer-item"><span>Land & Power</span><span class="dchub-nav-badge pro">PRO</span></a>';
    html += '<a href="/compare" class="dchub-drawer-item">Compare</a>';
    html += '<a href="/land-power" class="dchub-drawer-item">Site Selection</a>';
    html += '</div>';

    html += '<div class="dchub-drawer-section">';
    html += '<div class="dchub-drawer-section-title">Intelligence</div>';
    html += '<a href="/market-intelligence" class="dchub-drawer-item">Markets</a>';
    html += '<a href="/rankings" class="dchub-drawer-item">Rankings</a>';
    html += '<a href="/capacity-pipeline" class="dchub-drawer-item">Capacity Pipeline</a>';
    html += '<a href="/construction-pipeline" class="dchub-drawer-item">Construction</a>';
    html += '<a href="/transactions" class="dchub-drawer-item">Transactions</a>';
    html += '<a href="/transaction-comps" class="dchub-drawer-item">Comps</a>';
    html += '<a href="/ai-pipeline" class="dchub-drawer-item"><span>AI Pipeline</span><span class="dchub-nav-badge live">LIVE</span></a>';
    html += '<a href="/ai-deals" class="dchub-drawer-item"><span>AI Deals</span><span class="dchub-nav-badge live">LIVE</span></a>';
    html += '<a href="/ai-inventory" class="dchub-drawer-item">AI Inventory</a>';
    html += '<a href="/analytics" class="dchub-drawer-item">Analytics</a>';
    html += '<a href="/tax-incentives" class="dchub-drawer-item">Tax Incentives</a>';
    html += '<a href="/dcpi/totals" class="dchub-drawer-item"><span>Total Power</span><span class="dchub-nav-badge new-badge">NEW</span></a>';
    html += '<a href="/news" class="dchub-drawer-item">News</a>';
    html += '</div>';

    html += '<div class="dchub-drawer-section">';
    html += '<div class="dchub-drawer-section-title">Platform</div>';
    html += '<a href="/api-docs" class="dchub-drawer-item">API & MCP</a>';
    html += '<a href="/developers" class="dchub-drawer-item">Developers</a>';
    html += '<a href="/ai" class="dchub-drawer-item">AI Integration</a>';
    html += '<a href="/ai-wars" class="dchub-drawer-item"><span>AI Wars</span><span class="dchub-nav-badge live">LIVE</span></a>';
    html += '<a href="/assets" class="dchub-drawer-item">Assets Explorer</a>';
    html += '</div>';

    html += '<div class="dchub-drawer-section">';
    html += '<div class="dchub-drawer-section-title">Research</div>';
    html += '<a href="/research/grid-intelligence" class="dchub-drawer-item"><span>Grid Intelligence</span><span class="dchub-nav-badge new-badge">NEW</span></a>';
    html += '<a href="/press" class="dchub-drawer-item"><span>Press Releases</span><span class="dchub-nav-badge new-badge">NEW</span></a>';
    html += '<a href="/gdci" class="dchub-drawer-item"><span>GDCI</span><span class="dchub-nav-badge new-badge">NEW</span></a>';
    html += '<a href="/testimonials" class="dchub-drawer-item">AI Validation</a>';
    html += '<a href="/vs" class="dchub-drawer-item"><span>vs Competitors</span><span class="dchub-nav-badge new-badge">NEW</span></a>';
    html += '<a href="/intelligence" class="dchub-drawer-item"><span>Live Pulse</span><span class="dchub-nav-badge new-badge">NEW</span></a>';
    html += '<a href="/announcements" class="dchub-drawer-item">Announcements</a>';
    html += '<a href="/architecture" class="dchub-drawer-item">Architecture</a>';
    html += '</div>';

    html += '<div class="dchub-drawer-section">';
    html += '<div class="dchub-drawer-section-title">About</div>';
    html += '<a href="/about" class="dchub-drawer-item">About DC Hub</a>';
    html += '<a href="/advertise" class="dchub-drawer-item">Advertise</a>';
    html += '<a href="/faq" class="dchub-drawer-item">FAQ</a>';
    html += '</div>';

    html += '<div class="dchub-drawer-section">';
    if (!loggedIn) {
      html += '<a href="/login.html" class="dchub-drawer-cta">Sign in</a>';
      html += '<a href="/pricing" class="dchub-drawer-cta secondary">Get started</a>';
    } else if (!isDev) {
      html += '<a href="/pricing" class="dchub-drawer-cta">Upgrade → $99/mo</a>';
      html += '<a href="#" id="dchub-drawer-signout" class="dchub-drawer-item">Sign out</a>';
    } else {
      html += '<a href="/dashboard.html" class="dchub-drawer-item">Dashboard</a>';
      html += '<a href="#" id="dchub-drawer-signout" class="dchub-drawer-item">Sign out</a>';
    }
    html += '</div>';
    html += '</div>';
    return html;
  }

  // ── MOBILE BOTTOM NAV ────────────────────────────────────
  function buildBottomNavHTML() {
    var path = window.location.pathname;
    var html = '<nav class="dchub-bottom-nav">';
    MOBILE_NAV.forEach(function(item) {
      var isActive = path === item.href || (item.href !== '/' && path.startsWith(item.href));
      html += '<a href="' + item.href + '" class="dchub-bottom-item' + (isActive ? ' active' : '') + '">';
      html += '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="' + item.icon + '"/></svg>';
      html += '<span>' + item.label + '</span>';
      html += '</a>';
    });
    html += '</nav>';
    return html;
  }

  // ── CSS ──────────────────────────────────────────────────
  function injectStyles() {
    if (document.getElementById('dchub-nav-styles')) return;
    var s = document.createElement('style');
    s.id = 'dchub-nav-styles';
    s.textContent = `
.dchub-nav{position:fixed;top:0;left:0;right:0;z-index:2000;background:rgba(8,11,16,.96);backdrop-filter:blur(12px);border-bottom:1px solid rgba(255,255,255,.07);height:48px;display:flex;align-items:center;padding:0 16px;gap:0;font-family:'IBM Plex Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.dchub-nav-brand{display:flex;align-items:center;gap:8px;text-decoration:none;flex-shrink:0;padding-right:12px;border-right:1px solid rgba(255,255,255,.07);margin-right:8px}
.dchub-nav-logo{width:20px;height:26px}
.dchub-nav-title{font-size:14px;font-weight:700;color:#cdd6e8;letter-spacing:-.3px}
.dchub-nav-sub{font-size:10px;color:#3d4a5c;margin-left:2px;font-weight:300}
.dchub-nav-links{display:flex;align-items:center;gap:1px;flex:1}
.dchub-nav-link{color:#6b7a94;padding:6px 11px;border-radius:5px;font-size:12px;font-weight:500;text-decoration:none;transition:all .15s;background:none;border:none;cursor:pointer;font-family:inherit;white-space:nowrap;display:flex;align-items:center;gap:5px;height:32px}
.dchub-nav-link:hover,.dchub-nav-link.active,.dchub-nav-dropdown.active>.dchub-nav-dropbtn{color:#cdd6e8;background:rgba(255,255,255,.05)}
.dchub-nav-right{margin-left:auto;display:flex;align-items:center;gap:7px;flex-shrink:0}
.dchub-nav-signin{padding:5px 12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:5px;color:#cdd6e8;font-size:12px;font-weight:500;cursor:pointer;text-decoration:none;font-family:inherit;transition:all .15s}
.dchub-nav-signin:hover{background:rgba(255,255,255,.1)}
.dchub-nav-upgrade{padding:5px 12px;background:#00c8f0;border:none;border-radius:5px;color:#000;font-size:12px;font-weight:700;cursor:pointer;text-decoration:none;font-family:inherit;transition:opacity .15s;white-space:nowrap}
.dchub-nav-upgrade:hover{opacity:.85}
.dchub-plan-badge{font-size:10px;font-family:'IBM Plex Mono',monospace;padding:2px 7px;border-radius:3px;border:1px solid;white-space:nowrap}
.dchub-nav-new{font-size:8px;font-weight:800;background:#00c8f0;color:#000;padding:1px 4px;border-radius:3px;margin-left:3px;vertical-align:middle;letter-spacing:.5px}
.dchub-nav-badge{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;margin-left:5px;vertical-align:middle;letter-spacing:.4px}
.dchub-nav-badge.live{background:rgba(0,217,138,.15);color:#00d98a;border:1px solid rgba(0,217,138,.25)}
.dchub-nav-badge.pro{background:rgba(0,200,240,.12);color:#00c8f0;border:1px solid rgba(0,200,240,.22)}
.dchub-nav-badge.new-badge{background:rgba(255,165,0,.12);color:#f0a020;border:1px solid rgba(255,165,0,.22)}

/* Dropdowns */
.dchub-nav-dropdown{position:relative}
.dchub-nav-dropbtn svg{opacity:.5;transition:transform .15s}
.dchub-nav-dropdown:hover .dchub-nav-dropbtn svg,.dchub-nav-dropdown.open .dchub-nav-dropbtn svg{transform:rotate(180deg);opacity:1}
.dchub-nav-dropmenu{position:absolute;top:calc(100% + 8px);left:0;background:#0d1219;border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:5px;min-width:220px;max-height:calc(100vh - 80px);overflow-y:auto;opacity:0;visibility:hidden;transform:translateY(-6px);transition:all .15s;z-index:100;box-shadow:0 12px 40px rgba(0,0,0,.6)}
.dchub-nav-dropdown:hover .dchub-nav-dropmenu{opacity:1;visibility:visible;transform:translateY(0)}
.dchub-nav-dropitem{display:flex;flex-direction:column;padding:8px 10px;border-radius:5px;text-decoration:none;transition:background .15s;gap:2px}
.dchub-nav-dropitem:hover{background:rgba(255,255,255,.06)}
.dchub-nav-dropitem-label{font-size:12px;font-weight:500;color:#cdd6e8;display:flex;align-items:center}
.dchub-nav-dropitem-desc{font-size:10px;color:#3d4a5c;font-family:'IBM Plex Mono',monospace}

/* User avatar + menu */
.dchub-nav-user-wrap{position:relative}
.dchub-nav-avatar{width:30px;height:30px;border-radius:50%;background:rgba(0,200,240,.15);border:1px solid rgba(0,200,240,.3);color:#00c8f0;font-size:11px;font-weight:700;cursor:pointer;font-family:'IBM Plex Mono',monospace;transition:all .15s}
.dchub-nav-avatar:hover{background:rgba(0,200,240,.25)}
.dchub-nav-user-menu{position:absolute;top:calc(100% + 8px);right:0;background:#0d1219;border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:5px;min-width:180px;opacity:0;visibility:hidden;transform:translateY(-6px);transition:all .15s;z-index:100;box-shadow:0 12px 40px rgba(0,0,0,.6)}
.dchub-nav-user-menu.open{opacity:1;visibility:visible;transform:translateY(0)}
.dchub-nav-user-menu a{display:block;padding:7px 10px;font-size:12px;color:#cdd6e8;text-decoration:none;border-radius:5px;transition:background .15s}
.dchub-nav-user-menu a:hover{background:rgba(255,255,255,.06)}
.dchub-user-email{font-size:10px;color:#3d4a5c;padding:6px 10px;font-family:'IBM Plex Mono',monospace;border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:4px}
.dchub-user-divider{height:1px;background:rgba(255,255,255,.06);margin:4px 0}

/* Hamburger */
.dchub-nav-hamburger{display:none;background:none;border:none;color:#6b7a94;cursor:pointer;padding:4px;transition:color .15s}
.dchub-nav-hamburger:hover{color:#cdd6e8}

/* Drawer */
.dchub-drawer-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:2999;opacity:0;visibility:hidden;transition:all .2s;backdrop-filter:blur(4px)}
.dchub-drawer-overlay.open{opacity:1;visibility:visible}
.dchub-drawer{position:fixed;top:0;right:0;bottom:0;width:280px;background:#0d1219;border-left:1px solid rgba(255,255,255,.08);z-index:3000;transform:translateX(100%);transition:transform .25s ease;overflow-y:auto;padding-bottom:80px}
.dchub-drawer.open{transform:translateX(0)}
.dchub-drawer-head{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid rgba(255,255,255,.07)}
.dchub-drawer-close{background:none;border:none;color:#6b7a94;font-size:22px;cursor:pointer;line-height:1;padding:2px;transition:color .15s}
.dchub-drawer-close:hover{color:#cdd6e8}
.dchub-drawer-section{padding:12px 12px 4px}
.dchub-drawer-section-title{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#3d4a5c;margin-bottom:5px;padding:0 4px}
.dchub-drawer-item{display:flex;justify-content:space-between;align-items:center;padding:9px 12px;border-radius:6px;text-decoration:none;font-size:13px;color:#6b7a94;transition:all .15s;font-weight:500}
.dchub-drawer-item:hover{background:rgba(255,255,255,.05);color:#cdd6e8}
.dchub-drawer-cta{display:block;margin:6px 4px;padding:10px 16px;background:#00c8f0;color:#000;border-radius:6px;text-decoration:none;font-size:13px;font-weight:700;text-align:center;transition:opacity .15s}
.dchub-drawer-cta.secondary{background:rgba(255,255,255,.06);color:#cdd6e8;border:1px solid rgba(255,255,255,.1)}
.dchub-drawer-cta:hover{opacity:.85}

/* Mobile bottom nav */
.dchub-bottom-nav{display:none;position:fixed;bottom:0;left:0;right:0;background:rgba(8,11,16,.97);border-top:1px solid rgba(255,255,255,.07);z-index:1999;padding:6px 0 env(safe-area-inset-bottom)}
.dchub-bottom-item{display:flex;flex-direction:column;align-items:center;gap:3px;flex:1;padding:6px 4px;text-decoration:none;color:#3d4a5c;font-size:9px;font-weight:500;transition:color .15s}
.dchub-bottom-item.active,.dchub-bottom-item:hover{color:#00c8f0}

/* Page offset */
body{padding-top:48px!important}
.dchub-nav~*{margin-top:0}

/* Responsive */
@media(max-width:768px){
  .dchub-nav-links{display:none}
  .dchub-nav-signin,.dchub-nav-upgrade,.dchub-plan-badge{display:none}
  .dchub-nav-hamburger{display:flex}
  .dchub-bottom-nav{display:flex}
  body{padding-bottom:65px!important}
}
@media(max-width:900px){
  .dchub-nav-sub{display:none}
}
    `;
    document.head.appendChild(s);
  }

  // ── EVENTS ───────────────────────────────────────────────
  function wireEvents() {
    // Avatar toggle
    var avatarBtn = document.getElementById('dchub-avatar-btn');
    var userMenu  = document.getElementById('dchub-user-menu');
    if (avatarBtn && userMenu) {
      avatarBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        userMenu.classList.toggle('open');
      });
      document.addEventListener('click', function() { userMenu.classList.remove('open'); });
    }

    // Hamburger + drawer
    var hamburger = document.getElementById('dchub-hamburger');
    var drawer    = document.getElementById('dchub-drawer');
    var overlay   = document.getElementById('dchub-drawer-overlay');
    var closeBtn  = document.getElementById('dchub-drawer-close');

    function openDrawer()  { drawer && drawer.classList.add('open'); overlay && overlay.classList.add('open'); document.body.style.overflow='hidden'; }
    function closeDrawer() { drawer && drawer.classList.remove('open'); overlay && overlay.classList.remove('open'); document.body.style.overflow=''; }

    if (hamburger) hamburger.addEventListener('click', openDrawer);
    if (overlay)   overlay.addEventListener('click', closeDrawer);
    if (closeBtn)  closeBtn.addEventListener('click', closeDrawer);

    // Sign out
    ['dchub-signout-btn','dchub-drawer-signout'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('click', function(e) {
        e.preventDefault();
        ['dchub_token','dchub_user','dchub_session','dchub_api_key'].forEach(function(k) { localStorage.removeItem(k); });
        window.location.href = '/';
      });
    });
  }

  // ── INJECT ───────────────────────────────────────────────
  function inject() {
    injectStyles();

    // Nav
    var navEl = document.createElement('div');
    navEl.innerHTML = buildNavHTML();
    document.body.insertBefore(navEl.firstChild, document.body.firstChild);

    // Drawer
    var drawerEl = document.createElement('div');
    drawerEl.innerHTML = buildDrawerHTML();
    while (drawerEl.firstChild) document.body.appendChild(drawerEl.firstChild);

    // Bottom nav
    var bnEl = document.createElement('div');
    bnEl.innerHTML = buildBottomNavHTML();
    document.body.appendChild(bnEl.firstChild);

    wireEvents();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }

})();
