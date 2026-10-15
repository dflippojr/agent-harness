// Imports the shared session chrome helpers under plain Node (no DOM at module top level) and exercises them with stubs (#258 stage k).
import assert from "node:assert/strict";
import { mountSessionUi, sessionMenuItems, SESSION_MENU_LABELS, pageMetrics, scrollPage } from "../public/lib/session-ui.mjs";

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

const make = (isGuest) => mountSessionUi({ h: el, api: async () => ({}), setHeader() {}, toast() {}, isGuest: () => isGuest, onLeave() {}, layoutBar() {}, browser });

const jumps = make(false).bindSessionJumps();
assert.equal(typeof jumps.updateJumps, "function");
assert.equal(jumps.pageHeight(), 2000);

// The overflow menu (#514): entries follow status and taint; guests get none. Opening it, its keys, rename and focus are
// the AH-10 journey and layout.spec.ts in tools/ui-tests.
const TERMINAL = new Set(["done", "failed", "cancelled"]);
assert.deepEqual(sessionMenuItems({ status: "running" }, { guest: false, terminal: TERMINAL }), ["rename", "cancel", "download"]);
assert.deepEqual(sessionMenuItems({ status: "done", taint: [{ origin: "x" }] }, { guest: false, terminal: TERMINAL }), ["rename", "rerun", "clear-taint", "download"]);
assert.deepEqual(sessionMenuItems({ status: "running", taint: [{ origin: "x" }] }, { guest: true, terminal: TERMINAL }), [], "guests get no owner-only entries");
for (const id of ["rename", "cancel", "rerun", "clear-taint", "download"]) assert.ok(SESSION_MENU_LABELS[id], id);

console.log("ok");
