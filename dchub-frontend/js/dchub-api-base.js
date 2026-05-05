/**
 * DC Hub API Base v4.0 — JWT Auto-Injection
 * ═══════════════════════════════════════════════════════════
 *
 * Automatically attaches the stored JWT auth token to all API calls
 * so users stay authenticated across all pages after login.
 *
 * v4.0: Removed URL rewriting (no more Replit). JWT injection only.
 *
 * USAGE: Add to every HTML page BEFORE other scripts:
 *   <script src="js/dchub-api-base.js?v=4"></script>
 */
(function() {
  'use strict';

  var originalFetch = window.fetch;

  function getToken() {
    try { return localStorage.getItem('dchub_token') || ''; }
    catch (e) { return ''; }
  }

  function isApiCall(url) {
    if (typeof url !== 'string') return false;
    return url.includes('/api/') || url.includes('/ai/') || url.includes('/mcp');
  }

  function addAuthHeader(headers, token) {
    if (!headers) return { 'Authorization': 'Bearer ' + token };
    if (headers instanceof Headers) {
      if (!headers.has('Authorization')) headers.set('Authorization', 'Bearer ' + token);
    } else if (typeof headers === 'object') {
      if (!headers['Authorization'] && !headers['authorization']) {
        headers['Authorization'] = 'Bearer ' + token;
      }
    }
    return headers;
  }

  window.fetch = function(input, init) {
    var token = getToken();
    if (!token) return originalFetch.call(window, input, init);

    var url = '';

    if (typeof input === 'string') {
      url = input;
      if (isApiCall(url)) {
        init = init || {};
        init.headers = addAuthHeader(init.headers, token);
      }
    } else if (input instanceof Request) {
      url = input.url;
      if (isApiCall(url) && !input.headers.get('Authorization')) {
        var newHeaders = new Headers(input.headers);
        newHeaders.set('Authorization', 'Bearer ' + token);
        input = new Request(input, { headers: newHeaders });
      }
    }

    return originalFetch.call(window, input, init);
  };

  // Also patch XMLHttpRequest for any legacy code
  var originalXHROpen = XMLHttpRequest.prototype.open;
  var originalXHRSend = XMLHttpRequest.prototype.send;
  var xhrUrls = new WeakMap();

  XMLHttpRequest.prototype.open = function(method, url) {
    xhrUrls.set(this, url);
    return originalXHROpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function() {
    var url = xhrUrls.get(this) || '';
    var token = getToken();
    if (token && isApiCall(url)) {
      try { this.setRequestHeader('Authorization', 'Bearer ' + token); }
      catch (e) { /* already set */ }
    }
    return originalXHRSend.apply(this, arguments);
  };

  console.log('[DC Hub] API Base v4.0 — JWT auto-injection active');
})();
