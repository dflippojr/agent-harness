// UI harness: navigating to #/chat must paint the page shell (chat-wrap, feed) before the
// /chats/options (or /chats/<id>) fetch resolves, so the route feels instant (#152).
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";
import { El, Emitter, Node, createDocument, storage } from "./web_stub_dom.mjs";

const { byId, make, doc } = createDocument();
doc.addEventListener = () => {};

const loc = {
  href: "http://localhost/#/",
  origin: "http://localhost",
  hash: "#/",
  protocol: "http:",
  pathname: "/",
  replace(url) { this.hash = String(url).startsWith("#") ? String(url) : `#${url}`; },
};
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body,
  text: async () => JSON.stringify(body),
});

// Controls when /chats/options resolves, so the test can assert the shell is already
// painted while this fetch is still pending.
let releaseChatOptions;
const chatOptionsGate = new Promise((r) => { releaseChatOptions = r; });

const fetched = [];
const fakeFetch = async (url) => {
  const href = String(url);
  fetched.push(href);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/chats/options") { await chatOptionsGate; return jsonResp({ backends: [{ name: "local", models: ["local"], efforts: [] }], default_backend: "local" }); }
  if (path.startsWith("/chats")) return jsonResp([]);
  return jsonResp({});
};

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
  innerHeight: 800,
  scrollY: 0,
  caches: undefined,
  EventSource: class { constructor() { this.readyState = 1; } close() {} },
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
  EventSource: win.EventSource,
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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Navigate to #/chat while /chats/options is still pending (gated above).
loc.hash = "#/chat";
loc.href = "http://localhost/#/chat";
win.dispatchEvent({ type: "hashchange" });

// Give the route's synchronous shell-painting code a chance to run, but not the gated fetch.
await sleep(30);

const hasChatWrap = (node) => {
  if (!(node instanceof El)) return false;
  if (node.className && node.className.split(/\s+/).includes("chat-wrap")) return true;
  return (node.childNodes || []).some(hasChatWrap);
};

if (!hasChatWrap(byId.app)) {
  throw new Error("chat shell (.chat-wrap) was not painted before /chats/options resolved");
}
if (byId.title.hidden) {
  throw new Error("header title should be visible (though possibly empty) before data loads");
}

const findByClass = (node, cls) => {
  if (!(node instanceof El)) return null;
  if (node.className && node.className.split(/\s+/).includes(cls)) return node;
  for (const child of node.childNodes || []) {
    const found = findByClass(child, cls);
    if (found) return found;
  }
  return null;
};
const findTag = (node, tag) => {
  if (!(node instanceof El)) return null;
  if (node.tagName === tag) return node;
  for (const child of node.childNodes || []) {
    const found = findTag(child, tag);
    if (found) return found;
  }
  return null;
};

// Clicking a chat-starter button before /chats/options resolves (ui is still null) must be a
// harmless no-op, not throw (#180 review finding: TypeError on ui.input while ui is null).
const starters = findByClass(byId.app, "chat-starters");
if (!starters) throw new Error("chat-starters welcome shell was not painted");
const starterButton = findTag(starters, "BUTTON");
if (!starterButton) throw new Error("no chat-starter button found in the welcome shell");
starterButton.click();

releaseChatOptions();
await sleep(30);

if (!fetched.some((u) => u.includes("/chats/options"))) {
  throw new Error("expected /chats/options to have been requested");
}
if (byId.title.textContent !== "Chat") {
  throw new Error(`expected header title to read "Chat" once options resolved, got "${byId.title.textContent}"`);
}

console.log("ok");
