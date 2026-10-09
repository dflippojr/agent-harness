// Renders the extracted session page (transcript and Changes tab) with stub deps so a missing import or dep fails CI (#258 stage j).
import assert from "node:assert/strict";
import { mountSession } from "../harness/web/pages/session.mjs";

const el = (tag, attrs, ...kids) => {
  const node = { tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
    value: "", hidden: false, dataset: {}, style: {}, classList: { add() {}, remove() {} }, addEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], remove() {}, prepend() {} };
  node.append = (...more) => node.kids.push(...more.flat(Infinity));
  Object.defineProperty(node, "childNodes", { get: () => node.kids });
  return node;
};
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));

const session = { id: "abc", title: "Fix the bug", status: "running", project: "scratch", target: "tower", backend: "local", model: "m",
  totals: { prompt_tokens: 10, completion_tokens: 5 }, context_used: 0, context_limit: 0, repo_kind: "local", branch: "agent/abc", base_branch: "main" };
const changes = { removed: false, secret_scan: null, repos: [{ path: ".", branch: "agent/abc", base: "0123456789abcdef", head: "f", files: ["a.py"], commits: [],
  truncated: false, diff: "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n", parsed: null }] };
const api = async (path) => {
  if (path === "/sessions/abc") return session;
  if (path === "/sessions/abc/changes") return changes;
  return [];
};

const rendered = [];
const headers = [];
const streams = [];
const body = [];
const page = mountSession({
  $app: "APP", h: el, fill: (_t, ...n) => rendered.push(...n.flat(Infinity)), append: (_t, ...n) => rendered.push(...n.flat(Infinity)), api,
  setHeader: (...a) => headers.push(a), toast() {}, go() {}, route() {}, validId: () => true, isGuest: () => false, isMember: () => false, isOwner: () => true,
  onLeave() {}, badge: (s) => el("badge", {}, s), reviewBadge: () => el("rb"), progressBar: () => el("bar"),
  openStream: (_url, handlers) => { streams.push(handlers); return () => {}; },
  layoutBar() {}, viewInfo() {}, TERMINAL: new Set(["done", "failed", "cancelled"]), agentHarnessWeb: { url: (p) => p, token: "", sessionStreamUrl: (id, n) => `/s/${id}?${n}` },
  browser: { window: { addEventListener() {}, removeEventListener() {}, scrollTo() {} },
    document: { body: { append: (...n) => body.push(...n) }, documentElement: {}, addEventListener() {}, removeEventListener() {} },
    requestAnimationFrame() {}, location: {}, confirm: () => true, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0 },
});
assert.equal(typeof page.viewSession, "function");

await page.viewSession("abc", "transcript");
assert.deepEqual(headers.at(-1), ["agents", "Fix the bug"]);
assert.equal(streams.length, 1);
streams[0].user_message({ seq: 1, data: { content: "please fix it" } });
streams[0].status({ seq: 2, data: { status: "done", answer: "All fixed" } });
const transcript = rendered.map(text).join(" ");
assert.match(transcript, /please fix it/);
assert.match(transcript, /done/);
assert.ok(body.length, "the composer is attached to the body");

// A pending approval is a sheet on the body, not an inline card; it needs Deny/Approve and yields the composer.
const composerEl = body.find((n) => n.attrs?.class === "composer");
const before = body.length;
streams[0].approval_requested({ seq: 3, data: { id: "ap1", tool: "write_file", tool_call_id: "c1", reason: "Edit outside allowlist",
  detail: "@@ -1 +1 @@\n-a\n+b", args: { path: "sw.js" }, smart: { recommendation: "approve", confidence: 0.92, reason: "routine" } } });
assert.equal(body.length, before + 1, "the approval sheet is attached to the body");
const sheet = body.at(-1);
assert.equal(sheet.attrs.class, "approval-sheet");
assert.equal(composerEl.hidden, true, "the composer yields while a decision is pending");
const sheetText = text(sheet);
// The stub append() collects the Deny/Approve row into `rendered`.
const actionsText = rendered.map(text).join(" ");
assert.match(actionsText, /Deny/);
assert.match(actionsText, /Approve/);
assert.match(sheetText, /Reviewer: approve · 92%/);
assert.match(sheetText, /Add a note for the agent/);
assert.match(sheetText, /Show in transcript/);
let removed = false;
sheet.remove = () => { removed = true; };
streams[0].approval_decided({ seq: 4, data: { id: "ap1", status: "approved" } });
assert.ok(removed, "deciding removes the sheet");
assert.equal(composerEl.hidden, false, "the composer returns after the decision");

// A newer request supersedes an older orphaned one.
streams[0].approval_requested({ seq: 10, data: { id: "ap3", tool: "write_file", tool_call_id: "c3", reason: "First", detail: "x", args: {} } });
const stale = body.at(-1);
let staleRemoved = false;
stale.remove = () => { staleRemoved = true; };
streams[0].approval_requested({ seq: 11, data: { id: "ap4", tool: "write_file", tool_call_id: "c4", reason: "Second", detail: "x", args: {} } });
assert.ok(staleRemoved, "an older sheet is removed when a newer approval arrives");
body.at(-1).remove = () => {};
streams[0].approval_decided({ seq: 12, data: { id: "ap4", status: "denied" } });
assert.equal(composerEl.hidden, false, "deciding the newest approval frees the composer even with an orphaned older one");

// A run cancelled elsewhere never emits approval_decided; the status change must clear the sheet and bring the composer back.
streams[0].approval_requested({ seq: 13, data: { id: "ap2", tool: "write_file", tool_call_id: "c2", reason: "Edit", detail: "x", args: {} } });
const sheet2 = body.at(-1);
assert.match(text(sheet2), /Cancel the whole task/);
assert.equal(composerEl.hidden, true);
let removed2 = false;
sheet2.remove = () => { removed2 = true; };
streams[0].status({ seq: 14, data: { status: "cancelled" } });
assert.ok(removed2, "a status change clears the stale sheet");
assert.equal(composerEl.hidden, false);

rendered.length = 0;
await page.viewSession("abc", "changes");
const changesText = rendered.map(text).join(" ");
assert.match(changesText, /Review/);
assert.match(changesText, /a\.py/);
console.log("ok");
