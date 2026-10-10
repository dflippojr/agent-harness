// Session chrome helpers (#258): the overflow menu and its rename (#514), the scroll measurements and the jump buttons that
// the session page uses and app.js's repaint hook reuses. Nothing here touches document/window at module top level, so it imports
// under plain Node; browser globals arrive through `browser` (globalThis in the app, a stub under Node).
import { sessionJumpHidden } from "./layout.mjs";

// Window vs body vs html disagree on Edge/desktop: use every scroller's metric.
export function pageMetrics(browser) {
  const { window, document } = browser;
  const se = document.scrollingElement || document.documentElement;
  const body = document.body;
  const vv = window.visualViewport;
  const y = Math.max(
    window.scrollY || 0, window.pageYOffset || 0, se.scrollTop || 0, body ? body.scrollTop : 0);
  const viewH = Math.max(
    1, se.clientHeight || 0, window.innerHeight || 0, vv?.height ? vv.height : 0);
  const pageH = Math.max(
    se.scrollHeight || 0,
    document.documentElement.scrollHeight || 0,
    body ? body.scrollHeight : 0);
  return { y, viewH, pageH };
}

export function scrollPage(top, browser) {
  const { window, document } = browser;
  const y = Math.max(0, top);
  window.scrollTo(0, y);
  const se = document.scrollingElement || document.documentElement;
  se.scrollTop = y;
  if (document.body) document.body.scrollTop = y;
}

// The session overflow menu's entries (#514), in order. Pure: the page supplies the actions. Guests get none: every entry,
// the transcript download included, is an owner route that answers a guest with 404.
export function sessionMenuItems(session, { guest, terminal }) {
  if (guest) return [];
  const active = !terminal.has(session.status);
  return ["rename", active ? "cancel" : "rerun", (session.taint || []).length ? "clear-taint" : null, "download"].filter(Boolean);
}

export const SESSION_MENU_LABELS = {
  rename: "Rename", cancel: "Cancel task", rerun: "Run again as new session", "clear-taint": "Clear taint", download: "Download transcript",
};

// `onRenamed` tells the rest of the app (the Agents list beside the session, #563), since a rename has no server event.
export function mountSessionUi({ h, api, setHeader, toast, isGuest, onLeave, layoutBar, browser, onRenamed = () => {} }) {
const { window, document } = browser;

// Older servers answer PATCH with 405, so a rename retries once with PUT.
async function putSessionTitle(session, title) {
  const body = { title };
  try {
    return await api(`/sessions/${session.id}`, { method: "PATCH", body });
  } catch (e) {
    if (!/405|Method Not Allowed/i.test(e.message)) throw e;
    return api(`/sessions/${session.id}`, { method: "PUT", body });
  }
}

async function commitSessionTitle(session, raw, isActive) {
  const next = raw.replace(/\s+/g, " ").trim();
  if (!next || next === session.title) return;
  try {
    const updated = await putSessionTitle(session, next);
    session.title = updated.title;
    onRenamed();
    if (isActive()) setHeader("agents", session.title || "Session", { page: true });
  } catch (e) { toast(e.message); }
}

// Rename edits the header title in place: the bar's h1 hides behind an input until Enter, Escape or blur.
function renameTitle(session, isActive) {
  const title = document.getElementById("title");
  if (!title || title.hidden || document.querySelector(".session-title-edit")) return;
  const input = h("input", { class: "session-title-edit", type: "text", value: session.title, maxlength: "120", "aria-label": "Session title" });
  title.hidden = true;
  title.after(input);
  layoutBar();
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    if (commit) await commitSessionTitle(session, input.value, isActive);
    if (!isActive()) return;  // the page left mid-save: the next page's setHeader owns the shared #title now
    input.remove();
    title.hidden = false;
    layoutBar();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); void finish(true); }
    if (e.key === "Escape") { e.preventDefault(); void finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
  onLeave(() => { done = true; input.remove(); });
}

// The ⋯ button in the header bar and its menu (#514). `run(id)` runs an entry; `items()` is read on every open, so the
// entries follow the session's status and taint. The button and menu leave with the page.
function sessionMenu({ items, run }) {
  const bar = document.getElementById("bar");
  const button = h("button", { class: "icon session-menu-btn", type: "button", "aria-label": "Session menu", "aria-haspopup": "menu", "aria-expanded": "false" },
    h("span", { class: "menu-dots", "aria-hidden": "true" }));
  const menu = h("div", { class: "session-menu", role: "menu", "aria-label": "Session", hidden: true });
  const wrap = h("div", { class: "session-menu-wrap" }, button, menu);
  const close = () => {
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", onOutside, true);
  };
  const onOutside = (e) => { if (!wrap.contains(e.target)) close(); };
  const open = () => {
    menu.replaceChildren(...items().map((id) => h("button", {
      class: `session-menu-item${id === "cancel" ? " bad" : ""}`, type: "button", role: "menuitem",
      onclick: () => { close(); button.focus(); run(id); },
    }, SESSION_MENU_LABELS[id])));
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", onOutside, true);
    menu.querySelector("button")?.focus();
  };
  button.addEventListener("click", () => (menu.hidden ? open() : close()));
  menu.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); button.focus(); }
  });
  bar?.append(wrap);
  onLeave(() => { close(); wrap.remove(); });
  return { button, menu, open, close };
}

function bindSessionJumps() {
  const pageHeight = () => pageMetrics(browser).pageH;
  const jumpTop = h("button", { class: "btn small jump jump-top", type: "button", hidden: true, "aria-label": "Jump to start" }, "↑");
  const jumpBottom = h("button", { class: "btn small jump jump-bottom", type: "button", hidden: true, "aria-label": "Jump to end" }, "↓");
  const updateJumps = () => {
    layoutBar();
    const { y, viewH, pageH } = pageMetrics(browser);
    const hide = sessionJumpHidden(y, viewH, pageH);
    jumpTop.hidden = hide.top;
    jumpBottom.hidden = hide.bottom;
  };
  jumpTop.addEventListener("click", () => scrollPage(0, browser));
  jumpBottom.addEventListener("click", () => scrollPage(pageHeight(), browser));
  document.body.append(jumpTop, jumpBottom);
  window.addEventListener("scroll", updateJumps, { passive: true, capture: true });
  document.addEventListener("scroll", updateJumps, { passive: true, capture: true });
  window.addEventListener("resize", updateJumps);
  const vv = window.visualViewport;
  if (vv) {
    vv.addEventListener("resize", updateJumps);
    vv.addEventListener("scroll", updateJumps);
  }
  onLeave(() => {
    window.removeEventListener("scroll", updateJumps, true);
    document.removeEventListener("scroll", updateJumps, true);
    window.removeEventListener("resize", updateJumps);
    if (vv) {
      vv.removeEventListener("resize", updateJumps);
      vv.removeEventListener("scroll", updateJumps);
    }
    jumpTop.remove();
    jumpBottom.remove();
  });
  browser.requestAnimationFrame(updateJumps);
  return { updateJumps, pageHeight };
}

return { renameTitle, sessionMenu, bindSessionJumps };
}
