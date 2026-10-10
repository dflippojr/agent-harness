// Session list page (#258): the Agents home, with search, machine filter and live refresh. The shell (DOM builder,
// router hooks, event stream) is injected by app.js so this module imports under plain Node and never reaches into
// another page.
import { ago, gpuText } from "../lib/format.mjs";
import { TARGET_LABEL, compareTargets } from "../lib/targets.mjs";
import { escapeHtml } from "../lib/markdown.mjs";
import { approvalLine } from "../lib/tools.mjs";
import { STATUS_LABEL, staleNote } from "../lib/widgets.mjs";

import { groupSessions } from "../lib/session-groups.mjs";
export { FAILED_NEEDS_YOU_SECONDS, SESSION_GROUPS, sessionGroup, groupSessions } from "../lib/session-groups.mjs";

const LAPTOP_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="12" rx="2"/><path d="M2 19h20"/></svg>';
const CHEVRON_SVG = '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';

export function mountSessions({ $app, h, fill, append, api, setHeader, showListAction, onLeave, isMember, isGuest, badge, reviewBadge, REVIEW_LABEL,
  jobStatusBadge, onDaemonChange, onDaemonState, browser }) {
// Browser globals come in through `browser` (globalThis in the app, a stub under Node) so importing this module touches no DOM.
const { window, document, localStorage } = browser;

let searchQuery = "";  // kept while navigating, so Back from a result returns to the results
let sessionTarget = "all";
try { sessionTarget = localStorage.getItem("harness.sessionTarget") || "all"; } catch (_) { /* private mode */ }

// Search passages mark matches with U+0002 … U+0003 (control characters, matched on purpose); everything else is escaped.
const markPassage = (text) => escapeHtml(text).replaceAll("\u0002", "<mark>").replaceAll("\u0003", "</mark>");
const PASSAGE_KIND = { title: "title", message: "you", assistant: "agent", tool: "tool output", answer: "answer", context: "app context" };

// With a `pane` (#563, lib/layout.mjs) the list renders beside the open session at 1280 px+: into the pane, with its own
// header, torn down when the split closes rather than on every session change.
async function viewList(pane = null) {
  const host = pane ? pane.body : $app;
  const leave = pane ? pane.onLeave : onLeave;
  const list = h("div", { class: "agent-groups" });
  const results = h("div", { hidden: true });
  const queueNote = h("p", { class: "note" });
  const search = h("input", { type: "search", placeholder: "Search", value: searchQuery, class: "search" });
  const targetSwitch = h("div", { class: "tabs", role: "group", "aria-label": "Filter sessions by machine" });
  // #510: a failed refresh says so (when the list last updated, and why) instead of silently keeping the old list.
  const stale = staleNote({ make: h, place: (el) => targetSwitch.after(el), onRetry: () => refreshNow() });
  append(host, h("div", { class: "search-wrap" }, search), targetSwitch, queueNote, results, list);
  if (pane) pane.header("Agents", isGuest() ? null : { href: "#/new", label: "+ New task" });
  else {
    setHeader("agents", "Agents");
    showListAction("#/new", "+ New task");
  }

  let sessions = [];
  let targets = [];
  const targetName = (target) => target === "tower" ? "Tower" : TARGET_LABEL[target] || target;
  const sessionCardKey = (s) => [
    s.id, s.title, s.status, s.updated_at, s.chat_summary, s.queue_position, s.review, s.job_status,
    s.target, s.project, (s.pending_approvals || []).map((a) => a.id).join(","),
  ].join("\0");
  const dot = () => h("span", { "aria-hidden": "true" }, "·");
  // Sentence-case pill; a queued row carries its place in line ("Queued · #2").
  const statusPill = (s) => {
    const label = STATUS_LABEL[s.status] || s.status;
    const place = s.status === "queued" && s.queue_position > 0 ? ` · #${s.queue_position}` : "";
    return h("span", { class: `badge ${s.status}` }, label.charAt(0).toUpperCase() + label.slice(1) + place);
  };
  const sessionRow = (s) => {
    const approvals = s.pending_approvals || [];
    const approvalPath = approvals.length ? `/approval/${approvals[0].id}` : "";
    const ask = approvals.length ? approvalLine(approvals[0]) + (approvals.length > 1 ? ` (+${approvals.length - 1} more)` : "") : "";
    return h("a", { class: "agent-row", href: `#/s/${s.id}${approvalPath}`, "data-split-key": s.id },
      h("div", { class: "agent-body" },
        h("h3", {}, s.title),
        h("div", { class: "agent-meta" },
          statusPill(s),
          s.queue_position > 0 && s.status !== "queued" ? h("span", {}, `#${s.queue_position} in queue`) : null,
          s.review ? reviewBadge(s.review, REVIEW_LABEL[s.review] || s.review) : null,
          s.job_status ? jobStatusBadge(s.job_status) : null,
          h("span", { class: "agent-project" }, s.project),
          s.target === "tower" ? null : [dot(), h("span", { class: "agent-target", html: LAPTOP_SVG }),
            h("span", {}, TARGET_LABEL[s.target] || s.target)],
          dot(), h("span", {}, ago(s.updated_at))),
        ask ? h("p", { class: "agent-ask", title: ask }, ask) : null,
        !ask && s.chat_summary ? h("p", { class: "agent-preview" }, s.chat_summary) : null),
      h("span", { class: "agent-chev", html: CHEVRON_SVG }));
  };
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
    const groups = groupSessions(visible);
    // The group is part of the key: a failure that ages out of Needs you moves without any field changing.
    const keys = groups.flatMap((g) => g.sessions.map((s) => `${g.key}\0${sessionCardKey(s)}`)).join("\n");
    if (list.dataset.keys === keys && list.querySelector("a.agent-row")) return;
    list.dataset.keys = keys;
    fill(list, groups.map((g) => h("section", { class: `agent-group ${g.key}`, "aria-label": g.label },
      h("h2", { class: "agent-sec" }, g.label, g.key === "recent" ? null : h("span", { class: "agent-count" }, String(g.sessions.length))),
      h("div", { class: "agent-list" }, g.sessions.map(sessionRow)))));
    pane?.paint();
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
        data.results.length ? data.results.map((r) => h("a", { class: "card", href: `#/s/${r.id}`, "data-split-key": r.id },
          h("h3", {}, r.title),
          h("div", { class: "meta" }, badge(r.status), h("span", {}, r.project), h("span", {}, ago(r.created_at)),
            h("span", {}, `${r.hits} match${r.hits === 1 ? "" : "es"}`)),
          r.passages.map((p) => h("div", { class: "passage small" }, h("span", { class: "muted" }, `${PASSAGE_KIND[p.kind] || p.kind}: `),
            h("span", { html: markPassage(p.text) }))))) : h("p", { class: "empty" }, `Nothing matches “${q}”.`));
      pane?.paint();
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
  // Shown search results are part of the list too: a refresh (a rename, a status change) re-runs the query.
  const refreshNow = () => render().then(() => { if (search.value.trim()) void runSearch(); }).catch((e) => {
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
  leave(() => {
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
  // Session events come from the app-wide stream behind the header chip (session_created, status, approvals, run_finished,
  // queue), so the list beside an open session holds no second connection. It also fires when that stream is live again,
  // since events missed while it was down are not replayed; and a drop refreshes once, so a server that is really gone
  // shows as a stale list rather than a quiet one.
  leave(onDaemonChange(refresh));
  let streamState = "live";
  leave(onDaemonState((next) => {
    if (streamState === "live" && next !== "live") refresh();
    streamState = next;
  }));
  leave(() => { clearTimeout(timer); stale.stop(); });
  const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
  document.addEventListener("visibilitychange", onVisible);
  leave(() => document.removeEventListener("visibilitychange", onVisible));
}

return { viewList };
}
