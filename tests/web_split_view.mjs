// UI harness for the desktop split view (#563): boots the real app.js at 1440 px and 1024 px and checks that #/agents and
// #/s/<id> share one list pane at 1280 px+ (kept mounted while the hash moves between sessions, the open row marked, [
// hiding it), that nothing open shows an empty state, and that below 1280 px both stay single pages with Back.
import assert from "node:assert/strict";
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";
import { El as BaseEl, Emitter, Node, createDocument, fakeEventSource, storage, walk } from "./web_stub_dom.mjs";
import { SPLITS, splitRoute, isListToggleKey } from "../harness/web/lib/layout.mjs";

// ---------- pure rules ----------
const agents = SPLITS.find((s) => s.key === "agents");
assert.deepEqual(splitRoute(["agents"]), { split: agents, selected: null });
assert.deepEqual(splitRoute(["s", "abc"]), { split: agents, selected: "abc" });
assert.deepEqual(splitRoute(["s", "abc", "changes"]), { split: agents, selected: "abc" });
assert.deepEqual(splitRoute(["s", "abc", "approval", "a1"]), { split: agents, selected: "abc" });
assert.equal(splitRoute(["s", "../x"]), null, "an id the daemon could not have issued is not a row");
assert.equal(splitRoute(["agents", "extra"]), null);
for (const parts of [[], ["chat"], ["new"], ["profile"], ["images"]]) {
  assert.equal(splitRoute(parts), null, `${parts.join("/")} is not part of the Agents split`);
}
const key = (extra = {}) => ({ key: "[", target: { tagName: "BODY" }, ...extra });
assert.equal(isListToggleKey(key()), true);
assert.equal(isListToggleKey(key({ key: "]" })), false);
for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) assert.equal(isListToggleKey(key({ target: { tagName } })), false, `never while typing in ${tagName}`);
assert.equal(isListToggleKey(key({ target: { tagName: "DIV", isContentEditable: true } })), false);
for (const mod of ["ctrlKey", "metaKey", "altKey", "isComposing", "defaultPrevented"]) assert.equal(isListToggleKey(key({ [mod]: true })), false, mod);

// ---------- the app ----------
class El extends BaseEl {
  setAttribute(k, v) {
    super.setAttribute(k, v);
    if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
  }
  insertBefore(node, ref) {
    const i = this.childNodes.indexOf(ref);
    this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, node);
    node.parentNode = this;
  }
  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  querySelectorAll(sel) {
    if (sel === "[data-split-key]") return walk(this, (n) => n !== this && n.dataset.splitKey !== undefined);
    return [];
  }
  closest() { return null; }
}

const { byId, doc } = createDocument({ ElClass: El });
doc.body.append(byId.bar, byId["tab-bar"], byId.app);

const loc = {
  href: "http://localhost/#/agents", origin: "http://localhost", protocol: "http:", pathname: "/", _hash: "#/agents",
  get hash() { return this._hash; },
  set hash(v) { this._hash = String(v); this.href = `http://localhost/${v}`; },
  replace(url) { this.hash = String(url); win.dispatchEvent({ type: "hashchange" }); },
};
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body, text: async () => JSON.stringify(body),
});
const now = Date.now() / 1000;
const sessions = [
  { id: "s1", title: "Fix the flaky test", status: "running", project: "web", target: "tower", updated_at: now - 60 },
  { id: "s2", title: "Rotate creds", status: "done", project: "homelab", target: "tower", updated_at: now - 600 },
];
const detail = (s) => ({ ...s, backend: "local", model: "m", totals: {}, created_at: now - 900, workspace: "/tmp" });
const fetched = [];
let failList = false;
let health = { protocols: { admin: { min: 1, max: 99 } }, update_hint: {} };  // the list's /sessions answers 502 (the rail's count shares the path; it just hides)
const fakeFetch = async (url) => {
  const path = String(url).replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  fetched.push(path);
  if (path === "/health") return jsonResp(health);
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/sessions") return failList ? jsonResp({ detail: "Bad gateway" }, 502) : jsonResp(sessions);
  const one = sessions.find((s) => path === `/sessions/${s.id}`);
  if (one) return jsonResp(detail(one));
  if (/^\/sessions\/s\d\/changes$/.test(path)) return jsonResp({ removed: false, secret_scan: null, repos: [] });
  if (path === "/queue" || path.startsWith("/chats")) return jsonResp([]);
  const jobList = [
    { id: "j1", name: "Morning check", prompt: "Check services", cron: "0 8 * * *", project: "web", backend: "local", model: "", notify: "low", enabled: true, recent: [] },
    { id: "j2", name: "Disk report", prompt: "Check storage", cron: "0 8 * * *", project: "web", backend: "local", model: "", notify: "attention", enabled: false, recent: [] },
  ];
  if (path === "/jobs") return jsonResp(jobList);
  const jobDetail = jobList.find((j) => path === `/jobs/${j.id}`);
  if (jobDetail) return jsonResp(jobDetail);
  if (path === "/models") return jsonResp([]);
  if (path === "/backends?auth=skip") return jsonResp([{ name: "local", available: true }]);
  if (path.startsWith("/jobs/preview")) return jsonResp({ ok: true, next: [] });
  if (path === "/gpu") return jsonResp({ manual: false, state: "clear" });
  if (path === "/projects") return jsonResp([{ name: "web", target: "tower" }]);
  return jsonResp({});
};

// A controllable (min-width: 1280px) query: flipping `matches` and emitting "change" is the window crossing it.
const wideQuery = Object.assign(new Emitter(), { matches: true });
const sources = [];  // every EventSource the app opens
const win = new Emitter();
Object.assign(win, {
  addEventListener: (...a) => Emitter.prototype.addEventListener.call(win, ...a),
  removeEventListener: (...a) => Emitter.prototype.removeEventListener.call(win, ...a),
  dispatchEvent: (...a) => Emitter.prototype.dispatchEvent.call(win, ...a),
  localStorage: storage(), sessionStorage: storage(), location: loc,
  navigator: { serviceWorker: undefined, userAgent: "test" },
  history: { replaceState() {}, back() {} },
  scrollTo() {}, innerWidth: 1440, innerHeight: 900, scrollY: 0,
  matchMedia: (q) => (q === "(min-width: 1280px)" ? wideQuery : Object.assign(new Emitter(), { matches: false })),
  EventSource: fakeEventSource(sources), fetch: fakeFetch,
  requestAnimationFrame: (fn) => setTimeout(fn, 0), cancelAnimationFrame: (id) => clearTimeout(id),
});
globalThis.window = win;
globalThis.localStorage = win.localStorage;
globalThis.location = loc;
globalThis.fetch = fakeFetch;
const { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } = await import("../harness/web/client.mjs");
await runApp(createContext({
  window: win, document: doc, location: loc, history: win.history, navigator: win.navigator, localStorage: win.localStorage,
  sessionStorage: win.sessionStorage, EventSource: win.EventSource, fetch: fakeFetch, URL, AbortController, TextDecoder, console,
  getComputedStyle: (el) => el.style, setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: win.requestAnimationFrame, cancelAnimationFrame: win.cancelAnimationFrame,
  agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL, Node, Event,
}));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = async (pred, label, ms = 2000) => {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (pred()) return;
    await sleep(10);
  }
  throw new Error(`timeout waiting for ${label}`);
};
const go = async (hash) => {
  loc.hash = hash;
  win.dispatchEvent({ type: "hashchange" });
  await sleep(60);
};
const body = doc.body.classList;
const pane = () => doc.body.childNodes.find((n) => n instanceof BaseEl && n.id === "split-list");
const rows = () => walk(pane(), (n) => n.dataset?.splitKey !== undefined);
const marked = () => rows().filter((r) => r.attributes["aria-current"] === "page").map((r) => r.dataset.splitKey);
const toggle = () => byId.bar.childNodes.find((n) => n instanceof BaseEl && n.id === "split-toggle");
const press = (extra = {}) => doc.dispatchEvent({ type: "keydown", key: "[", target: doc.body, ...extra });

// #/agents at 1440: the list in its pane beside the rail, an empty detail, no Back and no section title in the bar.
await waitFor(() => rows().length === 2, "the list in its pane");
assert.equal(doc.body.childNodes.indexOf(pane()), doc.body.childNodes.indexOf(byId.bar) - 1,
  "the pane sits just before the header bar, so Tab reads the list before the detail (#571)");
assert.ok(body.contains("split") && !body.contains("split-open"), "a split with nothing open");
assert.equal(pane().attributes["aria-label"], "Agents list");
const head = walk(pane(), (n) => n.className === "split-head")[0];
assert.match(head.textContent, /^Agents\+ New task$/, "the pane carries the title and New task");
assert.equal(walk(head, (n) => n.tagName === "A")[0].attributes.href, "#/new");
assert.ok(!byId.bar.childNodes.some((n) => n instanceof BaseEl && n.classList.contains("list-new")), "New task is not repeated in the bar");
assert.match(byId.app.textContent, /No session open/);
assert.match(byId.app.textContent, /Pick one from the list, or start a new task\./);
assert.equal(byId.back.hidden, true);
assert.equal(byId.title.hidden, true, "the bar repeats no title while nothing is open");
assert.deepEqual(marked(), []);
assert.deepEqual(rows().map((r) => r.attributes.href), ["#/s/s1", "#/s/s2"]);
press();
assert.ok(!body.contains("split-collapsed"), "[ does nothing with nothing open: the list is all there is");

// Opening a row: the session renders in <main>, the same list stays mounted with that row marked.
const listBody = walk(pane(), (n) => n.className === "split-body")[0];
const firstRow = rows()[0];
const sessionFetches = () => fetched.filter((p) => p === "/sessions").length;
await go("#/s/s1");
await waitFor(() => byId.title.textContent === "Fix the flaky test", "the session title");
assert.ok(body.contains("split-open"));
assert.equal(byId.back.hidden, true, "beside its list a session needs no Back");
assert.equal(walk(pane(), (n) => n.className === "split-body")[0], listBody, "the list was not rebuilt");
assert.equal(rows()[0], firstRow);
assert.deepEqual(marked(), ["s1"]);
assert.doesNotMatch(byId.app.textContent, /No session open/);
const before = sessionFetches();
await go("#/s/s2");
await waitFor(() => byId.title.textContent === "Rotate creds", "the second session");
assert.deepEqual(marked(), ["s2"], "the mark follows the hash");
assert.equal(walk(pane(), (n) => n.className === "split-body")[0], listBody);
await go("#/s/s2/changes");
assert.deepEqual(marked(), ["s2"], "a session's tabs keep its row marked");
assert.ok(sessionFetches() - before <= 1, "moving between rows does not reload the list (the rail's count may refresh)");

// The pane scrolls on its own, so its wheel events stop there: an upward wheel over the list must not reach the window,
// where the transcript would stop following new output (review on #578).
let stopped = 0;
pane().dispatchEvent({ type: "wheel", deltaY: -40, stopPropagation() { stopped++; } });
assert.equal(stopped, 1, "the pane stops its wheel events");

// [ hides the list and shows it again; the bar's toggle says which; typing in a field never toggles it.
assert.equal(toggle().attributes["aria-controls"], "split-list");
assert.equal(toggle().attributes["aria-label"], "Hide the list");
press();
assert.ok(body.contains("split-collapsed"));
assert.equal(toggle().attributes["aria-pressed"], "true");
assert.equal(toggle().attributes["aria-label"], "Hide the list", "a toggle keeps its name; aria-pressed carries the state");
press({ target: new El("textarea") });
assert.ok(body.contains("split-collapsed"), "[ typed into the composer is text");
press({ ctrlKey: true });
assert.ok(body.contains("split-collapsed"));
toggle().click();
assert.ok(!body.contains("split-collapsed"), "the toggle shows the list again");
assert.equal(toggle().attributes["aria-pressed"], "false");
press();
await go("#/s/s1");
assert.ok(body.contains("split-collapsed"), "the list stays hidden while reading the next session");
press();
assert.ok(!body.contains("split-collapsed"));
await go("#/agents");
assert.ok(body.contains("split") && !body.contains("split-open"));
assert.equal(walk(pane(), (n) => n.className === "split-body")[0], listBody, "back to the list route: the same list");
assert.deepEqual(marked(), []);

// Leaving the split closes the pane.
await go("#/chat");
assert.ok(!body.contains("split") && !body.contains("split-open"));
assert.equal(pane().childNodes.length, 0, "the pane is emptied");
await go("#/s/s1");
await waitFor(() => rows().length === 2, "a fresh list when the split reopens on a deep link");
assert.notEqual(walk(pane(), (n) => n.className === "split-body")[0], listBody);
assert.deepEqual(marked(), ["s1"]);

// 1024 px: one pane. Crossing the breakpoint on a session keeps the session's page (review on #578: an unsent message
// must survive a resize) and only drops the list beside it; Back takes its place.
const sessionPage = byId.app.childNodes[0];
assert.ok(sessionPage, "the session is rendered");
wideQuery.matches = false;
wideQuery.emit("change");
await sleep(60);
assert.equal(byId.app.childNodes[0], sessionPage, "narrowing does not re-render the session");
assert.equal(byId.title.textContent, "Fix the flaky test");
assert.ok(!body.contains("split"));
assert.equal(pane().childNodes.length, 0);
assert.equal(byId.back.hidden, false, "below 1280 px a session has Back");
wideQuery.matches = true;
wideQuery.emit("change");
await waitFor(() => rows().length === 2, "the list beside the session again");
assert.equal(byId.app.childNodes[0], sessionPage, "widening does not re-render the session either");
assert.equal(byId.back.hidden, true);
assert.deepEqual(marked(), ["s1"]);
wideQuery.matches = false;
wideQuery.emit("change");
await sleep(60);
press();
assert.ok(!body.contains("split-collapsed"), "[ does nothing without a split");
await go("#/agents");
await waitFor(() => /Fix the flaky test/.test(byId.app.textContent), "the list as its own page");
assert.ok(!body.contains("split"));
assert.equal(byId.title.textContent, "Agents");
assert.ok(byId.bar.childNodes.some((n) => n instanceof BaseEl && n.classList.contains("list-new")), "New task back in the bar");
assert.doesNotMatch(byId.app.textContent, /No session open/);
wideQuery.matches = true;
wideQuery.emit("change");
await waitFor(() => rows().length === 2 && /No session open/.test(byId.app.textContent), "the split again at 1440");

// A list that fails to load says why with Retry, and is not counted as loaded (review on #578): Retry, the next route in
// the split and the next server event each try again.
const paneError = () => walk(pane(), (n) => n.className === "note bad")[0];
const retryButton = () => walk(pane(), (n) => n.tagName === "BUTTON" && n.textContent === "Retry")[0];
const errors = console.error;
console.error = () => {};
failList = true;
await go("#/jobs");
await go("#/agents");
await waitFor(() => paneError(), "the pane's load error");
assert.ok(retryButton(), "the error offers Retry");
assert.equal(rows().length, 0);
retryButton().click();
await sleep(60);
assert.ok(paneError(), "still failing: still saying so");
failList = false;
retryButton().click();
await waitFor(() => rows().length === 2 && !paneError(), "Retry loads the list");
failList = true;
await go("#/jobs");
await go("#/s/s1");
await waitFor(() => paneError(), "a deep link whose list fails");
failList = false;
await go("#/s/s2");
await waitFor(() => rows().length === 2 && !paneError(), "the next route in the split retries");
assert.deepEqual(marked(), ["s2"]);
failList = true;
await go("#/jobs");
await go("#/agents");
await waitFor(() => paneError(), "a failed list again");
failList = false;
const daemon = sources.find((src) => /\/v1\/events$/.test(src.url) && src.readyState !== 2);
assert.ok(daemon, "the app-wide stream is open");
daemon.emit("status", { status: "running" }, 1);
await waitFor(() => rows().length === 2 && !paneError(), "the next server event retries");
console.error = errors;


// Jobs use the same router lifecycle: deep links, empty detail, selection and preserving an unsaved form on resize.
await go("#/jobs");
await waitFor(() => rows().length === 2 && /No job open/.test(byId.app.textContent), "Jobs list with empty detail");
assert.equal(pane().attributes["aria-label"], "Jobs list");
assert.equal(byId.back.hidden, true);
await go("#/jobs/j1");
await waitFor(() => /Morning check/.test(byId.app.textContent), "Jobs deep link");
assert.deepEqual(marked(), ["j1"]);
const jobPage = byId.app.childNodes[0];
const jobName = walk(jobPage, (n) => n.id === "job-name")[0];
jobName.value = "Unsaved job name";
const jobList = walk(pane(), (n) => n.className === "split-body")[0];
await go("#/jobs/j2");
await waitFor(() => /Disk report/.test(byId.app.textContent), "second job");
assert.deepEqual(marked(), ["j2"]);
assert.equal(walk(pane(), (n) => n.className === "split-body")[0], jobList, "Jobs list stays mounted");
const secondPage = byId.app.childNodes[0];
const secondName = walk(secondPage, (n) => n.id === "job-name")[0];
secondName.value = "Draft survives resize";
wideQuery.matches = false;
wideQuery.emit("change");
await sleep(60);
assert.equal(byId.app.childNodes[0], secondPage);
assert.equal(secondName.value, "Draft survives resize");
assert.equal(byId.back.hidden, false);
assert.ok(!body.contains("split"));
wideQuery.matches = true;
wideQuery.emit("change");
await waitFor(() => rows().length === 2, "Jobs list after widening");
assert.equal(byId.app.childNodes[0], secondPage);
assert.equal(secondName.value, "Draft survives resize");
assert.deepEqual(marked(), ["j2"]);
press();
assert.ok(body.contains("split-collapsed"));
press({ target: secondName });
assert.ok(body.contains("split-collapsed"), "[ in the job form is text");
press();
await go("#/jobs/new");
await waitFor(() => /Create/.test(byId.app.textContent), "New job in the detail pane");
assert.ok(body.contains("split-open"));
assert.deepEqual(marked(), []);

// A protocol mismatch blocks the app on the update card: no list pane beside it (review on #578).
await go("#/s/s1");
await waitFor(() => body.contains("split-open"), "a session beside its list");
health = { protocols: { admin: { min: 0, max: 0 } }, update_hint: {} };
doc.dispatchEvent({ type: "visibilitychange" });
await waitFor(() => /Update Agent Harness/.test(byId.app.textContent), "the update card");
assert.ok(!body.contains("split") && !body.contains("split-open"), "the split closes for the update card");
assert.equal(pane().childNodes.length, 0);
console.log("ok");
process.exit(0);
