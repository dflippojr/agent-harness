// The prompt belongs to the settings form in both layouts.
import assert from "node:assert/strict";
import { appended, text } from "./web_new_task.mjs";

const walk = (n) => n && typeof n === "object" ? [n, ...(n.kids || []).flatMap(walk)] : [];
const nodes = appended.flatMap(walk);
const settings = nodes.find((n) => n.attrs.class === "new-task-settings");
const promptPane = nodes.find((n) => n.attrs.class === "new-task-prompt");
const promptInput = walk(promptPane).find((n) => n.tag === "textarea");
assert.equal(settings.tag, "form");
assert.equal(promptInput.attrs.form, settings.attrs.id, "the prompt participates in native form behavior");
assert.equal(promptInput.attrs.id, walk(promptPane).find((n) => n.tag === "label").attrs.for);
assert.ok(!walk(settings).includes(promptInput), "desktop can place the prompt separately from settings");
const layout = nodes.find((n) => n.attrs.class === "new-task-layout");
assert.deepEqual(layout.kids.map((n) => n.attrs.class), ["new-task-context", "new-task-prompt", "new-task-settings"],
  "phone flow retains context before prompt before settings");
assert.ok(walk(settings).some((n) => n.tag === "button" && n.attrs.type === "submit" && text(n) === "Start"));
console.log("ok");
