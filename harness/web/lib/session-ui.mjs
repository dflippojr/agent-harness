// Session chrome helpers (#258): the rename-in-place title, the scroll measurements and the jump buttons that the session
// page uses and app.js's repaint hook reuses. Nothing here touches document/window at module top level, so it imports
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

export function mountSessionUi({ h, api, setHeader, toast, isGuest, onLeave, layoutBar, browser }) {
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
    if (isActive()) setHeader("agents", session.title || "Session");
  } catch (e) { toast(e.message); }
}

function sessionTitle(session, isActive) {
  if (isGuest()) return h("h2", { class: "session-title" }, session.title);
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
      if (commit) await commitSessionTitle(session, input.value, isActive);
      label.textContent = session.title;
      if (input.isConnected) input.replaceWith(label);
      layoutBar();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); void finish(true); }
      if (e.key === "Escape") { e.preventDefault(); void finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  };
  label.addEventListener("click", startEdit);
  return label;
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

return { sessionTitle, bindSessionJumps };
}
