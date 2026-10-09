// The app's persistent chrome (#258): header bar, floating action button, toast, connection chip, guest banner and the
// repaint hooks for the installed iOS app. mountChrome() takes the shell elements and browser globals as arguments and
// registers the window/document listeners when called, so importing this module touches nothing and works under plain Node.
import { pageMetrics, scrollPage } from "./session-ui.mjs";

const SECTION_TITLES = { chat: "Chat", agents: "Agents", jobs: "Jobs", images: "Images" };
const CONN_LABEL = { live: "Live", reconnecting: "Reconnecting", offline: "Offline" };
const CONN_TITLE = {
  live: "Live connection to Agent Harness Server",
  reconnecting: "Lost the live connection to Agent Harness Server; reconnecting",
  offline: "Can't reach Agent Harness Server; still retrying",
};

export function mountChrome({ els, browser, session }) {
  const { $app, $title, $back, $conn, $fabHost, $fab } = els;
  const { window, document } = browser;
  const { isGuest, isMember } = session;
  let barPaintFrame = 0;
  let barPaintPhase = false;
  let pagePaintFrame = 0;
  let pagePaintPhase = false;

  function layoutBar() {
    const bar = document.getElementById("bar");
    const chrome = document.querySelector(".session-chrome");
    if (bar) document.documentElement.style.setProperty("--bar-h", `${bar.offsetHeight}px`);
    document.documentElement.style.setProperty("--session-chrome-h", `${chrome ? chrome.offsetHeight : 0}px`);
  }

  function repaintBar() {
    cancelAnimationFrame(barPaintFrame);
    barPaintFrame = requestAnimationFrame(() => {
      const bar = document.getElementById("bar");
      barPaintPhase = !barPaintPhase;
      bar.classList.toggle("paint-refresh", barPaintPhase);
      layoutBar();
    });
  }

  // #81: after back navigation the installed iOS app can leave the upper part of the page unpainted until a scroll
  // invalidates it. Like repaintBar(), alternate a sub-pixel transform on the page content (and re-clamp the scroll
  // position, since a long subpage's restored offset can exceed the shorter page's height) once a route has rendered.
  function repaintPage() {
    cancelAnimationFrame(pagePaintFrame);
    pagePaintFrame = requestAnimationFrame(() => {
      pagePaintPhase = !pagePaintPhase;
      $app.classList.toggle("paint-refresh", pagePaintPhase);
      const { y, viewH, pageH } = pageMetrics(browser); // the cross-engine measurements the jump buttons use
      if (y > pageH - viewH) scrollPage(pageH - viewH, browser);
    });
  }

  // A section's own screen (no Back) gets the large title; until a page knows its title (a chat still loading) the
  // section's name stands in.
  function setHeader(feature, pageTitle = "", { page = false } = {}) {
    const top = $back.hidden;
    const title = pageTitle || (top ? SECTION_TITLES[feature] || "" : "");
    $title.textContent = title;
    $title.hidden = !title;
    const bar = document.getElementById("bar");
    bar.classList.toggle("page", page);
    bar.classList.toggle("top", top);
    repaintBar();
  }

  function showFab(href, label) {
    if (isGuest()) return;
    $fab.href = href;
    $fab.textContent = label;
    $fabHost.hidden = false;
  }

  // `action` ({ label, onClick }) adds a button such as Undo (#511); tapping it runs onClick and dismisses the toast.
  function toast(text, ms = 2600, action = null) {
    const t = document.getElementById("toast");
    t.textContent = text;
    t.classList.toggle("has-action", !!action);
    if (action) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "toast-action";
      btn.textContent = action.label;
      btn.addEventListener("click", () => {
        clearTimeout(toast.timer);
        t.hidden = true;
        action.onClick();
      });
      t.append(" ", btn);
    }
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, ms);
  }

  // The header's connection chip (#510): Live, Reconnecting or Offline, in words rather than a bare 8 px dot. It stays
  // hidden until the daemon stream first reports, so boot never flashes a false state. The "live" class is what other
  // pages read.
  function setConnState(state) {
    const label = CONN_LABEL[state] || CONN_LABEL.offline;
    $conn.hidden = false;
    $conn.dataset.state = state in CONN_LABEL ? state : "offline";
    $conn.classList.toggle("live", state === "live");
    $conn.textContent = label;
    $conn.title = CONN_TITLE[state] || CONN_TITLE.offline;
  }

  function paintGuestChrome() {
    const banner = document.getElementById("guest-banner");
    const guest = isGuest();
    const member = isMember();
    document.documentElement.classList.toggle("guest", guest);
    document.documentElement.classList.toggle("member", member);
    if (!banner) return;
    if (!guest) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    const until = session.getMe().guest_until;
    const when = until ? new Date(until) : null;
    const ends = when && !Number.isNaN(when.getTime())
      ? ` Ends ${when.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}.`
      : "";
    banner.textContent = `Demo access — look around only.${ends}`;
    banner.hidden = false;
  }

  window.addEventListener("resize", repaintBar);
  window.addEventListener("orientationchange", repaintBar);
  window.addEventListener("pageshow", repaintBar);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) repaintBar(); });
  window.addEventListener("pageshow", repaintPage);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) repaintPage(); });

  return { layoutBar, repaintBar, repaintPage, setHeader, showFab, toast, setConnState, paintGuestChrome };
}
