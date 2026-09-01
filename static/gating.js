/* phase71_runtime_gating -- DOM observer + pattern scanner
 * Hosted from dchub-frontend so dchub.cloud/static/gating.js resolves natively
 * via CF Pages. Same logic as Phase 71's dchub-backend copy.
 */
(function () {
  'use strict';
  if (window.__dchubGatingApplied) return;
  window.__dchubGatingApplied = true;

  var TIER_ORDER = ['anonymous', 'free', 'developer', 'pro', 'enterprise', 'founding'];
  var currentTierIdx = 0;
  var sessionId = '';
  var redeemTemplate = 'https://dchub.cloud/api/v1/redeem/{session_id}';
  var GATING_VERSION = 'phase74-map-subtree-scope';

  // Phase 285: placeholders that DON'T look like real pricing data.
  // Previously rate/amount used "$1,188" which renders identically to a
  // real (terrible) price — users mistook it for broken data, not a paywall.
  // The CI/healer correctly flagged "$1,188" as absent_text on /markets/.
  // Now both use em-dash, matching the capacity placeholder convention and
  // making "this is redacted, click to unlock" visually obvious. The blue
  // dotted-underline + click-to-redeem behavior is preserved via the
  // .gated-redacted CSS class (unchanged).
  var PATTERNS = [
    {
      regex: /\b(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{2,6}(?:\.\d+)?)(\s*)(MW|GW)\b/g,
      placeholder: '—',
      label: 'capacity'
    },
    {
      regex: /\$\s*\d+(?:\.\d+)?\s*\/\s*(?:MW-day|kWh|MWh)/g,
      placeholder: '—',  // phase 285: was '$1,188' — looked like real broken pricing
      label: 'rate'
    },
    {
      regex: /\b\d+\.\d+x\b/g,
      placeholder: '1x-2x',
      label: 'ratio'
    },
    {
      regex: /\$\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:M|B|K)?\b/g,
      placeholder: '—',  // phase 285: was '$1,188' — looked like real broken pricing
      label: 'amount'
    }
  ];

  var SKIP_TAGS = ['SCRIPT', 'STYLE', 'NOSCRIPT', 'INPUT', 'TEXTAREA',
                    'CODE', 'PRE', 'TEMPLATE', 'IFRAME', 'TITLE', 'META'];

  // phase74 (2026-07-02, INP): the Leaflet map mutates constantly (pan/zoom
  // re-renders markers, tiles, popups) and every burst used to trigger a full
  // TreeWalker rescan of document.body — the main INP long-task driver on
  // /land-power-map (Clarity: 350ms). Map internals never contain gateable MW
  // text that isn't already gated at the API, so both the observer and the
  // scanner skip the map subtree entirely. Non-map surfaces (side panels,
  // score cards, tables) are still scanned and gated exactly as before.
  function isMapContainerEl(el) {
    if (el.id === 'map') return true;
    var cl = el.classList;
    return !!(cl && (cl.contains('leaflet-pane') || cl.contains('leaflet-container')));
  }
  function inMapSubtree(node) {
    var el = node.nodeType === 1 ? node : node.parentNode;
    while (el && el.nodeType === 1) {
      if (isMapContainerEl(el)) return true;
      el = el.parentNode;
    }
    return false;
  }

  function tierIndex(t) {
    var i = TIER_ORDER.indexOf(String(t || 'anonymous').toLowerCase());
    return i < 0 ? 0 : i;
  }

  // 2026-06-20: the backend resolves tier from several legacy vocabularies
  // ('paid', 'identified', 'team', 'admin', …) that aren't in TIER_ORDER, so a
  // genuinely-paid user could map to index 0 and get redacted. /me/tier now
  // normalizes server-side, but keep a client-side bridge so a stale cached
  // response (e.g. 'paid') still resolves to the right tier.
  var TIER_ALIAS = {
    paid: 'pro', team: 'pro', metered: 'pro',
    identified: 'free', starter: 'free', dev: 'developer',
    ent: 'enterprise', admin: 'enterprise', research_seed: 'enterprise'
  };
  function normalizeTier(t) {
    var s = String(t || 'anonymous').toLowerCase().trim();
    return TIER_ALIAS[s] || s;
  }

  // 2026-06-20: the Land & Power map (and the rest of the app) signs paying
  // users in via a localStorage token/key — NOT a session cookie. Sending only
  // credentials:'include' made /api/v1/me/tier resolve 'anonymous' for a
  // logged-in Pro/Enterprise (even the owner), so this scanner redacted their
  // CAPACITY popups. Forward the same Bearer/X-API-Key the map's own fetches
  // use so the tier resolves correctly and paid users are never gated.
  function authHeaders() {
    var h = {};
    try {
      var tok = localStorage.getItem('dchub_token');
      var key = localStorage.getItem('dchub_api_key');
      if (tok) h['Authorization'] = 'Bearer ' + tok;
      if (key) h['X-API-Key'] = key;
    } catch (e) {}
    return h;
  }

  function injectStyles() {
    if (document.getElementById('dchub-gating-style')) return;
    var s = document.createElement('style');
    s.id = 'dchub-gating-style';
    s.textContent =
      '.gated-redacted { ' +
      '  color: #1976d2 !important; cursor: pointer; ' +
      '  text-decoration: underline dotted !important; ' +
      '  text-underline-offset: 2px; ' +
      '  padding: 0 4px; border-radius: 3px; ' +
      '  background: rgba(25, 118, 210, 0.08); ' +
      '  transition: background 0.15s; display: inline; ' +
      '}' +
      '.gated-redacted:hover { background: rgba(25, 118, 210, 0.20); }' +
      '.gated-pill { ' +
      '  display: inline-block; padding: 2px 8px; ' +
      '  background: linear-gradient(90deg, #ff6b35, #ff8a4f); ' +
      '  color: #fff !important; border-radius: 999px; ' +
      '  font-size: 0.78em; font-weight: 600; cursor: pointer; ' +
      '}';
    (document.head || document.documentElement).appendChild(s);
  }

  function redeemUrl() {
    // Phase FF+1 (2026-05-13): when sessionId is empty (anonymous user
    // never went through the AI-assistant flow), send them to /pricing
    // instead of /api/v1/redeem/browse. The redeem endpoint validates
    // session_id against UUID_RE and returns a 400 "Invalid session ID"
    // page for 'browse' — that broke the Markets→Pricing nav for
    // incognito users ("I was on a call and couldn't access our LA
    // pricing"). Real UUID sessions still flow through redeem; the
    // anonymous case now bounces cleanly to the public pricing wall.
    if (!sessionId || sessionId === 'browse') return '/pricing';
    return (redeemTemplate || 'https://dchub.cloud/api/v1/redeem/{session_id}')
      .replace('{session_id}', sessionId);
  }

  function attachClick(el) {
    el.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      window.location.href = redeemUrl();
    }, true);
  }

  function makeGatedSpan(original, placeholder, label) {
    var span = document.createElement('span');
    span.className = 'gated-redacted';
    span.setAttribute('data-gate', 'developer');
    span.setAttribute('data-placeholder', placeholder);
    span.setAttribute('data-label', label || 'value');
    span.setAttribute('data-original', original);
    span.setAttribute('data-gating-applied', '1');
    span.title = 'Free dev key unlocks the exact ' + label + ' → click to sign up';
    span.textContent = placeholder;
    attachClick(span);
    return span;
  }

  function shouldSkipNode(parent) {
    if (!parent) return true;
    if (SKIP_TAGS.indexOf(parent.tagName) >= 0) return true;
    if (parent.classList && parent.classList.contains('gated-redacted')) return true;
    if (parent.hasAttribute && parent.hasAttribute('data-gate-skip')) return true;
    return false;
  }

  function processTextNode(node) {
    var parent = node.parentNode;
    if (shouldSkipNode(parent)) return;
    var text = node.nodeValue;
    if (!text || text.length < 2) return;

    var fragments = [];
    var lastIdx = 0;
    var hasMatch = false;
    var safety = 0;

    while (lastIdx < text.length && safety++ < 200) {
      var earliestIdx = Infinity;
      var earliestMatch = null;
      var earliestPattern = null;

      for (var i = 0; i < PATTERNS.length; i++) {
        var pat = PATTERNS[i];
        pat.regex.lastIndex = lastIdx;
        var m = pat.regex.exec(text);
        if (m && m.index < earliestIdx) {
          earliestIdx = m.index;
          earliestMatch = m;
          earliestPattern = pat;
        }
      }

      if (!earliestMatch) break;
      if (earliestIdx > lastIdx) {
        fragments.push({type: 'text', value: text.substring(lastIdx, earliestIdx)});
      }
      fragments.push({
        type: 'gated',
        original: earliestMatch[0],
        placeholder: earliestPattern.placeholder,
        label: earliestPattern.label
      });
      lastIdx = earliestIdx + earliestMatch[0].length;
      hasMatch = true;
    }

    if (!hasMatch) return;
    if (lastIdx < text.length) {
      fragments.push({type: 'text', value: text.substring(lastIdx)});
    }

    var docFrag = document.createDocumentFragment();
    for (var j = 0; j < fragments.length; j++) {
      var f = fragments[j];
      if (f.type === 'text') {
        docFrag.appendChild(document.createTextNode(f.value));
      } else {
        docFrag.appendChild(makeGatedSpan(f.original, f.placeholder, f.label));
      }
    }
    parent.replaceChild(docFrag, node);
  }

  function scanRoot(root) {
    if (currentTierIdx >= 2) return;
    if (!root) return;
    // phase74: walk ELEMENT+TEXT so FILTER_REJECT on an element prunes its
    // ENTIRE subtree in one step (TreeWalker semantics) — the Leaflet map,
    // scripts/styles and already-gated spans are never descended into,
    // instead of being re-checked per text node on every rescan.
    var walker = document.createTreeWalker(
      root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
      {
        acceptNode: function (n) {
          if (n.nodeType === 1) {
            if (isMapContainerEl(n)) return NodeFilter.FILTER_REJECT;
            if (SKIP_TAGS.indexOf(n.tagName) >= 0) return NodeFilter.FILTER_REJECT;
            if (n.classList && n.classList.contains('gated-redacted')) return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_SKIP; // descend, but don't emit elements
          }
          var p = n.parentNode;
          if (!p) return NodeFilter.FILTER_REJECT;
          if (SKIP_TAGS.indexOf(p.tagName) >= 0) return NodeFilter.FILTER_REJECT;
          if (p.classList && p.classList.contains('gated-redacted')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      },
      false
    );
    var nodes = [];
    var n;
    while ((n = walker.nextNode())) nodes.push(n);
    for (var i = nodes.length - 1; i >= 0; i--) {
      processTextNode(nodes[i]);
    }
  }

  function applyMarkedGating(root) {
    if (currentTierIdx >= 2) return;
    var ctx = root && root.querySelectorAll ? root : document;
    var nodes = ctx.querySelectorAll('[data-gate]:not(.gated-redacted)');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      // 2026-09-01: [data-gate] is a SHARED attribute namespace. js/dchub-access-gate.js
      // reads the SAME attribute with its own, different vocabulary (its TIER_HIERARCHY
      // has registered:1, identified:1, starter:1, developer:1, pro:2, enterprise:3), and
      // it owns every data-gate="registered" element on /platform and /app — 6 per page:
      // #facilities-grid, #market-intel-grid, #news-feed-content, #announcements-container,
      // #chart-bars, #pipeline-timeline. gating.js owns ONLY the TIER_ORDER words.
      //
      // Skipping unrecognized values explicitly is behaviour-identical to the old
      // tierIndex()->0 fallthrough (0 is <= any currentTierIdx, so the loop already
      // 'continue'd). It is written out so a future well-meaning fix — routing this
      // through normalizeTier(), or making tierIndex() return -1 for unknowns — cannot
      // silently hand these elements to the branch below, which does
      // `el.textContent = placeholder` and would WIPE those JS-populated grids.
      var rawGate = el.getAttribute('data-gate');
      if (TIER_ORDER.indexOf(String(rawGate || '').toLowerCase()) < 0) continue;
      var requiredIdx = tierIndex(rawGate);
      if (currentTierIdx >= requiredIdx) continue;
      var placeholder = el.getAttribute('data-placeholder') || 'Pro only';
      el.classList.add('gated-redacted');
      el.setAttribute('data-gating-applied', '1');
      el.textContent = placeholder;
      attachClick(el);
    }
  }

  var rescanTimer = null;
  function debouncedRescan() {
    if (rescanTimer) clearTimeout(rescanTimer);
    rescanTimer = setTimeout(function () {
      applyMarkedGating(document);
      scanRoot(document.body);
    }, 300);
  }

  function setupObserver() {
    if (!window.MutationObserver) return;
    var observer = new MutationObserver(function (muts) {
      var rescanNeeded = false;
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type !== 'childList' && m.type !== 'characterData') continue;
        // phase74: map-internal mutations (markers, tiles, popups) never carry
        // gateable text that isn't API-gated — don't let them trigger rescans.
        if (inMapSubtree(m.target)) continue;
        var allOurs = true;
        for (var j = 0; j < m.addedNodes.length; j++) {
          var node = m.addedNodes[j];
          if (node.nodeType !== 1) { allOurs = false; break; }
          if (!node.hasAttribute || !node.hasAttribute('data-gating-applied')) {
            allOurs = false; break;
          }
        }
        if (m.addedNodes.length === 0 || !allOurs) {
          rescanNeeded = true;
          break;
        }
      }
      if (rescanNeeded) debouncedRescan();
    });
    observer.observe(document.body, {
      childList: true, subtree: true, characterData: true
    });
  }

  function init() {
    // r62-qa: NEVER run the paywall scanner on public sales/checkout surfaces.
    // PATTERN 4 (comma-grouped dollar amounts) was redacting the pricing page's
    // OWN annual prices ($2,990 / $5,990 / save $1,200 …) to '—' for anonymous
    // visitors, making real pricing look broken. These pages must always show
    // real prices regardless of tier.
    var _p = (location.pathname || '').replace(/\/+$/, '').toLowerCase();
    if (_p === '/pricing' || _p === '/checkout' || _p === '/signup' || _p === '/upgrade') {
      return;
    }
    injectStyles();
    // Cache-bust: the CF edge/worker caches /api/v1/me/tier keyed on URL+Cookie
    // (NOT the X-API-Key header), so a paid user authenticating via a
    // localStorage key (no session cookie) was served a previously-cached
    // 'anonymous' response and got their CAPACITY popups redacted. A unique
    // query per load forces a fresh origin resolution. (2026-06-20)
    var _bust = '/api/v1/me/tier?_=' + (new Date().getTime()) + '-' +
                Math.floor(Math.random() * 1e9);
    fetch(_bust, { credentials: 'include', headers: authHeaders(), cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        currentTierIdx = tierIndex(normalizeTier(data.tier));
        sessionId = data.session_id || '';
        redeemTemplate = data.redeem_url_template || redeemTemplate;
        applyMarkedGating(document);
        scanRoot(document.body);
        setupObserver();
      })
      .catch(function () {
        currentTierIdx = 0;
        applyMarkedGating(document);
        scanRoot(document.body);
        setupObserver();
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
