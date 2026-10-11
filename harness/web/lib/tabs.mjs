// @ts-nocheck
// Bottom tab bar and Settings gear (#506), replacing the navigation drawer: which tab a route belongs to and when the bar
// shows (pure), and mountTabs(), whose paint() the router calls after each route. The tabs are plain links in index.html;
// on wide screens style.css turns the same bar into a left rail. Importing this module touches nothing.
import { isTopLevel, isProfileRoute } from "./router.mjs";

const SECTIONS = new Set(["chat", "agents", "jobs", "images"]);

export function currentTab(parts) {
  const first = parts[0] || "";
  if (first === "s" || first === "new") return "agents";
  if (first === "actions" || isProfileRoute(parts)) return "profile"; // Actions are reached from Settings
  return SECTIONS.has(first) ? first : "";
}

// Phone detail/editor pages have Back and their own bottom controls; desktop keeps the rail.
export const tabBarHidden = (parts, desktop = false) => !desktop && !(isTopLevel(parts) || isProfileRoute(parts) || parts[0] === "actions");

// The same role rules the drawer had: Chat needs chat access, and household members have no Jobs or Images.
export const tabHidden = (tab, { canChat, member }) => (tab === "chat" && !canChat) || (member && (tab === "jobs" || tab === "images"));

export function mountTabs({ els, session, browser, chrome, stream }) {
  const { $tabBar, $settings } = els;
  let lastParts = [];
  let lastOptions = { hidden: true };

  // `show` keeps the bar on a page that is not a section (the offline card); `hidden` drops it (sign-in, blocked).
  function paint(parts, { show = false, hidden = false } = {}) {
    lastParts = parts;
    lastOptions = { show, hidden };
    const desktop = browser.window.innerWidth >= 768;
    const off = hidden || (!show && tabBarHidden(parts, desktop));
    $tabBar.hidden = off;
    browser.document.body.classList.toggle("has-tabs", !off);
    $settings.hidden = hidden || !(show || isTopLevel(parts));
    const tab = currentTab(parts);
    const roles = { canChat: session.canChat(), member: session.isMember() };
    for (const a of $tabBar.querySelectorAll("a[data-tab]")) {
      a.hidden = tabHidden(a.dataset.tab, roles);
      if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    chrome?.watchNeedsYou?.(desktop && !off, stream);
  }

  // Resizing on a detail page changes the navigation without reloading the page or its editor.
  browser.window.addEventListener("resize", () => paint(lastParts, lastOptions));

  return { paint };
}
