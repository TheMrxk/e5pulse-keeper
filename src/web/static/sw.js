// E5Pulse Service Worker
// Strategy: API requests always go to the network (data must stay live); the static
// shell uses network-first with a cache fallback so the last UI still renders offline.
const CACHE = "e5pulse-shell-v7";
const SHELL = ["/", "/index.html", "/app.js?v=6", "/styles.css?v=6",
               "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // API / health checks: never cached, always live
  if (url.pathname.startsWith("/api/") || url.pathname === "/health") {
    return; // fall through to the browser's default network handling
  }
  // Static shell: network first, fall back to cache when offline
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
        return resp;
      })
      .catch(() => caches.match(event.request).then((r) => r || caches.match("/")))
  );
});
