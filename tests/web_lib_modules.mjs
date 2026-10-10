// The lib/ modules split out of app.js (#258) import under plain Node (no DOM at module top level) and behave on their own:
// each is imported directly and driven with a small stub DOM and fake client.
import assert from "node:assert/strict";

// ---- stub DOM (installed only after the imports below, to prove the modules need none at import time) ----
const imports = {};
for (const name of ["dom", "widgets", "session", "stream", "chrome", "files", "signin", "router", "tabs", "update", "boot", "warm-model", "secret", "sheet"]) {
  imports[name] = await import(`../harness/web/lib/${name}.mjs`);
}
const { h, fill, append, kids } = imports.dom;
const { Emitter, Node } = await import("./web_stub_dom.mjs");

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
  append(...nodes) {
    for (const node of nodes) {
      if (node instanceof El) node.parentNode = this;
      this.children.push(node);
    }
  }
  after(node) {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    siblings.splice(siblings.indexOf(this) + 1, 0, node);
    node.parentNode = this.parentNode;
  }
  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }
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
  const { safeStreamUrl, validId, mountStream, retryDelay, RETRY_CAP_MS, OFFLINE_AFTER, GRACE_AFTER_MS } = imports.stream;
  // #510: exponential backoff with equal jitter, capped, in place of a fixed 3 s.
  assert.equal(retryDelay(0, () => 0), 500);
  assert.equal(retryDelay(0, () => 1), 1000);
  assert.equal(retryDelay(3, () => 0), 4000);
  assert.equal(retryDelay(3, () => 1), 8000);
  assert.equal(retryDelay(20, () => 1), RETRY_CAP_MS, "the delay is capped");
  assert.equal(retryDelay(20, () => 0), RETRY_CAP_MS / 2, "half the delay is always random, even at the cap");
  const spread = new Set(Array.from({ length: 20 }, () => retryDelay(2)));
  assert.ok(spread.size > 1, "real retries are jittered");
  for (const d of spread) assert.ok(d >= 2000 && d <= 4000);
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
  const sb = { ...browser, EventSource: FakeEventSource, navigator: { onLine: true } };
  const stream = mountStream({ agentHarnessWeb: { token: "", baseUrl: "", headers: () => ({}), url: (p) => `/api/v1${p}` },
    isBlocked: () => blocked, setConnState: (state) => live.push(state), ownerSurface: () => "app", isGuest: () => false, browser: sb });
  const seen = [];
  const states = [];
  const stop = stream.openStream(async () => "/api/v1/events", { ping: (d) => seen.push(d) }, { indicate: true, onState: (s) => states.push(s) });
  await tick();
  assert.equal(sources.length, 1);
  sources[0].onopen();
  sources[0].emit("ping", { data: '{"n":1}' });
  sources[0].emit("ping", { data: undefined });
  assert.deepEqual(seen, [{ n: 1 }]);
  assert.deepEqual(live, ["live"]);
  // A dropped stream closes the EventSource (no browser auto-retry at a fixed interval) and says Reconnecting.
  sources[0].onerror();
  assert.ok(sources[0].closed, "an error closes the source; our backoff schedules the retry");
  assert.deepEqual(states, ["live", "reconnecting"]);
  // Each failed attempt in a row counts; after OFFLINE_AFTER of them the state is Offline (still retrying).
  for (let i = 1; i < OFFLINE_AFTER; i++) {
    win.dispatchEvent({ type: "online" });  // reconnect at once, as when the network comes back
    await tick();
    sources.at(-1).onerror();
  }
  assert.equal(sources.length, OFFLINE_AFTER);
  assert.deepEqual(states, ["live", "reconnecting", "offline"]);
  win.dispatchEvent({ type: "online" });
  await tick();
  sources.at(-1).onopen();
  assert.deepEqual(states.at(-1), "live", "a successful open is Live again and resets the count");
  sources.at(-1).onerror();
  assert.equal(states.at(-1), "reconnecting");
  // The browser saying it is offline drops the stream and goes straight to Offline.
  win.dispatchEvent({ type: "online" });
  await tick();
  sources.at(-1).onopen();
  sb.navigator.onLine = false;
  win.dispatchEvent({ type: "offline" });
  assert.ok(sources.at(-1).closed);
  assert.equal(states.at(-1), "offline");
  sb.navigator.onLine = true;
  // A stream that was live a while and then ends reconnects once at once, quietly: no Reconnecting for a routine close.
  win.dispatchEvent({ type: "online" });
  await tick();
  sources.at(-1).onopen();
  const realNow = Date.now;
  Date.now = () => realNow() + GRACE_AFTER_MS;
  const quiet = states.length;
  const opened = sources.length;
  sources.at(-1).onerror();
  await tick();
  Date.now = realNow;
  assert.equal(states.length, quiet, "the first close after a long live spell changes no state");
  assert.equal(sources.length, opened + 1, "it reconnects at once");
  sources.at(-1).onerror();
  assert.equal(states.at(-1), "reconnecting", "if that reconnect fails too, say so");
  // A stale source's late error (an earlier generation) changes nothing.
  win.dispatchEvent({ type: "online" });
  await tick();
  sources.at(-1).onopen();
  const before = states.length;
  sources[0].onerror();
  assert.equal(states.length, before, "an old source's error is ignored");
  assert.deepEqual(live, states, "the indicating stream drives the header chip with the same states");
  const count = sources.length;
  stop();
  assert.ok(sources.at(-1).closed);
  assert.equal(live.length, before, "closing a stream says nothing: no false Offline when a page leaves");
  win.dispatchEvent({ type: "online" });
  await tick();
  assert.equal(sources.length, count, "a closed stream stops listening for the network");
  blocked = true;
  stream.openStream(async () => "/api/v1/events", {})();
  await tick();
  assert.equal(sources.length, count, "a blocked app opens no stream");
  blocked = false;
  stream.watchDaemonConnection();
  stream.watchDaemonConnection();
  await tick();
  assert.equal(sources.length, count + 1, "the daemon connection is watched once");
  // The daemon stream fails and waits out its backoff; a page stream reaching the server, or a route change, retries it now.
  const daemonSource = sources.at(-1);
  daemonSource.onopen();
  daemonSource.onerror();
  assert.equal(live.at(-1), "reconnecting");
  const pageStop = stream.openStream(async () => "/api/v1/sessions/x/events", {});
  await tick();
  sources.at(-1).onopen();  // the page stream is live
  await tick();
  assert.equal(sources.length, count + 3, "a page stream going live nudges the daemon stream to retry at once");
  sources.at(-1).onopen();
  assert.equal(live.at(-1), "live");
  stream.watchDaemonConnection();
  await tick();
  assert.equal(sources.length, count + 3, "a live daemon stream is left alone on a route change");
  sources.at(-1).onerror();
  stream.watchDaemonConnection();
  await tick();
  assert.equal(sources.length, count + 4, "a route change retries a failed daemon stream now");
  pageStop();
}

// ---- chrome ----
const els = Object.fromEntries(["app", "title", "back", "conn", "profile-icon", "fab-host", "fab", "tab-bar", "settings-btn"]
  .map((id) => [id, doc.getElementById(id)]));
const E = { $app: els.app, $title: els.title, $back: els.back, $conn: els.conn, $profileIcon: els["profile-icon"],
  $fabHost: els["fab-host"], $fab: els.fab, $tabBar: els["tab-bar"], $settings: els["settings-btn"] };
doc.getElementById("bar").append(E.$back, E.$title, E.$conn, E.$settings);
const tabLinks = ["chat", "agents", "jobs", "images", "profile"].map((tab) => Object.assign(new El("a"), { dataset: { tab } }));
E.$tabBar.querySelectorAll = (sel) => (sel === "a[data-tab]" ? tabLinks : []);
const chrome = imports.chrome.mountChrome({ els: E, browser, session });
{
  chrome.toast("hello", 10);
  assert.equal(doc.getElementById("toast").textContent, "hello");
  assert.equal(doc.getElementById("toast").hidden, false);
  let undone = 0;
  chrome.toast("Job paused", 10, { label: "Undo", onClick: () => { undone++; } });
  const undo = doc.getElementById("toast").children.find((c) => c instanceof El);
  assert.equal(undo.textContent, "Undo", "an action toast carries its button (#511)");
  undo.dispatchEvent({ type: "click" });
  assert.equal(undone, 1);
  assert.equal(doc.getElementById("toast").hidden, true, "tapping the action dismisses the toast");
  // #510: the header chip says Live / Reconnecting / Offline in words, with a matching data-state for its colours.
  chrome.setConnState("live");
  assert.ok(E.$conn.set.has("live"));
  assert.equal(E.$conn.hidden, false);
  assert.equal(E.$conn.textContent, "Live");
  assert.equal(E.$conn.dataset.state, "live");
  chrome.setConnState("reconnecting");
  assert.ok(!E.$conn.set.has("live"));
  assert.equal(E.$conn.textContent, "Reconnecting");
  assert.equal(E.$conn.dataset.state, "reconnecting");
  chrome.setConnState("offline");
  assert.equal(E.$conn.textContent, "Offline");
  assert.equal(E.$conn.dataset.state, "offline");
  assert.match(E.$conn.title, /retrying/);
  // #512: a page can follow the chip; each report reaches it until it unsubscribes.
  const followed = [];
  const unfollow = chrome.onConnState((state) => followed.push(state));
  chrome.setConnState("bogus");
  unfollow();
  chrome.setConnState("live");
  assert.deepEqual(followed, ["offline"], "an unknown state reaches followers as offline, and none after unsubscribing");
  E.$back.hidden = true;
  chrome.setHeader("agents", "Title", { page: true });
  assert.equal(E.$title.textContent, "Title");
  assert.ok(doc.getElementById("bar").set.has("page"));
  assert.ok(doc.getElementById("bar").set.has("top"), "a section's own screen gets the large title");
  chrome.setHeader("chat", "");
  assert.equal(E.$title.textContent, "Chat", "the section name stands in until the page knows its title");
  E.$back.hidden = false;
  chrome.setHeader("jobs", "");
  assert.equal(E.$title.hidden, true);
  assert.ok(!doc.getElementById("bar").set.has("top"));
  E.$back.hidden = true;
  chrome.showListAction("#/new", "Go");
  assert.equal(E.$fab.href, "#/new");
  assert.equal(E.$fabHost.hidden, false);
  const header = doc.getElementById("bar");
  const newAction = header.children[header.children.indexOf(E.$title) + 1];
  assert.equal(newAction.className, "btn primary list-new");
  assert.equal(newAction.href, "#/new");
  chrome.showListAction("#/jobs/new", "+ New job");
  assert.ok(!header.children.includes(newAction), "showListAction replaces the previous list action");
  const jobAction = header.children[header.children.indexOf(E.$title) + 1];
  assert.equal(jobAction.href, "#/jobs/new");
  chrome.setHeader("jobs", "Job", { page: true });
  assert.ok(!header.children.includes(jobAction), "a detail header clears the list action");
  E.$fabHost.hidden = true;
  session.setMe({ role: "guest", guest_until: "2099-01-01T00:00:00Z" });
  chrome.showListAction("#/new", "Go");
  assert.equal(E.$fabHost.hidden, true, "guests get no floating button");
  chrome.paintGuestChrome();
  assert.ok(doc.documentElement.set.has("guest"));
  assert.match(doc.getElementById("guest-banner").textContent, /^Demo access/);
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
  const { hashParts, isTopLevel, normalizeHash, isProfileRoute, blockedRedirect } = imports.router;
  assert.deepEqual(hashParts("#/s/abc/info"), ["s", "abc", "info"]);
  assert.deepEqual(hashParts(""), []);
  assert.ok(isTopLevel([]) && isTopLevel(["chat", "x"]) && isTopLevel(["agents"]) && !isTopLevel(["agents", "x"]) && !isTopLevel(["s", "a"]));
  assert.ok(isTopLevel(["profile"]) && !isTopLevel(["profile", "appearance"]) && !isTopLevel(["settings"]) && !isTopLevel(["actions", "disk"]),
    "Profile is a tab; Settings and Actions sit under the gear with Back (#506)");
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

  const signin = { viewSignIn: (failed) => visited.push(["signin", failed]) };
  const stream = { watchDaemonConnection: () => visited.push(["watch"]) };
  const tabs = imports.tabs.mountTabs({ els: E, session, browser });
  router = imports.router.mountRouter({ els: E, session, chrome, tabs, signin, stream, views: viewsFor, toast: () => {}, browser });
  const run = async (hash, role = "owner") => {
    visited.length = 0;
    browser.location.hash = hash;
    session.setBootMe({ role });
    await router.route();
    return visited.filter(([name]) => name !== "watch" && name !== "viewSession" ? true : name === "viewSession");
  };
  assert.deepEqual(await run("#/agents"), [["viewList"]]);
  assert.equal(E.$back.hidden, true);
  assert.equal(E.$tabBar.hidden, false);
  assert.equal(E.$settings.hidden, false);
  assert.equal(tabLinks[1].attrs["aria-current"], "page");
  assert.deepEqual(await run("#/s/abc/changes"), [["viewSession", "abc", "changes", undefined]]);
  assert.equal(E.$back.hidden, false);
  assert.equal(E.$tabBar.hidden, true, "a session has its own bottom controls");
  assert.equal(E.$settings.hidden, true);
  assert.deepEqual(await run("#/images/abc/edit"), [["viewImageEdit", "abc"]]);
  assert.deepEqual(await run("#/jobs/j1"), [["viewJob", "j1"]]);
  assert.deepEqual(await run("#/profile/account"), [["viewProfile", "account", undefined]]);
  assert.equal(E.$tabBar.hidden, false, "Settings pages keep the tab bar");
  assert.equal(tabLinks[4].attrs["aria-current"], "page");
  assert.deepEqual(await run("#/tasks"), []);
  assert.equal(browser.location.hash, "#/jobs", "old Tasks links land on Jobs");
  assert.deepEqual(await run("#/tasks/j1"), []);
  assert.equal(browser.location.hash, "#/jobs/j1");
  assert.deepEqual(await run("#/actions/disk"), [["viewActions", "disk"]]);
  assert.deepEqual(await run("#/chat/c1"), [["viewChat", "c1"]]);
  assert.deepEqual(await run("#/profile/disk"), []);
  assert.equal(browser.location.hash, "#/actions/disk", "owner bookmarks redirect to Actions");
  assert.deepEqual(await run("#/jobs", "member"), []);
  assert.equal(browser.location.hash, "#/agents");
  assert.equal(tabLinks[2].hidden, true, "members have no Jobs tab");
  assert.equal(tabLinks[0].hidden, true, "members have no Chat tab");
  chrome.showListAction("#/new", "+ New task");
  const expiredAction = doc.getElementById("bar").children.find((el) => el.className === "btn primary list-new");
  assert.ok(expiredAction);
  assert.deepEqual(await run("#/agents", "signin"), [["signin", false]]);
  assert.ok(!doc.getElementById("bar").children.includes(expiredAction), "sign-in clears the desktop list action");
  assert.equal(E.$fabHost.hidden, true, "sign-in also hides the phone FAB");
  assert.equal(E.$back.hidden, true);
  assert.equal(E.$tabBar.hidden, true, "sign-in offers no navigation");
  let cleaned = 0;
  router.onLeave(() => { cleaned++; });
  await run("#/agents");
  assert.equal(cleaned, 1, "leaving a route runs its cleanups");
  session.setBlocked(true);
  router.go("#/jobs");
  assert.equal(browser.location.hash, "#/agents", "a blocked app does not navigate");
  session.setBlocked(false);
  browser.location.hash = "#/s/abc/approval/z";
  E.$back.emit("click");
  assert.equal(browser.location.hash, "#/s/abc");
  browser.location.hash = "#/agents/x";
  E.$back.emit("click");
  assert.equal(browser.history.backed, 1);
}

// ---- tabs ----
{
  const { currentTab, tabBarHidden, tabHidden } = imports.tabs;
  for (const [hash, tab, hidden] of [
    ["#/chat", "chat", false], ["#/chat/c1", "chat", false], ["#/agents", "agents", false], ["#/jobs", "jobs", false],
    ["#/images", "images", false], ["#/profile", "profile", false], ["#/settings", "profile", false],
    ["#/profile/appearance", "profile", false], ["#/actions/disk", "profile", false], ["#/", "", false],
    ["#/s/a", "agents", true], ["#/new", "agents", true], ["#/jobs/j1", "jobs", true], ["#/images/i/edit", "images", true],
  ]) {
    const parts = imports.router.hashParts(hash);
    assert.equal(currentTab(parts), tab, `tab for ${hash}`);
    assert.equal(tabBarHidden(parts), hidden, `tab bar on ${hash}`);
  }
  assert.ok(tabHidden("chat", { canChat: false, member: false }) && !tabHidden("chat", { canChat: true, member: false }));
  assert.ok(tabHidden("images", { canChat: true, member: true }) && !tabHidden("agents", { canChat: false, member: true }));
  assert.ok(!tabHidden("profile", { canChat: false, member: true }));
}

// ---- update ----
{
  const meta = { protocols: { admin: { min: 2, max: 2 } }, update_hint: {} };
  const web = { compatibility: async () => meta };
  let routes = 0;
  const offers = [];
  const tabs = imports.tabs.mountTabs({ els: E, session, browser });
  const update = imports.update.mountUpdate({ els: E, agentHarnessWeb: web, session, chrome, tabs, route: async () => { routes++; },
    build: { WEB_BUILD_ID: "b1", WEB_PROTOCOL: 2 }, browser: { ...browser, window: { caches: null }, navigator: {} }, confirmSheet: async (ask) => { offers.push(ask.title); return false; } });
  assert.equal(update.hasUnsavedInput(), false);
  assert.equal(await update.checkCompatibility(), true);
  meta.protocols.admin = { min: 3, max: 4 };
  assert.equal(await update.checkCompatibility(), false);
  assert.ok(session.isBlocked());
  assert.match(E.$app.text, /Update required|Update Agent Harness Web/);
  assert.equal(E.$tabBar.hidden, true, "a blocked app offers no navigation");
  meta.protocols.admin = { min: 2, max: 2 };
  assert.equal(await update.checkCompatibility(), true);
  assert.equal(routes, 1, "recovering from a blocked state re-routes");
  assert.ok(!session.isBlocked());
  meta.update_hint = { web: { build_id: "b2" } };
  assert.equal(await update.checkCompatibility(), true, "declining the newer bundle keeps the app running");
  await tick();
  assert.deepEqual(offers, ["Update Agent Harness Web?"], "the newer bundle is offered once");
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
