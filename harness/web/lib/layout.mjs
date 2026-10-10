// Page layout rules: the session jump buttons (pure) and the desktop split view (#563). Importing this module touches
// nothing; mountSplitView() receives the shell elements and browser globals as arguments.
import { validId } from "./stream.mjs";

// Jump-button visibility rules. Pure: no DOM.
// 0.75*innerHeight on a tall desktop window is often larger than the whole
// overflow, so both arrows stay hidden unless the transcript is >1.75 viewports.
export function sessionJumpHidden(y, viewH, pageH) {
  const vh = Math.max(1, Number(viewH) || 0);
  const far = Math.min(160, 0.75 * vh);
  return { top: y <= far, bottom: pageH - vh - y <= far, far };
}

// ---------- split view (#563) ----------
// From SPLIT_MIN_WIDTH up, a list route and its detail routes render side by side: the list in a pane fixed beside the
// rail with its own scroll, the detail in <main> as on every other route (so it keeps the window's scroll, the composer
// and the jump buttons). The URL stays the source of truth: the hash says which row is open, and moving between rows of
// the same split re-renders only the detail. Below the breakpoint nothing changes: the list and the detail are separate
// pages with Back.
//
// Adding a split (Jobs, Settings):
//   1. Add an entry to SPLITS: `key`, `list` (the router's view name), `match(parts)` and the `empty` text.
//   2. Let that list view take an optional pane: `viewX(pane)`. With a pane it renders into `pane.body`, registers its
//      teardown with `pane.onLeave` (run when the split closes, not on every row change), puts its title and New action
//      in `pane.header(title, action)` instead of the bar, gives each row `data-split-key="<the key match() returns>"`,
//      and calls `pane.paint()` after it re-renders rows so the open row stays highlighted.
//   3. Style the open row with `#split-list [aria-current="page"]` (or a narrower selector) in that page's CSS section.
export const SPLIT_MIN_WIDTH = 1280;

// match(parts) returns undefined when the route is not part of the split, null for the list route (nothing open) and the
// open row's key for a detail route.
export const SPLITS = [
  {
    key: "agents",
    list: "viewList",
    label: "Agents list",
    match: (parts) => {
      if (parts[0] === "agents" && parts.length === 1) return null;
      if (parts[0] === "s" && validId(parts[1])) return parts[1];
      return undefined;
    },
    empty: { title: "No session open", text: "Pick one from the list, or start a new task." },
  },
];

// The split a route belongs to and its open row, or null.
export function splitRoute(parts, splits = SPLITS) {
  for (const split of splits) {
    const selected = split.match(parts);
    if (selected !== undefined) return { split, selected };
  }
  return null;
}

// A key press toggles the list only when it cannot be typing or a browser/assistive-technology chord.
export function isListToggleKey(e) {
  if (e.key !== "[" || e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return false;
  const t = e.target;
  const tag = String(t?.tagName || "").toUpperCase();
  return !(t?.isContentEditable || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT");
}

const SIDEBAR_SVG = '<svg class="tab-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg>';
const AGENT_SVG = '<svg class="tab-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="4" y="7" width="16" height="12" rx="3"/><circle cx="9" cy="13" r="1.5"/><circle cx="15" cy="13" r="1.5"/><path d="M12 7V4"/></svg>';

// The router calls sync(parts) on every route and gets back the active split ({ split, selected, pane }) or null; it
// renders the list once per open with renderList(view) and, when nothing is open, the detail's empty state with empty().
// `onChange` runs when the window crosses the breakpoint. `onDaemonChange` (the app-wide stream's event hook) lets a
// list that failed to load retry when the server is heard from again.
export function mountSplitView({ els, h, fill, browser, onChange, onDaemonChange = null }) {
  const { $app, $back } = els;
  const { window, document } = browser;
  const query = window.matchMedia?.(`(min-width: ${SPLIT_MIN_WIDTH}px)`) || null;
  let $pane = null;
  let $toggle = null;
  let current = null;  // { split, selected, pane, cleanup, closed, rendered, loading }
  let collapsed = false;

  const wide = () => !!query?.matches;
  query?.addEventListener?.("change", () => onChange());

  function ensureElements() {
    if ($pane) return;
    $pane = document.createElement("section");
    $pane.id = "split-list";
    $app.parentNode.insertBefore($pane, $app);
    // The pane scrolls on its own: its wheel events must not reach the window, where an open transcript reads an upward
    // wheel as the reader leaving the bottom and stops following new output.
    $pane.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
    // A toggle keeps one name; aria-pressed says whether the list is hidden.
    $toggle = h("button", { id: "split-toggle", class: "icon split-toggle", type: "button", "aria-controls": "split-list",
      "aria-label": "Hide the list", "aria-pressed": "false", title: "Hide the list ( [ )", html: SIDEBAR_SVG,
      onclick: () => setCollapsed(!collapsed) });
    $back.after($toggle);
  }

  function paintBody() {
    const { body } = document;
    const open = !!current;
    body.classList.toggle("split", open);
    body.classList.toggle("split-open", open && current.selected !== null);
    // Nothing open: hiding the list would leave only the empty state, so the list shows and the toggle hides.
    body.classList.toggle("split-collapsed", open && collapsed && current.selected !== null);
    $toggle?.setAttribute("aria-pressed", body.classList.contains("split-collapsed") ? "true" : "false");
  }

  function setCollapsed(next) {
    collapsed = next;
    if (collapsed && $pane?.contains(document.activeElement)) $toggle.focus();
    paintBody();
  }

  // Marks the open row; with `reveal`, scrolls the list so it is in view (a deep link, or the hash changed elsewhere).
  function mark(reveal = false) {
    if (!current?.pane) return;
    for (const row of current.pane.body.querySelectorAll("[data-split-key]")) {
      const on = row.dataset.splitKey === current.selected;
      if (on) {
        row.setAttribute("aria-current", "page");
        if (reveal) row.scrollIntoView?.({ block: "nearest" });
      } else row.removeAttribute("aria-current");
    }
  }

  const runCleanup = (state) => {
    const fns = state.cleanup;
    state.cleanup = [];
    fns.forEach((fn) => { try { fn(); } catch (_) { /* ignore */ } });
  };

  function close() {
    if (!current) return;
    const state = current;
    state.closed = true;
    current = null;
    runCleanup(state);
    if ($pane) fill($pane);
    paintBody();
  }

  function open(split, selected) {
    ensureElements();
    const state = { split, selected, cleanup: [], closed: false, rendered: false, loading: false };
    const body = h("div", { class: "split-body" });
    const head = h("header", { class: "split-head" });
    state.pane = {
      body,
      selected: () => state.selected,
      // A list that finishes rendering after the split closed tears down at once.
      onLeave: (fn) => { if (state.closed) fn(); else state.cleanup.push(fn); },
      // `action` is { href, label } or null (guests start nothing).
      header: (title, action = null) => fill(head, h("h2", {}, title),
        action ? h("a", { class: "btn primary list-new", href: action.href }, action.label) : null),
      paint: () => { if (current === state) mark(); },
    };
    $pane.setAttribute("aria-label", split.label);
    fill($pane, head, body);
    current = state;
  }

  function sync(parts) {
    const found = wide() ? splitRoute(parts) : null;
    if (!found) { close(); return null; }
    if (current?.split !== found.split) { close(); open(found.split, found.selected); }
    const changed = current.selected !== found.selected;
    current.selected = found.selected;
    paintBody();
    if (changed) mark(true);
    return current;
  }

  // Renders the split's list into its pane once per open; the router passes the view (it may still be fetching). A load
  // that fails is not counted: the pane says why with Retry, and the next route in the split or the next event from the
  // server tries again.
  async function renderList(view) {
    const state = current;
    if (!state || state.rendered || state.loading) return;
    state.loading = true;
    fill(state.pane.body);
    try {
      await view(state.pane);
      state.rendered = true;
    } catch (e) {
      if (!state.closed) failed(state, view, e);
    } finally {
      state.loading = false;
    }
    if (current === state) mark(true);
  }

  function failed(state, view, e) {
    runCleanup(state);  // whatever the half-built list registered
    const retry = () => { if (current === state) void renderList(view); };
    fill(state.pane.body, h("p", { class: "note bad" }, e.message),
      h("button", { class: "btn", type: "button", onclick: retry }, "Retry"));
    const stop = onDaemonChange?.(() => { stop?.(); retry(); });
    if (stop) state.cleanup.push(stop);
  }

  function empty() {
    const { empty: text } = current.split;
    fill($app, h("section", { class: "split-empty" },
      h("span", { class: "split-empty-icon", html: AGENT_SVG }),
      h("h2", {}, text.title),
      h("p", {}, text.text)));
  }

  document.addEventListener("keydown", (e) => {
    if (!current || current.selected === null || !isListToggleKey(e) || document.querySelector("dialog[open]")) return;
    e.preventDefault();
    setCollapsed(!collapsed);
  });

  return { sync, close, renderList, empty, wide };
}
