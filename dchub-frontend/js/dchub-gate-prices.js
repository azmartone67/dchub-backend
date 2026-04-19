/**
 * DC Hub Access Gate Price Fix — v1.0
 * =====================================
 * 
 * The access-gate.js has current pricing ($199 Pro, $699 Enterprise)
 * that don't match current pricing ($199 Pro, $699 Enterprise).
 * 
 * Rather than editing the minified access-gate.js, this script
 * overrides the TIER_FEATURES config after it loads.
 * 
 * DEPLOY: /js/dchub-gate-prices.js on Cloudflare Pages
 * LOAD: AFTER dchub-access-gate.js
 *
 *   <script src="/js/dchub-access-gate.js"></script>
 *   <script src="/js/dchub-gate-prices.js"></script>
 */

(function () {
  'use strict';

  // Wait for access gate to initialize, then patch prices
  function patchPrices() {
    // The access gate exposes DCHubGate globally
    // But the TIER_FEATURES object is in the IIFE closure
    // We need to monkey-patch the showProModal function instead

    if (!window.DCHubGate) {
      // Access gate not loaded yet — retry
      setTimeout(patchPrices, 100);
      return;
    }

    // Override the show function to inject correct prices
    var _origShow = window.DCHubGate.show;
    window.DCHubGate.show = function (tier) {
      // Temporarily patch the DOM after the modal renders
      setTimeout(function () {
        var modal = document.getElementById('gate-modal');
        if (!modal) return;

        // Fix prices in the modal
        var priceOld = modal.querySelector('.gate-price-old');
        var priceNew = modal.querySelector('.gate-price-new');

        if (tier === 'pro' || tier === 'registered') {
          if (priceOld) priceOld.textContent = '$199';
          if (priceNew) {
            priceNew.innerHTML = '$99<span>/mo</span>';
          }
        } else if (tier === 'enterprise') {
          if (priceOld) priceOld.textContent = '$699';
          if (priceNew) {
            priceNew.innerHTML = '$699<span>/mo</span>';
          }
          // Update tagline
          var title = modal.querySelector('.gate-title');
          if (title) title.textContent = 'Unlock full platform access';
          var sub = modal.querySelector('.gate-sub');
          if (sub) sub.textContent = 'Everything in Pro plus AI Brain, site analysis, grid monitoring, and Land & Power mapping.';
        }

        // Update CTA links to use correct pricing page anchors
        var primaryBtn = modal.querySelector('.gate-btn-primary');
        if (primaryBtn) {
          if (tier === 'enterprise') {
            primaryBtn.href = '/pricing#enterprise';
            primaryBtn.textContent = 'View Enterprise Plans →';
          }
        }
      }, 50); // Small delay for DOM to render

      // Call original
      _origShow.call(window.DCHubGate, tier);
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', patchPrices);
  } else {
    patchPrices();
  }

})();
