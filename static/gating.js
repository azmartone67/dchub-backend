/* phase71_runtime_gating -- DOM observer + pattern scanner
 *
 * Three layers of gating:
 *   1. Hard-coded data-gate attrs (set in templates) -> redacted on load
 *   2. Pattern scanner over text nodes -> auto-wraps MW/GW values, $/MW-day
 *      rates, ratios, large counts in tables
 *   3. MutationObserver re-runs both above on dynamic content insertions
 *
 * Single file, no dependencies, ~2 KB.
 */
(function () {
  'use strict';
  if (window.__dchubGatingApplied) return;
  window.__dchubGatingApplied = true;

  var TIER_ORDER = ['anonymous', 'free', 'developer', 'pro', 'enterprise', 'founding'];
  var currentTierIdx = 0;
  var sessionId = '';
  var redeemTemplate = 'https://dchub.cloud/api/v1/redeem/{session_id}';
  var GATING_VERSION = 'phase71';

  // Patterns to detect at runtime (case-sensitive on units to avoid false positives)
  var PATTERNS = [
    {
      // Large MW/GW values: 30 GW, 1,200 MW, 63,691 MW
      regex: /\b(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{2,6}(?:\.\d+)?)(\s*)(MW|GW)\b/g,
      placeholder: 'multi-GW',
      label: 'capacity'
    },
    {
      // $/MW-day, $/kWh, $/MWh rates
      regex: /\$\s*\d+(?:\.\d+)?\s*\/\s*(?:MW-day|kWh|MWh)/g,
      placeholder: '$$$$',
      label: 'rate'
    },
    {
      // Ratios: 1.4x, 2.7x
      regex: /\b\d+\.\d+x\b/g,
      placeholder: '1x-2x',
      label: 'ratio'
    },
    {
      // Dollar amounts >= $1M
      regex: /\$\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:M|B|K)?\b/g,
      placeholder: '$$$$',
      label: 'amount'
    }
  ];

  var SKIP_TAGS = ['SCRIPT', 'STYLE', 'NOSCRIPT', 'INPUT', 'TEXTAREA',
                    'CODE', 'PRE', 'TEMPLATE', 'IFRAME', 'TITLE', 'META'];

  function tierIndex(t) {
    var i = TIER_ORDER.indexOf(String(t || 'anonymous').toLowerCase());
    return i < 0 ? 0 : i;
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
    return (redeemTemplate || 'https://dchub.cloud/api/v1/redeem/{session_id}')
      .replace('{session_id}', sessionId || 'browse');
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

    // Walk the text, finding earliest match across patterns at each position
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
    if (currentTierIdx >= 2) return; // 2 = developer; dev+ sees everything
    if (!root) return;
    var walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode: function (n) {
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
    // Reverse to avoid invalidating walker's parent references
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
      var requiredIdx = tierIndex(el.getAttribute('data-gate'));
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
      // Skip mutations triggered by our own gating (replaceChild adds new nodes)
      // The data-gating-applied attribute on the new spans tells us to skip
      var rescanNeeded = false;
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type !== 'childList' && m.type !== 'characterData') continue;
        // Check if all added nodes are our own gated spans
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
      childList: true,
      subtree: true,
      characterData: true
    });
  }

  function init() {
    injectStyles();
    fetch('/api/v1/me/tier', { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        currentTierIdx = tierIndex(data.tier);
        sessionId = data.session_id || '';
        redeemTemplate = data.redeem_url_template || redeemTemplate;
        // initial pass + observer
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
