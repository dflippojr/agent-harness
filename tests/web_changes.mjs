// Desktop Changes (#565): real DOM helpers with shared stubs exercise file navigation and inline review comments.
import assert from "node:assert/strict";
import { El as BaseEl, Node, createDocument } from "./web_stub_dom.mjs";
import { h, fill, append } from "../harness/web/lib/dom.mjs";
import { mountSession } from "../harness/web/pages/session.mjs";

class El extends BaseEl {
  get tag() { return this.tagName.toLowerCase(); }
  get attrs() { return { ...this.attributes, class: this.className }; }
  get kids() { return this.childNodes; }
  focus(opts) { this.focused = opts || true; }
  scrollIntoView(opts) { this.scrolled = opts; }
  querySelector(selector) { return walk(this).find(n => n.tag === selector); }
}
const { byId, doc } = createDocument({ ElClass: El });
globalThis.document = doc;
globalThis.Node = Node;
const text = (n) => n && typeof n === "object" ? (n.kids || []).map(text).join(" ") : String(n ?? "");
const session = { id: "abc", title: "Fix the bug", status: "running", project: "scratch", target: "tower", backend: "local", model: "m",
  totals: { prompt_tokens: 10, completion_tokens: 5 }, context_used: 0, context_limit: 0, repo_kind: "local", branch: "agent/abc", base_branch: "main" };
let changes = { removed: false, secret_scan: null, repos: [{ path: ".", branch: "agent/abc", base: "0123456789abcdef", head: "f", files: ["a.py"], commits: [],
  truncated: false, diff: "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n", parsed: null }] };
let guest = false;
let changesError = null;
const requests = [];
const api = async (path, options) => {
  requests.push({ path, options });
  if (options?.method === "POST" && path.endsWith("/review-comments")) return { id: "c1", ...options.body };
  if (path === "/sessions/abc") return session;
  if (path === "/sessions/abc/changes") { if (changesError) throw changesError; return changes; }
  return [];
};


const page = mountSession({
  $app: byId.app, h, fill, append, api, setHeader() {}, toast() {}, go() {}, route() {}, validId: () => true,
  isGuest: () => guest, isMember: () => false, isOwner: () => true, onLeave() {},
  badge: (s) => h("span", {}, s), reviewBadge: () => h("span"), progressBar: () => h("span"),
  openStream: () => () => {}, layoutBar() {}, viewInfo() {}, downloadDaemonFile() {},
  TERMINAL: new Set(["done", "failed", "cancelled"]),
  agentHarnessWeb: { url: (p) => p, token: "", sessionStreamUrl: () => "/events" },
  browser: { window: { addEventListener() {}, removeEventListener() {}, scrollTo() {} }, document: doc,
    requestAnimationFrame() {}, location: {}, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0 },
  confirmSheet: async () => true,
});
const walk = (node) => node && typeof node === "object" ? [node, ...(node.kids || []).flatMap(walk)] : [];
const layout = () => walk(byId.app).find(n => n.attrs?.class === "changes-layout");
const byClass = (name) => walk(layout()).filter(n => n.attrs?.class?.split(" ").includes(name));
const click = (node) => node._l.click[0]({ currentTarget: node });
const line = (label) => walk(layout()).find(n => n.attrs?.["aria-label"] === label);
const repo = { ...changes.repos[0], files: ["a.py", "b.py", "c.py", "d.py", "image.png"], parsed: [
  { name: "a.py", lines: [{kind:"hunk",text:"@@ -1,2 +1,2 @@",old:null,new:null},
    {kind:"del",text:"old",old:1,new:null}, {kind:"add",text:"new",old:null,new:1},
    {kind:"add",text:"second",old:null,new:2}] }],
  diff: ["a.py", "b.py", "c.py", "d.py"].map(name => "diff --git a/"+name+" b/"+name+"\n@@ -1 +1 @@\n-old\n+new").join("\n")+
    "\ndiff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n" };
changes.repos = [repo, { ...repo, path: "nested", files: ["a.py"], diff: changes.repos[0].diff }];
await page.viewSession("abc", "changes");
assert.equal(byClass("changes-sidebar").length, 1);
assert.match(text(byClass("changes-sidebar")[0]), /Review.*Changed files/);
assert.equal(byClass("changes-file-link").length, 6, "binary and nested-repository files remain navigable");
const files = byClass("file");
assert.equal(files[4].attrs.open, undefined, "large diffs start collapsed as on the phone");
await click(byClass("changes-file-link")[4]);
assert.equal(files[4].open, true, "file navigation opens a collapsed binary diff");
assert.deepEqual(files[4].scrolled, { block: "start" });
assert.deepEqual(files[4].kids[0].focused, { preventScroll: true }, "keyboard focus follows to its summary");
assert.match(text(files[4]), /Binary files/);
await click(byClass("changes-file-link")[5]);
assert.equal(byClass("changes-file-link").filter(b => b.attrs["aria-current"] === "true").length, 1, "current file is unique across repos");

// A keyboard line selection keeps its composer immediately underneath it, including range extension.
line("Comment on line 1")._l.keydown[0]({ key: "Enter", preventDefault() {} });
let composer = byClass("review-composer")[0];
let diff = byClass("diff")[0];
assert.equal(diff.kids.indexOf(composer), diff.kids.indexOf(line("Comment on line 1")) + 1);
const input = walk(composer).find(n => n.tag === "textarea");
input.value = "Keep this behavior";
input.dispatchEvent({ type: "input" });
await click(line("Comment on line 2"));
composer = byClass("review-composer")[0];
diff = byClass("diff")[0];
assert.equal(diff.kids.indexOf(composer), diff.kids.indexOf(line("Comment on line 2")) + 1);
assert.equal(walk(composer).find(n => n.tag === "textarea").value, "Keep this behavior", "extending preserves the draft text");
await click(walk(composer).find(n => n.tag === "button" && text(n) === "Add comment"));
const request = requests.find(r => r.options?.body?.comment);
assert.deepEqual(request.options.body, {repo:".",path:"a.py",side:"new",start_line:1,end_line:2,quoted:["new","second"],comment:"Keep this behavior",base:repo.base,head:repo.head});
assert.equal(byClass("review-composer").length, 0);
assert.equal(byClass("changes-file-link")[0].attrs["aria-current"], "true", "commenting marks its file current in the sidebar");
assert.match(text(byClass("review-drafts")[0]), /Keep this behavior/);
assert.equal(byClass("changes-file-link").filter(b => b.attrs["aria-current"] === "true").length, 1, "navigation selection survives a comment render");
await click(line("Comment on removed line 1"));
assert.match(text(byClass("review-composer")[0]), /removed line 1/);
await click(walk(byClass("review-composer")[0]).find(n=>n.tag==="button"&&text(n)==="Cancel"));
assert.equal(byClass("review-composer").length, 0);

// Read-only and unavailable workspaces keep their existing behaviors.
guest = true;
fill(byId.app);
await page.viewSession("abc", "changes");
assert.equal(walk(layout()).filter(n => n.attrs?.role === "button").length, 0, "guests cannot select lines or post comments");
assert.match(text(layout()), /new/);
for (const unavailable of [{removed:true}, {repos:[]}]) {
  changes = unavailable;
  fill(byId.app);
  await page.viewSession("abc", "changes");
  assert.match(text(byId.app), /Review/);
  assert.match(text(byId.app), /cleaned up|No git repositories/);
}
// A failed diff request must leave Review available instead of hiding Merge/Push/Discard.
guest = false;
fill(byId.app);
changesError = new Error("diff unavailable");
await assert.rejects(page.viewSession("abc", "changes"), /diff unavailable/);
assert.match(text(byId.app), /Review/);
assert.match(text(byId.app), /Loading changes/);
console.log("ok");
