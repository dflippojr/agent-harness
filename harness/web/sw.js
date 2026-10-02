// Service worker: caches the app shell so the app opens instantly (and shows a clear offline state).
// API responses are never cached: session state must always be live.
const BUILD_ID = "2026.10.02.8";
const SHELL = `harness-shell-${BUILD_ID}`;
// Every module app.js loads is listed explicitly (tests/test_web_sw_assets.py fails on an omission), so the whole
// module graph is served network-first and cached together for offline use.
const ASSETS = [
  "/", "/style.css", "/app.js", "/client.mjs", "/icon-192.png", "/manifest.webmanifest",
  "/lib/compat.mjs",
  "/lib/diff.mjs",
  "/lib/jobs.mjs",
  "/lib/format.mjs",
  "/lib/layout.mjs",
  "/lib/markdown.mjs",
  "/lib/setting-input.mjs",
  "/lib/settings-text.mjs",
  "/lib/snippets.mjs",
  "/lib/taint.mjs",
  "/lib/targets.mjs",
  "/lib/tools.mjs",
  "/pages/actions.mjs",
  "/pages/daemon-settings.mjs",
  "/pages/jobs.mjs",
  "/lib/trace.mjs",
];

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
  const isShell = url.pathname === "/" || ASSETS.includes(url.pathname);
  if (!isShell) return;
  // Network first so deploys show up right away; the cache is only the offline fallback.
  event.respondWith(
    // Bypass Chromium's HTTP cache here. Otherwise a successful fetch can still
    // return a stale app bundle, defeating this worker's network-first policy.
    fetch(event.request, { cache: "no-cache" })
      .then((resp) => {
        const copy = resp.clone();
        caches.open(SHELL).then((c) => c.put(event.request, copy)).catch(() => {});
        return resp;
      })
      .catch(() => caches.match(event.request)),
  );
});

self.addEventListener("message", (event) => {
  if (event.origin !== self.location.origin || event.data !== "PURGE_SHELL") return;
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith("harness-shell-")).map((key) => caches.delete(key)),
  )));
});
