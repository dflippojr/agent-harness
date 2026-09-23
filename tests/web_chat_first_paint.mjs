// UI harness: navigating to #/chat must paint the page shell (chat-wrap, feed) before the
// /chats/options (or /chats/<id>) fetch resolves, so the route feels instant (#152).
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
  href: "http://localhost/#/",
  origin: "http://localhost",
  hash: "#/",
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

// Controls when /chats/options resolves, so the test can assert the shell is already
// painted while this fetch is still pending.
let releaseChatOptions;
const chatOptionsGate = new Promise((r) => { releaseChatOptions = r; });

const fetched = [];
const fakeFetch = async (url) => {
  const href = String(url);
  fetched.push(href);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/chats/options") { await chatOptionsGate; return jsonResp({ backends: [{ name: "local", models: ["local"], efforts: [] }], default_backend: "local" }); }
  if (path.startsWith("/chats")) return jsonResp([]);
  return jsonResp({});
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
  EventSource: class { constructor() { this.readyState = 1; } close() {} },
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

runInContext(appSrc, sandbox);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Navigate to #/chat while /chats/options is still pending (gated above).
loc.hash = "#/chat";
loc.href = "http://localhost/#/chat";
win.dispatchEvent({ type: "hashchange" });

// Give the route's synchronous shell-painting code a chance to run, but not the gated fetch.
await sleep(30);

const hasChatWrap = (node) => {
  if (!(node instanceof El)) return false;
  if (node.className && node.className.split(/\s+/).includes("chat-wrap")) return true;
  return (node.childNodes || []).some(hasChatWrap);
};

if (!hasChatWrap(byId.app)) {
  throw new Error("chat shell (.chat-wrap) was not painted before /chats/options resolved");
}
if (byId.title.hidden) {
  throw new Error("header title should be visible (though possibly empty) before data loads");
}

releaseChatOptions();
await sleep(30);

if (!fetched.some((u) => u.includes("/chats/options"))) {
  throw new Error("expected /chats/options to have been requested");
}
if (byId.title.textContent !== "Chat") {
  throw new Error(`expected header title to read "Chat" once options resolved, got "${byId.title.textContent}"`);
}

console.log("ok");
