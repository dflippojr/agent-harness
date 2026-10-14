// @ts-nocheck
// Profile page (#258): account, appearance, notifications, install, backends, smart approvals, memory and endpoint
// cards. Daemon administration (settings, Apps and keys, skills, members) lives in the Hub and the CLI (#545). The shell (DOM builder, router, auth state) is injected by app.js so this module
// imports under plain Node and never reaches into another page.
import { ago } from "../lib/format.mjs";
import { md } from "../lib/markdown.mjs";
import * as sheets from "../lib/sheet.mjs";
import { protocolMismatch } from "../lib/compat.mjs";
import { SPLITS } from "../lib/layout.mjs";
import { adminPointer } from "../lib/widgets.mjs";
import { backendsValue, smartApprovalsValue, memoryValue, notificationsValue, resourcesValue, remoteControlValue, versionStatus,
  serverVersionText } from "../lib/settings-text.mjs";

// A mounted desktop menu follows successful writes from any of its detail pages.
const settingsRefreshes = new Set();
export function refreshingSettingsApi(api) {
  return async (path, options) => {
    const result = await api(path, options);
    if (options?.method && options.method !== "GET") settingsRefreshes.forEach((refresh) => refresh());
    return result;
  };
}

export function mountProfile({ $app, $conn, $profileIcon, layoutBar, setHeader, h, fill, append, api, getWebAuth, startGoogle, agentHarnessWeb, isGuest, isMember, isOwner, toast, go, route,
  build = {}, reloadAndUpdate = async () => false, onConnState = () => () => {}, browser, confirmSheet = sheets.confirmSheet }) {
// Browser globals come in through `browser` (globalThis in the app, a stub under Node) so importing this module touches no DOM.
const { document, window, localStorage, location, navigator, history, getComputedStyle, requestAnimationFrame, open,
  setTimeout, clearTimeout, fetch } = browser;
api = refreshingSettingsApi(api);
const escalateSuffix = (row) => (row.escalate_reason ? ` (${row.escalate_reason})` : "");

const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || !!navigator.standalone;
const GUEST_HIDDEN_PAGES = new Set(["notifications", "endpoint", "smart-approvals"]);
const MEMBER_HIDDEN_PAGES = new Set(["notifications", "endpoint", "memory", "backends", "smart-approvals"]);
const PROFILE_PAGES = {
  connection: "Connection",
  appearance: "Appearance",
  notifications: "Notifications",
  install: "Install",
  backends: "Backends",
  "smart-approvals": "Smart approvals",
  memory: "Memory",
  endpoint: "Inference endpoint",
};
// The Settings menu (#512): four groups of rows, each row showing its current value. `action` rows are the owner's
// Actions pages (#/actions/<id>), folded in under Server; the rest open #/profile/<id>. Daemon administration rows
// (Server settings, Accounts, Skills, Apps) moved to the Hub and the CLI (#545).
const SETTINGS_GROUPS = [
  ["This device", ["appearance", "notifications", "connection", "install"]],
  ["Agents", ["backends", "smart-approvals", "memory"]],
  ["Server", ["resources", "remote-control", "disk"]],
  ["Integrations", ["endpoint"]],
];
const ACTION_PAGES = { resources: "Resources", "remote-control": "Remote control", disk: "Disk" };
// Swatches preview each theme's --bg, --panel and --accent from style.css (tests/test_web_tokens.py keeps them in step).
const THEMES = {
  auto: { label: "System", swatch: ["#f3f4f6", "#ffffff", "#2457d9"] },
  light: { label: "Light", swatch: ["#f3f4f6", "#ffffff", "#2457d9"] },
  dark: { label: "Dark", swatch: ["#000000", "#15181d", "#8cb5ff"] },
  midnight: { label: "Midnight", swatch: ["#0b1220", "#152038", "#7dd3fc"] },
  forest: { label: "Forest", swatch: ["#0f1a14", "#1a2c22", "#86efac"] },
  paper: { label: "Paper", swatch: ["#f4efe6", "#fffaf2", "#9a3412"] },
  custom: { label: "Custom", swatch: ["#888888", "#aaaaaa", "#2563eb"] },
};
const THEME_COLORS = { bg: "#f3f4f6", panel: "#ffffff", accent: "#2457d9" };

function readTheme() {
  try { return localStorage.getItem("harness.theme") || "auto"; } catch (_) { return "auto"; }
}
function readHues() {
  try { return { ...THEME_COLORS, ...JSON.parse(localStorage.getItem("harness.themeHues") || "null") }; }
  catch (_) { return { ...THEME_COLORS }; }
}
function applyTheme(name, hues) {
  const root = document.documentElement;
  const theme = name || readTheme();
  const colors = hues || readHues();
  if (theme && theme !== "auto") root.dataset.theme = theme;
  else delete root.dataset.theme;
  for (const key of ["bg", "panel", "accent"]) {
    if (theme === "custom" && colors[key]) root.style.setProperty(`--${key}`, colors[key]);
    else root.style.removeProperty(`--${key}`);
  }
  const meta = document.querySelector('meta[name="theme-color"]:not([media])')
    || document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(root).getPropertyValue("--bg").trim() || "#000000";
  try {
    localStorage.setItem("harness.theme", theme);
    localStorage.setItem("harness.themeHues", JSON.stringify(colors));
  } catch (_) { /* private mode */ }
  refreshAppearanceValue();
}

const TEXT_SIZES = {
  s: { label: "Small", sample: "Aa", scale: 0.875 },
  m: { label: "Default", sample: "Aa", scale: 1 },
  l: { label: "Large", sample: "Aa", scale: 1.125 },
  xl: { label: "Extra", sample: "Aa", scale: 1.25 },
};
function readTextSize() {
  try {
    const id = localStorage.getItem("harness.textSize") || "m";
    return TEXT_SIZES[id] ? id : "m";
  } catch (err) {
    console.debug("text size not readable from storage; using the default", err);
    return "m";
  }
}
function applyTextSize(id) {
  const size = TEXT_SIZES[id] ? id : readTextSize();
  document.documentElement.style.setProperty("--text-scale", String(TEXT_SIZES[size].scale));
  try { localStorage.setItem("harness.textSize", size); } catch (_) { /* private mode */ }
  requestAnimationFrame(layoutBar);
  refreshAppearanceValue();
}

// Copies text and says so; when the clipboard is unavailable (insecure context, permission denied) the
// caller's fallback leaves the text selected so it can be copied by hand.
async function copyToClipboard(text, selectFallback) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied");
  } catch (err) {
    console.debug("clipboard write failed", err);
    selectFallback();
  }
}

function copyBox(value) {
  const code = h("code", {}, value);
  const btn = h("button", {
    class: "btn small", type: "button",
    onclick: async () => {
      void copyToClipboard(value, () => {
        const range = document.createRange();
        range.selectNodeContents(code);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
      });
    },
  }, "Copy");
  return h("div", { class: "copy-box", onclick: () => btn.click() }, code, btn);
}

function emojiPicker(profile, onPick) {
  return h("div", { class: "emoji-grid" }, profile.choices.map((emoji) => h("button", {
    class: `btn emoji-choice${emoji === profile.emoji ? " selected" : ""}`, type: "button", "aria-label": `Use ${emoji}`,
    onclick: async (event) => {
      try {
        await api("/profile", { method: "PUT", body: { emoji } });
        $profileIcon.textContent = emoji;
        profile.emoji = emoji;
        if (readAppIcon() === "profile") applyAppIcon("profile", emoji);
        for (const button of event.currentTarget.parentNode.children) button.classList.toggle("selected", button === event.currentTarget);
        onPick?.(emoji);
      } catch (e) { toast(e.message); }
    },
  }, emoji)));
}

function accountCard(me, profile) {
  const live = $conn.classList.contains("live");
  const usage = me.usage || {};
  const identityNote = isMember() ? "Household member identity." : "Profile icon is owner-only during demo access.";
  return h("div", {},
    isGuest() || isMember() ? h("div", { class: "card" },
      h("p", { class: "muted small" }, identityNote),
      h("p", { style: "font-size:2rem;margin:0" }, profile.emoji || "🙂"))
      : h("div", { class: "card" },
      h("p", { class: "muted small" }, "Shown at the top left of the app."),
      emojiPicker(profile)),
    h("div", { class: "card" },
      h("h3", {}, "Account"),
      h("p", {}, me.name || "You"),
      h("p", { class: "muted small" }, me.login || "Not identified by Tailscale on this request."),
      me.user_id && isMember() ? h("p", { class: "muted small" }, `Account ${usage.account_hint || me.user_id}`) : null,
      isGuest() ? h("p", { class: "muted small" }, "Demo access — look around only.") : null,
      isMember() && usage.disk_note ? h("p", { class: "muted small" }, usage.disk_note) : null,
      isMember() ? h("p", { class: "muted small" }, `${usage.running || 0} running · ${usage.queued || 0} queued`) : null),
    isMember() ? googleSignInCard() : null,
    isMember() ? githubConnectionCard() : null,
    isMember() ? apiKeysCard() : null,
    h("div", { class: "card" },
      h("h3", {}, "Connection"),
      me.public_url ? copyBox(me.public_url) : h("p", { class: "muted small" }, "No public URL configured."),
      h("p", { class: "muted small" }, live
        ? `Stables is connected to Agent Harness Server at ${me.public_url || location.origin}.`
        : `Stables is not receiving the live stream from Agent Harness Server at ${me.public_url || location.origin}.`)));
}

// Issue #64: this member's linked Google identity, Web sessions, logout, and unlink. Never shows tokens or `sub`.
function googleSignInCard() {
  const card = h("div", { class: "card" }, h("h3", {}, "Google sign-in"), h("p", { class: "muted small" }, "Loading…"));
  const render = async () => {
    let view;
    try { view = await api("/me/google", { surface: "app" }); }
    catch (e) { fill(card, h("h3", {}, "Google sign-in"), h("p", { class: "note bad" }, e.message)); return; }
    if (!view.linked && !view.available) { card.remove(); return; }
    const logout = h("button", { class: "btn small", type: "button", onclick: async () => {
      try { await api("/auth/logout", { method: "POST", surface: "app" }); }
      catch (e) { toast(e.message, 6000); return; }
      agentHarnessWeb.csrf = "";
      go("#/", true);
      await route();
    } }, "Log out");
    const unlink = h("button", { class: "btn small danger", type: "button", onclick: async () => {
      const warning = view.unlink_removes_this_device
        ? "You signed in here with Google, so this device will no longer open your account. You can still use a device signed in to your own Tailscale login, or ask the owner for a new link code. "
        : "";
      if (!(await confirmSheet({ title: "Unlink Google from your household account?", message: `${warning}Your data stays.`,
        confirmLabel: "Unlink Google", destructive: true }))) return;
      try { await api("/me/google", { method: "DELETE", surface: "app", body: { confirm: true } }); }
      catch (e) { toast(e.message, 6000); return; }
      toast("Google unlinked");
      await route();
    } }, "Unlink Google");
    const link = h("button", { class: "btn small", type: "button", onclick: async () => {
      link.disabled = true;
      try { await startGoogle("link"); } catch (e) { toast(e.message, 6000); link.disabled = false; }
    } }, "Link Google account");
    fill(card, h("h3", {}, "Google sign-in"),
      h("p", { class: "muted small" }, view.explanation),
      view.linked ? h("p", {}, view.email) : h("p", { class: "muted small" }, "No Google account linked."),
      view.linked ? h("p", { class: "muted small" }, `Linked ${ago(view.linked_at)}`
        + (view.last_sign_in_at ? ` · last sign-in ${ago(view.last_sign_in_at)}` : "")
        + ` · ${view.active_web_sessions} active Web session${view.active_web_sessions === 1 ? "" : "s"}`) : null,
      view.linked ? h("p", { class: "muted small" }, "To use a different Google account, unlink first, then link again.") : null,
      h("div", { class: "row", style: "flex-wrap:wrap;gap:8px" },
        getWebAuth()?.signed_in ? logout : null,
        view.linked ? unlink : null,
        !view.linked && view.available ? link : null));
  };
  void render();
  return card;
}

// Issue #393: a household member's own Anthropic / OpenAI API keys for hosted Claude Code and Codex. The key goes to
// the server once and never comes back; only its last four characters are shown.
function apiKeysCard() {
  const card = h("div", { class: "card" }, h("h3", {}, "Your API keys"), h("p", { class: "muted small" }, "Loading…"));
  const render = async () => {
    try { show(await api("/me/api-keys")); }
    catch (e) { fill(card, h("h3", {}, "Your API keys"), h("p", { class: "note bad" }, e.message)); }
  };
  const call = (path, method, body) => async () => {
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try { return await api(path, { method, body }); }
    catch (e) { toast(e.message, 6000); return null; }
    finally { await render(); }
  };
  const row = (k, usage) => {
    const input = h("input", { type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: k.configured ? "Paste a new key to replace it" : `${k.provider} API key (${k.env})` });
    const result = h("p", { class: "muted small" });
    const save = async () => {
      const key = input.value;
      input.value = "";
      if (key.trim()) await call(`/me/api-keys/${k.backend}`, "PUT", { key })();
    };
    const test = async () => {
      card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try { const r = await api(`/me/api-keys/${k.backend}/test`, { method: "POST" }); result.textContent = r.message;
        result.className = r.ok ? "muted small" : "note bad"; }
      catch (e) { result.textContent = e.message; result.className = "note bad"; }
      finally { card.querySelectorAll("button").forEach((b) => { b.disabled = false; }); }
    };
    return h("div", { style: "margin-top:12px" },
      h("strong", {}, k.backend === "claude" ? "Claude Code" : "Codex"),
      h("p", { class: "muted small" }, k.configured ? `${k.provider} key saved, ending …${k.last4}` : `No ${k.provider} key yet`),
      k.configured ? h("p", { class: "muted small" },
        `${usage?.sessions || 0} sessions · ${(usage?.prompt_tokens || 0) + (usage?.completion_tokens || 0)} tokens`) : null,
      input,
      h("div", { class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:6px" },
        h("button", { class: "btn primary small", type: "button", onclick: save }, k.configured ? "Replace" : "Save"),
        k.configured ? h("button", { class: "btn small", type: "button", onclick: test }, "Test") : null,
        k.configured ? h("button", { class: "btn bad small", type: "button", onclick: async () => {
          if (await confirmSheet({ title: `Delete your ${k.provider} key?`, message: `Your running ${k.backend} sessions stop.`,
            confirmLabel: "Delete key", destructive: true })) await call(`/me/api-keys/${k.backend}`, "DELETE")();
        } }, "Delete") : null),
      result);
  };
  const show = (st) => {
    fill(card, h("h3", {}, "Your API keys"),
      h("p", { class: "small" }, "Run Claude Code or Codex on your own provider account. Your key is stored encrypted, "
        + "used only for your sessions, and never shown again. The owner's subscription is never used for you."),
      h("p", { class: "muted small" }, st.billing_warning || ""),
      st.keys.map((k) => row(k, st.usage?.[k.backend])));
  };
  void render();
  return card;
}

const GITHUB_DEVICE_URL = "https://github.com/login/device";

// Issue #63: a household member's own GitHub sign-in (Git Credential Manager device flow). The token never
// reaches the browser; only the verification URL and user code, shown to this member while connecting.
function githubConnectionCard({ onConnected } = {}) {
  const card = h("div", { class: "card" }, h("h3", {}, "GitHub"), h("p", { class: "muted small" }, "Loading…"));
  let timer = null;
  let shown = false;
  const render = async () => {
    try { show(await api("/me/github-connection")); }
    catch (e) { fill(card, h("h3", {}, "GitHub"), h("p", { class: "note bad" }, e.message)); }
  };
  const act = (path, method, ask) => async () => {
    if (ask && !(await confirmSheet(ask))) return;
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try { show(await api(path, { method })); }
    catch (e) { toast(e.message, 6000); await render(); }
  };
  const show = (st) => {
    clearTimeout(timer);
    if (shown && !card.isConnected) return;  // navigated away: stop polling
    shown = true;
    const note = h("p", { class: "muted small" }, st.scopes_note || "");
    const err = st.message ? h("p", { class: "note bad" }, st.message) : null;
    const used = st.last_used_at ? h("p", { class: "muted small" }, `Last used ${ago(st.last_used_at)}`) : null;
    const connect = (label) => h("button", { class: "btn primary", type: "button",
      onclick: act("/me/github-connection/connect", "POST") }, label);
    if (st.status === "disabled") {
      fill(card, h("h3", {}, "GitHub"), h("p", { class: "muted small" }, st.message || "GitHub sign-in is unavailable."));
      return;
    }
    if (st.status === "connecting") {
      const prompt = st.prompt;
      const left = st.seconds_left || 0;
      fill(card, h("h3", {}, "GitHub — connecting"),
        prompt && prompt.verification_uri === GITHUB_DEVICE_URL ? [
          h("p", { class: "small" }, "Open GitHub's device page and enter this code:"),
          copyBox(prompt.user_code),
          h("div", { class: "row", style: "gap:8px;flex-wrap:wrap" },
            h("a", { class: "btn small", href: GITHUB_DEVICE_URL, target: "_blank", rel: "noopener noreferrer" },
              "Open github.com/login/device"),
            h("button", { class: "btn small", type: "button", onclick: () => copyToClipboard(GITHUB_DEVICE_URL) },
              "Copy link")),
        ] : h("p", { class: "muted small" }, "Waiting for GitHub's sign-in code…"),
        h("p", { class: "muted small" }, `This code expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}.`),
        note,
        h("div", { class: "row", style: "margin-top:8px" },
          h("button", { class: "btn small", type: "button", onclick: act("/me/github-connection/cancel", "POST") }, "Cancel")));
      timer = setTimeout(render, 2000);
      return;
    }
    if (st.status === "connected") {
      fill(card, h("h3", {}, "GitHub — connected"), used, note,
        h("div", { class: "row", style: "gap:8px;flex-wrap:wrap" },
          connect("Reconnect"),
          h("button", { class: "btn bad small", type: "button", onclick: act("/me/github-connection", "DELETE",
            { title: "Disconnect GitHub?", message: "The stored credential is erased. Your projects, workspaces, and history stay.",
              confirmLabel: "Disconnect", destructive: true }) },
            "Disconnect")));
      const done = onConnected;
      onConnected = null;
      done?.();
      return;
    }
    const again = st.status === "reconnect_required";
    fill(card, h("h3", {}, again ? "GitHub — reconnect needed" : "GitHub"), err,
      h("p", { class: "small" }, "Connect your own GitHub account to clone, fetch, and push your private repositories. "
        + "Git Credential Manager keeps the credential in this machine's secure store; it never reaches the browser "
        + "or agent sandboxes."),
      note, used, h("div", { class: "row" }, connect(again ? "Reconnect" : "Connect")));
  };
  void render();
  return card;
}

// Each profile subpage builds its card from the account and the profile emoji.
const PROFILE_CARDS = {
  account: (me, profile) => accountCard(me, profile),
  appearance: () => appearanceCard(),
  notifications: (me) => notificationsCard(me),
  install: () => installCard(),
  backends: () => backendsCard(),
  "smart-approvals": () => smartApprovalsCard(),
  memory: () => memoryCard(),
  endpoint: (me) => endpointCard(me),
};

// Register through the shared framework without changing other desktop workers' files.
// The shell resolves this list through its existing viewProfile view.
if (!SPLITS.some((split) => split.key === "settings")) SPLITS.push({
  key: "settings", list: "viewProfile", label: "Settings menu",
  match: (parts) => {
    // An unknown offline identity uses the router's single-pane recovery path, including on resize.
    if (!isOwner() && !isMember() && !isGuest()) return undefined;
    if (["profile", "settings"].includes(parts[0])) {
      if (parts.length === 1) return null;
      if (parts[1] === "account" || Object.hasOwn(PROFILE_PAGES, parts[1])) return parts[1];
    }
    if (parts[0] === "actions" && Object.hasOwn(ACTION_PAGES, parts[1])) return parts[1];
    return undefined;
  },
  empty: { title: "No setting open", text: "Choose your profile or a setting from the menu." },
});
let menuValues = null;
function refreshAppearanceValue() {
  const value = menuValues?.appearance;
  if (value) value.textContent = `${THEMES[readTheme()]?.label || "System"} · ${TEXT_SIZES[readTextSize()].label} text`;
}

function settingsGroupRows(values, isOwner, hidden) {
  const shown = (id) => (Object.hasOwn(ACTION_PAGES, id) ? isOwner()
    : (id !== "install" || !isStandalone()) && !hidden.has(id));
  return SETTINGS_GROUPS.map(([label, ids]) => {
    const rows = ids.filter(shown).map((id) => {
      values[id] = h("span", { class: "set-value" });
      const href = Object.hasOwn(ACTION_PAGES, id) ? `#/actions/${id}` : `#/profile/${id}`;
      return h("a", { class: "set-row", href, "data-setting": id, "data-split-key": id },
        h("span", { class: `set-icon ic-${id}`, "aria-hidden": "true" }),
        h("span", { class: "set-label" }, ACTION_PAGES[id] || PROFILE_PAGES[id]),
        values[id],
        h("span", { class: "chevron", "aria-hidden": "true" }, "›"));
    });
    // A group whose rows are all hidden for this role (a member's Agents, say) gets no header either.
    return rows.length ? [h("p", { class: "section-label" }, label), h("div", { class: "card settings-group" }, rows)] : null;
  }).flat().concat(isOwner() ? adminPointer(h) : null);
}

async function viewProfile(page) {
  // With a pane, the router is asking for the persistent menu rather than a detail page.
  if (page?.body) return settingsMenu(page);
  const titles = { account: "Account", ...PROFILE_PAGES };
  if (page && !titles[page]) { go("#/profile", true); return; }
  if (page === "install" && isStandalone()) { go("#/profile", true); return; }
  // The header's Settings gear (#/settings, #506) opens this same menu under its own name.
  const home = location.hash.startsWith("#/settings") ? "Settings" : "Profile";
  setHeader("agents", titles[page] || home, { page: true });
  if (page === "connection") return append($app, connectionCard());
  const [me, profile] = await Promise.all([api("/me"), api("/profile").catch(() => ({ emoji: "🙂", choices: [] }))]);
  if (Object.hasOwn(PROFILE_CARDS, page)) return append($app, await PROFILE_CARDS[page](me, profile));
  return settingsMenu(null, me, profile);
}

async function settingsMenu(pane = null, me = null, profile = null) {
  const target = pane?.body || $app;
  let active = true;
  let generation = 0;
  let menuError = null;
  const values = {};
  if (pane) {
    pane.header("Settings");
    pane.onLeave(() => {
      active = false;
      if (menuValues === values) menuValues = null;
    });
  }
  if (!me) [me, profile] = await Promise.all([
    api("/me").catch((err) => { menuError = err; return {}; }),
    api("/profile").catch(() => ({ emoji: "🙂", choices: [] })),
  ]);
  if (!active) return;
  const currentRole = () => isGuest() ? "guest" : isMember() ? "member" : isOwner() ? "owner" : "offline";
  let menuRole = currentRole();
  const groups = pane ? h("div") : null;
  let groupNodes;
  const renderGroups = () => {
    for (const id of Object.keys(values)) delete values[id];
    const hidden = menuRole === "guest" ? GUEST_HIDDEN_PAGES : menuRole === "member" ? MEMBER_HIDDEN_PAGES : new Set();
    groupNodes = settingsGroupRows(values, () => menuRole === "owner", hidden);
    if (groups) fill(groups, groupNodes);
  };
  renderGroups();
  const identityNote = h("div", { class: "muted small" }, menuRole === "member" ? "Household member" : serverNote());
  {
    // Follows the header chip while Settings is open; the first change after the page is gone unsubscribes.
    const stop = onConnState(() => {
      if (identityNote.isConnected) identityNote.textContent = menuRole === "member" ? "Household member" : serverNote();
      else stop();
    });
    pane?.onLeave(stop);
  }
  const identityEmoji = h("span", { class: "identity-emoji" }, profile.emoji || "🙂");
  const identityName = h("h3", {}, me.name || "You");
  const usage = pane ? h("div") : null;
  let usageCard;
  const renderUsage = (latest) => {
    usageCard = menuRole === "member" && latest.usage ? h("div", { class: "card" },
      h("h3", {}, "Usage"),
      h("p", { class: "muted small" }, latest.usage.disk_note || ""),
      h("p", { class: "muted small" }, `${latest.usage.running || 0} running · ${latest.usage.queued || 0} queued`)) : null;
    if (usage) fill(usage, usageCard);
  };
  renderUsage(me);
  const errorNote = menuError ? h("p", { class: "note bad" }, menuError.message) : null;
  append(target,
    errorNote,
    h("a", { class: "card identity", href: "#/profile/account", "data-split-key": "account" },
      h("div", { class: "row" },
        identityEmoji,
        h("div", { class: "spacer" },
          identityName,
          identityNote),
        h("span", { class: "chevron", "aria-hidden": "true" }, "›"))),
    pane ? usage : usageCard,
    pane ? groups : groupNodes,
    versionRow());
  if (pane) menuValues = values;
  if (pane) {
    const readMe = () => api("/me").catch((err) => {
      // The router may still be adopting a new member identity when this hash-change listener runs.
      if (err.status === 403) return api("/me", { surface: "app" });
      throw err;
    });
    const refresh = () => {
      const next = ++generation;
      const current = () => active && next === generation;
      Promise.all([readMe(), api("/profile").catch(() => null)]).then(([latest, icon]) => {
        if (!current()) return;
        if (errorNote) errorNote.hidden = true;
        const latestRole = latest.role || currentRole();
        if (menuRole !== latestRole) {
          menuRole = latestRole;
          renderGroups();
          pane.paint();
        }
        fillSettingValues(values, latest, current, menuRole === "owner");
        identityName.textContent = latest.name || "You";
        identityNote.textContent = menuRole === "member" ? "Household member" : serverNote();
        renderUsage(latest);
        if (icon) identityEmoji.textContent = icon.emoji || "🙂";
      }).catch((err) => console.debug("settings menu refresh", err));
    };
    settingsRefreshes.add(refresh);
    window.addEventListener("hashchange", refresh);
    window.addEventListener("online", refresh);
    const stopConnectionRefresh = onConnState((state) => { if (state === "live") refresh(); });
    pane.onLeave(() => {
      settingsRefreshes.delete(refresh);
      window.removeEventListener("hashchange", refresh);
      window.removeEventListener("online", refresh);
      stopConnectionRefresh();
    });
  }
  fillSettingValues(values, me, () => active && generation === 0, menuRole === "owner");
  pane?.paint();
}

function serverLabel() {
  if (!agentHarnessWeb.independent) return "Bundled server";
  try { return new URL(agentHarnessWeb.baseUrl).host; } catch (_) { return agentHarnessWeb.baseUrl; }
}

// "Bundled server · live", in the header chip's state (#510). No state until the chip first shows, as the chip does.
function serverNote() {
  if ($conn.hidden) return serverLabel();
  const state = $conn.dataset?.state || ($conn.classList.contains("live") ? "live" : "offline");
  return `${serverLabel()} · ${state}`;
}

// Paints the menu first, then each row's value as its own request answers. A failed or slow request leaves that
// row's value blank; it never toasts or holds up the others. Only the owner's rows read the owner API.
function fillSettingValues(values, me, current = () => true, owner = isOwner()) {
  const paint = (id, value) => {
    const el = values[id];
    if (!current() || !el || !value) return;
    el.textContent = value.text || "";
    el.classList.toggle("warn", !!value.warn);
  };
  const theme = THEMES[readTheme()]?.label || "System";
  paint("appearance", { text: `${theme} · ${TEXT_SIZES[readTextSize()].label} text` });
  paint("connection", { text: serverLabel() });
  paint("notifications", notificationsValue(me.notify));
  paint("install", { text: "Add to Home Screen" });
  if (!owner) return;
  const loaders = {
    backends: () => api("/backends?auth=skip").then(backendsValue),
    "smart-approvals": () => api("/smart-approvals").then(smartApprovalsValue),
    memory: () => api("/memory").then(memoryValue),
    resources: () => api("/resources").then(resourcesValue),
    "remote-control": () => api("/remote-control").then(remoteControlValue),
    // Inference endpoint has no value: its keys are daemon administration, in the Hub and the CLI (#545).
    // Disk has no value here: /maintenance walks every workspace and asks Docker for container sizes, too slow to
    // run on every visit to this menu. The Disk page measures it on open.
  };
  for (const [id, load] of Object.entries(loaders)) {
    if (!values[id]) continue;
    Promise.resolve().then(load).then((value) => paint(id, value)).catch((err) => console.debug(`settings value ${id}`, err));
  }
}

// Version and update (#512): this bundle's build and protocol, and the connected server's view of it. Check for
// update asks the server again; when a newer bundle is offered the button reloads into it.
function versionRow() {
  const { WEB_BUILD_ID = "", WEB_PROTOCOL = "" } = build;
  const line = h("p", { class: "version-line" }, `Stables ${WEB_BUILD_ID} · protocol ${WEB_PROTOCOL}`);
  const server = h("p", { class: "version-line muted" });
  const button = h("button", { class: "btn version-btn", type: "button" }, "Check for update");
  let update = false;
  const check = async () => {
    button.disabled = true;
    let meta = null;
    try { meta = await agentHarnessWeb.compatibility(); } catch (err) { console.debug("compatibility check failed", err); }
    const status = versionStatus(meta, WEB_BUILD_ID, WEB_PROTOCOL, protocolMismatch(meta?.protocols?.admin, WEB_PROTOCOL));
    line.textContent = `Stables ${WEB_BUILD_ID} · protocol ${WEB_PROTOCOL} · ${status.text}`;
    line.classList.toggle("warn", !!status.warn);
    server.textContent = serverVersionText(meta);
    update = status.update;
    button.textContent = update ? "Reload and update" : "Check for update";
    button.disabled = false;
    return status;
  };
  button.addEventListener("click", async () => {
    if (update) { await reloadAndUpdate(); return; }
    const status = await check();
    toast(status.update ? "A newer Stables is available" : `Stables is ${status.text}`);
  });
  void check();
  return h("div", { class: "version-row" }, line, server, button);
}

function connectionCard() {
  const server = h("input", {
    type: "url", inputmode: "url", value: agentHarnessWeb.baseUrl,
    placeholder: "Blank for this server, or https://tower.example.ts.net",
    autocomplete: "url", spellcheck: "false",
  });
  const token = h("input", {
    type: "password", value: agentHarnessWeb.token, placeholder: "ho-… owner token",
    autocomplete: "off", spellcheck: "false",
  });
  const serverUrl = agentHarnessWeb.baseUrl || location.origin;
  const status = h("p", { class: "muted small" },
    `Stables connects to Agent Harness Server at ${serverUrl}.`);
  const save = async () => {
    try {
      agentHarnessWeb.configure(server.value, token.value);
      status.textContent = "Checking Agent Harness Server…";
      const root = await agentHarnessWeb.request("", { surface: "admin" });
      status.textContent = `Stables is connected to Agent Harness Server at ${server.value || location.origin} (${root.server} owner API ${root.api_version}). Reloading…`;
      setTimeout(() => location.reload(), 350);
    } catch (e) {
      status.className = "note bad";
      status.textContent = `${e.message} Settings were saved so you can correct them here.`;
    }
  };
  return h("div", {},
    h("div", { class: "card" },
      h("h3", {}, "This copy of Stables"),
      h("p", { class: "muted small" }, "Leave the Agent Harness Server URL blank when this copy of Stables is bundled with the Server. For a separately hosted copy, enter the Server URL and an owner token approved for this Stables origin."),
      h("label", {}, "Agent Harness Server URL"), server,
      h("label", {}, "Owner token"), token,
      h("p", { class: "muted small" }, "The token is stored only in this browser. Do not use an Agent Harness App token; Stables uses the owner API."),
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "btn", onclick: () => { server.value = ""; token.value = ""; void save(); } }, "Use bundled Server"),
        h("span", { class: "spacer" }),
        h("button", { class: "btn primary", onclick: save }, "Save and test")), status),
    adminPointer(h));
}

const iconGlyph = (spec) => (spec.id === "profile" ? ($profileIcon.textContent || "🙂") : spec.emoji);
const APP_ICONS = [
  { id: "default", label: "Default" },
  { id: "profile", label: "Profile icon" },
  { id: "robot", emoji: "🤖", label: "Robot" },
  { id: "spark", emoji: "✨", label: "Spark" },
];

function readAppIcon() {
  try { return localStorage.getItem("harness.appIcon") || "default"; } catch (_) { return "default"; }
}
function emojiIconDataUrl(emoji) {
  const size = 180;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#101418";
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, size, size);
  ctx.font = "120px system-ui, Apple Color Emoji, Segoe UI Emoji, Noto Color Emoji";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(emoji, size / 2, size / 2 + 6);
  return canvas.toDataURL("image/png");
}
function applyAppIcon(id, profileEmoji) {
  const spec = APP_ICONS.find((icon) => icon.id === id) || APP_ICONS[0];
  let href = "/static/icon-180.png";
  const emoji = spec.id === "profile" ? (profileEmoji || $profileIcon.textContent || "🙂") : spec.emoji;
  if (emoji) href = emojiIconDataUrl(emoji);
  const apple = document.querySelector('link[rel="apple-touch-icon"]');
  const fav = document.querySelector('link[rel="icon"]');
  if (apple) apple.href = href;
  if (fav) fav.href = spec.id === "default" ? "/static/icon-192.png" : href;
  try { localStorage.setItem("harness.appIcon", spec.id); } catch (_) { /* private mode */ }
}

function appearanceCard() {
  let theme = readTheme();
  let hues = readHues();
  let appIcon = readAppIcon();
  let textSize = readTextSize();
  const hueRow = h("div", { class: "hue-row", hidden: theme !== "custom" });
  const grid = h("div", { class: "theme-grid" });
  const sizes = h("div", { class: "size-grid", role: "group", "aria-label": "Text size" });
  const icons = h("div", { class: "app-icon-grid" });
  const themeSwatch = (id, spec) => {
    if (id === "auto") {
      return h("div", { class: "swatch split" },
        h("div", { class: "swatch-half light" }, THEMES.light.swatch.map((color) => h("span", { style: `background:${color}` }))),
        h("div", { class: "swatch-half dark" }, THEMES.dark.swatch.map((color) => h("span", { style: `background:${color}` }))));
    }
    const colors = id === "custom" ? [hues.bg, hues.panel, hues.accent] : spec.swatch;
    return h("div", { class: "swatch" }, colors.map((color) => h("span", { style: `background:${color}` })));
  };
  const paint = () => {
    fill(grid, Object.entries(THEMES).map(([id, spec]) => h("button", {
      class: `theme-choice${theme === id ? " on" : ""}`, type: "button",
      onclick: () => { theme = id; applyTheme(theme, hues); hueRow.hidden = theme !== "custom"; paint(); },
    },
      themeSwatch(id, spec),
      h("div", { class: "name" }, spec.label))));
    fill(sizes, Object.entries(TEXT_SIZES).map(([id, spec]) => h("button", {
      class: `size-choice${textSize === id ? " on" : ""}`, type: "button", "aria-label": spec.label,
      onclick: () => { textSize = id; applyTextSize(textSize); paint(); },
    },
      h("span", { class: "sample", style: `font-size:${16 * spec.scale}px` }, spec.sample),
      h("span", { class: "name" }, spec.label))));
    fill(icons, APP_ICONS.map((spec) => h("button", {
      class: `app-icon-choice${appIcon === spec.id ? " on" : ""}`, type: "button",
      onclick: () => { appIcon = spec.id; applyAppIcon(appIcon); paint(); },
    },
      h("div", { class: "preview" }, spec.id === "default"
        ? h("img", { src: "/static/icon-180.png", alt: "" })
        : iconGlyph(spec)),
      h("div", { class: "name" }, spec.label))));
  };
  paint();
  fill(hueRow, ["bg", "panel", "accent"].map((key) => {
    const hex = /^#[0-9a-fA-F]{6}$/.test(hues[key]) ? hues[key] : THEME_COLORS[key];
    const preview = h("span", { class: "hue-preview", style: `background:${hex}` });
    const input = h("input", {
      type: "text", inputmode: "text", maxlength: "7", spellcheck: "false",
      value: hex, "aria-label": `${key} hex color`, autocomplete: "off",
    });
    input.addEventListener("input", () => {
      const value = input.value.trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(value)) return;
      hues = { ...hues, [key]: value };
      preview.style.background = value;
      applyTheme("custom", hues);
      theme = "custom";
      hueRow.hidden = false;
      paint();
    });
    return h("label", {},
      { bg: "Background", panel: "Panel" }[key] || "Accent",
      h("div", { class: "hue-control" }, preview, input));
  }));
  return h("div", { class: "card" },
    h("p", { class: "muted small" }, "How the app looks on this device. The profile icon — the emoji next to your name — lives on the Profile card."),
    grid, hueRow,
    h("p", { class: "section-label" }, "Text size"),
    h("p", { class: "muted small" }, "This device only, like the theme. Session list, transcript, Settings, and the header all follow it."),
    sizes,
    h("p", { class: "section-label" }, "Home screen icon"),
    h("p", { class: "muted small" }, "Used when you add this app to the home screen. Separate from the profile icon."),
    icons,
    h("p", { class: "muted small" }, "iPhone keeps the icon from when you added the app. To apply a new one, delete it from the home screen and Add to Home Screen again."));
}

function notificationsCard(me) {
  const ntfyUrl = me.public_url ? `${me.public_url}:8443` : "(set public_url)";
  return h("div", { class: "card" },
    me.notify.enabled ? h("ol", {},
      h("li", {}, "Install the ntfy app from the App Store."),
      h("li", {}, "In ntfy: Settings → Users → add ", h("code", {}, ntfyUrl), " with the phone username and password", String.raw` (D:\Docker\ntfy\secrets\phone-login.txt on the tower).`),
      h("li", {}, "Settings → Default server → the same URL. Then + → topic ", h("code", {}, me.notify.topic), "."),
      h("li", {}, "Tap a notification to open the session; long-press it for Approve / Deny.")) : h("p", {}, "Disabled in config/harness.yaml."),
    adminPointer(h));
}

function installCard() {
  return h("div", { class: "card" },
    h("p", {}, "Install Stables in Safari: Share → Add to Home Screen. It appears as Stables and opens full screen."),
    h("p", { class: "muted small" }, "An existing iPhone or iPad icon may keep its previous label until you remove it and add Stables to the Home Screen again."));
}

function backendUsage(b) {
  if (b.name === "local") return "Local Qwen on this PC";
  if (b.kind === "cli") return `${b.login_note} · ${b.notice}`;
  const limits = b.limits || {};
  const pct = limits.utilization === undefined ? null : Math.round(limits.utilization * 100);
  const period = String(limits.rateLimitType || "limit").replace("seven_day", "7d").replace("five_hour", "5h");
  const signedIn = b.logged_in ? "signed in" : "sign-in needed";
  const sub = pct === null ? signedIn : `${period} ${pct}% used`;
  const req = `${b.today.requests || 0} today · ${b.week.requests || 0} this week`;
  const cost = Number(b.today.cost_usd || 0) + Number(b.week.cost_usd || 0);
  const dollars = cost > 0 ? ` · $${Number(b.today.cost_usd || 0).toFixed(2)} today` : "";
  return `${b.logged_in ? "signed in" : "sign-in/key needed"} · ${sub} · ${req}${dollars}`;
}

async function smartApprovalsCard() {
  let data;
  try { data = await api("/smart-approvals"); }
  catch (e) { return h("div", { class: "card" }, h("p", { class: "note bad" }, e.message)); }
  const status = h("p", { class: "muted small" });
  const configured = data.configured || data.enabled;
  status.textContent = configured
    ? `${data.provider || "provider"} · ${data.model || "model"} · mode ${data.mode}`
    : "Off. The owner enables this in harness.yaml with a hosted API secret reference.";
  const stats = h("p", { class: "muted small" },
    `${data.attempts || 0} reviews · ${data.auto_approvals || 0} auto-approved · ${data.escalations || 0} escalated · `
    + `${data.latency_ms || 0} ms avg · $${Number(data.cost_usd || 0).toFixed(4)}`);
  const recent = (data.recent || []).slice(0, 12).map((row) => h("div", { class: "muted small" },
    `${row.outcome} · ${row.recommendation}${escalateSuffix(row)} · `
    + `${row.provider}/${row.model}`));
  return h("div", {},
    h("div", { class: "card" },
      h("h3", {}, "Smart approvals"),
      h("p", { class: "muted small" }, "A small hosted model can rate tagged, local, reversible shell asks after the deterministic policy. It never auto-denies. Shadow records a recommendation and still asks; auto only approves a high-confidence approve."),
      status, stats, adminPointer(h)),
    recent.length ? h("div", { class: "card" }, h("h3", {}, "Recent reviews"), ...recent) : null);
}

async function backendsCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Checking…"));
  let failed = false;
  const rows = await api("/backends?auth=skip").catch((e) => {
    fill(body, h("p", { class: "note bad" }, e.message));
    failed = true;
    return [];
  });
  if (failed) {
    return h("div", { class: "card" }, body);
  }
  const BACKEND_TITLES = { local: "Qwen (this PC)", claude: "Claude", codex: "Codex", cursor: "Cursor", opencode: "OpenCode" };
  const title = (b) => BACKEND_TITLES[b.name] || b.name;
  const hasEffort = (b) => b.effort && b.name !== "local" && b.kind !== "cli";
  const defaultsText = (b) => `Default model ${b.model}${hasEffort(b) ? ` · effort ${b.effort}` : ""}`;
  const usage = {};
  fill(body, rows.length ? rows.map((b) => {
    const line = h("p", { class: `small ${b.billing_warning ? "bad" : "muted"}` }, b.billing_warning || backendUsage(b));
    usage[b.name] = line;
    return h("div", { class: "backend-block" },
      h("strong", {}, title(b)),
      line,
      b.model ? h("p", { class: "muted small" }, defaultsText(b)) : null);
  }) : h("p", { class: "muted small" }, "No backends configured."));
  api("/backends").then((fresh) => {
    for (const b of fresh) {
      const line = usage[b.name];
      if (!line) continue;
      line.className = `small ${b.billing_warning ? "bad" : "muted"}`;
      line.textContent = b.billing_warning || backendUsage(b);
    }
  }).catch(() => {});
  return h("div", { class: "card" },
    h("p", { class: "muted small" }, "New tasks use these defaults. You can still pick a backend when you start one."),
    body, adminPointer(h));
}

function memoryCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Loading…"));
  void (async () => {
    try {
      const mem = await api("/memory");
      if (!mem.enabled) return fill(body, h("p", { class: "muted small" }, "The memory library is disabled in config/harness.yaml."));
      const editor = h("textarea", { class: "memory-editor", rows: 24, readOnly: true }, mem.profile || "");
      fill(body,
        h("p", { class: "small" }, "A short purpose statement given to every new session: how agents should use this library. Keep personal biography in category files, not here."),
        h("p", { class: "small" }, `Agents can read ${mem.categories.join(", ")}. `,
          mem.writes ? "They can propose changes; every change asks you first." : "Read-only for agents."),
        editor,
        h("p", { class: "muted small" }, `${mem.profile_path || "profile"} · limit ${mem.profile_max_chars} characters`),
        adminPointer(h),
        mem.last_commit?.head ? h("p", { class: "muted small" }, `Last saved change: ${mem.last_commit.summary} (${mem.last_commit.head}, ${ago(mem.last_commit.at)})`) : null,
        mem.refresh_error ? h("p", { class: "small bad" }, `Couldn't refresh the library: ${mem.refresh_error}`) : null);
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  })();
  return h("div", { class: "card" }, body);
}

function endpointCard(me) {
  const base = me.public_url || location.origin;
  return h("div", { class: "card" },
    h("p", { class: "muted small" }, "Point a coding tool at these URLs. Tap a box to copy. Any model name works; unknown names use the default."),
    h("p", { class: "small", style: "margin-bottom:0" }, "OpenAI-compatible"),
    copyBox(`${base}/v1`),
    h("p", { class: "small", style: "margin-bottom:0" }, "Anthropic-compatible"),
    copyBox(base),
    adminPointer(h));
}

return { viewProfile, copyBox, githubConnectionCard, readAppIcon, applyAppIcon, applyTheme, applyTextSize };
}
