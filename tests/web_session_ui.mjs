// Imports the shared session chrome helpers under plain Node (no DOM at module top level) and exercises them with stubs (#258 stage k).
import assert from "node:assert/strict";
import { mountSessionUi, pageMetrics, scrollPage } from "../harness/web/lib/session-ui.mjs";

const made = [];
const el = (tag, attrs, ...kids) => {
  const listeners = {};
  const node = { tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined), hidden: attrs?.hidden || false,
    textContent: kids[0] ?? "", value: attrs?.value ?? "", isConnected: false, listeners, addEventListener: (t, fn) => { listeners[t] = fn; }, remove() {}, replaceWith() {}, focus() {}, select() {} };
  made.push(node);
  return node;
};

const calls = [];
const win = { scrollY: 40, pageYOffset: 0, innerHeight: 500, visualViewport: { height: 450, addEventListener() {}, removeEventListener() {} }, addEventListener() {}, removeEventListener() {},
  scrollTo: (...a) => calls.push(a) };
const body = { scrollTop: 0, scrollHeight: 2000, append() {} };
const docEl = { scrollTop: 0, clientHeight: 480, scrollHeight: 1800 };
const doc = { scrollingElement: docEl, documentElement: docEl, body, addEventListener() {}, removeEventListener() {} };
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

const guestTitle = make(true).sessionTitle({ id: "s1", title: "Hello" }, () => true);
assert.equal(guestTitle.tag, "h2");

const jumps = make(false).bindSessionJumps();
assert.equal(typeof jumps.updateJumps, "function");
assert.equal(jumps.pageHeight(), 2000);

const renamed = [];
const session = { id: "s1", title: "Hello" };
const ownerTitle = mountSessionUi({ h: el, api, setHeader: (...a) => renamed.push(a), toast() {}, isGuest: () => false, onLeave() {}, layoutBar() {}, browser })
  .sessionTitle(session, () => true);
assert.equal(ownerTitle.tag, "button");
ownerTitle.listeners.click();
const input = made.at(-1);
assert.equal(input.tag, "input");
input.value = "  New   name ";
input.listeners.keydown({ key: "Enter", preventDefault() {} });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.deepEqual(requests.slice(-2), [["PATCH", "/sessions/s1"], ["PUT", "/sessions/s1"]]); // a 405 retries once with PUT
assert.equal(session.title, "New name");
assert.deepEqual(renamed.at(-1), ["agents", "New name"]);
console.log("ok");
