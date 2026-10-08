// UI harness: load app.js in a stub DOM, enter protocol-blocked state, and prove
// hashchange / feature-nav / visibility / online / SW messages cannot dismiss the
// update card or start /api/v1 traffic.
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";
import { El, Emitter, Node, createDocument, fakeEventSource, storage } from "./web_stub_dom.mjs";

const { byId, make, doc, feature } = createDocument();

const loc = {
  href: "http://localhost/",
  origin: "http://localhost",
  hash: "#/",
  protocol: "http:",
  pathname: "/",
  replace(url) { this.hash = String(url); },
};
const fetches = [];
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body,
  text: async () => JSON.stringify(body),
  blob: async () => body,
});

const fakeFetch = async (url) => {
  const href = String(url);
  fetches.push(href);
  if (href.includes("/health") && !href.includes("/api/")) {
    return jsonResp({
      protocols: { admin: { min: 3, max: 4 } },
      update_hint: {},
    });
  }
  return jsonResp({ detail: "upgrade required", error: { code: "client_update_required" } }, 426);
};

class FakeEventSource extends fakeEventSource() {
  constructor(url) { super(url); fetches.push(String(url)); }
}

const win = new Emitter();
Object.assign(win, {
  addEventListener: (...a) => Emitter.prototype.addEventListener.call(win, ...a),
  removeEventListener: (...a) => Emitter.prototype.removeEventListener.call(win, ...a),
  dispatchEvent: (...a) => Emitter.prototype.dispatchEvent.call(win, ...a),
  localStorage: storage(),
  sessionStorage: storage(),
  location: loc,
  navigator: { serviceWorker: undefined, userAgent: "test" },
  history: { back() { loc.hash = "#/"; }, replaceState() {} },
  scrollTo() {},
  confirm: () => false,
  caches: undefined,
  EventSource: FakeEventSource,
  fetch: fakeFetch,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
});

globalThis.window = win;
globalThis.localStorage = win.localStorage;
globalThis.location = loc;
globalThis.fetch = fakeFetch;
const { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } = await import("../harness/web/client.mjs");

const sandbox = createContext({
  window: win,
  document: doc,
  location: loc,
  history: win.history,
  navigator: win.navigator,
  localStorage: win.localStorage,
  sessionStorage: win.sessionStorage,
  EventSource: FakeEventSource,
  fetch: fakeFetch,
  URL,
  AbortController,
  TextDecoder,
  console,
  getComputedStyle: (el) => el.style,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: win.requestAnimationFrame,
  cancelAnimationFrame: win.cancelAnimationFrame,
  confirm: win.confirm,
  agentHarnessWeb,
  WEB_BUILD_ID,
  WEB_PROTOCOL,
  Node,
  Event,
  JSON,
  Date,
  Math,
  Number,
  String,
  Boolean,
  Array,
  Object,
  Set,
  Map,
  Promise,
  Error,
  parseInt,
  encodeURIComponent,
  decodeURIComponent,
  undefined,
});

await runApp(sandbox);

const waitForCard = async () => {
  for (let i = 0; i < 40; i++) {
    if (byId.app.textContent.includes("Reload and update")) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error(`blocking card missing: ${byId.app.textContent}`);
};
await waitForCard();

const apiCalls = () => fetches.filter((u) => /\/api\/v1\b|\/api\/admin\b/.test(u));
if (apiCalls().length) throw new Error(`API traffic during block paint: ${apiCalls()}`);

const before = byId.app.textContent;
loc.hash = "#/profile";
win.dispatchEvent({ type: "hashchange" });
await new Promise((r) => setTimeout(r, 50));

feature.value = "jobs";
feature.dispatchEvent({ type: "change" });
if (loc.hash !== "#/profile" && loc.hash !== "#/jobs") {
  // go() may rewrite the hash; the card must still stay.
}
win.dispatchEvent({ type: "hashchange" });
await new Promise((r) => setTimeout(r, 50));

doc.visibilityState = "visible";
doc.hidden = false;
doc.dispatchEvent({ type: "visibilitychange" });
win.dispatchEvent({ type: "online" });
win.navigator.serviceWorker = {
  addEventListener() {},
  controller: { postMessage() {} },
  getRegistration: async () => ({ active: { postMessage() {} }, update: async () => {} }),
};
win.dispatchEvent({ type: "message", data: "PURGE_SHELL", origin: loc.origin });
await new Promise((r) => setTimeout(r, 50));

if (!byId.app.textContent.includes("Reload and update")) {
  throw new Error(`blocking card left after nav: ${byId.app.textContent}`);
}
if (byId.app.textContent !== before && !byId.app.textContent.includes("Update Agent Harness Web")) {
  throw new Error(`blocking card replaced: ${byId.app.textContent}`);
}
if (apiCalls().length) {
  throw new Error(`API calls while blocked: ${apiCalls().join(", ")}`);
}
if (!fetches.some((u) => u.includes("/health"))) {
  throw new Error("expected compatibility /health poll");
}
console.log("ok");
