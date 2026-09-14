// Agent Harness web app: plain ES module, no build step. Hash routes:
//   #/                       session list
//   #/new                    new task (templates)
//   #/s/<id>                 session transcript (live)
//   #/s/<id>/approval/<aid>  same, focused on one approval (notification deep link)
//   #/s/<id>/changes         diff viewer
//   #/s/<id>/info            session details
//   #/settings               identity, notifications, install help

const $app = document.getElementById("app");
const $title = document.getElementById("title");
const $back = document.getElementById("back");
const $conn = document.getElementById("conn");
const TERMINAL = new Set(["done", "failed", "cancelled"]);
const STATUS_LABEL = {
  queued: "queued", running: "running", waiting_approval: "needs approval",
  done: "done", failed: "failed", cancelled: "cancelled",
};
const SESSION_EVENT_TYPES = [
  "session_created", "user_message", "status", "assistant", "delta", "tool_call", "tool_result",
  "approval_requested", "approval_decided", "compaction", "compacting", "error", "llm_retry", "resumed",
  "run_finished", "queue", "notes", "model_waking", "model_ready", "workspace_ready", "branch_saved", "review",
];
const REVIEW_LABEL = { merged: "merged", pushed: "pushed", discarded: "discarded" };

let cleanup = [];
const onLeave = (fn) => cleanup.push(fn);

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

// Small, safe Markdown subset: everything is escaped first, then a few constructs are re-enabled.
function md(src) {
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
  return out.join("\n").replace(/\u0000(\d+)\u0000/g, (_, n) => blocks[Number(n)]);
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
async function route() {
  cleanup.forEach((fn) => { try { fn(); } catch (_) { /* ignore */ } });
  cleanup = [];
  $app.replaceChildren();
  document.querySelector(".composer")?.remove();
  document.querySelector(".fab")?.remove();
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  $back.hidden = parts.length === 0;
  try {
    if (parts.length === 0) await viewList();
    else if (parts[0] === "new") await viewNew();
    else if (parts[0] === "settings") await viewSettings();
    else if (parts[0] === "s" && parts[1]) await viewSession(parts[1], parts[2] || "transcript", parts[3]);
    else location.hash = "#/";
  } catch (e) {
    $app.append(h("p", { class: "note bad" }, e.message));
  }
}
$back.addEventListener("click", () => {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  location.hash = parts[0] === "s" && parts.length > 2 ? `#/s/${parts[1]}` : "#/";
});
window.addEventListener("hashchange", route);

// ---------- session list ----------
async function viewList() {
  $title.textContent = "Agents";
  const list = h("div");
  const queueNote = h("p", { class: "note" });
  $app.append(queueNote, list);
  document.body.append(h("a", { class: "btn primary fab", href: "#/new" }, "+ New task"));

  const render = async () => {
    const [sessions, queue] = await Promise.all([api("/sessions"), api("/queue")]);
    const waiting = queue.filter((q) => q.position > 0).length;
    queueNote.textContent = waiting ? `${waiting} waiting for the GPU` : "";
    if (!sessions.length) {
      list.replaceChildren(h("p", { class: "empty" }, "No sessions yet. Start one with “New task”."));
      return;
    }
    list.replaceChildren(...sessions.map((s) => {
      const pending = (s.pending_approvals || []).length;
      return h("a", { class: "card", href: `#/s/${s.id}${pending ? `/approval/${s.pending_approvals[0].id}` : ""}` },
        h("h3", {}, s.title),
        h("div", { class: "meta" },
          badge(s.status),
          pending ? h("span", { class: "badge waiting_approval" }, `${pending} approval${pending > 1 ? "s" : ""}`) : null,
          s.queue_position > 0 ? h("span", {}, `#${s.queue_position} in queue`) : null,
          s.review ? h("span", { class: `badge ${s.review === "discarded" ? "cancelled" : "done"}` }, REVIEW_LABEL[s.review] || s.review) : null,
          h("span", {}, s.project), h("span", {}, ago(s.updated_at))),
        s.answer_preview ? h("div", { class: "preview" }, s.answer_preview) : null);
    }));
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
  $title.textContent = "New task";
  const [projects, models, templates] = await Promise.all([api("/projects"), api("/models"), api("/templates")]);
  const tplSelect = h("select", {},
    h("option", { value: "" }, templates.length ? "— none —" : "No templates yet"),
    templates.map((t) => h("option", { value: t.id }, t.name)));
  const project = h("select", {}, projects.map((p) => h("option", { value: p.name }, p.description ? `${p.name} — ${p.description}` : p.name)));
  const model = h("select", {}, models.map((m) => h("option", { value: m.name, selected: m.default }, m.name)));
  const prompt = h("textarea", { placeholder: "e.g. Clone local:invoice-tools, fix the failing test, and report back." });
  const title = h("input", { type: "text", placeholder: "Optional; defaults to the first line" });
  const modelState = h("div", { class: "muted small", style: "margin-top:6px" });
  const MODEL_STATE = {
    ready: "✓ Model loaded",
    sleeping: "Model is asleep; loading it now (about a minute)",
    waking: "Model is loading (about a minute); you can start the task anyway",
    unreachable: "Model server isn't answering",
  };
  const pollModel = async () => {
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
    if (t.model) model.value = t.model;
    prompt.value = t.prompt;
  });

  const start = h("button", { class: "btn primary", type: "submit" }, "Start");
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      if (!prompt.value.trim()) return toast("Write a prompt first");
      start.disabled = true;
      try {
        const s = await api("/sessions", { method: "POST", body: { prompt: prompt.value, project: project.value, model: model.value, title: title.value || null } });
        try { localStorage.removeItem(draftKey); } catch (_) { /* ignore */ }
        location.hash = `#/s/${s.id}`;
      } catch (err) {
        toast(err.message);
        start.disabled = false;
      }
    },
  },
  templates.length ? [h("label", {}, "Template"), tplSelect] : null,
  h("label", {}, "Prompt"), prompt,
  h("label", {}, "Project"), project,
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
          await api("/templates", { method: "POST", body: { name, project: project.value, model: model.value, prompt: prompt.value } });
          toast("Template saved");
          warmModel();
route();
        } catch (err) { toast(err.message); }
      },
    }, "Save as template"),
    h("span", { class: "spacer" }), start));
  $app.append(form);

  if (templates.length) {
    $app.append(h("details", { style: "margin-top:28px" }, h("summary", { class: "muted" }, "Manage templates"),
      templates.map((t) => h("div", { class: "card" },
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

// ---------- session ----------
async function viewSession(sid, tab, focusApproval) {
  let session = await api(`/sessions/${sid}`);
  sid = session.id;
  $title.textContent = session.title;

  const tabs = h("div", { class: "tabs" },
    ["transcript", "changes", "info"].map((name) => h("button", {
      class: (tab === name || (tab === "approval" && name === "transcript")) ? "on" : "",
      onclick: () => { location.hash = name === "transcript" ? `#/s/${sid}` : `#/s/${sid}/${name}`; },
    }, name[0].toUpperCase() + name.slice(1))));
  const head = h("div", { class: "row small" });
  $app.append(head, tabs);

  const renderHead = () => {
    fill(head, badge(session.status),
      session.queue_position > 0 ? h("span", { class: "muted" }, `#${session.queue_position} in GPU queue`) : null,
      h("span", { class: "muted" }, `${session.project} · ${session.model}`));
  };
  renderHead();

  if (tab === "changes") return viewChanges(session);
  if (tab === "info") return viewInfo(session);

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
      h("a", { class: "btn small", href: `#/s/${sid}/changes` }, "Changes"));
  };
  renderActions();

  // transcript rendering
  const nearBottom = () => window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
  const add = (el) => {
    const stick = nearBottom();
    feed.append(el);
    if (stick) window.scrollTo(0, document.body.scrollHeight);
    return el;
  };
  const calls = new Map();   // tool call id -> {el, state, body}
  const approvals = new Map();
  let live = null;           // streaming bubble
  let lastSeq = 0;
  let lastContent = "";
  let wakingNote = null;

  const liveBubble = () => {
    if (live) return live;
    const thinkText = h("div", { class: "text" });
    const think = h("details", { class: "thinking" }, h("summary", { class: "dots" }, "Thinking"), thinkText);
    const content = h("div", { class: "msg assistant", style: "white-space:pre-wrap", hidden: true });
    const el = add(h("div", { class: "ev" }, think, content));
    live = { el, think, thinkText, content, chars: 0 };
    return live;
  };

  const toolEl = (call) => {
    const fn = call.function || {};
    let args = {};
    try { args = JSON.parse(fn.arguments || "{}"); } catch (_) { args = { raw: fn.arguments }; }
    const summaryText = fn.name === "run_shell" ? args.command
      : fn.name === "git_clone" ? args.url
        : fn.name === "prometheus_query" ? args.query
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
    const card = h("div", { class: "approval", id: `approval-${a.id}` },
      h("h4", {}, `Approval needed: ${a.reason || a.tool}`),
      h("pre", {}, a.detail || what),
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
    delta: (e) => {
      const b = liveBubble();
      if (e.data.kind === "reasoning") {
        b.thinkText.textContent += e.data.text;
        b.chars += e.data.text.length;
      } else {
        b.content.hidden = false;
        b.content.textContent += e.data.text;
        b.think.querySelector("summary").classList.remove("dots");
        b.think.querySelector("summary").textContent = "Thought";
      }
      if (nearBottom()) window.scrollTo(0, document.body.scrollHeight);
    },
    assistant: (e) => {
      const d = e.data;
      live?.el.remove();
      live = null;
      const wrap = h("div", { class: "ev" });
      if (d.reasoning) {
        wrap.append(h("details", { class: "thinking" }, h("summary", {}, `Thought (${d.completion_tokens} tokens · ${d.gen_tps} tok/s)`),
          h("div", { class: "text" }, d.reasoning)));
      }
      if (d.content && d.content.trim()) {
        lastContent = d.content.trim();
        wrap.append(h("div", { class: `msg assistant${d.tool_calls.length ? "" : " final"}`, html: md(d.content) }));
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
      const stick = nearBottom();
      if (c) c.slot.append(card); else feed.append(h("div", { class: "ev" }, card));
      if (stick && focusApproval !== e.data.id) window.scrollTo(0, document.body.scrollHeight);
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
      const c = calls.get(e.data.id);
      const out = h("pre", {}, e.data.output);
      if (!c) { add(h("details", { class: "tool ev" }, h("summary", {}, e.data.name), out)); return; }
      c.state.textContent = `${e.data.ok ? "ok" : "error"} · ${e.data.seconds}s`;
      c.state.className = `state ${e.data.ok ? "ok" : "err"}`;
      c.body.append(out);
    },
    compacting: () => add(h("p", { class: "note" }, "Condensing older context…")),
    compaction: (e) => add(h("p", { class: "note" }, `Context condensed (${e.data.tier}): ~${e.data.tokens_before} → ~${e.data.tokens_after} tokens`)),
    notes: (e) => add(h("details", { class: "thinking ev" }, h("summary", {}, "Agent saved notes"), h("div", { class: "text" }, e.data.notes))),
    error: (e) => add(h("p", { class: "note bad" }, e.data.message)),
    llm_retry: (e) => add(h("p", { class: "note" }, `Model call retried (${e.data.attempt})`)),
    resumed: () => add(h("p", { class: "note" }, "Daemon restarted — session resumed")),
    workspace_ready: (e) => add(h("p", { class: "note" }, `Checked out on branch ${e.data.branch} (from ${e.data.base_branch})`)),
    branch_saved: (e) => add(h("p", { class: "note" }, h("a", { href: `#/s/${sid}/changes` },
      `Branch saved: ${e.data.commits.length} commit${e.data.commits.length === 1 ? "" : "s"} to review${e.data.auto_commit ? " (leftover edits committed)" : ""}`))),
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
    queue: (e) => { session.queue_position = e.data.position; renderHead(); },
    status: (e) => {
      session.status = e.data.status;
      if (e.data.status !== "queued") session.queue_position = null;
      renderHead();
      renderActions();
      if (TERMINAL.has(e.data.status)) {
        live?.el.remove();
        live = null;
        const answer = (e.data.answer || "").trim();
        if (answer && answer !== lastContent) add(h("div", { class: "ev msg assistant final", html: md(answer) }));
        add(h("p", { class: "status-line" }, badge(e.data.status),
          e.data.stop_reason && !["final_message", "finished"].includes(e.data.stop_reason) ? ` ${e.data.stop_reason}` : ""));
      }
    },
  };
  const tracked = {};
  for (const type of SESSION_EVENT_TYPES) {
    tracked[type] = (e) => {
      if (e.seq !== null && e.seq !== undefined) {
        if (e.seq <= lastSeq) return;
        lastSeq = e.seq;
      }
      handlers[type]?.(e);
    };
  }
  onLeave(openStream(() => `/sessions/${sid}/events?after=${lastSeq}`, tracked));
  onLeave(() => composer.remove());
}

function reviewCard(s) {
  if (!s.repo_kind || !s.branch) return null;
  const busy = !TERMINAL.has(s.status);
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
    }
  };
  const base = s.base_branch || "base";
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
    ["Session", s.id], ["Status", `${s.status}${s.stop_reason ? ` (${s.stop_reason})` : ""}`],
    ["Project", s.project], ["Target", s.target], ["Model", s.model],
    ["Created", new Date(s.created_at * 1000).toLocaleString()], ["Updated", new Date(s.updated_at * 1000).toLocaleString()],
    ["Model turns", t.turns || 0], ["Prompt tokens", t.prompt_tokens || 0], ["Completion tokens", t.completion_tokens || 0],
    ["Workspace", s.workspace_removed ? `${s.workspace} (removed)` : s.workspace],
  ];
  if (s.branch) rows.push(["Branch", `${s.branch}${s.base_branch ? ` from ${s.base_branch}` : ""}`], ["Review", s.review || "pending"]);
  $app.append(h("div", { class: "card" }, rows.map(([k, v]) => h("div", { class: "row", style: "justify-content:space-between;padding:4px 0" },
    h("span", { class: "muted" }, k), h("span", { style: "overflow-wrap:anywhere;text-align:right" }, String(v))))),
  h("a", { class: "btn", href: `/sessions/${s.id}/transcript`, target: "_blank" }, "Open Markdown transcript"));
}

// ---------- settings ----------
async function viewSettings() {
  $title.textContent = "Settings";
  const me = await api("/me");
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const ntfyUrl = me.public_url ? `${me.public_url}:8443` : "(set public_url)";
  $app.append(
    h("div", { class: "card" }, h("h3", {}, "Signed in"),
      h("p", {}, me.login ? `${me.name || ""} ${me.login}` : "Local access (no Tailscale identity)")),
    h("div", { class: "card" }, h("h3", {}, "Notifications"),
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
      }, "Send test notification") : null),
    diskCard(),
    h("div", { class: "card" }, h("h3", {}, "Install"),
      h("p", {}, standalone ? "Running as an installed app." : "In Safari: Share → Add to Home Screen. The app then opens full screen.")));
}

function diskCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Measuring…"));
  const load = async () => {
    try {
      const u = await api("/maintenance");
      const top = u.workspaces.slice(0, 5);
      const mb = (n) => (n < 1 ? "<1 MB" : `${n} MB`);
      fill(body,
        h("p", {}, `${u.free_gb} GB free of ${u.total_gb} GB · workspaces ${mb(u.workspaces_mb)} (${u.workspaces.length}) · quota ${u.quota_mb} MB each`),
        top.length ? h("ul", { class: "small" }, top.map((w) => h("li", {}, h("a", { href: `#/s/${w.session}/info` }, w.session), ` ${mb(w.mb)}`))) : null,
        h("p", { class: "muted small" }, `${u.containers.length} sandbox container${u.containers.length === 1 ? "" : "s"}`));
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  return h("div", { class: "card" }, h("h3", {}, "Disk"), body,
    h("button", {
      class: "btn",
      onclick: async (ev) => {
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
route();
