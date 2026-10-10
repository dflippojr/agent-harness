// Session page (#258): the live transcript with composer, approvals and checkpoints, plus the Changes tab (review card,
// secret scan, inline review comments). The shell (DOM builder, router hooks, event stream) is injected by app.js and the
// session chrome helpers come from lib/session-ui.mjs, so this module imports under plain Node and never reaches into another page.
import { TARGET_LABEL } from "../lib/targets.mjs";
import { fmtElapsed, fmtTokens, readFraction, readingText, fmtSpan, pluralize } from "../lib/format.mjs";
import { toolSummaryText, approvalWhat, toolKind, resultState } from "../lib/tools.mjs";
import { createToolRows } from "../lib/tool-row.mjs";
import { approvalDiffClass, diffLineClass, splitDiff } from "../lib/diff.mjs";
import { md } from "../lib/markdown.mjs";
import { withTaint } from "../lib/taint.mjs";
import { mountSessionUi, sessionMenuItems, pageMetrics as measurePage, scrollPage as scrollPageOf } from "../lib/session-ui.mjs";
import { sinceText } from "../lib/widgets.mjs";
import * as sheets from "../lib/sheet.mjs";

const SESSION_EVENT_TYPES = [
  "session_created", "user_message", "status", "assistant", "delta", "tool_call", "tool_result",
  "approval_requested", "approval_decided", "approval_auto_approved", "smart_review", "compaction", "compacting", "error", "llm_retry", "resumed",
  "run_finished", "queue", "notes", "state", "model_waking", "model_ready", "workspace_ready", "branch_saved", "review",
  "target_waiting", "target_online", "compaction_started", "prompt_progress", "gpu_paused", "gpu_resumed", "waiting_memory", "memory_recovered", "app_context", "app_tool_call", "app_tool_result",
  "quote_check", "ungrounded_quotes", "taint_added", "taint_cleared", "checkpoint", "rewound", "forked", "sandbox_setup",
];

export function mountSession({ $app, h, fill, append, api, setHeader, toast, go, route, validId, isGuest, isMember, isOwner, onLeave, badge, reviewBadge,
  progressBar, openStream, layoutBar, viewInfo, downloadDaemonFile, TERMINAL, agentHarnessWeb, browser, confirmSheet = sheets.confirmSheet,
  promptSheet = sheets.promptSheet, announceChange = () => {}, onDaemonChange = null }) {
// Browser globals come in through `browser` (globalThis in the app, a stub under Node) so importing this module touches no DOM.
const { window, document, location, setInterval, clearInterval, setTimeout, clearTimeout } = browser;
const { renameTitle, sessionMenu, bindSessionJumps, placeSessionHeader } = mountSessionUi({ h, api, setHeader, toast, isGuest, onLeave, layoutBar, browser,
  onRenamed: announceChange });
const pageMetrics = () => measurePage(browser);
const scrollPage = (top) => scrollPageOf(top, browser);
const { toolRow, closeViewer } = createToolRows({ h, fill, toast, browser });

async function viewSession(sid, tab, focusApproval) {
  if (!validId(sid)) { go("#/agents", true); return; }
  let session = await api(`/sessions/${sid}`);
  sid = session.id;
  setHeader("agents", session.title || "Session", { page: true });
  let left = false;
  onLeave(() => { left = true; });

  // One compact header (#514): the bar carries back, the title and the ⋯ menu; below it sit a 32 px status strip and the
  // segmented Transcript / Changes / Info control.
  const tabs = h("div", { class: "tabs session-tabs", role: "group", "aria-label": "Session view" },
    ["transcript", "changes", "info"].map((name) => h("button", {
      class: (tab === name || (tab === "approval" && name === "transcript")) ? "on" : "",
      type: "button",
      onclick: () => go(name === "transcript" ? `#/s/${sid}` : `#/s/${sid}/${name}`, true),
    }, name[0].toUpperCase() + name.slice(1))));
  const head = h("div", { class: "session-strip" });
  const sessionChrome = h("div", { class: "session-chrome" }, head, tabs);
  append($app, sessionChrome);
  const unplace = placeSessionHeader(head, tabs, sessionChrome);
  onLeave(unplace);
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
    const limitName = String(limits.rateLimitType || "limit").replace("seven_day", "7d").replace("five_hour", "5h");
    const backendUsage = session.backend && session.backend !== "local" && limits.utilization !== undefined
      ? ` · ${limitName} ${Math.round(limits.utilization * 100)}%` : "";
    const onTarget = session.target !== "tower" ? ` on ${TARGET_LABEL[session.target] || session.target}` : "";
    const pct = ctxLimit && ctxUsed ? Math.round((100 * ctxUsed) / ctxLimit) : null;
    const tokens = `Tokens ${fmtTokens(totals.prompt_tokens)} in · ${fmtTokens(totals.completion_tokens)} out`;
    const taint = session.taint || [];
    fill(head, badge(session.status),
      session.queue_position > 0 ? h("span", { class: "badge" }, `#${session.queue_position} in GPU queue`) : null,
      taint.length ? h("span", { class: "badge warn", title: `Untrusted content read: ${taint.map((t) => t.origin).join(", ")}. Risky actions ask for approval until cleared.` }, "Tainted") : null,
      h("span", { class: "session-strip-meta", title: tokens }, `${session.project}${onTarget} · ${session.backend || "local"}${backendUsage} · ${session.model}`),
      pct === null ? null : h("span", { class: `ctx${pct >= 55 ? " high" : ""}`, title: `Context window: ~${ctxUsed} of ${ctxLimit} tokens. Older context is condensed as it fills up. ${tokens}.` },
        progressBar(pct / 100), `${pct}%`));
  };
  renderHead();

  // The overflow menu's actions work on every tab, so they live above the tab split. Changes and Info get no stream, so
  // each action repaints the strip itself.
  const cancelTask = async () => {
    if (!(await confirmSheet({ title: "Cancel this task?", confirmLabel: "Cancel task", cancelLabel: "Keep running",
      destructive: true }))) return;
    try { session = { ...session, ...(await api(`/sessions/${sid}/cancel`, { method: "POST" })) }; renderHead(); } catch (e) { toast(e.message); }
  };
  const menuActions = {
    rename: () => renameTitle(session, () => !left),
    cancel: cancelTask,
    rerun: async () => {
      try {
        const s = await api(`/sessions/${sid}/rerun`, { method: "POST" });
        location.hash = `#/s/${s.id}`;
      } catch (e) { toast(e.message); }
    },
    "clear-taint": async () => {
      if (!(await confirmSheet({ title: "Clear taint?", message: "Risky actions will follow the project rules again.", confirmLabel: "Clear taint",
        destructive: true }))) return;
      try { session = { ...session, ...(await api(`/sessions/${sid}/taint/clear`, { method: "POST" })) }; renderHead(); } catch (e) { toast(e.message); }
    },
    download: () => downloadDaemonFile(`/sessions/${sid}/transcript`, `${sid}.md`),
  };
  if (!isGuest()) sessionMenu({ items: () => sessionMenuItems(session, { guest: false, terminal: TERMINAL }), run: (id) => menuActions[id]() });

  if (tab === "changes" || tab === "info") {
    if (tab === "changes") {
      document.body.classList?.add("session-changes");
      onLeave(() => document.body.classList?.remove("session-changes"));
    }
    pendingApprovalBar(session, () => !left);
    if (tab === "changes") await viewChanges(session); else viewInfo(session);
    jumps.updateJumps();
    return;
  }

  const feed = h("div");
  append($app, feed);

  // #510: while the transcript's stream is down, a strip under the segmented control says so and how long ago the last
  // event arrived, instead of the old silent retry loop. It sits in the sticky chrome so it shows at any scroll position.
  const connStrip = h("div", { class: "conn-strip", role: "status", hidden: true });
  sessionChrome.append(connStrip);
  let lastHeardAt = Date.now();
  let streamState = "";
  let connTick = null;
  const paintConnStrip = () => {
    const down = streamState === "reconnecting" || streamState === "offline";
    connStrip.classList.toggle("offline", streamState === "offline");
    connStrip.textContent = down ? `${streamState === "offline" ? "Offline" : "Reconnecting"} · last event ${sinceText(lastHeardAt)}` : "";
    if (connStrip.hidden === down) {
      connStrip.hidden = !down;
      layoutBar();  // the sticky chrome changed height
    }
    if (down && !connTick) connTick = setInterval(paintConnStrip, 1000);
    if (!down && connTick) { clearInterval(connTick); connTick = null; }
  };
  onLeave(() => clearInterval(connTick));

  // composer (owner only; guests may watch the live transcript)
  const input = h("textarea", { placeholder: "Message the agent…", rows: 1 });
  const send = h("button", { class: "btn primary" }, "Send");
  const composer = isGuest() ? null : h("div", { class: "composer" }, h("div", { class: "inner", style: "flex-direction:column;align-items:stretch" },
    h("div", { class: "row", style: "flex-wrap:nowrap;align-items:flex-end" }, input, send)));
  if (composer) document.body.append(composer);
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

  // Cancel, Run again and Clear taint moved to the ⋯ menu (#514); the composer keeps only its placeholder in step.
  const renderActions = () => {
    input.placeholder = TERMINAL.has(session.status) ? "Continue this session…" : "Add guidance…";
  };
  renderActions();

  // transcript rendering
  // Follow new output only while the reader is at the bottom. Any upward scroll (wheel, finger, momentum) stops
  // following, however small; reaching the bottom again resumes it. A generous "near the bottom" margin used to
  // snap slow upward scrolls back down on every streamed token.
  let follow = true;
  let touching = false;
  let lastY = pageMetrics().y;
  const pageHeight = jumps.pageHeight;
  const atBottom = () => {
    const { y, viewH, pageH } = pageMetrics();
    return viewH + y >= pageH - 2;
  };
  const scrollDown = (force = false) => {
    if (!force && (!follow || touching)) return;
    scrollPage(pageHeight());
    lastY = pageMetrics().y;
    jumps.updateJumps();
  };
  const onScroll = () => {
    const y = pageMetrics().y;
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
  let memoryNote = null;
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
    const row = toolRow({ name: fn.name, summary: toolSummaryText(fn, args), kind: toolKind(fn.name, args), args });
    const slot = h("div", { class: "ev" }, row.el);
    calls.set(call.id, Object.assign(row, { slot }));
    return slot;
  };

  // A pending approval is a bottom sheet pinned over the session (#507); the transcript keeps a one-line record that the
  // decision fills in. The composer yields while any sheet is open.
  const syncComposer = () => { if (composer) composer.hidden = pendingSheets.size > 0; };
  // The docked card's height (#564), so the transcript's end and the jump button clear it.
  let sheetObserver = null;
  const watchSheetHeight = (sheet) => {
    const paint = () => document.documentElement.style?.setProperty?.("--approval-h", `${sheet.offsetHeight || 0}px`);
    sheetObserver?.disconnect();
    sheetObserver = browser.ResizeObserver ? new browser.ResizeObserver(paint) : null;
    sheetObserver?.observe(sheet);
    paint();
  };
  onLeave(() => sheetObserver?.disconnect());
  const pendingSheets = new Set();
  const dismissSheets = () => {
    for (const id of [...pendingSheets]) {
      const a = approvals.get(id);
      a.sheet.remove();
      a.card.classList.add("decided");
      fill(a.slotState, "No longer pending");
    }
    pendingSheets.clear();
    syncComposer();
  };
  const approvalCard = (a) => {
    const note = h("input", { type: "text", id: `approval-note-${a.id}`, placeholder: "Note for the agent (optional)", hidden: true });
    const noteToggle = h("button", { class: "approval-note-toggle", type: "button", "aria-expanded": "false", "aria-controls": note.id }, "Add a note for the agent");
    noteToggle.addEventListener("click", () => {
      note.hidden = false;
      noteToggle.hidden = true;
      if (note.focus) note.focus();
    });
    const buttons = h("div", { class: "approval-actions" });
    const decide = async (decision) => {
      buttons.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        await api(`/sessions/${sid}/approvals/${a.id}`, { method: "POST", body: { decision, note: note.value } });
      } catch (e) {
        toast(e.message);
        buttons.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    };
    if (isGuest()) {
      append(buttons, h("p", { class: "muted small" }, "Demo access cannot approve or deny."));
    } else {
      append(buttons,
        h("button", { class: "btn approval-deny", type: "button", onclick: () => decide("deny") }, "✕ Deny"),
        h("button", { class: "btn approval-approve", type: "button", onclick: () => decide("approve") }, "✓ Approve"));
    }
    const what = approvalWhat(a);
    const reviewerReason = a.smart?.reason ? `: ${a.smart.reason}` : "";
    const rec = a.smart?.recommendation
      ? h("p", { class: "smart-rec" },
          `Reviewer: ${a.smart.recommendation} · ${Math.round((a.smart.confidence || 0) * 100)}%${reviewerReason}`)
      : null;
    // Memory library changes carry "summary\n\n<unified diff>"; file writes carry just the diff.
    const memory = a.tool === "memory_edit" || a.tool === "memory_write";
    const [summary, diff] = memory && a.detail.includes("\n\n") ? [a.detail.slice(0, a.detail.indexOf("\n\n")), a.detail.slice(a.detail.indexOf("\n\n") + 2)] : ["", a.detail || ""];
    const diffView = /^@@ /m.test(diff) ? h("div", { class: "diff approval-diff" }, diff.split("\n")
      .filter((line) => !/^(---|\+\+\+) /.test(line))
      .map((line) => h("div", { class: approvalDiffClass(line) }, line))) : null;
    const slotState = h("div", { class: "muted small" }, "Waiting for your decision");
    const slot = h("div", { class: "approval approval-slot", id: `approval-${a.id}` },
      h("strong", {}, `Approval needed: ${a.reason || a.tool}`), slotState);
    const heading = h("h4", { tabindex: "-1" }, "Approval needed");
    // On desktop the sheet is a card docked at the pane's foot (#564); the classes let style.css lay its parts out in rows.
    const sheet = h("section", { class: "approval-sheet", role: "region", "aria-label": "Approval needed" },
      h("div", { class: "approval-head" },
        heading,
        h("a", { href: `#approval-${a.id}`, class: "approval-show", onclick: (ev) => {
          ev.preventDefault();
          if (slot.scrollIntoView) slot.scrollIntoView({ block: "center", behavior: "smooth" });
        } }, "Show in transcript")),
      h("p", { class: "approval-what" }, a.reason || a.tool),
      rec,
      summary ? h("p", { class: "approval-summary", style: "margin:4px 0 8px" }, summary) : null,
      diffView || h("pre", { class: "approval-detail" }, a.detail || what),
      a.detail ? h("div", { class: "muted small approval-tool" }, `${a.tool} ${a.args?.path || ""}`) : null,
      noteToggle, note, buttons,
      isGuest() ? null : h("button", { class: "approval-cancel", type: "button", onclick: cancelTask }, "Cancel the whole task"));
    approvals.set(a.id, { card: slot, slotState, sheet, buttons, note });
    // Focus never jumps to Approve. It moves to the card's heading only when it was in the transcript or the composer
    // (which hides now), so a keyboard reader lands on the decision instead of on the page body.
    const active = document.activeElement;
    const superseded = [...pendingSheets].map((id) => approvals.get(id)?.sheet);
    const wasInPane = !!active && [$app, composer, ...superseded].some((el) => typeof el?.contains === "function" && el.contains(active));
    dismissSheets(); // one decision at a time: a newer request supersedes an orphaned older one, so nothing can hold the composer hidden
    pendingSheets.add(a.id);
    document.body.append(sheet);
    watchSheetHeight(sheet);
    syncComposer();
    if (wasInPane && focusApproval !== a.id) heading.focus?.({ preventScroll: true });
    if (focusApproval === a.id) {
      slot.classList.add("focus");
      setTimeout(() => slot.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
    }
    return slot;
  };

  const handlers = {
    user_message: (e) => { add(h("div", { class: "ev msg user" }, e.data.content)); },
    app_context: (e) => add(h("details", { class: "thinking ev" }, h("summary", {}, "Context from the app"), h("div", { class: "text" }, e.data.content))),
    taint_added: (e) => {
      session = { ...session, taint: withTaint(session.taint, e.data) };
      renderHead();
      add(h("p", { class: "note" }, `Session read untrusted content from ${e.data.origin}: risky actions now ask for approval`));
    },
    taint_cleared: () => {
      session = { ...session, taint: [] };
      renderHead();
      add(h("p", { class: "note" }, "Taint cleared by the owner"));
    },
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
        const thought = took ? ` for ${took}` : "";
        wrap.append(h("details", { class: "thinking" }, h("summary", {}, `Thought${thought} (${d.completion_tokens} tokens · ${d.gen_tps} tok/s)`),
          h("div", { class: "text" }, d.reasoning)));
      }
      if (d.content?.trim()) {
        lastContent = d.content.trim();
        wrap.append(h("div", { class: `msg assistant${d.tool_calls.length ? "" : " final"}`, html: md(d.content, pages) }));
      }
      if (wrap.childNodes.length) add(wrap);
      for (const call of d.tool_calls || []) add(toolEl(call));
    },
    tool_call: (e) => {
      const c = calls.get(e.data.id);
      if (c && e.data.decision === "ask") c.setState("needs approval", "warn");
      else if (c && e.data.decision !== "allow") c.setState("blocked", "err");
    },
    approval_requested: (e) => {
      const card = approvalCard(e.data);
      const c = calls.get(e.data.tool_call_id);
      if (c) c.slot.append(card); else feed.append(h("div", { class: "ev" }, card));
      if (focusApproval !== e.data.id) grew();
    },
    approval_auto_approved: (e) => {
      const badge = h("p", { class: "note smart-auto" },
        `Auto-approved: the deterministic gate and smart reviewer both allowed this ${e.data.tool || "call"} (${e.data.reason || "routine workspace work"}).`);
      add(badge);
      const c = calls.get(e.data.tool_call_id);
      if (c) c.setState("auto-approved", "ok");
    },
    smart_review: () => {},
    approval_decided: (e) => {
      const a = approvals.get(e.data.id);
      if (!a) return;
      a.card.classList.add("decided");
      a.card.classList.remove("focus");
      a.sheet.remove();
      pendingSheets.delete(e.data.id);
      syncComposer();
      fill(a.slotState, h("span", { class: `badge ${e.data.status === "approved" ? "done" : "failed"}` },
        e.data.status + (e.data.note ? `: ${e.data.note}` : "")));
    },
    tool_result: (e) => {
      pendingCalls.delete(e.data.id);
      if ((e.data.name === "web_fetch" || fetchById.has(e.data.id)) && e.data.output) {
        const fromOutput = (e.data.output.split("\n").find((line) => /^https?:\/\//i.test(line.trim())) || "").trim();
        rememberFetch(e.data.id, fetchById.get(e.data.id) || fromOutput, e.data.output);
      }
      let c = calls.get(e.data.id);
      if (!c) {
        c = toolRow({ name: e.data.name, summary: "", kind: toolKind(e.data.name, null), args: null });
        add(h("div", { class: "ev" }, c.el));
      }
      const done = resultState(e.data.ok, e.data.seconds);
      c.setState(done.text, done.kind);
      c.setOutput(e.data.output, e.data.output_chars);
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
      if (d.tier === "mask") {
        add(h("p", { class: "note" },
          `Replaced old tool outputs with recoverable receipts (~${fmtTokens(d.tokens_saved)} tokens saved)`));
        return;
      }
      if (d.totals) totals = d.totals;
      if (d.tokens_after) ctxUsed = d.tokens_after;
      renderHead();
      const text = d.tier === "round_reset"
        ? `Round reset: ~${fmtTokens(d.tokens_before)} → ~${fmtTokens(d.tokens_after)} tokens`
        : d.tier === "summary"
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
    state: (e) => add(h("details", { class: "thinking ev" }, h("summary", {}, "Agent saved state"), h("pre", { class: "text" }, JSON.stringify(e.data.state || e.data, null, 2)))),
    error: (e) => add(h("p", { class: "note bad" }, e.data.message)),
    quote_check: (e) => add(h("details", { class: "thinking ev" },
      h("summary", {}, `Asked the agent to fix ${e.data.quotes.length} quote${e.data.quotes.length === 1 ? "" : "s"} not found in anything it read`),
      h("div", { class: "text" }, e.data.quotes.map((q) => `“${q}”`).join("\n")))),
    ungrounded_quotes: (e) => add(h("div", { class: "note bad" },
      h("p", {}, `⚠ ${e.data.quotes.length === 1 ? "This quote" : "These quotes"} in the answer didn't appear in anything the agent read, so ${e.data.quotes.length === 1 ? "it" : "they"} may be made up:`),
      h("div", { class: "text", style: "white-space:pre-wrap" }, e.data.quotes.map((q) => `“${q}”`).join("\n")))),
    llm_retry: (e) => add(h("p", { class: "note" }, `Model call retried (${e.data.attempt})`)),
    resumed: () => add(h("p", { class: "note" }, "Agent Harness Server restarted — session resumed")),
    workspace_ready: (e) => add(h("p", { class: "note" }, `Checked out on branch ${e.data.branch} (from ${e.data.base_branch})`)),
    branch_saved: (e) => add(h("p", { class: "note" }, h("button", {
      class: "btn small", type: "button", onclick: () => go(`#/s/${sid}/changes`, true),
    }, `Branch saved: ${e.data.commits.length} commit${e.data.commits.length === 1 ? "" : "s"} to review${e.data.auto_commit ? " (leftover edits committed)" : ""}`))),
    review: (e) => add(h("p", { class: "note" }, `Review: ${e.data.detail}`)),
    checkpoint: (e) => {
      if (e.data.status === "skipped") { add(h("p", { class: "note" }, `Turn not checkpointed: ${e.data.reason}`)); return; }
      const turn = e.data.turn;
      const act = async (btn, path, body) => {
        btn.disabled = true;
        try {
          const s = await api(`/sessions/${sid}/checkpoints/${turn}/${path}`, { method: "POST", body });
          if (path === "fork") go(`#/s/${s.id}`, true); else await viewSession(sid);
        } catch (err) { toast(err.message, 8000); btn.disabled = false; }
      };
      // Hosted CLI sessions keep their own state, which can't be truncated: Fork (with a transcript digest) only.
      const local = !session.backend || session.backend === "local";
      if (isMember()) { add(h("p", { class: "note checkpoint" }, `Checkpoint ${turn} saved`)); return; } // owner API only
      add(h("p", { class: "note checkpoint" }, `Checkpoint ${turn} saved `,
        local ? h("button", {
          class: "btn small", type: "button", title: "Restore the workspace and the agent's context to this point. Packages, processes and files outside the workspace are not undone.",
          onclick: async (ev) => {
            const btn = ev.currentTarget;
            if (await confirmSheet({ title: `Rewind to checkpoint ${turn}?`, message: "Later file changes are undone (the transcript keeps them).",
              confirmLabel: "Rewind", destructive: true })) void act(btn, "rewind");
          },
        }, "Rewind here") : null, " ",
        h("button", {
          class: "btn small", type: "button", title: "Start a new session from this point, on its own branch",
          onclick: async (ev) => {
            const btn = ev.currentTarget;
            const prompt = await promptSheet({ title: `Fork from checkpoint ${turn}`, label: "Instruction for the forked session",
              message: "Starts a new session from this point, on its own branch.", confirmLabel: "Fork", validate: sheets.required("an instruction") });
            if (prompt?.trim()) void act(btn, "fork", { prompt });
          },
        }, "Fork from here")));
    },
    rewound: (e) => add(h("p", { class: "note" }, `Rewound to checkpoint ${e.data.turn}: the workspace and context are as they were then; later turns above are kept for the record`)),
    forked: (e) => add(h("p", { class: "note" }, "Forked from ", h("a", { href: `#/s/${e.data.parent}` }, e.data.parent),
      ` at checkpoint ${e.data.turn}${e.data.summary_note ? ` (${e.data.summary_note})` : ""}`)),
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
        `Paused: ${e.data.reason} needs the GPU, so the model was unloaded. The task continues ${Math.round(e.data.resume_after_seconds / 60)} min after that ends (Actions → Resources to resume now)`)));
    },
    gpu_resumed: (e) => {
      const text = `GPU free again after ${fmtSpan(e.data.seconds)}; reloading the model`;
      if (gpuNote) fill(gpuNote, text);
      else add(h("p", { class: "note" }, text));
      gpuNote = null;
    },
    waiting_memory: (e) => {
      memoryNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `Waiting for memory: ${e.data.reason}, so the ${e.data.waiting_for || "work"} doesn't start yet (Actions → Resources)`)));
    },
    memory_recovered: (e) => {
      const text = `Memory recovered after ${fmtSpan(e.data.seconds)}; continuing`;
      if (memoryNote) fill(memoryNote, text);
      else add(h("p", { class: "note" }, text));
      memoryNote = null;
    },
    target_waiting: (e) => {
      targetNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `Waiting for the ${TARGET_LABEL[e.data.target] || e.data.target}: it's offline or asleep. The task continues when it wakes`)));
    },
    target_online: (e) => {
      const text = `${TARGET_LABEL[e.data.target] || e.data.target} is back after ${fmtSpan(e.data.seconds)}`;
      if (targetNote) fill(targetNote, text);
      else add(h("p", { class: "note" }, text));
      targetNote = null;
    },
    queue: (e) => { session.queue_position = e.data.position; renderHead(); },
    status: (e) => {
      session.status = e.data.status;
      // The run ended (cancelled elsewhere, finished): a sheet left open would offer a decision that no longer exists.
      // Not on waiting_target and the like: a pending approval survives those and is not replayed.
      if (TERMINAL.has(e.data.status)) dismissSheets();
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
      lastHeardAt = Date.now();
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
  onLeave(openStream(() => (isGuest() && !agentHarnessWeb.token
    ? agentHarnessWeb.url(`/sessions/${encodeURIComponent(sid)}/events?after=${lastSeq}`, "legacy")
    : agentHarnessWeb.sessionStreamUrl(sid, lastSeq)), tracked,
    { authorized: !!agentHarnessWeb.token, onState: (next) => { streamState = next; paintConnStrip(); } }));
  if (composer) onLeave(() => composer.remove());
  onLeave(() => { for (const a of approvals.values()) a.sheet.remove(); });
  onLeave(closeViewer);
}

// Changes and Info have no transcript stream, so a pending approval shows there as a one-line bar at the pane's foot with
// Review (#564), refreshed by the app-wide stream's events. style.css shows it from 768 px up only.
function pendingApprovalBar(session, isActive) {
  // Built once and updated in place, so a refresh never removes a Review link that has focus.
  const text = h("span", { class: "approval-bar-text" });
  const review = h("a", { class: "btn approval-bar-review" }, "Review");
  const bar = h("div", { class: "approval-bar", role: "status", hidden: true }, text, review);
  document.body.append(bar);
  const paint = (s, pending) => {
    const a = pending[0] || null;
    bar.hidden = !a;
    if (!a) return;
    const label = `Approval needed · ${a.reason || a.tool}`;
    const href = `#/s/${s.id}/approval/${a.id}`;
    if (text.textContent !== label) text.textContent = label;
    if (review.getAttribute("href") !== href) review.setAttribute("href", href);
  };
  // The summary lists pending approvals only while the status is waiting_approval, but one can outlive that status (a
  // session resumed as waiting_target after a restart), so refreshes ask the approvals route itself.
  const pendingOf = async (s) => {
    if (TERMINAL.has(s.status)) return [];
    if (isGuest()) return s.pending_approvals || [];
    try { return await api(`/sessions/${s.id}/approvals`); } catch (_) { return s.pending_approvals || []; }
  };
  paint(session, session.pending_approvals || []);
  let timer = null;
  let latest = 0;  // only the newest refresh paints, so a slow older answer can't undo a newer one
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      timer = null;
      const mine = ++latest;
      try {
        const s = await api(`/sessions/${session.id}`);
        const pending = await pendingOf(s);
        if (isActive() && mine === latest) paint(s, pending);
      } catch (_) { /* keep the last state; the next event retries */ }
    }, 300);
  };
  const stop = onDaemonChange?.(refresh);
  // An approval requested while the page's first fetch was in flight raised no event this listener heard: catch up once.
  if (stop) refresh();
  onLeave(() => { clearTimeout(timer); stop?.(); bar.remove(); });
  return bar;
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
    if (isGuest()) {
      return h("div", { class: "merge-conflict" },
        h("p", { class: "muted small" }, `Merge conflicts in ${files.join(", ")}. Demo access cannot ask the agent to resolve them.`));
    }
    const ask = h("button", { class: "btn primary" }, "Ask agent to resolve");
    ask.addEventListener("click", async () => {
      if (!(await confirmSheet({ title: `Ask the agent to merge origin/${base} and resolve ${files.length} conflicting file${files.length === 1 ? "" : "s"}?`,
        confirmLabel: "Ask agent" }))) return;
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
  const act = (action, ask) => async (ev) => {
    const card = ev.target.closest(".card");
    if (ask && !(await confirmSheet(ask))) return;
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      const updated = await api(`/sessions/${s.id}/review/${action}`, { method: "POST" });
      toast(updated.review_detail || `${action} done`, 4000);
      void route();
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
  if (!isGuest() && !busy && !s.workspace_removed && s.review !== "discarded") {
    if (s.repo_kind === "local") {
      buttons.push(h("button", { class: "btn ok", onclick: act("merge", { title: `Squash-merge ${s.branch} into ${base}?`, confirmLabel: "Merge" }) },
        `Merge into ${base}`));
      if (s.push_target) {
        buttons.push(h("button", { class: "btn ok", onclick: act("push", {
          title: `Push branch ${s.branch} to GitHub repository ${s.push_target} (branch ${s.branch}) using your GitHub connection?`,
          message: "GitHub records your account as the pusher; commit authors stay as they are.", confirmLabel: "Push" }) }, "Push to GitHub"));
      }
    } else {
      buttons.push(h("button", { class: "btn ok", onclick: act("push", { title: `Push ${s.branch} to the remote?`, confirmLabel: "Push" }) }, "Push branch"));
    }
    buttons.push(h("button", { class: "btn bad solid", onclick: act("discard", { title: "Discard this branch and delete the workspace?",
      message: "This can't be undone.", confirmLabel: "Discard", destructive: true }) }, "Discard"));
  }
  return h("section", { class: "card" },
    h("h3", {}, "Review"),
    h("div", { class: "meta" }, h("span", {}, `branch ${s.branch}`), s.base_branch ? h("span", {}, `from ${s.base_branch}`) : null,
      s.review ? reviewBadge(s.review, s.review) : null),
    s.review_detail ? h("p", { class: "muted small" }, s.review_detail) : null,
    busy ? h("p", { class: "muted small" }, "The agent is still working; review when the run ends.") : null,
    buttons.length ? h("div", { class: "row end", style: "margin-top:8px" }, buttons) : null);
}

async function viewChanges(session) {
  const sid = session.id;
  const box = h("div", {}, h("p", { class: "note" }, "Loading changes…"));
  const review = reviewCard(session);
  append($app, box);
  const message = (text) => fill(box, review, h("p", { class: "empty" }, text));
  const data = await api(`/sessions/${sid}/changes`);
  if (data.removed) {
    message("This workspace was cleaned up or discarded.");
    return;
  }
  if (!data.repos.length) {
    message("No git repositories in this workspace yet.");
    return;
  }
  const canComment = !isGuest() && data.repos.some((r) => r.parsed);
  let comments = [];
  if (canComment) {
    try { comments = await api(`/sessions/${sid}/review-comments`); } catch { comments = []; }
  }
  const state = { comments, sel: null };  // sel: {repo, path, side, anchor, start, end}
  const render = () => {
    state.fileButtons = [];
    const repos = data.repos.map((repo) => repoChanges(sid, repo, state, canComment, render));
    fill(box, h("div", { class: "changes-layout" },
      h("aside", { class: "changes-sidebar", "aria-label": "Review and changed files" }, review,
        secretScanCard(sid, data.secret_scan, state, canComment, render),
        h("nav", { class: "changes-files card", "aria-label": "Changed files" }, h("h3", {}, "Changed files"),
          repos.map((repo) => repo.navigation))),
      h("div", { class: "changes-diffs" }, repos.map((repo) => repo.detail))));
  };
  render();
}

// Secret scan of the added lines (issue #263): findings block Merge/Push until fixed or dismissed with a reason.
// Values never reach the browser; `preview` keeps at most the first and last two characters.
function secretScanCard(sid, scan, state, canComment, render) {
  if (!scan) return null;
  if (scan.status === "unsupported") {  // older runners cannot supply scan input
    return h("section", { class: "card secret-scan" }, h("h3", {}, "Secret scan"),
      h("p", { class: "note" }, `Secret scan not available for this target: ${scan.message}.`));
  }
  if (scan.status !== "ok") {
    return h("section", { class: "card secret-scan" }, h("h3", {}, "Secret scan"),
      h("p", { class: "note" }, `The secret scan could not run, so Merge and Push are blocked: ${scan.message}`));
  }
  if (!scan.findings.length) return null;
  const askFix = async (e) => {
    e.currentTarget.disabled = true;
    try {
      // Drafts for lines in the diff; a history-rewrite request for values only in earlier commits; commits
      // already on the remote can only be dismissed. The server's message says which happened.
      const result = await api(`/sessions/${sid}/secret-findings/fix`, { method: "POST" });
      state.comments.push(...result.drafts);
      toast(result.message, 6000);
    } catch (err) { toast(err.message, 6000); }
    render();
  };
  const dismiss = (f) => async (e) => {
    const box = e.currentTarget.closest(".secret-finding");
    const input = h("input", { type: "text", maxlength: "500", placeholder: "Why this is not a secret (required)", "aria-label": "Reason" });
    const confirm = h("button", { class: "btn small bad", type: "button", onclick: async () => {
      const reason = input.value.trim();
      if (!reason) return input.focus();
      confirm.disabled = true;
      try {
        const done = await api(`/sessions/${sid}/secret-findings/${f.fingerprint}/dismiss`, { method: "POST", body: { reason } });
        Object.assign(f, { dismissed: true, dismissal: done.dismissal });
        scan.open = scan.findings.filter((x) => !x.dismissed).length;
        render();
      } catch (err) { toast(err.message, 6000); confirm.disabled = false; }
    } }, "Dismiss");
    box.querySelector(".secret-actions").replaceChildren(input, confirm);
    input.focus();
  };
  const row = (f) => h("div", { class: "secret-finding" },
    h("div", { class: "row", style: "justify-content:space-between" },
      h("span", { class: "small" }, `${f.repo === "." ? "" : f.repo + "/"}${f.file}:${f.line}${f.commit ? ` @ ${f.commit}` : ""} · ${f.rule} · `, h("code", {}, f.preview)),
      f.dismissed ? h("span", { class: "badge cancelled" }, "dismissed") : null),
    f.commit && !f.dismissed ? h("div", { class: "muted small" }, `Removed by a later commit but still in commit ${f.commit}, so it blocks Push (not Merge). Dismiss it, or rewrite the branch.`) : null,
    f.dismissed && f.dismissal ? h("div", { class: "muted small" }, `Reason: ${f.dismissal.reason}`) : null,
    !f.dismissed && isOwner() ? h("div", { class: "row end secret-actions" },
      h("button", { class: "btn small", type: "button", onclick: dismiss(f) }, "Dismiss…")) : null);
  return h("section", { class: "card secret-scan" },
    h("h3", {}, "Secret scan"),
    h("p", { class: scan.open ? "note" : "muted small" }, scan.open
      ? `${pluralize(scan.open, "possible secret")} in the added lines. Merge and Push are blocked until each is fixed or dismissed with a reason.`
      : "Every finding was dismissed."),
    scan.findings.map(row),
    scan.open && canComment ? h("div", { class: "row end", style: "margin-top:8px" },
      h("button", { class: "btn ok", type: "button", onclick: askFix }, "Ask agent to fix")) : null,
    h("p", { class: "muted small" }, `${scan.scanner}${scan.cached ? " · cached" : ""}`));
}

// Text of each line on one side of a file in a parsed diff: {line number: text}.
function sideLines(parsed, path, side) {
  const key = side === "old" ? "old" : "new";
  const out = {};
  for (const f of parsed || []) {
    if (f.name !== path) continue;
    for (const ln of f.lines) if (ln[key] !== null) out[ln[key]] = ln.text;
  }
  return out;
}

// A draft is stale once any commented line no longer reads the same in the current diff.
function commentStale(repo, c) {
  const lines = sideLines(repo.parsed, c.path, c.side);
  for (let n = c.start_line; n <= c.end_line; n++) if (lines[n] !== c.quoted[n - c.start_line]) return true;
  return false;
}

function lineRange(start, end) { return start === end ? `${start}` : `${start}–${end}`; }

function repoChanges(sid, repo, state, canComment, render) {
  const files = splitDiff(repo.diff);
  const parsedFiles = new Map((repo.parsed || []).map((f) => [f.name, f]));
  const mine = state.comments.filter((c) => c.repo === repo.path);
  const sel = state.sel?.repo === repo.path ? state.sel : null;

  const pick = (path, side, num) => {
    if (sel?.path === path && sel.side === side) {
      const lines = sideLines(repo.parsed, path, side);
      const start = Math.min(sel.anchor, num), end = Math.max(sel.anchor, num);
      for (let n = start; n <= end; n++) if (lines[n] === undefined) return toast("Pick lines within one hunk.");
      state.sel = { ...sel, start, end };
    } else {
      state.sel = { repo: repo.path, path, side, anchor: num, start: num, end: num };
    }
    render();
  };
  const composer = () => {
    const lines = sideLines(repo.parsed, sel.path, sel.side);
    const quoted = [];
    for (let n = sel.start; n <= sel.end; n++) quoted.push(lines[n]);
    const input = h("textarea", { class: "review-input", rows: 3, placeholder: "Comment for the agent", "aria-label": "Comment" });
    input.value = sel.text || "";  // kept across re-renders while extending the range
    input.addEventListener("input", () => { sel.text = input.value; });
    const add = h("button", { class: "btn ok", type: "button", onclick: async () => {
      const text = input.value.trim();
      if (!text) return input.focus();
      add.disabled = true;
      try {
        const made = await api(`/sessions/${sid}/review-comments`, { method: "POST", body: {
          repo: repo.path, path: sel.path, side: sel.side, start_line: sel.start, end_line: sel.end,
          quoted, comment: text, base: repo.base, head: repo.head } });
        state.comments.push(made);
        state.sel = null;
        render();
      } catch (e) { toast(e.message, 6000); add.disabled = false; }
    } }, "Add comment");
    const where = `${sel.side === "old" ? "removed " : ""}line ${lineRange(sel.start, sel.end)}`;
    return h("div", { class: "review-composer" },
      h("div", { class: "muted small" }, `${sel.path} · ${where}. Tap another line to extend.`),
      input,
      h("div", { class: "row end" }, h("button", { class: "btn", type: "button", onclick: () => { state.sel = null; render(); } }, "Cancel"), add));
  };
  const lineRow = (f, ln) => {
    if (ln.kind === "hunk") return h("div", { class: "hunk" }, ln.text);
    const sign = { add: "+", del: "-" }[ln.kind] || " ";
    let side = "new";
    if (ln.kind === "del") side = "old";
    else if (ln.kind !== "add" && sel?.path === f.name) side = sel.side;
    const num = side === "old" ? ln.old : ln.new;
    const picked = sel?.path === f.name && sel.side === side && num >= sel.start && num <= sel.end;
    const commented = mine.some((c) => c.path === f.name && c.side === side && num >= c.start_line && num <= c.end_line);
    const removed = side === "old" ? "removed " : "";
    const tap = canComment ? {
      role: "button", tabindex: "0", "aria-label": `Comment on ${removed}line ${num}`,
      onclick: () => pick(f.name, side, num),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(f.name, side, num); } },
    } : {};
    const rowClass = `dl ${ln.kind}${picked ? " picked" : ""}${commented ? " commented" : ""}`;
    return h("div", { class: rowClass, ...tap },
      h("span", { class: "ln" }, ln.old ?? ""), h("span", { class: "ln" }, ln.new ?? ""), h("span", { class: "tx" }, `${sign}${ln.text}`));
  };
  const fileBody = (f) => {
    const parsed = parsedFiles.get(f.name);
    if (!parsed) {
      return f.lines.map((line) => h("div", {
        class: diffLineClass(line),
      }, line));
    }
    const out = [];
    for (const ln of parsed.lines) {
      out.push(lineRow(f, ln));
      // The composer opens under the last selected line.
      if (sel?.path === f.name && ln.kind !== "hunk" && (sel.side === "old" ? ln.old : ln.new) === sel.end) out.push(composer());
    }
    return out;
  };
  const removeDraft = async (c) => {
    try {
      await api(`/sessions/${sid}/review-comments/${c.id}`, { method: "DELETE" });
      state.comments = state.comments.filter((x) => x.id !== c.id);
      render();
    } catch (e) { toast(e.message, 6000); }
  };
  const send = async (e) => {
    e.currentTarget.disabled = true;
    try {
      await api(`/sessions/${sid}/review-comments/send`, { method: "POST" });
      state.comments = [];
      toast("Sent to the agent.");
      go(`#/s/${sid}`, true);
    } catch (err) { toast(err.message, 6000); render(); }
  };
  const drafts = mine.length ? h("div", { class: "review-drafts" },
    h("h4", {}, `Draft comments (${mine.length})`),
    mine.map((c) => h("div", { class: "review-draft" },
      h("div", { class: "row", style: "justify-content:space-between" },
        h("span", { class: "small" }, `${c.path} · ${c.side === "old" ? "removed " : ""}line ${lineRange(c.start_line, c.end_line)}`,
          commentStale(repo, c) ? h("span", { class: "badge cancelled", style: "margin-left:6px" }, "stale") : null),
        h("button", { class: "btn small bad", type: "button", "aria-label": "Delete comment", onclick: () => removeDraft(c) }, "Delete")),
      h("pre", { class: "small review-quote" }, c.quoted.join("\n")),
      h("div", {}, c.comment))),
    h("div", { class: "row end", style: "margin-top:8px" },
      h("button", { class: "btn ok", type: "button", onclick: send }, `Send ${state.comments.length} to agent`))) : null;
  const fileViews = new Map();
  const navigation = h("div", { class: "changes-repo-files" },
    h("h4", { class: "muted small" }, repo.path === "." ? "workspace" : repo.path),
    files.length ? files.map((f) => {
      const button = h("button", { class: "changes-file-link", type: "button", "aria-current": state.file?.repo === repo.path && state.file.path === f.name ? "true" : undefined, onclick: () => {
        const file = fileViews.get(f.name);
        file.open = true;
        state.file = { repo: repo.path, path: f.name };
        state.fileButtons.forEach((b) => b.removeAttribute("aria-current"));
        button.setAttribute("aria-current", "true");
        file.scrollIntoView({ block: "start" });
        file.querySelector("summary").focus({ preventScroll: true });
      } }, f.name);
      state.fileButtons.push(button);
      return button;
    }) : h("p", { class: "muted small" }, "No differences."));
  const detail = h("section", { class: "card changes-repo" },
    h("h3", {}, repo.path === "." ? "workspace" : repo.path),
    h("div", { class: "meta" }, h("span", {}, `branch ${repo.branch}`), repo.base ? h("span", {}, `since ${repo.base.slice(0, 8)}`) : null,
      h("span", {}, `${repo.files.length} changed file${repo.files.length === 1 ? "" : "s"}`)),
    repo.commits.length ? h("details", { style: "margin-top:8px" }, h("summary", {}, pluralize(repo.commits.length, "new commit")),
      h("pre", { class: "small", style: "white-space:pre-wrap" }, repo.commits.join("\n"))) : null,
    drafts,
    files.length ? files.map((f) => {
      const file = h("details", { class: "file", open: files.length <= 4 || (sel?.path === f.name) || (state.file?.repo === repo.path && state.file.path === f.name) || undefined },
        h("summary", {}, f.name), h("div", { class: "diff" }, fileBody(f)));
      fileViews.set(f.name, file);
      return file;
    }) : h("p", { class: "muted small" }, "No differences."),
    repo.truncated ? h("p", { class: "note" }, "Diff truncated.") : null);
  return { navigation, detail };
}


return { viewSession };
}
