/**
 * DC Hub — Land & Power Nav Button
 * 
 * Injects a highlighted "⚡ Land & Power" button into the main nav bar,
 * positioned in the nav-right area before the discovery badge.
 *
 * Usage: <script src="/static/js/lp-banner.js"></script> before </body>
 */
(function () {
  'use strict';

  // Don't show on land-power page itself
  if (window.location.pathname.indexOf('/land-power') === 0) return;

  // ——— STYLES ———
  var css = document.createElement('style');
  css.textContent = [
    '.lp-nav-btn{',
    '  display:inline-flex;align-items:center;gap:6px;',
    '  padding:7px 14px;',
    '  background:linear-gradient(135deg,#f59e0b,#f97316);',
    '  color:#fff;',
    '  border-radius:7px;',
    '  font-size:12.5px;',
    '  font-weight:700;',
    '  font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;',
    '  text-decoration:none;',
    '  white-space:nowrap;',
    '  transition:all .2s;',
    '  position:relative;',
    '  overflow:hidden;',
    '  letter-spacing:0.01em;',
    '}',
    '.lp-nav-btn:hover{',
    '  transform:translateY(-1px);',
    '  box-shadow:0 4px 16px rgba(249,115,22,0.35);',
    '  color:#fff;',
    '}',
    '.lp-nav-btn::after{',
    '  content:"";position:absolute;top:-50%;left:-50%;width:200%;height:200%;',
    '  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15),transparent);',
    '  transform:rotate(45deg);',
    '  animation:lp-btn-shine 3s ease-in-out infinite;',
    '}',
    '@keyframes lp-btn-shine{',
    '  0%{transform:translateX(-100%) rotate(45deg)}',
    '  30%,100%{transform:translateX(100%) rotate(45deg)}',
    '}',
    '.lp-nav-btn .lp-bolt{font-size:13px;filter:brightness(1.2)}',
    '.lp-nav-btn .lp-new{',
    '  font-size:9px;font-weight:800;',
    '  background:rgba(255,255,255,0.25);',
    '  padding:1px 5px;border-radius:3px;',
    '  letter-spacing:0.05em;',
    '  text-transform:uppercase;',
    '}',
    '',
    '/* Mobile: show in nav drawer instead */',
    '@media(max-width:768px){',
    '  .lp-nav-btn.lp-desktop{display:none !important}',
    '}',
  ].join('\n');
  document.head.appendChild(css);

  // ——— CREATE BUTTON ———
  var btn = document.createElement('a');
  btn.href = '/land-power';
  btn.className = 'lp-nav-btn lp-desktop';
  btn.innerHTML = '<span class="lp-bolt">⚡</span> Land & Power <span class="lp-new">New</span>';

  // ——— INSERT INTO NAV ———
  var navRight = document.querySelector('.nav-right');
  if (navRight) {
    // Insert as the first child of nav-right (before discovery badge)
    navRight.insertBefore(btn, navRight.firstChild);
  }

  // ——— ALSO ADD TO MOBILE NAV DRAWER ———
  var mobileNavLinks = document.querySelector('.mobile-nav-drawer-links');
  if (mobileNavLinks) {
    // Check if Land & Power link already exists in mobile nav
    var existingLP = false;
    mobileNavLinks.querySelectorAll('a').forEach(function(a) {
      if (a.href && a.href.indexOf('/land-power') !== -1) existingLP = true;
    });

    if (!existingLP) {
      var mobileLink = document.createElement('a');
      mobileLink.href = '/land-power';
      mobileLink.style.cssText = 'background:linear-gradient(135deg,rgba(249,115,22,0.15),rgba(245,158,11,0.1));border:1px solid rgba(249,115,22,0.3);';
      mobileLink.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="#f59e0b" style="width:20px;height:20px;"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> <span style="color:#f59e0b;font-weight:600">Land & Power</span> <span style="font-size:10px;background:#f59e0b;color:#000;padding:1px 6px;border-radius:3px;font-weight:700;margin-left:4px">NEW</span>';

      // Insert after the first link (Home)
      var firstLink = mobileNavLinks.querySelector('a');
      if (firstLink && firstLink.nextSibling) {
        mobileNavLinks.insertBefore(mobileLink, firstLink.nextSibling);
      } else {
        mobileNavLinks.appendChild(mobileLink);
      }
    }
  }

})();
