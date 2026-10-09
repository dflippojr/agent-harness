// Renders the extracted New task page with stub deps so a missing import or dep fails CI (#258 stage h).
import assert from "node:assert/strict";
import { mountNewTask } from "../harness/web/pages/new-task.mjs";

const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
  value: "", options: [], children: [], hidden: false, addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, style: {} });
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));
const store = new Map();
const timers = [];
const browser = {
  localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) },
  location: { hash: "" },
  setInterval: (f) => { timers.push(f); return timers.length; }, clearInterval() {},
};
const calls = [];
const api = async (path) => {
  calls.push(path);
  if (path === "/projects") return [{ name: "scratch", description: "", target: "tower" }];
  if (path === "/models") return [{ name: "m1", default: true }];
  if (path === "/templates") return [{ id: "t1", name: "Tpl", project: "scratch", prompt: "do it", backend: "local" }];
  if (path.startsWith("/backends")) return [{ name: "local", available: true }, { name: "claude", available: true }];
  if (path === "/gpu") return { manual: true, manual_remaining_seconds: 120, state: "clear" };
  if (path === "/skills/enabled") return [];
  return [];
};
const appended = [];
const asked = [];
let answer = true;
const headers = [];
let leave = null;
const page = mountNewTask({
  $app: "APP", h: el, fill() {}, append: (_app, ...n) => appended.push(...n), api, setHeader: (...a) => headers.push(a), toast() {},
  route: async () => {}, isMember: () => false, isOwner: () => true, onLeave: (f) => { leave = f; },
  githubConnectionCard: () => el("gh"), warmModel: async () => {}, browser,
  confirmSheet: async (ask) => { asked.push(ask); return answer; },
});
for (const name of ["viewNew", "confirmGpuQueue"]) assert.equal(typeof page[name], "function", name);

await page.viewNew();
assert.deepEqual(headers.at(-1), ["agents", "New task", { page: true }]);
assert.ok(calls.includes("/projects") && calls.includes("/skills/enabled"));
const all = appended.map(text).join(" ");
assert.match(all, /New project/);
assert.match(all, /Manage templates/);
assert.match(all, /Save as template/);
assert.equal(typeof leave, "function");
assert.equal(await page.confirmGpuQueue("This task"), true);
assert.match(asked.at(-1).title, /^GPU hold is on/);
assert.equal(asked.at(-1).confirmLabel, "Queue it");
answer = false;
assert.equal(await page.confirmGpuQueue("This task"), false, "declining the GPU-hold sheet does not queue");
console.log("ok");
