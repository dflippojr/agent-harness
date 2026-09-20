// UI harness: load app.js in a stub DOM, enter protocol-blocked state, and prove
// hashchange / feature-nav / visibility / online / SW messages cannot dismiss the
// update card or start /api/v1 traffic.
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
  append(...nodes) {
    for (const n of nodes.flat()) {
      if (n === null || n === undefined || n === false) continue;
      this.childNodes.push(n instanceof El ? n : String(n));
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
  href: "http://localhost/",
  origin: "http://localhost",
  hash: "#/",
  protocol: "http:",
  pathname: "/",
  replace(url) { this.hash = String(url); },
};
const storage = () => {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
};

const fetches = [];
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body,
  text: async () => JSON.stringify(body),
  blob: async () => body,
});

const fakeFetch = async (url) => {
  const href = String(url);
  fetches.push(href);
  if (href.includes("/health") && !href.includes("/api/")) {
    return jsonResp({
      protocols: { admin: { min: 3, max: 4 } },
      update_hint: {},
    });
  }
  return jsonResp({ detail: "upgrade required", error: { code: "client_update_required" } }, 426);
};

class FakeEventSource extends Emitter {
  constructor(url) { super(); fetches.push(String(url)); this.url = url; this.readyState = 1; }
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
  history: { back() { loc.hash = "#/"; }, replaceState() {} },
  scrollTo() {},
  confirm: () => false,
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

const waitForCard = async () => {
  for (let i = 0; i < 40; i++) {
    if (byId.app.textContent.includes("Reload and update")) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error(`blocking card missing: ${byId.app.textContent}`);
};
await waitForCard();

const apiCalls = () => fetches.filter((u) => /\/api\/v1\b|\/api\/admin\b/.test(u));
if (apiCalls().length) throw new Error(`API traffic during block paint: ${apiCalls()}`);

const before = byId.app.textContent;
loc.hash = "#/profile";
win.dispatchEvent({ type: "hashchange" });
await new Promise((r) => setTimeout(r, 50));

feature.value = "jobs";
feature.dispatchEvent({ type: "change" });
if (loc.hash !== "#/profile" && loc.hash !== "#/jobs") {
  // go() may rewrite the hash; the card must still stay.
}
win.dispatchEvent({ type: "hashchange" });
await new Promise((r) => setTimeout(r, 50));

doc.visibilityState = "visible";
doc.hidden = false;
doc.dispatchEvent({ type: "visibilitychange" });
win.dispatchEvent({ type: "online" });
win.navigator.serviceWorker = {
  addEventListener() {},
  controller: { postMessage() {} },
  getRegistration: async () => ({ active: { postMessage() {} }, update: async () => {} }),
};
win.dispatchEvent({ type: "message", data: "PURGE_SHELL", origin: loc.origin });
await new Promise((r) => setTimeout(r, 50));

if (!byId.app.textContent.includes("Reload and update")) {
  throw new Error(`blocking card left after nav: ${byId.app.textContent}`);
}
if (byId.app.textContent !== before && !byId.app.textContent.includes("Update Agent Harness Web")) {
  throw new Error(`blocking card replaced: ${byId.app.textContent}`);
}
if (apiCalls().length) {
  throw new Error(`API calls while blocked: ${apiCalls().join(", ")}`);
}
if (!fetches.some((u) => u.includes("/health"))) {
  throw new Error("expected compatibility /health poll");
}
console.log("ok");
