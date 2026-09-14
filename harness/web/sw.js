// Service worker: caches the app shell so the app opens instantly (and shows a clear offline state).
// API responses are never cached: session state must always be live.
const SHELL = "harness-shell-v1";
const ASSETS = ["/", "/static/style.css", "/static/app.js", "/static/icon-192.png", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  const isShell = url.pathname === "/" || url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest";
  if (!isShell) return;
  // Network first so deploys show up right away; the cache is only the offline fallback.
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(SHELL).then((c) => c.put(event.request, copy));
        return resp;
      })
      .catch(() => caches.match(event.request)),
  );
});
