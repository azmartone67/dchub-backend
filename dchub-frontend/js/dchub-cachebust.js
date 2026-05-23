// dchub-cachebust.js — global fetch wrapper that adds a per-page-load
// cache-bust to any GET against /api/*.
//
// The CF Pages worker edge-caches /api/* GETs with cacheEverything for
// 60-3600s depending on tier. A 404 (from a not-yet-deployed endpoint)
// or a stale data response can poison that cache for the full TTL, even
// after Railway/origin has fixed it. The user sees empty tiles or
// wrong data on dashboards while waiting for the cache to expire.
//
// Rather than touch every fetch site or wait an hour, monkey-patch
// window.fetch ONCE — only adds a `_cb=<timestamp-random>` query param
// to /api/* GETs, leaves everything else (POSTs, third-party APIs,
// static asset fetches) untouched.
//
// MUST load synchronously (no defer/async) BEFORE any other script that
// fetches /api/* — typically as the first <script> tag in <head>.
//
// Phase ZZZZZ — brain takeover hardening (2026-05-23).
(function(){
  if (window.__dchubCachebustWrapped) return;
  window.__dchubCachebustWrapped = true;
  var _origFetch = window.fetch;
  if (typeof _origFetch !== 'function') return;
  function _bust(u) {
    var sep = u.indexOf('?') >= 0 ? '&' : '?';
    return u + sep + '_cb=' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
  }
  window.fetch = function(input, init) {
    try {
      var method = (init && init.method ? String(init.method) : 'GET').toUpperCase();
      // Only touch GETs (no body / read-only). Leave POST/PUT/PATCH/DELETE alone.
      if (method === 'GET' && typeof input === 'string' && input.indexOf('/api/') === 0) {
        input = _bust(input);
      } else if (method === 'GET' && input && typeof input === 'object' && typeof input.url === 'string' && input.url.indexOf('/api/') === 0) {
        // Request object form
        input = new Request(_bust(input.url), input);
      }
    } catch (e) { /* never break the real fetch */ }
    return _origFetch.call(this, input, init);
  };
})();
