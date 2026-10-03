// Navigation drawer (#258): which section is current (pure) and mountDrawer(), which wires the menu button, scrim, focus
// trap and the recent-chats list. mountDrawer() receives the shell elements and browser globals as arguments, so importing
// this module touches nothing and works under plain Node.
import { h, fill } from "./dom.mjs";
import { hashParts } from "./router.mjs";

export function currentSection(parts) {
  const first = parts[0] || "";
  if (first === "s" || first === "new") return "agents";
  if (first === "actions") return "actions";
  return first;
}

export function mountDrawer({ els, session, browser }) {
  const { $menu, $drawer, $scrim, $drawerChats, $profileIcon } = els;
  const { document, window } = browser;
  const { api, canChat, isMember, isOwner } = session;
  let drawerReturnFocus = null;
  let drawerChatsCache = null;
  const parts = () => hashParts(browser.location.hash);

  function drawerFocusable() {
    return [...$drawer.querySelectorAll("a[href], button")].filter((el) => !el.hidden && !el.closest("[hidden]"));
  }

  async function refreshDrawerChats() {
    const recent = $drawer.querySelector(".drawer-recent");
    if (!canChat()) { fill($drawerChats); recent.hidden = true; return; }
    recent.hidden = false;
    const render = (chats) => {
      const active = parts()[0] === "chat" ? parts()[1] : "";
      fill($drawerChats, chats.length ? chats.map((c) => h("a", {
        href: `#/chat/${c.id}`, class: c.id === active ? "on" : "", title: c.title,
        "aria-current": c.id === active ? "page" : false,
      }, c.title)) : h("p", { class: "muted small" }, "No chats yet."));
    };
    // Show the last known list immediately, then revalidate in the background (#152).
    if (drawerChatsCache) render(drawerChatsCache);
    let chats;
    try { chats = await api("/chats?limit=30"); } catch (_) { return; } // offline: keep what is shown
    drawerChatsCache = chats;
    render(chats);
  }

  function openDrawer() {
    if (session.isBlocked() || !$drawer.hidden) return;
    drawerReturnFocus = document.activeElement;
    const section = currentSection(parts());
    $drawer.querySelectorAll("a[data-nav]").forEach((a) => {
      const nav = a.dataset.nav;
      a.hidden = (nav === "chat" && !canChat()) || (isMember() && (nav === "jobs" || nav === "images"))
        || (nav === "actions" && !isOwner());
      const on = nav === "actions" ? section === "actions" : nav === section;
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    document.getElementById("drawer-profile-icon").textContent = $profileIcon.textContent || "🙂";
    $drawer.hidden = false;
    $scrim.hidden = false;
    $menu.setAttribute("aria-expanded", "true");
    document.body.classList.add("drawer-open");
    void refreshDrawerChats();
    drawerFocusable()[0]?.focus();
  }

  function closeDrawer({ restoreFocus = true } = {}) {
    if ($drawer.hidden) return;
    $drawer.hidden = true;
    $scrim.hidden = true;
    $menu.setAttribute("aria-expanded", "false");
    document.body.classList.remove("drawer-open");
    if (restoreFocus) (drawerReturnFocus && document.contains(drawerReturnFocus) ? drawerReturnFocus : $menu).focus?.();
    drawerReturnFocus = null;
  }

  $menu.addEventListener("click", () => ($drawer.hidden ? openDrawer() : closeDrawer()));
  $scrim.addEventListener("click", () => closeDrawer());
  $drawer.addEventListener("click", (event) => {
    // Choosing the page we are already on does not fire hashchange, so close here as well.
    if (event.target.closest("a[href]")) closeDrawer({ restoreFocus: false });
  });
  document.addEventListener("keydown", (event) => {
    if ($drawer.hidden) return;
    if (event.key === "Escape") { event.preventDefault(); closeDrawer(); return; }
    if (event.key !== "Tab") return;
    const items = drawerFocusable();
    if (!items.length) return;
    const first = items[0];
    const last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  window.addEventListener("hashchange", () => closeDrawer({ restoreFocus: false }));

  return { openDrawer, closeDrawer };
}
