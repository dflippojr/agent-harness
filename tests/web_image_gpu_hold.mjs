// UI harness: Images hides uninstalled models, Queue Generation during a GPU
// hold, and spaces the gallery below Generate. (#186)
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
    this.hidden = !!attrs.hidden;
    this.value = attrs.value || "";
    this.href = attrs.href || "";
    this.type = attrs.type || "";
    this.disabled = false;
    this.defaultValue = this.value;
    this.checked = false;
    this.selected = !!attrs.selected;
    this.options = [];
    this.style = { _p: {}, setProperty(k, v) { this._p[k] = v; }, removeProperty(k) { delete this._p[k]; }, getPropertyValue(k) { return this._p[k] || ""; } };
    this.dataset = {};
    this.classList = {
      _sync: () => { this.classList._s = new Set(String(this.className || "").split(/\s+/).filter(Boolean)); },
      _s: new Set(String(attrs.class || "").split(/\s+/).filter(Boolean)),
      add: (c) => { this.classList._sync(); this.classList._s.add(c); this.className = [...this.classList._s].join(" "); },
      remove: (c) => { this.classList._sync(); this.classList._s.delete(c); this.className = [...this.classList._s].join(" "); },
      toggle: (c, force) => {
        this.classList._sync();
        const on = force === undefined ? !this.classList._s.has(c) : !!force;
        if (on) this.classList.add(c); else this.classList.remove(c);
        return on;
      },
      contains: (c) => { this.classList._sync(); return this.classList._s.has(c); },
    };
    this._text = "";
    this.parentNode = null;
  }
  get firstElementChild() { return this.childNodes.find((c) => c instanceof El) || null; }
  get isConnected() { return !!this.parentNode; }
  get textContent() {
    if (this.childNodes.length) {
      return this.childNodes.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
    }
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
    if (this.tagName === "SELECT" && !this.value) {
      const opt = this.childNodes.find((c) => c instanceof El && c.tagName === "OPTION");
      if (opt) this.value = opt.value || opt.attributes.value || "";
    }
  }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  remove() { this.removed = true; }
  click() { this.dispatchEvent({ type: "click" }); }
  focus() {}
  blur() {}
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  setAttribute(k, v) {
    this.attributes[k] = v;
    if (k === "id") this.id = v;
    if (k === "href") this.href = v;
    if (k === "value") this.value = v;
    if (k === "class") this.className = v;
    if (k === "type") this.type = v;
    if (k === "hidden") this.hidden = true;
  }
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
doc.body = new El("body");
doc.hidden = false;
doc.visibilityState = "visible";
doc.getElementById = (id) => byId[id] || null;
doc.querySelector = (sel) => {
  if (sel === 'link[rel="apple-touch-icon"]' || sel === 'link[rel="icon"]') return new El("link");
  if (sel === "#app") return byId.app;
  return null;
};
doc.querySelectorAll = (sel) => (sel === "input, textarea, select" ? [feature] : []);
doc.createElement = (tag) => new El(tag);
doc.createTextNode = (t) => String(t);

make("main", "app");
make("h1", "title");
make("button", "back");
make("span", "conn");
make("a", "profile-icon");
make("button", "menu-btn");
make("nav", "nav-drawer");
byId["nav-drawer"].hidden = true;
byId["nav-drawer"].querySelectorAll = () => [];
byId["nav-drawer"].querySelector = () => null;
make("div", "drawer-scrim");
make("div", "drawer-chats");
make("span", "drawer-profile-icon");
make("div", "fab-host");
make("a", "fab");
make("header", "bar");
make("div", "guest-banner");
make("div", "toast");

const historyStack = ["#/"];
const loc = {
  href: "http://localhost/#/",
  origin: "http://localhost",
  _hash: "#/",
  get hash() { return this._hash; },
  set hash(v) {
    const next = !v || v === "#" ? "#/" : String(v);
    if (next === this._hash) return;
    this._hash = next;
    this.href = `http://localhost/${next}`;
    historyStack.push(next);
  },
  protocol: "http:",
  pathname: "/",
  replace(url) {
    const next = String(url).startsWith("#") ? String(url) : `#${url}`;
    if (next === this._hash) return;
    this._hash = next;
    this.href = `http://localhost/${next}`;
    historyStack[historyStack.length - 1] = next;
    this.onHashReplace?.();
  },
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
});

const MIXED_MODES = {
  fast: { label: "Z-Image-Turbo (fast, Apache 2.0)", available: true, resolution: "standard" },
  quality: { label: "Qwen-Image-2512 (quality, Apache 2.0)", available: true, resolution: "high" },
  "quality-fast": { label: "Qwen quality (fast, 4-step)", available: false, resolution: "high" },
  "flux-fast": { label: "FLUX.2 klein 4B (fast, Apache 2.0)", available: false, resolution: "standard" },
};
const NONE_MODES = Object.fromEntries(Object.entries(MIXED_MODES).map(([k, spec]) => [k, { ...spec, available: false }]));
const imageStatus = (modes) => ({
  phase: "idle", queued: 0, progress: {}, modes,
  aspect_ratios: ["1:1"],
  resolutions: {
    standard: { label: "Standard", sizes: { "1:1": [1024, 1024] } },
    high: { label: "High", sizes: { "1:1": [1328, 1328] } },
  },
  edit: { available: false, enabled: false },
  upscale: { available: false },
});

let gpuPayload = { manual: false, state: "clear" };
let imagesPayload = { images: [], status: imageStatus(MIXED_MODES) };
const fakeFetch = async (url) => {
  const href = String(url);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) {
    return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  }
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/sessions" || path.startsWith("/sessions?")) return jsonResp([]);
  if (path === "/queue") return jsonResp([]);
  if (path === "/gpu") return jsonResp(gpuPayload);
  if (path === "/projects") return jsonResp([{ name: "scratch", target: "tower" }]);
  if (path === "/models") return jsonResp([{ name: "Qwen", default: true, context_tokens: 32768 }]);
  if (path === "/models/status") return jsonResp([{ name: "Qwen", state: "paused" }]);
  if (path === "/models/warm") return jsonResp({});
  if (path === "/templates") return jsonResp([]);
  if (path === "/skills/enabled") return jsonResp([]);
  if (path.startsWith("/backends")) {
    return jsonResp([
      { name: "local", available: true, model: "Qwen" },
      { name: "claude", available: true, model: "claude-sonnet" },
    ]);
  }
  if (path.startsWith("/chats")) return jsonResp([]);
  if (path === "/jobs") return jsonResp([]);
  if (path === "/images" || path.startsWith("/images?")) return jsonResp(imagesPayload);
  if (path === "/images/warmup") return jsonResp({ ok: true });
  return jsonResp({});
};

class FakeEventSource extends Emitter {
  constructor(url) { super(); this.url = String(url); this.readyState = 1; this.onopen = null; }
  close() { this.readyState = 2; }
}

const win = new Emitter();
Object.assign(win, {
  addEventListener: (...a) => Emitter.prototype.addEventListener.call(win, ...a),
  removeEventListener: (...a) => Emitter.prototype.removeEventListener.call(win, ...a),
  dispatchEvent: (...a) => Emitter.prototype.dispatchEvent.call(win, ...a),
  localStorage: storage(),
  sessionStorage: storage(),
  location: loc,
  navigator: { serviceWorker: undefined, userAgent: "test" },
  history: { replaceState() {}, back() {} },
  scrollTo() {},
  confirm: () => false,
  innerHeight: 800,
  scrollY: 0,
  EventSource: FakeEventSource,
  fetch: fakeFetch,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
});

loc.onHashReplace = () => win.dispatchEvent({ type: "hashchange" });
globalThis.window = win;
globalThis.localStorage = win.localStorage;
globalThis.location = loc;
globalThis.fetch = fakeFetch;
const { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } = await import("../harness/web/client.mjs");

const sandbox = createContext({
  window: win, document: doc, location: loc, history: win.history, navigator: win.navigator,
  localStorage: win.localStorage, sessionStorage: win.sessionStorage, EventSource: FakeEventSource,
  fetch: fakeFetch, URL, AbortController, TextDecoder, console, getComputedStyle: (el) => el.style,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: win.requestAnimationFrame, cancelAnimationFrame: win.cancelAnimationFrame,
  confirm: win.confirm, agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL, Node, Event, JSON, Date, Math,
  Number, String, Boolean, Array, Object, Set, Map, Promise, Error, parseInt, encodeURIComponent,
  decodeURIComponent, undefined,
});

runInContext(appSrc, sandbox);

process.on("unhandledRejection", (err) => {
  console.error(err);
  process.exit(1);
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = async (pred, label, ms = 2000) => {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (pred()) return;
    await sleep(20);
  }
  throw new Error(`timeout waiting for ${label}: ${byId.app.textContent.slice(0, 240)}`);
};

const go = async (hash) => {
  loc.hash = hash;
  loc.href = `http://localhost/${hash}`;
  win.dispatchEvent({ type: "hashchange" });
  await sleep(40);
};

const walk = (node, acc = []) => {
  if (!(node instanceof El)) return acc;
  acc.push(node);
  for (const c of node.childNodes || []) walk(c, acc);
  return acc;
};

const optVal = (el) => el.value || el.attributes.value || "";

const modelSelect = () => walk(byId.app).find((el) =>
  el.tagName === "SELECT" && walk(el).some((o) => o.tagName === "OPTION" && optVal(o) === "fast"));

const emptyNote = () => walk(byId.app).find((el) => String(el.className).split(/\s+/).includes("image-models-empty"));

const gallery = () => walk(byId.app).find((el) => String(el.className).split(/\s+/).includes("image-grid"));

const submitBtn = () => walk(byId.app).find((el) =>
  el.tagName === "BUTTON" && el.type === "submit" && /^(Generate|Queue Generation)$/.test(el.textContent));

const optionValues = (sel) => walk(sel).filter((o) => o.tagName === "OPTION").map(optVal);

const assertQueued = (queued) => {
  const btn = submitBtn();
  if (!btn) throw new Error("generate/queue button missing");
  if (queued) {
    if (btn.textContent !== "Queue Generation") throw new Error(`expected Queue Generation, got ${btn.textContent}`);
    if (!btn.classList.contains("queued")) throw new Error(`expected queued class, got ${btn.className}`);
    if (btn.classList.contains("primary")) throw new Error("queued button should not keep primary");
  } else {
    if (btn.textContent !== "Generate") throw new Error(`expected Generate, got ${btn.textContent}`);
    if (btn.classList.contains("queued")) throw new Error("Generate should not have queued class");
    if (!btn.classList.contains("primary")) throw new Error(`expected primary, got ${btn.className}`);
  }
};

await waitFor(() => !byId.title.hidden, "boot");

gpuPayload = { manual: false, state: "clear" };
imagesPayload = { images: [], status: imageStatus(MIXED_MODES) };
await go("#/images");
await waitFor(() => modelSelect(), "images form with installed models");
const installed = optionValues(modelSelect());
if (installed.includes("quality-fast") || installed.includes("flux-fast")) {
  throw new Error(`uninstalled models must not appear: ${installed.join(",")}`);
}
if (!installed.includes("fast") || !installed.includes("quality")) {
  throw new Error(`installed models missing: ${installed.join(",")}`);
}
if (walk(modelSelect()).some((o) => /not installed/i.test(o.textContent))) {
  throw new Error("selector still labels a model as not installed");
}
if (emptyNote()) throw new Error("empty-state should not show when models are installed");
assertQueued(false);
if (!gallery()) throw new Error("gallery missing");

gpuPayload = { manual: true, state: "paused", manual_remaining_seconds: null };
await go("#/agents");
await go("#/images");
await waitFor(() => submitBtn()?.textContent === "Queue Generation", "hold on queues generation");
assertQueued(true);

gpuPayload = { manual: false, state: "paused" };
await go("#/agents");
await go("#/images");
await waitFor(() => submitBtn()?.textContent === "Queue Generation", "scheduled hold queues generation");
assertQueued(true);

gpuPayload = { manual: false, state: "clear" };
await go("#/agents");
await go("#/images");
await waitFor(() => submitBtn()?.textContent === "Generate", "hold off restores Generate");
assertQueued(false);

imagesPayload = { images: [], status: imageStatus(NONE_MODES) };
await go("#/agents");
await go("#/images");
await waitFor(() => emptyNote(), "empty-state when nothing is installed");
if (modelSelect()) throw new Error("model dropdown must not render when nothing is installed");
if (!/ops\/images-models\.ps1/.test(emptyNote().textContent) || !/Comfy models directory/.test(emptyNote().textContent)) {
  throw new Error(`empty-state should say where models get installed, got ${emptyNote().textContent}`);
}
if (submitBtn() && !submitBtn().disabled) throw new Error("Generate must be disabled with no models");

await go("#/agents");
console.log("ok");
process.exit(0);
