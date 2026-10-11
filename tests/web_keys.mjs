// Keyboard shortcuts (#571): keyAction() reads the shortcut set from #540, never while focus is in a field and with no
// Approve key; mountKeys() opens and closes the `?` sheet, focuses search, starts New, switches section after `G` and moves
// through list rows with J and K, and leaves the page alone while a sheet or menu answers its own keys.
import assert from "node:assert/strict";
import { keyAction, isTypingTarget, newHash, stepRow, mountKeys, SHORTCUTS, GO_KEYS, CHORD_MS, ROW_SELECTOR,
  shortcutsBody } from "../harness/web/lib/keys.mjs";
import { SPLITS } from "../harness/web/lib/layout.mjs";
import { createDocument, Emitter, Node, El, walk } from "./web_stub_dom.mjs";

const el = (tagName, extra = {}) => ({ tagName, ...extra });
const press = (key, extra = {}) => ({ key, target: el("BODY"), ...extra });

// ---------- keyAction: the shortcut set ----------
assert.deepEqual(keyAction(press("?", { shiftKey: true })), { action: "help" }, "? needs Shift on most layouts");
assert.deepEqual(keyAction(press("/")), { action: "search" });
assert.deepEqual(keyAction(press("n")), { action: "new" });
assert.deepEqual(keyAction(press("N")), { action: "new" }, "Caps Lock still means N");
assert.deepEqual(keyAction(press("j")), { action: "next" });
assert.deepEqual(keyAction(press("k")), { action: "prev" });
assert.deepEqual(keyAction(press("g")), { action: "chord" });
for (const [key, hash] of Object.entries(GO_KEYS)) {
  assert.deepEqual(keyAction(press(key), { chord: true }), { action: "go", hash }, `G then ${key}`);
}
assert.deepEqual(GO_KEYS, { a: "#/agents", j: "#/jobs", i: "#/images", s: "#/settings" });
assert.deepEqual(keyAction(press("x"), { chord: true }), { action: "cancel" }, "any other key drops the chord");
assert.deepEqual(keyAction(press("j"), { chord: true }), { action: "go", hash: "#/jobs" }, "G J is Jobs, not the next row");
assert.equal(keyAction(press("j", { repeat: true }))?.action, "next", "J and K repeat while held");
assert.equal(keyAction(press("n", { repeat: true })), null, "other keys don't repeat");
assert.equal(keyAction(press("N", { shiftKey: true })), null, "Shift+N is not N");

// Modified keys belong to the browser and assistive technology; composing text is typing.
for (const mod of ["ctrlKey", "metaKey", "altKey"]) {
  for (const key of ["?", "/", "n", "g", "j", "k"]) assert.equal(keyAction(press(key, { [mod]: true })), null, `${mod}+${key}`);
}
assert.equal(keyAction(press("n", { isComposing: true })), null);
assert.equal(keyAction(press("n", { defaultPrevented: true })), null);

// Never while focus is in a field.
const fields = [el("INPUT"), el("INPUT", { type: "text" }), el("INPUT", { type: "search" }), el("INPUT", { type: "email" }),
  el("TEXTAREA"), el("SELECT"), el("DIV", { isContentEditable: true }), el("input", { type: "password" })];
for (const target of fields) {
  assert.ok(isTypingTarget(target), `${target.tagName} ${target.type || ""} is typing`);
  for (const key of ["?", "/", "n", "g", "j", "k", "["]) assert.equal(keyAction(press(key, { target })), null, `${key} in ${target.tagName}`);
  assert.equal(keyAction(press("a", { target }), { chord: true }), null, "a pending G never navigates from a field");
}
for (const target of [el("INPUT", { type: "checkbox" }), el("INPUT", { type: "radio" }), el("BUTTON"), el("A"), null]) {
  assert.ok(!isTypingTarget(target), `${target?.tagName} ${target?.type || ""} is not typing`);
}
assert.equal(keyAction(press("j", { target: el("INPUT", { type: "checkbox" }) }))?.action, "next", "a job's switch keeps J");

// No Approve key: nothing an approval could take as a yes does anything, and the sheet says why.
for (const key of ["a", "y", "Enter", " ", "A", "Y"]) assert.equal(keyAction(press(key)), null, `${JSON.stringify(key)} alone`);
const rows = SHORTCUTS.flatMap((g) => g.rows);
assert.ok(!rows.some((r) => /approve|deny/i.test(r.label)), "no shortcut approves or denies");
assert.deepEqual(SHORTCUTS.map((g) => g.title), ["Anywhere", "Lists and sessions"]);
assert.deepEqual(rows.map((r) => r.keys.join("+")),
  ["/", "N", "G+A", "G+J", "G+I", "G+S", "?", "J", "K", "Enter", "[", "Esc", "Ctrl+Enter"], "the set from #540");
assert.equal(CHORD_MS, 1500);

// ---------- pure helpers ----------
assert.equal(newHash(["agents"], "owner"), "#/new");
assert.equal(newHash(["s", "abc"], "member"), "#/new");
assert.equal(newHash(["jobs", "abc"], "owner"), "#/jobs/new");
assert.equal(newHash(["jobs"], "member"), null, "members have no jobs");
assert.equal(newHash(["agents"], "guest"), null, "guests start nothing");
assert.equal(newHash(["images"], "owner"), null);
assert.equal(stepRow(0, -1, -1, 1), -1);
assert.equal(stepRow(3, -1, -1, 1), 0, "J starts at the first row");
assert.equal(stepRow(3, -1, -1, -1), 0, "so does K");
assert.equal(stepRow(3, -1, 1, 1), 2, "from the open row");
assert.equal(stepRow(3, 0, 2, 1), 1, "the focused row wins over the open one");
assert.equal(stepRow(3, 2, -1, 1), 2, "the ends hold");
assert.equal(stepRow(3, 0, -1, -1), 0);
assert.ok(SPLITS[0].empty.keys.some(([caps]) => caps.includes("?")), "the empty Agents pane points at ?");

// ---------- mountKeys in a stub page ----------
const { doc, byId } = createDocument();
globalThis.document = doc;
globalThis.Node = Node;
const win = new Emitter();
win.setTimeout = setTimeout;
win.clearTimeout = clearTimeout;
globalThis.window = win;
El.prototype.focus = function focus() { doc.activeElement = this; };
El.prototype.getClientRects = function getClientRects() {
  for (let n = this; n; n = n.parentNode) if (n.hidden) return [];
  return this.isConnected || this === doc.body ? [{}] : [];
};
El.prototype.scrollIntoView = function scrollIntoView() { this.scrolled = true; };
El.prototype.select = function select() { this.selected = true; };
doc.body.append(byId.bar, byId.app, byId["fab-host"]);
byId["fab-host"].append(byId.fab);
doc.activeElement = doc.body;

// Only the selectors keys.mjs asks for.
const all = (pred) => walk(doc.body, pred);
const openDialogs = () => all((n) => n.tagName === "DIALOG" && n.attributes.open !== undefined);
doc.querySelector = (sel) => {
  assert.equal(sel, "dialog[open], [role=menu]:not([hidden])");
  return openDialogs()[0] || all((n) => n.attributes.role === "menu" && !n.hidden)[0] || null;
};
doc.querySelectorAll = (sel) => {
  if (sel === "input[type=search]") return all((n) => n.tagName === "INPUT" && n.type === "search");
  if (sel === "#split-list .list-new, #bar .list-new, #fab") {
    return all((n) => n.classList.contains("list-new") || n === byId.fab);
  }
  throw new Error(`unexpected selector ${sel}`);
};
const rowsIn = (root) => () => walk(root, (n) => n.classList.contains("agent-row") || n.classList.contains("job-main"));
byId.app.querySelectorAll = (sel) => { assert.equal(sel, ROW_SELECTOR); return rowsIn(byId.app)(); };

const location = { hash: "#/agents" };
const went = [];
let role = "owner";
mountKeys({ browser: { document: doc, window: win, location }, go: (hash) => went.push(hash), role: () => role });
const key = (k, extra = {}) => {
  let prevented = false;
  doc.emit("keydown", { key: k, target: doc.activeElement, preventDefault() { prevented = true; }, ...extra });
  return prevented;
};
const tick = () => new Promise((r) => setTimeout(r, 0));

// ? opens the sheet on Close, lists the set for the role, and ? or Escape closes it, back where focus was.
{
  const opener = new El("a", { class: "agent-row" });
  byId.app.append(opener);
  opener.focus();
  assert.ok(key("?", { shiftKey: true }));
  const [dialog] = openDialogs();
  assert.ok(dialog.classList.contains("sheet") && dialog.classList.contains("keys-sheet"));
  assert.match(dialog.textContent, /Keyboard shortcuts/);
  assert.match(dialog.textContent, /Go to Jobs/);
  assert.match(dialog.textContent, /Approve and Deny have no single-key shortcut/);
  assert.equal(doc.activeElement.attributes["aria-label"], "Close", "the sheet starts on Close");
  assert.equal(walk(dialog, (n) => n.tagName === "KBD").length, rows.reduce((sum, r) => sum + r.keys.length, 0));
  assert.ok(!key("j"), "the page behind the sheet ignores J");
  assert.ok(!key("g") && !key("a"), "and G A");
  assert.deepEqual(went, []);
  assert.ok(key("?", { shiftKey: true }), "? closes it again");
  await tick();
  assert.equal(openDialogs().length, 0);
  assert.equal(doc.activeElement, opener, "focus returns to where it was");

  key("?", { shiftKey: true });
  openDialogs()[0].dispatchEvent({ type: "cancel" });
  await tick();
  assert.equal(openDialogs().length, 0, "Escape closes it");
  key("?", { shiftKey: true });
  walk(openDialogs()[0], (n) => n.attributes["aria-label"] === "Close")[0].click();
  await tick();
  assert.equal(openDialogs().length, 0, "so does Close");
  opener.remove();

  role = "member";
  const body = new El("div");
  body.append(...shortcutsBody("member"));
  assert.doesNotMatch(body.textContent, /Go to Jobs|Go to Images/, "a member's sheet leaves out what they can't open");
  const guest = new El("div");
  guest.append(...shortcutsBody("guest"));
  assert.doesNotMatch(guest.textContent, /New task/);
  role = "owner";
}

// G then a section key navigates; a slow second key or another key drops the chord.
{
  assert.ok(key("g"));
  assert.ok(key("j"));
  assert.deepEqual(went.splice(0), ["#/jobs"]);
  key("g"); key("x"); key("a");
  assert.deepEqual(went.splice(0), [], "G X A does nothing");
  const [realSet, realClear] = [win.setTimeout, win.clearTimeout];
  let expire = null;
  win.setTimeout = (fn) => { expire = fn; return 1; };
  win.clearTimeout = () => {};
  key("g");
  expire();
  key("s");
  assert.deepEqual(went.splice(0), [], "the chord times out");
  win.setTimeout = realSet;
  win.clearTimeout = realClear;
}

// / focuses the visible search field; with none, the browser keeps the key.
{
  assert.ok(!key("/"), "no search field: the key is the browser's");
  const search = new El("input", { type: "search" });
  byId.app.append(search);
  assert.ok(key("/"));
  assert.equal(doc.activeElement, search);
  assert.ok(search.selected, "the old query is selected, ready to replace");
  assert.ok(!key("n"), "typing N into search stays typing");
  search.remove();
  doc.activeElement = doc.body;
}

// N presses the page's visible New action, else goes to the section's New page; guests get nothing.
{
  let clicked = 0;
  byId.fab.addEventListener("click", () => { clicked++; });
  byId["fab-host"].hidden = false;
  assert.ok(key("n"));
  assert.equal(clicked, 1, "the phone's New task button");
  byId["fab-host"].hidden = true;
  const listNew = new El("a", { class: "btn primary list-new" });
  listNew.addEventListener("click", () => { clicked += 10; });
  byId.bar.append(listNew);
  key("n");
  assert.equal(clicked, 11, "the list header's New action");
  listNew.remove();
  location.hash = "#/jobs/abc";
  key("n");
  assert.deepEqual(went.splice(0), ["#/jobs/new"], "a job open full width still starts a new one");
  role = "guest";
  location.hash = "#/agents";
  assert.ok(!key("n"));
  assert.deepEqual(went, []);
  role = "owner";
}

// J and K move focus through the visible rows, from the open one when nothing is focused; Enter is the link's own.
{
  const list = new El("div");
  const rowEls = [0, 1, 2].map(() => new El("a", { class: "agent-row" }));
  list.append(...rowEls);
  byId.app.append(list);
  key("j");
  assert.equal(doc.activeElement, rowEls[0]);
  assert.ok(rowEls[0].scrolled, "the row scrolls into view");
  key("j"); key("j"); key("j");
  assert.equal(doc.activeElement, rowEls[2], "J holds at the last row");
  key("k");
  assert.equal(doc.activeElement, rowEls[1]);
  rowEls[1].hidden = true;
  key("k");
  assert.equal(doc.activeElement, rowEls[0], "hidden rows are skipped");
  rowEls[1].hidden = false;

  // Beside an open session the list pane is the one that moves, starting from the open row.
  const pane = new El("section", { id: "split-list" });
  const paneRows = [0, 1, 2].map(() => new El("a", { class: "agent-row" }));
  paneRows[1].setAttribute("aria-current", "page");
  pane.append(...paneRows);
  pane.querySelectorAll = (sel) => { assert.equal(sel, ROW_SELECTOR); return rowsIn(pane)(); };
  doc.body.append(pane);
  byId["split-list"] = pane;
  const getById = doc.getElementById;
  doc.getElementById = (id) => (id === "split-list" ? pane : getById(id));
  doc.activeElement = doc.body;
  key("j");
  assert.equal(doc.activeElement, paneRows[2], "J goes on from the open session's row");
  key("k"); key("k");
  assert.equal(doc.activeElement, paneRows[0]);

  // An open menu answers its own keys.
  const menu = new El("div", { role: "menu" });
  doc.body.append(menu);
  assert.ok(!key("j"));
  assert.equal(doc.activeElement, paneRows[0]);
  menu.remove();
  pane.remove();
  list.remove();
}

console.log("ok: shortcuts, the ? sheet, fields and no Approve key");
