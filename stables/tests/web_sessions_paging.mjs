// Agents list paging races (#677): refreshes that read every shown page again, a page of rows already shown, a late page
// after the machine switch. The first page, "Show older" and the machine switch are AH-16 in tools/ui-tests.
import assert from "node:assert/strict";
import { mountSessions } from "../public/pages/sessions.mjs";
import { fakeClock } from "./web_stub_dom.mjs";

// Debounces and refresh timers run on a manual clock: sleep(ms) advances it, so nothing waits in real time.
const clock = fakeClock();

const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
  value: "", hidden: !!attrs?.hidden, dataset: {}, addEventListener() {}, querySelector: () => null, after() {} });
const nodes = (n) => (n && typeof n === "object" ? [n, ...(n.kids || []).flatMap(nodes)] : []);
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));
const browser = {
  window: { addEventListener() {}, removeEventListener() {} },
  document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
};
const now = Date.now() / 1000;
const row = (id, status, age, target = "tower") => ({ id, title: `Task ${id}`, status, target, project: "scratch",
  created_at: now - age, updated_at: now - age });
// The first page as the server sends it: the newest finished sessions, then an old one that waits on an approval.
const firstPage = [row("new1", "done", 10), row("new2", "done", 20), row("old-wait", "waiting_approval", 9000)];
const olderPage = [row("old1", "done", 5000), row("old2", "done", 6000)];
// The server's pages by request path: [rows, X-Next-Before].
let server = {
  "/sessions": [firstPage, "c-new2"],
  "/sessions?before=c-new2": [olderPage, null],
  "/sessions?target=macbook": [[row("mac1", "done", 30, "macbook")], null],
};
const calls = [];
const surfaces = new Set();
const api = async (path, { onHeaders, surface } = {}) => {
  calls.push(path);
  if (path.startsWith("/sessions")) surfaces.add(surface);
  const headers = new Map();
  let body = [];
  if (gates[path]) await (typeof gates[path] === "function" ? gates[path]() : gates[path]);
  if (server[path]) {
    body = server[path][0];
    if (server[path][1]) headers.set("X-Next-Before", server[path][1]);
  } else if (path === "/projects") body = [{ name: "scratch", target: "tower" }, { name: "macproj", target: "macbook" }];
  else if (path === "/gpu") body = { manual: false, state: "clear" };
  onHeaders?.({ get: (name) => headers.get(name) ?? null });
  return body;
};
const gates = {};  // path -> a promise the request waits on (and fails with, if it rejects)
let painted = [];
let changed = null;
const left = [];
let switchKids = [];
const page = mountSessions({
  $app: "APP", h: el, api, ownerSurface: () => "admin", append() {}, setHeader() {}, showListAction() {}, onLeave: (fn) => left.push(fn),
  fill: (target, ...n) => {
    if (target?.attrs?.role === "group") switchKids = n.flat(Infinity);
    else if (target?.attrs?.class === "agent-groups") painted = n.flat(Infinity);
  },
  isMember: () => false, isGuest: () => false, badge: (s) => el("badge", {}, s), reviewBadge: () => el("rb"), REVIEW_LABEL: {},
  jobStatusBadge: () => el("jb"), onDaemonChange: (fn) => { changed = fn; return () => {}; }, onDaemonState: () => () => {}, browser,
});
await page.viewList();
const sleep = (ms) => clock.advance(ms);
const section = (label) => painted.flatMap(nodes).find((n) => n.tag === "section" && n.attrs["aria-label"] === label);
const rowIds = (n) => nodes(n).filter((x) => x.attrs?.class === "agent-row").map((x) => x.attrs["data-split-key"]);

// Setup: one older page is shown, so each refresh below has to read it again.
const more = painted.flatMap(nodes).find((n) => n.tag === "button" && text(n) === "Show older");
await more.attrs.onclick({ currentTarget: more });

// Review on #677: each refresh reads every shown page again, so an older row's status is current and a row pushed off
// the first page by a new session is still shown.
const showOlderButton = () => painted.flatMap(nodes).find((n) => n.tag === "button" && n.attrs.class?.includes("agent-more"));
server = {
  ...server,
  "/sessions": [[row("new0", "done", 5), row("new1", "done", 10), row("old-wait", "waiting_approval", 9000)], "c-new1"],
  "/sessions?before=c-new1": [[row("new2", "done", 20), { ...row("old1", "running", 5000) }], "c-old1"],
  "/sessions?before=c-old1": [[row("old2", "done", 6000)], null],
};
changed();
await sleep(350);
assert.deepEqual(rowIds(section("Running")), ["old1"], "a shown older row takes its current status");
assert.deepEqual(rowIds(section("Recent")), ["new0", "new1", "new2"], "the row pushed to the next page stays");
assert.equal(text(showOlderButton()), "Show older", "and the rest is one click away");
// A page holding only rows already shown, with more after it, still leaves a working button (not "Loading…").
server = {
  ...server,
  "/sessions": [firstPage, "c-new2"],
  "/sessions?before=c-new2": [[row("old-wait", "waiting_approval", 9000)], "c-wait"],
  "/sessions?before=c-wait": [olderPage, null],
};
changed();
await sleep(350);
// The one older page shown so far holds only that waiting row, already on the first page.
const after = showOlderButton();
assert.ok(after && !after.disabled && text(after) === "Show older", "the button is ready for the next page");
await after.attrs.onclick({ currentTarget: after });
assert.deepEqual(rowIds(section("Recent")), ["new1", "new2", "old1", "old2"]);

const mac = switchKids.find((n) => text(n) === "MacBook");
mac.attrs.onclick();
await sleep(20);
assert.ok(calls.includes("/sessions?target=macbook"), "the machine switch filters on the server");
assert.deepEqual(rowIds(section("Recent")), ["mac1"], "and replaces the pages shown so far");
// Review on #677: clicking into a page that holds only an active row already shown repaints a working button.
switchKids.find((n) => text(n) === "All").attrs.onclick();
await sleep(20);
const fresh = showOlderButton();
assert.equal(text(fresh), "Show older");
await fresh.attrs.onclick({ currentTarget: fresh });
const next = showOlderButton();
assert.ok(next !== fresh && !next.disabled && text(next) === "Show older", "not left disabled on Loading…");
assert.deepEqual(rowIds(section("Recent")), ["new1", "new2"]);
// Review on #677: an older page that fails after a machine switch does not take back a click of the new machine's list.
let failOlder;
gates["/sessions?before=c-wait"] = new Promise((_, reject) => { failOlder = reject; });
const errors = console.error;
console.error = () => {};
server["/sessions?target=macbook"] = [[row("mac2", "done", 30, "macbook")], "c-mac"];
server["/sessions?target=macbook&before=c-mac"] = [[row("mac1", "done", 40, "macbook")], null];
const pending = next.attrs.onclick({ currentTarget: next });
await sleep(10);
switchKids.find((n) => text(n) === "MacBook").attrs.onclick();
await sleep(20);
failOlder(new Error("HTTP 502"));
await pending;
console.error = errors;
delete gates["/sessions?before=c-wait"];
const count = calls.length;
const first = showOlderButton();
await first.attrs.onclick({ currentTarget: first });
assert.ok(calls.slice(count).includes("/sessions?target=macbook&before=c-mac"), "the MacBook list still pages");
assert.deepEqual(rowIds(section("Recent")), ["mac2", "mac1"]);
// Review on #677, again: the button follows from the pages wanted and shown, so no late answer can undo a newer one.
switchKids.find((n) => text(n) === "All").attrs.onclick();
await sleep(20);
console.error = () => {};
// (1) A daemon refresh loads the asked-for page before the click's own request fails: the page stays.
let failFirst;
let asked = 0;
gates["/sessions?before=c-new2"] = () => (asked++ === 0 ? new Promise((_, reject) => { failFirst = reject; }) : Promise.resolve());
const clicked = showOlderButton().attrs.onclick();
assert.equal(text(showOlderButton()), "Loading…");
assert.equal(showOlderButton().attrs.disabled, true);
changed();
await sleep(350);
failFirst(new Error("late"));
await clicked;
assert.equal(text(showOlderButton()), "Show older", "the late failure changes nothing");
delete gates["/sessions?before=c-new2"];
let mark = calls.length;
changed();
await sleep(350);
assert.ok(calls.slice(mark).includes("/sessions?before=c-new2"), "later refreshes still read the older page");
// (2) A refresh that fails while Show older's page is on its way offers Retry instead of leaving "Loading…".
let releaseOlder;
gates["/sessions?before=c-wait"] = new Promise((resolve) => { releaseOlder = resolve; });
const held = showOlderButton().attrs.onclick();
gates["/sessions"] = () => Promise.reject(new Error("HTTP 502"));
changed();
await sleep(350);
assert.equal(text(showOlderButton()), "Couldn't load older sessions. Retry");
assert.equal(showOlderButton().attrs.disabled, false);
delete gates["/sessions"];
releaseOlder();
await held;
delete gates["/sessions?before=c-wait"];
await showOlderButton().attrs.onclick();
assert.deepEqual(rowIds(section("Recent")), ["new1", "new2", "old1", "old2"], "Retry loads the page");
// (3) A machine switch clears the old machine's rows at once, even when the new machine's list fails to load.
gates["/sessions?target=macbook"] = () => Promise.reject(new Error("HTTP 502"));
switchKids.find((n) => text(n) === "MacBook").attrs.onclick();
await sleep(20);
assert.equal(text(painted[0]), "Loading…");
assert.deepEqual(painted.flatMap(nodes).filter((n) => n.attrs?.class === "agent-row"), []);
delete gates["/sessions?target=macbook"];
console.error = errors;
assert.deepEqual([...surfaces], ["admin"], "every page comes from the owner route, which holds the active sessions");
for (const fn of left) fn();  // stops the stale note's ticking, which the failures above started
console.log("ok");
