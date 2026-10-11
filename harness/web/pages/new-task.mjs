// @ts-nocheck
// New task page (#258): project/target pickers, backend and model, GitHub issue picker, skills, templates. The shell
// (DOM builder, router, auth state) is injected by app.js so this module imports under plain Node and never reaches
// into another page.
import { holdRemainingText } from "../lib/format.mjs";
import { TARGET_LABEL, runnerStateText, pickDefaultBackend } from "../lib/targets.mjs";
import * as sheets from "../lib/sheet.mjs";

export function mountNewTask({ $app, h, fill, append, api, setHeader, toast, route, isMember, isOwner, onLeave, githubConnectionCard, warmModel, browser,
  confirmSheet = sheets.confirmSheet, promptSheet = sheets.promptSheet }) {
// Browser globals come in through `browser` (globalThis in the app, a stub under Node) so importing this module touches no DOM.
const { localStorage, location, setInterval, clearInterval } = browser;

async function confirmGpuQueue(label) {
  if (isMember()) return true;
  let gpu;
  try { gpu = await api("/gpu"); } catch (err) {
    console.debug("GPU hold unreadable; not blocking the queue", err);
    return true;
  }
  if (!gpu.manual) return true;
  const remaining = holdRemainingText(gpu.manual_remaining_seconds);
  return confirmSheet({ title: `GPU hold is on ${remaining}`,
    message: `${label} can be queued, but nothing will be sent to the local model until the hold ends.`, confirmLabel: "Queue it" });
}

const MODEL_STATE = {
  ready: "✓ Model loaded",
  sleeping: "Model is unloaded; it loads when you type or start the task (about a minute)",
  unloaded: "Model is unloaded; it loads when you type or start the task (about a minute)",
  waking: "Model is loading (about a minute); you can start the task anyway",
  unreachable: "Model server isn't answering",
  paused: "⏸ Model unloaded while something else uses the GPU; tasks wait (Actions → Resources)",
};

function paintModelState(modelState, statuses, modelName, holdActive) {
  const current = statuses.find((s) => s.name === modelName) || statuses[0];
  if (!current) return;
  const holdPaused = current.state === "paused" && holdActive;
  modelState.textContent = holdPaused ? "" : (MODEL_STATE[current.state] || current.state);
  modelState.classList.toggle("dots", !holdPaused && (current.state === "waking" || current.state === "sleeping"));
}

// localStorage can be unavailable (private mode), so a failed read or write just means "not remembered".
function storeGet(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}
function storeSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* private mode: not remembered */ }
}
function storeRemove(key) {
  try { localStorage.removeItem(key); } catch (_) { /* private mode: nothing to remove */ }
}

function skillOption(sk, inputs) {
  const box = h("input", { type: "checkbox", class: "skill-opt", value: sk.slug });
  inputs.push(box);
  return h("label", { class: "row", style: "gap:8px;align-items:flex-start;margin:6px 0" }, box,
    h("span", {}, h("strong", {}, sk.title || sk.slug),
      h("div", { class: "muted small" }, sk.purpose || `v${sk.version} · ${sk.content_hash.slice(0, 12)}`)));
}

// Resolves true when the session was created (and the page moved on), false when the form should stay usable.
async function startSession(fields, draftKey, endpoint = "/sessions") {
  try {
    const s = await api(endpoint, { method: "POST", body: fields });
    storeRemove(draftKey);
    location.hash = `#/s/${s.id}`;
    return true;
  } catch (err) {
    toast(err.message);
    return false;
  }
}

async function saveTemplate({ prompt, project, backend, model }) {
  if (!prompt.trim()) return toast("Write a prompt first");
  const name = (await promptSheet({ title: "Save as template", label: "Template name", confirmLabel: "Save template",
    validate: sheets.required("a template name") }))?.trim();
  if (!name) return;
  try {
    await api("/templates", { method: "POST", body: { name, project, backend, model, prompt } });
    toast("Template saved");
    void route();
  } catch (err) { toast(err.message); }
}

function templateManager(templates) {
  return h("details", { style: "margin-top:28px" }, h("summary", { class: "muted" }, "Manage templates"),
    templates.map((t) => h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", {}, t.name), h("span", { class: "spacer" }),
        h("button", {
          class: "btn small bad",
          onclick: async () => {
            if (!(await confirmSheet({ title: `Delete template “${t.name}”?`, confirmLabel: "Delete template", destructive: true }))) return;
            await api(`/templates/${t.id}`, { method: "DELETE" });
            void route();
          },
        }, "Delete")),
      h("div", { class: "preview" }, `${t.project} · ${t.prompt}`))));
}

const NEW_PROJECT_NOTE = {
  member: "Saved in your household account. Use a lowercase project id; a public HTTPS git source is cloned into your own area.",
  owner: "Saved privately on Agent Harness Server. Use a lowercase project id; a git source gets a reviewable branch per task.",
};

const repoPlaceholder = () => (isMember() ? "https://github.com/org/repo" : String.raw`D:\Projects\example or https://…`);

// Members get a fixed "tower" value; the owner picks which machine the new project lives on.
function projectTargetInput(targets, target) {
  if (isMember()) return h("input", { type: "hidden", value: "tower" });
  const select = h("select", {}, targets.map((name) => h("option", { value: name },
    name === "tower" ? "Tower" : TARGET_LABEL[name] || name)));
  select.value = target;
  return select;
}

// A member runs the local model, plus Claude or Codex on their own API key once they have added it (#393).
async function memberBackends() {
  const local = { name: "local", available: true };
  try {
    const st = await api("/me/api-keys");
    return [local, ...st.keys.filter((k) => k.configured).map((k) => ({
      name: k.backend, available: true, model: "", billing_warning: st.billing_warning }))];
  } catch { return [local]; }
}

function loadNewTaskData() {
  const member = isMember();
  return Promise.all([
    api("/projects"), api("/models"),
    member ? Promise.resolve([]) : api("/templates").catch(() => []),
    member ? memberBackends() : api("/backends?auth=skip"),
    member ? Promise.resolve(null) : api("/gpu").catch(() => null)]);
}

async function viewNew() {
  setHeader("agents", "New task", { page: true });
  let [projects, models, allTemplates, backends, gpu] = await loadNewTaskData();
  // Where the task runs: the tower or a runner (the MacBook). Projects and templates for other machines are hidden.
  const targets = [...new Set(projects.map((p) => p.target))];
  const targetKey = "harness.target";
  let target = storeGet(targetKey) || "tower";
  if (!targets.includes(target)) target = targets[0] || "tower";
  const projectTarget = (name) => projects.find((p) => p.name === name)?.target || "tower";
  let templates = [];
  const tplSelect = h("select", {});
  const project = h("select", {});
  const fillChoices = () => {
    templates = allTemplates.filter((t) => projectTarget(t.project) === target);
    fill(tplSelect, h("option", { value: "" }, templates.length ? "— none —" : "No templates for this machine"),
      templates.map((t) => h("option", { value: t.id }, t.name)));
    fill(project, projects.filter((p) => p.target === target).map((p) => h("option", { value: p.name },
      p.description ? `${p.name} — ${p.description}` : p.name)));
  };
  fillChoices();
  const targetSwitch = targets.length > 1 ? h("div", { class: "row" }, targets.map((name) => h("button", {
    type: "button", class: `btn small${name === target ? " primary" : ""}`, "data-target": name,
    onclick: (ev) => {
      target = name;
      storeSet(targetKey, name);
      newProjectTarget.value = target;
      for (const b of ev.currentTarget.parentNode.children) b.classList.toggle("primary", b === ev.currentTarget);
      fillChoices();
      void showTarget();
      showProjectHint();
    },
  }, name === "tower" ? "🖥 Tower" : `💻 ${TARGET_LABEL[name] || name}`))) : null;
  const targetState = h("div", { class: "muted small", style: "margin-top:6px" });
  const showTarget = async () => {
    const p = projects.find((x) => x.name === project.value);
    if (!p || p.target === "tower") { targetState.textContent = ""; return; }
    try {
      const r = (await api("/runners")).find((x) => x.name === p.target);
      targetState.textContent = runnerStateText(p.target, r);
    } catch (_) { /* offline */ }
  };
  const projectHint = h("div", { class: "muted small", style: "margin-top:6px" });
  const showProjectHint = () => {
    projectHint.textContent = (project.value === "scratch" || project.value === "mac-scratch")
      ? "Scratch is a fresh empty folder for this session only. It is not a git repo and does not add a new project."
      : "";
  };
  project.addEventListener("change", () => { void showTarget(); showProjectHint(); });
  void showTarget();
  showProjectHint();
  const newProjectName = h("input", { type: "text", placeholder: "my-project", maxlength: "64", required: true,
    pattern: "[a-z0-9][a-z0-9._-]{0,63}" });
  const newProjectDescription = h("input", { type: "text", placeholder: "Optional description", maxlength: "240" });
  const newProjectTarget = projectTargetInput(targets, target);
  const newProjectSource = h("select", {},
    h("option", { value: "empty" }, "Empty workspace"),
    h("option", { value: "repo" }, isMember() ? "Public HTTPS repository" : "Local folder or git URL"),
    isMember() ? h("option", { value: "github" }, "Private GitHub repository (my GitHub connection)") : null);
  const githubConnect = h("div");
  const newProjectRepo = h("input", { type: "text", placeholder: repoPlaceholder(), hidden: true });
  newProjectSource.addEventListener("change", () => {
    const repoSource = newProjectSource.value !== "empty";
    newProjectRepo.hidden = !repoSource;
    newProjectRepo.required = repoSource;
    newProjectRepo.placeholder = newProjectSource.value === "github" ? "https://github.com/owner/repo" : repoPlaceholder();
    fill(githubConnect);
  });
  const createProjectButton = h("button", { class: "btn primary", type: "submit" }, "Create project");
  const projectCreator = h("details", { class: "card" }, h("summary", {}, "＋ New project"),
    h("form", { onsubmit: async (e) => {
      e.preventDefault();
      createProjectButton.disabled = true;
      try {
        const github = newProjectSource.value === "github";
        const created = await api("/projects", { method: "POST", body: {
          name: newProjectName.value, description: newProjectDescription.value, target: newProjectTarget.value,
          repo: newProjectSource.value === "empty" ? "" : newProjectRepo.value, github,
        } });
        projects.push(created);
        target = created.target;
        storeSet(targetKey, target);
        fillChoices();
        project.value = created.name;
        if (targetSwitch) for (const b of targetSwitch.children) b.classList.toggle("primary", b.dataset.target === target);
        void showTarget();
        showProjectHint();
        syncSkillChecks();
        projectCreator.open = false;
        toast(`Project ${created.name} created`);
      } catch (err) {
        if (["not_connected", "reconnect_required"].includes(err.code) && newProjectSource.value === "github") {
          // Keep the pending form in this page only (never browser storage) and submit it once connected.
          const form = e.target;
          fill(githubConnect, githubConnectionCard({ onConnected: () => { fill(githubConnect); form.requestSubmit(); } }));
        } else {
          toast(err.message, 6000);
        }
      } finally {
        createProjectButton.disabled = false;
      }
    } },
    h("p", { class: "muted small" }, NEW_PROJECT_NOTE[isMember() ? "member" : "owner"]),
    h("label", {}, "Name"), newProjectName,
    h("label", {}, "Description"), newProjectDescription,
    isMember() ? [] : [h("label", {}, "Runs on"), newProjectTarget],
    h("label", {}, "Workspace"), newProjectSource, newProjectRepo, githubConnect,
    h("div", { class: "row", style: "margin-top:18px" }, createProjectButton)));
  const model = h("select", {}, models.map((m) => h("option", { value: m.name, selected: m.default }, m.name)));
  let localModel = model.value;
  model.addEventListener("change", () => {
    localModel = model.value;
    if (backend.value === "local") void warmModel(true);
  });
  const gpuHold = () => !!(gpu && (gpu.manual || gpu.state !== "clear"));
  const availableBackends = backends.filter((b) => b.available);
  const defaultBackend = pickDefaultBackend(availableBackends, gpuHold());
  const backend = h("select", {}, availableBackends.map((b) =>
    h("option", { value: b.name, selected: b.name === defaultBackend }, b.name === "local" ? "Local model" : b.name)));
  backend.value = defaultBackend;
  const backendState = h("div", { class: "muted small", style: "margin-top:6px" });
  const holdNotice = h("div", { class: "muted small gpu-hold-note", style: "margin-top:6px" },
    "Model unloaded while something else uses the GPU; tasks wait (",
    h("a", { href: "#/actions/resources" }, "Actions → Resources"),
    ")");
  const modelState = h("div", { class: "muted small", style: "margin-top:6px" });
  const start = h("button", { class: "btn primary", type: "submit" }, "Start");
  const syncHoldUi = () => {
    const queued = gpuHold() && backend.value === "local";
    holdNotice.hidden = !queued;
    start.textContent = queued ? "Queue task" : "Start";
    start.classList.toggle("primary", !queued);
    start.classList.toggle("queued", queued);
  };
  const showBackend = () => {
    const b = backends.find((x) => x.name === backend.value);
    const isLocal = backend.value === "local";
    if (isLocal) {
      fill(model, models.map((m) => h("option", { value: m.name, selected: m.name === localModel }, m.name)));
    } else {
      if (models.some((m) => m.name === model.value)) localModel = model.value;
      fill(model, h("option", { value: b?.model || "" }, b?.model || `${backend.value} default`));
    }
    model.disabled = !isLocal;
    modelState.hidden = !isLocal;
    backendState.textContent = b?.billing_warning || "";
    backendState.classList.toggle("bad", !!b?.billing_warning);
    syncHoldUi();
  };
  backend.addEventListener("change", () => {
    showBackend();
    if (backend.value === "local") void warmModel(true);
  });
  showBackend();
  const prompt = h("textarea", { id: "new-task-prompt", form: "new-task-form", placeholder: "e.g. Clone local:invoice-tools, fix the failing test, and report back." });
  const title = h("input", { type: "text", placeholder: "Optional; defaults to the first line" });
  let selectedGitHub = null;
  const githubState = h("p", { class: "muted small" });
  const githubList = h("div", {});
  const githubSearch = h("input", { type: "search", placeholder: "Search loaded page" });
  const githubPage = h("span", { class: "muted small" });
  let githubPageNumber = 1;
  let githubRows = [];
  let githubMore = false;
  const showGitHubRows = () => {
    const q = githubSearch.value.toLowerCase();
    const rows = githubRows.filter((x) => x.title.toLowerCase().includes(q) || String(x.number).includes(q));
    fill(githubList, rows.length ? rows.map((x) => h("button", {
      type: "button", class: "btn small", style: "display:block;margin:6px 0;text-align:left",
      onclick: async () => {
        try {
          const item = await api(`/github/projects/${encodeURIComponent(project.value)}/items/${x.number}`);
          selectedGitHub = item;
          title.value = item.title;
          prompt.value = `GitHub ${item.kind.toUpperCase()} #${item.number}: ${item.title}\n\n${item.body}` +
            item.comments.map((c) => `\n\nReview comment at ${c.path}:${c.line} by ${c.author}:\n${c.body}`).join("");
          prompt.readOnly = true;
          githubState.textContent = `Selected ${item.kind} #${item.number}${item.base_branch ? `; starting from ${item.base_branch}` : ""}. GitHub content is re-fetched when you start.`;
        } catch (err) { githubState.textContent = err.message; }
      },
    }, `#${x.number} ${x.title} · ${x.author} · ${x.labels.join(", ")}`)) : h("p", { class: "muted small" }, "No open items on this page."));
    githubPage.textContent = `Page ${githubPageNumber}${githubMore ? " · more available" : ""}`;
  };
  const loadGitHub = async () => {
    githubState.textContent = "Loading GitHub items…";
    fill(githubList);
    try {
      const result = await api(`/github/projects/${encodeURIComponent(project.value)}/items?page=${githubPageNumber}`);
      githubRows = result.items;
      githubMore = result.has_more;
      githubState.textContent = result.stale ? result.notice : "Open issues and PRs · read only";
      showGitHubRows();
    } catch (err) { githubState.textContent = err.message; githubRows = []; githubMore = false; }
  };
  const githubPicker = isOwner() ? h("details", { class: "card" },
    h("summary", {}, "From issue / PR"),
    h("p", { class: "muted small" }, "Select a GitHub item from this project. Search filters the current page."),
    githubSearch,
    h("div", { class: "row" },
      h("button", { type: "button", class: "btn small", onclick: () => { githubPageNumber = Math.max(1, githubPageNumber - 1); void loadGitHub(); } }, "Previous"),
      githubPage,
      h("button", { type: "button", class: "btn small", onclick: () => { if (githubMore) { githubPageNumber++; void loadGitHub(); } } }, "Next")),
    githubState, githubList,
    h("button", { type: "button", class: "btn small", onclick: () => {
      selectedGitHub = null; prompt.readOnly = false; prompt.value = "";
      githubState.textContent = "Selection cleared";
    } }, "Clear selection")) : null;
  githubSearch.addEventListener("input", showGitHubRows);
  if (githubPicker) githubPicker.addEventListener("toggle", () => { if (githubPicker.open) void loadGitHub(); });
  project.addEventListener("change", () => {
    selectedGitHub = null; prompt.readOnly = false; githubPageNumber = 1;
    if (githubPicker?.open) void loadGitHub();
  });
  const pollModel = async () => {
    if (!isMember()) {
      try { gpu = await api("/gpu"); } catch (_) { /* offline */ }
      syncHoldUi();
    }
    if (backend.value !== "local") return;
    try {
      paintModelState(modelState, await api("/models/status"), model.value, gpuHold());
    } catch (_) { /* offline: the form's own errors cover it */ }
  };
  void pollModel();
  const modelTimer = setInterval(pollModel, 3000);
  onLeave(() => clearInterval(modelTimer));
  const draftKey = "harness.draft";
  prompt.value = storeGet(draftKey) || "";
  prompt.addEventListener("input", () => {
    storeSet(draftKey, prompt.value);
    if (backend.value === "local" && prompt.value.trim()) void warmModel();
  });

  tplSelect.addEventListener("change", () => {
    const t = templates.find((x) => x.id === tplSelect.value);
    if (!t) return;
    project.value = t.project;
    if ((t.backend || "local") === "local" && t.model) localModel = t.model;
    backend.value = t.backend || "local";
    showBackend();
    void showTarget();
    showProjectHint();
    prompt.value = t.prompt;
    syncSkillChecks();
  });

  const enabledSkills = await api("/skills/enabled").catch(() => []);
  const skillInputs = [];
  const skillBoxes = enabledSkills.map((sk) => skillOption(sk, skillInputs));
  const syncSkillChecks = () => {
    for (const box of skillInputs) {
      const sk = enabledSkills.find((s) => s.slug === box.value);
      box.checked = (sk?.projects || []).includes(project.value);
    }
  };
  syncSkillChecks();
  project.addEventListener("change", syncSkillChecks);
  const form = h("form", {
    id: "new-task-form", class: "new-task-settings",
    onsubmit: async (e) => {
      e.preventDefault();
      if (!prompt.value.trim()) return toast("Write a prompt first");
      if (backend.value === "local" && !(await confirmGpuQueue("This task"))) return;
      start.disabled = true;
      const selectedSkills = [...form.querySelectorAll("input.skill-opt:checked")].map((el) => el.value);
      const started = await startSession({ prompt: prompt.value, project: project.value, backend: backend.value,
        model: backend.value === "local" ? model.value : null, title: title.value || null, skills: selectedSkills,
        ...(selectedGitHub ? { number: selectedGitHub.number } : {}) }, draftKey,
        selectedGitHub ? "/github/sessions" : "/sessions");
      if (!started) start.disabled = false;
    },
  },
  h("label", {}, "Project"), project, targetState, projectHint,
  isMember() && availableBackends.length < 2 ? [] : [h("label", {}, "Backend"), backend, holdNotice, backendState],
  h("label", {}, "Model"), model, modelState,
  h("label", {}, "Title"), title,
  skillBoxes.length ? [h("label", {}, "Skills"), h("p", { class: "muted small" }, "Checked skills are injected for this session (exact include list). Skills allowlisted for the selected project start checked; uncheck to exclude them. They stay frozen even if you disable them later."), ...skillBoxes] : null,
  h("div", { class: "row", style: "margin-top:18px" },
    isMember() ? null : h("button", {
      class: "btn", type: "button",
      onclick: () => saveTemplate({ prompt: prompt.value, project: project.value, backend: backend.value,
        model: backend.value === "local" ? model.value : "" }),
    }, "Save as template"),
    h("span", { class: "spacer" }), start));
  const context = h("div", { class: "new-task-context" },
    targetSwitch ? [h("label", {}, "Runs on"), targetSwitch] : null,
    allTemplates.length ? [h("label", {}, "Template"), tplSelect] : null, githubPicker);
  // The prompt remains associated with the settings form even though desktop places it in its own column.
  // DOM order keeps the phone flow: context, prompt, settings, then the submit actions.
  const promptPane = h("div", { class: "new-task-prompt" }, h("label", { for: "new-task-prompt" }, "Prompt"), prompt);
  append($app, h("section", { class: "new-task-page", "aria-label": "New task" }, projectCreator,
    h("section", { class: "new-task-layout", "aria-label": "Task configuration" }, context, promptPane, form),
    allTemplates.length ? templateManager(allTemplates) : null));
}
return { viewNew, confirmGpuQueue };
}
