// Agents list grouping (#509): Needs you / Running / Recent membership, sort by activity, hidden empty groups and the
// inline approval line. The rendered rows, their approval links and the ask are checked in AH-02 (tools/ui-tests).
import assert from "node:assert/strict";
import { FAILED_NEEDS_YOU_SECONDS, groupSessions, sessionGroup } from "../public/pages/sessions.mjs";
import { approvalLine } from "../public/lib/tools.mjs";

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

console.log("ok");
