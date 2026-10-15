// The Changes and Info approval bar (#564) follows the app-wide stream: one debounced refresh per burst, the newest answer
// wins when they arrive out of order, and leaving cancels what is pending. The bar's text, its Review link and the desktop
// header and docked approval are the AH-22 journey in tools/ui-tests.
import assert from "node:assert/strict";
import { El as BaseEl, Node, createDocument, walk } from "./web_stub_dom.mjs";
import { h, fill, append } from "../public/lib/dom.mjs";
import { mountSession } from "../public/pages/session.mjs";

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
  const review = walk(abar, (n) => n.tagName === "A")[0];
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
