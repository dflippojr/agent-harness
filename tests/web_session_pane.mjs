// Desktop session pane (#564): at 768 px+ the status strip and the Transcript / Changes / Info control join the bar (one
// header row) and move back below it on a phone; a pending approval keeps focus where it was unless it was in the pane;
// Changes and Info show the pending decision as a one-line bar with Review that follows the app-wide stream.
import assert from "node:assert/strict";
import { El as BaseEl, Node, createDocument, walk } from "./web_stub_dom.mjs";
import { h, fill, append } from "../harness/web/lib/dom.mjs";
import { mountSession } from "../harness/web/pages/session.mjs";

// Real DOM moves: inserting a node takes it out of its old parent.
const detach = (n) => {
  if (!(n instanceof BaseEl) || !n.parentNode) return;
  const siblings = n.parentNode.childNodes;
  const i = siblings.indexOf(n);
  if (i !== -1) siblings.splice(i, 1);
  n.parentNode = null;
};
class El extends BaseEl {
  append(...nodes) { nodes.flat().forEach(detach); super.append(...nodes); }
  prepend(...nodes) { nodes.flat().forEach(detach); super.prepend(...nodes); }
  after(node) { detach(node); super.after(node); }
  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  focus(opts) { focused = this; this.focusOpts = opts; doc.activeElement = this; }
}
let focused = null;
const { byId, doc } = createDocument({ ElClass: El });
doc.createElement = (tag) => new El(tag);
doc.activeElement = doc.body;
globalThis.document = doc;
globalThis.Node = Node;

// A matchMedia stand-in whose width the test sets.
const media = { matches: true, listeners: new Set() };
const query = {
  get matches() { return media.matches; },
  addEventListener: (_t, fn) => media.listeners.add(fn),
  removeEventListener: (_t, fn) => media.listeners.delete(fn),
};
const resize = (desktop) => { media.matches = desktop; for (const fn of [...media.listeners]) fn(); };

const approval = { id: "ap1", tool: "write_file", tool_call_id: "c1", reason: "Edit outside the project allowlist",
  detail: "@@ -1 +1 @@\n-a\n+b", args: { path: "sw.js" }, smart: { recommendation: "approve", confidence: 0.92, reason: "routine" } };
// The server's state. Like the manager's summary, GET /sessions/<id> lists pending approvals only while the status is
// waiting_approval; the approvals route lists them whatever the status.
let server = { status: "waiting_approval", pending: [approval] };
const summary = () => ({ id: "s1", title: "Fix the bug", status: server.status, project: "p", target: "tower", backend: "claude", model: "m",
  totals: {}, context_used: 10, context_limit: 100, ...(server.status === "waiting_approval" ? { pending_approvals: server.pending } : {}) });
let deferApprovals = null;  // when set, the approvals route answers with a promise the test resolves
const api = async (path) => {
  if (path === "/sessions/s1") return summary();
  if (path === "/sessions/s1/approvals") return deferApprovals ? new Promise((resolve) => deferApprovals.push(resolve)) : [...server.pending];
  if (path === "/sessions/s1/changes") return { removed: true };
  return [];
};
let leaves = [];
const daemonListeners = new Set();
const streams = [];
// Cancellable timers: run() fires every pending one at once and returns when they all finish.
const timers = new Map();
let timerId = 0;
const run = () => { const fns = [...timers.values()]; timers.clear(); return Promise.all(fns.map((fn) => fn())); };
const settle = () => new Promise((resolve) => setImmediate(resolve));
const browser = {
  window: { addEventListener() {}, removeEventListener() {}, scrollTo() {}, innerHeight: 800, matchMedia: () => query },
  document: doc, location: {}, requestAnimationFrame() {},
  setInterval: () => 0, clearInterval() {}, setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; }, clearTimeout: (id) => { timers.delete(id); },
};
const page = mountSession({
  $app: byId.app, h, fill, append, api, setHeader() {}, toast() {}, go() {}, route() {}, validId: () => true,
  isGuest: () => false, isMember: () => false, isOwner: () => true, onLeave: (fn) => leaves.push(fn),
  badge: (s) => h("span", { class: "badge" }, s), reviewBadge: () => h("span"), progressBar: () => h("span", { class: "progress" }),
  openStream: (_url, handlers) => { streams.push(handlers); return () => {}; },
  layoutBar() {}, viewInfo: () => append(byId.app, h("section", { class: "card" }, "Info")), downloadDaemonFile() {},
  TERMINAL: new Set(["done", "failed", "cancelled"]), agentHarnessWeb: { token: "t", url: (p) => p, sessionStreamUrl: () => "/events" },
  browser, onDaemonChange: (fn) => { daemonListeners.add(fn); return () => daemonListeners.delete(fn); },
});
const leave = () => { const fns = leaves; leaves = []; fns.forEach((fn) => fn()); fill(byId.app); };
const bar = byId.bar;
const classes = (parent) => parent.childNodes.filter((n) => n instanceof El).map((n) => n.id || n.className);

// ---------- one header row ----------
await page.viewSession("s1", "transcript");
assert.ok(doc.body.classList.contains("session-page"), "the body says a session is open, for the desktop rules");
assert.deepEqual(classes(bar), ["back", "title", "session-strip", "tabs session-tabs", "conn", "settings-btn", "session-menu-wrap"],
  "desktop: the strip sits under the title's slot and the segmented control before ⋯, all in the bar");
const chrome = walk(byId.app, (n) => n.className === "session-chrome")[0];
assert.deepEqual(classes(chrome), ["conn-strip"], "only the reconnecting strip stays under the bar");
resize(false);
assert.deepEqual(classes(chrome), ["session-strip", "tabs session-tabs", "conn-strip"], "a phone keeps the strip and tabs under the bar");
assert.deepEqual(classes(bar), ["back", "title", "conn", "settings-btn", "session-menu-wrap"]);
resize(true);
assert.deepEqual(classes(bar).slice(1, 4), ["title", "session-strip", "tabs session-tabs"], "widening moves them back into the bar");

// ---------- the docked approval ----------
// Focus outside the pane (the list beside it): an arriving approval leaves it there.
const elsewhere = new El("a");
doc.activeElement = elsewhere;
focused = null;
streams[0].approval_requested({ seq: 1, data: approval });
const sheet = doc.body.childNodes.filter((n) => n instanceof El && n.className === "approval-sheet").at(-1);
assert.ok(sheet, "the approval sheet is on the body");
assert.equal(focused, null, "focus never jumps to the card, let alone to Approve");
for (const part of ["approval-head", "approval-what", "smart-rec", "approval-diff", "approval-tool", "approval-note-toggle", "approval-actions"]) {
  assert.ok(walk(sheet, (n) => n.classList.contains(part)).length, `the card has its ${part} part for the desktop layout`);
}
const heading = walk(sheet, (n) => n.tagName === "H4")[0];
assert.equal(heading.attributes.tabindex, "-1", "the heading can take focus without joining the tab order");
// Focus in the transcript: a newer approval moves it to the card's heading, without scrolling.
const inTranscript = new El("button");
byId.app.append(inTranscript);
doc.activeElement = inTranscript;
streams[0].approval_requested({ seq: 2, data: { ...approval, id: "ap2" } });
const sheet2 = doc.body.childNodes.filter((n) => n instanceof El && n.className === "approval-sheet").at(-1);
assert.notEqual(sheet2, sheet);
assert.equal(focused, walk(sheet2, (n) => n.tagName === "H4")[0], "focus lands on the heading, not on Approve");
assert.deepEqual(focused.focusOpts, { preventScroll: true });
// Focus in the superseded card follows to the new one too, since that card is removed.
const deny = walk(sheet2, (n) => n.classList.contains("approval-deny"))[0];
doc.activeElement = deny;
focused = null;
streams[0].approval_requested({ seq: 3, data: { ...approval, id: "ap3" } });
assert.equal(focused?.tagName, "H4");
leave();
assert.ok(!doc.body.classList.contains("session-page"), "leaving drops the body class");
assert.deepEqual(classes(bar), ["back", "title", "conn", "settings-btn"], "leaving takes the strip, the control and ⋯ out of the shared bar");
assert.equal(media.listeners.size, 0, "the width listener goes with the page");

// ---------- the approval bar on Changes and Info ----------
const approvalBars = () => doc.body.childNodes.filter((n) => n instanceof El && n.className === "approval-bar");
const event = () => [...daemonListeners][0]();
timers.clear();
for (const tab of ["changes", "info"]) {
  // The first fetch answers "running", but an approval arrives before the stream listener is attached.
  server = { status: "running", pending: [] };
  const viewing = page.viewSession("s1", tab);
  server = { status: "waiting_approval", pending: [approval] };
  await viewing;
  const [abar] = approvalBars();
  assert.equal(approvalBars().length, 1, `${tab}: one approval bar`);
  assert.equal(abar.hidden, true, "the first fetch saw nothing pending");
  assert.equal(timers.size, 1, `${tab}: the bar catches up once after subscribing`);
  await run();
  assert.equal(abar.hidden, false);
  assert.equal(abar.attributes.role, "status");
  assert.match(abar.textContent, /Approval needed · Edit outside the project allowlist/);
  const review = walk(abar, (n) => n.tagName === "A")[0];
  assert.equal(review.textContent, "Review");
  assert.equal(review.attributes.href, "#/s/s1/approval/ap1", "Review opens the transcript on that approval");
  assert.equal(daemonListeners.size, 1, "the bar follows the app-wide stream");
  // A burst of events is one refresh.
  event();
  event();
  event();
  assert.equal(timers.size, 1, "refreshes are debounced");
  await run();
  // An unchanged approval keeps the same Review link, so one a keyboard user has focused survives the refresh.
  assert.equal(walk(abar, (n) => n.tagName === "A")[0], review, "a refresh updates the bar in place");
  // A pending approval outlives waiting_approval (a session resumed as waiting_target): the summary drops it, the
  // approvals route still has it.
  server = { status: "waiting_target", pending: [approval] };
  event();
  await run();
  assert.equal(abar.hidden, false, "the bar follows the approvals route, not only the status");
  // Answers that arrive out of order: only the newest refresh paints.
  deferApprovals = [];
  event();
  const older = run();
  await settle();
  event();
  const newer = run();
  await settle();
  assert.equal(deferApprovals.length, 2);
  deferApprovals[1]([approval]);
  await newer;
  deferApprovals[0]([]);
  await older;
  assert.equal(abar.hidden, false, "a slower, older answer does not hide a still-pending approval");
  deferApprovals = null;
  // Decided elsewhere: the next event refetches and the bar goes away.
  server = { status: "running", pending: [] };
  event();
  await run();
  assert.equal(abar.hidden, true, `${tab}: the bar hides once nothing is pending`);
  // Leaving cancels a refresh still waiting on its debounce.
  event();
  assert.equal(timers.size, 1);
  leave();
  assert.equal(timers.size, 0, "leaving cancels the pending refresh");
  assert.equal(daemonListeners.size, 0, "leaving stops listening");
  assert.ok(abar.removed, "leaving removes the bar");
}
// A finished session shows no bar, even if a stale row is still listed.
server = { status: "done", pending: [approval] };
await page.viewSession("s1", "changes");
await run();
assert.equal(approvalBars().length, 1);
assert.equal(approvalBars()[0].hidden, true);
leave();
assert.equal(timers.size, 0);
console.log("ok");
