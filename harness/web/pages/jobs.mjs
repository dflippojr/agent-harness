// Scheduled jobs pages (#258): list, create/edit form and recent runs. The shell (DOM builder, api, router, header)
// is injected by app.js so this module imports under plain Node and never reaches into another page.
import { JOB_NOTIFY, CRON_PRESETS, JOB_GROUPS, fmtWhen, whenText, cronLabel, newJobDefaults, jobGroup, shortWhen, lastRunPill,
  lastRunText, jobBody } from "../lib/jobs.mjs";
import { ago } from "../lib/format.mjs";
import * as sheets from "../lib/sheet.mjs";

export function mountJobs({ $app, h, fill, append, api, setHeader, showListAction, toast, go, route, isGuest, confirmGpuQueue, badge, jobStatusBadge, location,
  confirmSheet = sheets.confirmSheet, onLeave = () => {}, onDaemonChange = null, browser = globalThis }) {
  let refreshList = null;
  let openForm = null;
  const enabledUpdates = new Map();
  // Serialize writes to one job: the list switch and its open editor must not overwrite each other.
  const writes = new Map();
  function writeJob(id, action) {
    const pending = (writes.get(id) || Promise.resolve()).catch(() => {}).then(action);
    writes.set(id, pending);
    return pending.finally(() => { if (writes.get(id) === pending) writes.delete(id); });
  }
  async function viewJobs(pane = null) {
    const target = pane?.body || $app;
    if (pane) pane.header("Jobs", isGuest() ? null : { href: "#/jobs/new", label: "+ New job" });
    else { setHeader("jobs", "Jobs"); showListAction("#/jobs/new", "+ New job"); }
    let gone = false;
    const leave = pane ? pane.onLeave : onLeave;
    leave(() => { gone = true; if (refreshList === refresh) refreshList = null; });
    let jobs = [];
    const list = h("div", { class: "job-groups" });
    // Job id -> the enabled state being saved. Repaints retain every in-flight switch.
    const saving = new Map();
    const toggleVersions = new Map();
    let paintKey = null;
    let loaded = false;
    let refreshing = null;
    let refreshAgain = false;
    let holdPaint = false;
    let nextNoisy = false;
    let pressed = false;
    let paintPending = false;
    let releaseTimer = null;
    const paint = () => {
      if (gone) return;
      if (pressed) { paintPending = true; return; }
      paintPending = false;
      const key = JSON.stringify(jobs.map((j) => [j.id, j.name, j.cron, j.project, j.enabled, j.next_run_at, j.last_error,
        j.recent?.[0], saving.has(j.id), saving.get(j.id), j.recent?.[0] ? lastRunText(j.recent[0].created_at) : null,
        j.next_run_at ? shortWhen(j.next_run_at) : null]));
      if (key === paintKey) { pane?.paint(); return; }
      paintKey = key;
      const active = browser.document?.activeElement;
      const row = active?.closest?.(".job-row");
      const focusedId = row?.getAttribute("data-job");
      const focusedControl = active?.classList?.contains("switch") ? ".switch" : active?.classList?.contains("job-main") ? ".job-main" : null;
      fill(list, jobs.length ? JOB_GROUPS.flatMap(([key, label]) => {
        const rows = jobs.filter((j) => jobGroup(j) === key);
        if (!rows.length) return [];
        return [h("p", { class: "section-label job-section" }, label, h("span", { class: `count${key === "attention" ? " warn" : ""}` }, String(rows.length))),
          h("div", { class: "card job-list" }, rows.map(jobRow))];
      }) : h("p", { class: "empty" }, "No scheduled jobs yet. A job runs a task on a schedule, such as a morning homelab check, and notifies you only when something needs attention."));
      pane?.paint();
      if (focusedId && focusedControl) {
        const control = list.querySelector(`[data-job="${focusedId}"] ${focusedControl}`);
        if (control && !control.disabled) control.focus();
      }
    };
    // Detail routes keep the split list mounted; successful edits refresh it explicitly.
    // Coalesce concurrent reads. A background notification cannot supersede the initial success, and an edit/delete
    // waits for a read made after that mutation instead of painting an obsolete response.
    async function refresh({ initial = false, quiet = false } = {}) {
      if (gone) return;
      if (refreshing) {
        refreshAgain = true;
        holdPaint ||= !quiet;
        nextNoisy ||= !quiet;
      } else refreshing = load(!quiet).finally(() => { refreshing = null; });
      try { await refreshing; }
      catch (err) {
        if (gone) return;
        if (initial) throw err; // the split framework supplies its load error and Retry action
        if (!quiet) toast(`Couldn't refresh jobs: ${err.message}`, 5000);
      }
    }

    async function load(noisy) {
      do {
        refreshAgain = false;
        holdPaint = false;
        nextNoisy = false;
        const toggles = new Map(toggleVersions);
        try {
          const fresh = await api("/jobs");
          if (gone) return;
          if (!holdPaint) {
            const existing = new Map(jobs.map((j) => [j.id, j]));
            jobs = fresh.map((j) => {
              const old = existing.get(j.id);
              // Preserve a switch changed during the read, while adopting create/delete membership changes.
              if (old && (saving.has(j.id) || toggles.get(j.id) !== toggleVersions.get(j.id))) return old;
              return Object.assign(old || {}, j);
            });
            paint();
            loaded = true;
          }
        } catch (err) {
          if (gone) return;
          if (refreshAgain) { noisy = nextNoisy; continue; }
          if (!loaded) throw err;
          if (noisy) toast(`Couldn't refresh jobs: ${err.message}`, 5000);
          return; // a failed background read retains the last successful list
        }
        noisy = nextNoisy;
      } while (refreshAgain && !gone);
    }
    refreshList = refresh;
    // Keep a pressed native control alive through its click. A daemon update may arrive between pointerdown/up.
    const press = (event) => {
      if (!event.target?.closest?.(".job-row")) return;
      clearTimeout(releaseTimer);
      pressed = true;
    };
    const release = () => {
      if (!pressed) return;
      clearTimeout(releaseTimer);
      releaseTimer = setTimeout(() => {
        pressed = false;
        if (paintPending) paint();
      }, 0);
    };
    list.addEventListener("pointerdown", press);
    browser.document?.addEventListener("pointerup", release);
    browser.document?.addEventListener("pointercancel", release);
    browser.window?.addEventListener("blur", release);
    leave(() => {
      clearTimeout(releaseTimer);
      list.removeEventListener("pointerdown", press);
      browser.document?.removeEventListener("pointerup", release);
      browser.document?.removeEventListener("pointercancel", release);
      browser.window?.removeEventListener("blur", release);
    });
    if (pane) {
      let refreshTimer = null;
      const schedule = () => {
        if (gone) return;
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => void refresh({ quiet: true }), 300);
      };
      const stop = onDaemonChange?.(schedule);
      const poll = setInterval(() => { if (!browser.document?.hidden) schedule(); }, 60000);
      const backToList = () => { if (browser.location?.hash === "#/jobs") schedule(); };
      const visible = () => { if (!browser.document?.hidden) schedule(); };
      browser.window?.addEventListener("hashchange", backToList);
      browser.document?.addEventListener("visibilitychange", visible);
      leave(() => {
        stop?.();
        clearTimeout(refreshTimer);
        clearInterval(poll);
        browser.window?.removeEventListener("hashchange", backToList);
        browser.document?.removeEventListener("visibilitychange", visible);
      });
    }

    function jobRow(j) {
      const last = j.recent?.[0];
      const pill = j.enabled ? lastRunPill(last) : ["", "Paused"];
      const next = j.enabled && j.next_run_at ? `Next ${shortWhen(j.next_run_at)}` : null;
      const toggle = h("input", { type: "checkbox", class: "switch", role: "switch", "aria-label": `${j.name} enabled`,
        disabled: isGuest() || saving.has(j.id) });
      toggle.checked = saving.has(j.id) ? saving.get(j.id) : !!j.enabled;
      toggle.addEventListener("change", () => setEnabled(j, toggle.checked, true, toggle));
      return h("div", { class: `job-row${j.enabled ? "" : " paused"}`, "data-job": j.id, "data-split-key": j.id },
        h("a", { class: "job-main", href: `#/jobs/${j.id}` },
          h("h3", {}, j.name),
          h("div", { class: "meta" }, h("span", {}, cronLabel(j.cron)), h("span", { "aria-hidden": "true" }, "·"), h("span", {}, j.project)),
          pill || next ? h("div", { class: "meta" }, pill ? h("span", { class: `badge ${pill[0]}` }, pill[1]) : null,
            last ? h("span", {}, lastRunText(last.created_at)) : null, next ? h("span", {}, next) : null) : null,
          j.last_error ? h("div", { class: "preview bad" }, `Couldn't start: ${j.last_error}`) : null),
        h("label", { class: "job-switch" }, toggle));
    }

    // PUT replaces the whole job, so re-read it first: an edit made elsewhere since the list loaded is kept.
    async function setEnabled(j, enabled, undoable = false, source = null) {
      if (saving.has(j.id)) return;
      const hadFocus = source && browser.document?.activeElement === source;
      const form = openForm?.id === j.id ? openForm : null;
      const formRevision = form?.revision();
      toggleVersions.set(j.id, (toggleVersions.get(j.id) || 0) + 1);
      saving.set(j.id, enabled);
      paint();
      try {
        await writeJob(j.id, async () => {
          const fresh = await api(`/jobs/${j.id}`);
          if (fresh.enabled !== enabled) Object.assign(j, await api(`/jobs/${j.id}`, { method: "PUT", body: jobBody(fresh, { enabled }) }));
          else Object.assign(j, fresh);
          enabledUpdates.set(j.id, { enabled: j.enabled });
          if (openForm?.id === j.id) openForm.syncEnabled(j.enabled, openForm === form ? formRevision : 0);
        });
        if (refreshList && refreshList !== refresh) await refreshList();
      } catch (err) {
        toast(err.message, 5000);
        return;
      } finally {
        toggleVersions.set(j.id, (toggleVersions.get(j.id) || 0) + 1);
        saving.delete(j.id);
        paint();
      }
      const active = browser.document?.activeElement;
      if (undoable && hadFocus && (active === source || active === browser.document?.body)) {
        list.querySelector(`[data-job="${j.id}"] .switch`)?.focus();
      }
      const said = `${j.name} ${enabled ? "resumed" : "paused"}`;
      if (undoable) toast(said, 5000, { label: "Undo", onClick: () => setEnabled(j, !enabled) });
      else toast(said);
    }
    append(target, list);
    await refresh({ initial: true });
  }

  async function viewJob(id) {
    const isNew = id === "new";
    const openingToggle = enabledUpdates.get(id);
    setHeader("jobs", isNew ? "New job" : "Job", { page: true });
    const [projects, models, backends, job] = await Promise.all([
      api("/projects"), api("/models"), api("/backends?auth=skip"), isNew ? null : api(`/jobs/${id}`)]);
    // The detail response may predate a list toggle completed while its other dependencies were loading.
    const latestToggle = enabledUpdates.get(id);
    if (job && latestToggle !== openingToggle) job.enabled = latestToggle.enabled;
    const j = job || newJobDefaults(projects);
    const name = h("input", { type: "text", value: j.name, placeholder: "e.g. Morning homelab check" });
    const prompt = h("textarea", { placeholder: "e.g. Check that every homelab service is running and nothing restarted overnight. Look at the logs of anything that isn't healthy." });
    prompt.value = j.prompt;
    const custom = !CRON_PRESETS.some(([c]) => c === j.cron);
    const preset = h("select", {}, CRON_PRESETS.map(([c, label]) => h("option", { value: c, selected: c === j.cron }, label)),
      h("option", { value: "", selected: custom }, "Custom (cron)"));
    const cron = h("input", { type: "text", value: j.cron, id: "job-cron", placeholder: "minute hour day month weekday", style: "font-family:var(--mono)" });
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
    const enabled = h("input", { type: "checkbox", checked: j.enabled, "aria-label": "Job enabled" });
    let enabledEdited = false;
    let enabledRevision = 0;
    enabled.addEventListener("change", () => { enabledEdited = true; enabledRevision++; });
    const state = { id, revision: () => enabledRevision, syncEnabled: (next, expected) => {
      if (enabledRevision !== expected) return; // a later editor choice wins over the pending list switch
      enabled.checked = next;
      enabledEdited = false;
    } };
    openForm = state;
    onLeave(() => { if (openForm === state) openForm = null; });
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
    onLeave(() => clearTimeout(previewTimer));
    if (isGuest()) {
      [name, prompt, cron, preset, project, backend, model, notify, enabled].forEach((el) => { el.disabled = true; });
    }
    const body = () => ({ name: name.value, prompt: prompt.value, cron: cron.value, project: project.value,
      backend: backend.value, model: backend.value === "local" ? model.value : "", notify: notify.value, enabled: enabled.checked,
      ...(j.catch_up_minutes == null ? {} : { catch_up_minutes: j.catch_up_minutes }) });
    const save = h("button", { class: "btn primary", type: "submit" }, isNew ? "Create" : "Save");
    const runButtons = [];
    let running = false;
    const startRun = async () => {
      if (running) return;
      running = true;
      runButtons.forEach((button) => { button.disabled = true; });
      try {
        if (backend.value === "local" && !(await confirmGpuQueue("This job run"))) return;
        const session = await api(`/jobs/${id}/run`, { method: "POST" });
        location.hash = `#/s/${session.id}`;
      } catch (err) { toast(err.message); }
      finally {
        running = false;
        runButtons.forEach((button) => { button.disabled = false; });
      }
    };
    const runNow = (className) => {
      if (isGuest() || isNew) return null;
      const button = h("button", { class: `btn ${className}`, type: "button", onclick: startRun }, "Run now");
      runButtons.push(button);
      return button;
    };
    const field = (id, label, control, ...notes) => {
      control.setAttribute("id", id);
      return h("div", { class: "job-field" }, h("label", { for: id }, label), control, ...notes);
    };
    const form = h("form", { class: "job-form",
      onsubmit: async (e) => {
        e.preventDefault();
        if (isNew && backend.value === "local" && !(await confirmGpuQueue("This scheduled job"))) return;
        save.disabled = true;
        try {
          const saved = isNew ? await api("/jobs", { method: "POST", body: body() }) : await writeJob(id, async () => {
            const fresh = await api(`/jobs/${id}`);
            const changes = body();
            // A form that did not edit Enabled keeps the latest persisted state, including a list switch change.
            if (!enabledEdited) changes.enabled = fresh.enabled;
            return api(`/jobs/${id}`, { method: "PUT", body: changes });
          });
          toast(`Saved · next run ${whenText(saved.next_run_at)}`, 3500);
          await refreshList?.();
          if (isNew) location.hash = `#/jobs/${saved.id}`; else void route();
        } catch (err) { toast(err.message, 5000); }
        save.disabled = false;
      },
    },
    h("div", { class: "job-fields" },
      field("job-name", "Name", name),
      field("job-task", "Task", prompt),
      h("p", { class: "muted small" }, "The agent is asked to end with STATUS: OK or STATUS: ATTENTION, which decides how loudly you're notified. Approvals always notify."),
      h("div", { class: "job-schedule-grid" },
        field("job-schedule", "Schedule (tower time)", preset),
        h("div", { class: "job-field job-cron" }, h("label", { class: "job-cron-label", for: "job-cron" }, "Cron"), cron, cronNote)),
      h("div", { class: "job-model-grid" }, field("job-project", "Project", project), field("job-backend", "Backend", backend, backendNote), field("job-model", "Model", model)),
      field("job-notify", "Notify me", notify)),
    h("div", { class: "job-toolbar" },
      h("h2", { class: "job-heading" }, isNew ? "New job" : j.name),
      h("label", { class: "row job-enabled", style: "font-weight:500" }, enabled, "Enabled"),
      runNow("job-run-header")),
    h("div", { class: "row job-footer", style: "margin-top:18px" },
      !isGuest() && !isNew ? h("button", {
        class: "btn bad", type: "button",
        onclick: async () => {
          if (!(await confirmSheet({ title: `Delete the job “${j.name}”?`, message: "Its past sessions stay.", confirmLabel: "Delete job",
            destructive: true }))) return;
          try { await writeJob(id, () => api(`/jobs/${id}`, { method: "DELETE" })); await refreshList?.(); go("#/jobs", true); } catch (err) { toast(err.message); }
        },
      }, "Delete") : null,
      h("span", { class: "spacer" }),
      runNow("job-run-footer"),
      isGuest() ? null : save));
    append($app, h("section", { class: "job-detail", "aria-label": isNew ? "New job" : j.name },
      h("div", { class: "job-layout" }, form,
        job ? h("section", { class: "job-runs", "aria-label": "Recent runs" }, ...recentRunsView(job)) : null)));

  }

  async function previewCron(cronValue, cronNote) {
    try {
      const r = await api(`/jobs/preview?cron=${encodeURIComponent(cronValue)}`);
      cronNote.classList.toggle("bad", !r.ok);
      cronNote.textContent = r.ok ? `Next: ${r.next.map(fmtWhen).join(" · ")}` : r.error;
    } catch (_) { /* offline */ }
  }

  function recentRunsView(job) {
    return [h("h3", { class: "job-runs-heading" }, "Recent runs"),
      job.last_skip ? h("p", { class: "muted small" }, `Last skipped: ${job.last_skip}`) : null,
      job.last_error ? h("p", { class: "small bad" }, `Last start failed: ${job.last_error}`) : null,
      job.recent.length ? job.recent.map((s) => h("a", { class: "card", href: `#/s/${s.id}` },
        h("div", { class: "meta" }, badge(s.status), s.job_status ? jobStatusBadge(s.job_status) : null, h("span", {}, ago(s.created_at))),
        s.answer ? h("div", { class: "preview" }, s.answer) : null)) : h("p", { class: "muted small" }, "No runs yet.")];
  }
  return { viewJobs, viewJob };
}
