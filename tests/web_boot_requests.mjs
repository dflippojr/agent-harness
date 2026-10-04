// UI harness (#289): boot starts /health and /me together, requests /me once, and starts route data
// right after both resolve without waiting on /profile, /gpu or /models/warm. Scenario = argv[2].
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";

class Emitter {
  constructor() { this._l = {}; }
  addEventListener(type, fn) { (this._l[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this._l[type] = (this._l[type] || []).filter((f) => f !== fn); }
  dispatchEvent(ev) {
    for (const fn of [...(this._l[ev.type] || [])]) fn.call(this, ev);
    return true;
  }
}

class Node extends Emitter {}
class El extends Node {
  constructor(tag, attrs = {}) {
    super();
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.attributes = { ...attrs };
    this.className = attrs.class || "";
    this.id = attrs.id || "";
    this.hidden = false;
    this.value = attrs.value || "";
    this.href = attrs.href || "";
    this.type = attrs.type || "";
    this.disabled = false;
    this.style = { _p: {}, setProperty(k, v) { this._p[k] = v; }, removeProperty(k) { delete this._p[k]; }, getPropertyValue(k) { return this._p[k] || ""; } };
    this.dataset = {};
    this.classList = {
      _s: new Set(this.className.split(/\s+/).filter(Boolean)),
      add: (c) => { this.classList._s.add(c); this.className = [...this.classList._s].join(" "); },
      remove: (c) => { this.classList._s.delete(c); this.className = [...this.classList._s].join(" "); },
      toggle: (c, force) => {
        const on = force === undefined ? !this.classList._s.has(c) : !!force;
        if (on) this.classList.add(c); else this.classList.remove(c);
        return on;
      },
      contains: (c) => this.classList._s.has(c),
    };
    this.offsetHeight = 48;
    this._text = "";
    this.parentNode = null;
    this.options = [];
  }
  get isConnected() { return !!this.parentNode; }
  get textContent() {
    if (this.childNodes.length) return this.childNodes.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
    return this._text;
  }
  set textContent(v) { this._text = String(v); this.childNodes = []; }
  get innerHTML() { return this.textContent; }
  set innerHTML(v) { this.textContent = v; }
  append(...nodes) {
    for (const n of nodes.flat()) {
      if (n === null || n === undefined || n === false) continue;
      if (n instanceof El) n.parentNode = this;
      this.childNodes.push(n instanceof El ? n : String(n));
    }
  }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  remove() { this.removed = true; if (this.parentNode) { const i = this.parentNode.childNodes.indexOf(this); if (i !== -1) this.parentNode.childNodes.splice(i, 1); } this.parentNode = null; }
  click() { this.dispatchEvent({ type: "click" }); }
  focus() {}
  blur() {}
  querySelector(sel) {
    if (sel === ".drawer-recent") return this._drawerRecent || null;
    return null;
  }
  querySelectorAll() { return []; }
  setAttribute(k, v) { this.attributes[k] = v; if (k === "id") this.id = v; }
}

const byId = {};
const make = (tag, id, extra = {}) => {
  const el = new El(tag, { id, ...extra });
  if (id) byId[id] = el;
  return el;
};

const feature = make("select", "feature-nav");
for (const value of ["agents", "chat", "jobs", "images"]) {
  const opt = new El("option", { value });
  opt.value = value;
  feature.options.push(opt);
}
feature.value = "chat";

const doc = new Emitter();
doc.documentElement = new El("html");
doc.documentElement.style = { setProperty() {}, removeProperty() {}, getPropertyValue() { return ""; } };
doc.documentElement.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
doc.body = new El("body");
doc.hidden = false;
doc.visibilityState = "visible";
doc.getElementById = (id) => byId[id] || null;
doc.querySelector = (sel) => {
  if (sel === 'link[rel="apple-touch-icon"]' || sel === 'link[rel="icon"]') return new El("link");
  if (sel === ".composer" || sel === ".session-chrome") return null;
  if (sel === "#app") return byId.app;
  return null;
};
doc.querySelectorAll = (sel) => {
  if (sel === ".jump") return [];
  return [];
};
doc.createElement = (tag) => new El(tag);
doc.createTextNode = (t) => String(t);
doc.addEventListener = () => {};

const drawer = make("nav", "nav-drawer");
drawer._drawerRecent = new El("div", { class: "drawer-recent" });

make("main", "app");
make("h1", "title");
make("button", "back");
make("span", "conn");
make("a", "profile-icon");
make("button", "menu-btn");
make("div", "drawer-scrim");
make("div", "drawer-chats");
make("span", "drawer-profile-icon");
make("div", "fab-host");
make("a", "fab");
make("header", "bar");
make("div", "guest-banner");
make("div", "toast");

const loc = {
  href: "http://localhost/#/agents",
  origin: "http://localhost",
  hash: "#/agents",
  protocol: "http:",
  pathname: "/",
  replace(url) { this.hash = String(url).startsWith("#") ? String(url) : `#${url}`; },
};
const storage = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
};

const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body,
  text: async () => JSON.stringify(body),
});

const scenario = process.argv[2] || "owner";
const network = { up: false };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const LATENCY = 15;
const log = [];
const NOT_ROUTE_DATA = new Set(["/health", "/me", "/profile", "/gpu", "/models/warm", "/auth/session"]);
const adminRange = scenario === "client-update" ? { min: 99, max: 100 }
  : scenario === "daemon-update" ? { min: 0, max: 0 } : { min: 1, max: 4 };
const me = { owner: { role: "owner", name: "Owner", login: "owner" }, member: { role: "member", name: "M", login: "m" },
  guest: { role: "guest" } }[scenario] || { role: "owner", name: "Owner", login: "owner" };

const fakeFetch = async (url, init = {}) => {
  const href = String(url);
  const path = href.replace(/^https?:\/\/[^/?]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "").split("?")[0];
  const entry = { path, method: init.method || "GET", start: performance.now(), tick: log.length };
  log.push(entry);
  await sleep(LATENCY);
  entry.end = performance.now();
  // #368: Airplane Mode. Every request fails at the network level until `network.up`.
  if (scenario.startsWith("offline") && !network.up) throw new TypeError("Failed to fetch");
  if (path === "/health") return jsonResp({ protocols: { admin: adminRange }, update_hint: {} });
  if (path === "/me") {
    if (scenario === "me-fallback" && !entry.fallbackSeen) {
      const prior = log.filter((e) => e.path === "/me").length;
      if (prior === 1) return jsonResp({ error: "nope" }, 500);
      return jsonResp({ role: "member", name: "M", login: "m" });
    }
    if (scenario === "me-fail") return jsonResp({ error: "nope" }, 500);
    return jsonResp(me);
  }
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/gpu") return jsonResp({ manual: false });
  return jsonResp([]);
};

const win = new Emitter();
Object.assign(win, {
  addEventListener: (...a) => Emitter.prototype.addEventListener.call(win, ...a),
  removeEventListener: (...a) => Emitter.prototype.removeEventListener.call(win, ...a),
  dispatchEvent: (...a) => Emitter.prototype.dispatchEvent.call(win, ...a),
  localStorage: storage(),
  sessionStorage: storage(),
  location: loc,
  navigator: { serviceWorker: undefined, userAgent: "test" },
  history: { back() { loc.hash = "#/"; }, replaceState() {} },
  scrollTo() {},
  confirm: () => false,
  innerHeight: 800,
  scrollY: 0,
  caches: undefined,
  EventSource: class { constructor() { this.readyState = 1; } addEventListener() {} close() {} },
  fetch: fakeFetch,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
});

if (scenario === "offline-cached") win.localStorage.setItem("harness.lastRole", "owner");
globalThis.window = win;
globalThis.localStorage = win.localStorage;
globalThis.location = loc;
globalThis.fetch = fakeFetch;
const { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } = await import("../harness/web/client.mjs");

const sandbox = createContext({
  window: win,
  document: doc,
  location: loc,
  history: win.history,
  navigator: win.navigator,
  localStorage: win.localStorage,
  sessionStorage: win.sessionStorage,
  EventSource: win.EventSource,
  fetch: fakeFetch,
  URL,
  AbortController,
  TextDecoder,
  console,
  getComputedStyle: (el) => el.style,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: win.requestAnimationFrame,
  cancelAnimationFrame: win.cancelAnimationFrame,
  confirm: win.confirm,
  agentHarnessWeb,
  WEB_BUILD_ID,
  WEB_PROTOCOL,
  Node,
  Event,
  JSON,
  Date,
  Math,
  Number,
  String,
  Boolean,
  Array,
  Object,
  Set,
  Map,
  Promise,
  Error,
  parseInt,
  encodeURIComponent,
  decodeURIComponent,
  undefined,
});

await runApp(sandbox);
await sleep(400);
const paths = log.map((e) => `${e.method} ${e.path}`);
const first = (p) => log.find((e) => e.path === p);
const fail = (m) => { throw new Error(`${m}
requests: ${paths.join(", ")}`); };
const routeData = log.filter((e) => !NOT_ROUTE_DATA.has(e.path));
const mes = log.filter((e) => e.path === "/me");
if (scenario.startsWith("offline")) {
  // Never guest: no guest banner, navigation kept, and an offline state painted.
  const text = byId.app.textContent;
  if (!byId["guest-banner"].hidden) fail("offline boot adopted the guest role");
  if (byId["menu-btn"].hidden) fail("offline boot hid the navigation");
  if (!/Can't reach Agent Harness Server/.test(text)) fail(`no offline state shown: ${text}`);
  if (scenario === "offline") {
    if (!/Retry/.test(text)) fail("the offline card needs a Retry button");
  } else if (/Retry/.test(text)) fail("a cached identity routes normally and paints the page's own error state");
  if (win.localStorage.getItem("harness.lastRole") !== (scenario === "offline" ? null : "owner")) fail("offline must not change the cached role");
  // Connectivity returns: the online event re-runs the identity check and the route loads.
  network.up = true;
  win.dispatchEvent({ type: "online" });
  await sleep(200);
  if (/Can't reach|Retry/.test(byId.app.textContent)) fail("the online event did not re-run boot identity");
  if (win.localStorage.getItem("harness.lastRole") !== "owner") fail("a successful /me caches the role");
  console.log("ok");
} else if (scenario === "client-update" || scenario === "daemon-update") {
  if (routeData.length || paths.includes("POST /models/warm") || first("/profile") || first("/gpu")) fail("blocked boot must not request route data, warm or profile");
  if (!/Update/.test(byId.app.textContent)) fail("update card was not shown");
  console.log("ok");
} else {
  const health = first("/health");
  if (!health || !mes[0] || mes[0].tick - health.tick !== 1 || mes[0].start - health.start > 5) fail("/health and /me must start together");
  const firstData = routeData[0];
  if (!firstData) fail("no route data requested");
  if (scenario === "me-fallback") {
    if (mes.length !== 2) fail("expected /me then fallback /me before route data");
  } else if (mes.length !== 1) fail(`expected exactly one /me, got ${mes.length}`);
  if (firstData.start < health.end || firstData.start < Math.max(...mes.map((e) => e.end))) fail("route data started before /health and /me resolved");
  // Nothing except /health, /me and /auth/session may start after both resolve and before route data finished sequentially:
  const before = log.filter((e) => e.start < firstData.start);
  const gated = before.filter((e) => ["/gpu", "/models/warm", "/profile"].includes(e.path) && e.end > firstData.start);
  if (gated.length && gated.some((e) => e.start < firstData.start - LATENCY * 2)) fail("route data must not wait on /gpu, /models/warm or /profile");
  const warm = paths.includes("POST /models/warm");
  if (scenario === "member" && first("/gpu")) fail("member must not request /gpu");
  if (warm) fail("boot must not warm the model; only a local-model selection does (#311)");

  // Round trips before route data: waves of requests that had to finish before the next began.
  const starts = before.filter((e) => ["/health", "/me", "/auth/session"].includes(e.path)).map((e) => e.start).sort((x, y) => x - y);
  const waves = { size: starts.filter((t, i) => i === 0 || t - starts[i - 1] > LATENCY / 2).length };
  if (scenario === "owner" && waves.size > 2) fail(`expected <=2 round trips before route data, got ${waves.size}`);
  console.log("ok");
}
process.exit(0);
