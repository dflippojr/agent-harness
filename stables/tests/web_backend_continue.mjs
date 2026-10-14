// Continue on another backend (#664): a session waiting on its subscription limit offers the move; confirming cancels
// it, then forks from its latest checkpoint on the chosen backend. Run again and Fork carry the picker's choice.
import assert from "node:assert/strict";
import { mountSession } from "../public/pages/session.mjs";
import { backendPicker } from "../public/lib/backend-choice.mjs";

const el = (tag, attrs, ...kids) => {
  const node = { tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
    value: "", hidden: !!attrs?.hidden, dataset: {}, style: {}, classList: { add() {}, remove() {}, toggle() {} }, listeners: {}, focus() {},
    querySelector: () => null, querySelectorAll: () => [], remove() {}, prepend() {}, setAttribute: (k, v) => { node.attrs[k] = v; },
    replaceChildren: (...n) => { node.kids = n; } };
  node.addEventListener = (t, fn) => { node.listeners[t] = fn; };
  if (attrs?.onclick) node.onclick = attrs.onclick;
  node.append = (...more) => node.kids.push(...more.flat(Infinity));
  return node;
};
const fill = (t, ...n) => { if (t && typeof t === "object") t.kids = n.flat(Infinity); };
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));

const backends = [{ name: "claude", available: true, model: "claude-opus-5" }, { name: "codex", available: true, model: "gpt-5.5" },
  { name: "local", available: true }, { name: "cursor", available: false, model: "auto" }];
const models = [{ name: "qwen", default: true }, { name: "gemma" }];

// The picker: "keep" leaves both to the server, a hosted backend sends no model, the local model sends the chosen one.
const keep = backendPicker({ h: el, fill, backends, models, keep: "claude" });
assert.deepEqual(keep.choice(), {});
assert.match(text(keep.backend), /Same as this session \(claude\)/);
assert.doesNotMatch(text(keep.backend), /cursor/, "unavailable backends are not offered");
keep.backend.value = "codex"; keep.backend.listeners.change();
assert.deepEqual(keep.choice(), { backend: "codex", model: "gpt-5.5" }, "the model shown is the model sent");
assert.match(text(keep.model), /gpt-5\.5/);
keep.backend.value = "local"; keep.backend.listeners.change();
assert.deepEqual(keep.choice(), { backend: "local", model: "qwen" });
const other = backendPicker({ h: el, fill, backends, models, exclude: "claude" });
assert.equal(other.backend.value, "codex", "with nothing to keep it starts on the first other backend");
assert.deepEqual(other.available.map((b) => b.name), ["codex", "local"]);

function mount(session, { formAnswer }) {
  const calls = [];
  const rendered = [];
  const barKids = [];
  const went = [];
  const forms = [];
  const api = async (path, opts = {}) => {
    calls.push([opts.method || "GET", path, opts.body]);
    if (path === `/sessions/${session.id}`) return session;
    if (path === "/backends?auth=skip") return backends;
    if (path === "/models") return models;
    if (path.endsWith("/checkpoints")) return { checkpoints: [{ turn: 2 }, { turn: 5 }, { turn: 3 }], can_fork: true };
    if (path.endsWith("/cancel")) return { ...session, status: "cancelled" };
    if (path.endsWith("/fork") || path.endsWith("/rerun")) return { id: "new1" };
    return [];
  };
  const location = {};
  const page = mountSession({
    $app: "APP", h: el, fill: (t, ...n) => { fill(t, ...n); rendered.push(...n.flat(Infinity)); }, append: (_t, ...n) => rendered.push(...n.flat(Infinity)), api,
    setHeader() {}, toast: (m) => { throw new Error(`unexpected toast: ${m}`); }, go: (to) => went.push(to), route() {}, validId: () => true,
    isGuest: () => false, isMember: () => false, isOwner: () => true, onLeave() {}, badge: (s) => el("badge", {}, s), reviewBadge: () => el("rb"),
    progressBar: () => el("bar"), openStream: () => () => {}, layoutBar() {}, viewInfo() {}, downloadDaemonFile() {},
    TERMINAL: new Set(["done", "failed", "cancelled"]), agentHarnessWeb: { url: (p) => p, token: "", sessionStreamUrl: () => "" },
    browser: { window: { addEventListener() {}, removeEventListener() {}, scrollTo() {} },
      document: { body: { append() {} }, documentElement: {}, addEventListener() {}, removeEventListener() {},
        getElementById: (id) => (id === "bar" ? { append: (...n) => barKids.push(...n) } : null), querySelector: () => null },
      requestAnimationFrame() {}, location, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0 },
    confirmSheet: async () => true,
    formSheet: async (opts) => {
      forms.push(opts);
      const answer = formAnswer(opts);
      if (!answer) return null;
      for (const f of opts.fields) if (f.control && answer[f.name] !== undefined) { f.control.value = answer[f.name]; f.control.listeners.change?.(); }
      return Object.fromEntries(opts.fields.map((f) => [f.name, f.control ? f.control.value : answer[f.name] ?? ""]));
    },
  });
  const find = (pred) => {
    const found = [];
    const walk = (n) => { if (!n || typeof n !== "object") return; if (pred(n)) found.push(n); (n.kids || []).forEach(walk); };
    rendered.forEach(walk);
    return found;
  };
  return { page, calls, went, forms, find, barKids, location };
}

const waiting = { id: "abc", title: "Port it", status: "waiting_limit", project: "scratch", target: "tower", backend: "claude",
  model: "claude-opus-5", totals: {}, context_used: 0, context_limit: 0 };
const button = (find, label) => find((n) => n.tag === "button" && text(n) === label)[0];

// A waiting_limit session shows the move; confirming cancels, then forks from the latest checkpoint on codex.
{
  const t = mount(waiting, { formAnswer: () => ({ backend: "codex" }) });
  await t.page.viewSession("abc", "info");
  const move = button(t.find, "Continue on another backend");
  assert.ok(move, "a waiting_limit session offers Continue on another backend");
  await move.onclick();
  assert.equal(t.forms.length, 1);
  assert.doesNotMatch(text(t.forms[0].fields[0].control), /claude/, "the waiting session's own backend is not offered");
  const writes = t.calls.filter(([method]) => method === "POST");
  assert.deepEqual(writes, [["POST", "/sessions/abc/cancel", undefined],
    ["POST", "/sessions/abc/checkpoints/5/fork", { prompt: "Continue the task.", backend: "codex", model: "gpt-5.5" }]]);
  // the checkpoints are read after the cancel, which is when a hosted run saves its last one
  const order = t.calls.map(([m, p]) => `${m} ${p}`);
  assert.ok(order.indexOf("POST /sessions/abc/cancel") < order.indexOf("GET /sessions/abc/checkpoints"));
  assert.deepEqual(t.went, ["#/s/new1"]);
}

// Dismissing the sheet changes nothing.
{
  const t = mount(waiting, { formAnswer: () => null });
  await t.page.viewSession("abc", "info");
  await button(t.find, "Continue on another backend").onclick();
  assert.deepEqual(t.calls.filter(([method]) => method === "POST"), []);
}

// Other states, and App sessions, don't offer it.
for (const s of [{ ...waiting, status: "running" }, { ...waiting, app_id: "app1" }]) {
  const t = mount(s, { formAnswer: () => null });
  await t.page.viewSession("abc", "info");
  assert.equal(button(t.find, "Continue on another backend"), undefined);
}

// Run again carries the picker's choice; keeping the session's backend sends an empty override.
{
  const done = { ...waiting, status: "done" };
  for (const [answer, body] of [[{ backend: "local", model: "gemma" }, { backend: "local", model: "gemma" }], [{}, {}]]) {
    const t = mount(done, { formAnswer: () => answer });
    await t.page.viewSession("abc", "info");
    const menuWrap = t.barKids.at(-1);
    menuWrap.kids[0].listeners.click();
    await menuWrap.kids[1].kids.find((b) => text(b) === "Run again as new session").onclick();
    await new Promise((r) => setImmediate(r));
    assert.deepEqual(t.calls.filter(([method]) => method === "POST"), [["POST", "/sessions/abc/rerun", body]]);
    assert.equal(t.location.hash, "#/s/new1");
  }
}
console.log("ok");
