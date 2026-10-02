// Session-page VM: mask compaction must not crash summary notes or ctxUsed (#248).
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";

const fail = (msg) => { throw new Error(msg); };

class Emitter {
  constructor() { this._l = {}; }
  addEventListener(type, fn) { (this._l[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this._l[type] = (this._l[type] || []).filter((f) => f !== fn); }
  dispatchEvent(ev) {
    for (const fn of [...(this._l[ev.type] || [])]) fn.call(this, { currentTarget: this, target: this, preventDefault() {}, ...ev });
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
    this.id = attrs.id || "";
    this.hidden = false;
    this.value = attrs.value || "";
    this.href = attrs.href || "";
    this.type = attrs.type || "";
    this.disabled = false;
    this.options = [];
    this.style = {
      _p: {},
      setProperty(k, v) { this._p[k] = v; },
      removeProperty(k) { delete this._p[k]; },
      getPropertyValue(k) { return this._p[k] || ""; },
    };
    this.dataset = {};
    this.offsetHeight = 48;
    this.scrollTop = 0;
    this.scrollHeight = 1200;
    this.clientHeight = 800;
    this._text = "";
    this._html = null;
    this.parentNode = null;
    this._className = "";
    this.classList = {
      _s: new Set(),
      add: (c) => { this.classList._s.add(c); this._syncClass(); },
      remove: (c) => { this.classList._s.delete(c); this._syncClass(); },
      toggle: (c, force) => {
        const on = force === undefined ? !this.classList._s.has(c) : !!force;
        if (on) this.classList.add(c); else this.classList.remove(c);
        return on;
      },
      contains: (c) => this.classList._s.has(c),
    };
    if (attrs.class) this.className = attrs.class;
  }
  _syncClass() { this._className = [...this.classList._s].join(" "); }
  get className() { return this._className; }
  set className(v) {
    this._className = String(v || "");
    this.classList._s = new Set(this._className.split(/\s+/).filter(Boolean));
  }
  get isConnected() { return !!this.parentNode; }
  get textContent() {
    if (this.childNodes.length) return this.childNodes.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
    return this._text;
  }
  set textContent(v) { this._text = String(v); this.childNodes = []; this._html = null; }
  get innerHTML() { return this._html ?? this.textContent; }
  set innerHTML(v) { this._html = String(v); this._text = String(v); this.childNodes = []; }
  append(...nodes) {
    for (const n of nodes.flat()) {
      if (n === null || n === undefined || n === false) continue;
      if (n instanceof El) n.parentNode = this;
      this.childNodes.push(n instanceof El ? n : String(n));
    }
  }
  prepend(...nodes) { const old = this.childNodes; this.childNodes = []; this.append(...nodes); this.childNodes.push(...old); }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  remove() {
    if (this.parentNode) {
      const i = this.parentNode.childNodes.indexOf(this);
      if (i !== -1) this.parentNode.childNodes.splice(i, 1);
    }
    this.parentNode = null;
  }
  click() { this.dispatchEvent({ type: "click" }); }
  focus() {}
  blur() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  setAttribute(k, v) {
    this.attributes[k] = v;
    if (k === "id") this.id = v;
    if (k === "hidden" || k === "disabled") this[k] = true;
    if (k === "class") this.className = v;
  }
  getAttribute(k) { return this.attributes[k] ?? null; }
}

const walk = (node, pred, out = []) => {
  if (!(node instanceof El)) return out;
  if (pred(node)) out.push(node);
  for (const c of node.childNodes) walk(c, pred, out);
  return out;
};

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
feature.value = "agents";

const doc = new Emitter();
doc.documentElement = new El("html");
doc.documentElement.scrollHeight = 1200;
doc.documentElement.clientHeight = 800;
doc.documentElement.scrollTop = 0;
doc.documentElement.style = { setProperty() {}, removeProperty() {}, getPropertyValue() { return ""; } };
doc.documentElement.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
doc.documentElement.dataset = {};
doc.body = make("body");
doc.scrollingElement = doc.documentElement;
doc.hidden = false;
doc.visibilityState = "visible";
doc.getElementById = (id) => byId[id] || null;
doc.querySelector = (sel) => {
  if (sel === 'link[rel="apple-touch-icon"]' || sel === 'link[rel="icon"]') return new El("link");
  if (sel === "#app") return byId.app;
  if (sel.startsWith(".")) {
    const cls = sel.slice(1);
    return walk(doc.body, (n) => n.classList.contains(cls))[0]
      || walk(byId.app, (n) => n.classList.contains(cls))[0]
      || null;
  }
  return null;
};
doc.querySelectorAll = (sel) => {
  if (sel === ".jump") return walk(doc.body, (n) => n.classList.contains("jump"));
  if (sel === "input, textarea, select") return [feature];
  if (sel.startsWith(".")) {
    const cls = sel.slice(1);
    return [...walk(doc.body, (n) => n.classList.contains(cls)), ...walk(byId.app, (n) => n.classList.contains(cls))];
  }
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
  replace(url) {
    const next = String(url).startsWith("#") ? String(url) : `#${url}`;
    if (next === this.hash) return;
    this.hash = next;
    this.href = `http://localhost/${next}`;
  },
};
const storage = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
};
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body, text: async () => JSON.stringify(body),
});

const sessionDetail = {
  id: "sess1", title: "Demo session", status: "running", project: "scratch", target: "tower",
  backend: "local", model: "local", totals: { prompt_tokens: 100, completion_tokens: 20 },
  context_used: 9000, context_limit: 10000, created_at: 1, updated_at: 2, workspace: "/tmp",
};

const fakeFetch = async (url) => {
  const href = String(url);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/sessions" || path.startsWith("/sessions?")) return jsonResp([]);
  if (path === "/sessions/sess1") return jsonResp(sessionDetail);
  if (path === "/queue") return jsonResp([]);
  if (path === "/projects") return jsonResp([{ name: "scratch", target: "tower" }]);
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
  emit(type, data, seq, extra = {}) {
    const msg = { data: JSON.stringify({ seq, type, data, ...extra }) };
    for (const fn of [...(this._l[type] || [])]) fn.call(this, msg);
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
  localStorage: storage(), sessionStorage: storage(), location: loc,
  navigator: { serviceWorker: undefined, userAgent: "test" },
  history: { back() {}, replaceState() {} },
  scrollTo() {}, confirm: () => false, innerHeight: 800, scrollY: 0, pageYOffset: 0,
  caches: undefined, EventSource: FakeEventSource, fetch: fakeFetch,
  requestAnimationFrame: (fn) => setTimeout(fn, 0), cancelAnimationFrame: (id) => clearTimeout(id),
});
globalThis.window = win;
globalThis.localStorage = win.localStorage;
globalThis.location = loc;
globalThis.fetch = fakeFetch;
const { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } = await import("../harness/web/client.mjs");

const sandbox = createContext({
  window: win, document: doc, location: loc, history: win.history, navigator: win.navigator,
  localStorage: win.localStorage, sessionStorage: win.sessionStorage, EventSource: FakeEventSource, fetch: fakeFetch,
  URL, AbortController, TextDecoder, console, getComputedStyle: (el) => el.style, setTimeout, clearTimeout,
  setInterval, clearInterval, requestAnimationFrame: win.requestAnimationFrame,
  cancelAnimationFrame: win.cancelAnimationFrame, confirm: win.confirm,
  agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL, Node, Event, JSON, Date, Math, Number, String, Boolean, Array, Object,
  Set, Map, Promise, Error, parseInt, encodeURIComponent, decodeURIComponent, undefined,
});
runApp(sandbox);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = async (pred, label, ms = 2000) => {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (pred()) return;
    await sleep(20);
  }
  fail(`timeout waiting for ${label}: ${byId.app.textContent.slice(0, 400)}`);
};

const MASK = "Replaced old tool outputs with recoverable receipts (~4K tokens saved)";
const FAILED = "the summary failed";
const ELIDE = "Trimmed old tool output: ~8K → ~5K tokens";
const SUMMARY = "Context condensed: ~8K → ~3K tokens (12 messages summarized)";

const sessionStream = () => sources.filter((s) => s.url.includes("/sessions/sess1/events") && s.readyState === 1).pop();

const openSession = async () => {
  loc.hash = "#/agents";
  loc.href = "http://localhost/#/agents";
  win.dispatchEvent({ type: "hashchange" });
  await sleep(40);
  loc.hash = "#/s/sess1";
  loc.href = "http://localhost/#/s/sess1";
  win.dispatchEvent({ type: "hashchange" });
  await waitFor(() => /Demo session/.test(byId.app.textContent) && sessionStream(), "session transcript");
  return sessionStream();
};

const text = () => byId.app.textContent;

// mask with no summary note showing: a note appears, ctx meter stays at 90%, no failed-summary copy.
let es = await openSession();
if (!text().includes("90% context")) fail(`expected 90% context before mask: ${text()}`);
es.emit("compaction", { tier: "mask", tokens_saved: 4200, characters_saved: 12600 }, 1);
await sleep(20);
if (!text().includes(MASK)) fail(`mask with no note: ${text()}`);
if (text().includes(FAILED) || text().includes("~0 → ~0")) fail(`mask with no note leaked summary copy: ${text()}`);
if (!text().includes("90% context")) fail(`mask must not change ctxUsed: ${text()}`);
if (text().includes("Trimmed old tool output")) fail(`mask must not use the elide label: ${text()}`);

// mask while a summary note is showing: add the mask line, leave the summary progress alone.
es = await openSession();
es.emit("compaction_started", { messages: 12, tokens_before: 8000 }, 1);
await sleep(20);
if (!text().includes("Condensing older context")) fail(`expected summary progress: ${text()}`);
const beforeMask = text();
es.emit("compaction", { tier: "mask", tokens_saved: 4200, characters_saved: 12600 }, 2);
await sleep(20);
if (!text().includes(MASK)) fail(`mask during summary: ${text()}`);
if (!text().includes("Condensing older context")) fail(`mask must not finish the summary note: ${text()}`);
if (text().includes(FAILED)) fail(`mask must not mark the summary as failed: ${text()}`);
if (!text().includes("90% context")) fail(`mask during summary must not change ctxUsed: ${text()}`);
if (!beforeMask.includes("Condensing older context")) fail("precondition");

// mask then summary (the live _maybe_compact order): summary note still completes.
es = await openSession();
es.emit("compaction", { tier: "mask", tokens_saved: 4200, characters_saved: 12600 }, 1);
await sleep(20);
es.emit("compaction_started", { messages: 12, tokens_before: 8000 }, 2);
await sleep(20);
if (!text().includes(MASK) || !text().includes("Condensing older context")) fail(`mask then start: ${text()}`);
es.emit("compaction", {
  tier: "summary", tokens_before: 8000, tokens_after: 3000, summarized_messages: 12, summary: "handoff",
}, 3);
await sleep(20);
if (!text().includes(MASK)) fail(`mask note lost after summary: ${text()}`);
if (!text().includes(SUMMARY)) fail(`summary did not complete: ${text()}`);
if (text().includes(FAILED) || text().includes("Condensing older context")) fail(`summary still in progress: ${text()}`);
if (!text().includes("30% context")) fail(`summary should update ctxUsed: ${text()}`);
if (!text().includes("Show summary") && !text().includes("handoff")) fail(`summary body missing: ${text()}`);

// summary rendering without a preceding mask stays the same.
es = await openSession();
es.emit("compaction_started", { messages: 12, tokens_before: 8000 }, 1);
await sleep(20);
es.emit("compaction", {
  tier: "summary", tokens_before: 8000, tokens_after: 3000, summarized_messages: 12, summary: "handoff",
}, 2);
await sleep(20);
if (!text().includes(SUMMARY)) fail(`summary-only: ${text()}`);
if (text().includes(MASK) || text().includes(FAILED)) fail(`summary-only leaked mask copy: ${text()}`);
if (!text().includes("30% context")) fail(`summary-only ctxUsed: ${text()}`);

// elide rendering without a compact note stays the same.
es = await openSession();
es.emit("compaction", { tier: "elide", tokens_before: 8000, tokens_after: 5000 }, 1);
await sleep(20);
if (!text().includes(ELIDE)) fail(`elide: ${text()}`);
if (text().includes(MASK) || text().includes(FAILED) || text().includes("Context condensed")) fail(`elide leaked other copy: ${text()}`);
if (!text().includes("50% context")) fail(`elide should update ctxUsed: ${text()}`);

console.log("ok");
process.exit(0);
