// Shared stub DOM for the Web UI harnesses (#479). Each harness boots the real harness/web/app.js against these stubs
// (see web_app_loader.mjs); a harness extends `El` or tweaks the returned document only where its scenario differs.

export class Emitter {
  constructor() { this._l = {}; }
  addEventListener(type, fn) { (this._l[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this._l[type] = (this._l[type] || []).filter((f) => f !== fn); }
  dispatchEvent(ev) {
    for (const fn of [...(this._l[ev.type] || [])]) fn.call(this, { currentTarget: this, target: this, preventDefault() {}, ...ev });
    return true;
  }
  // Lightweight dispatch for the lib/ harness: no event decoration, no `this` binding.
  emit(type, event = {}) { for (const fn of [...(this._l[type] || [])]) fn(event); }
}

export class Node extends Emitter {}

export class El extends Node {
  constructor(tag, attrs = {}) {
    super();
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.attributes = { ...attrs };
    this.id = attrs.id || "";
    this.hidden = !!attrs.hidden;
    this.value = attrs.value || "";
    this.href = attrs.href || "";
    this.type = attrs.type || "";
    this.disabled = false;
    this.defaultValue = this.value;
    this.defaultChecked = false;
    this.checked = !!attrs.checked;
    this.selected = !!attrs.selected;
    this.options = [];
    this.style = {
      _p: {},
      width: "",
      setProperty(k, v) { this._p[k] = v; },
      removeProperty(k) { delete this._p[k]; },
      getPropertyValue(k) { return this._p[k] || ""; },
    };
    this.dataset = {};
    this.offsetHeight = 48;
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
  get firstElementChild() { return this.childNodes.find((c) => c instanceof El) || null; }
  append(...nodes) {
    for (const n of nodes.flat()) {
      if (n === null || n === undefined || n === false) continue;
      if (n instanceof El) n.parentNode = this;
      this.childNodes.push(n instanceof El ? n : String(n));
      if (n instanceof El && this.tagName === "SELECT" && n.tagName === "OPTION") this.options.push(n);
    }
    if (this.tagName === "SELECT" && !this.value) {
      const opt = this.childNodes.find((c) => c instanceof El && c.tagName === "OPTION");
      if (opt) this.value = opt.value || opt.attributes.value || "";
    }
  }
  prepend(...nodes) { const old = this.childNodes; this.childNodes = []; this.append(...nodes); this.childNodes.push(...old); }
  replaceChildren(...nodes) { this.childNodes = []; this.options = []; this.append(...nodes); }
  remove() {
    this.removed = true;
    if (this.parentNode) {
      const i = this.parentNode.childNodes.indexOf(this);
      if (i !== -1) this.parentNode.childNodes.splice(i, 1);
    }
    this.parentNode = null;
  }
  after(node) {
    const siblings = this.parentNode.childNodes;
    siblings.splice(siblings.indexOf(this) + 1, 0, node);
    node.parentNode = this.parentNode;
  }
  replaceWith(...nodes) {
    if (!this.parentNode) return;
    const parent = this.parentNode;
    const idx = parent.childNodes.indexOf(this);
    if (idx === -1) return;
    for (const n of nodes) if (n instanceof El) n.parentNode = parent;
    parent.childNodes.splice(idx, 1, ...nodes);
    this.parentNode = null;
  }
  click() { this.dispatchEvent({ type: "click" }); }
  focus() {}
  select() {}
  blur() {}
  closest() { return null; }
  querySelector(sel) {
    if (sel === ".drawer-recent") return this._drawerRecent || null;
    return null;
  }
  querySelectorAll() { return []; }
  getContext() {
    return { fillRect() {}, fillText() {}, fillStyle: "", font: "", textAlign: "", textBaseline: "" };
  }
  toDataURL() { return "data:image/png;base64,"; }
  setAttribute(k, v) {
    this.attributes[k] = v;
    if (k === "id") this.id = v;
    if (k === "href") this.href = String(v);
    if (k === "value") this.value = v;
    if (k === "type") this.type = v;
    if (k === "class") this.className = v;
    if (k === "hidden" || k === "disabled") this[k] = true;
  }
  removeAttribute(k) { delete this.attributes[k]; }
  getAttribute(k) { return this.attributes[k] ?? null; }
}

// Elements under `node` (itself included) matching `pred`, depth first.
export const walk = (node, pred, out = []) => {
  if (!(node instanceof El)) return out;
  if (pred(node)) out.push(node);
  for (const c of node.childNodes) walk(c, pred, out);
  return out;
};

export const storage = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
};

// An EventSource stand-in. `sources`, when given, collects every instance and opens it on the next tick.
export function fakeEventSource(sources) {
  class FakeEventSource extends Emitter {
    constructor(url) {
      super();
      this.url = String(url);
      this.readyState = 1;
      this.onopen = null;
      this.onerror = null;
      if (sources) {
        sources.push(this);
        setTimeout(() => { if (this.readyState === 1) this.onopen?.(); }, 0);
      }
    }
    close() { this.readyState = FakeEventSource.CLOSED; }
    fail() {
      this.readyState = FakeEventSource.CLOSED;
      this.onerror?.();
    }
    emit(type, data, seq, extra = {}) {
      const msg = { data: JSON.stringify({ seq, type, data, ...extra }) };
      for (const fn of [...(this._l[type] || [])]) fn.call(this, msg);
    }
  }
  FakeEventSource.CONNECTING = 0;
  FakeEventSource.OPEN = 1;
  FakeEventSource.CLOSED = 2;
  return FakeEventSource;
}

// The document plus the elements app.js looks up by id. `focusables` makes "input, textarea, select" return the
// feature select; a harness overrides further hooks on the returned `doc` or `byId` entries.
export function createDocument({ ElClass = El, features = ["agents", "jobs", "images"], feature: current = features[0],
  focusables = true } = {}) {
  const byId = {};
  const make = (tag, id, extra = {}) => {
    const el = new ElClass(tag, { id, ...extra });
    if (id) byId[id] = el;
    return el;
  };
  const select = make("select", "feature-nav");
  for (const value of features) {
    const opt = new ElClass("option", { value });
    opt.value = value;
    select.options.push(opt);
  }
  select.value = current;

  const doc = new Emitter();
  doc.documentElement = new ElClass("html");
  doc.documentElement.scrollHeight = 1200;
  doc.body = new ElClass("body");
  doc.hidden = false;
  doc.visibilityState = "visible";
  doc.getElementById = (id) => byId[id] || null;
  doc.querySelector = (sel) => {
    if (sel === 'link[rel="apple-touch-icon"]' || sel === 'link[rel="icon"]') return new ElClass("link");
    if (sel === "#app") return byId.app;
    return null;
  };
  doc.querySelectorAll = (sel) => (focusables && sel === "input, textarea, select" ? [select] : []);
  doc.createElement = (tag) => new ElClass(tag);
  doc.createTextNode = (t) => String(t);
  doc.addEventListener = (...a) => Emitter.prototype.addEventListener.call(doc, ...a);

  for (const [tag, id] of [["main", "app"], ["h1", "title"], ["button", "back"], ["span", "conn"], ["a", "profile-icon"],
    ["button", "menu-btn"], ["nav", "nav-drawer"], ["div", "drawer-scrim"], ["div", "drawer-chats"],
    ["span", "drawer-profile-icon"], ["div", "fab-host"], ["a", "fab"], ["header", "bar"], ["div", "guest-banner"],
    ["div", "toast"]]) make(tag, id);
  return { byId, make, doc, feature: select };
}
