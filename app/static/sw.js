// Glowby service worker — v1
// Deliberately minimal: no caching of API responses or pages, so live
// checks, scores, and deploys are never served stale. Its job is to make
// the site installable and claim control quickly.
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  // pass-through: browser default network behavior
});
