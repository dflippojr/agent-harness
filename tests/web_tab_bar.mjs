// UI harness: the bottom tab bar and Settings gear on each route (#506; it replaced the drawer and the #187 profile icon).
// Boots the real app and checks which routes show the bar, which tab is current, and the role rules.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";
import { El as BaseEl, Emitter, Node, createDocument, fakeEventSource, storage } from "./web_stub_dom.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const cssSrc = readFileSync(join(root, "harness/web/style.css"), "utf8");
const indexHtml = readFileSync(join(root, "harness/web/index.html"), "utf8");

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// Static shell: no drawer, one name for scheduled work, and a bar that respects the home indicator.
assert(!/nav-drawer|menu-btn|feature-nav/.test(indexHtml), "the drawer and its menu button are retired");
assert(!/>Tasks</.test(indexHtml), "scheduled work is called Jobs everywhere");
for (const tab of ["Chat", "Agents", "Jobs", "Images", "Profile"]) {
  assert(new RegExp(`<span>${tab}</span>`).test(indexHtml), `tab bar is missing ${tab}`);
}
assert(/id="settings-btn"[^>]*aria-label="Settings"/.test(indexHtml), "the header has a labelled Settings gear");
assert(/id="agents-needs-you"[^>]*role="status"/.test(indexHtml), "the live count exposes its spoken status label");
assert(/#tab-bar \{[^}]*env\(safe-area-inset-bottom/.test(cssSrc), "the tab bar pads for the home indicator in standalone mode");
assert(/--tabbar-h: (4[4-9]|[5-9]\d)px/.test(cssSrc), "tabs are at least 44 px tall");
assert(/@media \(min-width: 768px\)[\s\S]*?#tab-bar \{[^}]*width: var\(--rail-w\)/.test(cssSrc), "the desktop rail starts at 768 px");
assert(/@media \(max-width: 420px\)/.test(cssSrc) && /#bar \{ gap: 6px; \}/.test(cssSrc),
  "phone-width bar gap stays tight so the gear does not crowd the title");

class El extends BaseEl {
  constructor(tag, attrs) {
    super(tag, attrs);
    this.offsetWidth = 44;
  }
}

const { byId, make, doc } = createDocument({ ElClass: El });
const tabLinks = byId["tab-bar"].querySelectorAll("a[data-tab]");
const tab = (name) => tabLinks.find((a) => a.dataset.tab === name);

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
let sessionPayload = [];
let holdSessions = null;
let sessionsOffline = false;
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
  if (path === "/sessions" || path.startsWith("/sessions?")) return holdSessions ? await holdSessions : sessionsOffline ? jsonResp({error:"offline"}, 503) : jsonResp(sessionPayload);
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

const sources = [];
const FakeEventSource = fakeEventSource(sources);

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

// `current` is the tab marked aria-current, or null when the bar is hidden.
const assertNav = (route, current, { settings = current !== null && current !== "profile" } = {}) => {
  const bar = byId["tab-bar"];
  if (bar.hidden !== (current === null)) throw new Error(`#tab-bar hidden=${bar.hidden} on ${route}, expected ${current === null}`);
  if (doc.body.classList.contains("has-tabs") === bar.hidden) throw new Error(`body.has-tabs out of step with the bar on ${route}`);
  const on = tabLinks.filter((a) => a.attributes["aria-current"] === "page").map((a) => a.dataset.tab);
  if (current && on.join() !== current) throw new Error(`current tab on ${route} is ${on}, expected ${current}`);
  if (byId["settings-btn"].hidden === settings) throw new Error(`Settings gear hidden=${byId["settings-btn"].hidden} on ${route}`);
};

await go("#/chat");
await waitFor(() => /Chat/.test(byId.title.textContent), "chat title");
assertNav("#/chat", "chat");
assert(byId.back.hidden, "chat is top-level; no Back");
assert(byId.bar.classList.contains("top"), "a section's own screen gets the large title");

await go("#/agents");
await waitFor(() => /Agents/.test(byId.title.textContent), "agents title");
assertNav("#/agents", "agents");

await go("#/jobs");
await waitFor(() => /Jobs/.test(byId.title.textContent), "jobs title");
assertNav("#/jobs", "jobs");

await go("#/tasks");
await waitFor(() => loc.hash === "#/jobs", "old Tasks link redirects to Jobs");

await go("#/images");
await waitFor(() => /Images/.test(byId.title.textContent), "images title");
assertNav("#/images", "images");

await go("#/profile");
await waitFor(() => /Profile/.test(byId.title.textContent), "profile title");
assertNav("#/profile", "profile", { settings: true });
assert(byId.back.hidden, "Profile is a tab; no Back");

await go("#/settings");
await waitFor(() => /Settings/.test(byId.title.textContent), "settings title");
assertNav("#/settings", "profile", { settings: false });
assert(!byId.back.hidden, "Settings opens from the gear and has Back");

await go("#/profile/appearance");
await waitFor(() => /Appearance/.test(byId.title.textContent), "appearance title");
assertNav("#/profile/appearance", "profile", { settings: false });

await go("#/actions/resources");
await waitFor(() => /Actions/.test(byId.title.textContent), "actions");
assertNav("#/actions/resources", "profile", { settings: false });
assert(!byId.back.hidden, "Actions sit under Settings and have Back");

await go("#/new");
await waitFor(() => /New task/.test(byId.title.textContent), "new task");
assertNav("#/new", null);
assert(!byId.back.hidden, "nested New task shows Back");

await go("#/jobs/job1");
await waitFor(() => /Job/.test(byId.title.textContent), "job detail");
assertNav("#/jobs/job1", null);

await go("#/images/img1");
await waitFor(() => /Image/.test(byId.title.textContent), "image detail");
assertNav("#/images/img1", null);

await go("#/s/sess1");
await waitFor(() => /Demo session/.test(byId.title.textContent), "session");
assertNav("#/s/sess1", null);
assert(!byId.back.hidden, "a session has Back and its own composer instead of the tab bar");

assert(!tab("chat").hidden && !tab("jobs").hidden && !tab("images").hidden, "the owner sees every tab");

meRole = "member";
await go("#/agents");
await waitFor(() => /Agents/.test(byId.title.textContent), "member agents");
assertNav("#/agents (member)", "agents");
assert(tab("chat").hidden && tab("jobs").hidden && tab("images").hidden, "members get no Chat, Jobs or Images tabs");
assert(!tab("profile").hidden, "members keep Profile");

meRole = "guest";
await go("#/jobs");
await waitFor(() => /Jobs/.test(byId.title.textContent), "guest jobs");
assertNav("#/jobs (guest)", "jobs");
assert(tab("chat").hidden && !tab("jobs").hidden, "guests look around Jobs but cannot chat");

// Desktop keeps the same sections on detail routes, including the approval deep link.
meRole = "owner";
const count = byId["agents-needs-you"];
const now = Date.now() / 1000;
sessionPayload = [
  { id: "a", status: "waiting_approval", updated_at: now },
  { id: "b", status: "running", pending_approvals: [{ id: "approval" }], updated_at: now },
  { id: "c", status: "failed", updated_at: now - 60 },
  { id: "d", status: "failed", updated_at: now - 86401 },
  { id: "e", status: "done", updated_at: now },
];
await go("#/s/sess1");
win.innerWidth = 767;
win.dispatchEvent({ type: "resize" });
assertNav("767 px session", null);
for (const width of [768, 959, 1024, 1280, 1440]) {
  win.innerWidth = width;
  win.dispatchEvent({ type: "resize" });
  assertNav(width + " px session", "agents", { settings: false });
}
await waitFor(() => !count.hidden && count.textContent === "3", "Needs you includes approvals and fresh failures");
assert(count.attributes["aria-label"] === "3 agents need you", "count has a spoken label");
for (const [route, section] of [["#/new", "agents"], ["#/jobs/job1", "jobs"], ["#/jobs/new", "jobs"],
  ["#/images/img1", "images"], ["#/s/sess1/approval/a", "agents"], ["#/profile/appearance", "profile"]]) {
  await go(route);
  assertNav(route + " desktop", section, { settings: false });
}
const daemon = sources[0];
sessionPayload = [{ id: "a", status: "waiting_approval", updated_at: now }];
daemon.emit("approval_decided", {});
await waitFor(() => count.textContent === "1", "badge refresh on daemon event while off the Agents page");
assert(count.attributes["aria-label"] === "1 agent needs you", "singular spoken label");
sessionPayload = [];
daemon.emit("run_finished", {});
await waitFor(() => count.hidden, "zero count hidden");
sessionPayload = [{ id: "a", status: "waiting_approval", updated_at: now }];
daemon.emit("approval_requested", {});
await waitFor(() => !count.hidden, "count restored before failed refresh");
sessionsOffline = true;
daemon.emit("status", {});
await sleep(350);
assert(count.hidden, "an unavailable count stays hidden");
sessionsOffline = false;
sessionPayload = [{ id: "a", status: "waiting_approval", updated_at: now }];
doc.dispatchEvent({ type: "visibilitychange" });
await waitFor(() => !count.hidden, "foreground refresh recovers the count");

// A response from a previous desktop/identity scope cannot repaint a phone or sign-in screen.
let releaseSessions;
holdSessions = new Promise((resolve) => { releaseSessions = resolve; });
doc.dispatchEvent({ type: "visibilitychange" });
await sleep(20);
win.innerWidth = 390;
win.dispatchEvent({ type: "resize" });
releaseSessions(jsonResp(sessionPayload));
holdSessions = null;
await sleep(20);
assert(count.hidden, "late desktop response is discarded after resize");
await go("#/s/sess1");
assertNav("session after resizing to phone", null);
meRole = "member";
win.innerWidth = 1024;
await go("#/s/sess1");
assertNav("member desktop session", "agents", { settings: false });
assert(tab("chat").hidden && tab("jobs").hidden && tab("images").hidden, "desktop preserves member restrictions");
meRole = "signin";
await go("#/agents");
assertNav("desktop sign-in", null);
win.innerWidth = 1440;
win.dispatchEvent({ type: "resize" });
assertNav("resized sign-in", null);
assert(count.hidden, "sign-in stops and clears the count");
console.log("ok");
