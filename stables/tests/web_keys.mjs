// Keyboard shortcuts (#571): keyAction() reads the shortcut set from #540, never while focus is in a field and with no
// Approve key; stepRow() and newHash() choose the row and the New page. The `?` sheet, search, G chords, J and K in a real
// page are the AH-19 journey in tools/ui-tests.
import assert from "node:assert/strict";
import { keyAction, isTypingTarget, newHash, stepRow, SHORTCUTS, GO_KEYS, CHORD_MS } from "../public/lib/keys.mjs";
import { SPLITS, mountReadingOrder, READING_ORDER_QUERY } from "../public/lib/layout.mjs";
import { Emitter } from "./web_stub_dom.mjs";

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
assert.equal(keyAction(press("?", { shiftKey: true, repeat: true })), null, "a held ? doesn't toggle the sheet");
assert.equal(keyAction(press("g", { repeat: true }), { chord: true }), null, "a held G keeps waiting for its second key");
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
for (const split of SPLITS) assert.ok(split.empty.keys?.some(([caps]) => caps.includes("?")), `the empty ${split.key} pane points at ?`);

// ---------- reading order: from 768 px the rail, then the list pane, then the header bar ----------
{
  const parent = { kids: [],
    insertBefore(node, ref) {
      this.kids = this.kids.filter((k) => k !== node);
      const at = ref ? this.kids.indexOf(ref) : -1;
      this.kids.splice(at < 0 ? this.kids.length : at, 0, node);
      node.parentNode = this;
    } };
  const node = (id) => ({ id, get nextSibling() { const i = parent.kids.indexOf(this); return parent.kids[i + 1] || null; } });
  const [bar, nav, banner, app] = ["bar", "tab-bar", "guest-banner", "app"].map(node);
  [bar, nav, banner, app].forEach((n) => parent.insertBefore(n, null));
  const ids = { bar, "tab-bar": nav };
  const order = () => parent.kids.map((k) => k.id).join(" ");
  const media = new Emitter();
  media.matches = true;
  let asked = null;
  mountReadingOrder({ document: { getElementById: (id) => ids[id] || null },
    window: { matchMedia: (q) => { asked = q; return media; } } });
  assert.equal(asked, READING_ORDER_QUERY);
  assert.equal(READING_ORDER_QUERY, "(min-width: 768px)");
  assert.equal(order(), "tab-bar bar guest-banner app", "desktop: the rail reads first");
  media.matches = false;
  media.emit("change");
  assert.equal(order(), "bar tab-bar guest-banner app", "phone: back after the bar, as index.html has it");
  const pane = node("split-list");
  parent.insertBefore(pane, bar);
  ids["split-list"] = pane;
  media.matches = true;
  media.emit("change");
  assert.equal(order(), "tab-bar split-list bar guest-banner app", "the rail, the list, then the detail's bar");
}

console.log("ok: shortcuts, the ? sheet, fields and no Approve key");
