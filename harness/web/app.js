// Agent Harness web app: plain ES module, no build step. Hash routes:
//   #/                       session list
//   #/new                    new task (templates)
//   #/s/<id>                 session transcript (live)
//   #/s/<id>/approval/<aid>  same, focused on one approval (notification deep link)
//   #/s/<id>/changes         diff viewer
//   #/s/<id>/info            session details
//   #/profile                identity, GPU, Claude Remote Control, and a Settings menu
//   #/profile/<section>      a Settings page (appearance, notifications, backends, …)
//   #/images[/<id>]          image generation and gallery
//   #/jobs[/new|/<id>]       scheduled jobs

const $app = document.getElementById("app");
const $title = document.getElementById("title");
const $back = document.getElementById("back");
const $conn = document.getElementById("conn");
const $feature = document.getElementById("feature-nav");
const $profileIcon = document.getElementById("profile-icon");
const TERMINAL = new Set(["done", "failed", "cancelled"]);
const STATUS_LABEL = {
  queued: "queued", running: "running", waiting_approval: "needs approval", waiting_target: "waiting for Mac", waiting_app: "waiting for app", waiting_limit: "waiting for limit",
  done: "done", failed: "failed", cancelled: "cancelled",
};
const TARGET_LABEL = { tower: "tower", macbook: "MacBook" };
const SESSION_EVENT_TYPES = [
  "session_created", "user_message", "status", "assistant", "delta", "tool_call", "tool_result",
  "approval_requested", "approval_decided", "compaction", "compacting", "error", "llm_retry", "resumed",
  "run_finished", "queue", "notes", "model_waking", "model_ready", "workspace_ready", "branch_saved", "review",
  "target_waiting", "target_online", "compaction_started", "prompt_progress", "gpu_paused", "gpu_resumed", "app_context", "app_tool_call", "app_tool_result",
  "quote_check", "ungrounded_quotes",
];
const REVIEW_LABEL = { merged: "merged", pushed: "pushed", discarded: "discarded" };
const fmtElapsed = (ms) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
};
const fmtTokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : `${n || 0}`);
// llama-server's prompt progress counts the cached prefix as processed; the bar covers only the part being read.
const readFraction = (d) => (d.total > d.cached ? (d.processed - d.cached) / (d.total - d.cached) : null);
const readingText = (what, d) => `${what} ${fmtTokens(Math.max(0, d.processed - d.cached))} of ${fmtTokens(d.total - d.cached)} new tokens${d.cached ? ` (${fmtTokens(d.cached)} cached)` : ""}`;
const progressBar = (fraction) => h("div", { class: `progress${fraction === null ? " indeterminate" : ""}` },
  h("span", { style: fraction === null ? "" : `width:${Math.max(2, Math.min(100, fraction * 100)).toFixed(1)}%` }));

let cleanup = [];
const onLeave = (fn) => cleanup.push(fn);

function layoutBar() {
  const bar = document.getElementById("bar");
  const chrome = document.querySelector(".session-chrome");
  if (bar) document.documentElement.style.setProperty("--bar-h", `${bar.offsetHeight}px`);
  document.documentElement.style.setProperty("--session-chrome-h", `${chrome ? chrome.offsetHeight : 0}px`);
}

function setHeader(feature, pageTitle = "", { page = false } = {}) {
  $feature.value = feature;
  $feature.hidden = page;
  $profileIcon.hidden = page;
  $title.textContent = pageTitle;
  $title.hidden = !pageTitle;
  document.getElementById("bar").classList.toggle("page", page);
  requestAnimationFrame(layoutBar);
}
window.addEventListener("resize", layoutBar);

async function loadProfileIcon() {
  try { $profileIcon.textContent = (await api("/profile")).emoji; } catch (_) { /* offline */ }
}

// ---------- utilities ----------
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// replaceChildren() would render null as the text "null"; this skips empty children like h() does.
function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

function toast(text, ms = 2600) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, ms);
}

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new Error("Can't reach the tower. Is Tailscale connected?");
  }
  if (resp.status === 204) return null;
  const type = resp.headers.get("content-type") || "";
  const data = type.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) throw new Error((data && data.detail) || `HTTP ${resp.status}`);
  return data;
}

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function badge(status) {
  return h("span", { class: `badge ${status}` }, STATUS_LABEL[status] || status);
}

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const normalizeQuote = (text) => (text || "").toLowerCase().replace(/[^a-z0-9]/g, "");
const quoteParts = (q) => q.split(/\.\.\.|…/).map(normalizeQuote).filter((p) => p.length >= 12);
const quoteIn = (q, text) => {
  const parts = quoteParts(q);
  return parts.length > 0 && parts.every((p) => normalizeQuote(text).includes(p));
};
function quoteHref(url, quote) {
  const base = (url || "").split("#")[0];
  const snippet = quote.replace(/\s+/g, " ").trim().slice(0, 80);
  return `${base}#:~:text=${encodeURIComponent(snippet)}`;
}
function quoteLinks(answer, pages) {
  const links = {};
  const re = /["“]([^"”\n]{25,400})["”]/g;
  let match;
  while ((match = re.exec(answer || ""))) {
    const q = match[1];
    const page = (pages || []).find((p) => /^https?:\/\//i.test(p.url || "") && quoteIn(q, p.text));
    if (page) links[q] = quoteHref(page.url, q);
  }
  return links;
}
function linkQuotes(html, answer, pages) {
  const links = quoteLinks(answer, pages);
  const quotes = Object.keys(links).sort((a, b) => b.length - a.length);
  for (const q of quotes) {
    const escaped = escapeHtml(q);
    html = html.split(escaped).join(`<a class="quote-source" href="${escapeHtml(links[q])}" target="_blank" rel="noopener">${escaped}</a>`);
  }
  return html;
}

// Small, safe Markdown subset: everything is escaped first, then a few constructs are re-enabled.
function md(src, pages) {
  const blocks = [];
  let text = escapeHtml(src || "").replace(/```[\w+-]*\n?([\s\S]*?)```/g, (_, code) => {
    blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (s) => s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\u0000\d+\u0000$/.test(line.trim())) { out.push(line.trim()); continue; }
    if (/^#{1,6} /.test(line)) {
      const level = Math.min(6, line.match(/^#+/)[0].length + 2);
      out.push(`<h${level}>${inline(line.replace(/^#+ /, ""))}</h${level}>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      let html = "<table><thead><tr>" + cells(line).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        html += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
        i++;
      }
      i--;
      out.push(html + "</tbody></table>");
      continue;
    }
    if (/^\s*([-*]|\d+\.) /.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      let html = ordered ? "<ol>" : "<ul>";
      while (i < lines.length && /^\s*([-*]|\d+\.) /.test(lines[i])) {
        html += `<li>${inline(lines[i].replace(/^\s*([-*]|\d+\.) /, ""))}</li>`;
        i++;
      }
      i--;
      out.push(html + (ordered ? "</ol>" : "</ul>"));
      continue;
    }
    if (/^&gt; ?/.test(line)) { out.push(`<blockquote>${inline(line.replace(/^&gt; ?/, ""))}</blockquote>`); continue; }
    if (!line.trim()) { out.push(""); continue; }
    out.push(`<p>${inline(line)}</p>`);
  }
  return linkQuotes(out.join("\n").replace(/\u0000(\d+)\u0000/g, (_, n) => blocks[Number(n)]), src, pages);
}

// EventSource that survives iOS suspending the app: reconnects from the last seq when visible again.
function openStream(urlFor, handlers) {
  let es = null;
  let closed = false;
  let retry = null;
  const connect = () => {
    if (closed) return;
    es?.close();
    es = new EventSource(urlFor());
    es.onopen = () => $conn.classList.add("live");
    es.onerror = () => {
      $conn.classList.remove("live");
      if (es.readyState === EventSource.CLOSED) {
        clearTimeout(retry);
        retry = setTimeout(connect, 3000);
      }
    };
    for (const [type, fn] of Object.entries(handlers)) {
      es.addEventListener(type, (msg) => fn(JSON.parse(msg.data)));
    }
  };
  const onVisible = () => { if (document.visibilityState === "visible") connect(); };
  document.addEventListener("visibilitychange", onVisible);
  connect();
  return () => {
    closed = true;
    clearTimeout(retry);
    es?.close();
    $conn.classList.remove("live");
    document.removeEventListener("visibilitychange", onVisible);
  };
}

// ---------- router ----------
const hashParts = () => location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
const isTopLevel = (parts) => parts.length === 0 || (parts.length === 1 && (parts[0] === "jobs" || parts[0] === "images"));

function go(hash, replace = false) {
  const url = !hash || hash === "#" || hash === "#/" ? "#/" : (hash.startsWith("#") ? hash : `#/${hash}`);
  const cur = location.hash || "#/";
  const same = url === cur || (url === "#/" && (cur === "" || cur === "#" || cur === "#/"));
  if (same) return;
  if (replace) location.replace(url);
  else location.hash = url;
}

async function route() {
  cleanup.forEach((fn) => { try { fn(); } catch (_) { /* ignore */ } });
  cleanup = [];
  $app.replaceChildren();
  document.querySelector(".composer")?.remove();
  document.querySelector(".fab")?.remove();
  document.querySelectorAll(".jump").forEach((el) => el.remove());
  const parts = hashParts();
  $back.hidden = isTopLevel(parts);
  try {
    if (parts.length === 0) await viewList();
    else if (parts[0] === "new") await viewNew();
    else if (parts[0] === "profile" || parts[0] === "settings") await viewProfile(parts[1]);
    else if (parts[0] === "images") await (parts[1] ? viewImage(parts[1]) : viewImages());
    else if (parts[0] === "jobs") await (parts[1] ? viewJob(parts[1]) : viewJobs());
    else if (parts[0] === "s" && parts[1]) await viewSession(parts[1], parts[2] || "transcript", parts[3]);
    else go("#/", true);
  } catch (e) {
    $app.append(h("p", { class: "note bad" }, e.message));
  }
}
$back.addEventListener("click", () => {
  const parts = hashParts();
  // Session Transcript/Changes/Info are tabs (replaceState), so Back always leaves the session.
  // An approval deep-link is a real subpage of the transcript.
  if (parts[0] === "s" && parts[2] === "approval") go(`#/s/${parts[1]}`, true);
  else history.back();
});
$feature.addEventListener("change", () => {
  go($feature.value === "jobs" ? "#/jobs" : $feature.value === "images" ? "#/images" : "#/", true);
});
window.addEventListener("hashchange", route);

// ---------- session list ----------
let searchQuery = "";  // kept while navigating, so Back from a result returns to the results
let sessionTarget = "all";
try { sessionTarget = localStorage.getItem("harness.sessionTarget") || "all"; } catch (_) { /* private mode */ }

// Search passages mark matches with  … ; everything else is escaped.
const markPassage = (text) => escapeHtml(text).replace(//g, "<mark>").replace(//g, "</mark>");
const PASSAGE_KIND = { title: "title", message: "you", assistant: "agent", tool: "tool output", answer: "answer", context: "app context" };

async function viewList() {
  setHeader("agents");
  const list = h("div");
  const results = h("div", { hidden: true });
  const queueNote = h("p", { class: "note" });
  const search = h("input", { type: "search", placeholder: "Search", value: searchQuery, class: "search" });
  const targetSwitch = h("div", { class: "tabs", role: "group", "aria-label": "Filter sessions by machine" });
  $app.append(h("div", { class: "search-wrap" }, search), targetSwitch, queueNote, results, list);
  document.body.append(h("a", { class: "btn new-task fab", href: "#/new" }, "+ New task"));

  let sessions = [];
  let targets = [];
  const targetName = (target) => target === "tower" ? "Tower" : TARGET_LABEL[target] || target;
  const renderSessions = () => {
    const visible = sessionTarget === "all" ? sessions : sessions.filter((s) => s.target === sessionTarget);
    if (!sessions.length) {
      list.replaceChildren(h("p", { class: "empty" }, "No sessions yet. Start one with “New task”."));
      return;
    }
    if (!visible.length) {
      list.replaceChildren(h("p", { class: "empty" }, `No sessions on the ${targetName(sessionTarget)} yet.`));
      return;
    }
    list.replaceChildren(...visible.map((s) => {
      const pending = (s.pending_approvals || []).length;
      return h("a", { class: "card", href: `#/s/${s.id}${pending ? `/approval/${s.pending_approvals[0].id}` : ""}` },
        h("h3", {}, s.title),
        h("div", { class: "meta" },
          badge(s.status),
          pending ? h("span", { class: "badge waiting_approval" }, `${pending} approval${pending > 1 ? "s" : ""}`) : null,
          s.queue_position > 0 ? h("span", {}, `#${s.queue_position} in queue`) : null,
          s.review ? h("span", { class: `badge ${s.review === "discarded" ? "cancelled" : "done"}` }, REVIEW_LABEL[s.review] || s.review) : null,
          s.job_status ? jobStatusBadge(s.job_status) : null,
          s.target !== "tower" ? h("span", {}, `💻 ${TARGET_LABEL[s.target] || s.target}`) : null,
          h("span", {}, s.project), h("span", {}, ago(s.updated_at))),
        s.chat_summary ? h("div", { class: "preview" }, s.chat_summary) : null);
    }));
  };
  const renderTargetSwitch = () => {
    if (!targets.includes(sessionTarget)) sessionTarget = "all";
    targetSwitch.hidden = !!search.value.trim() || targets.length < 2;
    fill(targetSwitch, ["all", ...targets].map((target) => h("button", {
      type: "button", class: target === sessionTarget ? "on" : "", "aria-pressed": target === sessionTarget,
      onclick: () => {
        sessionTarget = target;
        try { localStorage.setItem("harness.sessionTarget", target); } catch (_) { /* private mode */ }
        renderTargetSwitch();
        renderSessions();
      },
    }, target === "all" ? "All" : targetName(target))));
  };

  const runSearch = async () => {
    const q = search.value.trim();
    searchQuery = search.value;
    results.hidden = !q;
    list.hidden = !!q;
    queueNote.hidden = !!q;
    targetSwitch.hidden = !!q || targets.length < 2;
    if (!q) return;
    try {
      const data = await api(`/search?q=${encodeURIComponent(q)}`);
      if (search.value.trim() !== q) return;  // a newer query is on its way
      fill(results,
        data.mode === "any" && data.results.length ? h("p", { class: "muted small" }, "No session matches every word; showing partial matches.") : null,
        data.results.length ? data.results.map((r) => h("a", { class: "card", href: `#/s/${r.id}` },
          h("h3", {}, r.title),
          h("div", { class: "meta" }, badge(r.status), h("span", {}, r.project), h("span", {}, ago(r.created_at)),
            h("span", {}, `${r.hits} match${r.hits === 1 ? "" : "es"}`)),
          r.passages.map((p) => h("div", { class: "passage small" }, h("span", { class: "muted" }, `${PASSAGE_KIND[p.kind] || p.kind}: `),
            h("span", { html: markPassage(p.text) }))))) : h("p", { class: "empty" }, `Nothing matches “${q}”.`));
    } catch (e) { fill(results, h("p", { class: "note bad" }, e.message)); }
  };
  let searchTimer = null;
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250); });
  if (searchQuery.trim()) runSearch();

  const render = async () => {
    const [freshSessions, queue, gpu, projects] = await Promise.all([
      api("/sessions"), api("/queue"), api("/gpu").catch(() => null), api("/projects")]);
    sessions = freshSessions;
    targets = [...new Set(projects.map((p) => p.target || "tower"))]
      .sort((a, b) => (a === "tower" ? -1 : b === "tower" ? 1 : a.localeCompare(b)));
    const waiting = queue.filter((q) => q.position > 0).length;
    const paused = gpu && gpu.state !== "clear";
    queueNote.replaceChildren(
      paused ? h("a", { href: "#/profile" }, `⏸ ${gpuText(gpu)}`) : "",
      paused && waiting ? " · " : "",
      waiting ? `${waiting} waiting for the GPU` : "");
    renderTargetSwitch();
    renderSessions();
  };
  await render();
  let timer = null;
  const refresh = () => { clearTimeout(timer); timer = setTimeout(() => render().catch(() => {}), 300); };
  const handlers = {};
  for (const type of ["session_created", "status", "approval_requested", "approval_decided", "run_finished", "queue"]) {
    handlers[type] = refresh;
  }
  onLeave(openStream(() => "/events", handlers));
  const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
  document.addEventListener("visibilitychange", onVisible);
  onLeave(() => document.removeEventListener("visibilitychange", onVisible));
}

// ---------- new task ----------
async function viewNew() {
  setHeader("agents", "New task");
  const [projects, models, allTemplates, backends] = await Promise.all([
    api("/projects"), api("/models"), api("/templates"), api("/backends")]);
  // Where the task runs: the tower or a runner (the MacBook). Projects and templates for other machines are hidden.
  const targets = [...new Set(projects.map((p) => p.target))];
  const targetKey = "harness.target";
  let target = "tower";
  try { target = localStorage.getItem(targetKey) || "tower"; } catch (_) { /* private mode */ }
  if (!targets.includes(target)) target = targets[0] || "tower";
  const projectTarget = (name) => (projects.find((p) => p.name === name) || {}).target || "tower";
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
    type: "button", class: `btn small${name === target ? " primary" : ""}`,
    onclick: (ev) => {
      target = name;
      try { localStorage.setItem(targetKey, name); } catch (_) { /* ignore */ }
      for (const b of ev.currentTarget.parentNode.children) b.classList.toggle("primary", b === ev.currentTarget);
      fillChoices();
      showTarget();
    },
  }, name === "tower" ? "🖥 Tower" : `💻 ${TARGET_LABEL[name] || name}`))) : null;
  const targetState = h("div", { class: "muted small", style: "margin-top:6px" });
  const showTarget = async () => {
    const p = projects.find((x) => x.name === project.value);
    if (!p || p.target === "tower") { targetState.textContent = ""; return; }
    try {
      const r = (await api("/runners")).find((x) => x.name === p.target);
      const label = TARGET_LABEL[p.target] || p.target;
      targetState.textContent = r && r.online
        ? `Runs on the ${label} (online${r.info.free_gb !== undefined ? `, ${r.info.free_gb} GB free` : ""})`
        : `Runs on the ${label}, which is offline or asleep: the task will wait for it`;
    } catch (_) { /* offline */ }
  };
  project.addEventListener("change", showTarget);
  showTarget();
  const model = h("select", {}, models.map((m) => h("option", { value: m.name, selected: m.default }, m.name)));
  let localModel = model.value;
  model.addEventListener("change", () => { localModel = model.value; });
  const backend = h("select", {}, backends.filter((b) => b.available).map((b) =>
    h("option", { value: b.name }, b.name === "local" ? "Local model" : b.name)));
  const backendState = h("div", { class: "muted small", style: "margin-top:6px" });
  const modelState = h("div", { class: "muted small", style: "margin-top:6px" });
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
  };
  backend.addEventListener("change", showBackend);
  showBackend();
  const prompt = h("textarea", { placeholder: "e.g. Clone local:invoice-tools, fix the failing test, and report back." });
  const title = h("input", { type: "text", placeholder: "Optional; defaults to the first line" });
  const MODEL_STATE = {
    ready: "✓ Model loaded",
    sleeping: "Model is asleep; loading it now (about a minute)",
    waking: "Model is loading (about a minute); you can start the task anyway",
    unreachable: "Model server isn't answering",
    paused: "⏸ Model unloaded while something else uses the GPU; tasks wait (Settings → GPU)",
  };
  const pollModel = async () => {
    if (backend.value !== "local") return;
    try {
      const status = await api("/models/status");
      const current = status.find((s) => s.name === model.value) || status[0];
      if (current) {
        modelState.textContent = MODEL_STATE[current.state] || current.state;
        modelState.classList.toggle("dots", current.state === "waking" || current.state === "sleeping");
      }
    } catch (_) { /* offline: the form's own errors cover it */ }
  };
  warmModel(true);
  pollModel();
  const modelTimer = setInterval(pollModel, 3000);
  onLeave(() => clearInterval(modelTimer));
  const draftKey = "harness.draft";
  try { prompt.value = localStorage.getItem(draftKey) || ""; } catch (_) { /* private mode */ }
  prompt.addEventListener("input", () => { try { localStorage.setItem(draftKey, prompt.value); } catch (_) { /* ignore */ } });

  tplSelect.addEventListener("change", () => {
    const t = templates.find((x) => x.id === tplSelect.value);
    if (!t) return;
    project.value = t.project;
    if ((t.backend || "local") === "local" && t.model) localModel = t.model;
    backend.value = t.backend || "local";
    showBackend();
    showTarget();
    prompt.value = t.prompt;
  });

  const start = h("button", { class: "btn primary", type: "submit" }, "Start");
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      if (!prompt.value.trim()) return toast("Write a prompt first");
      start.disabled = true;
      try {
        const s = await api("/sessions", { method: "POST", body: { prompt: prompt.value, project: project.value,
          backend: backend.value, model: backend.value === "local" ? model.value : null, title: title.value || null } });
        try { localStorage.removeItem(draftKey); } catch (_) { /* ignore */ }
        location.hash = `#/s/${s.id}`;
      } catch (err) {
        toast(err.message);
        start.disabled = false;
      }
    },
  },
  targetSwitch ? [h("label", {}, "Runs on"), targetSwitch] : null,
  allTemplates.length ? [h("label", {}, "Template"), tplSelect] : null,
  h("label", {}, "Prompt"), prompt,
  h("label", {}, "Project"), project, targetState,
  h("label", {}, "Backend"), backend, backendState,
  h("label", {}, "Model"), model, modelState,
  h("label", {}, "Title"), title,
  h("div", { class: "row", style: "margin-top:18px" },
    h("button", {
      class: "btn", type: "button",
      onclick: async () => {
        if (!prompt.value.trim()) return toast("Write a prompt first");
        const name = window.prompt("Template name");
        if (!name) return;
        try {
          await api("/templates", { method: "POST", body: { name, project: project.value, backend: backend.value,
            model: backend.value === "local" ? model.value : "", prompt: prompt.value } });
          toast("Template saved");
          warmModel();
route();
        } catch (err) { toast(err.message); }
      },
    }, "Save as template"),
    h("span", { class: "spacer" }), start));
  $app.append(form);

  if (allTemplates.length) {
    $app.append(h("details", { style: "margin-top:28px" }, h("summary", { class: "muted" }, "Manage templates"),
      allTemplates.map((t) => h("div", { class: "card" },
        h("div", { class: "row" }, h("strong", {}, t.name), h("span", { class: "spacer" }),
          h("button", {
            class: "btn small bad",
            onclick: async () => {
              if (!confirm(`Delete template “${t.name}”?`)) return;
              await api(`/templates/${t.id}`, { method: "DELETE" });
              warmModel();
route();
            },
          }, "Delete")),
        h("div", { class: "preview" }, `${t.project} · ${t.prompt}`)))));
  }
}

function sessionTitle(session) {
  const label = h("button", { class: "session-title", type: "button", title: "Rename session" }, session.title);
  const startEdit = () => {
    const input = h("input", { class: "session-title-edit", type: "text", value: session.title, maxlength: "120", "aria-label": "Session title" });
    label.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      if (commit) {
        const next = input.value.replace(/\s+/g, " ").trim();
        if (next && next !== session.title) {
          try {
            const updated = await api(`/sessions/${session.id}`, { method: "PATCH", body: { title: next } });
            session.title = updated.title;
          } catch (e) { toast(e.message); }
        }
      }
      label.textContent = session.title;
      if (input.isConnected) input.replaceWith(label);
      layoutBar();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  };
  label.addEventListener("click", startEdit);
  return label;
}

function bindSessionJumps() {
  const pageHeight = () => document.documentElement.scrollHeight;
  const jumpTop = h("button", { class: "btn small jump jump-top", type: "button", hidden: true, "aria-label": "Jump to start" }, "↑");
  const jumpBottom = h("button", { class: "btn small jump jump-bottom", type: "button", hidden: true, "aria-label": "Jump to end" }, "↓");
  const updateJumps = () => {
    layoutBar();
    const y = window.scrollY;
    const two = 2 * window.innerHeight;
    jumpTop.hidden = y <= two;
    jumpBottom.hidden = pageHeight() - window.innerHeight - y <= two;
  };
  jumpTop.addEventListener("click", () => window.scrollTo(0, 0));
  jumpBottom.addEventListener("click", () => window.scrollTo(0, pageHeight()));
  document.body.append(jumpTop, jumpBottom);
  window.addEventListener("scroll", updateJumps, { passive: true });
  window.addEventListener("resize", updateJumps);
  onLeave(() => {
    window.removeEventListener("scroll", updateJumps);
    window.removeEventListener("resize", updateJumps);
    jumpTop.remove();
    jumpBottom.remove();
  });
  requestAnimationFrame(updateJumps);
  return { updateJumps, pageHeight };
}

// ---------- session ----------
async function viewSession(sid, tab, focusApproval) {
  let session = await api(`/sessions/${sid}`);
  sid = session.id;
  setHeader("agents");

  const tabs = h("div", { class: "tabs" },
    ["transcript", "changes", "info"].map((name) => h("button", {
      class: (tab === name || (tab === "approval" && name === "transcript")) ? "on" : "",
      onclick: () => go(name === "transcript" ? `#/s/${sid}` : `#/s/${sid}/${name}`, true),
    }, name[0].toUpperCase() + name.slice(1))));
  const head = h("div", { class: "row small" });
  const usage = h("div", { class: "row small usage" });
  $app.append(h("div", { class: "session-chrome" }, sessionTitle(session)), head, usage, tabs);
  const jumps = bindSessionJumps();
  const pages = [];
  const fetchById = new Map();
  const rememberFetch = (id, url, text) => {
    const href = (url || "").trim();
    if (!/^https?:\/\//i.test(href)) return;
    if (id) fetchById.set(id, href);
    pages.push({ url: href, text: text || "" });
  };
  let totals = session.totals || {};
  let ctxUsed = session.context_used || 0;
  const ctxLimit = session.context_limit || 0;

  const renderHead = () => {
    const limits = session.run?.rate_limits || {};
    const backendUsage = session.backend && session.backend !== "local" && limits.utilization !== undefined
      ? ` · ${String(limits.rateLimitType || "limit").replace("seven_day", "7d").replace("five_hour", "5h")} ${Math.round(limits.utilization * 100)}%` : "";
    fill(head, badge(session.status),
      session.queue_position > 0 ? h("span", { class: "muted" }, `#${session.queue_position} in GPU queue`) : null,
      h("span", { class: "muted" }, `${session.project}${session.target !== "tower" ? ` on ${TARGET_LABEL[session.target] || session.target}` : ""} · ${session.backend || "local"}${backendUsage} · ${session.model}`));
    const pct = ctxLimit && ctxUsed ? Math.round((100 * ctxUsed) / ctxLimit) : null;
    fill(usage,
      h("span", { class: "muted", title: "Cumulative tokens for this session (prompt tokens in, generated tokens out)" },
        `Tokens ${fmtTokens(totals.prompt_tokens)} in · ${fmtTokens(totals.completion_tokens)} out`),
      pct === null ? null : h("span", { class: `ctx${pct >= 55 ? " high" : ""}`, title: `Context window: ~${ctxUsed} of ${ctxLimit} tokens. Older context is condensed as it fills up.` },
        progressBar(pct / 100), `${pct}% context`));
  };
  renderHead();

  if (tab === "changes") { await viewChanges(session); jumps.updateJumps(); return; }
  if (tab === "info") { viewInfo(session); jumps.updateJumps(); return; }

  const feed = h("div");
  $app.append(feed);

  // composer
  const input = h("textarea", { placeholder: "Message the agent…", rows: 1 });
  const send = h("button", { class: "btn primary" }, "Send");
  const actions = h("div", { class: "row", style: "margin-bottom:6px" });
  const composer = h("div", { class: "composer" }, h("div", { class: "inner", style: "flex-direction:column;align-items:stretch" },
    actions, h("div", { class: "row", style: "flex-wrap:nowrap;align-items:flex-end" }, input, send)));
  document.body.append(composer);
  input.addEventListener("input", () => { input.style.height = "44px"; input.style.height = `${Math.min(160, input.scrollHeight)}px`; });
  send.addEventListener("click", async () => {
    const text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    try {
      await api(`/sessions/${sid}/messages`, { method: "POST", body: { content: text } });
      input.value = "";
      input.style.height = "44px";
    } catch (e) { toast(e.message); }
    send.disabled = false;
  });

  const renderActions = () => {
    const active = !TERMINAL.has(session.status);
    input.placeholder = active ? "Add guidance…" : "Continue this session…";
    fill(actions,
      active ? h("button", {
        class: "btn small bad",
        onclick: async () => {
          if (!confirm("Cancel this task?")) return;
          try { session = { ...session, ...(await api(`/sessions/${sid}/cancel`, { method: "POST" })) }; } catch (e) { toast(e.message); }
        },
      }, "Cancel") : null,
      !active ? h("button", {
        class: "btn small",
        onclick: async () => {
          try {
            const s = await api(`/sessions/${sid}/rerun`, { method: "POST" });
            location.hash = `#/s/${s.id}`;
          } catch (e) { toast(e.message); }
        },
      }, "Run again as new session") : null,
      h("span", { class: "spacer" }),
      h("button", { class: "btn small", type: "button", onclick: () => go(`#/s/${sid}/changes`, true) }, "Changes"));
  };
  renderActions();

  // transcript rendering
  // Follow new output only while the reader is at the bottom. Any upward scroll (wheel, finger, momentum) stops
  // following, however small; reaching the bottom again resumes it. A generous "near the bottom" margin used to
  // snap slow upward scrolls back down on every streamed token.
  let follow = true;
  let touching = false;
  let lastY = window.scrollY;
  const pageHeight = jumps.pageHeight;
  const atBottom = () => window.innerHeight + window.scrollY >= pageHeight() - 2;
  const scrollDown = (force = false) => {
    if (!force && (!follow || touching)) return;
    window.scrollTo(0, pageHeight());
    lastY = window.scrollY;
    jumps.updateJumps();
  };
  const onScroll = () => {
    const y = window.scrollY;
    if (y < lastY - 0.5 && !atBottom()) follow = false; // content shrinking at the bottom also moves y; ignore that
    else if (atBottom()) follow = true;
    lastY = y;
  };
  const stopFollowing = () => { follow = false; };
  const onWheel = (e) => { if (e.deltaY < 0) stopFollowing(); };
  const onTouchStart = () => { touching = true; };
  const onTouchEnd = () => { touching = false; };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("wheel", onWheel, { passive: true });
  window.addEventListener("touchstart", onTouchStart, { passive: true });
  window.addEventListener("touchend", onTouchEnd, { passive: true });
  window.addEventListener("touchcancel", onTouchEnd, { passive: true });
  onLeave(() => {
    window.removeEventListener("scroll", onScroll);
    window.removeEventListener("wheel", onWheel);
    window.removeEventListener("touchstart", onTouchStart);
    window.removeEventListener("touchend", onTouchEnd);
    window.removeEventListener("touchcancel", onTouchEnd);
  });
  const grew = () => { if (follow && !touching) scrollDown(); else jumps.updateJumps(); };
  const add = (el) => {
    feed.append(el);
    if (live && live.el !== el) feed.append(live.el); // the in-progress turn always stays last
    grew();
    return el;
  };
  const calls = new Map();   // tool call id -> {el, state, body}
  const approvals = new Map();
  let live = null;           // streaming bubble
  let lastSeq = 0;
  let lastContent = "";
  let wakingNote = null;
  let gpuNote = null;
  let targetNote = null;
  let compactNote = null;    // {el, label, bar, detail, elapsed, start} while older context is being summarized
  let lastEventAt = Date.now();  // server time of the latest persisted event: when the current step began
  let prevEventAt = Date.now();
  const pendingCalls = new Set();  // tool calls of the last assistant turn without a result yet

  const liveBubble = () => {
    if (live) return live;
    const thinkText = h("div", { class: "text" });
    const label = h("span", { class: "dots" }, "Thinking");
    const elapsed = h("span", { class: "elapsed" });
    const think = h("details", { class: "thinking" }, h("summary", {}, label, elapsed), thinkText);
    const content = h("div", { class: "msg assistant", style: "white-space:pre-wrap", hidden: true });
    const el = add(h("div", { class: "ev" }, think, content));
    live = { el, think, thinkText, content, label, elapsed, start: lastEventAt, frozen: false, chars: 0, reading: null };
    tick();
    return live;
  };
  // A model turn has started when the session is running and nothing is waiting on a tool.
  const maybeThinking = () => {
    if (session.status === "running" && pendingCalls.size === 0 && !compactNote) liveBubble();
  };

  const compacting = () => {
    if (compactNote) return compactNote;
    const label = h("span", { class: "dots" }, "Condensing older context");
    const elapsed = h("span", { class: "elapsed" });
    const bar = h("div");
    const detail = h("div", { class: "muted small" }, "Summarizing older messages so the agent can keep going");
    fill(bar, progressBar(null));
    const el = add(h("div", { class: "note compacting ev" }, h("div", { class: "row between" }, label, elapsed), bar, detail));
    compactNote = { el, label, bar, detail, elapsed, start: lastEventAt };
    tick();
    return compactNote;
  };
  function tick() {
    const now = Date.now();
    if (live && !live.frozen) live.elapsed.textContent = ` ${fmtElapsed(now - live.start)}`;
    if (compactNote) compactNote.elapsed.textContent = fmtElapsed(now - compactNote.start);
  }
  const ticker = setInterval(tick, 1000);
  onLeave(() => clearInterval(ticker));

  const toolEl = (call) => {
    const fn = call.function || {};
    let args = {};
    try { args = JSON.parse(fn.arguments || "{}"); } catch (_) { args = { raw: fn.arguments }; }
    if (fn.name === "web_fetch" && args.url) rememberFetch(call.id, args.url, "");
    const summaryText = fn.name === "run_shell" ? args.command
      : fn.name === "git_clone" ? args.url
        : fn.name === "prometheus_query" ? args.query
        : fn.name === "web_fetch" ? args.url
        : args.service ? `${args.service}${args.since ? ` since ${args.since}` : ""}`
        : args.path ? `${args.path}${args.start_line ? ` :${args.start_line}` : ""}` : fn.arguments;
    const state = h("span", { class: "state" }, "…");
    const body = h("div", { class: "body" }, h("pre", {}, JSON.stringify(args, null, 2)));
    const el = h("details", { class: "tool" },
      h("summary", {}, h("span", { class: "name" }, fn.name), h("span", { class: "args" }, summaryText || ""), state), body);
    const slot = h("div", { class: "ev" }, el);
    calls.set(call.id, { el, state, body, slot });
    return slot;
  };

  const approvalCard = (a) => {
    const note = h("input", { type: "text", placeholder: "Note for the agent (optional)" });
    const buttons = h("div", { class: "row end" });
    const decide = async (decision) => {
      buttons.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        await api(`/sessions/${sid}/approvals/${a.id}`, { method: "POST", body: { decision, note: note.value } });
      } catch (e) {
        toast(e.message);
        buttons.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    };
    buttons.append(
      h("button", { class: "btn bad solid", onclick: () => decide("deny") }, "Deny"),
      h("button", { class: "btn ok", onclick: () => decide("approve") }, "Approve"));
    const what = a.tool === "run_shell" ? `${a.args.network ? "🌐 network · " : ""}$ ${a.args.command}`
      : a.tool === "git_clone" ? `git clone ${a.args.url}`
        : a.tool === "restart_service" ? `restart ${a.args.service}` : JSON.stringify(a.args, null, 2);
    // Memory library changes carry "summary\n\n<unified diff>"; file writes carry just the diff.
    const memory = a.tool === "memory_edit" || a.tool === "memory_write";
    const [summary, diff] = memory && a.detail.includes("\n\n") ? [a.detail.slice(0, a.detail.indexOf("\n\n")), a.detail.slice(a.detail.indexOf("\n\n") + 2)] : ["", a.detail || ""];
    const diffView = /^@@ /m.test(diff) ? h("div", { class: "diff approval-diff" }, diff.split("\n")
      .filter((line) => !/^(---|\+\+\+) /.test(line))
      .map((line) => h("div", { class: line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "" }, line))) : null;
    const card = h("div", { class: "approval", id: `approval-${a.id}` },
      h("h4", {}, `Approval needed: ${a.reason || a.tool}`),
      summary ? h("p", { style: "margin:4px 0 8px" }, summary) : null,
      diffView || h("pre", {}, a.detail || what),
      a.detail ? h("div", { class: "muted small" }, `${a.tool} ${a.args.path || ""}`) : null,
      note, buttons);
    approvals.set(a.id, { card, buttons, note });
    if (focusApproval === a.id) {
      card.classList.add("focus");
      setTimeout(() => card.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
    }
    return card;
  };

  const handlers = {
    user_message: (e) => { add(h("div", { class: "ev msg user" }, e.data.content)); },
    app_context: (e) => add(h("details", { class: "thinking ev" }, h("summary", {}, "Context from the app"), h("div", { class: "text" }, e.data.content))),
    app_tool_call: (e) => add(h("p", { class: "note" }, `Asked the app to run ${e.data.name}`)),
    app_tool_result: (e) => add(h("p", { class: "note" }, `The app returned ${e.data.ok ? "a result" : "an error"} (${e.data.chars} characters)`)),
    billing_warning: (e) => add(h("p", { class: "note bad" }, e.data.message)),
    limit_waiting: (e) => add(h("p", { class: "note" }, `Rate limit reached; waiting until ${new Date(e.data.resets_at * 1000).toLocaleString()}`)),
    backend_fallback: (e) => add(h("p", { class: "note bad" }, `Rate limit reached; continuing ${e.data.backend} with an API key`)),
    prompt_progress: (e) => {
      const b = liveBubble();
      const d = e.data;
      if (!b.reading) {
        b.reading = h("div", { class: "reading small muted" });
        b.el.prepend(b.reading);
      }
      fill(b.reading, readingText("Reading context", d), progressBar(readFraction(d)));
      grew();
    },
    delta: (e) => {
      const b = liveBubble();
      if (b.reading) { b.reading.remove(); b.reading = null; }
      if (e.data.kind === "reasoning") {
        b.thinkText.textContent += e.data.text;
        b.chars += e.data.text.length;
      } else {
        b.content.hidden = false;
        b.content.textContent += e.data.text;
        if (!b.frozen) {
          tick();
          b.frozen = true;
          b.label.classList.remove("dots");
          b.label.textContent = "Thought for";
        }
      }
      if (follow && !touching) scrollDown();
    },
    assistant: (e) => {
      const d = e.data;
      if (d.totals) totals = d.totals;
      if (d.prompt_tokens) ctxUsed = d.prompt_tokens + d.completion_tokens;
      renderHead();
      live?.el.remove();
      live = null;
      const took = e.ts ? fmtElapsed(e.ts * 1000 - prevEventAt) : "";
      for (const call of d.tool_calls || []) pendingCalls.add(call.id);
      const wrap = h("div", { class: "ev" });
      if (d.reasoning) {
        wrap.append(h("details", { class: "thinking" }, h("summary", {}, `Thought${took ? ` for ${took}` : ""} (${d.completion_tokens} tokens · ${d.gen_tps} tok/s)`),
          h("div", { class: "text" }, d.reasoning)));
      }
      if (d.content && d.content.trim()) {
        lastContent = d.content.trim();
        wrap.append(h("div", { class: `msg assistant${d.tool_calls.length ? "" : " final"}`, html: md(d.content, pages) }));
      }
      if (wrap.childNodes.length) add(wrap);
      for (const call of d.tool_calls || []) add(toolEl(call));
    },
    tool_call: (e) => {
      const c = calls.get(e.data.id);
      if (c && e.data.decision !== "allow") c.state.textContent = e.data.decision === "ask" ? "needs approval" : "blocked";
    },
    approval_requested: (e) => {
      const card = approvalCard(e.data);
      const c = calls.get(e.data.tool_call_id);
      if (c) c.slot.append(card); else feed.append(h("div", { class: "ev" }, card));
      if (focusApproval !== e.data.id) grew();
    },
    approval_decided: (e) => {
      const a = approvals.get(e.data.id);
      if (!a) return;
      a.card.classList.add("decided");
      a.card.classList.remove("focus");
      a.note.remove();
      fill(a.buttons, h("span", { class: `badge ${e.data.status === "approved" ? "done" : "failed"}` },
        e.data.status + (e.data.note ? `: ${e.data.note}` : "")));
    },
    tool_result: (e) => {
      pendingCalls.delete(e.data.id);
      if ((e.data.name === "web_fetch" || fetchById.has(e.data.id)) && e.data.output) {
        const fromOutput = (e.data.output.split("\n").find((line) => /^https?:\/\//i.test(line.trim())) || "").trim();
        rememberFetch(e.data.id, fetchById.get(e.data.id) || fromOutput, e.data.output);
      }
      const c = calls.get(e.data.id);
      const out = h("pre", {}, e.data.output);
      if (!c) { add(h("details", { class: "tool ev" }, h("summary", {}, e.data.name), out)); return; }
      c.state.textContent = `${e.data.ok ? "ok" : "error"} · ${e.data.seconds}s`;
      c.state.className = `state ${e.data.ok ? "ok" : "err"}`;
      c.body.append(out);
    },
    compaction_started: (e) => {
      if (live && !live.thinkText.textContent && !live.content.textContent) { live.el.remove(); live = null; }
      const c = compacting();
      c.detail.textContent = `Summarizing ${e.data.messages} older messages (~${fmtTokens(e.data.tokens_before)} tokens in context) so the agent can keep going`;
    },
    compacting: (e) => {
      const c = compacting();
      const d = e.data;
      if (d.phase === "reading") {
        fill(c.label, readingText("Condensing older context · step 1 of 2: reading", d));
        fill(c.bar, progressBar(readFraction(d)));
      } else if (d.phase === "writing") {
        fill(c.label, `Condensing older context · step 2 of 2: writing the summary (${d.tokens} tokens)`);
        fill(c.bar, progressBar(null));
      }
    },
    compaction: (e) => {
      const d = e.data;
      if (d.totals) totals = d.totals;
      if (d.tokens_after) ctxUsed = d.tokens_after;
      renderHead();
      const text = d.tier === "summary"
        ? `Context condensed: ~${fmtTokens(d.tokens_before)} → ~${fmtTokens(d.tokens_after)} tokens (${d.summarized_messages} messages summarized)`
        : `Trimmed old tool output: ~${fmtTokens(d.tokens_before)} → ~${fmtTokens(d.tokens_after)} tokens`;
      if (compactNote) {
        const c = compactNote;
        compactNote = null;
        if (e.ts) c.elapsed.textContent = `took ${fmtElapsed(e.ts * 1000 - c.start)}`;
        c.label.classList.remove("dots");
        fill(c.label, d.tier === "summary" ? text : `${text} (the summary failed; see the error above)`);
        fill(c.bar, progressBar(1));
        fill(c.detail, d.summary ? h("details", {}, h("summary", {}, "Show summary"), h("div", { class: "text", style: "white-space:pre-wrap" }, d.summary)) : "");
        c.el.classList.add("done");
      } else if (d.tokens_before - d.tokens_after >= 1000) {
        add(h("p", { class: "note" }, text)); // small trims happen every turn near the limit; only the meter shows those
      }
    },
    notes: (e) => add(h("details", { class: "thinking ev" }, h("summary", {}, "Agent saved notes"), h("div", { class: "text" }, e.data.notes))),
    error: (e) => add(h("p", { class: "note bad" }, e.data.message)),
    quote_check: (e) => add(h("details", { class: "thinking ev" },
      h("summary", {}, `Asked the agent to fix ${e.data.quotes.length} quote${e.data.quotes.length === 1 ? "" : "s"} not found in anything it read`),
      h("div", { class: "text" }, e.data.quotes.map((q) => `“${q}”`).join("\n")))),
    ungrounded_quotes: (e) => add(h("div", { class: "note bad" },
      h("p", {}, `⚠ ${e.data.quotes.length === 1 ? "This quote" : "These quotes"} in the answer didn't appear in anything the agent read, so ${e.data.quotes.length === 1 ? "it" : "they"} may be made up:`),
      h("div", { class: "text", style: "white-space:pre-wrap" }, e.data.quotes.map((q) => `“${q}”`).join("\n")))),
    llm_retry: (e) => add(h("p", { class: "note" }, `Model call retried (${e.data.attempt})`)),
    resumed: () => add(h("p", { class: "note" }, "Daemon restarted — session resumed")),
    workspace_ready: (e) => add(h("p", { class: "note" }, `Checked out on branch ${e.data.branch} (from ${e.data.base_branch})`)),
    branch_saved: (e) => add(h("p", { class: "note" }, h("button", {
      class: "btn small", type: "button", onclick: () => go(`#/s/${sid}/changes`, true),
    }, `Branch saved: ${e.data.commits.length} commit${e.data.commits.length === 1 ? "" : "s"} to review${e.data.auto_commit ? " (leftover edits committed)" : ""}`))),
    review: (e) => add(h("p", { class: "note" }, `Review: ${e.data.detail}`)),
    model_waking: (e) => {
      wakingNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `The model was asleep. Waking it (about ${Math.round(e.data.expected_seconds / 60) || 1} min)`)));
    },
    model_ready: (e) => {
      if (wakingNote) fill(wakingNote, `Model woke up in ${e.data.seconds} s`);
      else add(h("p", { class: "note" }, `Model woke up in ${e.data.seconds} s`));
      wakingNote = null;
    },
    gpu_paused: (e) => {
      gpuNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `Paused: ${e.data.reason} needs the GPU, so the model was unloaded. The task continues ${Math.round(e.data.resume_after_seconds / 60)} min after that ends (Settings → GPU to resume now)`)));
    },
    gpu_resumed: (e) => {
      const text = `GPU free again after ${e.data.seconds < 90 ? `${e.data.seconds} s` : `${Math.round(e.data.seconds / 60)} min`}; reloading the model`;
      if (gpuNote) fill(gpuNote, text);
      else add(h("p", { class: "note" }, text));
      gpuNote = null;
    },
    target_waiting: (e) => {
      targetNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `Waiting for the ${TARGET_LABEL[e.data.target] || e.data.target}: it's offline or asleep. The task continues when it wakes`)));
    },
    target_online: (e) => {
      const text = `${TARGET_LABEL[e.data.target] || e.data.target} is back after ${e.data.seconds < 90 ? `${e.data.seconds} s` : `${Math.round(e.data.seconds / 60)} min`}`;
      if (targetNote) fill(targetNote, text);
      else add(h("p", { class: "note" }, text));
      targetNote = null;
    },
    queue: (e) => { session.queue_position = e.data.position; renderHead(); },
    status: (e) => {
      session.status = e.data.status;
      if (e.data.status !== "queued") session.queue_position = null;
      renderHead();
      renderActions();
      if (e.data.status !== "running" && live && !live.thinkText.textContent && !live.content.textContent) {
        live.el.remove(); // queued, waiting for an approval or the Mac: not thinking
        live = null;
      }
      if (TERMINAL.has(e.data.status)) {
        pendingCalls.clear();
        live?.el.remove();
        live = null;
        const answer = (e.data.answer || "").trim();
        if (answer && answer !== lastContent) add(h("div", { class: "ev msg assistant final", html: md(answer, pages) }));
        add(h("p", { class: "status-line" }, badge(e.data.status),
          e.data.stop_reason && !["final_message", "finished"].includes(e.data.stop_reason) ? ` ${e.data.stop_reason}` : ""));
      }
    },
  };
  const tracked = {};
  for (const type of SESSION_EVENT_TYPES) {
    tracked[type] = (e) => {
      const persisted = e.seq !== null && e.seq !== undefined;
      if (persisted) {
        if (e.seq <= lastSeq) return;
        lastSeq = e.seq;
        if (e.ts) { prevEventAt = lastEventAt; lastEventAt = e.ts * 1000; }
      }
      handlers[type]?.(e);
      const finalAnswer = type === "assistant" && !(e.data.tool_calls || []).length && (e.data.content || "").trim();
      if (persisted && !finalAnswer && !TERMINAL.has(session.status)) maybeThinking();
    };
  }
  onLeave(openStream(() => `/sessions/${sid}/events?after=${lastSeq}`, tracked));
  onLeave(() => composer.remove());
}

function reviewCard(s) {
  if (!s.repo_kind || !s.branch) return null;
  const busy = !TERMINAL.has(s.status);
  const base = s.base_branch || "base";
  const conflictFiles = (message) => {
    const match = /^merge conflicts in (.+?)\. Ask the agent/.exec(message);
    return match ? match[1].split(", ").filter(Boolean) : [];
  };
  const conflictHelp = (files) => {
    const ask = h("button", { class: "btn primary" }, "Ask agent to resolve");
    ask.addEventListener("click", async () => {
      if (!confirm(`Ask the agent to merge origin/${base} and resolve ${files.length} conflicting file${files.length === 1 ? "" : "s"}?`)) return;
      ask.disabled = true;
      try {
        await api(`/sessions/${s.id}/messages`, { method: "POST", body: { content:
          `Merge origin/${base} into your branch, resolve the merge conflicts in ${files.join(", ")}, run the relevant tests, and commit the resolution. Do not push.` } });
        toast("Asked the agent to resolve the conflicts", 4000);
        location.hash = `#/s/${s.id}`;
      } catch (e) {
        toast(e.message, 6000);
        ask.disabled = false;
      }
    });
    return h("div", { class: "approval merge-conflict", style: "margin-top:10px" },
      h("h4", {}, "Merge needs conflict resolution"),
      h("p", { class: "small" }, "Conflicting files:"),
      h("ul", { class: "small" }, files.map((file) => h("li", {}, h("code", {}, file)))),
      h("div", { class: "row end" }, ask));
  };
  const act = (action, question) => async (ev) => {
    if (question && !confirm(question)) return;
    const card = ev.target.closest(".card");
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      const updated = await api(`/sessions/${s.id}/review/${action}`, { method: "POST" });
      toast(updated.review_detail || `${action} done`, 4000);
      route();
    } catch (e) {
      toast(e.message, 6000);
      card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      const files = action === "merge" ? conflictFiles(e.message) : [];
      if (files.length) {
        card.querySelector(".merge-conflict")?.remove();
        card.append(conflictHelp(files));
      }
    }
  };
  const buttons = [];
  if (!busy && !s.workspace_removed && s.review !== "discarded") {
    if (s.repo_kind === "local") {
      buttons.push(h("button", { class: "btn ok", onclick: act("merge", `Squash-merge ${s.branch} into ${base}?`) }, `Merge into ${base}`));
    } else {
      buttons.push(h("button", { class: "btn ok", onclick: act("push", `Push ${s.branch} to the remote?`) }, "Push branch"));
    }
    buttons.push(h("button", { class: "btn bad solid", onclick: act("discard", "Discard this branch and delete the workspace? This can't be undone.") }, "Discard"));
  }
  return h("section", { class: "card" },
    h("h3", {}, "Review"),
    h("div", { class: "meta" }, h("span", {}, `branch ${s.branch}`), s.base_branch ? h("span", {}, `from ${s.base_branch}`) : null,
      s.review ? h("span", { class: `badge ${s.review === "discarded" ? "cancelled" : "done"}` }, s.review) : null),
    s.review_detail ? h("p", { class: "muted small" }, s.review_detail) : null,
    busy ? h("p", { class: "muted small" }, "The agent is still working; review when the run ends.") : null,
    buttons.length ? h("div", { class: "row end", style: "margin-top:8px" }, buttons) : null);
}

async function viewChanges(session) {
  const sid = session.id;
  const box = h("div", {}, h("p", { class: "note" }, "Loading changes…"));
  $app.append(reviewCard(session), box);
  const data = await api(`/sessions/${sid}/changes`);
  if (data.removed) {
    box.replaceChildren(h("p", { class: "empty" }, "This workspace was cleaned up or discarded."));
    return;
  }
  if (!data.repos.length) {
    box.replaceChildren(h("p", { class: "empty" }, "No git repositories in this workspace yet."));
    return;
  }
  box.replaceChildren(...data.repos.map((repo) => {
    const files = splitDiff(repo.diff);
    return h("section", { class: "card" },
      h("h3", {}, repo.path === "." ? "workspace" : repo.path),
      h("div", { class: "meta" }, h("span", {}, `branch ${repo.branch}`), repo.base ? h("span", {}, `since ${repo.base.slice(0, 8)}`) : null,
        h("span", {}, `${repo.files.length} changed file${repo.files.length === 1 ? "" : "s"}`)),
      repo.commits.length ? h("details", { style: "margin-top:8px" }, h("summary", {}, `${repo.commits.length} new commit${repo.commits.length === 1 ? "" : "s"}`),
        h("pre", { class: "small", style: "white-space:pre-wrap" }, repo.commits.join("\n"))) : null,
      files.length ? files.map((f) => h("details", { class: "file", open: files.length <= 4 },
        h("summary", {}, f.name),
        h("div", { class: "diff" }, f.lines.map((line) => h("div", {
          class: line.startsWith("@@") ? "hunk" : line.startsWith("+") && !line.startsWith("+++") ? "add"
            : line.startsWith("-") && !line.startsWith("---") ? "del" : "",
        }, line))))) : h("p", { class: "muted small" }, "No differences."),
      repo.truncated ? h("p", { class: "note" }, "Diff truncated.") : null);
  }));
}

function splitDiff(diff) {
  const files = [];
  let cur = null;
  for (const line of (diff || "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { name: m ? m[1] : line, lines: [] };
      files.push(cur);
    } else if (cur && !/^(index |new file mode|deleted file mode|--- |\+\+\+ )/.test(line)) {
      cur.lines.push(line);
    }
  }
  files.forEach((f) => { while (f.lines.length && !f.lines[f.lines.length - 1]) f.lines.pop(); });
  return files;
}

function viewInfo(s) {
  const t = s.totals || {};
  const rows = [
    ["Title", s.title], ["Session", s.id], ["Status", `${s.status}${s.stop_reason ? ` (${s.stop_reason})` : ""}`],
    ["Project", s.project], ["Target", s.target], ["Backend", s.backend || "local"], ["Model", s.model],
    ["Created", new Date(s.created_at * 1000).toLocaleString()], ["Updated", new Date(s.updated_at * 1000).toLocaleString()],
    ["Model turns", t.turns || 0], ["Prompt tokens", t.prompt_tokens || 0], ["Completion tokens", t.completion_tokens || 0],
    ["Workspace", s.workspace_removed ? `${s.workspace} (removed)` : s.workspace],
  ];
  if (s.branch) rows.push(["Branch", `${s.branch}${s.base_branch ? ` from ${s.base_branch}` : ""}`], ["Review", s.review || "pending"]);
  $app.append(h("div", { class: "card" }, rows.map(([k, v]) => h("div", { class: "row", style: "justify-content:space-between;padding:4px 0" },
    h("span", { class: "muted" }, k), h("span", { style: "overflow-wrap:anywhere;text-align:right" }, String(v))))),
  h("a", { class: "btn", href: `/sessions/${s.id}/transcript`, target: "_blank" }, "Open Markdown transcript"));
}

// ---------- images ----------
const IMAGE_PHASE = {
  idle: "", waiting: "Waiting for the GPU (a game or transcode is using it)…", switching: "Unloading the language model…",
  starting: "Starting ComfyUI…", generating: "Generating…", lingering: "Done; keeping the image model loaded for another minute",
  restoring: "Reloading the language model…",
};

function imageCard(img) {
  const ready = img.status === "done";
  return h("a", { class: "card image-card", href: `#/images/${img.id}` },
    ready ? h("img", { src: `/images/${img.id}.png`, alt: img.prompt, loading: "lazy" })
      : h("div", { class: `image-placeholder ${img.status}` }, img.status === "failed" ? "failed" : h("span", { class: "dots" }, img.status)),
    h("div", { class: "preview small" }, img.prompt));
}

async function viewImages() {
  setHeader("images");
  let data;
  try { data = await api("/images"); } catch (e) { $app.append(h("p", { class: "note bad" }, e.message)); return; }
  const prompt = h("textarea", { placeholder: "Describe the image…" });
  const draftKey = "harness.imageDraft";
  try { prompt.value = localStorage.getItem(draftKey) || ""; } catch (_) { /* private mode */ }
  prompt.addEventListener("input", () => { try { localStorage.setItem(draftKey, prompt.value); } catch (_) { /* ignore */ } });
  const model = h("select", {}, Object.entries(data.status.models).map(([k, label]) => h("option", { value: k }, label)));
  const aspect = h("select", {}, data.status.aspect_ratios.map((a) => h("option", { value: a }, a)));
  let resolutionTouched = false;
  const resolutionInputs = Object.entries(data.status.resolutions).map(([name, spec]) => {
    const input = h("input", { type: "radio", name: "resolution", value: name, checked: name === "standard" });
    input.addEventListener("change", () => { resolutionTouched = true; });
    const size = h("span", { class: "size" });
    const label = h("label", { class: "resolution-option" }, input, spec.label, size);
    return { name, spec, input, size, label };
  });
  const renderResolutions = () => {
    for (const choice of resolutionInputs) {
      const [w, height] = choice.spec.sizes[aspect.value];
      choice.size.textContent = `${w} × ${height}`;
    }
  };
  aspect.addEventListener("change", renderResolutions);
  model.addEventListener("change", () => {
    if (!resolutionTouched) {
      const recommended = model.value === "quality" ? "high" : "standard";
      resolutionInputs.find((choice) => choice.name === recommended).input.checked = true;
    }
    renderResolutions();
  });
  renderResolutions();
  const phase = h("p", { class: "note" });
  const grid = h("div", { class: "image-grid" });
  const render = (d) => {
    const s = d.status;
    const text = s.phase in IMAGE_PHASE ? IMAGE_PHASE[s.phase] : s.phase;
    fill(phase, text ? h("span", { class: s.phase === "lingering" ? "" : "dots" },
      `${text}${s.progress && s.progress.seconds ? ` ${s.progress.seconds} s` : ""}${s.queued ? ` · ${s.queued} queued` : ""}`) : "");
    fill(grid, d.images.map(imageCard));
  };
  render(data);
  const go = h("button", { class: "btn primary", type: "submit" }, "Generate");
  $app.append(
    h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!prompt.value.trim()) return toast("Describe the image first");
        go.disabled = true;
        try {
          const resolution = resolutionInputs.find((choice) => choice.input.checked).name;
          await api("/images", { method: "POST", body: { prompt: prompt.value, model: model.value, aspect_ratio: aspect.value, resolution } });
          try { localStorage.removeItem(draftKey); } catch (_) { /* ignore */ }
          render(await api("/images"));
        } catch (err) { toast(err.message); }
        go.disabled = false;
      },
    },
    h("label", {}, "Prompt"), prompt,
    h("div", { class: "row" }, h("div", { style: "flex:2" }, h("label", {}, "Model"), model),
      h("div", { style: "flex:1" }, h("label", {}, "Aspect ratio"), aspect)),
    h("div", { class: "resolution-group" }, h("div", { class: "field-label" }, "Resolution"),
      h("div", { class: "resolution-options" }, resolutionInputs.map((choice) => choice.label))),
    h("p", { class: "muted small" }, "The language model is unloaded while images generate; running tasks pause for a few minutes."),
    h("div", { class: "row", style: "margin-top:12px" }, h("span", { class: "spacer" }), go)),
    phase, grid);
  const timer = setInterval(async () => { try { render(await api("/images")); } catch (_) { /* offline */ } }, 4000);
  onLeave(() => clearInterval(timer));
}

async function viewImage(id) {
  setHeader("images", "Image");
  const load = async () => {
    const img = await api(`/images/${id}`);
    const when = img.finished_at ? ago(img.finished_at) : ago(img.created_at);
    fill($app,
      img.status === "done" ? h("a", { href: `/images/${id}.png`, target: "_blank" }, h("img", { class: "image-full", src: `/images/${id}.png`, alt: img.prompt }))
        : h("p", { class: `note${img.status === "failed" ? " bad" : ""}` }, img.status === "failed" ? `Failed: ${img.error}` : h("span", { class: "dots" }, IMAGE_PHASE[img.service.phase] || img.status)),
      h("div", { class: "card" },
        h("p", {}, img.prompt),
        h("p", { class: "muted small" }, `${img.model} · ${img.width}×${img.height} · seed ${img.seed} · ${img.source}${img.seconds ? ` · ${Math.round(img.seconds)} s` : ""} · ${when}`),
        h("div", { class: "row" },
          h("button", {
            class: "btn",
            onclick: async () => {
              try {
                const again = await api("/images", { method: "POST", body: { prompt: img.prompt, model: img.model, aspect_ratio: img.aspect_ratio, resolution: img.resolution } });
                location.hash = `#/images/${again.id}`;
              } catch (e) { toast(e.message); }
            },
          }, "Another one"),
          img.session_id ? h("a", { class: "btn", href: `#/s/${img.session_id}` }, "Open session") : null)));
    return img;
  };
  let img = await load();
  const timer = setInterval(async () => {
    if (img.status === "done" || img.status === "failed") return clearInterval(timer);
    try { img = await load(); } catch (_) { /* offline */ }
  }, 3000);
  onLeave(() => clearInterval(timer));
}

// ---------- scheduled jobs ----------
const JOB_NOTIFY = {
  attention: "Only when something needs attention",
  low: "Quietly when OK (low-priority notification)",
  always: "Every run",
};
const CRON_PRESETS = [
  ["0 8 * * *", "Every day at 8:00"], ["0 7 * * 1-5", "Weekdays at 7:00"], ["0 * * * *", "Every hour"],
  ["*/30 * * * *", "Every 30 minutes"], ["0 10 * * 0", "Sundays at 10:00"], ["0 9 1 * *", "Monthly, on the 1st at 9:00"],
];
const jobStatusBadge = (st) => h("span", { class: `badge ${st === "ok" ? "done" : "waiting_approval"}` }, st === "ok" ? "OK" : "⚠ attention");
const fmtWhen = (ts) => new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const whenText = (ts) => {
  if (!ts) return "—";
  const s = ts - Date.now() / 1000;
  const rel = s < 0 ? "due" : s < 3600 ? `in ${Math.max(1, Math.round(s / 60))} min` : s < 86400 ? `in ${Math.round(s / 3600)} h` : `in ${Math.round(s / 86400)} d`;
  return `${fmtWhen(ts)} (${rel})`;
};
const cronLabel = (cron) => (CRON_PRESETS.find(([c]) => c === cron) || [null, cron])[1];

async function viewJobs() {
  setHeader("jobs");
  document.body.append(h("a", { class: "btn new-task fab", href: "#/jobs/new" }, "+ New job"));
  const jobs = await api("/jobs");
  if (!jobs.length) {
    $app.append(h("p", { class: "empty" }, "No scheduled jobs yet. A job runs a task on a schedule, such as a morning homelab check, and notifies you only when something needs attention."));
    return;
  }
  $app.append(...jobs.map((j) => {
    const last = j.recent[0];
    return h("a", { class: "card", href: `#/jobs/${j.id}` },
      h("h3", {}, `${j.enabled ? "" : "⏸ "}${j.name}`),
      h("div", { class: "meta" }, h("span", {}, cronLabel(j.cron)), h("span", {}, j.project),
        j.enabled ? h("span", {}, `next ${whenText(j.next_run_at)}`) : h("span", {}, "paused")),
      last ? h("div", { class: "meta", style: "margin-top:4px" }, h("span", {}, `last run ${ago(last.created_at)}`),
        badge(last.status), last.job_status ? jobStatusBadge(last.job_status) : null) : null,
      j.last_error ? h("div", { class: "preview bad" }, `Couldn't start: ${j.last_error}`) : null);
  }));
}

async function viewJob(id) {
  const isNew = id === "new";
  setHeader("jobs", isNew ? "New job" : "Job");
  const [projects, models, backends, job] = await Promise.all([
    api("/projects"), api("/models"), api("/backends"), isNew ? null : api(`/jobs/${id}`)]);
  const j = job || {
    name: "", prompt: "", cron: "0 8 * * *", backend: "local", model: "", notify: "low", enabled: true,
    project: projects.some((p) => p.name === "homelab") ? "homelab" : "scratch",
  };
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
      try {
        const r = await api(`/jobs/preview?cron=${encodeURIComponent(cron.value)}`);
        cronNote.classList.toggle("bad", !r.ok);
        cronNote.textContent = r.ok ? `Next: ${r.next.map(fmtWhen).join(" · ")}` : r.error;
      } catch (_) { /* offline */ }
    }, 250);
  };
  preset.addEventListener("change", () => { if (preset.value) { cron.value = preset.value; preview(); } else cron.focus(); });
  cron.addEventListener("input", () => {
    const match = CRON_PRESETS.find(([c]) => c === cron.value.trim());
    preset.value = match ? match[0] : "";
    preview();
  });
  preview();
  const body = () => ({ name: name.value, prompt: prompt.value, cron: cron.value, project: project.value,
    backend: backend.value, model: backend.value === "local" ? model.value : "", notify: notify.value, enabled: enabled.checked });
  const save = h("button", { class: "btn primary", type: "submit" }, isNew ? "Create" : "Save");
  $app.append(h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        const saved = await api(isNew ? "/jobs" : `/jobs/${id}`, { method: isNew ? "POST" : "PUT", body: body() });
        toast(`Saved · next run ${whenText(saved.next_run_at)}`, 3500);
        if (isNew) location.hash = `#/jobs/${saved.id}`; else route();
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
    !isNew ? h("button", {
      class: "btn bad", type: "button",
      onclick: async () => {
        if (!confirm(`Delete the job “${j.name}”? Its past sessions stay.`)) return;
        try { await api(`/jobs/${id}`, { method: "DELETE" }); go("#/jobs", true); } catch (err) { toast(err.message); }
      },
    }, "Delete") : null,
    h("span", { class: "spacer" }),
    !isNew ? h("button", {
      class: "btn", type: "button",
      onclick: async () => {
        try { const s = await api(`/jobs/${id}/run`, { method: "POST" }); location.hash = `#/s/${s.id}`; } catch (err) { toast(err.message); }
      },
    }, "Run now") : null,
    save)));
  if (job) {
    $app.append(h("h3", { style: "margin-top:28px" }, "Recent runs"),
      job.last_skip ? h("p", { class: "muted small" }, `Last skipped: ${job.last_skip}`) : null,
      job.last_error ? h("p", { class: "small bad" }, `Last start failed: ${job.last_error}`) : null,
      job.recent.length ? job.recent.map((s) => h("a", { class: "card", href: `#/s/${s.id}` },
        h("div", { class: "meta" }, badge(s.status), s.job_status ? jobStatusBadge(s.job_status) : null, h("span", {}, ago(s.created_at))),
        s.answer ? h("div", { class: "preview" }, s.answer) : null)) : h("p", { class: "muted small" }, "No runs yet."));
  }
}

// ---------- profile ----------
const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || !!navigator.standalone;
const PROFILE_PAGES = {
  appearance: "Appearance",
  notifications: "Notifications",
  install: "Install",
  backends: "Backends",
  memory: "Memory library",
  apps: "Apps",
  endpoint: "Inference endpoint",
  disk: "Disk",
};
const THEMES = {
  auto: { label: "System", swatch: ["#f6f7f9", "#ffffff", "#2563eb"] },
  light: { label: "Light", swatch: ["#f6f7f9", "#ffffff", "#2563eb"] },
  dark: { label: "Dark", swatch: ["#000000", "#232323", "#dddddd"] },
  midnight: { label: "Midnight", swatch: ["#0b1220", "#152038", "#7dd3fc"] },
  forest: { label: "Forest", swatch: ["#0f1a14", "#1a2c22", "#86efac"] },
  paper: { label: "Paper", swatch: ["#f4efe6", "#fffaf2", "#9a3412"] },
  custom: { label: "Custom", swatch: ["#888888", "#aaaaaa", "#2563eb"] },
};
const THEME_COLORS = { bg: "#f6f7f9", panel: "#ffffff", accent: "#2563eb" };

function readTheme() {
  try { return localStorage.getItem("harness.theme") || "auto"; } catch (_) { return "auto"; }
}
function readHues() {
  try { return { ...THEME_COLORS, ...(JSON.parse(localStorage.getItem("harness.themeHues") || "null") || {}) }; }
  catch (_) { return { ...THEME_COLORS }; }
}
function applyTheme(name, hues) {
  const root = document.documentElement;
  const theme = name || readTheme();
  const colors = hues || readHues();
  if (theme && theme !== "auto") root.dataset.theme = theme;
  else delete root.dataset.theme;
  for (const key of ["bg", "panel", "accent"]) {
    if (theme === "custom" && colors[key]) root.style.setProperty(`--${key}`, colors[key]);
    else root.style.removeProperty(`--${key}`);
  }
  const meta = document.querySelector('meta[name="theme-color"]:not([media])')
    || document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(root).getPropertyValue("--bg").trim() || "#000000";
  try {
    localStorage.setItem("harness.theme", theme);
    localStorage.setItem("harness.themeHues", JSON.stringify(colors));
  } catch (_) { /* private mode */ }
}
applyTheme();

function copyBox(value) {
  const code = h("code", {}, value);
  const btn = h("button", {
    class: "btn small", type: "button",
    onclick: async () => {
      try { await navigator.clipboard.writeText(value); toast("Copied"); }
      catch (_) { const range = document.createRange(); range.selectNodeContents(code); getSelection().removeAllRanges(); getSelection().addRange(range); }
    },
  }, "Copy");
  return h("div", { class: "copy-box", onclick: () => btn.click() }, code, btn);
}

function emojiPicker(profile, onPick) {
  return h("div", { class: "emoji-grid" }, profile.choices.map((emoji) => h("button", {
    class: `btn emoji-choice${emoji === profile.emoji ? " selected" : ""}`, type: "button", "aria-label": `Use ${emoji}`,
    onclick: async (event) => {
      try {
        await api("/profile", { method: "PUT", body: { emoji } });
        $profileIcon.textContent = emoji;
        profile.emoji = emoji;
        for (const button of event.currentTarget.parentNode.children) button.classList.toggle("selected", button === event.currentTarget);
        onPick?.(emoji);
      } catch (e) { toast(e.message); }
    },
  }, emoji)));
}

async function viewProfile(page) {
  if (page && !PROFILE_PAGES[page]) { go("#/profile", true); return; }
  if (page === "install" && isStandalone()) { go("#/profile", true); return; }
  setHeader("agents", PROFILE_PAGES[page] || "Profile", { page: true });
  const [me, profile] = await Promise.all([api("/me"), api("/profile")]);
  if (page === "appearance") return $app.append(appearanceCard());
  if (page === "notifications") return $app.append(notificationsCard(me));
  if (page === "install") return $app.append(installCard());
  if (page === "backends") return $app.append(await backendsCard());
  if (page === "memory") return $app.append(memoryCard());
  if (page === "apps") return $app.append(appsCard(me));
  if (page === "endpoint") return $app.append(endpointCard(me));
  if (page === "disk") return $app.append(diskCard());
  const activeEmoji = h("span", { class: "identity-emoji" }, profile.emoji);
  const picker = h("div", { class: "card", hidden: true },
    h("p", { class: "muted small" }, "Shown at the top left of the app."),
    emojiPicker(profile, (emoji) => { activeEmoji.textContent = emoji; }));
  $app.append(
    h("button", {
      class: "card identity", type: "button",
      onclick: () => { picker.hidden = !picker.hidden; },
    },
      h("div", { class: "row" },
        activeEmoji,
        h("div", { class: "spacer" },
          h("h3", {}, me.name || "You"),
          h("div", { class: "muted small" }, "Tap to change your icon")),
        h("span", { class: "chevron", "aria-hidden": "true" }, "›"))),
    picker,
    gpuCard(),
    remoteControlCard(),
    h("p", { class: "section-label" }, "Settings"),
    h("div", { class: "card settings-list" },
      Object.entries(PROFILE_PAGES)
        .filter(([id]) => id !== "install" || !isStandalone())
        .map(([id, label]) => h("a", { href: `#/profile/${id}` }, label))),
  );
}

function appearanceCard() {
  let theme = readTheme();
  let hues = readHues();
  const hueRow = h("div", { class: "hue-row", hidden: theme !== "custom" });
  const grid = h("div", { class: "theme-grid" });
  const paint = () => {
    fill(grid, Object.entries(THEMES).map(([id, spec]) => h("button", {
      class: `theme-choice${theme === id ? " on" : ""}`, type: "button",
      onclick: () => { theme = id; applyTheme(theme, hues); hueRow.hidden = theme !== "custom"; paint(); },
    },
      h("div", { class: "swatch" }, spec.swatch.map((color, i) => h("span", { style: `background:${i === 2 && id === "custom" ? hues.accent : color}` }))),
      h("div", { class: "name" }, spec.label))));
  };
  paint();
  fill(hueRow, ["bg", "panel", "accent"].map((key) => {
    const input = h("input", { type: "color", value: /^#[0-9a-fA-F]{6}$/.test(hues[key]) ? hues[key] : THEME_COLORS[key] });
    input.addEventListener("input", () => {
      hues = { ...hues, [key]: input.value };
      applyTheme("custom", hues);
      theme = "custom";
      paint();
    });
    return h("label", {}, key === "bg" ? "Background" : key === "panel" ? "Panel" : "Accent", input);
  }));
  return h("div", { class: "card" },
    h("p", { class: "muted small" }, "How the app looks on this phone. The icon lives on your profile card."),
    grid, hueRow);
}

function notificationsCard(me) {
  const ntfyUrl = me.public_url ? `${me.public_url}:8443` : "(set public_url)";
  return h("div", { class: "card" },
    me.notify.enabled ? h("ol", {},
      h("li", {}, "Install the ntfy app from the App Store."),
      h("li", {}, "In ntfy: Settings → Users → add ", h("code", {}, ntfyUrl), " with the phone username and password (D:\\Docker\\ntfy\\secrets\\phone-login.txt on the tower)."),
      h("li", {}, "Settings → Default server → the same URL. Then + → topic ", h("code", {}, me.notify.topic), "."),
      h("li", {}, "Tap a notification to open the session; long-press it for Approve / Deny.")) : h("p", {}, "Disabled in config/harness.yaml."),
    me.notify.enabled ? h("button", {
      class: "btn",
      onclick: async () => {
        try { await api("/notify/test", { method: "POST" }); toast("Test notification sent"); } catch (e) { toast(e.message); }
      },
    }, "Send test notification") : null);
}

function installCard() {
  return h("div", { class: "card" },
    h("p", {}, "In Safari: Share → Add to Home Screen. The app then opens full screen."));
}

function backendUsage(b) {
  if (b.name === "local") return "Local Qwen on this PC";
  const limits = b.limits || {};
  const pct = limits.utilization === undefined ? null : Math.round(limits.utilization * 100);
  const period = String(limits.rateLimitType || "limit").replace("seven_day", "7d").replace("five_hour", "5h");
  const sub = pct === null ? (b.logged_in ? "signed in" : "sign-in needed") : `${period} ${pct}% used`;
  const req = `${b.today.requests || 0} today · ${b.week.requests || 0} this week`;
  const cost = Number(b.today.cost_usd || 0) + Number(b.week.cost_usd || 0);
  const dollars = cost > 0 ? ` · $${Number(b.today.cost_usd || 0).toFixed(2)} today` : "";
  return `${b.logged_in ? "signed in" : "sign-in/key needed"} · ${sub} · ${req}${dollars}`;
}

async function backendsCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Checking…"));
  let failed = false;
  const [rows, models] = await Promise.all([api("/backends"), api("/models")]).catch((e) => {
    fill(body, h("p", { class: "note bad" }, e.message));
    failed = true;
    return [[], []];
  });
  if (failed) {
    return h("div", { class: "card" }, body);
  }
  const effortSelect = (b) => {
    const sel = h("select", {}, ["low", "medium", "high"].map((level) =>
      h("option", { value: level, selected: (b.effort || "high") === level }, level)));
    sel.addEventListener("change", async () => {
      try { await api(`/backends/${b.name}`, { method: "PUT", body: { effort: sel.value } }); toast(`Saved ${b.name} effort`); }
      catch (e) { toast(e.message); }
    });
    return sel;
  };
  const modelControl = (b) => {
    if (b.name === "local") {
      const sel = h("select", {}, models.map((m) => h("option", { value: m.name, selected: m.name === b.model }, m.name)));
      sel.addEventListener("change", async () => {
        try { await api("/backends/local", { method: "PUT", body: { model: sel.value } }); toast("Saved local model"); }
        catch (e) { toast(e.message); }
      });
      return sel;
    }
    const input = h("input", { type: "text", value: b.model || "", placeholder: "Provider model name" });
    input.addEventListener("change", async () => {
      const model = input.value.trim();
      if (!model) return toast("Model is empty");
      try { await api(`/backends/${b.name}`, { method: "PUT", body: { model } }); toast(`Saved ${b.name} model`); }
      catch (e) { toast(e.message); }
    });
    return input;
  };
  const title = (b) => (b.name === "local" ? "Qwen (this PC)" : b.name === "claude" ? "Claude" : b.name === "codex" ? "Codex" : b.name === "cursor" ? "Cursor" : b.name);
  fill(body, rows.length ? rows.map((b) => h("div", { class: "backend-block" },
    h("strong", {}, title(b)),
    h("p", { class: `small ${b.billing_warning ? "bad" : "muted"}` }, b.billing_warning || backendUsage(b)),
    h("label", {}, "Default model"), modelControl(b),
    b.name === "local" ? null : [h("label", {}, "Effort"), effortSelect(b)],
  )) : h("p", { class: "muted small" }, "No backends configured."));
  return h("div", { class: "card" },
    h("p", { class: "muted small" }, "New tasks use these defaults. You can still pick a backend when you start one."),
    body);
}

function backupLine(b) {
  if (!b || !b.enabled) return null;
  const failed = b.error && (b.error_at || 0) > (b.ok_at || 0);
  return h("p", { class: `small${failed ? " bad" : ""}` },
    b.ok_at ? `Backup ${ago(b.ok_at)} (${Math.max(1, Math.round(b.bytes / 2 ** 20))} MB) in ${b.dir}` : "No backup yet",
    failed ? ` · last attempt failed: ${b.error}` : "");
}

function gpuText(g) {
  const why = (g.reasons || []).map((r) => r.detail).filter((d, i, a) => a.indexOf(d) === i).join(", ") || "GPU busy";
  if (g.state === "pausing") return `Pausing for ${why}: finishing the current model turn`;
  if (g.state === "paused") return g.manual ? "Paused from the app" : `Paused for ${why}`;
  if (g.state === "resuming") return "GPU free: reloading the model";
  return "Agents have the GPU";
}

function gpuCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Checking…"));
  const act = async (action) => {
    try { render(await api(`/gpu/${action}`, { method: "POST" })); } catch (e) { toast(e.message); }
    setTimeout(load, 1500);
  };
  const render = (g) => {
    if (!g.enabled) return fill(body, h("p", { class: "muted small" }, "The GPU guard is disabled in config/harness.yaml."));
    const now = g.signals.map((s) => s.detail);
    const left = g.resume_after_seconds - (g.clear_for_seconds || 0);
    const reload = g.state === "paused" && !now.length && !g.manual && g.clear_for_seconds !== null
      ? ` · reloads in ${left >= 90 ? `${Math.round(left / 60)} min` : `${Math.max(0, Math.round(left))} s`}` : "";
    fill(body,
      h("p", {}, `${g.state === "clear" ? "✓" : "⏸"} ${gpuText(g)}${reload}`),
      h("p", { class: "muted small" }, now.length ? `Using the GPU now: ${now.join(", ")}${g.override ? " (ignored until that changes)" : ""}`
        : "No game or Plex transcode detected. Desktop streaming doesn't pause agents."),
      g.plex_error ? h("p", { class: "muted small" }, `Plex check: ${g.plex_error}`) : null,
      h("div", { class: "row" },
        g.state === "clear" ? h("button", { class: "btn", onclick: () => act("pause") }, "Pause agents (I'm gaming)")
          : h("button", { class: "btn", onclick: () => act("resume") }, now.length ? "Resume anyway" : "Resume now")));
  };
  const load = async () => { try { render(await api("/gpu")); } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); } };
  load();
  const timer = setInterval(load, 5000);
  onLeave(() => clearInterval(timer));
  return h("div", { class: "card" }, h("h3", {}, "GPU"), body);
}

function remoteControlCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Checking…"));
  let busy = "";
  const act = async (project, stop) => {
    busy = project;
    load();
    try {
      const r = await api(`/remote-control/${encodeURIComponent(project)}${stop ? "/stop" : ""}`, { method: "POST" });
      toast(stop ? `Stopped Remote Control for ${project}` : r.already_running ? "Already running" : "Remote Control is ready");
    } catch (e) { toast(e.message); }
    busy = "";
    load();
  };
  const trust = async (project) => {
    if (!confirm(`Open Claude on the tower to trust “${project}”?\n\nReview the folder shown by Claude, then accept its workspace trust prompt. The harness cannot accept it for you.`)) return;
    busy = project;
    load();
    try {
      const r = await api(`/remote-control/${encodeURIComponent(project)}/trust`, { method: "POST" });
      toast(r.already_trusted ? "This repository is already trusted" : r.already_open ? "The trust window is already open" : "Claude trust window opened on the tower", 5000);
    } catch (e) { toast(e.message); }
    busy = "";
    load();
  };
  const row = (p) => {
    const state = busy === p.project ? h("span", { class: "dots" }, "working")
      : p.running ? `running${p.active_sessions ? ` · ${p.active_sessions} session${p.active_sessions === 1 ? "" : "s"}` : ""} · started ${ago(p.started_at)}`
      : !p.trusted && p.trust_prompt_open ? "trust window open on the tower"
        : !p.trusted ? "needs one-time Claude workspace trust" : "stopped";
    return h("div", { class: "rc-row" },
      h("p", {}, h("strong", {}, p.project), " ", h("span", { class: `muted small${!p.running && !p.trusted ? " bad" : ""}` }, state)),
      h("p", { class: "muted small" }, p.path),
      !p.trusted && p.trust_prompt_open ? h("p", { class: "note small" }, "On the tower, review the folder in Claude and accept its trust prompt. This page will notice automatically.") : null,
      h("div", { class: "row" },
        p.running && p.pairing_url ? h("a", { class: "btn", href: p.pairing_url, target: "_blank", rel: "noopener" }, "Open in Claude") : null,
        p.running ? h("button", { class: "btn", disabled: !!busy, onclick: () => act(p.project, true) }, "Stop")
          : !p.trusted ? h("button", { class: "btn", disabled: !!busy || p.trust_prompt_open, onclick: () => trust(p.project) }, p.trust_prompt_open ? "Trust window open" : "Trust in Claude…")
            : h("button", { class: "btn", disabled: !!busy, onclick: () => act(p.project, false) }, "Start")));
  };
  const load = async () => {
    try {
      const r = await api("/remote-control");
      if (!r.enabled) return fill(body, h("p", { class: "muted small" }, "Disabled in config/harness.yaml (remote_control)."));
      fill(body,
        h("p", { class: "muted small" }, "Start Claude Code in a project folder and continue in the Claude app. These sessions use your Claude subscription, not the harness."),
        r.projects.length ? r.projects.map(row) : h("p", { class: "muted small" }, "No tower projects with a local folder."));
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  const timer = setInterval(() => { if (!busy) load(); }, 3000);
  onLeave(() => clearInterval(timer));
  return h("div", { class: "card" }, h("h3", {}, "Claude Remote Control"), body);
}

function memoryCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Loading…"));
  (async () => {
    try {
      const mem = await api("/memory");
      if (!mem.enabled) return fill(body, h("p", { class: "muted small" }, "The memory library is disabled in config/harness.yaml."));
      const editor = h("textarea", { rows: 8 }, mem.profile || "");
      const save = h("button", { class: "btn primary", disabled: !mem.writes }, "Save");
      save.addEventListener("click", async () => {
        if (!confirm("Save this profile to the memory library? It is given to every new session, then committed and pushed.")) return;
        save.disabled = true;
        try {
          const saved = await api("/memory/profile", { method: "PUT", body: { content: editor.value, summary: "Update agent profile from Settings" } });
          toast(`Saved (${saved.last_commit.head})`);
        } catch (e) { toast(e.message); }
        save.disabled = !mem.writes;
      });
      fill(body,
        h("p", { class: "small" }, "A short purpose statement given to every new session: how agents should use this library. Keep personal biography in category files, not here."),
        h("p", { class: "small" }, `Agents can read ${mem.categories.join(", ")}. `,
          mem.writes ? "They can propose changes; every change asks you first." : "Read-only for agents."),
        editor,
        h("p", { class: "muted small" }, `${mem.profile_path || "profile"} · limit ${mem.profile_max_chars} characters`),
        mem.writes ? save : h("p", { class: "muted small" }, "Enable memory_library.writes to edit from here."),
        mem.last_commit?.head ? h("p", { class: "muted small" }, `Last saved change: ${mem.last_commit.summary} (${mem.last_commit.head}, ${ago(mem.last_commit.at)})`) : null,
        mem.refresh_error ? h("p", { class: "small bad" }, `Couldn't refresh the library: ${mem.refresh_error}`) : null);
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  })();
  return h("div", { class: "card" }, body);
}

function endpointCard(me) {
  const base = me.public_url || location.origin;
  const body = h("div", {}, h("p", { class: "muted small" }, "Loading…"));
  const load = async () => {
    try {
      const keys = await api("/keys");
      const active = keys.filter((k) => !k.revoked_at && k.kind !== "app");
      const form = h("div");
      const newBtn = h("button", { class: "btn", type: "button", onclick: () => { newBtn.hidden = true; showForm(); } }, "New key");
      const showForm = () => {
        const name = h("input", { type: "text", placeholder: "Name (the device or app)" });
        fill(form,
          h("p", { class: "small" }, "The token is shown once. Anyone with it can call the inference endpoint as you."),
          h("label", {}, "Key name"), name,
          h("div", { class: "row", style: "margin-top:10px" },
            h("button", { class: "btn", type: "button", onclick: load }, "Cancel"),
            h("span", { class: "spacer" }),
            h("button", {
              class: "btn primary", type: "button",
              onclick: async () => {
                if (!name.value.trim()) return toast("Name the key");
                try {
                  const k = await api("/keys", { method: "POST", body: { name: name.value } });
                  const field = h("input", { type: "text", readonly: true, value: k.key, onclick: (e) => e.target.select() });
                  fill(form, h("p", { class: "small" }, `Key for ${k.name}. Copy it now; it isn't shown again.`), field,
                    h("div", { class: "row", style: "margin-top:8px" },
                      h("button", { class: "btn", onclick: async () => { try { await navigator.clipboard.writeText(k.key); toast("Copied"); } catch (_) { field.select(); } } }, "Copy"),
                      h("button", { class: "btn", onclick: load }, "Done")));
                } catch (e) { toast(e.message); }
              },
            }, "Create")));
      };
      fill(body,
        h("p", { class: "muted small" }, "Point a coding tool at these URLs. Tap a box to copy. Any model name works; unknown names use the default."),
        h("p", { class: "small", style: "margin-bottom:0" }, "OpenAI-compatible"),
        copyBox(`${base}/v1`),
        h("p", { class: "small", style: "margin-bottom:0" }, "Anthropic-compatible"),
        copyBox(base),
        active.length ? h("ul", { class: "small" }, active.map((k) => h("li", {},
          h("strong", {}, k.name), ` ${k.prefix}… · ${k.requests} request${k.requests === 1 ? "" : "s"}${k.last_used_at ? ` · used ${ago(k.last_used_at)}` : ""} `,
          h("button", {
            class: "btn small bad",
            onclick: async () => {
              if (!confirm(`Revoke the key “${k.name}”? Tools using it stop working.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
            },
          }, "Revoke")))) : h("p", { class: "muted small" }, "No keys yet."),
        form, newBtn);
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  return h("div", { class: "card" }, body);
}

const APP_SCOPES = {
  sessions: "Start and follow its own sessions (with context and tools)",
  "sessions:all": "Read all sessions, not only its own",
  approvals: "Approve or deny in its own sessions",
  images: "Generate images",
  inference: "Use the inference endpoint",
  remote_control: "Start and stop Claude Remote Control in a project folder",
};

function appsCard(me) {
  const base = me.public_url || location.origin;
  const body = h("div", {}, h("p", { class: "muted small" }, "Loading…"));
  const load = async () => {
    try {
      const apps = (await api("/keys")).filter((k) => k.kind === "app" && !k.revoked_at);
      const form = h("div");
      const newBtn = h("button", { class: "btn", type: "button", onclick: () => { newBtn.hidden = true; showForm(); } }, "New app");
      const showForm = () => {
        const name = h("input", { type: "text", placeholder: "App name" });
        const boxes = Object.entries(APP_SCOPES).map(([scope, label]) => h("label", { class: "small", style: "display:block;font-weight:normal" },
          h("input", { type: "checkbox", value: scope, checked: scope === "sessions" }), ` ${label}`));
        fill(form,
          h("p", { class: "small" }, "This mints a token shown once. Anyone with it can use the permissions you tick. It is not your Claude/Codex/Cursor login."),
          h("label", {}, "Name"), name, h("label", {}, "What it may do"), boxes,
          h("div", { class: "row", style: "margin-top:10px" },
            h("button", { class: "btn", onclick: load }, "Cancel"), h("span", { class: "spacer" }),
            h("button", {
              class: "btn primary",
              onclick: async () => {
                const scopes = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
                if (!name.value.trim() || !scopes.length) return toast("Name the app and allow at least one thing");
                try {
                  const k = await api("/keys", { method: "POST", body: { name: name.value, kind: "app", scopes } });
                  const field = h("input", { type: "text", readonly: true, value: k.key, onclick: (e) => e.target.select() });
                  fill(form, h("p", { class: "small" }, `Token for ${k.name}. Copy it now; it isn't shown again.`), field,
                    h("div", { class: "row", style: "margin-top:8px" },
                      h("button", { class: "btn", onclick: async () => { try { await navigator.clipboard.writeText(k.key); toast("Copied"); } catch (_) { field.select(); } } }, "Copy"),
                      h("button", { class: "btn", onclick: load }, "Done")));
                } catch (e) { toast(e.message); }
              },
            }, "Create")));
      };
      fill(body,
        h("p", { class: "small" }, "An app token lets another program start and follow sessions on this daemon — a script, a bot, or a future phone client. It is shown once and can be revoked later."),
        h("p", { class: "muted small" }, "API: ", h("code", {}, `${base}/api/v1`), " · guide: docs/app-api.md"),
        apps.length ? h("ul", { class: "small" }, apps.map((k) => h("li", {},
          h("strong", {}, k.name), ` ${k.prefix}… · ${k.scopes.split(" ").join(", ")}${k.last_used_at ? ` · used ${ago(k.last_used_at)}` : ""} `,
          h("button", {
            class: "btn small bad",
            onclick: async () => {
              if (!confirm(`Revoke the app “${k.name}”? It can no longer start or read sessions.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
            },
          }, "Revoke")))) : h("p", { class: "muted small" }, "No apps yet."),
        form, newBtn);
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  return h("div", { class: "card" }, body);
}

function diskCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Measuring…"));
  const load = async () => {
    try {
      const u = await api("/maintenance");
      const mb = (n) => (n < 1 ? "<1 MB" : `${n} MB`);
      const device = (name, free, total, extra) => h("div", { class: "disk-device" },
        h("strong", {}, name),
        h("div", { class: "disk-meter" },
          h("span", {}, `${Math.round(Number(free)) || free} GB free`),
          total != null ? h("span", { class: "muted" }, `of ${Math.round(Number(total))} GB`) : null),
        extra);
      const top = u.workspaces.slice(0, 5);
      fill(body,
        device("Tower", u.free_gb, u.total_gb, [
          h("p", { class: "muted small" }, `Workspaces ${mb(u.workspaces_mb)} (${u.workspaces.length}) · quota ${u.quota_mb} MB each`),
          top.length ? h("ul", { class: "small" }, top.map((w) => h("li", {}, h("a", { href: `#/s/${w.session}/info` }, w.session), ` ${mb(w.mb)}`))) : null,
          h("p", { class: "muted small" }, `${u.containers.length} sandbox container${u.containers.length === 1 ? "" : "s"}`),
          backupLine(u.backup),
        ]),
        (u.runners || []).map((r) => device(TARGET_LABEL[r.name] || r.name,
          r.online ? r.info.free_gb : "—",
          r.online && r.info.total_gb != null ? r.info.total_gb : null,
          h("p", { class: "muted small" },
            r.online ? `online · runner ${r.info.version} · macOS ${r.info.macos}`
              : `offline${r.last_seen_seconds !== null ? ` (last seen ${Math.round(r.last_seen_seconds / 60)} min ago)` : " (not connected since the daemon started)"}`))));
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  return h("div", { class: "card" }, body,
    h("p", { class: "muted small" }, "Clean up now removes stopped sandbox containers, expired session workspaces, and leftover workspace folders."),
    h("button", {
      class: "btn",
      onclick: async (ev) => {
        if (!confirm("Remove stopped sandbox containers, expired session workspaces, and leftover workspace folders?")) return;
        ev.target.disabled = true;
        try {
          const r = await api("/maintenance/cleanup", { method: "POST" });
          toast(`Removed ${r.containers_removed.length} containers, ${r.workspaces_removed.length + r.orphans_removed.length} workspaces`);
          load();
        } catch (e) { toast(e.message); }
        ev.target.disabled = false;
      },
    }, "Clean up now"));
}

// ---------- model warm-up ----------
// Loading the model takes about a minute after it has slept, so start as soon as the app is opened.
let lastWarm = 0;
async function warmModel(force = false) {
  if (!force && Date.now() - lastWarm < 60_000) return;
  lastWarm = Date.now();
  try { await api("/models/warm", { method: "POST" }); } catch (_) { /* offline */ }
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") warmModel(); });

// ---------- boot ----------
if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
warmModel();
loadProfileIcon();
route();
