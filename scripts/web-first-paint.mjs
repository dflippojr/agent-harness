// Cold first-load timing of harness/web through headless Chromium's DevTools protocol (no npm packages), for comparing
// a web bundle before and after a change such as the app.js module split (#258) on the same setup.
//   node scripts/web-first-paint.mjs <web-dir> [runs=5] [route=#/agents]
// Serves <web-dir> on localhost (gzip for text, like harness/webgzip.py; /api answers 503 so the page renders its
// offline state), then per run opens a fresh profile with the #188 study's throttled mobile model applied through
// CDP: 390x844 DPR 3, 4x CPU, +150 ms per request, 200 KB/s down. Prints median (min-max) ms of:
//   fcp     first-contentful-paint (the shell and boot splash)
//   modules DOMContentLoaded end; module scripts run before it, so this is "app.js and its imports have executed"
//   scripts number of JS requests (app.js, client.mjs, lib/, pages/)
// The browser defaults to Edge; set CHROME to another Chromium (the #188 study used Playwright's Chromium 153).
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { extname, join, normalize } from "node:path";
import { gzipSync } from "node:zlib";

const BROWSER = process.env.CHROME || String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const [webDir, runsArg = "5", route = "#/agents"] = process.argv.slice(2);
if (!webDir) throw new Error("usage: node scripts/web-first-paint.mjs <web-dir> [runs] [route]");
const runs = Number(runsArg);
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
  ".png": "image/png", ".webmanifest": "application/manifest+json" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (path.startsWith("/api/")) { res.writeHead(503, { "content-type": "application/json" }); res.end("{}"); return; }
  const file = normalize(join(webDir, path === "/" ? "index.html" : path));
  let body;
  try { if (!file.startsWith(normalize(webDir)) || !statSync(file).isFile()) throw new Error(); body = readFileSync(file); } catch (_) {
    res.writeHead(404); res.end(); return;
  }
  const type = TYPES[extname(file)] || "application/octet-stream";
  const headers = { "content-type": type, "cache-control": "no-cache" };
  if (type.startsWith("text/") && /\bgzip\b/.test(req.headers["accept-encoding"] || "")) {
    body = gzipSync(body);
    headers["content-encoding"] = "gzip";
  }
  res.writeHead(200, headers);
  res.end(body);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const origin = `http://127.0.0.1:${server.address().port}`;

async function coldLoad(port) {
  const profile = mkdtempSync(join(tmpdir(), "harness-first-paint-"));
  const browser = spawn(BROWSER, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`, "--no-first-run", "about:blank"], { stdio: "ignore" });
  try {
    let wsUrl;
    for (let i = 0; i < 50 && !wsUrl; i++) {
      await sleep(200);
      try {
        const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
        wsUrl = pages.find((p) => p.type === "page")?.webSocketDebuggerUrl;
      } catch (_) { /* not up yet */ }
    }
    if (!wsUrl) throw new Error("DevTools endpoint didn't come up");
    const ws = new WebSocket(wsUrl);
    await new Promise((r) => ws.addEventListener("open", r));
    let nextId = 1;
    const pending = new Map();
    const waiters = [];
    ws.addEventListener("message", (e) => {
      const msg = JSON.parse(e.data);
      if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg.result); pending.delete(msg.id); }
      for (const w of waiters.filter((x) => x.method === msg.method)) { waiters.splice(waiters.indexOf(w), 1); w.resolve(); }
    });
    const send = (method, params = {}) => new Promise((resolve) => {
      const id = nextId++;
      pending.set(id, resolve);
      ws.send(JSON.stringify({ id, method, params }));
    });
    const once = (method) => new Promise((resolve) => waiters.push({ method, resolve }));
    await send("Page.enable");
    await send("Network.enable");
    await send("Network.setCacheDisabled", { cacheDisabled: true });
    await send("Network.emulateNetworkConditions", { offline: false, latency: 150, downloadThroughput: 200 * 1024, uploadThroughput: 200 * 1024 });
    await send("Emulation.setCPUThrottlingRate", { rate: 4 });
    await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 3, mobile: true });
    const loaded = once("Page.loadEventFired");
    await send("Page.navigate", { url: `${origin}/${route}` });
    await loaded;
    await sleep(500);
    const { result } = await send("Runtime.evaluate", { returnByValue: true, expression: `(() => {
      const nav = performance.getEntriesByType("navigation")[0];
      const fcp = performance.getEntriesByName("first-contentful-paint")[0];
      const scripts = performance.getEntriesByType("resource").filter((r) => /\\.m?js(\\?|$)/.test(r.name));
      return { fcp: fcp ? fcp.startTime : null, modules: nav.domContentLoadedEventEnd, scripts: scripts.length };
    })()` });
    ws.close();
    return result.value;
  } finally {
    browser.kill();
    await sleep(500);
    try { rmSync(profile, { recursive: true, force: true }); } catch (_) { /* browser still releasing files */ }
  }
}

const samples = [];
for (let i = 0; i < runs; i++) samples.push(await coldLoad(9340 + i));
server.close();
const stat = (key) => {
  const v = samples.map((s) => s[key]).sort((a, b) => a - b);
  return `${Math.round(v[Math.floor(v.length / 2)])} (${Math.round(v[0])}-${Math.round(v.at(-1))})`;
};
console.log(`${webDir} ${route} cold, ${runs} runs: fcp ${stat("fcp")} ms, modules ${stat("modules")} ms, scripts ${samples[0].scripts}`);
