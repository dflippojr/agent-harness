// #638: Tools appears only for a backend that lists bash-only, and the choice goes out as tool_profile.
import assert from "node:assert/strict";
globalThis.newTaskFixture = {
  backends: [{ name: "local", available: true }, { name: "claude", available: true, tool_profiles: ["default", "bash-only"] }],
  onPost: (_path, body) => { globalThis.sent = body; return { id: "s1" }; },
};
const { appended } = await import("./web_new_task.mjs");
const walk = (n) => n && typeof n === "object" ? [n, ...(n.kids || []).flatMap(walk)] : [];
const nodes = appended.flatMap(walk);
const select = nodes.find((n) => n.attrs.id === "new-task-tool-profile");
assert.deepEqual(select.kids.map((o) => o.attrs.value), ["default", "bash-only"]);
const fieldBox = nodes.find((n) => n.tag === "div" && n.kids.includes(select));
const backend = nodes.find((n) => n.attrs.id === "new-task-backend");
assert.equal(backend.value, "claude", "the GPU hold picks the hosted backend");
assert.equal(fieldBox.hidden, false);
const settings = nodes.find((n) => n.attrs.class === "new-task-settings");
settings.querySelectorAll = () => [];
nodes.find((n) => n.attrs.id === "new-task-prompt").value = "do it";
select.value = "bash-only";
await settings.attrs.onsubmit({ preventDefault() {} });
assert.equal(globalThis.sent.tool_profile, "bash-only");
assert.equal(globalThis.sent.backend, "claude");
console.log("ok");
