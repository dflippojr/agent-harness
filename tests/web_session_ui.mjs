// Imports the shared session chrome helpers under plain Node (no DOM at module top level) and exercises them with stubs (#258 stage k).
import assert from "node:assert/strict";
import { mountSessionUi, sessionMenuItems, SESSION_MENU_LABELS, pageMetrics, scrollPage } from "../harness/web/lib/session-ui.mjs";

const made = [];
const el = (tag, attrs, ...kids) => {
  const listeners = {};
  const node = { tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined), hidden: attrs?.hidden || false,
    textContent: kids[0] ?? "", value: attrs?.value ?? "", isConnected: false, listeners, addEventListener: (t, fn) => { listeners[t] = fn; },
    removed: false, remove() { node.removed = true; }, replaceWith() {}, focus() {}, select() {},
    setAttribute: (k, v) => { node.attrs[k] = v; }, replaceChildren: (...n) => { node.kids = n; }, querySelector: () => node.kids[0] || null,
    contains: (t) => t === node || node.kids.some((k) => k.contains?.(t)), after: (n) => { node.next = n; }, append: (...n) => node.kids.push(...n) };
  if (attrs?.onclick) listeners.click = attrs.onclick;
  made.push(node);
  return node;
};

const calls = [];
const win = { scrollY: 40, pageYOffset: 0, innerHeight: 500, visualViewport: { height: 450, addEventListener() {}, removeEventListener() {} }, addEventListener() {}, removeEventListener() {},
  scrollTo: (...a) => calls.push(a) };
const body = { scrollTop: 0, scrollHeight: 2000, append() {} };
const docEl = { scrollTop: 0, clientHeight: 480, scrollHeight: 1800 };
const docListeners = {};
const bar = el("header", { id: "bar" });
const title = el("h1", { id: "title" }, "Hello");
const doc = { scrollingElement: docEl, documentElement: docEl, body, addEventListener: (t, fn) => { docListeners[t] = fn; },
  removeEventListener: (t) => { delete docListeners[t]; }, getElementById: (id) => ({ bar, title })[id] || null, querySelector: () => null };
const browser = { window: win, document: doc, requestAnimationFrame: (fn) => fn() };

assert.deepEqual(pageMetrics(browser), { y: 40, viewH: 500, pageH: 2000 });
scrollPage(-5, browser);
assert.deepEqual(calls.at(-1), [0, 0]);
scrollPage(300, browser);
assert.equal(docEl.scrollTop, 300);
assert.equal(body.scrollTop, 300);

const requests = [];
const api = async (path, opts) => {
  requests.push([opts.method, path]);
  if (opts.method === "PATCH") throw new Error("405 Method Not Allowed");
  return { title: opts.body.title };
};
const make = (isGuest) => mountSessionUi({ h: el, api, setHeader() {}, toast() {}, isGuest: () => isGuest, onLeave() {}, layoutBar() {}, browser });

const jumps = make(false).bindSessionJumps();
assert.equal(typeof jumps.updateJumps, "function");
assert.equal(jumps.pageHeight(), 2000);

// The overflow menu (#514): entries follow status and taint; guests get none.
const TERMINAL = new Set(["done", "failed", "cancelled"]);
assert.deepEqual(sessionMenuItems({ status: "running" }, { guest: false, terminal: TERMINAL }), ["rename", "cancel", "download"]);
assert.deepEqual(sessionMenuItems({ status: "done", taint: [{ origin: "x" }] }, { guest: false, terminal: TERMINAL }), ["rename", "rerun", "clear-taint", "download"]);
assert.deepEqual(sessionMenuItems({ status: "running", taint: [{ origin: "x" }] }, { guest: true, terminal: TERMINAL }), [], "guests get no owner-only entries");
for (const id of ["rename", "cancel", "rerun", "clear-taint", "download"]) assert.ok(SESSION_MENU_LABELS[id], id);

// Rename lives in the menu now: picking it swaps the header title for an input; the title itself is not a button.
const renamed = [];
const leaves = [];
const session = { id: "s1", title: "Hello" };
const ui = mountSessionUi({ h: el, api, setHeader: (...a) => renamed.push(a), toast() {}, isGuest: () => false, onLeave: (fn) => leaves.push(fn), layoutBar() {}, browser });
assert.equal(ui.sessionTitle, undefined, "tapping the title no longer starts a rename");
const picked = [];
let status = "running";
const menu = ui.sessionMenu({ items: () => sessionMenuItems({ status }, { guest: false, terminal: TERMINAL }), run: (id) => picked.push(id) });
assert.ok(bar.kids.length === 1 && bar.kids[0].contains(menu.button), "the ⋯ button sits in the header bar");
assert.equal(menu.button.attrs["aria-haspopup"], "menu");
assert.equal(menu.menu.hidden, true);
menu.button.listeners.click();
assert.equal(menu.menu.hidden, false);
assert.equal(menu.button.attrs["aria-expanded"], "true");
assert.deepEqual(menu.menu.kids.map((b) => b.textContent), ["Rename", "Cancel task", "Download transcript"]);
assert.ok(menu.menu.kids.every((b) => b.attrs.role === "menuitem"));
assert.ok(docListeners.pointerdown, "an outside tap closes the open menu");
docListeners.pointerdown({ target: {} });
assert.equal(menu.menu.hidden, true);
assert.equal(docListeners.pointerdown, undefined);
status = "done";
menu.button.listeners.click();
assert.deepEqual(menu.menu.kids.map((b) => b.textContent), ["Rename", "Run again as new session", "Download transcript"], "entries are rebuilt on each open");
menu.menu.listeners.keydown({ key: "Escape", preventDefault() {} });
assert.equal(menu.menu.hidden, true);
menu.button.listeners.click();
menu.menu.kids[0].listeners.click();
assert.deepEqual(picked, ["rename"]);
assert.equal(menu.menu.hidden, true, "picking an entry closes the menu");

ui.renameTitle(session, () => true);
assert.equal(title.hidden, true, "the title hides behind the rename input");
const input = title.next;
assert.equal(input.tag, "input");
assert.equal(input.attrs.class, "session-title-edit");
input.value = "  New   name ";
input.listeners.keydown({ key: "Enter", preventDefault() {} });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.deepEqual(requests.slice(-2), [["PATCH", "/sessions/s1"], ["PUT", "/sessions/s1"]]); // a 405 retries once with PUT
assert.equal(session.title, "New name");
assert.deepEqual(renamed.at(-1), ["agents", "New name", { page: true }]);
assert.equal(title.hidden, false);
assert.ok(input.removed);

leaves.forEach((fn) => fn());
assert.ok(bar.kids[0].removed, "the menu leaves with the page");
console.log("ok");
