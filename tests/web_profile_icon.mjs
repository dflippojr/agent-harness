// UI harness: top-bar #profile-icon visibility by feature / nested page (#187).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext } from "node:vm";
import { profileIconHidden } from "../harness/web/lib/layout.mjs";
import { runApp } from "./web_app_loader.mjs";
import { El as BaseEl, Emitter, Node, createDocument, fakeEventSource, storage } from "./web_stub_dom.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const cssSrc = readFileSync(join(root, "harness/web/style.css"), "utf8");

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

const visibility = [
  ["chat", true, false, false],
  ["agents", true, false, false],
  ["jobs", true, false, false],
  ["images", true, false, false],
  ["actions", true, false, false],
  ["profile", false, true, true],
  ["agents nested", false, false, true],
  ["jobs nested", false, true, true],
  ["images nested", false, true, true],
  ["new task", false, true, true],
];
for (const [label, topLevel, page, hidden] of visibility) {
  const got = profileIconHidden(topLevel, page);
  assert(got === hidden, `${label}: expected hidden=${hidden}, got ${got}`);
}

assert(/#profile-icon/.test(cssSrc) && /flex:\s*0 0 auto/.test(cssSrc),
  "phone-width top bar must keep #profile-icon from flex-growing into the title");
assert(/@media \(max-width: 420px\)/.test(cssSrc) && /#bar \{ gap: 6px; \}/.test(cssSrc),
  "phone-width bar gap must stay tight so the icon does not crowd the title");

class El extends BaseEl {
  constructor(tag, attrs) {
    super(tag, attrs);
    this.offsetWidth = 44;
  }
}

const { byId, make, doc, feature } = createDocument({ ElClass: El, features: ["chat", "agents", "jobs", "images"], feature: "chat", focusables: false });
const drawer = byId["nav-drawer"];
drawer._drawerRecent = new El("div", { class: "drawer-recent" });
drawer.hidden = true;
const profileIcon = byId["profile-icon"];
profileIcon.href = "#/profile";
profileIcon.hidden = true;

const loc = {
  href: "http://localhost/#/",
  origin: "http://localhost",
  _hash: "#/",
  get hash() { return this._hash; },
  set hash(v) { this._hash = !v || v === "#" ? "#/" : String(v); this.href = `http://localhost/${this._hash}`; },
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

let meRole = "owner";
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
const sessionDetail = {
  id: "sess1", title: "Demo session", status: "done", project: "scratch", target: "tower",
  backend: "local", model: "local", totals: {}, created_at: 1, updated_at: 2, workspace: "/tmp",
};
const jobDetail = {
  id: "job1", name: "Morning check", prompt: "Look around", cron: "0 8 * * *",
  backend: "local", model: "", notify: "low", enabled: true, project: "scratch", recent: [],
};
const imageDetail = {
  id: "img1", status: "done", prompt: "a cat", model: "fast", width: 1024, height: 1024,
  seed: 1, source: "web", created_at: 1, finished_at: 2, scale: 1, service: { edit: {} },
};

const fakeFetch = async (url) => {
  const href = String(url);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) {
    return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  }
  if (path === "/me") return jsonResp({ role: meRole, name: "Owner", login: "owner", public_url: "http://localhost" });
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
  if (path.startsWith("/chats")) return jsonResp([]);
  if (path.startsWith("/backends")) return jsonResp([{ name: "local", available: true }]);
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
  innerWidth: 390,
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

await runApp(sandbox);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = async (pred, label, ms = 2000) => {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (pred()) return;
    await sleep(20);
  }
  throw new Error(`timeout waiting for ${label}`);
};

const go = async (hash) => {
  loc.hash = hash;
  loc.href = `http://localhost/${hash}`;
  win.dispatchEvent({ type: "hashchange" });
  await sleep(40);
};

const assertIcon = (route, visible) => {
  if (byId["profile-icon"].hidden === visible) {
    throw new Error(`#profile-icon hidden=${byId["profile-icon"].hidden} on ${route}, expected visible=${visible}`);
  }
  if (visible && byId["profile-icon"].href !== "#/profile") {
    throw new Error(`#profile-icon href is ${byId["profile-icon"].href} on ${route}`);
  }
};

await go("#/chat");
await waitFor(() => /Chat/.test(byId.title.textContent), "chat title");
assertIcon("#/chat", true);
assert(byId.back.hidden, "chat is top-level; Back must stay hidden so the icon does not sit next to it");

await go("#/agents");
await waitFor(() => /Agents/.test(byId.title.textContent), "agents title");
assertIcon("#/agents", true);

await go("#/jobs");
await waitFor(() => /Jobs/.test(byId.title.textContent), "jobs title");
assertIcon("#/jobs", true);

await go("#/images");
await waitFor(() => /Images/.test(byId.title.textContent), "images title");
assertIcon("#/images", true);

await go("#/actions");
await waitFor(() => loc.hash === "#/actions/gpu" || /Actions/.test(byId.title.textContent), "actions");
assertIcon("#/actions", true);

await go("#/profile");
await waitFor(() => /Profile/.test(byId.title.textContent), "profile title");
assertIcon("#/profile", false);

await go("#/new");
await waitFor(() => /New task/.test(byId.title.textContent), "new task");
assertIcon("#/new", false);
assert(!byId.back.hidden, "nested New task shows Back instead of duplicating the profile icon");

await go("#/jobs/job1");
await waitFor(() => /Job/.test(byId.title.textContent), "job detail");
assertIcon("#/jobs/job1", false);
assert(!byId.back.hidden, "job detail Back is visible; profile icon stays off");

await go("#/images/img1");
await waitFor(() => /Image/.test(byId.title.textContent), "image detail");
assertIcon("#/images/img1", false);

await go("#/s/sess1");
await waitFor(() => /Demo session/.test(byId.title.textContent), "session");
assertIcon("#/s/sess1", false);
assert(!byId.back.hidden, "session Back plus inline rename must not share the bar with the profile icon at phone width");

meRole = "guest";
await go("#/agents");
await waitFor(() => /Agents/.test(byId.title.textContent), "guest agents");
assertIcon("#/agents (guest)", true);

console.log("ok");
