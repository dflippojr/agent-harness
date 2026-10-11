// Keyboard shortcuts (#571): the shortcut set from #540 and the `?` sheet that lists it. keyAction() is pure: it reads one
// keydown and says what it asks for. mountKeys() receives the browser globals and the router's go(), so importing this
// module touches nothing. Shortcuts never fire while focus is in a field, a sheet or a menu, and Approve and Deny have no
// key: an approval is answered by Tab and Enter, like any other button.
import { h } from "./dom.mjs";
import { hashParts } from "./router.mjs";
import { panelSheet, dismissSheet } from "./sheet.mjs";

// `G` then one of these switches section.
export const GO_KEYS = { a: "#/agents", j: "#/jobs", i: "#/images", s: "#/settings" };
// How long `G` waits for its second key.
export const CHORD_MS = 1500;

// The `?` sheet, in its two columns. `hide(role)` drops a row the person can't use.
export const SHORTCUTS = [
  { title: "Anywhere", rows: [
    { label: "Search the list", keys: ["/"] },
    { label: "New task (Agents) · New job (Jobs)", keys: ["N"], hide: (role) => role === "guest" },
    { label: "Go to Agents", keys: ["G", "A"] },
    { label: "Go to Jobs", keys: ["G", "J"], hide: (role) => role === "member" },
    { label: "Go to Images", keys: ["G", "I"], hide: (role) => role === "member" },
    { label: "Go to Settings", keys: ["G", "S"] },
    { label: "This list", keys: ["?"] },
  ] },
  { title: "Lists and sessions", rows: [
    { label: "Next row", keys: ["J"] },
    { label: "Previous row", keys: ["K"] },
    { label: "Open", keys: ["Enter"] },
    { label: "Hide or show the list", keys: ["["] },
    { label: "Close a sheet or menu", keys: ["Esc"] },
    { label: "Send a message", keys: ["Ctrl", "Enter"] },
  ] },
];

// Inputs that take no typing (a switch, a radio, a button) leave shortcuts on.
const NOT_TYPED = new Set(["button", "checkbox", "color", "file", "image", "radio", "range", "reset", "submit"]);

// True when keys pressed on `target` are typing: a text field, a text area, a select or editable content.
export function isTypingTarget(target) {
  if (!target) return false;
  if (target.isContentEditable) return true;
  const tag = String(target.tagName || "").toUpperCase();
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  return !NOT_TYPED.has(String(target.type || "text").toLowerCase());
}

// What one keydown asks for, or null. `chord` is true while `G` waits for its second key. Actions:
//   { action: "help" | "search" | "new" | "next" | "prev" | "chord" | "cancel" } or { action: "go", hash }.
// A modified key (Ctrl, Alt, Meta) is the browser's or assistive technology's; Shift only makes `?`.
export function keyAction(e, { chord = false } = {}) {
  if (!e || e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return null;
  if (isTypingTarget(e.target)) return null;
  const key = String(e.key || "");
  if (chord) {
    const hash = GO_KEYS[key.toLowerCase()];
    return hash && !e.shiftKey ? { action: "go", hash } : { action: "cancel" };
  }
  if (key === "?") return { action: "help" };
  // Held down, only J and K repeat.
  if (e.shiftKey || (e.repeat && !["j", "k"].includes(key.toLowerCase()))) return null;
  switch (key.toLowerCase()) {
    case "/": return { action: "search" };
    case "n": return { action: "new" };
    case "g": return { action: "chord" };
    case "j": return { action: "next" };
    case "k": return { action: "prev" };
    default: return null;
  }
}

// Where `N` goes when the page shows no New action of its own (a session or job opened full width), or null.
export function newHash(parts, role) {
  if (role === "guest") return null;
  if (parts[0] === "agents" || parts[0] === "s") return "#/new";
  if (parts[0] === "jobs" && role !== "member") return "#/jobs/new";
  return null;
}

// The next row for J (step 1) or K (step -1). `at` is the focused row's index, `open` the open row's (aria-current), each -1
// when there is none. With neither, both keys start at the first row; the ends hold rather than wrap.
export function stepRow(count, at, open, step) {
  if (count <= 0) return -1;
  const from = at >= 0 ? at : open;
  if (from < 0) return 0;
  return Math.min(count - 1, Math.max(0, from + step));
}

// The rows J and K move through: session rows and search results, job rows, image cards and Settings rows.
export const ROW_SELECTOR = "a.agent-row, a.card[data-split-key], a.job-main, a.image-card, a.set-row";

const KEYBOARD_SVG = '<svg class="tab-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg>';

const keyCaps = (keys) => h("span", { class: "keys-caps" }, keys.map((k) => h("kbd", {}, k)));

// The `?` sheet's body for `role`.
export function shortcutsBody(role) {
  return [
    h("div", { class: "keys-groups" }, SHORTCUTS.map((group) => h("section", { class: "keys-group", "aria-label": group.title },
      h("h3", { class: "keys-group-title" }, group.title),
      h("dl", { class: "keys-list" }, group.rows.filter((row) => !row.hide?.(role)).map((row) =>
        h("div", { class: "keys-row" }, h("dt", {}, row.label), h("dd", {}, keyCaps(row.keys)))))))),
    h("p", { class: "keys-hint" },
      "Shortcuts never fire while you type in a field. Approve and Deny have no single-key shortcut: Tab to them and press Enter."),
  ];
}

export function mountKeys({ browser, go, role }) {
  const { document, window } = browser;
  let chordTimer = null;
  let chord = false;
  let helpOpen = false;

  const endChord = () => { chord = false; window.clearTimeout(chordTimer); };
  // Hidden by CSS, a closed ancestor or the `hidden` attribute: nothing to focus or press.
  const shown = (el) => !!el && el.getClientRects().length > 0;
  // A sheet or a menu is answering its own keys (Escape, arrows); the page behind it waits.
  const busy = () => !!document.querySelector("dialog[open], [role=menu]:not([hidden])");

  async function help() {
    if (helpOpen) { dismissSheet(); return; }
    helpOpen = true;
    try {
      await panelSheet({ title: "Keyboard shortcuts", icon: KEYBOARD_SVG, body: shortcutsBody(role()), className: "keys-sheet" });
    } finally {
      helpOpen = false;
    }
  }

  function search() {
    const field = [...document.querySelectorAll("input[type=search]")].find(shown);
    if (!field) return false;
    field.focus();
    field.select?.();
    return true;
  }

  // The page's own New action wherever it shows (the list header, the list pane or the phone's button), else the section's.
  function create() {
    const action = [...document.querySelectorAll("#split-list .list-new, #bar .list-new, #fab")].find(shown);
    if (action) { action.click(); return true; }
    const hash = newHash(hashParts(browser.location.hash), role());
    if (hash) go(hash);
    return !!hash;
  }

  // J and K move through the list beside an open session when there is one, else the page's.
  function move(step) {
    const pane = document.getElementById("split-list");
    const inPane = pane && shown(pane) ? [...pane.querySelectorAll(ROW_SELECTOR)].filter(shown) : [];
    const rows = inPane.length ? inPane : [...document.getElementById("app").querySelectorAll(ROW_SELECTOR)].filter(shown);
    const next = stepRow(rows.length, rows.indexOf(document.activeElement), rows.findIndex((r) => r.getAttribute("aria-current")), step);
    if (next < 0) return false;
    rows[next].focus();
    rows[next].scrollIntoView?.({ block: "nearest" });
    return true;
  }

  document.addEventListener("keydown", (e) => {
    const found = keyAction(e, { chord });
    if (!found) return;
    if (found.action === "help" && helpOpen) { e.preventDefault(); void help(); return; }
    if (busy()) { endChord(); return; }
    let handled = true;
    switch (found.action) {
      case "help": void help(); break;
      case "search": handled = search(); break;
      case "new": handled = create(); break;
      case "next": handled = move(1); break;
      case "prev": handled = move(-1); break;
      case "chord":
        chord = true;
        window.clearTimeout(chordTimer);
        chordTimer = window.setTimeout(endChord, CHORD_MS);
        break;
      case "go": endChord(); go(found.hash); break;
      default: endChord(); handled = false;  // any other key after `G` drops the chord
    }
    if (handled) e.preventDefault();
  });

  return { help };
}
