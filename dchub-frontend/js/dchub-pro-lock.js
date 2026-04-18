/**
 * DC Hub — PRO Feature Lock v1.0
 * /js/dchub-pro-lock.js?v=1
 *
 * Disables Pro-only features for free/anonymous users with visible PRO badges.
 * Runs AFTER all other scripts load (including dynamically injected panels).
 *
 * Locked features:
 *   - Evaluate button (id: site-search-btn)
 *   - Export button (id: export-report-btn)
 *   - Site Planner panel (injected by site-planner-panel.js)
 *   - Energy Discovery panel (injected by energy-discovery-integration.js)
 *   - Competitive Intelligence (injected by dchub-competitive-intel.js)
 */
(function () {
  'use strict';

  var PAID = ['pro', 'enterprise', 'founding'];

  function isPaid() {
    try {
      var s = JSON.parse(localStorage.getItem('dchub_session') || '{}');
      if (PAID.indexOf((s.plan || '').toLowerCase()) !== -1) return true;
    } catch (e) {}
    try {
      var u = JSON.parse(localStorage.getItem('dchub_user') || '{}');
      if (PAID.indexOf((u.plan || '').toLowerCase()) !== -1) return true;
    } catch (e) {}
    return false;
  }

  if (isPaid()) return; // Pro users — do nothing

  /* ── PRO badge style ── */
  var style = document.createElement('style');
  style.textContent = [
    '.dchub-pro-badge {',
    '  display: inline-block;',
    '  background: linear-gradient(135deg, #6366f1, #8b5cf6);',
    '  color: #fff;',
    '  font-size: 9px;',
    '  font-weight: 800;',
    '  padding: 1px 5px;',
    '  border-radius: 3px;',
    '  margin-left: 4px;',
    '  letter-spacing: 0.5px;',
    '  vertical-align: middle;',
    '  pointer-events: none;',
    '}',
    '.dchub-pro-locked {',
    '  opacity: 0.4 !important;',
    '  cursor: not-allowed !important;',
    '  pointer-events: none !important;',
    '  position: relative;',
    '}',
    '.dchub-pro-locked-clickable {',
    '  cursor: pointer !important;',
    '  pointer-events: auto !important;',
    '  position: relative;',
    '}',
    '.dchub-pro-overlay {',
    '  position: absolute;',
    '  inset: 0;',
    '  background: rgba(15, 14, 26, 0.6);',
    '  backdrop-filter: blur(4px);',
    '  -webkit-backdrop-filter: blur(4px);',
    '  display: flex;',
    '  align-items: center;',
    '  justify-content: center;',
    '  border-radius: 8px;',
    '  z-index: 100;',
    '  cursor: pointer;',
    '}',
    '.dchub-pro-overlay-text {',
    '  color: #e0e7ff;',
    '  font-size: 13px;',
    '  font-weight: 600;',
    '  text-align: center;',
    '  line-height: 1.6;',
    '  background: rgba(15, 14, 26, 0.8);',
    '  padding: 12px 20px;',
    '  border-radius: 10px;',
    '  border: 1px solid rgba(99, 102, 241, 0.3);',
    '}',
    '#dchub-founding-banner {',
    '  position: fixed;',
    '  bottom: 40px;',
    '  right: 20px;',
    '  z-index: 9998;',
    '  background: linear-gradient(135deg, #1e1b4b, #312e81);',
    '  border: 1px solid #6366f1;',
    '  border-radius: 14px;',
    '  padding: 16px 20px;',
    '  max-width: 300px;',
    '  box-shadow: 0 8px 32px rgba(99,102,241,0.3);',
    '  font-family: system-ui, sans-serif;',
    '  animation: dchubBannerSlide 0.5s ease-out;',
    '}',
    '@keyframes dchubBannerSlide {',
    '  from { transform: translateY(20px); opacity: 0; }',
    '  to { transform: translateY(0); opacity: 1; }',
    '}',
    '#dchub-founding-banner .fb-title {',
    '  color: #fbbf24; font-size: 14px; font-weight: 700; margin: 0 0 6px;',
    '}',
    '#dchub-founding-banner .fb-price {',
    '  color: #e0e7ff; font-size: 13px; margin: 0 0 4px;',
    '}',
    '#dchub-founding-banner .fb-price s { color: #64748b; }',
    '#dchub-founding-banner .fb-spots {',
    '  color: #f87171; font-size: 11px; margin: 0 0 10px;',
    '}',
    '#dchub-founding-banner .fb-btn {',
    '  display: block; width: 100%; padding: 10px; border-radius: 8px;',
    '  background: linear-gradient(135deg, #f59e0b, #f97316);',
    '  color: #fff; font-size: 13px; font-weight: 700;',
    '  border: none; cursor: pointer; text-align: center; text-decoration: none;',
    '}',
    '#dchub-founding-banner .fb-btn:hover { opacity: 0.9; }',
    '#dchub-founding-banner .fb-close {',
    '  position: absolute; top: 6px; right: 10px;',
    '  color: #64748b; cursor: pointer; font-size: 16px; background: none; border: none;',
    '}'
  ].join('\n');
  document.head.appendChild(style);

  /* ── Helper: add PRO badge to a button ── */
  function addBadge(el) {
    if (!el || el.querySelector('.dchub-pro-badge')) return;
    var badge = document.createElement('span');
    badge.className = 'dchub-pro-badge';
    badge.textContent = 'PRO';
    el.appendChild(badge);
  }

  /* ── Helper: lock a button (disable + badge + show modal on click) ── */
  function lockButton(el) {
    if (!el) return;
    addBadge(el);
    el.style.opacity = '0.5';
    el.style.cursor = 'not-allowed';
    el.disabled = true;
    // Override onclick
    el.onclick = null;
    el.removeAttribute('onclick');
    // Add click interceptor on parent to catch bubbled events
    el.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      showProModal();
    }, true);
    // Re-enable pointer events so the click handler fires
    el.style.pointerEvents = 'auto';
  }

  /* ── Helper: lock a panel with overlay ── */
  function lockPanel(el, label, teaser) {
    if (!el || el.querySelector('.dchub-pro-overlay')) return;
    el.style.position = 'relative';
    el.style.overflow = 'hidden';
    var overlay = document.createElement('div');
    overlay.className = 'dchub-pro-overlay';
    overlay.innerHTML = '<div class="dchub-pro-overlay-text">' +
      '🔒 <span class="dchub-pro-badge" style="font-size:11px;padding:2px 8px;">PRO</span><br>' +
      '<span style="font-size:12px;color:#a5b4fc;">' + (label || 'Upgrade to unlock') + '</span>' +
      (teaser ? '<br><span style="font-size:10px;color:#64748b;margin-top:4px;display:inline-block;">' + teaser + '</span>' : '') +
      '</div>';
    overlay.addEventListener('click', function (e) {
      e.stopPropagation();
      showProModal();
    });
    el.appendChild(overlay);
  }

  /* ── PRO upgrade modal ── */
  function showProModal() {
    var modal = document.getElementById('lp-upgrade-modal');
    if (modal) {
      var title = document.getElementById('lp-modal-title');
      var msg = document.getElementById('lp-modal-message');
      if (title) title.textContent = 'Pro Feature';
      if (msg) msg.innerHTML =
        'This feature requires a <strong>Pro subscription</strong>.<br><br>' +
        '✅ Unlimited infrastructure layers (40+)<br>' +
        '✅ Site scoring &amp; evaluate tool<br>' +
        '✅ Energy discovery &amp; competitive intel<br>' +
        '✅ PDF &amp; KMZ export<br>' +
        '✅ Full API access<br><br>' +
        '<strong>$99/mo</strong> · Cancel anytime';
      modal.classList.add('show');
    } else {
      // Fallback if modal doesn't exist
      window.location.href = '/pricing.html';
    }
  }

  /* ── Lock specific features ── */

  // Layer buttons that are FREE (no badge, no lock)
  var FREE_LAYERS = [
    'datacenters', 'nuclear', 'airports', 'fema', 'railroad'
  ];

  function applyLocks() {
    // 1. Evaluate button
    var evalBtn = document.getElementById('site-search-btn');
    lockButton(evalBtn);

    // 2. Export button
    var exportBtn = document.getElementById('export-report-btn');
    lockButton(exportBtn);

    // 3. Site Planner button (injected by site-planner-panel.js — look for it)
    var plannerBtn = document.querySelector('[data-panel="site-planner"]')
                  || document.querySelector('.site-planner-btn')
                  || document.getElementById('site-planner-toggle');
    if (!plannerBtn) {
      document.querySelectorAll('button, .btn, a').forEach(function (el) {
        if (el.textContent.trim().indexOf('Site Planner') !== -1 && !el.querySelector('.dchub-pro-badge')) {
          plannerBtn = el;
        }
      });
    }
    if (plannerBtn) {
      addBadge(plannerBtn);
      plannerBtn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        showProModal();
      }, true);
    }

    // 4. Energy Discovery panel (right sidebar section)
    var edPanel = document.querySelector('.energy-discovery-panel')
               || document.getElementById('energy-discovery-panel');
    if (!edPanel) {
      document.querySelectorAll('.sidebar-section, .sidebar-title, [class*="discovery"]').forEach(function (el) {
        if (el.textContent.indexOf('ENERGY DISCOVERY') !== -1) {
          edPanel = el.closest('.sidebar-section') || el.parentElement;
        }
      });
    }
    if (edPanel) lockPanel(edPanel, 'Energy Discovery', '7,000+ power plants · 1,900+ TX lines · 31 pipelines');

    // 5. Competitive Intelligence panel
    var ciPanel = document.querySelector('.competitive-intel-panel')
               || document.getElementById('competitive-intel-panel')
               || document.getElementById('comp-intel-container');
    if (!ciPanel) {
      document.querySelectorAll('[class*="compet"], [id*="compet"], [class*="intel"]').forEach(function (el) {
        if (el.textContent.indexOf('Competitive') !== -1 || el.textContent.indexOf('competitive') !== -1) {
          ciPanel = el.closest('.sidebar-section') || el.parentElement || el;
        }
      });
    }
    if (ciPanel) lockPanel(ciPanel, 'Competitive Intelligence', 'Real estate · Fiber · SEC filings · Permits');

    // 6. Export KMZ button (in energy discovery)
    document.querySelectorAll('button, .btn').forEach(function (el) {
      var txt = el.textContent.trim().toLowerCase();
      if (txt.indexOf('export') !== -1 && txt.indexOf('kmz') !== -1) {
        lockButton(el);
      }
    });

    // 7. PRO badges on layer buttons — free layers stay open, rest get badge + block
    document.querySelectorAll('.layer-btn').forEach(function (btn) {
      if (btn.querySelector('.dchub-pro-badge')) return; // already badged
      if (btn.hasAttribute('data-pro-locked')) return; // already locked
      var layer = btn.getAttribute('data-layer') || '';
      var id = btn.id || '';

      // Skip free layers
      if (FREE_LAYERS.indexOf(layer) !== -1) return;
      // Skip special buttons that are already handled (drought, risk)
      if (id === 'drought-btn' || id === 'risk-btn') return; // these get locked separately below
      if (id === 'fiber-panel-btn') return; // locked separately below

      // Add small PRO badge
      var badge = document.createElement('span');
      badge.className = 'dchub-pro-badge';
      badge.textContent = 'PRO';
      badge.style.fontSize = '8px';
      badge.style.padding = '0px 4px';
      badge.style.marginLeft = 'auto';
      badge.style.flexShrink = '0';
      btn.appendChild(badge);

      // Mark as locked and block clicks
      btn.setAttribute('data-pro-locked', 'true');
      btn.style.opacity = '0.6';
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        showProModal();
        return false;
      }, true); // capture phase — fires before any other handler
    });

    // 8. Lock special buttons: Fiber Networks, Drought Risk, Risk Assessment
    ['fiber-panel-btn', 'drought-btn', 'risk-btn'].forEach(function (btnId) {
      var btn = document.getElementById(btnId);
      if (!btn || btn.hasAttribute('data-pro-locked')) return;
      addBadge(btn);
      btn.setAttribute('data-pro-locked', 'true');
      btn.style.opacity = '0.6';
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        showProModal();
        return false;
      }, true);
    });
  }

  /* ── Run after all scripts have injected their UI ── */
  // Run immediately for elements already in DOM
  if (document.readyState === 'complete') {
    applyLocks();
  } else {
    window.addEventListener('load', applyLocks);
  }

  // Also run after a delay to catch late-injected panels
  setTimeout(applyLocks, 3000);
  setTimeout(applyLocks, 6000);

  // Watch for dynamically added panels
  if (window.MutationObserver) {
    var applied = false;
    var observer = new MutationObserver(function () {
      if (!applied) {
        applied = true;
        setTimeout(function () {
          applyLocks();
          applied = false;
        }, 500);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  console.log('[DCHub] Pro Lock active — Evaluate, Export, Site Planner, Energy Discovery, Competitive Intel locked');

  /* ── Founding Member Banner — appears after 15s ── */
  setTimeout(function () {
    if (isPaid()) return;
    if (document.getElementById('dchub-founding-banner')) return;

    var banner = document.createElement('div');
    banner.id = 'dchub-founding-banner';
    banner.innerHTML =
      '<button class="fb-close" onclick="this.parentElement.remove()">&times;</button>' +
      '<div class="fb-title">🎉 Founding Member Offer</div>' +
      '<div class="fb-price"><strong>$99/mo</strong> for life <s>$199/mo</s></div>' +
      '<div class="fb-spots">🔥 Limited spots remaining</div>' +
      '<a href="https://buy.stripe.com/9B6fZi1cCdjT3ml8i6aZi00" class="fb-btn">Claim Your Spot →</a>';
    document.body.appendChild(banner);

    // Auto-dismiss after 30 seconds if not interacted with
    setTimeout(function () {
      if (banner.parentElement) banner.style.opacity = '0.7';
    }, 30000);
  }, 15000);

})();
