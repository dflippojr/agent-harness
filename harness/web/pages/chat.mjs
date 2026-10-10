// Chat page (#258): the welcome state, the durable non-agent conversation with its model picker and composer, and the
// snippet runner (#85). The shell (DOM builder, router hooks, event stream) is injected by app.js so this module imports
// under plain Node and never reaches into another page.
import { SNIPPET_LANGUAGES, snippetLanguage } from "../lib/snippets.mjs";
import { md } from "../lib/markdown.mjs";
import { staleNote } from "../lib/widgets.mjs";
import * as sheets from "../lib/sheet.mjs";

export function mountChat({ $app, h, fill, append, api, setHeader, toast, go, validId, canChat, onLeave, openStream, ownerSurface, badge,
  TERMINAL, agentHarnessWeb, browser, confirmSheet = sheets.confirmSheet, promptSheet = sheets.promptSheet }) {
// Browser globals come in through `browser` (globalThis in the app, a stub under Node) so importing this module touches no DOM.
const { window, document, localStorage } = browser;

// ---------- chat snippets (#85) ----------
const SNIPPET_STATUS = {
  completed: "Completed", failed: "Failed", compile_failed: "Compile failed", timeout: "Timed out",
  cancelled: "Cancelled", limit_exceeded: "Limit reached", error: "Sandbox error", interrupted: "Interrupted",
};
const SNIPPET_REASON = {
  timeout: "time limit (30 s)", output_limit: "output limit (1 MiB)", memory_limit: "memory limit (1 GiB)",
  pids_limit: "process limit (64)", temp_storage_limit: "temporary storage limit (128 MiB)",
  cancelled: "cancelled", daemon_restart: "the server restarted",
};

function snippetRunRow(lang, getSource, run) {
  const label = SNIPPET_LANGUAGES[lang].label;
  return h("div", { class: "row snippet-run" }, h("button", {
    class: "btn small", type: "button", title: `Run this code as ${label} in an isolated sandbox`,
    onclick: () => run(lang, getSource(), "block"),
  }, `▶ Run ${label}`));
}

// A user message stays plain text; only fenced blocks in a supported language become code blocks with Run.
function userMessageParts(content, run) {
  const text = String(content || "");
  const parts = [];
  let last = 0;
  for (const m of text.matchAll(/```([\w+#-]*)[^\S\n]*\n([\s\S]*?)```/g)) {
    const lang = snippetLanguage(m[1]);
    if (!lang) continue;
    const code = m[2].replace(/\n$/, "");
    parts.push(text.slice(last, m.index), h("pre", { "data-snippet-lang": lang }, h("code", {}, code)),
      snippetRunRow(lang, () => code, run));
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return parts.filter((p) => p !== "");
}

// Adds Run buttons under the marked code blocks md() produced. The source is the block's text, never its HTML.
function addRunControls(root, run) {
  for (const pre of root.querySelectorAll("pre[data-snippet-lang]")) {
    const lang = pre.dataset.snippetLang;
    if (SNIPPET_LANGUAGES[lang]) pre.after(snippetRunRow(lang, () => pre.textContent, run));
  }
}

// Every value here is source or program output: untrusted, so it only ever becomes text nodes.
const snippetStopped = (code) => code === null || code === undefined;
const snippetBlock = (label, text, cls) => [h("p", { class: "snippet-label" }, label), h("pre", { class: `snippet-out ${cls}` }, text)];

function snippetCompileParts(compile) {
  const code = compile.exit_code;
  let title = `Compile failed (exit ${code})`;
  if (code === 0) title = "Compiled";
  else if (snippetStopped(code)) title = "Compile stopped";
  if (compile.output) return snippetBlock(`${title} · compiler diagnostics`, compile.output, "compile");
  return [h("p", { class: "snippet-label" }, title)];
}

function snippetRunParts(run) {
  const code = run.exit_code;
  const parts = [h("p", { class: "snippet-label" }, snippetStopped(code) ? "Program stopped" : `Exit status ${code}`)];
  if (run.stdout) parts.push(...snippetBlock("stdout", run.stdout, "stdout"));
  if (run.stderr) parts.push(...snippetBlock("stderr", run.stderr, "stderr"));
  if (!run.stdout && !run.stderr) parts.push(h("p", { class: "muted small" }, "No output."));
  return parts;
}

function snippetResultParts(r) {
  const tc = r.toolchain || {};
  const meta = [tc.version, tc.image, r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)} s` : ""];
  const parts = [h("p", { class: "muted small" }, meta.filter(Boolean).join(" · "))];
  if (r.error) parts.push(h("p", { class: "bad small" }, r.error));
  const reasons = (r.reasons || []).map((x) => SNIPPET_REASON[x] || x);
  if (reasons.length) parts.push(h("p", { class: "bad small" }, `Reason: ${reasons.join("; ")}`));
  if (r.truncated) parts.push(h("p", { class: "bad small" }, "Output was truncated at the 1 MiB limit."));
  if (r.compile) parts.push(...snippetCompileParts(r.compile));
  if (r.run) parts.push(...snippetRunParts(r.run));
  return parts;
}

function snippetCard(started, onCancel) {
  const label = SNIPPET_LANGUAGES[started.language]?.label || started.language || "Snippet";
  const status = h("span", { class: "badge running" }, "Running…");
  const cancel = h("button", { class: "btn small bad", type: "button", onclick: () => onCancel(started.id) }, "Cancel");
  const body = h("div", { class: "snippet-body" });
  const el = h("div", { class: "snippet", "data-run": started.id },
    h("div", { class: "row snippet-head" }, h("strong", {}, `${label} run`), status, cancel),
    started.source ? h("details", {}, h("summary", {}, "Source"), h("pre", {}, h("code", {}, started.source))) : null,
    body);
  const finish = (r) => {
    cancel.remove();
    status.className = `badge ${r.status === "completed" ? "done" : "failed"}`;
    status.textContent = SNIPPET_STATUS[r.status] || r.status || "Finished";
    fill(body, snippetResultParts(r));
  };
  return { el, finish };
}

// The manual editor: the owner picks the language; there is no default and no guessing from the code.
function snippetEditor(run) {
  const select = h("select", { class: "snippet-language", "aria-label": "Snippet language" },
    h("option", { value: "" }, "Language…"),
    Object.entries(SNIPPET_LANGUAGES).map(([id, spec]) => h("option", { value: id }, spec.label)));
  const code = h("textarea", { class: "snippet-code", rows: 8, spellcheck: "false", placeholder: "Code to run…",
    "aria-label": "Code to run" });
  const runBtn = h("button", { class: "btn primary small", type: "button", disabled: true }, "▶ Run");
  const sync = () => { runBtn.disabled = !select.value || !code.value.trim(); };
  select.addEventListener("change", sync);
  code.addEventListener("input", sync);
  runBtn.addEventListener("click", async () => {
    if (runBtn.disabled) return;
    runBtn.disabled = true;
    await run(select.value, code.value, "editor");
    sync();
  });
  const el = h("div", { class: "snippet-editor", hidden: true },
    h("div", { class: "row" }, select, runBtn),
    code,
    h("p", { class: "muted small" }, "Runs once in a fresh sandbox: standard library only, no network, 30 s, 1 GiB memory. Output is shown as untrusted text."));
  return { el, select, code, runBtn };
}

// ---------- chat ----------
const CHAT_CHOICE_KEY = "harness.chatChoice";
const CHAT_STARTERS = ["Explain a concept simply", "Review some code I paste", "Summarize a topic with sources"];

function readChatChoice() {
  try { return JSON.parse(localStorage.getItem(CHAT_CHOICE_KEY) || "null") || {}; } catch (_) { return {}; }
}

// Keeps the fixed composer above the on-screen keyboard (iOS does not resize the layout viewport).
function trackKeyboard(composer) {
  const vv = window.visualViewport;
  if (!vv) return () => {};
  // With the keyboard closed the stylesheet places the composer (above the tab bar, #506); open, it sits on the keyboard.
  const update = () => {
    const keyboard = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    composer.style.bottom = keyboard ? `${keyboard}px` : "";
  };
  vv.addEventListener("resize", update);
  vv.addEventListener("scroll", update);
  update();
  return () => { vv.removeEventListener("resize", update); vv.removeEventListener("scroll", update); };
}

function chatComposer(options, session) {
  const fixed = Boolean(session);
  const choice = readChatChoice();
  const backends = options.backends || [];
  const modelSelect = h("select", { class: "chat-model", "aria-label": "Model", disabled: fixed });
  const effortSelect = h("select", { class: "chat-effort", "aria-label": "Reasoning effort", disabled: fixed });
  const input = h("textarea", { placeholder: "Message…", rows: 1, "aria-label": "Message" });
  const send = h("button", { class: "btn primary", type: "button" }, "Send");
  const cancel = h("button", { class: "btn bad", type: "button", hidden: true }, "Cancel");
  const notice = h("p", { class: "note chat-notice", hidden: true });

  const keyOf = (backend, model) => `${backend}|${model}`;
  if (fixed) {
    modelSelect.append(h("option", {}, `${session.backend || "local"} · ${session.model}`));
    effortSelect.append(h("option", {}, session.effort || "default"));
    effortSelect.hidden = !session.effort;
    modelSelect.title = effortSelect.title = "Start a new chat to change the model or effort.";
  } else {
    for (const b of backends) {
      modelSelect.append(h("optgroup", { label: b.name === "local" ? "Local" : b.name },
        b.models.map((m) => h("option", { value: keyOf(b.name, m) }, m))));
    }
    const wanted = keyOf(choice.backend, choice.model);
    const fallback = backends.find((b) => b.name === options.default_backend) || backends[0];
    if ([...modelSelect.options].some((o) => o.value === wanted)) modelSelect.value = wanted;
    else modelSelect.value = fallback ? keyOf(fallback.name, fallback.model || fallback.models[0]) : "";
  }
  const selected = () => {
    const [backend, ...rest] = (modelSelect.value || "").split("|");
    return { backend, model: rest.join("|"), spec: backends.find((b) => b.name === backend) };
  };
  const syncEffort = () => {
    if (fixed) return;
    const { spec } = selected();
    const efforts = spec?.efforts || [];
    fill(effortSelect, efforts.map((e) => h("option", { value: e }, e)));
    effortSelect.hidden = !efforts.length;
    const pick = choice.effort && efforts.includes(choice.effort) ? choice.effort : spec?.effort;
    if (pick && efforts.includes(pick)) effortSelect.value = pick;
    notice.textContent = spec?.billing_warning || "";
    notice.hidden = !spec?.billing_warning;
  };
  modelSelect.addEventListener("change", syncEffort);
  syncEffort();

  input.addEventListener("input", () => { input.style.height = "44px"; input.style.height = `${Math.min(160, input.scrollHeight)}px`; });
  const el = h("div", { class: "composer chat-composer" }, h("div", { class: "inner", style: "flex-direction:column;align-items:stretch" },
    notice,
    h("div", { class: "row chat-pickers" }, modelSelect, effortSelect),
    h("div", { class: "row", style: "flex-wrap:nowrap;align-items:flex-end" }, input, send, cancel)));
  const remember = () => {
    const { backend, model } = selected();
    try { localStorage.setItem(CHAT_CHOICE_KEY, JSON.stringify({ backend, model, effort: effortSelect.value })); } catch (_) { /* private mode */ }
  };
  return { el, input, send, cancel, effortSelect, selected, remember };
}

// Recent chats on the Chat home (#506; they were in the navigation drawer). Shows the last known list at once, then
// revalidates in the background (#152). A failed revalidation keeps what is shown and says it may be stale (#510); with
// nothing shown yet there is nothing stale, and the page's own offline state speaks for the server.
let recentChatsCache = null;
let recentChatsAt = 0;
function recentChats() {
  const label = h("p", { class: "section-label" }, "Recent chats");
  const list = h("div", { class: "card settings-list recent-chats" });
  const section = h("section", { class: "recent-chats-section", "aria-label": "Recent chats", hidden: true }, label, list);
  const render = (chats) => {
    section.hidden = !chats.length;
    fill(list, chats.map((c) => h("a", { href: `#/chat/${c.id}`, title: c.title }, c.title)));
  };
  const load = () => api("/chats?limit=30").then((chats) => {
    recentChatsCache = chats;
    recentChatsAt = Date.now();
    stale.ok();
    render(chats);
  }).catch((e) => {
    console.error("recent chats refresh failed", e);
    if (!recentChatsCache?.length) return;
    stale.failed(e);
  });
  const stale = staleNote({ make: h, place: (el) => label.after(el), onRetry: load, updatedAt: recentChatsAt });
  if (recentChatsCache) render(recentChatsCache);
  onLeave(stale.stop);
  void load();
  return section;
}

async function viewChat(id) {
  if (!canChat()) { go("#/agents", true); return; }
  if (id && !validId(id)) { go("#/chat", true); return; }

  // Paint the page shell before the data fetch below so the route feels instant; the
  // composer and header title are filled in once /chats/<id> or /chats/options resolves (#152).
  setHeader("chat", id ? "" : "Chat");
  let active = true;
  document.body.classList.add("chat-page");
  onLeave(() => { active = false; document.body.classList.remove("chat-page"); });

  let ui = null;
  const feed = h("div", { class: "chat-feed", "aria-live": "polite" });
  const welcome = id ? null : h("div", { class: "chat-welcome" },
    h("div", { class: "chat-welcome-content" },
      h("div", { class: "chat-welcome-mark", "aria-hidden": "true" }, "💬"),
      h("h2", {}, "How can I help?"),
      h("p", { class: "muted" }, "Ask a question or paste code to review. To change files or run work, use Agents."),
      h("div", { class: "chat-starters" }, CHAT_STARTERS.map((text) => h("button", {
        class: "btn small", type: "button",
        onclick: () => {
          if (!ui) return;
          ui.input.value = text;
          ui.input.focus();
        },
      }, text)))), recentChats());
  const wrap = h("div", { class: "chat-wrap" }, welcome, feed);
  append($app, wrap);

  let session = null;
  let options = { backends: [] };
  if (id) session = await api(`/chats/${id}`);
  else options = await api("/chats/options");
  if (!active) return;
  if (session) id = session.id;
  setHeader("chat", session ? session.title : "Chat");
  ui = chatComposer(options, session);
  document.body.append(ui.el);
  const stopKeyboard = trackKeyboard(ui.el);
  onLeave(() => { ui.el.remove(); stopKeyboard(); });

  if (!session && !options.backends.length) {
    feed.append(h("p", { class: "note bad" }, "No model backend is available right now. Check Profile → Backends."));
    ui.send.disabled = true;
  }

  const scrollDown = () => window.scrollTo({ top: document.body.scrollHeight });
  const setBusy = (busy) => { ui.send.hidden = busy; ui.cancel.hidden = !busy; };
  const send = async () => {
    const text = ui.input.value.trim();
    if (!text) return;
    ui.send.disabled = true;
    try {
      if (!session) {
        const { backend, model, spec } = ui.selected();
        ui.remember();
        const created = await api("/chats", { method: "POST", body: {
          prompt: text, backend, model, effort: spec?.efforts?.length ? ui.effortSelect.value : "" } });
        ui.input.value = "";
        go(`#/chat/${created.id}`, true);
        return;
      }
      await api(`/chats/${id}/messages`, { method: "POST", body: { content: text } });
      ui.input.value = "";
      ui.input.style.height = "44px";
      setBusy(true);
    } catch (e) { toast(e.message); }
    ui.send.disabled = false;
  };
  ui.send.addEventListener("click", send);
  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void send(); }
  });
  ui.cancel.addEventListener("click", async () => {
    try { await api(`/chats/${id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
  });
  if (!session) { ui.input.focus(); return; }

  const runSnippet = async (language, source, origin) => {
    try { await api(`/chats/${id}/snippets`, { method: "POST", body: { language, source, origin } }); }
    catch (e) { toast(e.message); }
  };
  const cancelSnippet = async (runId) => {
    try { await api(`/chats/${id}/snippets/${encodeURIComponent(runId)}/cancel`, { method: "POST" }); }
    catch (e) { toast(e.message); }
  };
  const editor = snippetEditor(runSnippet);
  const editorToggle = h("button", { class: "btn small", type: "button", "aria-expanded": "false", onclick: () => {
    editor.el.hidden = !editor.el.hidden;
    editorToggle.setAttribute("aria-expanded", String(!editor.el.hidden));
    if (!editor.el.hidden) editor.select.focus();
  } }, "Run code");
  const effortSuffix = session.effort ? ` · ${session.effort}` : "";
  wrap.prepend(h("div", { class: "row small chat-tools" },
    h("span", { class: "muted" }, `${session.backend || "local"} · ${session.model}${effortSuffix}`),
    editorToggle,
    h("button", { class: "btn small", type: "button", onclick: async () => {
      const title = await promptSheet({ title: "Rename chat", label: "Title", value: session.title, confirmLabel: "Rename",
        validate: sheets.required("a title") });
      if (!title?.trim()) return;
      try { session = await api(`/chats/${id}`, { method: "PATCH", body: { title } }); if (active) setHeader("chat", session.title); }
      catch (e) { toast(e.message); }
    } }, "Rename"),
    h("button", { class: "btn small bad", type: "button", onclick: async () => {
      if (!(await confirmSheet({ title: "Delete this chat?", confirmLabel: "Delete chat", destructive: true }))) return;
      try { await api(`/chats/${id}`, { method: "DELETE" }); go("#/chat", true); } catch (e) { toast(e.message); }
    } }, "Delete")), editor.el);

  let lastSeq = 0;
  let live = null;
  let sawContent = false;
  const add = (el) => { feed.append(el); scrollDown(); return el; };
  const assistantMessage = (content) => {
    const el = add(h("div", { class: "msg assistant final", html: md(content, []) }));
    addRunControls(el, runSnippet);
    return el;
  };
  const snippetCards = {};
  const handlers = {
    user_message: (e) => add(h("div", { class: "msg user" }, userMessageParts(e.data.content, runSnippet))),
    snippet_started: (e) => {
      const card = snippetCard(e.data, cancelSnippet);
      snippetCards[e.data.id] = card;
      add(card.el);
    },
    snippet_result: (e) => {
      let card = snippetCards[e.data.id];
      if (!card) {
        card = snippetCards[e.data.id] = snippetCard({ id: e.data.id, language: e.data.language }, cancelSnippet);
        add(card.el);
      }
      card.finish(e.data);
      scrollDown();
    },
    delta: (e) => {
      if (e.data.kind === "reasoning") return;
      if (!live) live = add(h("div", { class: "msg assistant" }));
      live.textContent += e.data.text;
      scrollDown();
    },
    assistant: (e) => {
      live?.remove();
      live = null;
      const d = e.data;
      if (d.content?.trim()) sawContent = true;
      if (d.content?.trim()) assistantMessage(d.content);
      for (const call of d.tool_calls || []) {
        add(h("p", { class: "note" }, call.function?.name === "web_fetch" ? "Reading a web page…" : "Searching the web…"));
      }
    },
    billing_warning: (e) => add(h("p", { class: "note bad" }, e.data.message)),
    limit_waiting: (e) => add(h("p", { class: "note" }, `Rate limit reached; waiting until ${new Date(e.data.resets_at * 1000).toLocaleString()}`)),
    backend_fallback: (e) => add(h("p", { class: "note bad" }, `Rate limit reached; continuing ${e.data.backend} with an API key`)),
    status: (e) => {
      const status = e.data.status;
      session = { ...session, status };
      setBusy(!TERMINAL.has(status));
      if (!TERMINAL.has(status)) return;
      live?.remove();
      live = null;
      const answer = (e.data.answer || "").trim();
      if (answer && !sawContent) assistantMessage(answer);
      if (status !== "done") {
        add(h("p", { class: `status-line${status === "failed" ? " bad" : ""}` }, badge(status),
          e.data.stop_reason && !["final_message", "finished"].includes(e.data.stop_reason) ? ` ${e.data.stop_reason}` : ""));
      }
    },
  };
  const tracked = {};
  for (const type of Object.keys(handlers)) {
    tracked[type] = (e) => {
      if (e.seq !== null && e.seq !== undefined) {
        if (e.seq <= lastSeq) return;
        lastSeq = e.seq;
      }
      handlers[type](e);
    };
  }
  setBusy(!TERMINAL.has(session.status));
  onLeave(openStream(
    () => agentHarnessWeb.url(`/chats/${encodeURIComponent(id)}/events?after=${lastSeq}`, ownerSurface()),
    tracked,
    { authorized: !!agentHarnessWeb.token },
  ));
}

return { viewChat };
}
