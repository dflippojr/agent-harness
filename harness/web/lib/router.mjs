// Hash routing (#258): the pure route rules (what is top level, where a blocked role lands instead) and mountRouter(), which
// owns go() and route(). mountRouter() receives the shell elements, the page views and the browser globals as arguments,
// so importing this module touches nothing and works under plain Node. app.js only registers the views.
import { h, fill, append } from "./dom.mjs";
import { validId } from "./stream.mjs";
import { GOOGLE_FAILED } from "./signin.mjs";
import { mountSplitView } from "./layout.mjs";

export const hashParts = (hash) => hash.replace(/^#\/?/, "").split("/").filter(Boolean);
// The tab bar's sections (#506). Settings and its pages, Actions included, are nested under the gear and show Back.
export const isTopLevel = (parts) => parts.length === 0 || parts[0] === "chat"
  || (parts.length === 1 && ["agents", "jobs", "images", "profile"].includes(parts[0]));

export const normalizeHash = (hash) => {
  if (!hash || hash === "#" || hash === "#/") return "#/";
  return hash.startsWith("#") ? hash : `#/${hash}`;
};

export const isProfileRoute = (parts) => parts[0] === "profile" || parts[0] === "settings";
const MEMBER_HIDDEN_PROFILE = new Set(["notifications", "apps", "endpoint", "memory", "remote-control", "backends", "disk", "accounts"]);

// Where a guest or member who asked for a page they may not see should land instead (null = allowed).
export function blockedRedirect(parts, role) {
  const guestBlocked = role === "guest" && (
    parts[0] === "new" || (parts[0] === "jobs" && parts[1] === "new")
    || (isProfileRoute(parts) && ["notifications", "apps", "endpoint"].includes(parts[1])));
  if (guestBlocked) return parts[0] === "jobs" ? "#/jobs" : "#/profile";
  const memberBlocked = role === "member" && (
    parts[0] === "jobs" || parts[0] === "images"
    || (isProfileRoute(parts) && MEMBER_HIDDEN_PROFILE.has(parts[1])));
  if (!memberBlocked) return null;
  return isProfileRoute(parts) ? "#/profile" : "#/agents";
}

// `views` is a function returning the page views, read at route time because the pages are mounted after the router.
export function mountRouter({ els, session, chrome, tabs, signin, stream, views, toast, browser }) {
  const { $app, $back } = els;
  const { document, window } = browser;
  const { api, fetchMe, loadWebAuth, isGuest, isMember, isOwner, canChat } = session;
  let cleanup = [];
  const onLeave = (fn) => cleanup.push(fn);
  // Split views (#563, lib/layout.mjs): at 1280 px+ a list stays mounted beside its detail routes.
  const split = mountSplitView({ els, h, fill, browser, onChange: () => route() });

  function go(hash, replace = false) {
    if (session.isBlocked()) return;
    const url = normalizeHash(hash);
    const cur = browser.location.hash || "#/";
    const same = url === cur || (url === "#/" && (cur === "" || cur === "#" || cur === "#/"));
    if (same) return;
    if (replace) browser.location.replace(url);
    else browser.location.hash = url;
  }

  async function routeImages(v, parts) {
    if (parts[1] && !validId(parts[1])) go("#/images", true);
    else if (parts[1] && parts[2] === "edit") await v.viewImageEdit(parts[1]);
    else if (parts[1] && parts[2] === "full") await v.viewImageFull(parts[1]);
    else if (parts[1]) await v.viewImage(parts[1]);
    else await v.viewImages();
  }

  async function routeProfile(v, parts) {
    // Bookmarks from when these lived under Profile. Non-owners never land on Actions.
    if (!["accounts", "disk", "remote-control"].includes(parts[1])) {
      await v.viewProfile(parts[1], parts[2]);
    } else if (!isOwner()) go("#/profile", true);
    else go(`#/actions/${parts[1]}`, true);
  }

  async function routeActions(v, parts) {
    if (!isOwner()) go(isMember() ? "#/agents" : "#/profile", true);
    else await v.viewActions(parts[1]);
  }

  async function routeView(parts, open) {
    const v = views();
    if (open) {
      void split.renderList(v[open.split.list]);
      if (open.selected === null) {
        chrome.setHeader("", "");  // the list pane carries the section title
        split.empty();
        return;
      }
    }
    if (parts.length === 0) go(canChat() ? "#/chat" : "#/agents", true);
    else if (parts[0] === "chat") await v.viewChat(parts[1]);
    else if (parts[0] === "agents") await v.viewList();
    else if (parts[0] === "new") await v.viewNew();
    else if (parts[0] === "actions") await routeActions(v, parts);
    else if (isProfileRoute(parts)) await routeProfile(v, parts);
    else if (parts[0] === "images") await routeImages(v, parts);
    else if (parts[0] === "jobs") await (parts[1] ? v.viewJob(parts[1]) : v.viewJobs());
    else if (parts[0] === "tasks") go(["#", "jobs", ...parts.slice(1)].join("/"), true); // scheduled work was once labelled Tasks
    else if (parts[0] === "s" && parts[1]) await v.viewSession(parts[1], parts[2] || "transcript", parts[3]);
    else if (parts[0] === "signin" && parts[1] === "failed") {
      toast(GOOGLE_FAILED, 6000);
      go(isMember() ? "#/profile/account" : "#/", true);
    }
    else go("#/", true);
  }

  // Offline with no cached identity (#368): keep the navigation and say so, rather than falling back to guest mode.
  // Retry re-runs route(), which asks /me again.
  function viewOffline() {
    $back.hidden = true;
    tabs.paint(hashParts(browser.location.hash), { show: true });
    chrome.setHeader("agents", "Offline", { page: true });
    append($app, h("div", { class: "card" },
      h("h2", {}, "Can't reach Agent Harness Server"),
      h("p", {}, "Check your connection, Tailscale and Connection settings. The app reconnects when you're back online."),
      h("button", { class: "btn primary", onclick: () => route() }, "Retry"),
      " ",
      h("a", { class: "btn", href: "#/profile/connection" }, "Connection settings")));
    chrome.repaintPage();
  }

  async function route() {
    if (session.isBlocked()) return;
    stream.watchDaemonConnection();
    cleanup.forEach((fn) => { try { fn(); } catch (_) { /* ignore */ } });
    cleanup = [];
    fill($app);
    document.querySelector(".composer")?.remove();
    chrome.hideListAction();
    document.querySelectorAll(".jump").forEach((el) => el.remove());
    const prefetched = session.takeBootMe();
    const [, me] = await Promise.all([loadWebAuth(), prefetched || fetchMe()]);
    session.setMe(me);
    chrome.paintGuestChrome();
    const parts = hashParts(browser.location.hash);
    if (session.needsSignIn()) {
      split.close();
      $back.hidden = true;
      tabs.paint(parts, { hidden: true });
      signin.viewSignIn(parts[0] === "signin" && parts[1] === "failed");
      chrome.repaintPage();
      return;
    }
    // Profile stays reachable so Connection settings can be fixed while offline.
    if (session.getMe().role === "offline" && !isProfileRoute(parts)) {
      split.close();
      viewOffline();
      return;
    }
    const images = parts[0] === "images";
    if (!isGuest() && !images && route.onImages && route.imageWarmupStarted) {
      route.imageWarmupStarted = false;
      route.imageWarmupPromise = null;
      api("/images/cooldown", { method: "POST" }).catch(() => {});
    }
    route.onImages = images;
    const redirect = blockedRedirect(parts, session.getMe().role);
    const open = redirect ? null : split.sync(parts);
    $back.hidden = !!open || isTopLevel(parts);  // beside its list, a detail needs no Back
    tabs.paint(parts);
    if (redirect) { go(redirect, true); return; }
    try {
      await routeView(parts, open);
    } catch (e) {
      append($app, h("p", { class: "note bad" }, e.message),
        h("a", { class: "btn", href: "#/profile/connection" }, "Connection settings"));
    }
    chrome.repaintPage();
  }

  $back.addEventListener("click", () => {
    if (session.isBlocked()) return;
    const parts = hashParts(browser.location.hash);
    // Session Transcript/Changes/Info are tabs (replaceState), so Back always leaves the session.
    // An approval deep-link is a real subpage of the transcript.
    if (parts[0] === "s" && parts[2] === "approval") go(`#/s/${parts[1]}`, true);
    else browser.history.back();
  });
  window.addEventListener("hashchange", route);
  // Connectivity is back: re-run the identity check if the last one could not reach the server (#368).
  window.addEventListener("online", () => { if (session.isOffline()) void route(); });

  return { go, route, onLeave };
}
