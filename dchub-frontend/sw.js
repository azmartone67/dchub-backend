// DC Hub Service Worker - Self-uninstalling
// Root sw.js was causing stale cache issues. This version cleans up and exits.
// The correct stub is at /js/sw.js

self.addEventListener('install', function(event) {
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  self.registration.unregister();
  event.waitUntil(
    caches.keys().then(function(names) {
      return Promise.all(names.map(function(name) { return caches.delete(name); }));
    })
  );
});
