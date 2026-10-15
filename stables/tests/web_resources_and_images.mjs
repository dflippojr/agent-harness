// UI harness: the Resources tab takes one diagnostics reading and never polls it (#311), and Images names an
// upscale or an edit that is running and offers the upscale choices.
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

const IMAGE_MODES = { fast: { label: "Z-Image-Turbo (fast, Apache 2.0)", available: true, resolution: "standard" } };
const imageStatus = (extra = {}, upscale = { available: true }) => ({
  phase: "idle", queued: 0, progress: {}, modes: IMAGE_MODES,
  aspect_ratios: ["1:1"],
  resolutions: { standard: { label: "Standard", sizes: { "1:1": [1024, 1024] } } },
  edit: { available: true, enabled: true },
  upscale,
  ...extra,
});
const DONE = {
  id: "img1", status: "done", prompt: "a lighthouse", model: "fast", width: 1024, height: 1024, scale: 1, seed: 7,
  source: "owner", created_at: 1, finished_at: 2, aspect_ratio: "1:1", resolution: "standard", editable: true,
  service: { edit: { enabled: true, available: true } },
};

let imagesPayload = { images: [], status: imageStatus() };
const counts = { diagnostics: 0, hold: 0 };
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
  if (path === "/resources") {
    counts.hold += 1;
    return jsonResp({ enabled: true, manual: false, state: "clear", signals: [] });
  }
  if (path === "/resources/diagnostics") {
    counts.diagnostics += 1;
    return jsonResp({
      ram: { total_bytes: 16 * 2 ** 30, available_bytes: 8 * 2 ** 30 }, vram: {}, gpu_load: 3,
      guard: { enabled: true, state: "clear", reasons: [] }, model: { state: "loaded" }, as_of: 1,
    });
  }
  if (path === "/gpu") return jsonResp({ manual: false, state: "clear" });
  if (path === "/projects") return jsonResp([{ name: "scratch", target: "tower" }]);
  if (path === "/models") return jsonResp([{ name: "Qwen", default: true, context_tokens: 32768 }]);
  if (path === "/models/status") return jsonResp([{ name: "Qwen", state: "loaded" }]);
  if (path === "/templates" || path === "/jobs" || path.startsWith("/chats")) return jsonResp([]);
  if (path === "/skills/enabled") return jsonResp([]);
  if (path.startsWith("/backends")) return jsonResp([{ name: "local", available: true, model: "Qwen" }]);
  if (path === "/images" || path.startsWith("/images?")) return jsonResp(imagesPayload);
  if (path === "/images/img1") return jsonResp(DONE);
  if (path === "/images/warmup") return jsonResp({ ok: true });
  return jsonResp({});
};

const FakeEventSource = fakeEventSource();

// Intervals are recorded, never scheduled: the harness fires them itself, as often as it likes.
const intervals = new Map();
let nextInterval = 1;
const fakeSetInterval = (fn) => { intervals.set(nextInterval, fn); return nextInterval++; };
const fakeClearInterval = (id) => { intervals.delete(id); };

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
const { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } = await import("../public/client.mjs");

const sandbox = createContext({
  window: win, document: doc, location: loc, history: win.history, navigator: win.navigator,
  localStorage: win.localStorage, sessionStorage: win.sessionStorage, EventSource: FakeEventSource,
  fetch: fakeFetch, URL, AbortController, TextDecoder, console, getComputedStyle: (el) => el.style,
  setTimeout, clearTimeout, setInterval: fakeSetInterval, clearInterval: fakeClearInterval,
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
const walk = (node, acc = []) => {
  if (!(node instanceof El)) return acc;
  acc.push(node);
  for (const c of node.childNodes || []) walk(c, acc);
  return acc;
};
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
const find = (pred) => walk(byId.app).find(pred);
const classes = (el) => String(el.className).split(/\s+/);
const upscaleChoice = () => find((el) => el.tagName === "SELECT" && el.attributes["aria-label"] === "Upscale");

await waitFor(() => !byId.title.hidden, "boot");

// #311: Actions -> Resources reads the diagnostics once when the tab opens. The GPU hold row beside it polls, so
// firing every recorded interval shows the diagnostics card is not among them.
await go("#/actions/resources");
await waitFor(() => counts.diagnostics >= 1 && /VRAM|RAM/.test(byId.app.textContent), "a diagnostics reading");
await sleep(100);
const readings = counts.diagnostics;
const holdBefore = counts.hold;
for (let i = 0; i < 3; i += 1) {
  for (const fn of [...intervals.values()]) fn();
  await sleep(30);
}
if (counts.hold <= holdBefore) throw new Error("the GPU hold row should still poll, or this check proves nothing");
if (counts.diagnostics !== readings) throw new Error(`diagnostics polled: ${counts.diagnostics - readings} more readings`);
find((el) => el.tagName === "BUTTON" && el.attributes["aria-label"] === "Refresh diagnostics").click();
await waitFor(() => counts.diagnostics === readings + 1, "the refresh button takes one more reading");

// An upscale and an edit in flight are named in the gallery status, not shown as plain "Generating".
for (const [stage, label] of [["upscaling", "Upscaling"], ["editing", "Editing"], [undefined, "Generating"]]) {
  imagesPayload = { images: [], status: imageStatus({ phase: "generating", progress: { stage, value: 2, max: 4 } }) };
  await go("#/agents");
  await go("#/images");
  await waitFor(() => find((el) => classes(el).includes("image-status-label")), "image status");
  const shown = find((el) => classes(el).includes("image-status-label")).textContent;
  const detail = find((el) => classes(el).includes("image-status-detail")).textContent;
  if (!shown.startsWith(label)) throw new Error(`stage ${stage}: expected label ${label}, got ${shown}`);
  if (stage && !detail.startsWith(`${label} 50%`)) throw new Error(`stage ${stage}: detail ${detail}`);
}

// Generate offers 2x and 4x upscaling only when the Real-ESRGAN weights are installed.
for (const available of [true, false]) {
  imagesPayload = { images: [], status: imageStatus({}, { available }) };
  await go("#/agents");
  await go("#/images");
  await waitFor(upscaleChoice, "upscale choice");
  const options = walk(upscaleChoice()).filter((el) => el.tagName === "OPTION");
  const labels = options.map((el) => el.textContent).join("|");
  if (labels !== "Don't upscale|Upscale 2× after generate|Upscale 4× after generate") throw new Error(labels);
  const disabled = options.slice(1).map((el) => Boolean(el.disabled || el.attributes.disabled));
  if (disabled.some((flag) => flag !== !available)) throw new Error(`available=${available}, disabled=${disabled}`);
}

// A finished image offers Edit, Upscale 2x and Upscale 4x.
await go("#/images/img1");
await waitFor(() => /Upscale 2×/.test(byId.app.textContent), "image page");
for (const label of ["Upscale 2×", "Upscale 4×", "Another one", "Download"]) {
  if (!find((el) => el.tagName === "BUTTON" && el.textContent === label)) {
    throw new Error(`missing ${label}: ${byId.app.textContent}`);
  }
}
const edit = find((el) => el.tagName === "A" && el.textContent === "Edit");
if (!edit || edit.attributes.href !== "#/images/img1/edit") throw new Error("the mask editor link is missing");

await go("#/agents");
console.log("ok");
process.exit(0);
