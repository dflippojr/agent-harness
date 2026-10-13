// Agents list paging (#663): an old session that waits on you shows under Needs you and counts toward the badge,
// "Show older" appends the next page by the server's cursor, and the machine switch asks the server for that machine.
import assert from "node:assert/strict";
import { mountSessions, sessionGroup } from "../stables/public/pages/sessions.mjs";

const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
  value: "", hidden: !!attrs?.hidden, dataset: {}, addEventListener() {}, querySelector: () => null, after() {} });
const nodes = (n) => (n && typeof n === "object" ? [n, ...(n.kids || []).flatMap(nodes)] : []);
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));
const browser = {
  window: { addEventListener() {}, removeEventListener() {} },
  document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
};
const now = Date.now() / 1000;
const row = (id, status, age, target = "tower") => ({ id, title: `Task ${id}`, status, target, project: "scratch",
  created_at: now - age, updated_at: now - age });
// The first page as the server sends it: the newest finished sessions, then an old one that waits on an approval.
const firstPage = [row("new1", "done", 10), row("new2", "done", 20), row("old-wait", "waiting_approval", 9000)];
const olderPage = [row("old1", "done", 5000), row("old2", "done", 6000)];
const calls = [];
const surfaces = new Set();
const api = async (path, { onHeaders, surface } = {}) => {
  calls.push(path);
  if (path.startsWith("/sessions")) surfaces.add(surface);
  const headers = new Map();
  let body = [];
  if (path === "/sessions") { body = firstPage; headers.set("X-Next-Before", "1760000000.5:new2"); }
  else if (path === "/sessions?before=1760000000.5%3Anew2") body = olderPage;
  else if (path === "/sessions?target=macbook") body = [row("mac1", "done", 30, "macbook")];
  else if (path === "/projects") body = [{ name: "scratch", target: "tower" }, { name: "macproj", target: "macbook" }];
  else if (path === "/gpu") body = { manual: false, state: "clear" };
  onHeaders?.({ get: (name) => headers.get(name) ?? null });
  return body;
};
let painted = [];
let switchKids = [];
const page = mountSessions({
  $app: "APP", h: el, api, ownerSurface: () => "admin", append() {}, setHeader() {}, showListAction() {}, onLeave() {},
  fill: (target, ...n) => {
    if (target?.attrs?.role === "group") switchKids = n.flat(Infinity);
    else if (target?.attrs?.class === "agent-groups") painted = n.flat(Infinity);
  },
  isMember: () => false, isGuest: () => false, badge: (s) => el("badge", {}, s), reviewBadge: () => el("rb"), REVIEW_LABEL: {},
  jobStatusBadge: () => el("jb"), onDaemonChange: () => () => {}, onDaemonState: () => () => {}, browser,
});
await page.viewList();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const section = (label) => painted.flatMap(nodes).find((n) => n.tag === "section" && n.attrs["aria-label"] === label);
const rowIds = (n) => nodes(n).filter((x) => x.attrs?.class === "agent-row").map((x) => x.attrs["data-split-key"]);

assert.deepEqual(rowIds(section("Needs you")), ["old-wait"], "the old waiting session is under Needs you");
// The rail badge counts Needs you from the same first page (lib/chrome.mjs), so it includes that session too.
assert.equal(firstPage.filter((s) => sessionGroup(s) === "needs").length, 1);
assert.deepEqual(rowIds(section("Recent")), ["new1", "new2"]);

const more = painted.flatMap(nodes).find((n) => n.tag === "button" && text(n) === "Show older");
assert.ok(more, "a next cursor shows the Show older button");
await more.attrs.onclick({ currentTarget: more });
assert.equal(calls.at(-1), "/sessions?before=1760000000.5%3Anew2", "it asks for the page before the cursor");
assert.deepEqual(rowIds(section("Recent")), ["new1", "new2", "old1", "old2"], "older rows are appended");
assert.ok(!painted.flatMap(nodes).some((n) => n.tag === "button" && text(n) === "Show older"), "the last page hides it");

const mac = switchKids.find((n) => text(n) === "MacBook");
mac.attrs.onclick();
await sleep(20);
assert.ok(calls.includes("/sessions?target=macbook"), "the machine switch filters on the server");
assert.deepEqual(rowIds(section("Recent")), ["mac1"], "and replaces the pages shown so far");
assert.deepEqual([...surfaces], ["admin"], "every page comes from the owner route, which holds the active sessions");
console.log("ok");
