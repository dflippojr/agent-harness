// Scheduled jobs pages (#258): list, create/edit form and recent runs. The shell (DOM builder, api, router, header)
// is injected by app.js so this module imports under plain Node and never reaches into another page.
import { JOB_NOTIFY, CRON_PRESETS, JOB_GROUPS, fmtWhen, whenText, cronLabel, newJobDefaults, jobGroup, shortWhen, lastRunPill,
  lastRunText, jobBody } from "../lib/jobs.mjs";
import { ago } from "../lib/format.mjs";
import * as sheets from "../lib/sheet.mjs";

export function mountJobs({ $app, h, fill, append, api, setHeader, showFab, toast, go, route, isGuest, confirmGpuQueue, badge, jobStatusBadge, location,
  confirmSheet = sheets.confirmSheet }) {
  async function viewJobs() {
    setHeader("jobs", "Jobs");
    showFab("#/jobs/new", "+ New job");
    const jobs = await api("/jobs");
    if (!jobs.length) {
      append($app, h("p", { class: "empty" }, "No scheduled jobs yet. A job runs a task on a schedule, such as a morning homelab check, and notifies you only when something needs attention."));
      return;
    }
    const list = h("div", { class: "job-groups" });
    // Job id -> the enabled state being saved. paint() rebuilds every row, so a row mid-save takes its state from here.
    const saving = new Map();
    // #511: grouped by attention, each row with an inline Enabled switch; the row body still opens the full form.
    const paint = () => fill(list, JOB_GROUPS.flatMap(([key, label]) => {
      const rows = jobs.filter((j) => jobGroup(j) === key);
      if (!rows.length) return [];
      return [h("p", { class: "section-label job-section" }, label, h("span", { class: `count${key === "attention" ? " warn" : ""}` }, String(rows.length))),
        h("div", { class: "card job-list" }, rows.map(jobRow))];
    }));

    function jobRow(j) {
      const last = j.recent?.[0];
      const pill = j.enabled ? lastRunPill(last) : ["", "Paused"];
      const next = j.enabled && j.next_run_at ? `Next ${shortWhen(j.next_run_at)}` : null;
      const toggle = h("input", { type: "checkbox", class: "switch", role: "switch", "aria-label": `${j.name} enabled`,
        disabled: isGuest() || saving.has(j.id) });
      toggle.checked = saving.has(j.id) ? saving.get(j.id) : !!j.enabled;
      toggle.addEventListener("change", () => setEnabled(j, toggle.checked, true));
      return h("div", { class: `job-row${j.enabled ? "" : " paused"}`, "data-job": j.id },
        h("a", { class: "job-main", href: `#/jobs/${j.id}` },
          h("h3", {}, j.name),
          h("div", { class: "meta" }, h("span", {}, cronLabel(j.cron)), h("span", { "aria-hidden": "true" }, "·"), h("span", {}, j.project)),
          pill || next ? h("div", { class: "meta" }, pill ? h("span", { class: `badge ${pill[0]}` }, pill[1]) : null,
            last ? h("span", {}, lastRunText(last.created_at)) : null, next ? h("span", {}, next) : null) : null,
          j.last_error ? h("div", { class: "preview bad" }, `Couldn't start: ${j.last_error}`) : null),
        h("label", { class: "job-switch" }, toggle));
    }

    // PUT replaces the whole job, so re-read it first: an edit made elsewhere since the list loaded is kept.
    async function setEnabled(j, enabled, undoable = false) {
      if (saving.has(j.id)) return;
      saving.set(j.id, enabled);
      paint();
      try {
        const fresh = await api(`/jobs/${j.id}`);
        if (fresh.enabled !== enabled) Object.assign(j, await api(`/jobs/${j.id}`, { method: "PUT", body: jobBody(fresh, { enabled }) }));
        else Object.assign(j, fresh);
      } catch (err) {
        toast(err.message, 5000);
        return;
      } finally {
        saving.delete(j.id);
        paint();
      }
      if (undoable) list.querySelector(`[data-job="${j.id}"] .switch`)?.focus();  // keep keyboard focus on the row that moved
      const said = `${j.name} ${enabled ? "resumed" : "paused"}`;
      if (undoable) toast(said, 5000, { label: "Undo", onClick: () => setEnabled(j, !enabled) });
      else toast(said);
    }
    paint();
    append($app, list);
  }

  async function viewJob(id) {
    const isNew = id === "new";
    setHeader("jobs", isNew ? "New job" : "Job", { page: true });
    const [projects, models, backends, job] = await Promise.all([
      api("/projects"), api("/models"), api("/backends?auth=skip"), isNew ? null : api(`/jobs/${id}`)]);
    const j = job || newJobDefaults(projects);
    const name = h("input", { type: "text", value: j.name, placeholder: "e.g. Morning homelab check" });
    const prompt = h("textarea", { placeholder: "e.g. Check that every homelab service is running and nothing restarted overnight. Look at the logs of anything that isn't healthy." });
    prompt.value = j.prompt;
    const custom = !CRON_PRESETS.some(([c]) => c === j.cron);
    const preset = h("select", {}, CRON_PRESETS.map(([c, label]) => h("option", { value: c, selected: c === j.cron }, label)),
      h("option", { value: "", selected: custom }, "Custom (cron)"));
    const cron = h("input", { type: "text", value: j.cron, placeholder: "minute hour day month weekday", style: "font-family:var(--mono)" });
    const cronNote = h("div", { class: "muted small", style: "margin-top:6px" });
    const project = h("select", {}, projects.filter((p) => p.target === "tower" || p.name === j.project)
      .map((p) => h("option", { value: p.name, selected: p.name === j.project }, p.description ? `${p.name} — ${p.description}` : p.name)));
    const model = h("select", {}, h("option", { value: "" }, "Default model"),
      models.map((m) => h("option", { value: m.name, selected: m.name === j.model }, m.name)));
    let localModel = (j.backend || "local") === "local" ? j.model : "";
    model.addEventListener("change", () => { localModel = model.value; });
    const backend = h("select", {}, backends.filter((b) => b.available).map((b) =>
      h("option", { value: b.name, selected: b.name === (j.backend || "local") }, b.name === "local" ? "Local model" : b.name)));
    const backendNote = h("div", { class: "muted small", style: "margin-top:6px" });
    const showBackend = () => {
      const b = backends.find((x) => x.name === backend.value);
      const isLocal = backend.value === "local";
      if (isLocal) {
        fill(model, h("option", { value: "", selected: !localModel }, "Default model"),
          models.map((m) => h("option", { value: m.name, selected: m.name === localModel }, m.name)));
      } else {
        if (!model.disabled) localModel = model.value;
        fill(model, h("option", { value: b?.model || "" }, b?.model || `${backend.value} default`));
      }
      model.disabled = !isLocal;
      backendNote.textContent = b?.billing_warning || "";
      backendNote.classList.toggle("bad", !!b?.billing_warning);
    };
    backend.addEventListener("change", showBackend);
    showBackend();
    const notify = h("select", {}, Object.entries(JOB_NOTIFY).map(([k, label]) => h("option", { value: k, selected: k === j.notify }, label)));
    const enabled = h("input", { type: "checkbox", checked: j.enabled });
    let previewTimer = null;
    const preview = () => {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(async () => {
        await previewCron(cron.value, cronNote);
      }, 250);
    };
    preset.addEventListener("change", () => { if (preset.value) { cron.value = preset.value; preview(); } else cron.focus(); });
    cron.addEventListener("input", () => {
      const match = CRON_PRESETS.find(([c]) => c === cron.value.trim());
      preset.value = match ? match[0] : "";
      preview();
    });
    preview();
    if (isGuest()) {
      [name, prompt, cron, preset, project, backend, model, notify, enabled].forEach((el) => { el.disabled = true; });
    }
    const body = () => ({ name: name.value, prompt: prompt.value, cron: cron.value, project: project.value,
      backend: backend.value, model: backend.value === "local" ? model.value : "", notify: notify.value, enabled: enabled.checked,
      ...(j.catch_up_minutes == null ? {} : { catch_up_minutes: j.catch_up_minutes }) });
    const save = h("button", { class: "btn primary", type: "submit" }, isNew ? "Create" : "Save");
    append($app, h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (isNew && backend.value === "local" && !(await confirmGpuQueue("This scheduled job"))) return;
        save.disabled = true;
        try {
          const saved = await api(isNew ? "/jobs" : `/jobs/${id}`, { method: isNew ? "POST" : "PUT", body: body() });
          toast(`Saved · next run ${whenText(saved.next_run_at)}`, 3500);
          if (isNew) location.hash = `#/jobs/${saved.id}`; else void route();
        } catch (err) { toast(err.message, 5000); }
        save.disabled = false;
      },
    },
    h("label", {}, "Name"), name,
    h("label", {}, "Task"), prompt,
    h("p", { class: "muted small" }, "The agent is asked to end with STATUS: OK or STATUS: ATTENTION, which decides how loudly you're notified. Approvals always notify."),
    h("label", {}, "Schedule (tower time)"), preset, h("div", { style: "margin-top:8px" }, cron), cronNote,
    h("label", {}, "Project"), project,
    h("label", {}, "Backend"), backend, backendNote,
    h("label", {}, "Model"), model,
    h("label", {}, "Notify me"), notify,
    h("label", { class: "row", style: "font-weight:500" }, enabled, "Enabled"),
    h("div", { class: "row", style: "margin-top:18px" },
      !isGuest() && !isNew ? h("button", {
        class: "btn bad", type: "button",
        onclick: async () => {
          if (!(await confirmSheet({ title: `Delete the job “${j.name}”?`, message: "Its past sessions stay.", confirmLabel: "Delete job",
            destructive: true }))) return;
          try { await api(`/jobs/${id}`, { method: "DELETE" }); go("#/jobs", true); } catch (err) { toast(err.message); }
        },
      }, "Delete") : null,
      h("span", { class: "spacer" }),
      !isGuest() && !isNew ? h("button", {
        class: "btn", type: "button",
        onclick: async () => {
          if (backend.value === "local" && !(await confirmGpuQueue("This job run"))) return;
          try { const s = await api(`/jobs/${id}/run`, { method: "POST" }); location.hash = `#/s/${s.id}`; } catch (err) { toast(err.message); }
        },
      }, "Run now") : null,
      isGuest() ? null : save)));
    if (job) append($app, ...recentRunsView(job));
  }

  async function previewCron(cronValue, cronNote) {
    try {
      const r = await api(`/jobs/preview?cron=${encodeURIComponent(cronValue)}`);
      cronNote.classList.toggle("bad", !r.ok);
      cronNote.textContent = r.ok ? `Next: ${r.next.map(fmtWhen).join(" · ")}` : r.error;
    } catch (_) { /* offline */ }
  }

  function recentRunsView(job) {
    return [h("h3", { style: "margin-top:28px" }, "Recent runs"),
      job.last_skip ? h("p", { class: "muted small" }, `Last skipped: ${job.last_skip}`) : null,
      job.last_error ? h("p", { class: "small bad" }, `Last start failed: ${job.last_error}`) : null,
      job.recent.length ? job.recent.map((s) => h("a", { class: "card", href: `#/s/${s.id}` },
        h("div", { class: "meta" }, badge(s.status), s.job_status ? jobStatusBadge(s.job_status) : null, h("span", {}, ago(s.created_at))),
        s.answer ? h("div", { class: "preview" }, s.answer) : null)) : h("p", { class: "muted small" }, "No runs yet.")];
  }
  return { viewJobs, viewJob };
}
