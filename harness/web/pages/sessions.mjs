// Session list page (#258): the Agents home, with search, machine filter and live refresh. The shell (DOM builder,
// router hooks, event stream) is injected by app.js so this module imports under plain Node and never reaches into
// another page.
import { ago, pluralize, gpuText } from "../lib/format.mjs";
import { TARGET_LABEL, compareTargets } from "../lib/targets.mjs";
import { escapeHtml } from "../lib/markdown.mjs";
import { staleNote } from "../lib/widgets.mjs";

export function mountSessions({ $app, h, fill, append, api, setHeader, showFab, onLeave, isMember, isGuest, badge, reviewBadge, REVIEW_LABEL,
  jobStatusBadge, openStream, ownerSurface, agentHarnessWeb, browser }) {
// Browser globals come in through `browser` (globalThis in the app, a stub under Node) so importing this module touches no DOM.
const { window, document, localStorage } = browser;

let searchQuery = "";  // kept while navigating, so Back from a result returns to the results
let sessionTarget = "all";
try { sessionTarget = localStorage.getItem("harness.sessionTarget") || "all"; } catch (_) { /* private mode */ }

// Search passages mark matches with U+0002 … U+0003 (control characters, matched on purpose); everything else is escaped.
const markPassage = (text) => escapeHtml(text).replaceAll("\u0002", "<mark>").replaceAll("\u0003", "</mark>");
const PASSAGE_KIND = { title: "title", message: "you", assistant: "agent", tool: "tool output", answer: "answer", context: "app context" };

async function viewList() {
  setHeader("agents", "Agents");
  const list = h("div");
  const results = h("div", { hidden: true });
  const queueNote = h("p", { class: "note" });
  const search = h("input", { type: "search", placeholder: "Search", value: searchQuery, class: "search" });
  const targetSwitch = h("div", { class: "tabs", role: "group", "aria-label": "Filter sessions by machine" });
  // #510: a failed refresh says so (when the list last updated, and why) instead of silently keeping the old list.
  const stale = staleNote({ make: h, place: (el) => targetSwitch.after(el), onRetry: () => refreshNow() });
  append($app, h("div", { class: "search-wrap" }, search), targetSwitch, queueNote, results, list);
  showFab("#/new", "+ New task");

  let sessions = [];
  let targets = [];
  const targetName = (target) => target === "tower" ? "Tower" : TARGET_LABEL[target] || target;
  const sessionCardKey = (s) => [
    s.id, s.title, s.status, s.updated_at, s.chat_summary, s.queue_position, s.review, s.job_status,
    s.target, s.project, (s.pending_approvals || []).map((a) => a.id).join(","),
  ].join("\0");
  const renderSessions = () => {
    const visible = sessionTarget === "all" ? sessions : sessions.filter((s) => s.target === sessionTarget);
    if (!sessions.length) {
      delete list.dataset.keys;
      fill(list, h("p", { class: "empty" }, "No sessions yet. Start one with “New task”."));
      return;
    }
    if (!visible.length) {
      delete list.dataset.keys;
      fill(list, h("p", { class: "empty" }, `No sessions on the ${targetName(sessionTarget)} yet.`));
      return;
    }
    const keys = visible.map(sessionCardKey).join("\n");
    if (list.dataset.keys === keys && list.querySelector("a.card")) return;
    list.dataset.keys = keys;
    fill(list, visible.map((s) => {
      const pending = (s.pending_approvals || []).length;
      const approvalPath = pending ? `/approval/${s.pending_approvals[0].id}` : "";
      return h("a", { class: "card", href: `#/s/${s.id}${approvalPath}` },
        h("h3", {}, s.title),
        h("div", { class: "meta" },
          badge(s.status),
          pending ? h("span", { class: "badge waiting_approval" }, pluralize(pending, "approval")) : null,
          s.queue_position > 0 ? h("span", {}, `#${s.queue_position} in queue`) : null,
          s.review ? reviewBadge(s.review, REVIEW_LABEL[s.review] || s.review) : null,
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
  if (searchQuery.trim()) void runSearch();

  const render = async () => {
    const [freshSessions, queue, gpu, projects] = await Promise.all([
      api("/sessions"), api("/queue"), isMember() ? Promise.resolve(null) : api("/gpu").catch(() => null), api("/projects")]);
    sessions = freshSessions;
    targets = [...new Set(projects.map((p) => p.target || "tower"))]
      .sort(compareTargets);
    const waiting = queue.filter((q) => q.position > 0).length;
    const paused = gpu && (gpu.manual || gpu.state !== "clear");
    fill(queueNote,
      paused ? h("a", { href: "#/actions/resources" }, `⏸ ${gpuText(gpu)}`) : "",
      paused && waiting ? " · " : "",
      waiting ? `${waiting} waiting for the GPU` : "");
    renderTargetSwitch();
    renderSessions();
    stale.ok();
  };
  const refreshNow = () => render().catch((e) => {
    console.error("session list refresh failed", e);
    stale.failed(e);
  });
  await render();
  let timer = null;
  let holding = false;
  let pendingRefresh = false;
  const releaseHold = () => {
    holding = false;
    if (pendingRefresh) {
      pendingRefresh = false;
      void refreshNow();
    }
  };
  list.addEventListener("pointerdown", () => { holding = true; });
  window.addEventListener("pointerup", releaseHold);
  window.addEventListener("pointercancel", releaseHold);
  onLeave(() => {
    window.removeEventListener("pointerup", releaseHold);
    window.removeEventListener("pointercancel", releaseHold);
  });
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (holding) { pendingRefresh = true; return; }
      void refreshNow();
    }, 300);
  };
  const handlers = {};
  for (const type of ["session_created", "status", "approval_requested", "approval_decided", "run_finished", "queue"]) {
    handlers[type] = refresh;
  }
  // Events missed while the stream was down are not replayed here, so reload the list when it comes back; and when it
  // drops, refresh once so a server that is really gone shows as a stale list rather than a quiet one.
  let streamState = "";
  const onState = (next) => {
    if (streamState && (next === "live" || streamState === "live")) refresh();
    streamState = next;
  };
  onLeave(openStream(() => agentHarnessWeb.url("/events", ownerSurface()), handlers,
    { authorized: !(isGuest() && !agentHarnessWeb.token), onState }));
  onLeave(() => { clearTimeout(timer); stale.stop(); });
  const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
  document.addEventListener("visibilitychange", onVisible);
  onLeave(() => document.removeEventListener("visibilitychange", onVisible));
}

return { viewList };
}
