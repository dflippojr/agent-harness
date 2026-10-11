// Jobs list (#511): attention groups, the inline Enabled switch that saves at once with an Undo toast, and the row body
// that still opens the full form. Renders pages/jobs.mjs with the real dom.mjs helpers over the shared stub DOM.
import assert from "node:assert/strict";
import { El, Emitter, Node, createDocument, walk } from "./web_stub_dom.mjs";
import { h, fill, append } from "../harness/web/lib/dom.mjs";
import { SPLITS, splitRoute } from "../harness/web/lib/layout.mjs";
import { mountJobs } from "../harness/web/pages/jobs.mjs";
import { jobGroup, jobBody, lastRunPill, lastRunText, shortWhen, JOB_FIELDS } from "../harness/web/lib/jobs.mjs";

// Browsers stringify an array passed to append(), so this stub refuses one rather than flattening it.
class StrictEl extends El {
  setAttribute(name, value) {
    super.setAttribute(name, value);
    if (name === "checked") { this.checked = true; this.defaultChecked = true; }
  }
  focus() { doc.activeElement = this; }
  closest(selector) {
    if (selector === ".job-row") {
      for (let el = this; el; el = el.parentNode) if (el.classList?.contains("job-row")) return el;
    }
    return null;
  }
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
let listGate = null;
let gate = null;  // while set, PUTs wait on it, so a test can hold saves in flight
const api = async (path, opts = {}) => {
  calls.push({ path, method: opts.method || "GET", body: opts.body });
  if (path === "/jobs" && opts.method === "POST") {
    const created = job("j5", opts.body.name, opts.body);
    jobs.push(created);
    return structuredClone(created);
  }
  if (path === "/jobs") {
    const snapshot = structuredClone(jobs);
    if (listGate) await listGate;
    return snapshot;
  }
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
const mount = (guest, extra = {}) => {
  const $app = new StrictEl("main");
  const leaves = [];
  const location = {};
  const navigation = [];
  const page = mountJobs({ $app, h, fill, append, api, setHeader() {}, showListAction() {},
    toast: (text, ms, action) => toasts.push({ text, ms, action }), go: (...args) => navigation.push(args), route() {}, onLeave: (fn) => leaves.push(fn), isGuest: () => guest,
    confirmGpuQueue: async () => true, badge: (s) => h("span", {}, s), jobStatusBadge: () => null, location, confirmSheet: async () => true, ...extra });
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
const detailEnabled = controls().find((e) => e.attributes["aria-label"] === "Job enabled");
const listEnabled = walk(paneBody, (e) => e.attributes["aria-label"] === "Weekly disk report enabled")[0];
listEnabled.checked = false;
listEnabled.dispatchEvent({ type: "change" });
await flush(); await flush(); await flush();
assert.equal(detailEnabled.checked, false, "pausing from the list updates the open form without losing its fields");
control("job-name").value = "Edited report";
calls.length = 0;
form.dispatchEvent({ type: "submit" });
await flush(); await flush();
const savedBody = calls.find((c) => c.method === "PUT").body;
assert.equal(savedBody.name, "Edited report");
assert.equal(savedBody.catch_up_minutes, 90);
assert.equal(savedBody.enabled, false, "renaming after a list pause cannot resume scheduled runs");
assert.ok(paneBody.textContent.includes("Edited report"), "save refreshes the persistent list");
assert.ok(paints >= 2, "every repaint asks the split to mark the selection");

// Explicit Enabled edits in the header still save, while an external change with no header edit is preserved.
detailEnabled.checked = true;
detailEnabled.dispatchEvent({ type: "change" });
form.dispatchEvent({ type: "submit" });
await flush(); await flush(); await flush();
assert.equal(jobs.find((j) => j.id === "j1").enabled, true);
// The mock route does not remount the form; reopen it to get a fresh Enabled-edit baseline.
fill(desktop.$app);
await desktop.page.viewJob("j1");
jobs.find((j) => j.id === "j1").enabled = false;
walk(desktop.$app, (e) => e.tagName === "FORM")[0].dispatchEvent({ type: "submit" });
await flush(); await flush(); await flush();
assert.equal(jobs.find((j) => j.id === "j1").enabled, false, "a late persisted pause survives a stale form save");


// A later editor Enabled choice wins when Save queues behind a pending list pause.
jobs.find((j) => j.id === "j1").enabled = true;
fill(paneBody);
await desktop.page.viewJobs(pane);
fill(desktop.$app);
await desktop.page.viewJob("j1");
let finishPause;
gate = new Promise((resolve) => { finishPause = resolve; });
const pendingPause = walk(paneBody, (e) => e.attributes["aria-label"] === "Edited report enabled")[0];
pendingPause.checked = false;
pendingPause.dispatchEvent({ type: "change" });
await flush(); await flush();
const newerEnabled = controls().find((e) => e.attributes["aria-label"] === "Job enabled");
newerEnabled.checked = true;
newerEnabled.dispatchEvent({ type: "change" });
walk(desktop.$app, (e) => e.tagName === "FORM")[0].dispatchEvent({ type: "submit" });
await flush();
gate = null;
finishPause();
await flush(); await flush(); await flush(); await flush();
assert.equal(newerEnabled.checked, true, "a list response cannot overwrite a newer editor choice");
assert.equal(jobs.find((j) => j.id === "j1").enabled, true, "queued Save persists the later editor choice");

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
let releaseList;
listGate = new Promise((resolve) => { releaseList = resolve; });
walk(desktop.$app, (e) => e.tagName === "BUTTON" && e.textContent === "Delete")[0].click();
await flush(); await flush(); await flush();
const otherSwitch = walk(paneBody, (e) => e.attributes["aria-label"] === "Morning homelab check enabled")[0];
otherSwitch.checked = true;
otherSwitch.dispatchEvent({ type: "change" });
await flush(); await flush(); await flush();
listGate = null;
releaseList();
await flush(); await flush(); await flush();
assert.equal(walk(paneBody, (e) => e.attributes["aria-label"] === "Morning homelab check enabled")[0].checked, true, "a pending membership refresh cannot overwrite a completed toggle");
assert.ok(!paneBody.textContent.includes("Edited report"), "delete refreshes the persistent list");
assert.deepEqual(desktop.navigation.at(-1), ["#/jobs", true]);

// Guests see a read-only detail and no New action in the split pane.
const guestPane = new StrictEl("section");
await guest.page.viewJobs({ ...pane, body: guestPane });
assert.deepEqual(paneHeader, ["Jobs", null]);
await guest.page.viewJob("j2");
for (const c of walk(guest.$app, (e) => ["INPUT", "SELECT", "TEXTAREA"].includes(e.tagName))) assert.equal(c.disabled, true);
assert.equal(walk(guest.$app, (e) => e.tagName === "BUTTON").length, 0);



// Deleting through a deep link before the initial list GET completes cannot leave a phantom row.
const late = mount(false);
const latePane = new StrictEl("section");
let finishInitialList;
listGate = new Promise((resolve) => { finishInitialList = resolve; });
const opening = late.page.viewJobs({ ...pane, body: latePane });
await flush();
listGate = null; // later refreshes can answer before the initial snapshot
await late.page.viewJob("j3");
walk(late.$app, (e) => e.tagName === "BUTTON" && e.textContent === "Delete")[0].click();
await flush(); await flush(); await flush();
finishInitialList();
await opening;
assert.doesNotMatch(latePane.textContent, /Backup verification/, "the late initial snapshot cannot restore a deleted job");
assert.equal(walk(latePane, (e) => e.attributes["data-job"] === "j3").length, 0);
late.leaves.forEach((fn) => fn());

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


// Fresh fixture: daemon changes and returning to Jobs refresh the persistent list; teardown removes the watchers.
const freshWindow = new Emitter();
const freshDocument = new Emitter();
freshDocument.hidden = false;
const freshLocation = { hash: "#/jobs/j2" };
let daemonChange;
let daemonStopped = false;
const live = mount(false, {
  browser: { window: freshWindow, document: freshDocument, location: freshLocation },
  onDaemonChange: (fn) => { daemonChange = fn; return () => { daemonStopped = true; }; },
});
const liveBody = new StrictEl("section");
const liveLeaves = [];
await live.page.viewJobs({ body: liveBody, header() {}, paint() {}, onLeave: (fn) => liveLeaves.push(fn) });
const changedJob = jobs.find((j) => j.id === "j2");
const beforeRun = structuredClone(changedJob.recent);
changedJob.recent = [{ id: "late-failure", status: "failed", created_at: now }];
daemonChange();
await new Promise((resolve) => setTimeout(resolve, 350));
assert.match(liveBody.textContent, /Failed/, "a run failure repaints the mounted Jobs list");
changedJob.recent = [{ id: "late-ok", status: "done", job_status: "ok", created_at: now }];
freshLocation.hash = "#/jobs";
freshWindow.dispatchEvent({ type: "hashchange" });
await new Promise((resolve) => setTimeout(resolve, 350));
assert.match(liveBody.textContent, /OK/, "returning to Jobs also refreshes, even if a daemon event was missed");
liveLeaves.forEach((fn) => fn());
assert.equal(daemonStopped, true);
calls.length = 0;
daemonChange();
freshWindow.dispatchEvent({ type: "hashchange" });
await new Promise((resolve) => setTimeout(resolve, 350));
assert.equal(calls.length, 0, "a disposed pane does not fetch or repaint");
changedJob.recent = beforeRun;


// Fresh fixture: a failed notification read cannot erase a successful initial load, and background paints keep focus.
const stableJob = job("stable", "Stable job", { recent: [], next_run_at: null });
let reads = 0;
let resolveInitial;
let notifyStable;
const initialReply = new Promise((resolve) => { resolveInitial = resolve; });
const stable = mount(false, {
  api: async (path) => {
    assert.equal(path, "/jobs");
    reads++;
    if (reads === 1) return initialReply;
    if (reads === 2) throw new Error("Offline");
    return structuredClone([stableJob]);
  },
  onDaemonChange: (fn) => { notifyStable = fn; return () => {}; },
});
const stableBody = new StrictEl("section");
const stableLeaves = [];
const stableOpening = stable.page.viewJobs({ body: stableBody, header() {}, paint() {}, onLeave: (fn) => stableLeaves.push(fn) });
notifyStable();
await new Promise((resolve) => setTimeout(resolve, 350));
assert.equal(reads, 1, "a notification queues behind the initial request");
resolveInitial(structuredClone([stableJob]));
await stableOpening;
assert.match(stableBody.textContent, /Stable job/, "the initial success remains visible when the queued background read fails");
const stableList = walk(stableBody, (e) => e.classList.contains("job-groups"))[0];
stableList.querySelector = (selector) => {
  const id = selector.split('"')[1];
  const row = walk(stableBody, (e) => e.attributes["data-job"] === id)[0];
  const kind = selector.endsWith(".switch") ? "switch" : "job-main";
  return walk(row, (e) => e.classList.contains(kind))[0] || null;
};
const stableSwitch = walk(stableBody, (e) => e.classList.contains("switch"))[0];
stableSwitch.focus();
notifyStable();
await new Promise((resolve) => setTimeout(resolve, 350));
assert.equal(walk(stableBody, (e) => e.classList.contains("switch"))[0], stableSwitch, "unchanged data keeps native row elements");
assert.equal(doc.activeElement, stableSwitch);
stableJob.name = "Renamed stable job";
notifyStable();
await new Promise((resolve) => setTimeout(resolve, 350));
const newStableSwitch = walk(stableBody, (e) => e.classList.contains("switch"))[0];
assert.notEqual(newStableSwitch, stableSwitch);
assert.equal(doc.activeElement, newStableSwitch, "a changed row restores focus to the same job control");
stableLeaves.forEach((fn) => fn());


// These race scenarios own their data, requests, events, and both pane/form cleanups.
async function jobFixture(seed, {
  readList = async (data) => structuredClone(data),
  readJob = async (item) => structuredClone(item),
  putJob = async (item, body) => { Object.assign(item, body); return structuredClone(item); },
  runJob = async () => ({ id: "fixture-run" }),
  confirmGpuQueue = async () => true,
} = {}) {
  const data = structuredClone(seed);
  const requests = [];
  const notices = [];
  const window = new Emitter();
  const document = new Emitter();
  Object.defineProperty(document, "activeElement", { get: () => doc.activeElement });
  document.hidden = false;
  let notify;
  let listReads = 0;
  const fixture = mount(false, {
    browser: { window, document, location: { hash: "#/jobs" } },
    confirmGpuQueue,
    onDaemonChange: (fn) => { notify = fn; return () => {}; },
    toast: (message) => notices.push(message),
    api: async (path, opts = {}) => {
      requests.push({ path, method: opts.method || "GET", body: opts.body });
      if (path === "/jobs") return readList(data, ++listReads);
      if (path === "/projects") return [{ name: "homelab", target: "tower" }];
      if (path === "/models") return [];
      if (path === "/backends?auth=skip") return [{ name: "local", available: true }];
      if (path.startsWith("/jobs/preview")) return { ok: true, next: [] };
      if (path.endsWith("/run")) return runJob();
      const item = data.find((j) => path === "/jobs/" + j.id);
      assert.ok(item, path);
      if (opts.method === "PUT") return putJob(item, opts.body);
      return readJob(item);
    },
  });
  const body = new StrictEl("section");
  const leaves = [];
  await fixture.page.viewJobs({ body, header() {}, paint() {}, onLeave: (fn) => leaves.push(fn) });
  return { ...fixture, data, body, document, requests, notices, notify: () => notify(),
    readCount: () => listReads, cleanup: () => [...leaves, ...fixture.leaves].forEach((fn) => fn()) };
}




// Header and phone-footer Run now share one pending action across confirmation and the request.
let approveRun;
let finishRun;
let allowed = true;
let runFails = false;
let confirmations = 0;
const confirmReply = new Promise((resolve) => { approveRun = resolve; });
const runReply = new Promise((resolve) => { finishRun = resolve; });
const runningJob = await jobFixture([job("run-job", "Run job")], {
  confirmGpuQueue: async () => { confirmations++; await confirmReply; return allowed; },
  runJob: async () => { await runReply; if (runFails) throw new Error("Run unavailable"); return { id: "new-run" }; },
});
try {
  await runningJob.page.viewJob("run-job");
  const buttons = walk(runningJob.$app, (e) => e.classList.contains("job-run-header") || e.classList.contains("job-run-footer"));
  const runRequests = () => runningJob.requests.filter((r) => r.path.endsWith("/run"));
  buttons[0].click();
  assert.ok(buttons.every((b) => b.disabled), "both responsive buttons lock before GPU confirmation");
  buttons[1].click();
  assert.equal(confirmations, 1);
  approveRun();
  await flush(); await flush();
  assert.equal(runRequests().length, 1);
  assert.ok(buttons.every((b) => b.disabled), "both buttons stay locked during the request");
  buttons[1].click();
  await flush();
  assert.equal(runRequests().length, 1, "resizing cannot enqueue another run");
  finishRun();
  await flush(); await flush();
  assert.equal(runningJob.location.hash, "#/s/new-run");
  assert.ok(buttons.every((b) => !b.disabled));
  allowed = false;
  buttons[1].click();
  await flush(); await flush();
  assert.equal(runRequests().length, 1, "cancelled GPU confirmation starts no run");
  assert.ok(buttons.every((b) => !b.disabled), "cancellation releases both buttons");
  allowed = true;
  runFails = true;
  buttons[1].click();
  await flush(); await flush();
  assert.match(runningJob.notices.at(-1), /Run unavailable/);
  assert.ok(buttons.every((b) => !b.disabled), "a failed run releases both buttons");
} finally { approveRun(); finishRun(); runningJob.cleanup(); }

// A switch started in a disposed narrow list refreshes its replacement after resizing to split view.
let finishResizedToggle;
const resizedReply = new Promise((resolve) => { finishResizedToggle = resolve; });
const replacingList = await jobFixture([job("resized", "Resize job")], { putJob: async (item, body) => {
  await resizedReply;
  Object.assign(item, body);
  return structuredClone(item);
} });
const replacementBody = new StrictEl("section");
const replacementLeaves = [];
try {
  const narrowSwitch = walk(replacingList.body, (e) => e.classList.contains("switch"))[0];
  narrowSwitch.checked = false;
  narrowSwitch.dispatchEvent({ type: "change" });
  await flush(); await flush();
  replacingList.cleanup();
  await replacingList.page.viewJobs({ body: replacementBody, header() {}, paint() {}, onLeave: (fn) => replacementLeaves.push(fn) });
  assert.equal(walk(replacementBody, (e) => e.classList.contains("switch"))[0].checked, true, "the replacement first reads the pending toggle's old state");
  finishResizedToggle();
  await flush(); await flush(); await flush();
  assert.equal(replacingList.data[0].enabled, false);
  assert.equal(walk(replacementBody, (e) => e.classList.contains("switch"))[0].checked, false,
    "a completed toggle refreshes the currently mounted list");
} finally { finishResizedToggle(); replacementLeaves.forEach((fn) => fn()); replacingList.cleanup(); }

// A list toggle completed during detail loading wins over the earlier detail snapshot.
let releaseDetail;
const detailReply = new Promise((resolve) => { releaseDetail = resolve; });
let detailReads = 0;
const mountingForm = await jobFixture([job("mounting", "Loading job")], { readJob: async (item) => {
  if (++detailReads === 1) {
    const snapshot = structuredClone(item);
    await detailReply;
    return snapshot;
  }
  return structuredClone(item);
} });
try {
  const openingDetail = mountingForm.page.viewJob("mounting");
  await flush();
  const listSwitch = walk(mountingForm.body, (e) => e.classList.contains("switch"))[0];
  listSwitch.checked = false;
  listSwitch.dispatchEvent({ type: "change" });
  await flush(); await flush(); await flush();
  assert.equal(mountingForm.data[0].enabled, false);
  releaseDetail();
  await openingDetail;
  const formSwitch = walk(mountingForm.$app, (e) => e.attributes["aria-label"] === "Job enabled")[0];
  assert.equal(formSwitch.checked, false, "a newly mounted form adopts a toggle completed while loading");
} finally { releaseDetail(); mountingForm.cleanup(); }

// A failed notification request must still run the Save refresh queued behind it.
let failBackground;
const backgroundReply = new Promise((resolve, reject) => { failBackground = reject; });
const queuedSave = await jobFixture([job("queued", "Before save")],
  { readList: async (data, count) => count === 2 ? backgroundReply : structuredClone(data) });
try {
  await queuedSave.page.viewJob("queued");
  queuedSave.notify();
  await new Promise((resolve) => setTimeout(resolve, 350));
  assert.equal(queuedSave.readCount(), 2);
  walk(queuedSave.$app, (e) => e.id === "job-name")[0].value = "After save";
  walk(queuedSave.$app, (e) => e.tagName === "FORM")[0].dispatchEvent({ type: "submit" });
  await flush(); await flush();
  assert.ok(queuedSave.requests.some((r) => r.method === "PUT" && r.body.name === "After save"));
  failBackground(new Error("Offline"));
  await flush(); await flush(); await flush();
  assert.equal(queuedSave.readCount(), 3, "a failed background read cannot discard a queued Save refresh");
  assert.match(queuedSave.body.textContent, /After save/);
  assert.doesNotMatch(queuedSave.body.textContent, /Before save/);
} finally { queuedSave.cleanup(); }

// Updates wait until after the native pointer click, for both links and switches.
const pressedRows = await jobFixture([job("pressed", "Pressed job"), job("other", "Other job")]);
try {
  const list = walk(pressedRows.body, (e) => e.classList.contains("job-groups"))[0];
  const rowControl = (kind) => walk(pressedRows.body, (e) => e.attributes["data-job"] === "pressed")
    .flatMap((row) => walk(row, (e) => e.classList.contains(kind)))[0];
  for (const kind of ["job-main", "switch"]) {
    const control = rowControl(kind);
    let clicks = 0;
    control.addEventListener("click", () => { clicks++; });
    list.dispatchEvent({ type: "pointerdown", target: control });
    pressedRows.data[1].name += " changed";
    pressedRows.notify();
    await new Promise((resolve) => setTimeout(resolve, 350));
    assert.equal(rowControl(kind), control, "a background response preserves the pressed " + kind);
    pressedRows.document.dispatchEvent({ type: "pointerup" });
    assert.equal(rowControl(kind), control, "pointerup keeps the element for the following click");
    control.click();
    assert.equal(clicks, 1);
    await flush();
    assert.match(pressedRows.body.textContent, /Other job changed/);
    assert.notEqual(rowControl(kind), control, "the deferred update paints after the click");
  }
} finally { pressedRows.cleanup(); }

// Fresh fixture: completing a toggle must not take focus away from a neighbouring editor.
const focusPage = mount(false);
await focusPage.page.viewJobs();
await focusPage.page.viewJob("j2");
const focusSwitch = walk(focusPage.$app, (e) => e.attributes["aria-label"] === "Morning homelab check enabled")[0];
const taskEditor = walk(focusPage.$app, (e) => e.id === "job-task")[0];
const focusedList = walk(focusPage.$app, (e) => e.classList.contains("job-groups"))[0];
focusedList.querySelector = (selector) => {
  const id = selector.split('"')[1];
  const row = walk(focusedList, (e) => e.attributes["data-job"] === id)[0];
  return walk(row, (e) => e.classList.contains("switch"))[0] || null;
};
let releaseFocusSave;
gate = new Promise((resolve) => { releaseFocusSave = resolve; });
focusSwitch.focus();
focusSwitch.checked = !focusSwitch.checked;
focusSwitch.dispatchEvent({ type: "change" });
await flush(); await flush();
taskEditor.focus();
gate = null;
releaseFocusSave();
await flush(); await flush(); await flush();
assert.equal(doc.activeElement, taskEditor, "a completed toggle preserves the reader's newer focus");
focusPage.leaves.forEach((fn) => fn());

// Pane teardown stops subsequent list paints; form teardown cancels cron preview timers.
paneLeaves.forEach((fn) => fn());
desktop.leaves.forEach((fn) => fn());
guest.leaves.forEach((fn) => fn());

console.log("ok");
