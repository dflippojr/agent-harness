// Jobs list (#511): attention groups, the inline Enabled switch that saves at once with an Undo toast, and the row body
// that still opens the full form. Renders pages/jobs.mjs with the real dom.mjs helpers over the shared stub DOM.
import assert from "node:assert/strict";
import { El, Node, createDocument, walk } from "./web_stub_dom.mjs";
import { h, fill, append } from "../harness/web/lib/dom.mjs";
import { mountJobs } from "../harness/web/pages/jobs.mjs";
import { jobGroup, jobBody, lastRunPill, lastRunText, shortWhen, JOB_FIELDS } from "../harness/web/lib/jobs.mjs";

// Browsers stringify an array passed to append(), so this stub refuses one rather than flattening it.
class StrictEl extends El {
  append(...nodes) {
    assert.ok(!nodes.some(Array.isArray), "append() got a nested array; a browser would print it as text");
    super.append(...nodes);
  }
}
const { doc } = createDocument({ ElClass: StrictEl });
globalThis.document = doc;
globalThis.Node = Node;

const now = Date.now() / 1000;
const job = (id, name, extra = {}) => ({ id, name, prompt: "p", cron: "0 8 * * *", project: "homelab", backend: "local", model: "",
  notify: "low", enabled: true, catch_up_minutes: 90, next_run_at: now + 3600 * 5, last_error: null, recent: [], ...extra });

// ---- pure helpers ----
assert.equal(jobGroup(job("a", "A", { recent: [{ status: "done", job_status: "attention", created_at: now }] })), "attention");
assert.equal(jobGroup(job("a", "A", { recent: [{ status: "failed", created_at: now }] })), "attention");
assert.equal(jobGroup(job("a", "A", { recent: [{ status: "waiting_approval", created_at: now }] })), "attention");
assert.equal(jobGroup(job("a", "A", { recent: [{ status: "done", job_status: "ok", created_at: now }] })), "scheduled");
assert.equal(jobGroup(job("a", "A", { last_error: "GPU hold is on" })), "scheduled", "a start error keeps the job scheduled");
assert.equal(jobGroup(job("a", "A", { enabled: false, recent: [{ status: "failed", created_at: now }] })), "paused", "paused wins");
assert.deepEqual(lastRunPill({ status: "done", job_status: "ok" }), ["done", "OK"]);
assert.deepEqual(lastRunPill({ status: "done", job_status: "attention" }), ["waiting_approval", "Attention"]);
assert.deepEqual(lastRunPill({ status: "failed" }), ["failed", "Failed"]);
assert.equal(lastRunPill(undefined), null);
assert.match(lastRunText(now - 7 * 3600, now * 1000), /^Last run .+ · 7 h ago$/);
assert.match(lastRunText(now - 2 * 86400, now * 1000), /^Last run \S+ .+ · 2 d ago$/);
assert.ok(!shortWhen(now + 86400 * 2, now * 1000).includes(","), "short next-run text has no date clause");
{
  const far = now + 86400 * 20;
  const date = new Date(far * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  assert.ok(shortWhen(far, now * 1000).startsWith(date), "a run weeks away shows its date, not a bare weekday");
  const soon = now + 86400 * 2;
  assert.ok(shortWhen(soon, now * 1000).startsWith(new Date(soon * 1000).toLocaleDateString(undefined, { weekday: "short" })));
}
const body = jobBody(job("a", "A", { recent: [{}], next_run_at: 1 }), { enabled: false });
assert.deepEqual(Object.keys(body).sort(), [...JOB_FIELDS].sort(), "the switch sends every Job field and nothing else");
assert.equal(body.enabled, false);
assert.equal(body.catch_up_minutes, 90, "catch_up_minutes survives the toggle");

// ---- the page ----
const jobs = [
  job("j2", "Morning homelab check", { recent: [{ id: "s2", status: "done", job_status: "ok", created_at: now - 7 * 3600 }] }),
  job("j1", "Weekly disk report", { recent: [{ id: "s1", status: "done", job_status: "attention", created_at: now - 2 * 86400 }] }),
  job("j3", "Backup verification", { last_error: "GPU hold is on" }),
  job("j4", "Dependabot triage", { enabled: false }),
];
const calls = [];
let failPut = false;
let gate = null;  // while set, PUTs wait on it, so a test can hold saves in flight
const api = async (path, opts = {}) => {
  calls.push({ path, method: opts.method || "GET", body: opts.body });
  if (path === "/jobs") return structuredClone(jobs);
  const j = jobs.find((x) => path === `/jobs/${x.id}`);
  if (j && opts.method === "PUT") {
    if (gate) await gate;
    if (failPut) throw new Error("no such project: homelab");
    Object.assign(j, opts.body);
    return structuredClone(j);
  }
  if (j) return structuredClone(j);
  return [];
};
const toasts = [];
const mount = (guest) => {
  const $app = new StrictEl("main");
  const page = mountJobs({ $app, h, fill, append, api, setHeader() {}, showFab() {},
    toast: (text, ms, action) => toasts.push({ text, ms, action }), go() {}, route() {}, isGuest: () => guest,
    confirmGpuQueue: async () => true, badge: (s) => h("span", {}, s), jobStatusBadge: () => null, location: {}, confirm: () => true });
  return { $app, page };
};
const flush = () => new Promise((r) => setTimeout(r, 0));
const { $app, page } = mount(false);
await page.viewJobs();

const sections = () => walk($app, (e) => e.classList.contains("job-section")).map((e) => e.textContent);
const rows = () => walk($app, (e) => e.classList.contains("job-row"));
const toggleOf = (id) => rows().find((r) => r.attributes["data-job"] === id)
  .childNodes.find((c) => c.tagName === "LABEL").childNodes[0];
assert.deepEqual(sections(), ["Needs attention1", "Scheduled2", "Paused1"]);
assert.deepEqual(rows().map((r) => r.attributes["data-job"]), ["j1", "j2", "j3", "j4"]);
const text = $app.textContent;
assert.ok(!text.includes("⏸"), "no emoji marks a paused job");
assert.match(text, /Couldn't start: GPU hold is on/);
assert.match(text, /Paused/);

// The switch sits beside the link, never inside it, and the link still opens the full form.
for (const row of rows()) {
  const id = row.attributes["data-job"];
  const link = row.childNodes.find((c) => c.tagName === "A");
  assert.equal(link.href, `#/jobs/${id}`);
  assert.equal(walk(link, (e) => e.tagName === "INPUT").length, 0, "no control inside the row link");
  const sw = toggleOf(id);
  assert.equal(sw.attributes.role, "switch");
  assert.match(sw.attributes["aria-label"], / enabled$/);
  assert.equal(sw.disabled, false);
}
assert.equal(toggleOf("j4").checked, false);
assert.equal(toggleOf("j2").checked, true);

// Pausing saves at once: re-read, then a full-body PUT with only enabled flipped; the row moves to Paused.
calls.length = 0;
let sw = toggleOf("j2");
sw.checked = false;
sw.dispatchEvent({ type: "change" });
assert.equal(toggleOf("j2").disabled, true, "the switch waits for the save");
assert.equal(toggleOf("j2").checked, false);
await flush(); await flush();
assert.deepEqual(calls.map((c) => `${c.method} ${c.path}`), ["GET /jobs/j2", "PUT /jobs/j2"]);
assert.deepEqual(Object.keys(calls[1].body).sort(), [...JOB_FIELDS].sort());
assert.equal(calls[1].body.enabled, false);
assert.equal(calls[1].body.catch_up_minutes, 90);
assert.deepEqual(sections(), ["Needs attention1", "Scheduled1", "Paused2"]);
const undoToast = toasts.at(-1);
assert.equal(undoToast.text, "Morning homelab check paused");
assert.equal(undoToast.action.label, "Undo");
assert.ok(undoToast.ms >= 5000);

// Undo sends the reverse PUT and the row returns to Scheduled.
calls.length = 0;
undoToast.action.onClick();
await flush(); await flush();
assert.deepEqual(calls.map((c) => `${c.method} ${c.path}`), ["GET /jobs/j2", "PUT /jobs/j2"]);
assert.equal(calls[1].body.enabled, true);
assert.deepEqual(sections(), ["Needs attention1", "Scheduled2", "Paused1"]);
assert.equal(toasts.at(-1).text, "Morning homelab check resumed");
assert.equal(toasts.at(-1).action, undefined, "undoing an undo isn't offered");

// A failed save puts the switch back and says why.
failPut = true;
sw = toggleOf("j4");
sw.checked = true;
sw.dispatchEvent({ type: "change" });
await flush(); await flush();
assert.equal(toggleOf("j4").checked, false);
assert.equal(toggleOf("j4").disabled, false);
assert.equal(toasts.at(-1).text, "no such project: homelab");
assert.deepEqual(sections(), ["Needs attention1", "Scheduled2", "Paused1"]);
failPut = false;

// Two saves in flight: when one finishes and the list repaints, the other row stays locked on its pending state.
let open;
gate = new Promise((r) => { open = r; });
calls.length = 0;
sw = toggleOf("j2"); sw.checked = false; sw.dispatchEvent({ type: "change" });
await flush();
sw = toggleOf("j3"); sw.checked = false; sw.dispatchEvent({ type: "change" });
await flush();
assert.equal(toggleOf("j2").disabled, true);
assert.equal(toggleOf("j3").disabled, true);
toggleOf("j2").dispatchEvent({ type: "change" });  // a second tap on a locked row starts nothing
await flush();
assert.deepEqual(calls.map((c) => `${c.method} ${c.path}`), ["GET /jobs/j2", "PUT /jobs/j2", "GET /jobs/j3", "PUT /jobs/j3"]);
gate = null;
open();
await flush(); await flush(); await flush();
assert.equal(calls.filter((c) => c.method === "PUT").length, 2, "one PUT per row, no duplicates");
assert.deepEqual(sections(), ["Needs attention1", "Paused3"]);
for (const id of ["j2", "j3"]) { assert.equal(toggleOf(id).disabled, false); assert.equal(toggleOf(id).checked, false); }

// Guests see the switches disabled.
const guest = mount(true);
await guest.page.viewJobs();
const guestSwitches = walk(guest.$app, (e) => e.tagName === "INPUT");
assert.equal(guestSwitches.length, 4);
for (const s of guestSwitches) assert.equal(s.disabled, true);

console.log("ok");
