// UI harness: New task defaults to Claude during a GPU hold, shows a linked
// notice under Backend for local models, and labels the submit Queue task. (#185)
import { createContext } from "node:vm";
import { runApp } from "./web_app_loader.mjs";
import { El, Emitter, Node, createDocument, fakeEventSource, storage } from "./web_stub_dom.mjs";

const { byId, make, doc } = createDocument();

const historyStack = ["#/"];
const loc = {
  href: "http://localhost/#/",
  origin: "http://localhost",
  _hash: "#/",
  get hash() { return this._hash; },
  set hash(v) {
    const next = !v || v === "#" ? "#/" : String(v);
    if (next === this._hash) return;
    this._hash = next;
    this.href = `http://localhost/${next}`;
    historyStack.push(next);
  },
  protocol: "http:",
  pathname: "/",
  replace(url) {
    const next = String(url).startsWith("#") ? String(url) : `#${url}`;
    if (next === this._hash) return;
    this._hash = next;
    this.href = `http://localhost/${next}`;
    historyStack[historyStack.length - 1] = next;
    this.onHashReplace?.();
  },
};
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body,
  text: async () => JSON.stringify(body),
});

let gpuPayload = { manual: false, state: "clear" };
const fakeFetch = async (url) => {
  const href = String(url);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if (path === "/health" || href.endsWith("/health")) {
    return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  }
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === "/sessions" || path.startsWith("/sessions?")) return jsonResp([]);
  if (path === "/queue") return jsonResp([]);
  if (path === "/gpu") return jsonResp(gpuPayload);
  if (path === "/projects") return jsonResp([{ name: "scratch", target: "tower" }]);
  if (path === "/models") return jsonResp([{ name: "Qwen", default: true, context_tokens: 32768 }]);
  if (path === "/models/status") return jsonResp([{ name: "Qwen", state: "paused" }]);
  if (path === "/models/warm") return jsonResp({});
  if (path === "/templates") return jsonResp([]);
  if (path === "/skills/enabled") return jsonResp([]);
  if (path.startsWith("/backends")) {
    return jsonResp([
      { name: "local", available: true, model: "Qwen" },
      { name: "claude", available: true, model: "claude-sonnet" },
    ]);
  }
  if (path.startsWith("/chats")) return jsonResp([]);
  if (path === "/jobs") return jsonResp([]);
  if (path === "/images") return jsonResp({ images: [], status: { phase: "idle", queued: 0, progress: {}, modes: {}, aspect_ratios: [], resolutions: {}, edit: {}, upscale: {} } });
  return jsonResp({});
};

const FakeEventSource = fakeEventSource();

const win = new Emitter();
Object.assign(win, {
  addEventListener: (...a) => Emitter.prototype.addEventListener.call(win, ...a),
  removeEventListener: (...a) => Emitter.prototype.removeEventListener.call(win, ...a),
  dispatchEvent: (...a) => Emitter.prototype.dispatchEvent.call(win, ...a),
  localStorage: storage(),
  sessionStorage: storage(),
  location: loc,
  navigator: { serviceWorker: undefined, userAgent: "test" },
  history: { replaceState() {}, back() {} },
  scrollTo() {},
  confirm: () => false,
  innerHeight: 800,
  scrollY: 0,
  EventSource: FakeEventSource,
  fetch: fakeFetch,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
});

loc.onHashReplace = () => win.dispatchEvent({ type: "hashchange" });
globalThis.window = win;
globalThis.localStorage = win.localStorage;
globalThis.location = loc;
globalThis.fetch = fakeFetch;
const { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } = await import("../harness/web/client.mjs");

const sandbox = createContext({
  window: win, document: doc, location: loc, history: win.history, navigator: win.navigator,
  localStorage: win.localStorage, sessionStorage: win.sessionStorage, EventSource: FakeEventSource,
  fetch: fakeFetch, URL, AbortController, TextDecoder, console, getComputedStyle: (el) => el.style,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: win.requestAnimationFrame, cancelAnimationFrame: win.cancelAnimationFrame,
  confirm: win.confirm, agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL, Node, Event, JSON, Date, Math,
  Number, String, Boolean, Array, Object, Set, Map, Promise, Error, parseInt, encodeURIComponent,
  decodeURIComponent, undefined,
});

await runApp(sandbox);

process.on("unhandledRejection", (err) => {
  console.error(err);
  process.exit(1);
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = async (pred, label, ms = 2000) => {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (pred()) return;
    await sleep(20);
  }
  throw new Error(`timeout waiting for ${label}: ${byId.app.textContent.slice(0, 240)}`);
};

const go = async (hash) => {
  loc.hash = hash;
  loc.href = `http://localhost/${hash}`;
  win.dispatchEvent({ type: "hashchange" });
  await sleep(40);
};

const walk = (node, acc = []) => {
  if (!(node instanceof El)) return acc;
  acc.push(node);
  for (const c of node.childNodes || []) walk(c, acc);
  return acc;
};

const optVal = (el) => el.value || el.attributes.value || "";

const backendSelect = () => walk(byId.app).find((el) =>
  el.tagName === "SELECT" && walk(el).some((o) => o.tagName === "OPTION" && optVal(o) === "claude"));

const holdNote = () => walk(byId.app).find((el) => String(el.className).split(/\s+/).includes("gpu-hold-note"));

const submitBtn = () => walk(byId.app).find((el) =>
  el.tagName === "BUTTON" && el.type === "submit" && /^(Start|Queue task)$/.test(el.textContent));

const formKids = () => {
  const form = walk(byId.app).find((el) => el.tagName === "FORM" && backendSelect() && walk(el).includes(backendSelect()));
  return form ? form.childNodes.filter((c) => c instanceof El) : [];
};

const assertNoticeUnderBackend = () => {
  const kids = formKids();
  const backendIdx = kids.findIndex((el) => el.tagName === "SELECT" && walk(el).some((o) => o.tagName === "OPTION" && optVal(o) === "claude"));
  if (backendIdx < 0) throw new Error("backend select not in form");
  const note = kids[backendIdx + 1];
  if (!note || !String(note.className).split(/\s+/).includes("gpu-hold-note")) {
    throw new Error("gpu hold notice is not directly below the Backend dropdown");
  }
};

const assertQueued = (queued) => {
  const btn = submitBtn();
  if (!btn) throw new Error("start/queue button missing");
  if (queued) {
    if (btn.textContent !== "Queue task") throw new Error(`expected Queue task, got ${btn.textContent}`);
    if (!btn.classList.contains("queued")) throw new Error(`expected queued class, got ${btn.className}`);
    if (btn.classList.contains("primary")) throw new Error("queued button should not keep primary");
  } else {
    if (btn.textContent !== "Start") throw new Error(`expected Start, got ${btn.textContent}`);
    if (btn.classList.contains("queued")) throw new Error("Start should not have queued class");
    if (!btn.classList.contains("primary")) throw new Error(`expected primary, got ${btn.className}`);
  }
};

const pickBackend = (name) => {
  const sel = backendSelect();
  if (!sel) throw new Error("backend select missing");
  sel.value = name;
  sel.dispatchEvent({ type: "change" });
};

await waitFor(() => !byId.title.hidden, "boot");

gpuPayload = { manual: false, state: "clear" };
await go("#/new");
await waitFor(() => backendSelect(), "new task form hold off");
if (backendSelect().value !== "local") throw new Error(`hold off default should be local, got ${backendSelect().value}`);
assertQueued(false);
if (!holdNote() || !holdNote().hidden) throw new Error("hold notice should be hidden when the hold is off");

gpuPayload = { manual: true, state: "paused", manual_remaining_seconds: null };
await go("#/agents");
await go("#/new");
await waitFor(() => backendSelect()?.value === "claude", "hold on defaults to Claude");
assertQueued(false);
if (!holdNote().hidden) throw new Error("Claude should hide the hold notice");

pickBackend("local");
assertQueued(true);
if (holdNote().hidden) throw new Error("local model during hold should show the notice");
assertNoticeUnderBackend();
const link = walk(holdNote()).find((el) => el.tagName === "A");
if (!link || (link.href || link.attributes.href) !== "#/actions/resources") {
  throw new Error(`Actions → Resources should link to #/actions/resources, got ${link && (link.href || link.attributes.href)}`);
}
if (!/Model unloaded while something else uses the GPU; tasks wait \(/.test(holdNote().textContent)) {
  throw new Error(`unexpected notice text: ${holdNote().textContent}`);
}
if (!holdNote().textContent.includes("Actions → Resources")) throw new Error("notice should include Actions → Resources");
const modelState = walk(byId.app).find((el) => el.tagName === "DIV" && el.hidden === false && /Model unloaded/.test(el.textContent) && !String(el.className).includes("gpu-hold-note"));
if (modelState) throw new Error("paused hold copy should not remain under Model");

pickBackend("claude");
assertQueued(false);
if (!holdNote().hidden) throw new Error("hosted backend should hide the notice");

gpuPayload = { manual: false, state: "paused" };
await go("#/agents");
await go("#/new");
await waitFor(() => backendSelect()?.value === "claude", "scheduled hold defaults to Claude");
pickBackend("local");
assertQueued(true);
if (holdNote().hidden) throw new Error("scheduled hold should show the notice for a local model");

gpuPayload = { manual: false, state: "clear" };
await go("#/agents");
await go("#/new");
await waitFor(() => backendSelect()?.value === "local", "hold off restores local default");
assertQueued(false);
if (!holdNote().hidden) throw new Error("hold off should hide the notice");

await go("#/agents");
console.log("ok");
process.exit(0);
