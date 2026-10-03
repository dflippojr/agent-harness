// Renders the extracted Session Info page with stub deps so a missing import or dep fails CI (#258 stage f).
import assert from "node:assert/strict";
import { mountSessionInfo } from "../harness/web/pages/session-info.mjs";

const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined) });
const text = (n) => (typeof n === "object" ? [...n.kids].map(text).join(" ") : String(n));
const run = (session) => {
  const appended = [];
  const downloads = [];
  const { viewInfo } = mountSessionInfo({
    $app: "APP", h: el, append: (_app, ...nodes) => appended.push(...nodes),
    copyBox: (v) => el("copy", {}, v), downloadDaemonFile: (...a) => downloads.push(a),
  });
  viewInfo(session);
  return { appended, downloads };
};

const base = { id: "s1", title: "T", status: "done", project: "p", target: "tower", model: "m", created_at: 1, updated_at: 2,
  totals: { turns: 3 }, workspace: "/w" };
let { appended, downloads } = run(base);
assert.equal(appended.length, 2);
const card = text(appended[0]);
assert.match(card, /Title T/);
assert.match(card, /Backend local/);
assert.match(card, /Model turns 3/);
assert.doesNotMatch(card, /Branch/);
appended[1].attrs.onclick();
assert.deepEqual(downloads, [["/sessions/s1/transcript", "s1.md"]]);

({ appended } = run({ ...base, branch: "b", base_branch: "main", stop_reason: "limit", workspace_removed: true,
  skills: [{ slug: "x", version: 2, content_hash: "0123456789abcdef" }], trace_id: "abc" }));
const full = text(appended[0]);
assert.match(full, /done \(limit\)/);
assert.match(full, /b from main/);
assert.match(full, /x v2 \(0123456789ab\)/);
assert.match(full, /\/w \(removed\)/);
console.log("ok");
