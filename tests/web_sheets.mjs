// In-app sheets (#513): confirmSheet, promptSheet and formSheet open a <dialog> on the page, resolve on the person's choice,
// validate inline, dismiss on Escape, a backdrop tap or a route change, and leave nothing behind. The bundle-update offer
// uses one without holding up boot.
import assert from "node:assert/strict";
import { confirmSheet, promptSheet, formSheet, required, wholeNumber } from "../harness/web/lib/sheet.mjs";
import { mountUpdate } from "../harness/web/lib/update.mjs";
import { createDocument, Emitter, Node, El, walk, storage } from "./web_stub_dom.mjs";

const { doc } = createDocument();
let focused = null;
El.prototype.focus = function focus() { focused = this; };
const win = new Emitter();
globalThis.document = doc;
globalThis.window = win;
globalThis.Node = Node;
const tick = () => new Promise((r) => setTimeout(r, 0));

const sheets = () => walk(doc.body, (n) => n.tagName === "DIALOG");
const only = () => {
  const open = sheets();
  assert.equal(open.length, 1, "exactly one sheet is open");
  return open[0];
};
const pick = (dialog, which) => walk(dialog, (n) => n.dataset.sheet === which || n.attributes["data-sheet"] === which)[0];
const submit = (dialog) => walk(dialog, (n) => n.tagName === "FORM")[0].dispatchEvent({ type: "submit" });
const inputs = (dialog) => walk(dialog, (n) => n.tagName === "INPUT");
const text = (n) => n.textContent;

// Confirm: the action is named, a destructive one is styled so and starts on the safe button.
{
  const answer = confirmSheet({ title: "Delete the job “Nightly”?", message: "Its past sessions stay.", confirmLabel: "Delete job", destructive: true });
  const dialog = only();
  assert.equal(dialog.className, "sheet");
  assert.match(text(dialog), /Delete the job “Nightly”\?/);
  assert.match(text(dialog), /Its past sessions stay\./);
  const ok = pick(dialog, "confirm");
  assert.equal(text(ok), "Delete job");
  assert.ok(ok.classList.contains("sheet-danger"), "destructive action uses the destructive button");
  assert.equal(text(pick(dialog, "cancel")), "Cancel");
  assert.equal(focused, pick(dialog, "cancel"), "a destructive sheet starts on Cancel");
  assert.equal(dialog.attributes.open, "", "no showModal under the stub: the sheet opens with the attribute");
  submit(dialog);
  assert.equal(await answer, true);
  assert.equal(sheets().length, 0, "a closed sheet leaves the page");
}
{
  const answer = confirmSheet({ title: "Merge?", confirmLabel: "Merge" });
  const dialog = only();
  assert.ok(pick(dialog, "confirm").classList.contains("primary"), "a plain action is the primary button");
  assert.equal(focused, pick(dialog, "confirm"));
  pick(dialog, "cancel").click();
  assert.equal(await answer, false);
  assert.equal(sheets().length, 0);
}
// Escape, a tap on the backdrop and a route change each dismiss.
{
  let answer = confirmSheet({ title: "A", confirmLabel: "Go", cancelLabel: "Keep" });
  assert.equal(text(pick(only(), "cancel")), "Keep");
  only().dispatchEvent({ type: "cancel" });
  assert.equal(await answer, false, "Escape");
  answer = confirmSheet({ title: "B", confirmLabel: "Go" });
  const dialog = only();
  dialog.dispatchEvent({ type: "click", target: walk(dialog, (n) => n.tagName === "FORM")[0] });
  assert.equal(sheets().length, 1, "a tap inside the sheet does not dismiss it");
  dialog.dispatchEvent({ type: "click", target: dialog });
  assert.equal(await answer, false, "backdrop");
  answer = confirmSheet({ title: "C", confirmLabel: "Go" });
  win.dispatchEvent({ type: "hashchange" });
  assert.equal(await answer, false, "route change");
  assert.equal(sheets().length, 0);
  assert.equal((win._l.hashchange || []).length, 0, "the route listener goes with the sheet");
}
// A second sheet replaces the first, which resolves as dismissed.
{
  const first = confirmSheet({ title: "First", confirmLabel: "Go" });
  const second = confirmSheet({ title: "Second", confirmLabel: "Go" });
  assert.match(text(only()), /Second/);
  assert.equal(await first, false);
  submit(only());
  assert.equal(await second, true);
}
// Prompt: labelled input, inline validation keeps the sheet open, then resolves the text; dismiss resolves null.
{
  const answer = promptSheet({ title: "Rename chat", label: "Title", value: "Old", confirmLabel: "Rename", validate: required("a title") });
  const dialog = only();
  const [input] = inputs(dialog);
  assert.equal(input.value, "Old");
  assert.equal(focused, input, "an input sheet starts in its field");
  const label = walk(dialog, (n) => n.tagName === "LABEL")[0];
  assert.equal(label.attributes.for, input.id);
  input.value = "   ";
  submit(dialog);
  assert.equal(sheets().length, 1, "an invalid value keeps the sheet open");
  assert.equal(input.attributes["aria-invalid"], "true");
  const error = walk(dialog, (n) => n.className === "sheet-error")[0];
  assert.equal(text(error), "Enter a title.");
  assert.equal(input.attributes["aria-describedby"], error.id);
  input.value = "New";
  submit(dialog);
  assert.equal(await answer, "New");
  assert.equal(input.attributes["aria-invalid"], undefined);
}
{
  const answer = promptSheet({ title: "Fork", label: "Instruction" });
  pick(only(), "cancel").click();
  assert.equal(await answer, null);
}
// Form: several fields in one sheet, each validated.
{
  const answer = formSheet({ title: "Concurrency", fields: [
    { name: "running", label: "Max running", value: "2", inputmode: "numeric", validate: wholeNumber(0) },
    { name: "queued", label: "Max queued", value: "x", inputmode: "numeric", validate: wholeNumber(0) },
  ] });
  const dialog = only();
  const [running, queued] = inputs(dialog);
  assert.equal(running.attributes.inputmode, "numeric");
  submit(dialog);
  assert.equal(focused, queued, "focus moves to the first invalid field");
  assert.equal(running.attributes["aria-invalid"], undefined);
  queued.value = "5";
  submit(dialog);
  assert.deepEqual(await answer, { running: "2", queued: "5" });
}
assert.equal(wholeNumber(1)("0"), "Enter a whole number, 1 or more.");
assert.equal(wholeNumber(0)("3"), "");
assert.equal(required("x")("y"), "");

// The bundle-update offer opens a sheet but does not hold up boot: checkCompatibility resolves while it is open.
{
  const session = { blocked: false, isBlocked() { return this.blocked; }, setBlocked(v) { this.blocked = v; } };
  const sessionStorage = storage();
  let reloads = 0;
  const update = mountUpdate({
    els: { $app: new El("main") }, session, chrome: { toast() {}, setHeader() {} }, tabs: { paint() {} }, route: async () => {},
    agentHarnessWeb: { compatibility: async () => ({ protocols: { admin: { min: 2, max: 2 } }, update_hint: { web: { build_id: "b2" } } }) },
    build: { WEB_BUILD_ID: "b1", WEB_PROTOCOL: 2 },
    browser: { document: doc, window: {}, navigator: {}, sessionStorage, location: { reload() { reloads++; } } },
  });
  assert.equal(await update.checkCompatibility(), true, "boot continues while the offer is open");
  const dialog = only();
  assert.match(text(dialog), /Update Agent Harness Web\?/);
  assert.equal(text(pick(dialog, "confirm")), "Update now");
  win.dispatchEvent({ type: "hashchange" });
  assert.equal(sheets().length, 1, "the boot redirect to #/agents does not dismiss the update offer");
  assert.equal(await update.checkCompatibility(), true);
  assert.equal(sheets().length, 1, "offered once per bundle");
  submit(dialog);
  await tick(); await tick();
  assert.equal(reloads, 1, "Update now reloads into the new bundle");
}
console.log("ok: in-app sheets confirm, prompt, validate and dismiss");
