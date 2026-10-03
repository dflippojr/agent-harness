// Renders the extracted session list page with stub deps so a missing import or dep fails CI (#258 stage i).
import assert from "node:assert/strict";
import { mountSessions } from "../harness/web/pages/sessions.mjs";

const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
  value: "", hidden: false, dataset: {}, addEventListener() {}, querySelector: () => null });
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));
const store = new Map();
const browser = {
  window: { addEventListener() {}, removeEventListener() {} },
  document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {} },
  localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) },
};
const api = async (path) => {
  if (path === "/sessions") return [{ id: "s1", title: "Fix the bug", status: "done", updated_at: new Date().toISOString(), target: "tower", project: "scratch", pending_approvals: [{ id: "a1" }] }];
  if (path === "/projects") return [{ name: "scratch", target: "tower" }];
  if (path === "/gpu") return { manual: false, state: "clear" };
  return [];
};
const rendered = [];
const headers = [];
const page = mountSessions({
  $app: "APP", h: el, fill: (_t, ...n) => rendered.push(...n.flat(Infinity)), append() {}, api, setHeader: (...a) => headers.push(a), showFab() {}, onLeave() {},
  isMember: () => false, isGuest: () => false, badge: (s) => el("badge", {}, s), reviewBadge: () => el("rb"), REVIEW_LABEL: {}, jobStatusBadge: () => el("jb"),
  openStream: () => () => {}, ownerSurface: () => ({}), agentHarnessWeb: { url: (p) => p, token: "" }, browser,
});
assert.equal(typeof page.viewList, "function");
await page.viewList();
assert.deepEqual(headers.at(-1), ["agents", "Agents"]);
assert.match(rendered.map(text).join(" "), /Fix the bug/);
console.log("ok");
