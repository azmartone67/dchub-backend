/**
 * DC Hub — What's New Ticker v1.0
 * ════════════════════════════════
 * Auto-cycling announcements bar for homepage.
 * Drop-in: <script src="/js/dchub-ticker.js"></script>
 * Injects between hero and trust section.
 */
(function () {
  'use strict';

  var ANNOUNCEMENTS = [
    { icon: '🏆', label: 'NEW', color: '#fbbf24', text: 'Rankings Series — State infrastructure rankings across gas, fiber, power & construction', href: '/rankings' },
    { icon: '🗺️', label: 'LIVE', color: '#10b981', text: 'Land & Power Map — 70K+ substations, 300K+ pipelines, fiber routes & flood zones', href: '/land-power' },
    { icon: '🤖', label: 'MCP', color: '#a78bfa', text: 'AI Integration — 9 platforms connected via MCP protocol, cited by Claude, ChatGPT & Perplexity', href: '/connect' },
    { icon: '💰', label: 'NEW', color: '#f59e0b', text: 'Tax Incentives — Compare data center incentive programs across all 50 US states', href: '/tax-incentives' },
    { icon: '⚔️', label: 'LIVE', color: '#a78bfa', text: 'AI Wars — Watch 7 AI platforms compete head-to-head using DC Hub data', href: '/ai-wars' },
    { icon: '📊', label: 'NEW', color: '#6366f1', text: 'Dark Fiber Matrix — 14 providers, metro pricing, TCO calculator for connectivity planning', href: '/rankings' },
    { icon: '⚡', label: 'DATA', color: '#10b981', text: '32,851 gas pipelines + 4,939 infrastructure features mapped via KMZ & ArcGIS ingestion', href: '/land-power' },
    { icon: '🔌', label: 'API', color: '#3b82f6', text: 'Developer API — 11 MCP tools, RESTful endpoints, real-time facility & deal data', href: '/api-docs' }
  ];

  var currentIndex = 0;
  var intervalId = null;
  var CYCLE_MS = 4000;

  function injectStyles() {
    if (document.getElementById('dchub-ticker-styles')) return;
    var s = document.createElement('style');
    s.id = 'dchub-ticker-styles';
    s.textContent = [
      '.dchub-ticker{max-width:900px;margin:0 auto;padding:0 32px}',
      '.dchub-ticker-wrap{background:rgba(15,17,25,.8);border:1px solid rgba(255,255,255,.08);border-radius:14px;overflow:hidden;position:relative;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}',
      '.dchub-ticker-header{display:flex;align-items:center;justify-content:space-between;padding:10px 20px;border-bottom:1px solid rgba(255,255,255,.06)}',
      '.dchub-ticker-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#6366f1;display:flex;align-items:center;gap:8px}',
      '.dchub-ticker-title::before{content:"";width:6px;height:6px;background:#10b981;border-radius:50%;animation:dchubTickerPulse 2s infinite}',
      '@keyframes dchubTickerPulse{0%,100%{opacity:1}50%{opacity:.4}}',
      '.dchub-ticker-dots{display:flex;gap:6px}',
      '.dchub-ticker-dot{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.15);cursor:pointer;transition:all .3s}',
      '.dchub-ticker-dot.active{background:#6366f1;width:18px;border-radius:3px}',
      '.dchub-ticker-body{position:relative;height:52px;overflow:hidden}',
      '.dchub-ticker-slide{position:absolute;inset:0;display:flex;align-items:center;gap:14px;padding:0 20px;opacity:0;transform:translateY(8px);transition:all .4s cubic-bezier(.4,0,.2,1);cursor:pointer;text-decoration:none;color:#e2e8f0}',
      '.dchub-ticker-slide.active{opacity:1;transform:translateY(0)}',
      '.dchub-ticker-slide:hover{background:rgba(99,102,241,.06)}',
      '.dchub-ticker-icon{font-size:1.3rem;flex-shrink:0}',
      '.dchub-ticker-badge{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.5px;flex-shrink:0;color:#fff}',
      '.dchub-ticker-text{font-size:13px;color:#94a3b8;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.dchub-ticker-arrow{color:#4b5563;font-size:14px;flex-shrink:0;transition:color .2s}',
      '.dchub-ticker-slide:hover .dchub-ticker-arrow{color:#6366f1}',
      '.dchub-ticker-slide:hover .dchub-ticker-text{color:#e2e8f0}',
      '.dchub-ticker-nav{display:flex;gap:2px;align-items:center}',
      '.dchub-ticker-btn{background:none;border:none;color:#4b5563;cursor:pointer;padding:4px;font-size:16px;line-height:1;transition:color .2s}',
      '.dchub-ticker-btn:hover{color:#e2e8f0}',
      '@media(max-width:768px){',
        '.dchub-ticker{padding:0 16px}',
        '.dchub-ticker-text{font-size:12px}',
        '.dchub-ticker-body{height:48px}',
        '.dchub-ticker-dots{display:none}',
      '}'
    ].join('\n');
    document.head.appendChild(s);
  }

  function buildHTML() {
    var slides = '';
    var dots = '';
    for (var i = 0; i < ANNOUNCEMENTS.length; i++) {
      var a = ANNOUNCEMENTS[i];
      slides += '<a href="' + a.href + '" class="dchub-ticker-slide' + (i === 0 ? ' active' : '') + '" data-index="' + i + '">' +
        '<span class="dchub-ticker-icon">' + a.icon + '</span>' +
        '<span class="dchub-ticker-badge" style="background:' + a.color + '">' + a.label + '</span>' +
        '<span class="dchub-ticker-text">' + a.text + '</span>' +
        '<span class="dchub-ticker-arrow">→</span>' +
      '</a>';
      dots += '<div class="dchub-ticker-dot' + (i === 0 ? ' active' : '') + '" data-index="' + i + '"></div>';
    }

    return '<div class="dchub-ticker" id="dchub-ticker">' +
      '<div class="dchub-ticker-wrap">' +
        '<div class="dchub-ticker-header">' +
          '<div class="dchub-ticker-title">What\'s New</div>' +
          '<div style="display:flex;align-items:center;gap:12px">' +
            '<div class="dchub-ticker-dots">' + dots + '</div>' +
            '<div class="dchub-ticker-nav">' +
              '<button type="button" class="dchub-ticker-btn" id="dchub-ticker-prev" aria-label="Previous">‹</button>' +
              '<button type="button" class="dchub-ticker-btn" id="dchub-ticker-next" aria-label="Next">›</button>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="dchub-ticker-body">' + slides + '</div>' +
      '</div>' +
    '</div>';
  }

  function goTo(index) {
    currentIndex = ((index % ANNOUNCEMENTS.length) + ANNOUNCEMENTS.length) % ANNOUNCEMENTS.length;
    var slides = document.querySelectorAll('.dchub-ticker-slide');
    var dots = document.querySelectorAll('.dchub-ticker-dot');
    for (var i = 0; i < slides.length; i++) {
      slides[i].classList.toggle('active', i === currentIndex);
    }
    for (var j = 0; j < dots.length; j++) {
      dots[j].classList.toggle('active', j === currentIndex);
    }
  }

  function startCycle() {
    stopCycle();
    intervalId = setInterval(function () {
      goTo(currentIndex + 1);
    }, CYCLE_MS);
  }

  function stopCycle() {
    if (intervalId) { clearInterval(intervalId); intervalId = null; }
  }

  function wireEvents() {
    var prev = document.getElementById('dchub-ticker-prev');
    var next = document.getElementById('dchub-ticker-next');
    var wrap = document.querySelector('.dchub-ticker-wrap');

    if (prev) prev.addEventListener('click', function () { goTo(currentIndex - 1); startCycle(); });
    if (next) next.addEventListener('click', function () { goTo(currentIndex + 1); startCycle(); });

    document.querySelectorAll('.dchub-ticker-dot').forEach(function (dot) {
      dot.addEventListener('click', function () {
        goTo(parseInt(this.dataset.index));
        startCycle();
      });
    });

    if (wrap) {
      wrap.addEventListener('mouseenter', stopCycle);
      wrap.addEventListener('mouseleave', startCycle);
    }
  }

  function init() {
    injectStyles();

    // Find injection point: between hero and trust section
    var trustSection = document.querySelector('.trust-section');
    var quickStats = document.querySelector('.quick-stats');
    var hero = document.querySelector('.hero-v2');

    var target = trustSection || (quickStats ? quickStats.parentElement.nextElementSibling : null);
    if (!target && hero) target = hero.nextElementSibling;

    if (!target) return;

    var container = document.createElement('div');
    container.innerHTML = buildHTML();
    container.style.marginBottom = '0';
    target.parentNode.insertBefore(container, target);

    wireEvents();
    startCycle();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
