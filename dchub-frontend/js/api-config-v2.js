/**
 * DC Hub API Config v2.0
 * ═══════════════════════════════════════════════════════════
 * Single API base URL. Same-origin. No failover. No Replit.
 *
 * Replaces: api-config.js, dchub-api-fix.js
 *
 * USAGE: Add to every HTML page BEFORE other DC Hub scripts:
 *   <script src="js/api-config-v2.js"></script>
 */
(function() {
  'use strict';

  // Same-origin: all /api/* routes go through the Cloudflare Worker proxy
  window.DCHUB_API_BASE = '';
  window.DCHUB_API_URLS = [''];

  /**
   * Simple fetch wrapper with timeout.
   * Kept for backward compatibility with pages that call window.dchubFetch().
   */
  window.dchubFetch = async function(path, options) {
    options = options || {};
    if (!options.signal) {
      options.signal = AbortSignal.timeout(8000);
    }
    return fetch(path, options);
  };

  console.log('[DC Hub] API Config v2.0 — same-origin, no failover');
})();
