// Session-page VM: mask compaction must not crash summary notes or ctxUsed (#248).
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";
import { El as BaseEl, Emitter, Node, createDocument, fakeEventSource, storage, walk } from "./web_stub_dom.mjs";

const fail = (msg) => { throw new Error(msg); };

class El extends BaseEl {
  constructor(tag, attrs) {
    super(tag, attrs);
    this.scrollTop = 0;
    this.scrollHeight = 1200;
    this.clientHeight = 800;
  }
}

const { byId, make, doc, feature } = createDocument({ ElClass: El, features: ["agents", "chat", "jobs", "images"] });
doc.documentElement.clientHeight = 800;
doc.documentElement.scrollTop = 0;
doc.body = make("body");
doc.scrollingElement = doc.documentElement;
doc.querySelector = (sel) => {
  if (sel === 'link[rel="apple-touch-icon"]' || sel === 'link[rel="icon"]') return new El("link");
  if (sel === "#app") return byId.app;
  if (sel.startsWith(".")) {
    const cls = sel.slice(1);
    return walk(doc.body, (n) => n.classList.contains(cls))[0]
      || walk(byId.app, (n) => n.classList.contains(cls))[0]
      || null;
  }
  return null;
};
doc.querySelectorAll = (sel) => {
  if (sel === ".jump") return walk(doc.body, (n) => n.classList.contains("jump"));
  if (sel === "input, textarea, select") return [feature];
  if (sel.startsWith(".")) {
    const cls = sel.slice(1);
    return [...walk(doc.body, (n) => n.classList.contains(cls)), ...walk(byId.app, (n) => n.classList.contains(cls))];
  }
  return [];
};

const loc = {
  href: "http://localhost/#/",
  origin: "http://localhost",
  hash: "#/",
  protocol: "http:",
  pathname: "/",
  replace(url) {
    const next = String(url).startsWith("#") ? String(url) : `#${url}`;
    if (next === this.hash) return;
    this.hash = next;
    this.href = `http://localhost/${next}`;
  },
};
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body, text: async () => JSON.stringify(body),
});

const sessionDetail = {
  id: "sess1", title: "Demo session", status: "running", project: "scratch", target: "tower",
  backend: "local", model: "local", totals: { prompt_tokens: 100, completion_tokens: 20 },
  context_used: 9000, context_limit: 10000, created_at: 1, updated_at: 2, workspace: "/tmp",
};

const fakeFetch = async (url) => {
  const href = String(url);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/sessions" || path.startsWith("/sessions?")) return jsonResp([]);
  if (path === "/sessions/sess1") return jsonResp(sessionDetail);
  if (path === "/queue") return jsonResp([]);
  if (path === "/projects") return jsonResp([{ name: "scratch", target: "tower" }]);
  return jsonResp({});
};

const sources = [];
const FakeEventSource = fakeEventSource(sources);

const win = new Emitter();
Object.assign(win, {
  addEventListener: (...a) => Emitter.prototype.addEventListener.call(win, ...a),
  removeEventListener: (...a) => Emitter.prototype.removeEventListener.call(win, ...a),
  dispatchEvent: (...a) => Emitter.prototype.dispatchEvent.call(win, ...a),
  localStorage: storage(), sessionStorage: storage(), location: loc,
  navigator: { serviceWorker: undefined, userAgent: "test" },
  history: { back() {}, replaceState() {} },
  scrollTo() {}, confirm: () => false, innerHeight: 800, scrollY: 0, pageYOffset: 0,
  caches: undefined, EventSource: FakeEventSource, fetch: fakeFetch,
  requestAnimationFrame: (fn) => setTimeout(fn, 0), cancelAnimationFrame: (id) => clearTimeout(id),
});
globalThis.window = win;
globalThis.localStorage = win.localStorage;
globalThis.location = loc;
globalThis.fetch = fakeFetch;
const { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } = await import("../harness/web/client.mjs");

const sandbox = createContext({
  window: win, document: doc, location: loc, history: win.history, navigator: win.navigator,
  localStorage: win.localStorage, sessionStorage: win.sessionStorage, EventSource: FakeEventSource, fetch: fakeFetch,
  URL, AbortController, TextDecoder, console, getComputedStyle: (el) => el.style, setTimeout, clearTimeout,
  setInterval, clearInterval, requestAnimationFrame: win.requestAnimationFrame,
  cancelAnimationFrame: win.cancelAnimationFrame, confirm: win.confirm,
  agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL, Node, Event, JSON, Date, Math, Number, String, Boolean, Array, Object,
  Set, Map, Promise, Error, parseInt, encodeURIComponent, decodeURIComponent, undefined,
});
await runApp(sandbox);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = async (pred, label, ms = 2000) => {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (pred()) return;
    await sleep(20);
  }
  fail(`timeout waiting for ${label}: ${byId.app.textContent.slice(0, 400)}`);
};

const MASK = "Replaced old tool outputs with recoverable receipts (~4K tokens saved)";
const FAILED = "the summary failed";
const ELIDE = "Trimmed old tool output: ~8K → ~5K tokens";
const SUMMARY = "Context condensed: ~8K → ~3K tokens (12 messages summarized)";

const sessionStream = () => sources.filter((s) => s.url.includes("/sessions/sess1/events") && s.readyState === 1).pop();

const openSession = async () => {
  loc.hash = "#/agents";
  loc.href = "http://localhost/#/agents";
  win.dispatchEvent({ type: "hashchange" });
  await sleep(40);
  loc.hash = "#/s/sess1";
  loc.href = "http://localhost/#/s/sess1";
  win.dispatchEvent({ type: "hashchange" });
  await waitFor(() => /Demo session/.test(byId.app.textContent) && sessionStream(), "session transcript");
  return sessionStream();
};

const text = () => byId.app.textContent;

// mask with no summary note showing: a note appears, ctx meter stays at 90%, no failed-summary copy.
let es = await openSession();
if (!text().includes("90% context")) fail(`expected 90% context before mask: ${text()}`);
es.emit("compaction", { tier: "mask", tokens_saved: 4200, characters_saved: 12600 }, 1);
await sleep(20);
if (!text().includes(MASK)) fail(`mask with no note: ${text()}`);
if (text().includes(FAILED) || text().includes("~0 → ~0")) fail(`mask with no note leaked summary copy: ${text()}`);
if (!text().includes("90% context")) fail(`mask must not change ctxUsed: ${text()}`);
if (text().includes("Trimmed old tool output")) fail(`mask must not use the elide label: ${text()}`);

// mask while a summary note is showing: add the mask line, leave the summary progress alone.
es = await openSession();
es.emit("compaction_started", { messages: 12, tokens_before: 8000 }, 1);
await sleep(20);
if (!text().includes("Condensing older context")) fail(`expected summary progress: ${text()}`);
const beforeMask = text();
es.emit("compaction", { tier: "mask", tokens_saved: 4200, characters_saved: 12600 }, 2);
await sleep(20);
if (!text().includes(MASK)) fail(`mask during summary: ${text()}`);
if (!text().includes("Condensing older context")) fail(`mask must not finish the summary note: ${text()}`);
if (text().includes(FAILED)) fail(`mask must not mark the summary as failed: ${text()}`);
if (!text().includes("90% context")) fail(`mask during summary must not change ctxUsed: ${text()}`);
if (!beforeMask.includes("Condensing older context")) fail("precondition");

// mask then summary (the live _maybe_compact order): summary note still completes.
es = await openSession();
es.emit("compaction", { tier: "mask", tokens_saved: 4200, characters_saved: 12600 }, 1);
await sleep(20);
es.emit("compaction_started", { messages: 12, tokens_before: 8000 }, 2);
await sleep(20);
if (!text().includes(MASK) || !text().includes("Condensing older context")) fail(`mask then start: ${text()}`);
es.emit("compaction", {
  tier: "summary", tokens_before: 8000, tokens_after: 3000, summarized_messages: 12, summary: "handoff",
}, 3);
await sleep(20);
if (!text().includes(MASK)) fail(`mask note lost after summary: ${text()}`);
if (!text().includes(SUMMARY)) fail(`summary did not complete: ${text()}`);
if (text().includes(FAILED) || text().includes("Condensing older context")) fail(`summary still in progress: ${text()}`);
if (!text().includes("30% context")) fail(`summary should update ctxUsed: ${text()}`);
if (!text().includes("Show summary") && !text().includes("handoff")) fail(`summary body missing: ${text()}`);

// summary rendering without a preceding mask stays the same.
es = await openSession();
es.emit("compaction_started", { messages: 12, tokens_before: 8000 }, 1);
await sleep(20);
es.emit("compaction", {
  tier: "summary", tokens_before: 8000, tokens_after: 3000, summarized_messages: 12, summary: "handoff",
}, 2);
await sleep(20);
if (!text().includes(SUMMARY)) fail(`summary-only: ${text()}`);
if (text().includes(MASK) || text().includes(FAILED)) fail(`summary-only leaked mask copy: ${text()}`);
if (!text().includes("30% context")) fail(`summary-only ctxUsed: ${text()}`);

// elide rendering without a compact note stays the same.
es = await openSession();
es.emit("compaction", { tier: "elide", tokens_before: 8000, tokens_after: 5000 }, 1);
await sleep(20);
if (!text().includes(ELIDE)) fail(`elide: ${text()}`);
if (text().includes(MASK) || text().includes(FAILED) || text().includes("Context condensed")) fail(`elide leaked other copy: ${text()}`);
if (!text().includes("50% context")) fail(`elide should update ctxUsed: ${text()}`);

console.log("ok");
process.exit(0);
