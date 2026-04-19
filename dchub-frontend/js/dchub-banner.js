/**
 * DC Hub Dynamic Banner v1.2.0
 * ═══════════════════════════════════════════════════════════
 * Components:
 *   1. Dynamic version banner — pulls version from /api/version (public)
 *   2. Founding member bar — shows seat scarcity on homepage
 *
 * CHANGELOG v1.2.0:
 *   - NEW: Uses /api/version (no auth) instead of /api/v1/stats (401 fix)
 *   - NEW: Falls back to /api/v1/stats → /health in chain
 *   - FIXED: .hero-badge targeting is primary (no more announcements-container confusion)
 *   - FIXED: Founding bar hides old founding-banner div to prevent overlap
 *   - ADDED: Cache-bust with ?_t= timestamp on API calls
 */

(function () {
  'use strict';

  var API_BASE = 'https://dchub.cloud';
  var FOUNDING_TOTAL = 20;
  var FOUNDING_CLAIMED = 15;
  var DEBUG = false;

  function log() {
    if (DEBUG) console.log.apply(console, ['[DCHubBanner]'].concat(Array.prototype.slice.call(arguments)));
  }

  // ── Fetch version — tries /api/version first (public, no auth) ──
  function fetchVersion(callback) {
    var ts = '?_t=' + Date.now();

    log('Fetching version from', API_BASE + '/api/v1/version');
    fetch(API_BASE + '/api/v1/version' + ts, { mode: 'cors' })
      .then(function (r) {
        if (!r.ok) throw new Error('/api/v1/version returned ' + r.status);
        return r.json();
      })
      .then(function (d) {
        log('Version response:', d);
        callback(d.version || null, d.build || null, d);
      })
      .catch(function (err) {
        log('/api/v1/version failed (' + err.message + '), trying /api/v1/stats');
        // Fallback 1: /api/v1/stats
        fetch(API_BASE + '/api/v1/stats' + ts, { mode: 'cors' })
          .then(function (r) {
            if (!r.ok) throw new Error('/api/v1/stats returned ' + r.status);
            return r.json();
          })
          .then(function (d) {
            log('Stats response:', d);
            callback(d.version || null, d.build || null, d);
          })
          .catch(function (err2) {
            log('/api/v1/stats failed (' + err2.message + '), trying /health');
            // Fallback 2: /health
            fetch(API_BASE + '/health' + ts, { mode: 'cors' })
              .then(function (r) {
                if (!r.ok) throw new Error('/health returned ' + r.status);
                return r.json();
              })
              .then(function (d) {
                log('Health response:', d);
                callback(d.version || null, d.build || null, d);
              })
              .catch(function (err3) {
                log('All version endpoints failed — using fallback');
                callback(null, null, null);
              });
          });
      });
  }

  // ── Fetch founding member count ───────────────────────────────
  function fetchFoundingCount(callback) {
    var ts = '?_t=' + Date.now();
    log('Fetching founding count from', API_BASE + '/api/founding-members');

    fetch(API_BASE + '/api/founding-members' + ts, { mode: 'cors' })
      .then(function (r) {
        if (!r.ok) throw new Error('Founding-members returned ' + r.status);
        return r.json();
      })
      .then(function (d) {
        log('Founding response:', d);
        if (d && typeof d.claimed === 'number') callback(d.claimed);
        else callback(FOUNDING_CLAIMED);
      })
      .catch(function (err) {
        log('Founding fetch failed, using fallback:', err.message);
        callback(FOUNDING_CLAIMED);
      });
  }

  // ── Update version banner (.hero-badge) ───────────────────────
  function updateVersionBanner(version, build, stats) {
    // PRIMARY: target .hero-badge directly
    var bar = document.querySelector('.hero-badge');

    // FALLBACK: walk the DOM for version text, excluding announcement cards
    if (!bar) {
      var allElements = document.querySelectorAll('div, span, p, a');
      for (var i = 0; i < allElements.length; i++) {
        var el = allElements[i];
        var text = el.textContent || '';
        // Skip the announcements/news section
        if (el.className && /announcement/i.test(el.className)) continue;
        if (el.closest && el.closest('.announcements-container')) continue;
        if (/v\d+\s+(Released|Update|New)/i.test(text) && el.offsetHeight < 60) {
          bar = el;
          break;
        }
      }
    }

    if (!bar) {
      log('No version banner element found — skipping update');
      return;
    }

    log('Found version banner element:', bar.className || bar.tagName);

    // Build display version
    var displayVersion = version || 'v' + (build || '89');
    var now = new Date();
    var dateStr = now.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

    // Live stats for the message
    var facilityCount = (stats && stats.facilities) ? stats.facilities.toLocaleString() : '20,000+';
    var marketCount = (stats && stats.markets) ? stats.markets : '35+';
    var dealCount = (stats && stats.deals) ? stats.deals : '673';

    // Release notes from the API, or rotating messages
    var releaseNotes = (stats && stats.release_notes) ? stats.release_notes : null;

    var messages;
    if (releaseNotes) {
      messages = [releaseNotes];
    } else {
      messages = [
        'Real-time intelligence for ' + facilityCount + ' facilities',
        marketCount + ' markets tracked live',
        dealCount + ' M&A deals in database',
        'AI agents integrated — ChatGPT, Claude, Perplexity',
        'Land & Power map with 50+ live data layers',
        'Competitive intelligence panel now available'
      ];
    }

    var msg = messages[now.getDate() % messages.length];

    bar.innerHTML = '🚀 <strong>' + displayVersion + '</strong> — ' + msg +
      ' <span style="opacity:.6;margin-left:8px;font-size:0.85em">Updated ' + dateStr + '</span>';

    log('Version banner updated to:', displayVersion, msg);
  }

  // ── Founding member bar ───────────────────────────────────────
  function createFoundingBar(claimed) {
    var remaining = FOUNDING_TOTAL - claimed;
    var pct = Math.round((claimed / FOUNDING_TOTAL) * 100);
    var urgencyColor, pulseClass;

    if (remaining <= 10) { urgencyColor = '#ff4757'; pulseClass = 'fm-pulse'; }
    else if (remaining <= 25) { urgencyColor = '#ffa502'; pulseClass = ''; }
    else { urgencyColor = '#00c9a7'; pulseClass = ''; }

    // Don't duplicate
    if (document.getElementById('founding-bar')) {
      log('Founding bar already exists — skipping');
      return;
    }

    // Check if user previously dismissed
    try {
      if (localStorage.getItem('founding-bar-closed') === 'true') {
        log('Founding bar was dismissed — skipping');
        return;
      }
    } catch (e) { /* localStorage unavailable */ }

    // Hide the old hardcoded founding-banner if it exists
    var oldBanner = document.getElementById('founding-banner');
    if (oldBanner) {
      oldBanner.style.display = 'none';
      log('Hid old founding-banner element');
    }

    log('Creating founding bar. Claimed:', claimed, 'Remaining:', remaining);

    var barDiv = document.createElement('div');
    barDiv.id = 'founding-bar';
    barDiv.className = pulseClass;
    barDiv.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:10001;' +
      'background:linear-gradient(135deg,#0a0a1a 0%,#1a1a3e 50%,#0a0a1a 100%);' +
      'color:#fff;text-align:center;padding:8px 40px 8px 16px;font-size:14px;' +
      'border-bottom:1px solid rgba(124,58,237,0.3);font-family:-apple-system,BlinkMacSystemFont,sans-serif;';

    barDiv.innerHTML =
      '🏗️ <strong>Founding Member Program</strong> — ' +
      '<span style="text-decoration:line-through;opacity:.5">$199/mo</span> ' +
      '<strong style="color:#7c3aed">$99/mo</strong> locked for life · ' +
      '<span style="color:' + urgencyColor + ';font-weight:700">' + remaining + ' of ' + FOUNDING_TOTAL + ' seats left</span> · ' +
      '<span style="display:inline-block;width:80px;height:8px;background:rgba(255,255,255,0.15);' +
      'border-radius:4px;vertical-align:middle;margin:0 6px;overflow:hidden;position:relative">' +
      '<span style="position:absolute;left:0;top:0;height:100%;width:' + pct + '%;' +
      'background:' + urgencyColor + ';border-radius:4px;transition:width 0.5s"></span></span> ' +
      '<a href="/pricing" style="color:#a78bfa;text-decoration:underline;font-weight:600">Claim Your Seat →</a>';

    // Close button
    var closeBtn = document.createElement('span');
    closeBtn.innerHTML = '✕';
    closeBtn.style.cssText = 'position:absolute;right:12px;top:50%;transform:translateY(-50%);' +
      'cursor:pointer;opacity:.5;font-size:16px;padding:4px 8px;';
    closeBtn.onclick = function () {
      barDiv.style.display = 'none';
      try { localStorage.setItem('founding-bar-closed', 'true'); } catch (e) {}
      // Reset nav position
      var nav = document.querySelector('nav');
      if (nav) nav.style.top = '0';
      // Reset hero position
      var hero = document.querySelector('.hero-v2, .hero, section');
      if (hero) hero.style.marginTop = '';
    };
    barDiv.appendChild(closeBtn);

    // Add pulse animation
    if (pulseClass) {
      var style = document.createElement('style');
      style.textContent = '@keyframes fmPulse{0%,100%{opacity:1}50%{opacity:.7}}' +
        '.fm-pulse{animation:fmPulse 2s ease-in-out infinite}';
      document.head.appendChild(style);
    }

    // Insert at very top of body
    document.body.insertBefore(barDiv, document.body.firstChild);

    // Adjust nav position to account for founding bar
    var barHeight = barDiv.offsetHeight;
    var nav = document.querySelector('nav');
    if (nav) {
      nav.style.top = barHeight + 'px';
      log('Nav offset set to', barHeight + 'px');
    }

    // Adjust hero/main content margin
    var hero = document.querySelector('.hero-v2, .hero, main');
    if (hero) {
      var currentMargin = parseInt(window.getComputedStyle(hero).marginTop) || 0;
      hero.style.marginTop = (currentMargin + barHeight) + 'px';
    }

    log('Founding bar created');
  }

  // ── Is this the homepage? ─────────────────────────────────────
  function isHomepage() {
    var path = window.location.pathname;
    return path === '/' || path === '/index.html' || path === '' ||
      window.DCHUB_SHOW_FOUNDING_BAR === true;
  }

  // ── Initialize ────────────────────────────────────────────────
  function init() {
    log('DC Hub Banner v1.2.0 initializing');

    // 1. Always update the version banner (all pages)
    fetchVersion(function (version, build, stats) {
      updateVersionBanner(version, build, stats);
    });

    // 2. Show founding bar on homepage only
    if (isHomepage()) {
      fetchFoundingCount(function (claimed) {
        createFoundingBar(claimed);
      });
    }

    console.log('✅ DC Hub Banner loaded');
  }

  // ── Public API for testing ────────────────────────────────────
  window.DCHubBanner = {
    updateVersion: updateVersionBanner,
    showFoundingBar: createFoundingBar,
    refresh: init
  };

  // ── Boot ──────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
