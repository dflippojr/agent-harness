// The lib/ modules split out of app.js (#258) import under plain Node (no DOM at module top level) and behave on their own:
// each is imported directly and driven with a small stub DOM and fake client.
import assert from "node:assert/strict";

// ---- stub DOM (installed only after the imports below, to prove the modules need none at import time) ----
const imports = {};
for (const name of ["dom", "widgets", "session", "stream", "chrome", "files", "signin", "router", "drawer", "update", "boot", "warm-model", "secret"]) {
  imports[name] = await import(`../harness/web/lib/${name}.mjs`);
}
const { h, fill, append, kids } = imports.dom;

class Emitter {
  constructor() { this.listeners = {}; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  emit(type, event = {}) { for (const fn of [...(this.listeners[type] || [])]) fn(event); }
}
class Node extends Emitter {}
class El extends Node {
  constructor(tag = "div") {
    super();
    this.tag = tag; this.attrs = {}; this.children = []; this.hidden = false; this.textContent = "";
    this.className = ""; this.dataset = {}; this.style = { props: {}, setProperty(k, v) { this.props[k] = v; } };
    this.set = new Set();
    this.classList = { toggle: (c, on) => { if (on) this.set.add(c); else this.set.delete(c); }, add: (c) => this.set.add(c), remove: (c) => this.set.delete(c) };
    this.options = [];
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  get text() { return this.textContent + this.children.map((c) => (c instanceof El ? c.text : c.text ?? String(c))).join(""); }
}
const byId = {};
const doc = Object.assign(new Emitter(), {
  createElement: (tag) => new El(tag),
  createTextNode: (text) => ({ text: String(text) }),
  getElementById: (id) => (byId[id] ||= new El("div")),
  querySelector: () => null,
  querySelectorAll: () => [],
  documentElement: new El("html"),
  body: new El("body"),
  visibilityState: "visible",
  hidden: false,
});
const win = Object.assign(new Emitter(), { scrollTo() {}, innerHeight: 800, scrollY: 0 });
globalThis.document = doc;
globalThis.Node = Node;
const frames = [];
globalThis.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
globalThis.cancelAnimationFrame = () => {};
const browser = { window: win, document: doc, location: { hash: "", origin: "https://h", assign(url) { this.assigned = url; }, replace(url) { this.hash = url; } },
  history: { backed: 0, back() { this.backed++; } }, sessionStorage: new Map(), fetch: async () => ({ ok: false }) };
browser.sessionStorage.getItem = (k) => browser.sessionStorage.get(k) ?? null;
browser.sessionStorage.setItem = (k, v) => browser.sessionStorage.set(k, v);
browser.sessionStorage.removeItem = (k) => browser.sessionStorage.delete(k);
const tick = () => new Promise((r) => setTimeout(r, 0));

// ---- dom ----
{
  const el = h("div", { class: "a", onclick: () => {}, hidden: false, title: null, "data-x": true }, "t", [null, h("span"), false], 3);
  assert.equal(el.className, "a");
  assert.equal(el.attrs["data-x"], "");
  assert.equal(el.children.length, 3);
  assert.deepEqual(kids([1, null], undefined, false, 2), [1, 2]);
  assert.equal(fill(el, null, "x").children.length, 1);
  assert.equal(append(el, null).children.length, 1);
  assert.equal(append(el, "y").children.length, 2);
}

// ---- widgets ----
{
  const { badge, progressBar, reviewBadge, jobStatusBadge, TERMINAL, STATUS_LABEL, REVIEW_LABEL } = imports.widgets;
  assert.equal(badge("waiting_target").text, "waiting for Mac");
  assert.equal(badge("weird").text, "weird");
  assert.equal(progressBar(null).className, "progress indeterminate");
  assert.equal(progressBar(0.5).children[0].attrs.style, "width:50.0%");
  assert.equal(reviewBadge("discarded", "x").className, "badge cancelled");
  assert.equal(jobStatusBadge("ok").text, "OK");
  assert.equal(jobStatusBadge("bad").className, "badge waiting_approval");
  assert.ok(TERMINAL.has("done") && !TERMINAL.has("running") && STATUS_LABEL.queued && REVIEW_LABEL.merged);
}

// ---- session ----
const client = { token: "", independent: false, csrf: "", calls: [], routes: {},
  async request(path, opts) { this.calls.push([path, opts.surface]); const r = this.routes[path]; if (r instanceof Error) throw r; return r; } };
const session = imports.session.createSession({ agentHarnessWeb: client });
{
  const { apiSurface } = imports.session;
  assert.equal(apiSurface("/sessions", "GET", { role: "owner", hasToken: false }), "app");
  assert.equal(apiSurface("/sessions/abc123", "GET", { role: "owner", hasToken: false }), "app");
  assert.equal(apiSurface("/sessions/abc/cancel", "POST", { role: "owner", hasToken: true }), "app");
  assert.equal(apiSurface("/sessions/abc/approvals/z", "POST", { role: "owner", hasToken: true }), "app");
  assert.equal(apiSurface("/jobs", "GET", { role: "owner", hasToken: true }), "admin");
  assert.equal(apiSurface("/jobs", "GET", { role: "guest", hasToken: false }), "legacy");
  assert.equal(apiSurface("/jobs", "GET", { role: "guest", hasToken: true }), "admin");
  assert.equal(apiSurface("/jobs", "GET", { role: "member", hasToken: false }), "app");
  assert.ok(session.isOwner() && session.canChat() && !session.isGuest());
  session.setMe({ role: "member" });
  assert.ok(session.isMember() && !session.canChat() && session.ownerSurface() === "app");
  session.setMe({ role: "guest" });
  assert.ok(session.isGuest() && session.ownerSurface() === "legacy");
  session.setMe({ role: "signin" });
  assert.ok(session.needsSignIn());
  client.routes["/me"] = new Error("down");
  assert.deepEqual(await session.fetchMe(), { role: "guest" });
  const signIn = new Error("sign in");
  signIn.code = "sign_in_required";
  client.routes["/me"] = signIn;
  assert.deepEqual(await session.fetchMe(), { role: "signin" });
  client.routes["/me"] = { role: "owner" };
  assert.deepEqual(await session.fetchMe(), { role: "owner" });
  assert.equal(client.calls.at(-1)[1], "legacy");
  client.routes["/auth/session"] = { csrf: "tok", google: { available: true } };
  assert.equal((await session.loadWebAuth()).csrf, "tok");
  assert.equal(client.csrf, "tok");
  client.token = "t";
  assert.equal(await session.loadWebAuth(), null);
  assert.equal(client.csrf, "");
  client.token = "";
  // #368: a network failure never becomes guest. It resolves to the last cached role, or the offline marker.
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
  const cached = imports.session.createSession({ agentHarnessWeb: client, storage });
  const offline = new Error("Can't reach");
  offline.code = "offline";
  client.routes["/me"] = offline;
  assert.deepEqual(await cached.fetchMe(), { role: "offline", offline: true });
  client.routes["/me"] = { role: "member", name: "M" };
  const member = await cached.fetchMe();
  assert.equal(store.has(imports.session.LAST_ROLE_KEY), false, "fetching alone never caches: boot may discard it");
  cached.setMe(member);
  assert.equal(store.get(imports.session.LAST_ROLE_KEY), "member", "an adopted /me caches the role only");
  client.routes["/me"] = offline;
  assert.deepEqual(await cached.fetchMe(), { role: "member", offline: true });
  cached.setMe(await cached.fetchMe());
  assert.ok(cached.isOffline() && cached.isMember() && !cached.isGuest());
  assert.equal(store.get(imports.session.LAST_ROLE_KEY), "member", "adopting an offline identity keeps the cache");
  const update = Object.assign(new Error("Update required"), { status: 426, code: "client_update_required" });
  client.routes["/me"] = update;
  assert.deepEqual(await cached.fetchMe(), { role: "guest" });
  assert.equal(store.get(imports.session.LAST_ROLE_KEY), "member", "a discarded /me (protocol skew) keeps the cache");
  client.routes["/me"] = signIn;
  const signin = await cached.fetchMe();
  assert.deepEqual(signin, { role: "signin" }, "a real sign-in requirement still leads to sign-in");
  cached.setMe(signin);
  assert.equal(store.has(imports.session.LAST_ROLE_KEY), false, "and adopting it clears the cached role");
  client.routes["/me"] = Object.assign(new Error("Unauthorized"), { status: 401 });
  assert.deepEqual(await cached.fetchMe(), { role: "guest" }, "a real 401 is still guest");
  const throwing = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
  const blocked = imports.session.createSession({ agentHarnessWeb: client, storage: throwing });
  client.routes["/me"] = { role: "owner" };
  blocked.setMe(await blocked.fetchMe());
  assert.ok(blocked.isOwner());
  client.routes["/me"] = offline;
  assert.deepEqual(await blocked.fetchMe(), { role: "offline", offline: true });
  client.routes["/me"] = { role: "owner" };
  session.setBootMe({ role: "owner" });
  assert.deepEqual(await session.takeBootMe(), { role: "owner" });
  assert.equal(session.takeBootMe(), null);
  session.setBlocked(true);
  await assert.rejects(session.api("/x"), (e) => e.code === "client_update_required");
  session.setBlocked(false);
  session.setMe({ role: "owner" });
}

// ---- stream ----
{
  const { safeStreamUrl, validId, mountStream } = imports.stream;
  assert.ok(validId("abc-123_") && !validId("../x") && !validId(5) && !validId(""));
  assert.equal(safeStreamUrl("/api/v1/sessions/abc/events?after=3"), "/api/v1/sessions/abc/events?after=3");
  assert.equal(safeStreamUrl("https://h/api/v1/events", "https://h"), "https://h/api/v1/events");
  assert.equal(safeStreamUrl("https://evil/api/v1/events", "https://h"), null);
  assert.equal(safeStreamUrl("//evil/events"), null);
  assert.equal(safeStreamUrl("/api/v1/sessions/abc/other"), null);
  assert.equal(safeStreamUrl("/events?x=<"), null);
  assert.equal(safeStreamUrl("/events\\x"), null);

  const sources = [];
  class FakeEventSource extends Emitter {
    constructor(url) { super(); this.url = url; this.closed = false; sources.push(this); }
    close() { this.closed = true; }
  }
  FakeEventSource.CLOSED = 2;
  const live = [];
  let blocked = false;
  const sb = { ...browser, EventSource: FakeEventSource };
  const stream = mountStream({ agentHarnessWeb: { token: "", baseUrl: "", headers: () => ({}), url: (p) => `/api/v1${p}` },
    isBlocked: () => blocked, setConnLive: (on) => live.push(on), ownerSurface: () => "app", isGuest: () => false, browser: sb });
  const seen = [];
  const stop = stream.openStream(async () => "/api/v1/events", { ping: (d) => seen.push(d) }, { indicate: true });
  await tick();
  assert.equal(sources.length, 1);
  sources[0].onopen();
  sources[0].emit("ping", { data: '{"n":1}' });
  sources[0].emit("ping", { data: undefined });
  assert.deepEqual(seen, [{ n: 1 }]);
  assert.deepEqual(live, [true]);
  stop();
  assert.ok(sources[0].closed);
  assert.deepEqual(live, [true, false]);
  blocked = true;
  stream.openStream(async () => "/api/v1/events", {})();
  await tick();
  assert.equal(sources.length, 1, "a blocked app opens no stream");
  blocked = false;
  stream.watchDaemonConnection();
  stream.watchDaemonConnection();
  await tick();
  assert.equal(sources.length, 2, "the daemon connection is watched once");
}

// ---- chrome ----
const els = Object.fromEntries(["app", "title", "back", "conn", "feature-nav", "profile-icon", "fab-host", "fab", "menu-btn", "nav-drawer", "drawer-scrim", "drawer-chats"]
  .map((id) => [id, doc.getElementById(id)]));
const E = { $app: els.app, $title: els.title, $back: els.back, $conn: els.conn, $feature: els["feature-nav"], $profileIcon: els["profile-icon"],
  $fabHost: els["fab-host"], $fab: els.fab, $menu: els["menu-btn"], $drawer: els["nav-drawer"], $scrim: els["drawer-scrim"], $drawerChats: els["drawer-chats"] };
E.$feature.options = [{ value: "agents" }, { value: "jobs" }];
E.$feature.value = "jobs";
const chrome = imports.chrome.mountChrome({ els: E, browser, session });
{
  chrome.toast("hello", 10);
  assert.equal(doc.getElementById("toast").textContent, "hello");
  assert.equal(doc.getElementById("toast").hidden, false);
  chrome.setConnLive(true);
  assert.ok(E.$conn.set.has("live"));
  E.$back.hidden = true;
  chrome.setHeader("agents", "Title", { page: true });
  assert.equal(E.$title.textContent, "Title");
  assert.equal(E.$feature.value, "agents");
  assert.ok(doc.getElementById("bar").set.has("page"));
  chrome.showFab("#/new", "Go");
  assert.equal(E.$fab.href, "#/new");
  assert.equal(E.$fabHost.hidden, false);
  E.$fabHost.hidden = true;
  session.setMe({ role: "guest", guest_until: "2099-01-01T00:00:00Z" });
  chrome.showFab("#/new", "Go");
  assert.equal(E.$fabHost.hidden, true, "guests get no floating button");
  chrome.paintGuestChrome();
  assert.ok(doc.documentElement.set.has("guest"));
  assert.match(doc.getElementById("guest-banner").textContent, /^Demo access/);
  assert.equal(E.$feature.options[1].hidden, true);
  session.setMe({ role: "owner" });
  chrome.paintGuestChrome();
  assert.equal(doc.getElementById("guest-banner").hidden, true);
  frames.length = 0;
  win.emit("resize");
  assert.equal(frames.length, 1);
  frames.shift()();
  assert.ok(doc.getElementById("bar").set.has("paint-refresh"));
  chrome.repaintPage();
  frames.pop()();
  assert.ok(E.$app.set.has("paint-refresh"));
}

// ---- files ----
{
  const files = imports.files.mountDaemonFiles({ agentHarnessWeb: { token: "", url: (p, s) => `/${s}${p}` }, isBlocked: () => false,
    ownerSurface: () => "app", toast() {}, browser });
  assert.equal(files.daemonImage("/images/1", { alt: "pic" }).src, "/app/images/1");
  const blocked = imports.files.mountDaemonFiles({ agentHarnessWeb: { token: "" }, isBlocked: () => true, ownerSurface: () => "app", toast() {}, browser });
  assert.equal(blocked.daemonImage("/images/1").src, undefined);
  await blocked.downloadDaemonFile("/x", "x");
}

// ---- signin ----
let toasts = [];
{
  const signin = imports.signin.mountSignIn({ els: E, api: async () => ({ authorization_url: "https://accounts/x" }),
    getWebAuth: () => ({ google: { available: false } }), toast: (m) => toasts.push(m), browser });
  signin.viewSignIn(true);
  assert.equal(E.$title.textContent, "Sign in");
  assert.match(E.$app.text, /not available on this server/);
  assert.match(E.$app.text, new RegExp(imports.signin.GOOGLE_FAILED.slice(0, 20)));
  await signin.startGoogle("signin", "code");
  assert.equal(browser.location.assigned, "https://accounts/x");
  const on = imports.signin.mountSignIn({ els: E, api: async () => ({}), getWebAuth: () => ({ google: { available: true, explanation: "why" } }), toast() {}, browser });
  on.viewSignIn(false);
  assert.match(E.$app.text, /Sign in with Google/);
  assert.match(E.$app.text, /why/);
}

// ---- router ----
const visited = [];
const viewsFor = () => new Proxy({}, { get: (_, name) => async (...args) => { visited.push([name, ...args]); } });
let router;
{
  const { hashParts, isTopLevel, normalizeHash, isProfileRoute, blockedRedirect, FEATURE_ROUTES } = imports.router;
  assert.deepEqual(hashParts("#/s/abc/info"), ["s", "abc", "info"]);
  assert.deepEqual(hashParts(""), []);
  assert.ok(isTopLevel([]) && isTopLevel(["chat", "x"]) && isTopLevel(["agents"]) && !isTopLevel(["agents", "x"]) && !isTopLevel(["s", "a"]));
  assert.equal(normalizeHash("#"), "#/");
  assert.equal(normalizeHash("agents"), "#/agents");
  assert.equal(normalizeHash("#/jobs"), "#/jobs");
  assert.ok(isProfileRoute(["settings"]) && !isProfileRoute(["jobs"]));
  assert.equal(blockedRedirect(["new"], "guest"), "#/profile");
  assert.equal(blockedRedirect(["jobs", "new"], "guest"), "#/jobs");
  assert.equal(blockedRedirect(["profile", "apps"], "guest"), "#/profile");
  assert.equal(blockedRedirect(["jobs"], "member"), "#/agents");
  assert.equal(blockedRedirect(["profile", "disk"], "member"), "#/profile");
  assert.equal(blockedRedirect(["profile", "account"], "member"), null);
  assert.equal(blockedRedirect(["jobs", "new"], "owner"), null);
  assert.equal(FEATURE_ROUTES.chat, "#/chat");

  const signin = { viewSignIn: (failed) => visited.push(["signin", failed]) };
  const stream = { watchDaemonConnection: () => visited.push(["watch"]) };
  router = imports.router.mountRouter({ els: E, session, chrome, signin, stream, views: viewsFor, toast: () => {}, browser });
  const run = async (hash, role = "owner") => {
    visited.length = 0;
    browser.location.hash = hash;
    session.setBootMe({ role });
    await router.route();
    return visited.filter(([name]) => name !== "watch" && name !== "viewSession" ? true : name === "viewSession");
  };
  assert.deepEqual(await run("#/agents"), [["viewList"]]);
  assert.equal(E.$back.hidden, true);
  assert.equal(E.$menu.hidden, false);
  assert.deepEqual(await run("#/s/abc/changes"), [["viewSession", "abc", "changes", undefined]]);
  assert.equal(E.$back.hidden, false);
  assert.deepEqual(await run("#/images/abc/edit"), [["viewImageEdit", "abc"]]);
  assert.deepEqual(await run("#/jobs/j1"), [["viewJob", "j1"]]);
  assert.deepEqual(await run("#/profile/account"), [["viewProfile", "account", undefined]]);
  assert.deepEqual(await run("#/actions/disk"), [["viewActions", "disk"]]);
  assert.deepEqual(await run("#/chat/c1"), [["viewChat", "c1"]]);
  assert.deepEqual(await run("#/profile/disk"), []);
  assert.equal(browser.location.hash, "#/actions/disk", "owner bookmarks redirect to Actions");
  assert.deepEqual(await run("#/jobs", "member"), []);
  assert.equal(browser.location.hash, "#/agents");
  assert.deepEqual(await run("#/agents", "signin"), [["signin", false]]);
  assert.equal(E.$back.hidden, true);
  let cleaned = 0;
  router.onLeave(() => { cleaned++; });
  await run("#/agents");
  assert.equal(cleaned, 1, "leaving a route runs its cleanups");
  session.setBlocked(true);
  router.go("#/jobs");
  assert.equal(browser.location.hash, "#/agents", "a blocked app does not navigate");
  session.setBlocked(false);
  E.$feature.value = "images";
  E.$feature.emit("change");
  assert.equal(browser.location.hash, "#/images");
  browser.location.hash = "#/s/abc/approval/z";
  E.$back.emit("click");
  assert.equal(browser.location.hash, "#/s/abc");
  browser.location.hash = "#/agents/x";
  E.$back.emit("click");
  assert.equal(browser.history.backed, 1);
}

// ---- drawer ----
{
  const { currentSection, mountDrawer } = imports.drawer;
  assert.equal(currentSection(["s", "a"]), "agents");
  assert.equal(currentSection(["new"]), "agents");
  assert.equal(currentSection(["actions", "disk"]), "actions");
  assert.equal(currentSection(["jobs"]), "jobs");
  assert.equal(currentSection([]), "");
  E.$drawer.hidden = true;
  E.$scrim.hidden = true;
  const recent = new El("section");
  recent.className = "drawer-recent";
  recent.append(E.$drawerChats);
  E.$drawer.append(recent);
  E.$drawer.querySelector = (selector) => selector === ".drawer-recent" ? recent : null;
  doc.body.classList = new El().classList;
  const drawer = mountDrawer({ els: E, session: { ...session, api: async () => [{ id: "c1", title: "One" }] }, browser });
  drawer.openDrawer();
  assert.equal(E.$drawer.hidden, false);
  assert.equal(E.$menu.attrs["aria-expanded"], "true");
  await tick();
  assert.equal(recent.hidden, false);
  assert.equal(E.$drawerChats.children[0].attrs.href, "#/chat/c1");
  assert.equal(E.$drawerChats.text, "One");
  drawer.closeDrawer({ restoreFocus: false });
  assert.equal(E.$drawer.hidden, true);
  assert.equal(E.$menu.attrs["aria-expanded"], "false");
  E.$menu.emit("click");
  assert.equal(E.$drawer.hidden, false);
  doc.emit("keydown", { key: "Escape", preventDefault() {} });
  assert.equal(E.$drawer.hidden, true);
}

// ---- update ----
{
  const meta = { protocols: { admin: { min: 2, max: 2 } }, update_hint: {} };
  const web = { compatibility: async () => meta };
  let routes = 0;
  const update = imports.update.mountUpdate({ els: E, agentHarnessWeb: web, session, chrome, route: async () => { routes++; },
    build: { WEB_BUILD_ID: "b1", WEB_PROTOCOL: 2 }, browser: { ...browser, window: { caches: null }, confirm: () => false, navigator: {} } });
  assert.equal(update.hasUnsavedInput(), false);
  assert.equal(await update.checkCompatibility(), true);
  meta.protocols.admin = { min: 3, max: 4 };
  assert.equal(await update.checkCompatibility(), false);
  assert.ok(session.isBlocked());
  assert.match(E.$app.text, /Update required|Update Agent Harness Web/);
  meta.protocols.admin = { min: 2, max: 2 };
  assert.equal(await update.checkCompatibility(), true);
  assert.equal(routes, 1, "recovering from a blocked state re-routes");
  assert.ok(!session.isBlocked());
  meta.update_hint = { web: { build_id: "b2" } };
  assert.equal(await update.checkCompatibility(), true, "declining the newer bundle keeps the app running");
  assert.equal(browser.sessionStorage.getItem("harness.webUpdatePrompt"), "b1");
  web.compatibility = async () => { throw new Error("offline"); };
  assert.equal(await update.checkCompatibility(), true);
}

// ---- warm-model ----
{
  const calls = [];
  const state = { blocked: false, guest: false, member: false };
  const warm = imports["warm-model"].createWarmModel({
    api: async (path) => { calls.push(path); return { manual: false }; },
    session: { isBlocked: () => state.blocked, isGuest: () => state.guest, isMember: () => state.member } });
  await warm();
  assert.deepEqual(calls, ["/gpu", "/models/warm"]);
  await warm();
  assert.equal(calls.length, 2, "throttled to once a minute");
  await warm(true);
  assert.equal(calls.length, 4, "force skips the throttle");
  state.guest = true;
  await warm(true);
  assert.equal(calls.length, 4, "guests never warm the model");
}

// ---- boot and secret are exercised by web_boot_splash.mjs and web_show_secret.mjs ----
assert.equal(typeof imports.boot.startBoot, "function");
assert.equal(typeof imports.secret.showSecretOnce, "function");
console.log("ok: lib modules import under plain Node and behave on their own");
