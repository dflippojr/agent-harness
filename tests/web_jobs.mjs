// Jobs list (#511): attention groups, the inline Enabled switch that saves at once with an Undo toast, and the row body
// that still opens the full form. Renders pages/jobs.mjs with the real dom.mjs helpers over the shared stub DOM.
import assert from "node:assert/strict";
import { El, Node, createDocument, walk } from "./web_stub_dom.mjs";
import { h, fill, append } from "../harness/web/lib/dom.mjs";
import { SPLITS, splitRoute } from "../harness/web/lib/layout.mjs";
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
  if (path === "/jobs" && opts.method === "POST") {
    const created = job("j5", opts.body.name, opts.body);
    jobs.push(created);
    return structuredClone(created);
  }
  if (path === "/jobs") return structuredClone(jobs);
  if (path === "/projects") return [{ name: "homelab", target: "tower" }];
  if (path === "/backends?auth=skip") return [{ name: "local", available: true }, { name: "codex", available: true, model: "remote-model" }];
  if (path === "/models") return [{ name: "local-model" }];
  if (path.startsWith("/jobs/preview")) return { ok: true, next: [now + 3600] };
  if (path.endsWith("/run")) return { id: "run1" };
  const j = jobs.find((x) => path === `/jobs/${x.id}`);
  if (j && opts.method === "PUT") {
    if (gate) await gate;
    if (failPut) throw new Error("no such project: homelab");
    Object.assign(j, opts.body);
    return structuredClone(j);
  }
  if (j && opts.method === "DELETE") { jobs.splice(jobs.indexOf(j), 1); return null; }
  if (j) return structuredClone(j);
  return [];
};
const toasts = [];
const mount = (guest) => {
  const $app = new StrictEl("main");
  const leaves = [];
  const location = {};
  const navigation = [];
  const page = mountJobs({ $app, h, fill, append, api, setHeader() {}, showListAction() {},
    toast: (text, ms, action) => toasts.push({ text, ms, action }), go: (...args) => navigation.push(args), route() {}, onLeave: (fn) => leaves.push(fn), isGuest: () => guest,
    confirmGpuQueue: async () => true, badge: (s) => h("span", {}, s), jobStatusBadge: () => null, location, confirmSheet: async () => true });
  return { $app, page, leaves, location, navigation };
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


// Jobs join the shared split without treating arbitrary paths as selected rows.
const splitJobs = SPLITS.find((s) => s.key === "jobs");
assert.deepEqual(splitRoute(["jobs"]), { split: splitJobs, selected: null });
assert.deepEqual(splitRoute(["jobs", "j1"]), { split: splitJobs, selected: "j1" });
assert.deepEqual(splitRoute(["jobs", "new"]), { split: splitJobs, selected: "new" });
assert.equal(splitRoute(["jobs", "../bad"]), null);
assert.equal(splitRoute(["jobs", "j1", "extra"]), null);

// Render into the persistent pane, then save and delete through its neighbouring form.
const desktop = mount(false);
const paneBody = new StrictEl("section");
const paneLeaves = [];
let paneHeader;
let paints = 0;
const pane = { body: paneBody, header: (...args) => { paneHeader = args; }, onLeave: (fn) => paneLeaves.push(fn), paint: () => { paints++; } };
await desktop.page.viewJobs(pane);
assert.deepEqual(paneHeader, ["Jobs", { href: "#/jobs/new", label: "+ New job" }]);
assert.equal(desktop.$app.childNodes.length, 0, "the split list never writes into the detail");
assert.equal(walk(paneBody, (e) => e.attributes["data-split-key"] === "j1").length, 1);
await desktop.page.viewJob("j1");
const controls = () => walk(desktop.$app, (e) => ["INPUT", "SELECT", "TEXTAREA"].includes(e.tagName));
const control = (id) => controls().find((e) => e.id === id);
const form = walk(desktop.$app, (e) => e.tagName === "FORM")[0];
assert.ok(walk(desktop.$app, (e) => e.classList.contains("job-runs"))[0].textContent.includes("Recent runs"));
for (const id of ["job-name", "job-task", "job-schedule", "job-cron", "job-project", "job-backend", "job-model", "job-notify"]) {
  assert.ok(control(id), id);
  assert.equal(walk(desktop.$app, (e) => e.tagName === "LABEL" && e.attributes.for === id).length, 1, "label for " + id);
}
control("job-name").value = "Edited report";
controls().find((e) => e.attributes["aria-label"] === "Job enabled").checked = false;
calls.length = 0;
form.dispatchEvent({ type: "submit" });
await flush(); await flush();
assert.equal(calls[0].method, "PUT");
assert.equal(calls[0].body.name, "Edited report");
assert.equal(calls[0].body.catch_up_minutes, 90);
assert.equal(calls[0].body.enabled, false);
assert.ok(paneBody.textContent.includes("Edited report"), "save refreshes the persistent list");
assert.ok(paints >= 2, "every repaint asks the split to mark the selection");

// Backend/model switching and Run now still work from the new toolbar.
control("job-model").value = "local-model";
control("job-model").dispatchEvent({ type: "change" });
control("job-backend").value = "codex";
control("job-backend").dispatchEvent({ type: "change" });
assert.equal(control("job-model").disabled, true);
control("job-backend").value = "local";
control("job-backend").dispatchEvent({ type: "change" });
assert.equal(control("job-model").disabled, false);
assert.ok(control("job-model").options.some((o) => o.attributes.selected !== undefined && o.value === "local-model"));
walk(desktop.$app, (e) => e.classList.contains("job-run-header"))[0].click();
await flush(); await flush();
assert.equal(desktop.location.hash, "#/s/run1");
walk(desktop.$app, (e) => e.tagName === "BUTTON" && e.textContent === "Delete")[0].click();
await flush(); await flush();
assert.ok(!paneBody.textContent.includes("Edited report"), "delete refreshes the persistent list");
assert.deepEqual(desktop.navigation.at(-1), ["#/jobs", true]);

// Guests see a read-only detail and no New action in the split pane.
const guestPane = new StrictEl("section");
await guest.page.viewJobs({ ...pane, body: guestPane });
assert.deepEqual(paneHeader, ["Jobs", null]);
await guest.page.viewJob("j2");
for (const c of walk(guest.$app, (e) => ["INPUT", "SELECT", "TEXTAREA"].includes(e.tagName))) assert.equal(c.disabled, true);
assert.equal(walk(guest.$app, (e) => e.tagName === "BUTTON").length, 0);


// Creating the first job refreshes an already-mounted empty split list.
const previousJobs = jobs.splice(0);
const first = mount(false);
const emptyPane = new StrictEl("section");
await first.page.viewJobs({ ...pane, body: emptyPane });
assert.match(emptyPane.textContent, /No scheduled jobs yet/);
await first.page.viewJob("new");
assert.equal(walk(first.$app, (e) => e.classList.contains("job-runs")).length, 0);
const firstName = walk(first.$app, (e) => e.id === "job-name")[0];
firstName.value = "First job";
walk(first.$app, (e) => e.tagName === "FORM")[0].dispatchEvent({ type: "submit" });
await flush(); await flush();
assert.equal(first.location.hash, "#/jobs/j5");
assert.match(emptyPane.textContent, /First job/);
assert.doesNotMatch(emptyPane.textContent, /No scheduled jobs yet/);
first.leaves.forEach((fn) => fn());
jobs.splice(0, jobs.length, ...previousJobs);

// Pane teardown stops subsequent list paints; form teardown cancels cron preview timers.
paneLeaves.forEach((fn) => fn());
desktop.leaves.forEach((fn) => fn());
guest.leaves.forEach((fn) => fn());

console.log("ok");
