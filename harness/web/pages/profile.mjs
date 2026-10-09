// Profile page (#258): account, appearance, notifications, install, backends, smart approvals, skills, memory,
// endpoint and apps cards. The shell (DOM builder, router, auth state) is injected by app.js so this module
// imports under plain Node and never reaches into another page.
import { ago, pluralize } from "../lib/format.mjs";
import { md } from "../lib/markdown.mjs";
import { showSecretOnce as showSecret } from "../lib/secret.mjs";

export function mountProfile({ $app, $conn, $profileIcon, layoutBar, setHeader, h, fill, append, api, getWebAuth, startGoogle, agentHarnessWeb, isGuest, isMember, isOwner, toast, go, route,
  daemonSettingsCard, browser }) {
// Browser globals come in through `browser` (globalThis in the app, a stub under Node) so importing this module touches no DOM.
const { document, window, localStorage, location, navigator, history, getComputedStyle, requestAnimationFrame, confirm, prompt, open,
  setTimeout, clearTimeout, fetch } = browser;
const escalateSuffix = (row) => (row.escalate_reason ? ` (${row.escalate_reason})` : "");
const originsSuffix = (k) => (k.origins?.length ? ` · ${k.origins.join(", ")}` : "");
const usedSuffix = (k) => (k.last_used_at ? ` · used ${ago(k.last_used_at)}` : "");
// An App's sessions stay in its own store; the owner sees only this metadata about them (#330).
const storeLine = (k) => {
  const s = k.store;
  if (!s) return "";
  const counts = Object.entries(s.sessions || {}).map(([status, n]) => `${n} ${status}`);
  const parts = [counts.length ? `Sessions: ${counts.join(", ")}` : "No sessions"];
  if (s.usage?.requests) parts.push(`${s.usage.requests} hosted requests, $${Number(s.usage.cost_usd || 0).toFixed(2)}`);
  if (s.errors) parts.push(`${s.errors} failed, last ${s.last_error}${s.last_error_at ? ` ${ago(s.last_error_at)}` : ""}`);
  return parts.join(" · ");
};
// A revoked App's (or device's) store and files are erased after a 7-day grace the owner can undo (#330).
const ERASE_GRACE_DAYS = 7;
const eraseDate = (k) => new Date(k.erase_after * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });

const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || !!navigator.standalone;
const GUEST_HIDDEN_PAGES = new Set(["notifications", "apps", "endpoint", "smart-approvals", "skills"]);
const MEMBER_HIDDEN_PAGES = new Set(["notifications", "apps", "endpoint", "memory", "backends", "smart-approvals", "skills"]);
const PROFILE_PAGES = {
  connection: "Connection",
  appearance: "Appearance",
  notifications: "Notifications",
  install: "Install",
  backends: "Backends",
  "smart-approvals": "Smart approvals",
  daemon: "Server",
  memory: "Memory",
  skills: "Skills",
  apps: "Apps",
  endpoint: "Inference endpoint",
};
const ACTION_PAGES = [["resources", "Resources"], ["accounts", "Accounts"], ["remote-control", "Claude Remote Control"], ["disk", "Disk"]];
const THEMES = {
  auto: { label: "System", swatch: ["#f6f7f9", "#ffffff", "#2563eb"] },
  light: { label: "Light", swatch: ["#f6f7f9", "#ffffff", "#2563eb"] },
  dark: { label: "Dark", swatch: ["#000000", "#232323", "#dddddd"] },
  midnight: { label: "Midnight", swatch: ["#0b1220", "#152038", "#7dd3fc"] },
  forest: { label: "Forest", swatch: ["#0f1a14", "#1a2c22", "#86efac"] },
  paper: { label: "Paper", swatch: ["#f4efe6", "#fffaf2", "#9a3412"] },
  custom: { label: "Custom", swatch: ["#888888", "#aaaaaa", "#2563eb"] },
};
const THEME_COLORS = { bg: "#f6f7f9", panel: "#ffffff", accent: "#2563eb" };

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
        ? `Agent Harness Web is connected to Agent Harness Server at ${me.public_url || location.origin}.`
        : `Agent Harness Web is not receiving the live stream from Agent Harness Server at ${me.public_url || location.origin}.`)));
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
        ? " You signed in here with Google, so this device will no longer open your account. You can still use a device signed in to your own Tailscale login, or ask the owner for a new link code."
        : "";
      if (!confirm(`Unlink Google from your household account?${warning} Your data stays.`)) return;
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
          if (confirm(`Delete your ${k.provider} key? Your running ${k.backend} sessions stop.`)) await call(`/me/api-keys/${k.backend}`, "DELETE")();
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
  const act = (path, method, confirmText) => async () => {
    if (confirmText && !confirm(confirmText)) return;
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
            "Disconnect GitHub? The stored credential is erased. Your projects, workspaces, and history stay.") },
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

// Each profile subpage builds its card from the account, the profile emoji and the optional third path part.
const PROFILE_CARDS = {
  account: (me, profile) => accountCard(me, profile),
  appearance: () => appearanceCard(),
  notifications: (me) => notificationsCard(me),
  install: () => installCard(),
  backends: () => backendsCard(),
  "smart-approvals": () => smartApprovalsCard(),
  daemon: () => daemonSettingsCard(),
  memory: () => memoryCard(),
  skills: (me, profile, extra) => skillsPage(extra),
  apps: (me) => appsCard(me),
  endpoint: (me) => endpointCard(me),
};

async function viewProfile(page, extra) {
  const titles = { account: "Account", ...PROFILE_PAGES };
  if (page && !titles[page]) { go("#/profile", true); return; }
  if (page === "install" && isStandalone()) { go("#/profile", true); return; }
  // The header's Settings gear (#/settings, #506) opens this same menu under its own name.
  const home = location.hash.startsWith("#/settings") ? "Settings" : "Profile";
  setHeader("agents", titles[page] || home, { page: true });
  if (page === "connection") return append($app, connectionCard());
  const [me, profile] = await Promise.all([api("/me"), api("/profile").catch(() => ({ emoji: "🙂", choices: [] }))]);
  if (Object.hasOwn(PROFILE_CARDS, page)) return append($app, await PROFILE_CARDS[page](me, profile, extra));
  let hidden = new Set();
  if (isGuest()) hidden = GUEST_HIDDEN_PAGES;
  else if (isMember()) hidden = MEMBER_HIDDEN_PAGES;
  append($app, 
    h("a", { class: "card identity", href: "#/profile/account" },
      h("div", { class: "row" },
        h("span", { class: "identity-emoji" }, profile.emoji || "🙂"),
        h("div", { class: "spacer" },
          h("h3", {}, me.name || "You"),
          h("div", { class: "muted small" }, isMember() ? "Household member" : "Account and connection")),
        h("span", { class: "chevron", "aria-hidden": "true" }, "›"))),
    isMember() && me.usage ? h("div", { class: "card" },
      h("h3", {}, "Usage"),
      h("p", { class: "muted small" }, me.usage.disk_note || ""),
      h("p", { class: "muted small" }, `${me.usage.running || 0} running · ${me.usage.queued || 0} queued`)) : null,
    h("p", { class: "section-label" }, "Settings"),
    h("div", { class: "card settings-list" },
      Object.entries(PROFILE_PAGES)
        .filter(([id]) => (id !== "install" || !isStandalone()) && !hidden.has(id))
        .map(([id, label]) => h("a", { href: `#/profile/${id}` }, label))),
    // The drawer's Actions entry moved here when the tab bar replaced it (#506).
    isOwner() ? h("p", { class: "section-label" }, "Actions") : null,
    isOwner() ? h("div", { class: "card settings-list" }, ACTION_PAGES.map(([id, label]) => h("a", { href: `#/actions/${id}` }, label))) : null,
  );
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
    `Agent Harness Web connects to Agent Harness Server at ${serverUrl}.`);
  const save = async () => {
    try {
      agentHarnessWeb.configure(server.value, token.value);
      status.textContent = "Checking Agent Harness Server…";
      const root = await agentHarnessWeb.request("", { surface: "admin" });
      status.textContent = `Agent Harness Web is connected to Agent Harness Server at ${server.value || location.origin} (${root.server} owner API ${root.api_version}). Reloading…`;
      setTimeout(() => location.reload(), 350);
    } catch (e) {
      status.className = "note bad";
      status.textContent = `${e.message} Settings were saved so you can correct them here.`;
    }
  };
  const origin = h("input", {
    type: "url", inputmode: "url", value: location.origin,
    placeholder: "https://harness-web.example.com", spellcheck: "false",
  });
  const minted = h("div");
  const mint = async () => {
    let approvedOrigin;
    try { approvedOrigin = new URL(origin.value).origin; }
    catch (_) { toast("Enter Agent Harness Web's complete origin"); return; }
    try {
      const key = await api("/keys", { method: "POST", surface: "admin", body: {
        name: `agent-harness-web (${new URL(approvedOrigin).host})`, kind: "owner", scopes: ["admin"], origins: [approvedOrigin],
      } });
      const field = h("input", { type: "text", readonly: true, value: key.key, onclick: (e) => e.target.select() });
      fill(minted,
        h("p", { class: "note" }, "Copy this token now; it is not shown again."), field,
        h("button", { class: "btn small", onclick: async () => {
          void copyToClipboard(key.key, () => field.select());
        } }, "Copy token"));
    } catch (e) { toast(e.message, 5000); }
  };
  return h("div", {},
    h("div", { class: "card" },
      h("h3", {}, "This Agent Harness Web"),
      h("p", { class: "muted small" }, "Leave the Agent Harness Server URL blank when this Web UI is bundled with the Server. For a separately hosted copy, enter the Server URL and an owner token approved for this Web origin."),
      h("label", {}, "Agent Harness Server URL"), server,
      h("label", {}, "Owner token"), token,
      h("p", { class: "muted small" }, "The token is stored only in this browser. Do not use an Agent Harness App token; Agent Harness Web manages owner-only settings."),
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "btn", onclick: () => { server.value = ""; token.value = ""; void save(); } }, "Use bundled Server"),
        h("span", { class: "spacer" }),
        h("button", { class: "btn primary", onclick: save }, "Save and test")), status),
    h("div", { class: "card" },
      h("h3", {}, "Connect another Agent Harness Web"),
      h("p", { class: "muted small" }, "Open this bundled copy as the owner, then mint an origin-bound token for a separately hosted copy. Revocation is available under Settings → Apps."),
      h("label", {}, "Agent Harness Web origin"), origin,
      h("button", { class: "btn", onclick: mint }, "Create owner token"), minted));
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
    h("p", { class: "muted small" }, "How the app looks on this phone. The profile icon — the emoji next to your name — lives on the Profile card."),
    grid, hueRow,
    h("p", { class: "section-label" }, "Text size"),
    h("p", { class: "muted small" }, "This phone only, like the theme. Session list, transcript, Settings, and the header all follow it."),
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
    me.notify.enabled ? h("button", {
      class: "btn",
      onclick: async () => {
        try { await api("/notify/test", { method: "POST" }); toast("Test notification sent"); } catch (e) { toast(e.message); }
      },
    }, "Send test notification") : null);
}

function installCard() {
  return h("div", { class: "card" },
    h("p", {}, "Install Agent Harness Web in Safari: Share → Add to Home Screen. It appears as Harness and opens full screen."),
    h("p", { class: "muted small" }, "An existing iPhone or iPad icon may keep its previous label until you remove it and add Agent Harness Web to the Home Screen again."));
}

function backendUsage(b) {
  if (b.name === "local") return "Local Qwen on this PC";
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
  const setMode = async (mode) => {
    try {
      data = await api("/smart-approvals", { method: "PUT", body: { mode } });
      toast(mode === "off" ? "Smart approvals off" : `Smart approvals ${mode}`);
      go("#/profile/smart-approvals");
    } catch (e) { toast(e.message); }
  };
  const configured = data.configured || data.enabled;
  status.textContent = configured
    ? `${data.provider || "provider"} · ${data.model || "model"} · mode ${data.mode}`
    : "Off. The owner enables this in harness.yaml with a hosted API secret reference.";
  const modeButtons = configured ? [
    h("button", { class: "btn", onclick: () => setMode("shadow") }, "Shadow"),
    h("button", { class: "btn", onclick: () => setMode("auto") }, "Auto"),
  ] : [];
  const buttons = isGuest() ? h("p", { class: "muted small" }, "Demo access cannot change smart approvals.")
    : h("div", { class: "row", style: "margin-top:10px; gap:8px; flex-wrap:wrap" },
        modeButtons,
        h("button", { class: "btn", onclick: () => setMode("off") }, "Off"));
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
      status, stats, buttons),
    recent.length ? h("div", { class: "card" }, h("h3", {}, "Recent reviews"), ...recent) : null);
}

async function backendsCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Checking…"));
  let failed = false;
  const [rows, models] = await Promise.all([api("/backends?auth=skip"), api("/models")]).catch((e) => {
    fill(body, h("p", { class: "note bad" }, e.message));
    failed = true;
    return [[], []];
  });
  if (failed) {
    return h("div", { class: "card" }, body);
  }
  const effortSelect = (b) => {
    const sel = h("select", {}, ["low", "medium", "high"].map((level) =>
      h("option", { value: level, selected: (b.effort || "high") === level }, level)));
    sel.addEventListener("change", async () => {
      try { await api(`/backends/${b.name}`, { method: "PUT", body: { effort: sel.value } }); toast(`Saved ${b.name} effort`); }
      catch (e) { toast(e.message); }
    });
    return sel;
  };
  const modelControl = (b) => {
    if (b.name === "local") {
      const sel = h("select", {}, models.map((m) => h("option", { value: m.name, selected: m.name === b.model }, m.name)));
      sel.addEventListener("change", async () => {
        try { await api("/backends/local", { method: "PUT", body: { model: sel.value } }); toast("Saved local model"); }
        catch (e) { toast(e.message); }
      });
      return sel;
    }
    const popular = b.popular_models || [];
    const save = async (model) => {
      if (!model) return toast("Model is empty");
      try { await api(`/backends/${b.name}`, { method: "PUT", body: { model } }); toast(`Saved ${b.name} model`); }
      catch (e) { toast(e.message); }
    };
    if (!popular.length) {
      const input = h("input", { type: "text", value: b.model || "", placeholder: "Provider model name" });
      input.addEventListener("change", () => save(input.value.trim()));
      return input;
    }
    const known = popular.some((m) => m.id === b.model);
    const sel = h("select", {},
      popular.map((m) => h("option", { value: m.id, selected: m.id === b.model }, m.label || m.id)),
      h("option", { value: "__custom__", selected: !known }, "Custom"));
    const input = h("input", {
      type: "text", value: known ? "" : (b.model || ""),
      placeholder: "Model name the CLI accepts", hidden: known,
    });
    sel.addEventListener("change", () => {
      const custom = sel.value === "__custom__";
      input.hidden = !custom;
      if (custom) input.focus();
      else void save(sel.value);
    });
    input.addEventListener("change", () => save(input.value.trim()));
    return [sel, input];
  };
  const BACKEND_TITLES = { local: "Qwen (this PC)", claude: "Claude", codex: "Codex", cursor: "Cursor" };
  const title = (b) => BACKEND_TITLES[b.name] || b.name;
  const usage = {};
  fill(body, rows.length ? rows.map((b) => {
    const line = h("p", { class: `small ${b.billing_warning ? "bad" : "muted"}` }, b.billing_warning || backendUsage(b));
    usage[b.name] = line;
    return h("div", { class: "backend-block" },
      h("strong", {}, title(b)),
      line,
      isGuest() ? null : h("label", {}, "Default model"),
      isGuest() ? null : modelControl(b),
      isGuest() || b.name === "local" ? null : h("label", {}, "Effort"),
      isGuest() || b.name === "local" ? null : effortSelect(b));
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
    body);
}

async function skillProposalView(pid) {
  const p = await api(`/skills/proposals/${pid}`);
  const findings = (p.static_findings || []).map((f) => h("li", {}, `${f.code}: ${f.message}`));
  const review = p.review || {};
  const examples = (p.examples || []).map((ex) => h("div", { class: "card" },
    h("p", {}, ex.prompt), h("p", { class: "muted small" }, ex.expected || ex.expected_behavior || "")));
  const refs = (p.references || []).map((r) => h("details", {}, h("summary", {}, r.path), h("pre", {}, r.content || "")));
  const act = async (path, body, label) => {
    if (label && !confirm(label)) return;
    try {
      await api(path, { method: "POST", body: body || {} });
      toast("Done");
      go("#/profile/skills", true);
    } catch (e) { toast(e.message); }
  };
  return h("div", {},
    h("div", { class: "card" },
      h("h3", {}, p.title || p.slug),
      h("p", { class: "muted small" }, `${p.slug} · ${p.status} · hash ${p.content_hash} · session ${p.source_session_id || "—"}`),
      h("p", {}, p.purpose || ""),
      p.activation_suggestion ? h("p", { class: "muted small" }, `Suggested when: ${p.activation_suggestion}`) : null,
      p.diff ? h("pre", { class: "preview" }, p.diff) : null,
      h("p", { class: "section-label" }, "SKILL.md"),
      h("pre", {}, p.skill_md || ""),
      refs.length ? h("p", { class: "section-label" }, "References") : null, ...refs,
      h("p", { class: "section-label" }, "Examples"), ...examples,
      h("p", { class: "section-label" }, "Static findings"),
      findings.length ? h("ul", {}, findings) : h("p", { class: "muted small" }, "No static findings."),
      h("p", { class: "section-label" }, "Model review"),
      h("p", { class: "muted small" }, p.review_status || "not started"),
      review.summary ? h("p", {}, review.summary) : null,
      review.recommendation ? h("p", {}, `Recommendation: ${review.recommendation}`) : null,
      review.error ? h("p", { class: "bad" }, review.error) : null,
      h("div", { class: "row", style: "margin-top:18px;flex-wrap:wrap;gap:8px" },
        h("button", { class: "btn primary", onclick: () => act(`/skills/proposals/${p.id}/install`, { content_hash: p.content_hash },
          `Install hash ${p.content_hash.slice(0, 12)}? It stays disabled until you enable it.`) }, "Install"),
        h("button", { class: "btn", onclick: () => act(`/skills/proposals/${p.id}/reject`, { reason: "rejected from Skills page" }, "Reject this hash?") }, "Reject"),
        h("button", { class: "btn", onclick: () => act(`/skills/proposals/${p.id}/review`) }, "Run hosted review"),
        h("button", { class: "btn bad", onclick: async () => {
          if (!confirm("Delete this draft?")) return;
          try { await api(`/skills/proposals/${p.id}`, { method: "DELETE" }); go("#/profile/skills", true); }
          catch (e) { toast(e.message); }
        } }, "Delete draft"))));

}

function installedSkillCard(sk) {
  const toggle = sk.enabled ? "disable" : "enable";
  return h("div", { class: "card" },
    h("h3", {}, `${sk.enabled ? "" : "⏸ "}${sk.title || sk.slug}`),
    h("p", { class: "muted small" }, `${sk.slug} v${sk.version} · ${(sk.content_hash || "").slice(0, 12)}`),
    h("p", {}, sk.purpose || ""),
    sk.projects?.length ? h("p", { class: "muted small" }, `Projects: ${sk.projects.join(", ")}`) : h("p", { class: "muted small" }, "No project allowlist. Enable it and pick it on New task."),
    h("div", { class: "row", style: "flex-wrap:wrap;gap:8px" },
      h("button", { class: "btn small", onclick: async () => {
        try { await api(`/skills/${sk.slug}/${toggle}`, { method: "POST" }); void route(); } catch (e) { toast(e.message); }
      } }, sk.enabled ? "Disable" : "Enable"),
      h("button", { class: "btn small", onclick: async () => {
        const raw = window.prompt("Project allowlist (comma-separated names)", (sk.projects || []).join(", "));
        if (raw === null) return;
        try {
          await api(`/skills/${sk.slug}/projects`, { method: "PUT", body: { projects: raw.split(",").map((s) => s.trim()).filter(Boolean) } });
          void route();
        } catch (e) { toast(e.message); }
      } }, "Projects"),
      h("button", { class: "btn small", onclick: async () => {
        if (!confirm("Roll back to the previous version?")) return;
        try { await api(`/skills/${sk.slug}/rollback`, { method: "POST" }); void route(); } catch (e) { toast(e.message); }
      } }, "Rollback"),
      h("button", { class: "btn small bad", onclick: async () => {
        if (!confirm(`Uninstall ${sk.slug}? Later sessions will not receive it.`)) return;
        try { await api(`/skills/${sk.slug}/uninstall`, { method: "POST" }); void route(); } catch (e) { toast(e.message); }
      } }, "Uninstall")));
}

async function skillsPage(pid) {
  const data = await api("/skills");
  if (!data.enabled) {
    return h("div", { class: "card" }, h("p", { class: "muted small" }, "Instruction skills are disabled. Ordinary sessions are unchanged."));
  }
  if (pid) return skillProposalView(pid);
  const proposals = (data.proposals || []).map((p) => h("a", { class: "card", href: `#/profile/skills/${p.id}` },
    h("h3", {}, p.title || p.slug),
    h("div", { class: "meta" }, h("span", {}, p.status), h("span", {}, p.review_status || "no review"),
      h("span", {}, (p.content_hash || "").slice(0, 12)))));
  const installed = (data.installed || []).map(installedSkillCard);
  return h("div", {},
    h("p", { class: "muted small" }, "Agents can only stage drafts. You install an exact hash; new skills stay off until you enable them. Advisory model review never installs."),
    data.hosted_reviewer_configured ? h("p", { class: "muted small" }, "Hosted review is configured and spends that provider's quota only when you tap Run hosted review.") : h("p", { class: "muted small" }, "No hosted reviewer configured. Local Qwen review runs only when the GPU is idle."),
    h("p", { class: "section-label" }, "Proposals"),
    proposals.length ? proposals : h("p", { class: "empty" }, "No proposals yet."),
    h("p", { class: "section-label" }, "Installed"),
    installed.length ? installed : h("p", { class: "empty" }, "No installed skills."));
}

function memoryCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Loading…"));
  void (async () => {
    try {
      const mem = await api("/memory");
      if (!mem.enabled) return fill(body, h("p", { class: "muted small" }, "The memory library is disabled in config/harness.yaml."));
      const editor = h("textarea", { class: "memory-editor", rows: 24, readOnly: isGuest() || !mem.writes }, mem.profile || "");
      const save = h("button", { class: "btn primary", disabled: !mem.writes || isGuest() }, "Save");
      save.addEventListener("click", async () => {
        if (!confirm("Save this profile to the memory library? It is given to every new session, then committed and pushed.")) return;
        save.disabled = true;
        try {
          const saved = await api("/memory/profile", { method: "PUT", body: { content: editor.value, summary: "Update agent profile from Settings" } });
          toast(`Saved (${saved.last_commit.head})`);
        } catch (e) { toast(e.message); }
        save.disabled = !mem.writes;
      });
      const memoryProfileAction = () => {
        if (isGuest()) return h("p", { class: "muted small" }, "Demo access cannot edit the memory profile.");
        return mem.writes ? save : h("p", { class: "muted small" }, "Enable memory_library.writes to edit from here.");
      };
      fill(body,
        h("p", { class: "small" }, "A short purpose statement given to every new session: how agents should use this library. Keep personal biography in category files, not here."),
        h("p", { class: "small" }, `Agents can read ${mem.categories.join(", ")}. `,
          mem.writes ? "They can propose changes; every change asks you first." : "Read-only for agents."),
        editor,
        h("p", { class: "muted small" }, `${mem.profile_path || "profile"} · limit ${mem.profile_max_chars} characters`),
        memoryProfileAction(),
        mem.last_commit?.head ? h("p", { class: "muted small" }, `Last saved change: ${mem.last_commit.summary} (${mem.last_commit.head}, ${ago(mem.last_commit.at)})`) : null,
        mem.refresh_error ? h("p", { class: "small bad" }, `Couldn't refresh the library: ${mem.refresh_error}`) : null);
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  })();
  return h("div", { class: "card" }, body);
}

// Shows a secret exactly once (lib/secret.mjs), copying through this page's clipboard helper.
const showSecretOnce = (form, load, intro, secret, copyLabel) => showSecret(form, load, intro, secret, copyLabel, copyToClipboard);

function endpointCard(me) {
  const base = me.public_url || location.origin;
  const body = h("div", {}, h("p", { class: "muted small" }, "Loading…"));
  const load = async () => {
    try {
      const keys = await api("/keys");
      const active = keys.filter((k) => !k.revoked_at && k.kind !== "app");
      const form = h("div");
      const newBtn = h("button", { class: "btn", type: "button", onclick: () => { newBtn.hidden = true; showForm(); } }, "New key");
      const showForm = () => {
        const name = h("input", { type: "text", placeholder: "Name (the device or app)" });
        fill(form,
          h("p", { class: "small" }, "The token is shown once. Anyone with it can call the inference endpoint as you."),
          h("label", {}, "Key name"), name,
          h("div", { class: "row", style: "margin-top:10px" },
            h("button", { class: "btn", type: "button", onclick: load }, "Cancel"),
            h("span", { class: "spacer" }),
            h("button", {
              class: "btn primary", type: "button",
              onclick: async () => {
                if (!name.value.trim()) return toast("Name the key");
                try {
                  const k = await api("/keys", { method: "POST", body: { name: name.value } });
                  showSecretOnce(form, load, `Key for ${k.name}. Copy it now; it isn't shown again.`, k.key, "Copy");
                } catch (e) { toast(e.message); }
              },
            }, "Create")));
      };
      fill(body,
        h("p", { class: "muted small" }, "Point a coding tool at these URLs. Tap a box to copy. Any model name works; unknown names use the default."),
        h("p", { class: "small", style: "margin-bottom:0" }, "OpenAI-compatible"),
        copyBox(`${base}/v1`),
        h("p", { class: "small", style: "margin-bottom:0" }, "Anthropic-compatible"),
        copyBox(base),
        active.length ? h("ul", { class: "small" }, active.map((k) => h("li", {},
          h("strong", {}, k.name), ` ${k.prefix}… · ${pluralize(k.requests, "request")}${usedSuffix(k)} `,
          h("button", {
            class: "btn small bad",
            onclick: async () => {
              if (!confirm(`Revoke the key “${k.name}”? Tools using it stop working. Any sessions it started, and their files, are erased in ${ERASE_GRACE_DAYS} days unless you undo it under Apps.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); void load(); } catch (e) { toast(e.message); }
            },
          }, "Revoke")))) : h("p", { class: "muted small" }, "No keys yet."),
        form, newBtn);
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  void load();
  return h("div", { class: "card" }, body);
}

const APP_SCOPES = {
  sessions: "Start and follow its own sessions (with context and tools)",
  "sessions:all": "Read all sessions, not only its own",
  approvals: "Approve or deny in its own sessions",
  images: "Generate images",
  inference: "Use the inference endpoint",
  remote_control: "Start and stop Claude Remote Control in a project folder",
  "models:warm": "Start loading the local model ahead of a chat",
  memory_library: "Read the memory library and propose edits to it",
  homelab: "Use homelab tools (logs, service config, metrics) in homelab projects",
};

function appsCard(me) {
  const base = me.public_url || location.origin;
  const body = h("div", {}, h("p", { class: "muted small" }, "Loading…"));
  const load = async () => {
    try {
      const [keys, pairingCodes, runnerPairingCodes, runners] = await Promise.all([
        api("/keys"), api("/pairing-codes"), api("/runner-pairing-codes"), api("/runners"),
      ]);
      const apps = keys.filter((k) => k.kind === "app" && !k.revoked_at);
      const erasing = keys.filter((k) => k.kind !== "owner" && k.erase_after && !k.erased_at);
      const ownerConnections = keys.filter((k) => k.kind === "owner" && !k.revoked_at);
      const webConnections = ownerConnections.filter((k) => k.origins?.length);
      const cliConnections = ownerConnections.filter((k) => !k.origins?.length);
      const pending = pairingCodes.filter((p) => !p.used_at && p.expires_at > Date.now() / 1000);
      const pendingRunners = runnerPairingCodes.filter((p) => !p.used_at && p.expires_at > Date.now() / 1000);
      const form = h("div");
      const newBtn = h("button", { class: "btn", type: "button", onclick: () => {
        newBtn.hidden = true; pairBtn.hidden = true; macBtn.hidden = true; showForm();
      } }, "New app");
      const pairBtn = h("button", { class: "btn", type: "button", onclick: () => {
        newBtn.hidden = true; pairBtn.hidden = true; macBtn.hidden = true; showPairForm();
      } }, "Pair browser app");
      const macBtn = h("button", { class: "btn", type: "button", hidden: !runners.length, onclick: () => {
        newBtn.hidden = true; pairBtn.hidden = true; macBtn.hidden = true; showMacPairForm();
      } }, "Pair Agent Harness for Mac");
      const showForm = () => {
        const name = h("input", { type: "text", placeholder: "App name" });
        const boxes = Object.entries(APP_SCOPES).map(([scope, label]) => h("label", { class: "small", style: "display:block;font-weight:normal" },
          h("input", { type: "checkbox", value: scope, checked: scope === "sessions" }), ` ${label}`));
        fill(form,
          h("p", { class: "small" }, "This mints a token shown once. Anyone with it can use the permissions you tick. It is not your Claude/Codex/Cursor login."),
          h("label", {}, "Name"), name, h("label", {}, "What it may do"), boxes,
          h("div", { class: "row", style: "margin-top:10px" },
            h("button", { class: "btn", onclick: load }, "Cancel"), h("span", { class: "spacer" }),
            h("button", {
              class: "btn primary",
              onclick: async () => {
                const scopes = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
                if (!name.value.trim() || !scopes.length) return toast("Name the app and allow at least one thing");
                try {
                  const k = await api("/keys", { method: "POST", body: { name: name.value, kind: "app", scopes } });
                  showSecretOnce(form, load, `Token for ${k.name}. Copy it now; it isn't shown again.`, k.key, "Copy");
                } catch (e) { toast(e.message); }
              },
            }, "Create")));
      };
      const showPairForm = () => {
        const name = h("input", { type: "text", placeholder: "App name" });
        const origin = h("input", { type: "url", placeholder: "https://app.example.com" });
        const boxes = Object.entries(APP_SCOPES).map(([scope, label]) => h("label", { class: "small", style: "display:block;font-weight:normal" },
          h("input", { type: "checkbox", value: scope, checked: scope === "sessions" }), ` ${label}`));
        fill(form,
          h("p", { class: "small" }, "Approve one exact browser origin. The short-lived code is shown once and can only be redeemed from that origin."),
          h("label", {}, "Name"), name,
          h("label", {}, "Browser origin (scheme and host only)"), origin,
          h("label", {}, "What it may do"), boxes,
          h("div", { class: "row", style: "margin-top:10px" },
            h("button", { class: "btn", onclick: load }, "Cancel"), h("span", { class: "spacer" }),
            h("button", { class: "btn primary", onclick: async () => {
              const scopes = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
              if (!name.value.trim() || !origin.value.trim() || !scopes.length) return toast("Name the app, enter its origin, and allow at least one thing");
              try {
                const p = await api("/pairing-codes", { method: "POST", body: { name: name.value, origin: origin.value, scopes } });
                showSecretOnce(form, load, `Pairing code for ${p.name} at ${p.origin}. It expires in 10 minutes and works once.`, p.code, "Copy");
              } catch (e) { toast(e.message); }
            } }, "Approve and create code")));
      };
      const showMacPairForm = () => {
        const name = h("input", { type: "text", value: "Agent Harness for Mac", placeholder: "Mac connection name" });
        const runner = h("select", {}, runners.map((item) => h("option", { value: item.name }, item.name)));
        fill(form,
          h("p", { class: "small" }, "Create a 10-minute, one-use code. The install command sets up the harness CLI, runner, and launchd without SSH."),
          h("label", {}, "Name"), name,
          h("label", {}, "Runner"), runner,
          h("div", { class: "row", style: "margin-top:10px" },
            h("button", { class: "btn", onclick: load }, "Cancel"), h("span", { class: "spacer" }),
            h("button", { class: "btn primary", onclick: async () => {
              if (!name.value.trim()) return toast("Name this Agent Harness for Mac connection");
              try {
                const p = await api("/runner-pairing-codes", { method: "POST", body: { name: name.value, runner: runner.value } });
                const command = `curl -fsSL ${base}/mac-client/install.sh | bash -s -- --server ${base} --code ${p.code}`;
                showSecretOnce(form, load, "Run this in Terminal on the Mac. The code expires in 10 minutes and works once.", command, "Copy install command");
              } catch (e) { toast(e.message); }
            } }, "Create install command")));
      };
      fill(body,
        h("p", { class: "small" }, "An Agent Harness App token lets a third-party integration start and follow sessions on Agent Harness Server. It is shown once and can be revoked later."),
        h("p", { class: "muted small" }, "API: ", h("code", {}, `${base}/api/v1`), " · guide: docs/app-api.md"),
        apps.length ? h("ul", { class: "small" }, apps.map((k) => h("li", {},
          h("strong", {}, k.name), ` ${k.prefix}… · ${k.scopes.replaceAll(" ", ", ")}${originsSuffix(k)}${usedSuffix(k)} `,
          h("button", {
            class: "btn small bad",
            onclick: async () => {
              if (!confirm(`Revoke the app “${k.name}”? It can no longer start or read sessions, and its sessions and files are erased in ${ERASE_GRACE_DAYS} days unless you undo the revoke here.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); void load(); } catch (e) { toast(e.message); }
            },
          }, "Revoke"),
          storeLine(k) ? h("div", { class: "muted small" }, storeLine(k)) : null))) : h("p", { class: "muted small" }, "No apps yet."),
        erasing.length ? [h("p", { class: "section-label" }, "Revoked: data to be erased"),
          h("ul", { class: "small" }, erasing.map((k) => h("li", {},
            h("strong", {}, k.name), ` · its sessions and files are erased on ${eraseDate(k)} `,
            h("button", { class: "btn small", type: "button", onclick: async () => {
              if (!confirm(`Undo revoking “${k.name}”? Its sessions and files are kept, and it gets a new token.`)) return;
              try {
                const r = await api(`/apps/${k.id}/restore`, { method: "POST" });
                showSecretOnce(form, load, `New token for ${r.name}. Copy it now; it isn't shown again. The old token stays revoked.`, r.key, "Copy");
              } catch (e) { toast(e.message); }
            } }, "Undo"))))] : null,
        webConnections.length ? [h("p", { class: "section-label" }, "Web connections"),
          h("ul", { class: "small" }, webConnections.map((k) => h("li", {},
            h("strong", {}, k.name), ` ${k.prefix}… · ${k.origins?.join(", ") || "non-browser"}${usedSuffix(k)} `,
            h("button", { class: "btn small bad", onclick: async () => {
              if (!confirm(`Revoke “${k.name}”? That Agent Harness Web connection will stop working.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); void load(); } catch (e) { toast(e.message); }
            } }, "Revoke"))))] : null,
        cliConnections.length ? [h("p", { class: "section-label" }, "CLI connections"),
          h("ul", { class: "small" }, cliConnections.map((k) => h("li", {},
            h("strong", {}, k.name), ` ${k.prefix}… · non-browser${usedSuffix(k)} `,
            h("button", { class: "btn small bad", onclick: async () => {
              if (!confirm(`Revoke “${k.name}”? That Agent Harness CLI connection will stop working.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); void load(); } catch (e) { toast(e.message); }
            } }, "Revoke"))))] : null,
        pending.length ? h("ul", { class: "small" }, pending.map((p) => h("li", {},
          `Pairing pending for ${p.name} at ${p.origin} · expires ${new Date(p.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} `,
          h("button", { class: "btn small bad", onclick: async () => {
            try { await api(`/pairing-codes/${p.id}`, { method: "DELETE" }); void load(); } catch (e) { toast(e.message); }
          } }, "Cancel")))) : null,
        pendingRunners.length ? h("ul", { class: "small" }, pendingRunners.map((p) => h("li", {},
          `Mac pairing pending for ${p.name} (${p.runner}) Â· expires ${new Date(p.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} `,
          h("button", { class: "btn small bad", onclick: async () => {
            try { await api(`/runner-pairing-codes/${p.id}`, { method: "DELETE" }); void load(); } catch (e) { toast(e.message); }
          } }, "Cancel")))) : null,
        form, h("div", { class: "row", style: "margin-top:10px" }, newBtn, pairBtn, macBtn));
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  void load();
  return h("div", { class: "card" }, body);
}

return { viewProfile, copyBox, githubConnectionCard, readAppIcon, applyAppIcon, applyTheme, applyTextSize };
}
