// The app's persistent chrome (#258): header bar, floating action button, toast, connection chip, guest banner and the
// repaint hooks for the installed iOS app. mountChrome() takes the shell elements and browser globals as arguments and
// registers the window/document listeners when called, so importing this module touches nothing and works under plain Node.
import { pageMetrics, scrollPage } from "./session-ui.mjs";
import { sessionGroup } from "./session-groups.mjs";

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
  let listNew = null;

  // Desktop's count uses the same Needs you rule as the Agents list, independent of list filters.
  // Reuse the connection stream; do not open another EventSource for persistent chrome.
  const needsYou = document.getElementById("agents-needs-you");
  let needsScope = "";
  let needsGeneration = 0;
  let needsTimer = null;
  let needsPoll = null;
  let stopNeedsEvents = null;
  let refreshNeedsYou = null;
  function watchNeedsYou(active, stream) {
    const me = session.getMe();
    const scope = active && !session.isBlocked() && !session.needsSignIn() && me.role !== "offline"
      ? JSON.stringify([me.role, me.id, me.login]) : "";
    if (!needsYou || scope === needsScope) return;
    needsScope = scope;
    needsGeneration++;
    clearTimeout(needsTimer);
    clearInterval(needsPoll);
    stopNeedsEvents?.();
    needsYou.hidden = true;
    refreshNeedsYou = null;
    if (!scope) return;
    const refresh = async () => {
      if (document.hidden || session.isBlocked()) return;
      const generation = ++needsGeneration;
      try {
        const sessions = await session.api("/sessions");
        if (generation !== needsGeneration) return;
        const count = sessions.filter((s) => sessionGroup(s) === "needs").length;
        needsYou.textContent = String(count);
        needsYou.setAttribute("aria-label", `${count} ${count === 1 ? "agent needs" : "agents need"} you`);
        needsYou.hidden = count === 0;
      } catch (_) {
        if (generation === needsGeneration) needsYou.hidden = true; // an unavailable count is not zero
      }
    };
    const schedule = () => {
      clearTimeout(needsTimer);
      needsTimer = setTimeout(refresh, 300);
    };
    stopNeedsEvents = stream?.onDaemonChange(schedule);
    refreshNeedsYou = refresh;
    needsPoll = setInterval(refresh, 60000); // fresh failures age out even without a daemon event
    void refresh();
  }

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
    listNew?.remove();
    listNew = null;
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
    // The list's New action shares its route and label with the phone FAB. CSS selects the presentation at 768 px,
    // so resizing needs no route refresh. setHeader removes it when leaving a list (including role changes).
    listNew?.remove();
    listNew = document.createElement("a");
    listNew.className = "btn primary list-new";
    listNew.href = href;
    listNew.textContent = label;
    $title.after(listNew);
    repaintBar();
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
  // pages read; a page that says the state in its own words (Settings' identity line, #512) follows it through
  // onConnState instead of reading it once. onConnState returns the unsubscribe.
  const connListeners = new Set();
  function setConnState(state) {
    const label = CONN_LABEL[state] || CONN_LABEL.offline;
    $conn.hidden = false;
    $conn.dataset.state = state in CONN_LABEL ? state : "offline";
    $conn.classList.toggle("live", state === "live");
    $conn.textContent = label;
    $conn.title = CONN_TITLE[state] || CONN_TITLE.offline;
    for (const fn of connListeners) fn($conn.dataset.state);
  }

  function onConnState(fn) {
    connListeners.add(fn);
    return () => connListeners.delete(fn);
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
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void refreshNeedsYou?.(); });
  window.addEventListener("pageshow", repaintPage);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) repaintPage(); });

  return { layoutBar, repaintBar, repaintPage, setHeader, showFab, toast, setConnState, onConnState, paintGuestChrome, watchNeedsYou };
}
