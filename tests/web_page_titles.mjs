// UI harness: load app.js and prove the header title (#title) shows the current page's
// name on every route, not only Chat, and that the connection dot (#conn) stays present. (#154)
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";
import { El as BaseEl, Emitter, Node, createDocument, fakeEventSource, storage } from "./web_stub_dom.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

class El extends BaseEl {
  blur() { this.dispatchEvent({ type: "blur" }); }
  closest(sel) {
    let n = this;
    while (n instanceof BaseEl) {
      if (sel === "[hidden]" && n.hidden) return n;
      if (sel === "a[href]" && n.tagName === "A" && n.href) return n;
      n = n.parentNode;
    }
    return null;
  }
}

const { byId, make, doc } = createDocument({ ElClass: El });
// Settings links to Actions now that the drawer is gone (#506).
const actionsHref = "#/actions/resources";
if (!readFileSync(join(root, "harness/web/pages/profile.mjs"), "utf8").includes("`#/actions/${id}`")) {
  throw new Error("missing the Actions links in the Settings menu");
}
const profileTab = byId["tab-bar"].querySelectorAll("a[data-tab]").find((a) => a.dataset.tab === "profile");

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
  // Browsers fire hashchange when location.replace changes the fragment. The app's
  // legacy redirects depend on that second route() being the only one that paints.
  replace(url) {
    const next = String(url).startsWith("#") ? String(url) : `#${url}`;
    if (next === this._hash) return;
    this._hash = next;
    this.href = `http://localhost/${next}`;
    historyStack[historyStack.length - 1] = next;
    this.onHashReplace?.();
  },
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
  history: {
    replaceState() {},
    back() {
      if (historyStack.length < 2) return;
      historyStack.pop();
      const prev = historyStack[historyStack.length - 1];
      loc._hash = prev;
      loc.href = `http://localhost/${prev}`;
      win.dispatchEvent({ type: "hashchange" });
    },
  },
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

await runApp(sandbox);

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

// List actions belong to the current header, share the phone FAB's destination, and never leak onto detail pages.
const assertNewAction = (href = null, label = "") => {
  const actions = byId.bar.childNodes.filter((el) => el instanceof BaseEl && el.classList.contains("list-new"));
  if (actions.length !== (href ? 1 : 0)) throw new Error(`unexpected New actions on ${loc.hash}: ${actions.length}`);
  if (!href) return;
  const action = actions[0];
  if (action.href !== href || action.textContent !== label || byId.fab.href !== href || byId.fab.textContent !== label) {
    throw new Error(`header/FAB action mismatch on ${loc.hash}`);
  }
  if (byId.bar.childNodes.indexOf(action) !== byId.bar.childNodes.indexOf(byId.title) + 1) {
    throw new Error("New action must follow the list title in reading order");
  }
};

await waitFor(() => !byId.title.hidden, "initial title");

await go("#/agents");
await waitFor(() => /Agents/.test(byId.title.textContent), "agents list title");
assertTitle("#/agents", "Agents");
assertNewAction("#/new", "+ New task");
await go("#/agents");
assertNewAction("#/new", "+ New task");

await go("#/images");
await waitFor(() => /Images/.test(byId.title.textContent), "images list title");
assertTitle("#/images", "Images");
assertNewAction();

await go("#/jobs");
await waitFor(() => /Jobs/.test(byId.title.textContent), "jobs list title");
assertTitle("#/jobs", "Jobs");
assertNewAction("#/jobs/new", "+ New job");

await go("#/profile");
await waitFor(() => /Profile/.test(byId.title.textContent), "profile title");
assertTitle("#/profile", "Profile");
assertNewAction();
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
assertNewAction();

await go("#/images/img1");
await waitFor(() => byId.title.textContent && !byId.title.hidden, "image detail title");
assertTitle("#/images/img1", "Image");

await go("#/s/sess1");
await waitFor(() => /Demo session/.test(byId.title.textContent), "session transcript title");
assertTitle("#/s/sess1", "Demo session");
assertNewAction();

// Renaming the session from the ⋯ menu (#514) edits the topbar title in place and must update it (#178).
const findByClass = (root, cls) => {
  if (!root) return null;
  if (root instanceof El && (root.className || "").split(/\s+/).includes(cls)) return root;
  for (const c of root.childNodes || []) {
    const found = findByClass(c, cls);
    if (found) return found;
  }
  return null;
};
if (findByClass(byId.app, "session-title")) throw new Error("the session title must not be painted a second time (#514)");
const pickRename = () => {
  const menuBtn = findByClass(byId.bar, "session-menu-btn");
  if (!menuBtn) throw new Error("session menu button not found in the bar");
  menuBtn.click();
  const rename = (findByClass(byId.bar, "session-menu")?.childNodes || []).find((b) => b.textContent === "Rename");
  if (!rename) throw new Error("Rename entry not found in the session menu");
  rename.click();
};
pickRename();
const titleInput = findByClass(byId.bar, "session-title-edit");
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

pickRename();
const titleInput2 = findByClass(byId.bar, "session-title-edit");
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
if (!findHref(byId.app, "#/actions/resources")) {
  const stale = findHref(byId.app, "#/profile");
  throw new Error(`gpu hold notice links to ${stale ? stale.attributes.href : "nothing"}`);
}
gpuPayload = { manual: false, state: "clear" };

await go("#/actions");
await waitFor(() => loc.hash === "#/actions/resources" && /Resource guard disabled|Checking/.test(byId.app.textContent), "default resources tab");
assertTitle("#/actions", "Actions");
assertNewAction();
if (!byId.bar.classList.contains("page")) throw new Error("Actions must use the shared page header");
const gpuTabs = tabLabels(byId.app);
if (gpuTabs.join("|") !== "Resources|Accounts|Claude Remote Control|Disk") {
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
if (selected(byId.app).join("|") !== "Resources") throw new Error(`expected Resources selected, got ${selected(byId.app)}`);

const followActionsLink = async () => {
  if (loc.hash === actionsHref) return;
  loc.hash = actionsHref;
  win.dispatchEvent({ type: "hashchange" });
  await sleep(40);
};
await followActionsLink();
await waitFor(() => loc.hash === "#/actions/resources", "the Actions link stays on Resources");
win.history.back();
await sleep(40);
if (loc.hash !== "#/agents") {
  throw new Error(`tapping Actions on Resources left extra history; back landed on ${loc.hash}`);
}
await go("#/agents");
await waitFor(() => /Agents/.test(byId.title.textContent), "agents before bare #/actions");
await go("#/actions");
await waitFor(() => loc.hash === "#/actions/resources", "bare #/actions redirects to resources");
win.history.back();
await sleep(40);
if (loc.hash !== "#/agents") {
  throw new Error(`bare #/actions left extra history; back landed on ${loc.hash}`);
}
await go("#/actions/gpu");  // old bookmark
await waitFor(() => loc.hash === "#/actions/resources" && /Resource guard disabled|Checking/.test(byId.app.textContent), "gpu bookmark redirects to resources");

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
if (profileTab.attributes["aria-current"] !== "page") {
  throw new Error(`Profile tab not current on ${loc.hash}`);
}
clickTab("Disk");
win.dispatchEvent({ type: "hashchange" });
await waitFor(() => loc.hash === "#/actions/disk" && /Tower|Measuring/.test(byId.app.textContent), "disk tab");

await go("#/profile/accounts");
await waitFor(() => loc.hash === "#/actions/accounts" && /New member/.test(byId.app.textContent), "accounts bookmark redirects");
await sleep(50);
const accountTabs = tabLabels(byId.app);
if (accountTabs.join("|") !== "Resources|Accounts|Claude Remote Control|Disk") {
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
await go("#/agents");
assertNewAction("#/new", "+ New task");
await go("#/jobs");
await waitFor(() => loc.hash === "#/agents", "member blocked from jobs");
assertNewAction("#/new", "+ New task");
await go("#/actions/gpu");
await waitFor(() => loc.hash === "#/agents", "member blocked from actions");
await go("#/profile/disk");
await waitFor(() => loc.hash === "#/profile", "member disk bookmark stays off actions");
meRole = "guest";
await go("#/agents");
assertNewAction();
if (!byId["fab-host"].hidden) throw new Error("guest Agents FAB must be hidden");
await go("#/jobs");
assertNewAction();
if (!byId["fab-host"].hidden) throw new Error("guest Jobs FAB must be hidden");
await go("#/actions");
await waitFor(() => loc.hash === "#/profile", "guest blocked from actions");
await go("#/profile/remote-control");
await waitFor(() => loc.hash === "#/profile", "guest remote-control bookmark stays off actions");

console.log("ok");
