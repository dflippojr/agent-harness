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
const streams = [];
const left = [];
const headers = [];
const page = mountSessions({
  $app: "APP", h: el, fill: (_t, ...n) => rendered.push(...n.flat(Infinity)), append: (_t, ...n) => appended.push(...n.flat(Infinity)), api, setHeader: (...a) => headers.push(a), showFab() {}, onLeave: (fn) => left.push(fn),
  isMember: () => false, isGuest: () => false, badge: (s) => el("badge", {}, s), reviewBadge: () => el("rb"), REVIEW_LABEL: {}, jobStatusBadge: () => el("jb"),
  openStream: (_url, handlers, opts) => { streams.push({ handlers, opts }); return () => {}; }, ownerSurface: () => ({}), agentHarnessWeb: { url: (p) => p, token: "" }, browser,
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
streams[0].handlers.session_created({});
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
streams[0].opts.onState("live");
streams[0].opts.onState("reconnecting");
await sleep(350);
assert.equal(stale.hidden, false, "a dropped stream triggers a refresh, which fails and says so");
failSessions = null;
streams[0].opts.onState("live");
await sleep(350);
assert.equal(stale.hidden, true, "back to live refreshes the list");
// A refresh that fails after the page has left changes nothing and starts no tick (review on #536).
streams[0].handlers.session_created({});
for (const fn of left) fn();
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
console.log("ok");
