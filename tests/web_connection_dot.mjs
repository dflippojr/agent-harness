// UI harness: load app.js and prove the header connection dot stays live across
// routes that have no page stream, and that a real disconnect/reconnect still works.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const appSrc = readFileSync(join(root, "harness/web/app.js"), "utf8")
  .replace(/import \{[^}]+\} from "\.\/client\.mjs";\r?\n/, "");

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
    this.defaultValue = this.value;
    this.defaultChecked = false;
    this.checked = false;
    this.selected = false;
    this.options = [];
    this.style = {
      _p: {},
      width: "",
      setProperty(k, v) { this._p[k] = v; },
      removeProperty(k) { delete this._p[k]; },
      getPropertyValue(k) { return this._p[k] || ""; },
    };
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
  }
  get textContent() {
    if (this.childNodes.length) {
      return this.childNodes.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
    }
    return this._text;
  }
  set textContent(v) { this._text = String(v); this.childNodes = []; }
  get innerHTML() { return this.textContent; }
  set innerHTML(v) { this.textContent = v; }
  get firstElementChild() { return this.childNodes.find((c) => c instanceof El) || null; }
  append(...nodes) {
    for (const n of nodes.flat()) {
      if (n === null || n === undefined || n === false) continue;
      this.childNodes.push(n instanceof El ? n : String(n));
    }
    if (this.tagName === "SELECT" && !this.value) {
      const opt = this.childNodes.find((c) => c instanceof El && c.tagName === "OPTION");
      if (opt) this.value = opt.value || opt.attributes.value || "";
    }
  }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  remove() { this.removed = true; }
  click() { this.dispatchEvent({ type: "click" }); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getContext() {
    return {
      fillRect() {}, fillText() {},
      fillStyle: "", font: "", textAlign: "", textBaseline: "",
    };
  }
  toDataURL() { return "data:image/png;base64,"; }
  setAttribute(k, v) { this.attributes[k] = v; if (k === "id") this.id = v; }
}

const byId = {};
const make = (tag, id, extra = {}) => {
  const el = new El(tag, { id, ...extra });
  if (id) byId[id] = el;
  return el;
};

const feature = make("select", "feature-nav");
for (const value of ["agents", "jobs", "images"]) {
  const opt = new El("option", { value });
  opt.value = value;
  feature.options.push(opt);
}
feature.value = "agents";

const doc = new Emitter();
doc.documentElement = new El("html");
doc.documentElement.dataset = {};
doc.documentElement.scrollHeight = 1200;
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
  if (sel === "input, textarea, select") return [feature];
  return [];
};
doc.createElement = (tag) => new El(tag);
doc.createTextNode = (t) => String(t);

make("main", "app");
make("h1", "title");
make("button", "back");
make("span", "conn");
make("a", "profile-icon");
make("button", "menu-btn");
make("nav", "nav-drawer");
make("div", "drawer-scrim");
make("div", "drawer-chats");
make("span", "drawer-profile-icon");
make("div", "fab-host");
make("a", "fab");
make("header", "bar");
make("div", "guest-banner");
make("div", "toast");

const loc = {
  href: "http://localhost/#/",
  origin: "http://localhost",
  hash: "#/",
  protocol: "http:",
  pathname: "/",
  replace(url) { this.hash = String(url).startsWith("#") ? String(url) : `#${url}`; },
};
const storage = () => {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
};

const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body,
  text: async () => JSON.stringify(body),
  blob: async () => body,
});

const imagePayload = {
  images: [],
  status: {
    phase: "idle", queued: 0, progress: {},
    modes: { fast: { label: "Fast", available: true, resolution: "standard" } },
    aspect_ratios: ["1:1"],
    resolutions: { standard: { label: "Standard", sizes: { "1:1": [1024, 1024] } } },
    edit: { enabled: false, available: false, setup: "" },
    upscale: { available: false },
  },
};

const jobDetail = {
  id: "job1", name: "Morning check", prompt: "Look around", cron: "0 8 * * *",
  backend: "local", model: "", notify: "low", enabled: true, project: "scratch", recent: [],
};
const imageDetail = {
  id: "img1", status: "done", prompt: "a cat", model: "fast", width: 1024, height: 1024,
  seed: 1, source: "web", created_at: 1, finished_at: 2, scale: 1, service: { edit: {} },
};
const sessionDetail = {
  id: "sess1", title: "Demo session", status: "done", project: "scratch", target: "tower",
  backend: "local", model: "local", totals: {}, created_at: 1, updated_at: 2, workspace: "/tmp",
};

const fetched = [];
const fakeFetch = async (url) => {
  const href = String(url);
  fetched.push(href);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) {
    return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  }
  if (path === "/me") {
    return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  }
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/sessions" || path.startsWith("/sessions?")) return jsonResp([]);
  if (path === "/sessions/sess1") return jsonResp(sessionDetail);
  if (path === "/queue") return jsonResp([]);
  if (path === "/gpu") return jsonResp({ manual: false, state: "clear" });
  if (path === "/projects") return jsonResp([{ name: "scratch", target: "tower" }]);
  if (path === "/jobs") return jsonResp([]);
  if (path === "/jobs/job1") return jsonResp(jobDetail);
  if (path === "/images") return jsonResp(imagePayload);
  if (path === "/images/img1") return jsonResp(imageDetail);
  if (path === "/maintenance") {
    return jsonResp({
      free_gb: 100, total_gb: 500, workspaces_mb: 1, workspaces: [], quota_mb: 2048,
      containers: [], backup: { enabled: false }, image_archive: { enabled: false }, runners: [],
    });
  }
  if (path === "/models") return jsonResp([]);
  if (path.startsWith("/backends")) return jsonResp([{ name: "local", available: true }]);
  return jsonResp({});
};

const sources = [];
class FakeEventSource extends Emitter {
  constructor(url) {
    super();
    this.url = String(url);
    this.readyState = 1;
    this.onopen = null;
    this.onerror = null;
    sources.push(this);
    setTimeout(() => { if (this.readyState === 1) this.onopen?.(); }, 0);
  }
  close() { this.readyState = FakeEventSource.CLOSED; }
  fail() {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.();
  }
}
FakeEventSource.CONNECTING = 0;
FakeEventSource.OPEN = 1;
FakeEventSource.CLOSED = 2;

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
  EventSource: FakeEventSource,
  fetch: fakeFetch,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
});

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
  EventSource: FakeEventSource,
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

runInContext(appSrc, sandbox);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const live = () => byId.conn.classList.contains("live");
const assertLive = (where) => {
  if (!live()) throw new Error(`expected connected dot at ${where}; class=${byId.conn.className}`);
};
const waitFor = async (pred, label, ms = 2000) => {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (pred()) return;
    await sleep(20);
  }
  throw new Error(`timeout waiting for ${label}: ${byId.app.textContent.slice(0, 200)}`);
};

await waitFor(() => live(), "initial connection");
assertLive("Agents");

const go = async (hash, label) => {
  loc.hash = hash;
  loc.href = `http://localhost/${hash}`;
  win.dispatchEvent({ type: "hashchange" });
  await sleep(40);
  assertLive(label);
};

await go("#/profile", "Profile");
await waitFor(() => /Account|Settings|Profile/.test(byId.app.textContent + byId.title.textContent), "profile page");
assertLive("Profile painted");

await go("#/profile/disk", "Disk");
await waitFor(() => /Tower|Measuring/.test(byId.app.textContent), "disk page");
assertLive("Disk painted");

await go("#/images", "Images");
await waitFor(() => /Prompt|Fast|Resolution/.test(byId.app.textContent), "images page");
assertLive("Images painted");

await go("#/jobs", "Jobs");
await waitFor(() => /No scheduled jobs|New job/.test(byId.app.textContent), "jobs page");
assertLive("Jobs painted");

await go("#/profile/account", "Account");
await waitFor(() => /Connection|Account/.test(byId.app.textContent + byId.title.textContent), "account page");
assertLive("Account painted");

await go("#/images/img1", "image details");
await waitFor(() => /a cat|Image/.test(byId.app.textContent + byId.title.textContent), "image details");
assertLive("image details painted");

await go("#/images/img1/full", "image fullscreen");
await waitFor(() => live(), "image fullscreen still connected");
assertLive("image fullscreen");

await go("#/jobs/job1", "job details");
await waitFor(() => /Morning check|Job/.test(byId.app.textContent + byId.title.textContent), "job details");
assertLive("job details painted");

await go("#/s/sess1", "session transcript");
await waitFor(() => /Demo session|Transcript/.test(byId.app.textContent), "session transcript");
assertLive("session transcript painted");

await go("#/s/sess1/info", "session info");
await waitFor(() => /Workspace|Session/.test(byId.app.textContent), "session info");
assertLive("session info painted");

// Hostile route ids must never reach fetch/EventSource; valid ids still do.
const reached = (needle) => fetched.some((u) => u.includes(needle)) || sources.some((s) => s.url.includes(needle));
const hostile = ["../x", "%2e%2e", "a%2Fb", "x?y=1", "a".repeat(200)];
for (const kind of ["s", "chat", "images"]) {
  for (const bad of hostile) {
    await go(`#/${kind}/${bad}`, `hostile ${kind}`);
    if (reached(`/${bad}`) || reached("evil") || reached("..")) throw new Error(`hostile id reached a request: ${kind}/${bad}`);
  }
}
await go("#/s/a%5Cb", "backslash session id");
if (fetched.concat(sources.map((s) => s.url)).some((u) => /%5C|%2e|%2F|\\|\.\./i.test(u))) {
  throw new Error("hostile id reached a request");
}
await go("#/s/sess1", "valid session after hostile ids");
await waitFor(() => /Demo session|Transcript/.test(byId.app.textContent), "valid session still loads");
if (!reached("/sessions/sess1/events")) throw new Error("valid session id no longer opens its stream");
await go("#/s/sess1/info", "session info again");

const openSources = () => sources.filter((s) => s.readyState === 1);
if (!openSources().length) throw new Error("expected an app-level EventSource to stay open off Agents");

for (const src of openSources()) src.fail();
await sleep(20);
if (live()) throw new Error("expected gray after genuine disconnect");

doc.hidden = true;
doc.visibilityState = "hidden";
doc.dispatchEvent({ type: "visibilitychange" });
await sleep(20);
doc.hidden = false;
doc.visibilityState = "visible";
doc.dispatchEvent({ type: "visibilitychange" });
await waitFor(() => live(), "reconnect after foreground resume");
assertLive("after PWA resume");

console.log("ok");
