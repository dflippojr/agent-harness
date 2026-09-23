// UI harness: load app.js and prove the header title (#title) shows the current page's
// name on every route, not only Chat, and that the connection dot (#conn) stays present. (#154)
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
    this.parentNode = null;
  }
  get isConnected() { return !!this.parentNode; }
  replaceWith(...nodes) {
    if (!this.parentNode) return;
    const parent = this.parentNode;
    const idx = parent.childNodes.indexOf(this);
    if (idx === -1) return;
    for (const n of nodes) if (n instanceof El) n.parentNode = parent;
    parent.childNodes.splice(idx, 1, ...nodes);
    this.parentNode = null;
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
  select() {}
  blur() { this.dispatchEvent({ type: "blur" }); }
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
  // Browsers fire hashchange when location.replace changes the fragment. The app's
  // legacy redirects depend on that second route() being the only one that paints.
  replace(url) {
    const next = String(url).startsWith("#") ? String(url) : `#${url}`;
    if (next === this.hash) return;
    this.hash = next;
    this.href = `http://localhost/${next}`;
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

let pendingRenameGate = null;
let meRole = "owner";
let gpuPayload = { manual: false, state: "clear" };
const fetched = [];
const fakeFetch = async (url, opts = {}) => {
  const href = String(url);
  fetched.push(href);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) {
    return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  }
  if (path === "/me") {
    return jsonResp({ role: meRole, name: "Owner", login: "owner", public_url: "http://localhost" });
  }
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/sessions" || path.startsWith("/sessions?")) return jsonResp([]);
  if (path === "/sessions/sess1" && (opts.method === "PATCH" || opts.method === "PUT")) {
    const body = JSON.parse(opts.body || "{}");
    if (pendingRenameGate) await pendingRenameGate;
    sessionDetail.title = body.title;
    return jsonResp(sessionDetail);
  }
  if (path === "/sessions/sess1") return jsonResp(sessionDetail);
  if (path === "/queue") return jsonResp([]);
  if (path === "/gpu") return jsonResp(gpuPayload);
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

loc.onHashReplace = () => win.dispatchEvent({ type: "hashchange" });
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
const waitFor = async (pred, label, ms = 2000) => {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (pred()) return;
    await sleep(20);
  }
  throw new Error(`timeout waiting for ${label}: ${byId.app.textContent.slice(0, 200)}`);
};

const go = async (hash) => {
  loc.hash = hash;
  loc.href = `http://localhost/${hash}`;
  win.dispatchEvent({ type: "hashchange" });
  await sleep(40);
};

const assertTitle = (route, expected) => {
  const text = byId.title.textContent;
  if (byId.title.hidden) throw new Error(`title hidden on ${route} (expected "${expected}")`);
  if (!text || !text.includes(expected)) {
    throw new Error(`expected title on ${route} to include "${expected}", got "${text}"`);
  }
  // The connection dot must remain in the header regardless of the title.
  if (!byId.conn) throw new Error(`connection dot missing on ${route}`);
};

await waitFor(() => !byId.title.hidden, "initial title");

await go("#/agents");
await waitFor(() => /Agents/.test(byId.title.textContent), "agents list title");
assertTitle("#/agents", "Agents");

await go("#/images");
await waitFor(() => /Images/.test(byId.title.textContent), "images list title");
assertTitle("#/images", "Images");

await go("#/jobs");
await waitFor(() => /Jobs/.test(byId.title.textContent), "jobs list title");
assertTitle("#/jobs", "Jobs");

await go("#/profile");
await waitFor(() => /Profile/.test(byId.title.textContent), "profile title");
assertTitle("#/profile", "Profile");
if (/Claude Remote Control/.test(byId.app.textContent)) {
  throw new Error("profile still lists Actions");
}

await go("#/profile/account");
await waitFor(() => /Account/.test(byId.title.textContent), "profile account title");
assertTitle("#/profile/account", "Account");

await go("#/profile/disk");
await waitFor(() => loc.hash === "#/actions/disk" && /Actions/.test(byId.title.textContent), "disk bookmark redirects to actions");
assertTitle("#/actions/disk", "Actions");

await go("#/jobs/job1");
await waitFor(() => byId.title.textContent && !byId.title.hidden, "job detail title");
assertTitle("#/jobs/job1", "Job");

await go("#/images/img1");
await waitFor(() => byId.title.textContent && !byId.title.hidden, "image detail title");
assertTitle("#/images/img1", "Image");

await go("#/s/sess1");
await waitFor(() => /Demo session/.test(byId.title.textContent), "session transcript title");
assertTitle("#/s/sess1", "Demo session");

// Renaming the session via the inline title editor must update the topbar title too (#178).
const findByClass = (root, cls) => {
  if (!root) return null;
  if (root instanceof El && (root.className || "").split(/\s+/).includes(cls)) return root;
  for (const c of root.childNodes || []) {
    const found = findByClass(c, cls);
    if (found) return found;
  }
  return null;
};
const titleBtn = findByClass(byId.app, "session-title");
if (!titleBtn) throw new Error("session title button not found");
titleBtn.click();
const titleInput = findByClass(byId.app, "session-title-edit");
if (!titleInput) throw new Error("session title edit input not found");
titleInput.value = "Renamed session";
titleInput.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} });
await waitFor(() => /Renamed session/.test(byId.title.textContent), "topbar title after rename");
assertTitle("#/s/sess1", "Renamed session");

await go("#/new");
await waitFor(() => /New task/.test(byId.title.textContent), "new task title");
assertTitle("#/new", "New task");

// A rename PATCH that resolves after the user has already navigated away must not
// clobber the topbar title of the page the user is now on (#178).
sessionDetail.title = "Demo session";
await go("#/s/sess1");
await waitFor(() => /Demo session/.test(byId.title.textContent), "session transcript title (race setup)");

let releaseRename;
pendingRenameGate = new Promise((r) => { releaseRename = r; });

const titleBtn2 = findByClass(byId.app, "session-title");
if (!titleBtn2) throw new Error("session title button not found (race test)");
titleBtn2.click();
const titleInput2 = findByClass(byId.app, "session-title-edit");
if (!titleInput2) throw new Error("session title edit input not found (race test)");
titleInput2.value = "Renamed while leaving";
titleInput2.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} });

// Navigate away before the PATCH resolves.
await go("#/agents");
await waitFor(() => /Agents/.test(byId.title.textContent), "agents title before rename resolves");

releaseRename();
pendingRenameGate = null;
await sleep(60);

if (!/Agents/.test(byId.title.textContent)) {
  throw new Error(`stale session rename clobbered topbar title after navigating away: "${byId.title.textContent}"`);
}

const tabLabels = (root) => {
  const labels = [];
  const walk = (node) => {
    if (!node || typeof node !== "object") return;
    if (node.attributes && node.attributes.role === "tab") labels.push(node.textContent);
    for (const child of node.childNodes || []) walk(child);
  };
  walk(root);
  return labels;
};

const findHref = (root, href) => {
  let found = null;
  const walk = (node) => {
    if (found || !node || typeof node !== "object") return;
    if (node.tagName === "A" && node.attributes && node.attributes.href === href) found = node;
    for (const child of node.childNodes || []) walk(child);
  };
  walk(root);
  return found;
};
gpuPayload = {
  manual: true, state: "paused", manual_remaining_seconds: null,
  enabled: true, signals: [], reasons: [],
};
await go("#/agents");
await waitFor(() => /Local models held/.test(byId.app.textContent), "gpu hold notice");
if (!findHref(byId.app, "#/actions/gpu")) {
  const stale = findHref(byId.app, "#/profile");
  throw new Error(`gpu hold notice links to ${stale ? stale.attributes.href : "nothing"}`);
}
gpuPayload = { manual: false, state: "clear" };

await go("#/actions");
await waitFor(() => loc.hash === "#/actions/gpu" && /GPU guard disabled|Checking/.test(byId.app.textContent), "default gpu tab");
assertTitle("#/actions", "Actions");
const gpuTabs = tabLabels(byId.app);
if (gpuTabs.join("|") !== "GPU|Accounts|Claude Remote Control|Disk") {
  throw new Error(`tab order ${gpuTabs.join("|")}`);
}
const selected = (root) => {
  const labels = [];
  const walk = (node) => {
    if (!node || typeof node !== "object") return;
    if (node.attributes && node.attributes.role === "tab" && String(node.className).split(/\s+/).includes("on")) {
      labels.push(node.textContent);
    }
    for (const child of node.childNodes || []) walk(child);
  };
  walk(root);
  return labels;
};
if (selected(byId.app).join("|") !== "GPU") throw new Error(`expected GPU selected, got ${selected(byId.app)}`);

const clickTab = (label) => {
  let found = null;
  const walk = (node) => {
    if (found || !node || typeof node !== "object") return;
    if (node.attributes && node.attributes.role === "tab" && node.textContent === label) found = node;
    for (const child of node.childNodes || []) walk(child);
  };
  walk(byId.app);
  if (!found) throw new Error(`missing tab ${label}`);
  found.click();
};
clickTab("Accounts");
if (loc.hash !== "#/actions/accounts") throw new Error(`accounts tab hash ${loc.hash}`);
win.dispatchEvent({ type: "hashchange" });
await waitFor(() => /No household members yet|New member/.test(byId.app.textContent), "accounts tab");
clickTab("Disk");
win.dispatchEvent({ type: "hashchange" });
await waitFor(() => loc.hash === "#/actions/disk" && /Tower|Measuring/.test(byId.app.textContent), "disk tab");

await go("#/profile/accounts");
await waitFor(() => loc.hash === "#/actions/accounts" && /New member/.test(byId.app.textContent), "accounts bookmark redirects");
await sleep(50);
const accountTabs = tabLabels(byId.app);
if (accountTabs.join("|") !== "GPU|Accounts|Claude Remote Control|Disk") {
  throw new Error(`legacy #/profile/accounts duplicated tabs: ${accountTabs.join("|")}`);
}
const accountPanels = (byId.app.textContent.match(/New member/g) || []).length;
if (accountPanels !== 1) {
  throw new Error(`legacy #/profile/accounts rendered ${accountPanels} Accounts panels`);
}
await go("#/profile/remote-control");
await waitFor(() => loc.hash === "#/actions/remote-control", "remote-control bookmark redirects");
await go("#/settings/disk");
await waitFor(() => loc.hash === "#/actions/disk", "settings disk alias redirects");

meRole = "member";
await go("#/actions/gpu");
await waitFor(() => loc.hash === "#/agents", "member blocked from actions");
await go("#/profile/disk");
await waitFor(() => loc.hash === "#/profile", "member disk bookmark stays off actions");
meRole = "guest";
await go("#/actions");
await waitFor(() => loc.hash === "#/profile", "guest blocked from actions");
await go("#/profile/remote-control");
await waitFor(() => loc.hash === "#/profile", "guest remote-control bookmark stays off actions");

console.log("ok");
