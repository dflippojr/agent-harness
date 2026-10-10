// Agents list grouping (#509): Needs you / Running / Recent membership, sort by activity, hidden empty groups, the
// inline approval line, and the rendered sections.
import assert from "node:assert/strict";
import { FAILED_NEEDS_YOU_SECONDS, groupSessions, mountSessions, sessionGroup } from "../harness/web/pages/sessions.mjs";
import { approvalLine } from "../harness/web/lib/tools.mjs";

const now = 1_800_000_000;
const s = (id, status, ago, extra = {}) => ({ id, title: id, status, updated_at: now - ago, target: "tower", project: "p", ...extra });

// Membership: approvals and fresh failures need the owner; unfinished work is Running; the rest is Recent.
assert.equal(sessionGroup(s("a", "waiting_approval", 5), now), "needs");
assert.equal(sessionGroup(s("b", "running", 5, { pending_approvals: [{ id: "x" }] }), now), "needs");
assert.equal(sessionGroup(s("c", "failed", 60), now), "needs");
assert.equal(sessionGroup(s("d", "failed", FAILED_NEEDS_YOU_SECONDS + 1), now), "recent");
for (const status of ["running", "queued", "waiting_target", "waiting_app", "waiting_limit"]) {
  assert.equal(sessionGroup(s("e", status, 5), now), "running", status);
}
for (const status of ["done", "cancelled"]) assert.equal(sessionGroup(s("f", status, 5), now), "recent", status);

// Order: groups in display order, empty ones dropped, newest activity first within a group.
const groups = groupSessions([s("old", "done", 900), s("run", "running", 50), s("new", "done", 10), s("ask", "waiting_approval", 70)], now);
assert.deepEqual(groups.map((g) => g.key), ["needs", "running", "recent"]);
assert.deepEqual(groups.map((g) => g.label), ["Needs you", "Running", "Recent"]);
assert.deepEqual(groups[2].sessions.map((x) => x.id), ["new", "old"]);
assert.deepEqual(groupSessions([s("r", "running", 1)], now).map((g) => g.key), ["running"]);
assert.deepEqual(groupSessions([], now), []);

// The pending ask on one line.
assert.equal(approvalLine({ tool: "run_shell", args: { command: "docker compose restart" } }), "$ docker compose restart");
assert.equal(approvalLine({ tool: "write_file", args: { path: "harness/web/sw.js", content: "x\ny" } }), "write_file harness/web/sw.js");
assert.equal(approvalLine({ tool: "git_clone", args: { url: "https://example.com/r.git" } }), "git clone https://example.com/r.git");
assert.equal(approvalLine({ tool: "noop", args: {} }), "noop");
const long = approvalLine({ tool: "mcp_call", args: { blob: "z".repeat(300) } });
assert.ok(long.startsWith("mcp_call {") && long.endsWith("…") && !long.includes("\n"), long);

// Rendered page: one section per non-empty group, approval rows link to the approval and show the ask.
const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
  value: "", hidden: false, dataset: {}, addEventListener() {}, querySelector: () => null });
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));
const walk = (n, out = []) => { if (n && typeof n === "object") { out.push(n); (n.kids || []).forEach((k) => walk(k, out)); } return out; };
const t = Date.now() / 1000;
const sessions = [
  { id: "s1", title: "Ship it", status: "done", updated_at: t - 7200, target: "tower", project: "web", review: "merged" },
  { id: "s2", title: "Rotate creds", status: "waiting_approval", updated_at: t - 60, target: "tower", project: "homelab",
    pending_approvals: [{ id: "a9", tool: "run_shell", args: { command: "systemctl restart ntfy" } }] },
  { id: "s3", title: "On the Mac", status: "running", updated_at: t - 30, target: "macbook", project: "web" },
];
const rendered = [];
const api = async (path) => (path === "/sessions" ? sessions : path === "/projects" ? [{ name: "web", target: "tower" }] : path === "/gpu" ? { state: "clear" } : []);
const browser = { window: { addEventListener() {}, removeEventListener() {} }, document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} } };
const page = mountSessions({
  $app: "APP", h: el, fill: (_t, ...n) => rendered.push(n.flat(Infinity)), append() {}, api, setHeader() {}, showListAction() {}, onLeave() {},
  isMember: () => false, isGuest: () => false, badge: (st) => el("badge", {}, st), reviewBadge: (_r, l) => el("rb", {}, l), REVIEW_LABEL: { merged: "merged" },
  jobStatusBadge: () => el("jb"), onDaemonChange: () => () => {}, onDaemonState: () => () => {}, browser,
});
await page.viewList();
const sections = rendered.flat().filter((n) => n && n.tag === "section");
assert.deepEqual(sections.map((n) => n.attrs["aria-label"]), ["Needs you", "Running", "Recent"]);
const rows = sections.flatMap((n) => walk(n)).filter((n) => n.tag === "a");
assert.deepEqual(rows.map((r) => r.attrs.href), ["#/s/s2/approval/a9", "#/s/s3", "#/s/s1"]);
assert.match(text(rows[0]), /\$ systemctl restart ntfy/);
assert.match(text(rows[0]), /Needs approval/);
assert.ok(walk(rows[1]).some((n) => n.attrs?.class === "agent-target"), "non-tower rows carry the laptop icon");
assert.ok(!walk(rows[2]).some((n) => n.attrs?.class === "agent-target"), "tower rows don't");
assert.match(text(sections[0]), /Needs you 1/);
console.log("ok");
