// UI harness for Chat snippets (#85): Run appears only for supported-language fenced blocks and the manual
// editor, sending a message never runs code, the editor needs an explicit language, and results render as text.
import { createContext } from "node:vm";
import { md as renderMd } from "../harness/web/lib/markdown.mjs";
import { snippetLanguage } from "../harness/web/lib/snippets.mjs";
import { runApp } from "./web_app_loader.mjs";
import { El, Emitter, Node, createDocument, fakeEventSource, storage } from "./web_stub_dom.mjs";

const fail = (msg) => { throw new Error(msg); };

const { byId, doc } = createDocument({ features: [], feature: "chat", focusables: false });
doc.addEventListener = () => {};

const CHAT = "chatab12cd";
const loc = {
  href: "http://localhost/#/", origin: "http://localhost", hash: "#/", protocol: "http:", pathname: "/",
  replace(url) { this.hash = String(url).startsWith("#") ? String(url) : `#${url}`; },
};
const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body, text: async () => JSON.stringify(body),
});

const posts = [];
const fakeFetch = async (url, opts = {}) => {
  const href = String(url);
  const path = href.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "");
  if ((opts.method || "GET") === "POST") posts.push({ path, body: opts.body ? JSON.parse(opts.body) : null });
  if (path === "/health" || href.endsWith("/health")) return jsonResp({ protocols: { admin: { min: 1, max: 4 } }, update_hint: {} });
  if (path === "/me") return jsonResp({ role: "owner", name: "Owner", login: "owner", public_url: "http://localhost" });
  if (path === "/profile") return jsonResp({ emoji: "🙂", choices: ["🙂"] });
  if (path === `/chats/${CHAT}`) return jsonResp({ id: CHAT, title: "Snippets", status: "done", backend: "local", model: "fake", effort: "" });
  if (path.endsWith("/snippets")) return jsonResp({ id: "sn-new", status: "running" }, 202);
  if (path.includes("/snippets/")) return jsonResp({ id: "sn-1", status: "cancelling" });
  if (path.endsWith("/messages")) return jsonResp({ id: CHAT, status: "queued" });
  if (path.startsWith("/chats")) return jsonResp([]);
  return jsonResp({});
};

const streams = [];
const FakeEventSource = fakeEventSource(streams);

const win = new Emitter();
Object.assign(win, {
  localStorage: storage(), sessionStorage: storage(), location: loc,
  navigator: { serviceWorker: undefined, userAgent: "test" },
  history: { back() {}, replaceState() {} },
  scrollTo() {}, confirm: () => false, innerHeight: 800, scrollY: 0, caches: undefined,
  EventSource: FakeEventSource, fetch: fakeFetch,
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
  cancelAnimationFrame: win.cancelAnimationFrame, confirm: win.confirm, prompt: () => null,
  agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL, Node, Event, JSON, Date, Math, Number, String, Boolean, Array, Object,
  Set, Map, Promise, Error, parseInt, encodeURIComponent, decodeURIComponent, undefined,
});
await runApp(sandbox);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// md(): only supported fence tags are marked for a Run button; code stays escaped.
const md = (src) => renderMd(src, []);
const html = md("```python\nprint(1)\n```\n\n```bash\nrm -rf /\n```\n\n```c#\nConsole.WriteLine(1);\n```\n\n```\nplain\n```\n\n```html\n<script>alert(1)</script>\n```");
if ((html.match(/data-snippet-lang=/g) || []).length !== 2) fail(`expected two runnable blocks: ${html}`);
if (!html.includes('data-snippet-lang="python"') || !html.includes('data-snippet-lang="csharp"')) fail(html);
if (!html.includes("<pre><code>rm -rf /</code></pre>") || !html.includes("<pre><code>plain</code></pre>")) fail(html);
if (html.includes("<script>") || !html.includes("&lt;script&gt;")) fail(`fence content must stay escaped: ${html}`);
// A fence opener followed by a long whitespace run and no closing fence must stay linear (Sonar S8786).
// Compare 10x the input against a baseline instead of an absolute time: linear growth is about 10x, quadratic
// about 100x, and an absolute limit flakes on a slow, loaded runner (#217). Each time is the median of five runs,
// so one GC or JIT pause can't pass for quadratic growth (#312).
const timeUnclosedFence = (n) => {
  const input = "```py" + " ".repeat(n) + "\n".repeat(n);
  const times = [];
  for (let i = 0; i < 5; i++) {
    const t0 = performance.now();
    md(input);
    times.push(performance.now() - t0);
  }
  return times.sort((a, b) => a - b)[2];
};
timeUnclosedFence(1000); // warm up the regex engine
const baseline = Math.max(timeUnclosedFence(10000), 5);
const scaled = timeUnclosedFence(100000);
if (scaled > baseline * 40 + 300) fail(`md() backtracks super-linearly on an unclosed fence with a whitespace run: ${baseline.toFixed(1)}ms at 10k, ${scaled.toFixed(1)}ms at 100k`);
if (!md("```py  \nprint(1)```").includes("<code>print(1)</code>")) fail("trailing spaces after the tag are dropped");
for (const [tag, id] of [["py", "python"], ["js", "javascript"], ["node", "javascript"], ["java", "java"], ["cs", "csharp"], ["cpp", "cpp"], ["c++", "cpp"]]) {
  if (snippetLanguage(tag) !== id) fail(`fence ${tag} should map to ${id}`);
}
for (const tag of ["", "bash", "sh", "ruby", "c", "html", "python2", "rust"]) {
  if (snippetLanguage(tag) !== "") fail(`fence ${tag} must not be runnable`);
}

loc.hash = `#/chat/${CHAT}`;
loc.href = `http://localhost/#/chat/${CHAT}`;
win.dispatchEvent({ type: "hashchange" });
await sleep(50);
const es = streams.filter((s) => s.url.includes(`/chats/${CHAT}/events`)).pop() || fail("chat event stream not opened");

const all = (node, pred, out = []) => {
  if (!(node instanceof El)) return out;
  if (pred(node)) out.push(node);
  for (const c of node.childNodes) all(c, pred, out);
  return out;
};
const everything = () => [byId.app, doc.body];
const find = (pred) => everything().flatMap((n) => all(n, pred));
const buttons = (text) => find((n) => n.tagName === "BUTTON" && n.textContent === text);
const hasClass = (cls) => (n) => (n.className || "").split(/\s+/).includes(cls);

// A pasted message: Run only on the supported fence; the bash fence stays text.
es.emit("user_message", { content: "Try this:\n```python\nprint('hi')\n```\nand\n```bash\nrm -rf /\n```" }, 1);
await sleep(10);
const userMsg = find(hasClass("user"))[0] || fail("user message not rendered");
const runBtns = all(userMsg, (n) => n.tagName === "BUTTON");
if (runBtns.length !== 1 || runBtns[0].textContent !== "▶ Run Python") fail(`expected one Run Python button, got ${runBtns.map((b) => b.textContent)}`);
if (!userMsg.textContent.includes("```bash\nrm -rf /\n```")) fail("unsupported fence should stay as plain text");
if (posts.some((p) => p.path.endsWith("/snippets"))) fail("rendering a message must not run anything");

runBtns[0].click();
await sleep(10);
const blockRun = posts.find((p) => p.path === `/chats/${CHAT}/snippets`) || fail("Run did not POST");
if (JSON.stringify(blockRun.body) !== JSON.stringify({ language: "python", source: "print('hi')", origin: "block" })) fail(JSON.stringify(blockRun.body));

// Sending a message with code only sends a message.
posts.length = 0;
const composer = find((n) => n.tagName === "TEXTAREA" && n.attributes["aria-label"] === "Message")[0] || fail("no composer");
composer.value = "```python\nprint(3)\n```";
buttons("Send")[0].click();
await sleep(20);
if (!posts.some((p) => p.path.endsWith("/messages")) || posts.some((p) => p.path.includes("/snippets"))) fail(`send must not run code: ${JSON.stringify(posts)}`);

// Running card, then a hostile result: every value is text, no elements come from output.
const hostile = "<img src=x onerror=alert(1)><script>alert(2)</script>";
es.emit("snippet_started", { id: "sn-1", language: "python", label: "Python", source: hostile, origin: "editor" }, 2);
await sleep(10);
const card = find((n) => n.attributes["data-run"] === "sn-1")[0] || fail("no running card");
const cancel = all(card, (n) => n.tagName === "BUTTON" && n.textContent === "Cancel")[0] || fail("no Cancel button");
posts.length = 0;
cancel.click();
await sleep(10);
if (!posts.some((p) => p.path === `/chats/${CHAT}/snippets/sn-1/cancel`)) fail("Cancel did not POST");
es.emit("snippet_result", {
  id: "sn-1", language: "python", label: "Python", status: "limit_exceeded", reasons: ["output_limit"], truncated: true,
  toolchain: { image: "python:3.12-slim", version: "Python 3.12.14" }, compile: null, error: "", duration_ms: 1234,
  run: { exit_code: null, stdout: hostile, stderr: "\u001b[31m</pre><b>x</b>" },
}, 3);
await sleep(10);
if (all(card, (n) => n.tagName === "BUTTON").length) fail("Cancel should go away once the run finishes");
if (all(card, (n) => ["IMG", "SCRIPT", "B"].includes(n.tagName)).length) fail("output must never become elements");
if (all(card, (n) => n._html !== null).length) fail("snippet cards must not use innerHTML");
const text = card.textContent;
for (const want of ["Limit reached", "Python 3.12.14", "python:3.12-slim", "1.2 s", "Reason: output limit (1 MiB)",
  "Output was truncated at the 1 MiB limit.", "Program stopped", "stdout", "stderr", hostile, "</pre><b>x</b>"]) {
  if (!text.includes(want)) fail(`result card is missing ${JSON.stringify(want)}: ${text}`);
}

// Compile diagnostics are labeled apart from runtime output (and a result without a started event still shows).
es.emit("snippet_result", {
  id: "sn-2", language: "cpp", label: "C++", status: "compile_failed", reasons: [], truncated: false, error: "",
  toolchain: { image: "gcc:15", version: "g++ (GCC) 15.2.0" }, duration_ms: 800,
  compile: { exit_code: 1, output: "main.cpp:1: error: expected ';'" }, run: null,
}, 4);
await sleep(10);
const card2 = find((n) => n.attributes["data-run"] === "sn-2")[0] || fail("no card for a result without a start event");
const t2 = card2.textContent;
if (!t2.includes("Compile failed (exit 1) · compiler diagnostics") || !t2.includes("expected ';'")) fail(t2);
if (t2.includes("stdout") || t2.includes("Exit status")) fail(`no runtime section when compile failed: ${t2}`);
if (!all(card2, (n) => n.tagName === "PRE" && n.className.includes("compile")).length) fail("diagnostics need their own block");

// Manual editor: no default language; Run stays off until one is chosen.
const toggle = buttons("Run code")[0] || fail("no Run code button");
toggle.click();
const editor = find(hasClass("snippet-editor"))[0] || fail("no editor");
if (editor.hidden) fail("editor should open");
const select = all(editor, (n) => n.tagName === "SELECT")[0];
const code = all(editor, (n) => n.tagName === "TEXTAREA")[0];
const run = all(editor, (n) => n.tagName === "BUTTON")[0];
if (select.value !== "" || !run.disabled) fail("the editor must start with no language and Run disabled");
code.value = "int main() { return 0; }";
code.dispatchEvent({ type: "input" });
if (!run.disabled) fail("Run needs an explicit language");
select.value = "cpp";
select.dispatchEvent({ type: "change" });
if (run.disabled) fail("Run should enable once a language and code are set");
posts.length = 0;
run.click();
await sleep(20);
const editorRun = posts.find((p) => p.path === `/chats/${CHAT}/snippets`) || fail("editor Run did not POST");
if (JSON.stringify(editorRun.body) !== JSON.stringify({ language: "cpp", source: "int main() { return 0; }", origin: "editor" })) fail(JSON.stringify(editorRun.body));

console.log("ok");
process.exit(0);
