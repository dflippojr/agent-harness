// Tool-call rows (#508): a 48 px summary whose output and arguments are built only when the row opens, previews that
// carry the first lines only, Copy with a select-by-hand fallback, and a full-screen viewer with the whole text.
import assert from "node:assert/strict";
import { createToolRows } from "../harness/web/lib/tool-row.mjs";
import { lineCount, previewText, resultState, toolKind } from "../harness/web/lib/tools.mjs";

const el = (tag, attrs, ...kids) => {
  const node = { tag, attrs: { ...attrs }, className: attrs?.class || "", textContent: "", open: false, listeners: {}, removed: false };
  node.kids = kids.flat(Infinity).filter((k) => k !== null && k !== undefined);
  node.addEventListener = (type, fn) => { (node.listeners[type] ||= []).push(fn); };
  node.fire = (type, event = {}) => (node.listeners[type] || []).forEach((fn) => fn(event));
  node.focus = () => { browser.document.activeElement = node; };
  node.setAttribute = (k, v) => { node.attrs[k] = v; };
  node.append = (...more) => node.kids.push(...more.flat(Infinity));
  node.remove = () => { node.removed = true; };
  Object.defineProperty(node, "childNodes", { get: () => node.kids });
  return node;
};
const fill = (node, ...kids) => { node.kids = kids.flat(Infinity).filter((k) => k !== null && k !== undefined); return node; };
const text = (n) => (n && typeof n === "object" ? [n.textContent, ...(n.kids || []).map(text)].join(" ") : String(n ?? ""));
const find = (n, pred) => {
  if (!n || typeof n !== "object") return null;
  if (pred(n)) return n;
  for (const k of n.kids || []) { const hit = find(k, pred); if (hit) return hit; }
  return null;
};
const button = (n, label) => find(n, (x) => x.tag === "button" && text(x).trim() === label);
const click = (b) => (b.attrs.onclick ? b.attrs.onclick() : b.fire("click"));

// Pure helpers.
assert.equal(lineCount(""), 0);
assert.equal(lineCount("a"), 1);
assert.equal(lineCount("a\nb\n"), 2, "one trailing newline is not a line");
assert.deepEqual(previewText("1\n2\n3", 2), { text: "1\n2", more: true });
assert.deepEqual(previewText("1\n2\n", 8), { text: "1\n2\n", more: false });
assert.equal(previewText("x".repeat(5000)).text.length, 1200, "one huge line is cut too");
assert.deepEqual(resultState(true, 4.2), { text: "ok · 4 s", kind: "ok" });
assert.deepEqual(resultState(false, 0.03), { text: "error · <1 s", kind: "err" });
assert.equal(toolKind("run_shell", {}), "shell");
assert.equal(toolKind("web_fetch", {}), "web");
assert.equal(toolKind("read_file", { path: "a" }), "file");
assert.equal(toolKind("memory_search", null), "other");

const body = [];
const toasts = [];
let clipboard = null;
let clipboardFails = false;
const browser = {
  document: { body: { append: (...n) => body.push(...n) } },
  navigator: { clipboard: { writeText: async (t) => { if (clipboardFails) throw new Error("denied"); clipboard = t; } } },
  window: {},
};
const { toolRow, closeViewer } = createToolRows({ h: el, fill, toast: (m) => toasts.push(m), browser });

const output = Array.from({ length: 40 }, (_, i) => `line ${i + 1}`).join("\n");
const row = toolRow({ name: "run_shell", summary: "pytest -x", kind: "shell", args: { command: "pytest -x" } });
const summary = row.el.kids[0];
assert.equal(row.el.tag, "details");
assert.match(text(summary), /run_shell/);
assert.match(text(summary), /pytest -x/);
assert.equal(row.state.className, "tool-state run");

// Collapsed by default, and nothing is built until the row opens.
row.setOutput(output, output.length);
row.setState("ok · 4 s", "ok");
assert.equal(row.state.className, "tool-state ok");
assert.equal(row.body.kids.length, 0, "the body stays empty while the row is closed");
row.el.open = true;
row.el.fire("toggle");
const opened = text(row.body);
assert.match(opened, /Output · 40 lines/);
assert.ok(button(row.body, "Copy") && button(row.body, "Open"), "the output has Copy and Open");
assert.match(opened, /line 8\b/);
assert.doesNotMatch(opened, /line 9\b/, "the preview carries the first lines only");
const preview = find(row.body, (x) => x.tag === "pre");
assert.equal(preview.attrs.class, "tool-preview more");

// Arguments fold behind their own line count and pretty-print only when opened.
const fold = find(row.body, (x) => x.attrs?.class === "tool-args");
assert.match(text(fold), /Arguments · 3 lines/);
assert.doesNotMatch(text(fold), /"command"/);
fold.open = true;
fold.fire("toggle");
assert.match(text(fold), /"command": "pytest -x"/);

// Copy puts the whole output on the clipboard.
await click(button(row.body, "Copy"));
assert.equal(clipboard, output);
assert.equal(toasts.at(-1), "Copied");

// Open shows the whole text full screen; Close removes it.
const opener = button(row.body, "Open");
opener.focus();
click(opener);
const viewer = body.at(-1);
assert.equal(viewer.tag, "dialog");
assert.equal(viewer.attrs.class, "tool-viewer");
assert.equal(browser.document.activeElement, button(viewer, "Close"), "viewer starts on Close");
assert.equal(viewer.attrs.open, "", "without showModal the dialog is opened by attribute");
assert.match(text(viewer), /line 40/);
const wrap = button(viewer, "Wrap");
assert.equal(wrap.attrs["aria-pressed"], "true", "the viewer wraps by default");
click(wrap);
assert.equal(wrap.attrs["aria-pressed"], "false");
assert.equal(find(viewer, (x) => x.tag === "pre").className, "tool-viewer-text");
click(button(viewer, "Close"));
assert.ok(viewer.removed, "Close removes the viewer");
assert.equal(browser.document.activeElement, opener, "Close returns focus to Open");
click(opener);
let prevented = false;
body.at(-1).fire("cancel", { preventDefault() { prevented = true; } });
assert.ok(prevented && body.at(-1).removed, "Escape dismisses and removes the viewer");
assert.equal(browser.document.activeElement, opener);
click(opener);
const backdropViewer = body.at(-1);
backdropViewer.fire("click", { target: backdropViewer });
assert.equal(backdropViewer.removed, false, "dragging text onto the backdrop does not dismiss");
backdropViewer.fire("pointerdown", { target: backdropViewer });
backdropViewer.fire("click", { target: backdropViewer });
assert.ok(backdropViewer.removed, "a backdrop press dismisses");

// Without clipboard access, Copy opens the viewer so the text can be copied by hand.
clipboardFails = true;
const before = body.length;
await click(button(row.body, "Copy"));
assert.match(toasts.at(-1), /copy it by hand/);
assert.equal(body.length, before + 1, "the fallback opens the viewer");
closeViewer();
assert.ok(body.at(-1).removed, "closeViewer (route change) removes an open viewer");

// A result that lands while the row is open renders straight away; before that the row says it is waiting.
const live = toolRow({ name: "read_file", summary: "a.py", kind: "file", args: { path: "a.py" } });
live.el.open = true;
live.el.fire("toggle");
assert.match(text(live.body), /Waiting for output/);
live.setOutput("short\n", 6);
assert.match(text(live.body), /Output · 1 line\b/);
assert.equal(find(live.body, (x) => x.tag === "pre").attrs.class, "tool-preview");

// Output the runner trimmed says so; empty output has nothing to copy.
const big = toolRow({ name: "run_shell", summary: "", kind: "shell", args: null });
big.setOutput("a\nb", 45000);
big.el.open = true;
big.el.fire("toggle");
assert.match(text(big.body), /middle trimmed from 45K chars/);
assert.equal(find(big.body, (x) => x.attrs?.class === "tool-args"), null, "no arguments fold without arguments");
const empty = toolRow({ name: "noop", summary: "", kind: "other", args: null });
empty.setOutput("", 0);
empty.el.open = true;
empty.el.fire("toggle");
assert.match(text(empty.body), /No output/);
assert.equal(button(empty.body, "Copy"), null);

console.log("ok");
