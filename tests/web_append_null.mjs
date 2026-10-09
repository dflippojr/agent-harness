// UI harness: Profile, session Changes with no review card, and job details
// must not render a text node "null" or stringify run links as a URL list (#183).
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";
import { El as BaseEl, Emitter, Node, createDocument, fakeEventSource, storage } from "./web_stub_dom.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

class El extends BaseEl {
  toString() {
    if (this.tagName === "A" && this.href) return this.href;
    return `[object HTML${this.tagName}Element]`;
  }
  // Mirror browsers: do not skip null and do not flatten arrays.
  append(...nodes) {
    for (const n of nodes) {
      if (n instanceof BaseEl) {
        n.parentNode = this;
        this.childNodes.push(n);
        if (this.tagName === "SELECT" && n.tagName === "OPTION") this.options.push(n);
      } else {
        this.childNodes.push(String(n));
      }
    }
    if (this.tagName === "SELECT" && !this.value) {
      const opt = this.childNodes.find((c) => c instanceof BaseEl && c.tagName === "OPTION");
      if (opt) this.value = opt.value || opt.attributes.value || "";
    }
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      for (const c of n.childNodes || []) {
        if (!(c instanceof BaseEl)) continue;
        if (sel === "a.card") {
          if (c.tagName === "A" && String(c.className).split(/\s+/).includes("card")) out.push(c);
        } else if (sel === "button") {
          if (c.tagName === "BUTTON") out.push(c);
        }
        walk(c);
      }
    };
    walk(this);
    return out;
  }
}

const { byId, make, doc } = createDocument({ ElClass: El });

const loc = {
  href: "http://localhost/#/profile",
  origin: "http://localhost",
  _hash: "#/profile",
  get hash() { return this._hash; },
  set hash(v) {
    const next = !v || v === "#" ? "#/" : String(v);
    if (next === this._hash) return;
    this._hash = next;
    this.href = `http://localhost/${next}`;
  },
  protocol: "http:",
  pathname: "/",
  replace(url) {
    const next = String(url).startsWith("#") ? String(url) : `#${url}`;
    if (next === this._hash) return;
    this._hash = next;
    this.href = `http://localhost/${next}`;
    this.onHashReplace?.();
  },
};
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body,
  text: async () => JSON.stringify(body),
});

const sessionDetail = {
  id: "sess1", title: "Demo session", status: "done", project: "scratch", target: "tower",
  backend: "local", model: "local", totals: {}, created_at: 1, updated_at: 2, workspace: "/tmp",
};
const jobDetail = {
  id: "job1", name: "Morning check", prompt: "Look around", cron: "0 8 * * *",
  backend: "local", model: "", notify: "low", enabled: true, project: "scratch",
  recent: [], next_run_at: Date.now() / 1000 + 3600,
};

const fakeFetch = async (url) => {
  const href = String(url);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) {
    return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  }
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/sessions/sess1") return jsonResp(sessionDetail);
  if (path === "/sessions/sess1/changes") return jsonResp({ removed: false, repos: [] });
  if (path === "/sessions" || path.startsWith("/sessions?")) return jsonResp([]);
  if (path === "/queue") return jsonResp([]);
  if (path === "/gpu") return jsonResp({ manual: false, state: "clear" });
  if (path === "/projects") return jsonResp([{ name: "scratch", target: "tower" }]);
  if (path === "/jobs") return jsonResp([]);
  if (path === "/jobs/job1") return jsonResp(jobDetail);
  if (path === "/models") return jsonResp([]);
  if (path.startsWith("/chats")) return jsonResp({ backends: [{ name: "local", available: true, models: ["local"] }] });
  if (path.startsWith("/backends")) return jsonResp([{ name: "local", available: true }]);
  if (path === "/jobs/preview" || path.startsWith("/jobs/preview")) {
    return jsonResp({ ok: true, next: [Date.now() / 1000 + 3600] });
  }
  return jsonResp({});
};

const FakeEventSource = fakeEventSource();

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
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
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
  matchMedia: win.matchMedia,
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

const textNodes = (root) => {
  const out = [];
  const walk = (n) => {
    for (const c of n.childNodes || []) {
      if (typeof c === "string") out.push(c);
      else if (c instanceof El) walk(c);
    }
  };
  walk(root);
  return out;
};

const assertClean = (label) => {
  const nodes = textNodes(byId.app);
  if (nodes.some((t) => t === "null")) {
    throw new Error(`${label} rendered a text node "null": ${JSON.stringify(nodes.filter((t) => t === "null" || t.includes("null")).slice(0, 8))}`);
  }
  const joined = nodes.find((t) => /#\/s\/[^,\s]+,#\/s\//.test(t) || /https?:\/\/[^,\s]+,https?:\/\//.test(t));
  if (joined) throw new Error(`${label} rendered joined URL text: ${joined}`);
};

await waitFor(() => /This phone/.test(byId.app.textContent), "profile settings");
assertClean("profile");
if (!/Bundled server · /.test(byId.app.textContent)) {
  throw new Error("profile missing identity card");
}

await go("#/s/sess1/changes");
await waitFor(() => /No git repositories in this workspace yet/.test(byId.app.textContent), "empty changes");
assertClean("session changes");
if (/\bnull\b/.test(byId.app.textContent) && textNodes(byId.app).includes("null")) {
  throw new Error("changes view still has a null text node");
}

await go("#/jobs/job1");
await waitFor(() => /Recent runs/.test(byId.app.textContent) && /No runs yet/.test(byId.app.textContent), "job with no runs");
assertClean("job details empty");

jobDetail.recent = [
  { id: "run1", status: "done", created_at: 1, answer: "First run ok", job_status: "ok" },
  { id: "run2", status: "done", created_at: 2, answer: "Second run ok", job_status: "ok" },
];
await go("#/profile");
await waitFor(() => /This phone/.test(byId.app.textContent), "back to profile");
await go("#/jobs/job1");
await waitFor(() => /First run ok/.test(byId.app.textContent) && /Second run ok/.test(byId.app.textContent), "job with runs");
assertClean("job details with runs");
const cards = byId.app.querySelectorAll("a.card");
if (cards.length !== 2) throw new Error(`expected 2 run cards, got ${cards.length} (${byId.app.textContent.slice(0, 200)})`);
if (cards[0].href !== "#/s/run1" || cards[1].href !== "#/s/run2") {
  throw new Error(`run card hrefs were ${cards.map((c) => c.href).join(" | ")}`);
}

console.log("ok");
