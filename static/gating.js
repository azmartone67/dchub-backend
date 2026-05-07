/* phase68_gating -- client-side redaction for elements marked data-gate
 *
 * Usage in templates:
 *   <span data-gate="developer" data-placeholder="~530">{{ exact_count }}</span>
 *
 * For users below the required tier:
 *   - Element content is replaced with the placeholder (or "Pro only")
 *   - Click handler opens the redeem URL with the user's session_id
 *
 * For users at or above the required tier:
 *   - Element renders unchanged
 */
(function () {
  'use strict';
  if (window.__dchubGatingApplied) return;
  window.__dchubGatingApplied = true;

  var TIER_ORDER = ['anonymous', 'free', 'developer', 'pro', 'enterprise', 'founding'];

  function tierIndex(t) {
    var i = TIER_ORDER.indexOf((t || 'anonymous').toLowerCase());
    return i < 0 ? 0 : i;
  }

  function injectStyles() {
    if (document.getElementById('dchub-gating-style')) return;
    var s = document.createElement('style');
    s.id = 'dchub-gating-style';
    s.textContent = (
      '.gated-redacted { ' +
      '  color: #1976d2; cursor: pointer; ' +
      '  text-decoration: underline dotted; ' +
      '  text-underline-offset: 2px; ' +
      '  padding: 0 4px; border-radius: 3px; ' +
      '  background: rgba(25, 118, 210, 0.08); ' +
      '  transition: background 0.15s; ' +
      '} ' +
      '.gated-redacted:hover { ' +
      '  background: rgba(25, 118, 210, 0.18); ' +
      '} ' +
      '.gated-pill { ' +
      '  display: inline-block; padding: 2px 8px; ' +
      '  background: linear-gradient(90deg, #ff6b35, #ff8a4f); ' +
      '  color: #fff; border-radius: 999px; ' +
      '  font-size: 0.78em; font-weight: 600; ' +
      '  letter-spacing: 0.02em; ' +
      '} '
    );
    document.head.appendChild(s);
  }

  function redactElement(el, tier, sessionId, redeemTemplate) {
    var required = el.getAttribute('data-gate') || 'developer';
    var placeholder = el.getAttribute('data-placeholder') || 'Pro only';
    var redeemUrl = (redeemTemplate || 'https://dchub.cloud/api/v1/redeem/{session_id}')
      .replace('{session_id}', sessionId || 'browse');

    el.classList.add('gated-redacted');
    el.setAttribute('title',
      'Sign up free to see ' + (el.getAttribute('data-label') || 'this value') +
      ' (' + required + ' tier or higher)');
    el.textContent = placeholder;
    el.style.cursor = 'pointer';

    el.addEventListener('click', function (e) {
      e.preventDefault();
      window.location.href = redeemUrl;
    });
  }

  function applyGating(meta) {
    var currentIdx = tierIndex(meta.tier);
    var nodes = document.querySelectorAll('[data-gate]');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var requiredIdx = tierIndex(n.getAttribute('data-gate'));
      if (currentIdx < requiredIdx) {
        redactElement(n, meta.tier, meta.session_id, meta.redeem_url_template);
      }
    }
  }

  function loadTierAndApply() {
    injectStyles();
    fetch('/api/v1/me/tier', { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (data) { applyGating(data); })
      .catch(function () {
        applyGating({ tier: 'anonymous', session_id: '', redeem_url_template: '' });
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadTierAndApply);
  } else {
    loadTierAndApply();
  }
})();
