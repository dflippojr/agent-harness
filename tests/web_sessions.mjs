// Renders the extracted session list page with stub deps so a missing import or dep fails CI (#258 stage i).
import assert from "node:assert/strict";
import { mountSessions } from "../harness/web/pages/sessions.mjs";

const placed = [];
const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
  value: "", hidden: !!attrs?.hidden, dataset: {}, addEventListener() {}, querySelector: () => null,
  after(n) { n.parentNode = this; placed.push({ after: this, n }); } });
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));
const store = new Map();
const browser = {
  window: { addEventListener() {}, removeEventListener() {} },
  document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {} },
  localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) },
};
let failSessions = null;
const api = async (path) => {
  if (path === "/sessions" && failSessions) throw failSessions;
  if (path === "/sessions") return [{ id: "s1", title: "Fix the bug", status: "done", updated_at: new Date().toISOString(), target: "tower", project: "scratch", pending_approvals: [{ id: "a1" }] }];
  if (path === "/projects") return [{ name: "scratch", target: "tower" }];
  if (path === "/gpu") return { manual: false, state: "clear" };
  return [];
};
const rendered = [];
const appended = [];
// The list reuses the app-wide stream (#563): its change and state hooks stand in for an EventSource.
const changeHooks = [];
const stateHooks = [];
const subscribe = (hooks) => (fn) => { hooks.push(fn); return () => hooks.splice(hooks.indexOf(fn), 1); };
const left = [];
const headers = [];
const page = mountSessions({
  $app: "APP", h: el, fill: (_t, ...n) => rendered.push(...n.flat(Infinity)), append: (_t, ...n) => appended.push(...n.flat(Infinity)), api, setHeader: (...a) => headers.push(a), showListAction() {}, onLeave: (fn) => left.push(fn),
  isMember: () => false, isGuest: () => false, badge: (s) => el("badge", {}, s), reviewBadge: () => el("rb"), REVIEW_LABEL: {}, jobStatusBadge: () => el("jb"),
  onDaemonChange: subscribe(changeHooks), onDaemonState: subscribe(stateHooks), browser,
});
assert.equal(typeof page.viewList, "function");
await page.viewList();
assert.deepEqual(headers.at(-1), ["agents", "Agents"]);
assert.match(rendered.map(text).join(" "), /Fix the bug/);

// #510: a failed refresh is surfaced as a "may be stale" note with the reason and Retry, not swallowed.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
assert.ok(!appended.some((n) => n.attrs?.class === "stale-note") && !placed.length, "no note while refreshes succeed");
const errors = console.error;
console.error = () => {};
failSessions = new Error("HTTP 502");
assert.equal(changeHooks.length, 1, "one subscription to session events");
changeHooks[0]();
await sleep(350);
assert.equal(placed.length, 1, "the first failed refresh places the note");
const stale = placed[0].n;
assert.equal(stale.attrs.class, "stale-note");
assert.equal(placed[0].after.attrs["aria-label"], "Filter sessions by machine", "it sits under the machine filter");
const [textEl, whyEl] = stale.kids[0].kids;
assert.equal(stale.hidden, false, "a failed refresh shows the note");
assert.match(textEl.textContent, /^List may be stale · last updated \d+ s ago$/);
assert.equal(whyEl.textContent, "Couldn't refresh: HTTP 502");
// Retry runs the refresh now; success hides the note again.
failSessions = null;
stale.kids[1].attrs.onclick();
await sleep(10);
assert.equal(stale.hidden, true, "a successful retry hides the note");
// The stream dropping and coming back refreshes the list (events missed while down are not replayed).
failSessions = new Error("offline");
stateHooks[0]("reconnecting");
await sleep(350);
assert.equal(stale.hidden, false, "a dropped stream triggers a refresh, which fails and says so");
failSessions = null;
stateHooks[0]("live");
changeHooks[0]();  // the shared stream reports each return to live as a change
await sleep(350);
assert.equal(stale.hidden, true, "back to live refreshes the list");
// A refresh that fails after the page has left changes nothing and starts no tick (review on #536).
changeHooks[0]();
for (const fn of left) fn();
assert.equal(changeHooks.length + stateHooks.length, 0, "leaving unsubscribes from the shared stream");
failSessions = new Error("late");
const realSetInterval = globalThis.setInterval;
let ticks = 0;
globalThis.setInterval = (...a) => { ticks++; return realSetInterval(...a); };
stale.kids[1].attrs.onclick();
await sleep(20);
globalThis.setInterval = realSetInterval;
assert.equal(ticks, 0, "no interval after the page left");
assert.notEqual(whyEl.textContent, "Couldn't refresh: late");
console.error = errors;

// #563: with a split pane the list renders into the pane, puts its title and New task in the pane's header (none for a
// guest), leaves the bar alone, tears down with the pane and marks each row with its session id.
failSessions = null;
const nodes = (n) => (n && typeof n === "object" ? [n, ...(n.kids || []).flatMap(nodes)] : []);
for (const guest of [false, true]) {
  const paneHeaders = [];
  const paneLeft = [];
  let painted = 0;
  const filled = [];
  const appendedTo = [];
  const headerCalls = headers.length;
  let barActions = 0;
  const panePage = mountSessions({
    $app: "APP", h: el, fill: (_t, ...n) => filled.push(...n.flat(Infinity)), append: (t, ...n) => appendedTo.push(t), api,
    setHeader: (...a) => headers.push(a), showListAction() { barActions++; }, onLeave: () => assert.fail("the router's onLeave is not used in a pane"),
    isMember: () => false, isGuest: () => guest, badge: (s) => el("badge", {}, s), reviewBadge: () => el("rb"), REVIEW_LABEL: {}, jobStatusBadge: () => el("jb"),
    onDaemonChange: subscribe(changeHooks), onDaemonState: subscribe(stateHooks), browser,
  });
  const paneBody = el("div");
  await panePage.viewList({ body: paneBody, onLeave: (fn) => paneLeft.push(fn), header: (...a) => paneHeaders.push(a), paint: () => { painted++; } });
  assert.deepEqual(appendedTo, [paneBody], "everything goes into the pane");
  assert.deepEqual(paneHeaders, [["Agents", guest ? null : { href: "#/new", label: "+ New task" }]]);
  assert.equal(headers.length, headerCalls, "the bar's title is left to the detail");
  assert.equal(barActions, 0);
  assert.ok(painted >= 1, "rows are marked after they render");
  const row = filled.flatMap(nodes).find((n) => n.tag === "a" && n.attrs.class === "agent-row");
  assert.equal(row.attrs["data-split-key"], "s1");
  assert.ok(paneLeft.length >= 3, "its stream hooks and timers tear down with the pane");
  for (const fn of paneLeft) fn();
  assert.equal(changeHooks.length + stateHooks.length, 0);
}

// Shown search results refresh with the list (review on #578): a rename announced while a query is showing re-runs it, so
// the pane beside the session shows the new title.
{
  const searches = [];
  let title = "Fix the bug";
  const searchApi = async (path) => {
    if (!path.startsWith("/search")) return api(path);
    searches.push(path);
    return { mode: "all", results: [{ id: "s1", title, status: "done", project: "scratch", created_at: "2026-10-10T12:00:00Z", hits: 1, passages: [] }] };
  };
  let box = null;
  let resultsBox = null;
  let gate = null;  // holds /sessions open, for a refresh still in flight when the page leaves
  const lel = (tag, attrs, ...kids) => {
    const n = el(tag, attrs, ...kids);
    n.addEventListener = (type, fn) => { n[`on_${type}`] = fn; };
    if (attrs?.type === "search") box = n;
    if (tag === "div" && attrs?.hidden === true && !attrs.class) resultsBox ||= n;  // the results host
    return n;
  };
  const filledResults = [];
  let resultFills = 0;
  const searchLeft = [];
  const windowListeners = {};
  const searchBrowser = { ...browser, window: { addEventListener: (t, fn) => { windowListeners[t] = fn; }, removeEventListener() {} } };
  const gatedApi = async (path) => {
    if (path === "/sessions" && gate) await gate;
    return searchApi(path);
  };
  const searchPage = mountSessions({
    $app: "APP", h: lel, fill: (t, ...n) => { if (t === resultsBox) resultFills++; filledResults.push(...n.flat(Infinity)); }, append() {}, api: gatedApi,
    setHeader() {}, showListAction() {}, onLeave: (fn) => searchLeft.push(fn), isMember: () => false, isGuest: () => false, badge: (st) => el("badge", {}, st), reviewBadge: () => el("rb"), REVIEW_LABEL: {},
    jobStatusBadge: () => el("jb"), onDaemonChange: subscribe(changeHooks), onDaemonState: subscribe(stateHooks), browser: searchBrowser,
  });
  const hook = changeHooks.length;
  await searchPage.viewList();
  box.value = "bug";
  box.on_input();
  await sleep(300);
  assert.equal(searches.length, 1, "typing searches once the debounce settles");
  title = "Renamed";
  changeHooks[hook]();
  await sleep(350);
  assert.equal(searches.length, 2, "a change re-runs the showing query");
  assert.match(filledResults.map(text).join(" "), /Renamed/);
  // The same results again keep their links (a press or focus on one survives the refresh).
  const fills = resultFills;
  changeHooks[hook]();
  await sleep(350);
  assert.equal(searches.length, 3);
  assert.equal(resultFills, fills, "unchanged results are not rebuilt");
  // A press on a result holds refreshes like a press on a row.
  resultsBox.on_pointerdown();
  changeHooks[hook]();
  await sleep(350);
  assert.equal(searches.length, 3, "no refresh while a result is pressed");
  windowListeners.pointerup();
  await sleep(20);
  assert.equal(searches.length, 4, "releasing the press runs the held refresh");
  // A refresh in flight when the page leaves neither searches nor changes the remembered query.
  let open;
  gate = new Promise((r) => { open = r; });
  changeHooks[hook]();
  await sleep(320);  // the refresh has started and waits on /sessions
  for (const fn of searchLeft) fn();
  open();
  await sleep(20);
  assert.equal(searches.length, 4, "a refresh finishing after the page left runs no search");
  gate = null;
  box.value = "";
}
console.log("ok");
