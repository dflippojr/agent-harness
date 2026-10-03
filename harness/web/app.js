// Agent Harness web app: plain ES module, no build step. Hash routes:
//   #/                       redirects to #/chat (owner) or #/agents
//   #/chat[/<id>]            Chat home: welcome state, or a durable non-agent conversation
//   #/agents                 agent session list
//   #/new                    new task (templates)
//   #/s/<id>                 session transcript (live)
//   #/s/<id>/approval/<aid>  same, focused on one approval (notification deep link)
//   #/s/<id>/changes         diff viewer
//   #/s/<id>/info            session details
//   #/actions[/<tab>]        owner actions: resources, accounts, remote-control, disk
//   #/profile                identity plus Settings menu
//   #/profile/account        icon picker, account info, connection details
//   #/profile/<section>      a Settings page (appearance, notifications, backends, …)
//   #/profile/{accounts,disk,remote-control} redirect to #/actions/<tab>
//   #/images                 image generation and gallery
//   #/images/<id>            one result (prompt, metadata, Another one)
//   #/images/<id>/edit       masked inpainting / photo edit
//   #/images/<id>/full       in-app fullscreen viewer
//   #/jobs[/new|/<id>]       scheduled jobs
//   #/signin[/failed]        Google sign-in for a household member on a Tailscale-admitted device (issue #64)

import { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } from "./client.mjs";
import { SNIPPET_LANGUAGES, snippetLanguage } from "./lib/snippets.mjs";
import { md } from "./lib/markdown.mjs";
import { lastUpdateText, compatibilityText, lastSeenText } from "./lib/settings-text.mjs";
import { profileIconHidden, sessionJumpHidden } from "./lib/layout.mjs";
import { protocolMismatch } from "./lib/compat.mjs";
import { mountImages } from "./pages/images.mjs";
import { mountJobs } from "./pages/jobs.mjs";
import { mountActions } from "./pages/actions.mjs";
import { mountSessionInfo } from "./pages/session-info.mjs";
import { mountDaemonSettings } from "./pages/daemon-settings.mjs";
import { mountProfile } from "./pages/profile.mjs";
import { mountNewTask } from "./pages/new-task.mjs";
import { mountSessions } from "./pages/sessions.mjs";
import { mountSession } from "./pages/session.mjs";

const $app = document.getElementById("app");
const $title = document.getElementById("title");
const $back = document.getElementById("back");
const $conn = document.getElementById("conn");
const $feature = document.getElementById("feature-nav");
const $profileIcon = document.getElementById("profile-icon");
const $fabHost = document.getElementById("fab-host");
const $fab = document.getElementById("fab");
const TERMINAL = new Set(["done", "failed", "cancelled"]);
const STATUS_LABEL = {
  queued: "queued", running: "running", waiting_approval: "needs approval", waiting_target: "waiting for Mac", waiting_app: "waiting for app", waiting_limit: "waiting for limit",
  done: "done", failed: "failed", cancelled: "cancelled",
};
const REVIEW_LABEL = { merged: "merged", pushed: "pushed", discarded: "discarded" };
const progressBar = (fraction) => h("div", { class: `progress${fraction === null ? " indeterminate" : ""}` },
  h("span", { style: fraction === null ? "" : `width:${Math.max(2, Math.min(100, fraction * 100)).toFixed(1)}%` }));
let cleanup = [];
const onLeave = (fn) => cleanup.push(fn);
let protocolBlocked = false;

function layoutBar() {
  const bar = document.getElementById("bar");
  const chrome = document.querySelector(".session-chrome");
  if (bar) document.documentElement.style.setProperty("--bar-h", `${bar.offsetHeight}px`);
  document.documentElement.style.setProperty("--session-chrome-h", `${chrome ? chrome.offsetHeight : 0}px`);
}

let barPaintFrame = 0;
let barPaintPhase = false;
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
let pagePaintFrame = 0;
let pagePaintPhase = false;
function repaintPage() {
  cancelAnimationFrame(pagePaintFrame);
  pagePaintFrame = requestAnimationFrame(() => {
    pagePaintPhase = !pagePaintPhase;
    $app.classList.toggle("paint-refresh", pagePaintPhase);
    const { y, viewH, pageH } = pageMetrics(); // the cross-engine measurements the jump buttons use
    if (y > pageH - viewH) scrollPage(pageH - viewH);
  });
}

function setHeader(feature, pageTitle = "", { page = false } = {}) {
  if ([...$feature.options].some((o) => o.value === feature)) $feature.value = feature;
  $feature.hidden = true;
  $profileIcon.hidden = profileIconHidden($back.hidden, page);
  $title.textContent = pageTitle;
  $title.hidden = !pageTitle;
  document.getElementById("bar").classList.toggle("page", page);
  repaintBar();
}
window.addEventListener("resize", repaintBar);
window.addEventListener("orientationchange", repaintBar);
window.addEventListener("pageshow", repaintBar);
document.addEventListener("visibilitychange", () => { if (!document.hidden) repaintBar(); });
window.addEventListener("pageshow", repaintPage);
document.addEventListener("visibilitychange", () => { if (!document.hidden) repaintPage(); });

async function loadProfileIcon() {
  if (protocolBlocked) return;
  try { $profileIcon.textContent = (await api("/profile")).emoji; } catch (_) { /* offline */ }
}

// ---------- utilities ----------
const isEmptyChild = (c) => c === null || c === undefined || c === false;
function setAttr(el, k, v) {
  if (k === "class") el.className = v;
  else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
  else if (k === "html") el.innerHTML = v;
  else el.setAttribute(k, v === true ? "" : v);
}
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    setAttr(el, k, v);
  }
  for (const c of children.flat()) {
    if (isEmptyChild(c)) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Native append/replaceChildren stringify null as "null" and arrays via toString()
// (an anchor becomes its href, so a list of links becomes comma-joined URLs).
function kids(...children) {
  return children.flat().filter((c) => c !== null && c !== undefined && c !== false);
}
function fill(el, ...children) {
  el.replaceChildren(...kids(...children));
  return el;
}
function append(el, ...children) {
  const nodes = kids(...children);
  if (nodes.length) el.append(...nodes);
  return el;
}

function showFab(href, label) {
  if (isGuest()) return;
  $fab.href = href;
  $fab.textContent = label;
  $fabHost.hidden = false;
}

let currentMe = { role: "owner" };
// Resolves (never rejects) to the caller's identity without touching app state, so boot can start it
// speculatively beside /health and only adopt the result once compatibility has passed.
async function fetchMe() {
  const bootstrap = !agentHarnessWeb.token && !agentHarnessWeb.independent ? "legacy" : "admin";
  try {
    return await api("/me", { surface: bootstrap });
  } catch (e) {
    if (e.code === "sign_in_required") return { role: "signin" };
    try { return await api("/me", { surface: "app" }); }
    catch (_) { return { role: "guest" }; }
  }
}
async function currentUser() {
  currentMe = await fetchMe();
  return currentMe;
}
// Identity fetched during boot; the first route() adopts it instead of requesting /me a second time.
let bootMe = null;

// Issue #64: Google sign-in state for bundled, same-origin Web only. The CSRF value stays in memory.
let webAuth = null;
async function loadWebAuth() {
  webAuth = null;
  agentHarnessWeb.csrf = "";
  if (agentHarnessWeb.independent || agentHarnessWeb.token) return null;
  try { webAuth = await agentHarnessWeb.request("/auth/session", { surface: "app" }); }
  catch (_) { return null; }
  agentHarnessWeb.csrf = webAuth.csrf || "";
  return webAuth;
}
function needsSignIn() { return currentMe.role === "signin"; }

const GOOGLE_FAILED = "Google sign-in did not complete. Try again, or ask the owner for a new link code.";

async function startGoogle(mode, code) {
  const body = { mode };
  if (code) body.code = code;
  const started = await api("/auth/google/start", { method: "POST", surface: "app", body });
  location.assign(started.authorization_url);
}

function linkCodeForm(label) {
  const input = h("input", { type: "password", autocomplete: "off", spellcheck: "false",
    placeholder: "Link code from the owner", required: true });
  const submit = h("button", { class: "btn", type: "submit" }, label);
  return h("form", { onsubmit: async (e) => {
    e.preventDefault();
    submit.disabled = true;
    try { await startGoogle("invite", input.value.trim()); }
    catch (err) { toast(err.message, 6000); submit.disabled = false; }
    input.value = "";
  } }, input, h("div", { class: "row", style: "margin-top:8px" }, submit));
}

function viewSignIn(failed) {
  $title.textContent = "Sign in";
  const available = !!webAuth?.google?.available;
  const button = h("button", { class: "btn primary", type: "button", onclick: async () => {
    button.disabled = true;
    try { await startGoogle("signin"); } catch (e) { toast(e.message, 6000); button.disabled = false; }
  } }, "Sign in with Google");
  fill($app, h("div", { class: "card" },
    h("h3", {}, "Household sign-in"),
    failed ? h("p", { class: "note bad" }, GOOGLE_FAILED) : null,
    available ? h("p", { class: "muted small" }, webAuth.google.explanation) : null,
    available ? h("div", { class: "row" }, button)
      : h("p", { class: "muted small" }, "Google sign-in is not available on this server. Ask the owner."),
    available ? h("p", { class: "muted small", style: "margin-top:16px" },
      "First time on this device? Enter the one-time link code the owner gave you.") : null,
    available ? linkCodeForm("Link with Google") : null));
}
function isGuest() { return currentMe.role === "guest"; }
function isMember() { return currentMe.role === "member"; }
function isOwner() { return currentMe.role === "owner"; }

function paintGuestChrome() {
  const banner = document.getElementById("guest-banner");
  const guest = isGuest();
  const member = isMember();
  document.documentElement.classList.toggle("guest", guest);
  document.documentElement.classList.toggle("member", member);
  if ($feature) {
    for (const opt of $feature.options) {
      if (opt.value === "jobs" || opt.value === "images") opt.hidden = member || guest;
    }
    if (member && ($feature.value === "jobs" || $feature.value === "images")) $feature.value = "agents";
  }
  if (!banner) return;
  if (!guest) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  const until = currentMe.guest_until;
  const when = until ? new Date(until) : null;
  const ends = when && !Number.isNaN(when.getTime())
    ? ` Ends ${when.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}.`
    : "";
  banner.textContent = `Demo access — look around only.${ends}`;
  banner.hidden = false;
}

function toast(text, ms = 2600) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, ms);
}

function apiSurface(path, method) {
  if (isGuest() && !agentHarnessWeb.token) return "legacy";
  if (isMember()) return "app";
  const route = path.split("?")[0];
  if (route === "/sessions" && (method === "GET" || method === "POST")) return "app";
  if (/^\/sessions\/[^/]+$/.test(route) && method === "GET") return "app";
  if (/^\/sessions\/[^/]+\/(messages|cancel)$/.test(route) && method === "POST") return "app";
  if (/^\/sessions\/[^/]+\/approvals\/[^/]+$/.test(route) && method === "POST") return "app";
  return "admin";
}

async function api(path, { method = "GET", body, surface } = {}) {
  if (protocolBlocked) {
    const err = new Error("Update required");
    err.code = "client_update_required";
    throw err;
  }
  return agentHarnessWeb.request(path, { method, body, surface: surface || apiSurface(path, method) });
}

function ownerSurface() {
  if (isMember()) return "app";
  if (isGuest() && !agentHarnessWeb.token) return "legacy";
  return "admin";
}

function daemonImage(path, attrs = {}) {
  const img = h("img", { ...attrs, alt: attrs.alt || "" });
  if (protocolBlocked) return img;
  if (!agentHarnessWeb.token) {
    img.src = agentHarnessWeb.url(path, ownerSurface());
  } else {
    agentHarnessWeb.blob(path, "admin").then((blob) => {
      const url = URL.createObjectURL(blob);
      img.src = url;
      img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
    }).catch((e) => { img.alt = `${attrs.alt || "Image"} (${e.message})`; });
  }
  return img;
}

async function downloadDaemonFile(path, filename) {
  if (protocolBlocked) return;
  try {
    const blob = await agentHarnessWeb.blob(path, ownerSurface());
    const url = URL.createObjectURL(blob);
    const link = h("a", { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) { toast(e.message); }
}

const reviewBadge = (review, label) => h("span", { class: `badge ${review === "discarded" ? "cancelled" : "done"}` }, label);

function badge(status) {
  return h("span", { class: `badge ${status}` }, STATUS_LABEL[status] || status);
}

function setConnLive(on) {
  $conn.classList.toggle("live", !!on);
}

// Ids come from the URL hash, so only the characters the daemon issues (hex, "-", "_") may reach a request path.
const SAFE_ID = /^[A-Za-z0-9_-]{1,64}$/;
const validId = (id) => typeof id === "string" && SAFE_ID.test(id);
const STREAM_PATH = /^(?:\/api\/v1|\/api\/admin\/v1)?\/(?:(?:sessions|chats)\/[A-Za-z0-9_-]{1,64}\/events|events|queue)$/;
const STREAM_QUERY = /^(?:\?[A-Za-z0-9_=&.-]*)?$/;

// Returns a rebuilt same-origin stream URL, or null when it is not a known API stream path (fail closed).
function safeStreamUrl(url) {
  if (typeof url !== "string" || url.length > 2048) return null;
  const base = agentHarnessWeb.baseUrl || "";
  let rest = url;
  if (base) {
    if (!url.startsWith(`${base}/`)) return null;
    rest = url.slice(base.length);
  }
  if (!rest.startsWith("/") || rest.startsWith("//") || rest.includes("\\")) return null;
  const cut = rest.search(/\?/);
  const path = cut < 0 ? rest : rest.slice(0, cut);
  const query = cut < 0 ? "" : rest.slice(cut);
  if (!STREAM_PATH.test(path) || !STREAM_QUERY.test(query)) return null;
  return `${base}${path}${query}`;
}

// EventSource that survives iOS suspending the app: reconnects from the last seq when visible again.
// Connection-dot updates are opt-in (`indicate`) so page streams can close without a false offline state.
function openStream(urlFor, handlers, { authorized = false, indicate = false } = {}) {
  let es = null;
  let controller = null;
  let closed = false;
  let retry = null;
  let generation = 0;
  const mark = (on) => { if (indicate) setConnLive(on); };
  const dispatch = (block) => {
    let type = "message";
    const data = [];
    for (const line of block.replaceAll("\r", "").split("\n")) {
      if (line.startsWith("event:")) type = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length || !handlers[type]) return;
    // One bad event must not tear down the stream (and force a reconnect); skip it and keep reading.
    try { handlers[type](JSON.parse(data.join("\n"))); }
    catch (e) { console.error(`stream event "${type}" failed`, e); }
  };
  const fetchStream = async (url) => {
    controller = new AbortController();
    const resp = await fetch(url, { headers: agentHarnessWeb.headers(), cache: "no-store", signal: controller.signal });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
    mark(true);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!closed) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.search(/\r?\n\r?\n/)) >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end).replace(/^\r?\n\r?\n/, "");
        if (block && !block.startsWith(":")) dispatch(block);
      }
    }
  };
  const connect = async () => {
    if (closed || protocolBlocked) return;
    const run = ++generation;
    es?.close();
    controller?.abort();
    let url;
    try { url = await urlFor(); }
    catch (_) {
      mark(false);
      if (!closed && run === generation) retry = setTimeout(connect, 3000);
      return;
    }
    url = safeStreamUrl(url);
    if (!url) { mark(false); return; }
    if (authorized && agentHarnessWeb.token) {
      try { await fetchStream(url); } catch (_) { /* retry below */ }
      mark(false);
      if (!closed && run === generation) retry = setTimeout(connect, 3000);
      return;
    }
    const source = new EventSource(url);
    es = source;
    source.onopen = () => mark(true);
    source.onerror = () => {
      mark(false);
      if (source.readyState === EventSource.CLOSED && run === generation) {
        clearTimeout(retry);
        retry = setTimeout(connect, 3000);
      }
    };
    for (const [type, fn] of Object.entries(handlers)) {
      source.addEventListener(type, (msg) => {
        if (msg.data === undefined) return; // the browser's own connection "error" event, handled by onerror
        fn(JSON.parse(msg.data));
      });
    }
  };
  const onVisible = () => { if (!protocolBlocked && document.visibilityState === "visible") void connect(); };
  document.addEventListener("visibilitychange", onVisible);
  void connect();
  return () => {
    closed = true;
    clearTimeout(retry);
    es?.close();
    controller?.abort();
    mark(false);
    document.removeEventListener("visibilitychange", onVisible);
  };
}

function watchDaemonConnection() {
  if (watchDaemonConnection.started) return;
  watchDaemonConnection.started = true;
  openStream(() => agentHarnessWeb.url("/events", ownerSurface()), {}, {
    authorized: !(isGuest() && !agentHarnessWeb.token),
    indicate: true,
  });
}

// ---------- router ----------
const hashParts = () => location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
const isTopLevel = (parts) => parts.length === 0 || parts[0] === "chat" || parts[0] === "actions"
  || (parts.length === 1 && (parts[0] === "agents" || parts[0] === "jobs" || parts[0] === "images"));

const normalizeHash = (hash) => {
  if (!hash || hash === "#" || hash === "#/") return "#/";
  return hash.startsWith("#") ? hash : `#/${hash}`;
};

function go(hash, replace = false) {
  if (protocolBlocked) return;
  const url = normalizeHash(hash);
  const cur = location.hash || "#/";
  const same = url === cur || (url === "#/" && (cur === "" || cur === "#" || cur === "#/"));
  if (same) return;
  if (replace) location.replace(url);
  else location.hash = url;
}

const isProfileRoute = (parts) => parts[0] === "profile" || parts[0] === "settings";
const MEMBER_HIDDEN_PROFILE = new Set(["notifications", "apps", "endpoint", "memory", "remote-control", "backends", "disk", "accounts"]);

// Where a guest or member who asked for a page they may not see should land instead (null = allowed).
function blockedRedirect(parts) {
  const guestBlocked = isGuest() && (
    parts[0] === "new" || (parts[0] === "jobs" && parts[1] === "new")
    || (isProfileRoute(parts) && ["notifications", "apps", "endpoint"].includes(parts[1])));
  if (guestBlocked) return parts[0] === "jobs" ? "#/jobs" : "#/profile";
  const memberBlocked = isMember() && (
    parts[0] === "jobs" || parts[0] === "images"
    || (isProfileRoute(parts) && MEMBER_HIDDEN_PROFILE.has(parts[1])));
  if (!memberBlocked) return null;
  return isProfileRoute(parts) ? "#/profile" : "#/agents";
}

async function routeImages(parts) {
  if (parts[1] && !validId(parts[1])) go("#/images", true);
  else if (parts[1] && parts[2] === "edit") await viewImageEdit(parts[1]);
  else if (parts[1] && parts[2] === "full") await viewImageFull(parts[1]);
  else if (parts[1]) await viewImage(parts[1]);
  else await viewImages();
}

async function routeProfile(parts) {
  // Bookmarks from when these lived under Profile. Non-owners never land on Actions.
  if (!["accounts", "disk", "remote-control"].includes(parts[1])) {
    await viewProfile(parts[1], parts[2]);
  } else if (!isOwner()) go("#/profile", true);
  else go(`#/actions/${parts[1]}`, true);
}

async function routeActions(parts) {
  if (!isOwner()) go(isMember() ? "#/agents" : "#/profile", true);
  else await viewActions(parts[1]);
}

async function routeView(parts) {
  if (parts.length === 0) go(canChat() ? "#/chat" : "#/agents", true);
  else if (parts[0] === "chat") await viewChat(parts[1]);
  else if (parts[0] === "agents") await viewList();
  else if (parts[0] === "new") await viewNew();
  else if (parts[0] === "actions") await routeActions(parts);
  else if (isProfileRoute(parts)) await routeProfile(parts);
  else if (parts[0] === "images") await routeImages(parts);
  else if (parts[0] === "jobs") await (parts[1] ? viewJob(parts[1]) : viewJobs());
  else if (parts[0] === "s" && parts[1]) await viewSession(parts[1], parts[2] || "transcript", parts[3]);
  else if (parts[0] === "signin" && parts[1] === "failed") {
    toast(GOOGLE_FAILED, 6000);
    go(isMember() ? "#/profile/account" : "#/", true);
  }
  else go("#/", true);
}

async function route() {
  if (protocolBlocked) return;
  watchDaemonConnection();
  cleanup.forEach((fn) => { try { fn(); } catch (_) { /* ignore */ } });
  cleanup = [];
  fill($app);
  document.querySelector(".composer")?.remove();
  $fabHost.hidden = true;
  document.querySelectorAll(".jump").forEach((el) => el.remove());
  const prefetched = bootMe;
  bootMe = null;
  const [, me] = await Promise.all([loadWebAuth(), prefetched || fetchMe()]);
  currentMe = me;
  paintGuestChrome();
  const parts = hashParts();
  if (needsSignIn()) {
    $back.hidden = true;
    viewSignIn(parts[0] === "signin" && parts[1] === "failed");
    repaintPage();
    return;
  }
  const images = parts[0] === "images";
  if (!isGuest() && !images && route.onImages && route.imageWarmupStarted) {
    route.imageWarmupStarted = false;
    route.imageWarmupPromise = null;
    api("/images/cooldown", { method: "POST" }).catch(() => {});
  }
  route.onImages = images;
  $back.hidden = isTopLevel(parts);
  $menu.hidden = !$back.hidden;
  const redirect = blockedRedirect(parts);
  if (redirect) { go(redirect, true); return; }
  try {
    await routeView(parts);
  } catch (e) {
    append($app, h("p", { class: "note bad" }, e.message),
      h("a", { class: "btn", href: "#/profile/connection" }, "Connection settings"));
  }
  repaintPage();
}
$back.addEventListener("click", () => {
  if (protocolBlocked) return;
  const parts = hashParts();
  // Session Transcript/Changes/Info are tabs (replaceState), so Back always leaves the session.
  // An approval deep-link is a real subpage of the transcript.
  if (parts[0] === "s" && parts[2] === "approval") go(`#/s/${parts[1]}`, true);
  else history.back();
});
const FEATURE_ROUTES = { jobs: "#/jobs", images: "#/images", chat: "#/chat" };
$feature.addEventListener("change", () => {
  if (protocolBlocked) return;
  go(FEATURE_ROUTES[$feature.value] || "#/agents", true);
});
window.addEventListener("hashchange", route);

// ---------- navigation drawer ----------
const $menu = document.getElementById("menu-btn");
const $drawer = document.getElementById("nav-drawer");
const $scrim = document.getElementById("drawer-scrim");
const $drawerChats = document.getElementById("drawer-chats");
const canChat = () => !isGuest() && !isMember();
let drawerReturnFocus = null;

function drawerFocusable() {
  return [...$drawer.querySelectorAll("a[href], button")].filter((el) => !el.hidden && !el.closest("[hidden]"));
}

function currentSection() {
  const first = hashParts()[0] || "";
  if (first === "s" || first === "new") return "agents";
  if (first === "actions") return "actions";
  return first;
}

let drawerChatsCache = null;
async function refreshDrawerChats() {
  const recent = $drawer.querySelector(".drawer-recent");
  if (!canChat()) { fill($drawerChats); recent.hidden = true; return; }
  recent.hidden = false;
  const render = (chats) => {
    const active = hashParts()[0] === "chat" ? hashParts()[1] : "";
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
  if (protocolBlocked || !$drawer.hidden) return;
  drawerReturnFocus = document.activeElement;
  const section = currentSection();
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

// ---------- chat snippets (#85) ----------
const SNIPPET_STATUS = {
  completed: "Completed", failed: "Failed", compile_failed: "Compile failed", timeout: "Timed out",
  cancelled: "Cancelled", limit_exceeded: "Limit reached", error: "Sandbox error", interrupted: "Interrupted",
};
const SNIPPET_REASON = {
  timeout: "time limit (30 s)", output_limit: "output limit (1 MiB)", memory_limit: "memory limit (1 GiB)",
  pids_limit: "process limit (64)", temp_storage_limit: "temporary storage limit (128 MiB)",
  cancelled: "cancelled", daemon_restart: "the server restarted",
};

function snippetRunRow(lang, getSource, run) {
  const label = SNIPPET_LANGUAGES[lang].label;
  return h("div", { class: "row snippet-run" }, h("button", {
    class: "btn small", type: "button", title: `Run this code as ${label} in an isolated sandbox`,
    onclick: () => run(lang, getSource(), "block"),
  }, `▶ Run ${label}`));
}

// A user message stays plain text; only fenced blocks in a supported language become code blocks with Run.
function userMessageParts(content, run) {
  const text = String(content || "");
  const parts = [];
  let last = 0;
  for (const m of text.matchAll(/```([\w+#-]*)[^\S\n]*\n([\s\S]*?)```/g)) {
    const lang = snippetLanguage(m[1]);
    if (!lang) continue;
    const code = m[2].replace(/\n$/, "");
    parts.push(text.slice(last, m.index), h("pre", { "data-snippet-lang": lang }, h("code", {}, code)),
      snippetRunRow(lang, () => code, run));
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return parts.filter((p) => p !== "");
}

// Adds Run buttons under the marked code blocks md() produced. The source is the block's text, never its HTML.
function addRunControls(root, run) {
  for (const pre of root.querySelectorAll("pre[data-snippet-lang]")) {
    const lang = pre.dataset.snippetLang;
    if (SNIPPET_LANGUAGES[lang]) pre.after(snippetRunRow(lang, () => pre.textContent, run));
  }
}

// Every value here is source or program output: untrusted, so it only ever becomes text nodes.
const snippetStopped = (code) => code === null || code === undefined;
const snippetBlock = (label, text, cls) => [h("p", { class: "snippet-label" }, label), h("pre", { class: `snippet-out ${cls}` }, text)];

function snippetCompileParts(compile) {
  const code = compile.exit_code;
  let title = `Compile failed (exit ${code})`;
  if (code === 0) title = "Compiled";
  else if (snippetStopped(code)) title = "Compile stopped";
  if (compile.output) return snippetBlock(`${title} · compiler diagnostics`, compile.output, "compile");
  return [h("p", { class: "snippet-label" }, title)];
}

function snippetRunParts(run) {
  const code = run.exit_code;
  const parts = [h("p", { class: "snippet-label" }, snippetStopped(code) ? "Program stopped" : `Exit status ${code}`)];
  if (run.stdout) parts.push(...snippetBlock("stdout", run.stdout, "stdout"));
  if (run.stderr) parts.push(...snippetBlock("stderr", run.stderr, "stderr"));
  if (!run.stdout && !run.stderr) parts.push(h("p", { class: "muted small" }, "No output."));
  return parts;
}

function snippetResultParts(r) {
  const tc = r.toolchain || {};
  const meta = [tc.version, tc.image, r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)} s` : ""];
  const parts = [h("p", { class: "muted small" }, meta.filter(Boolean).join(" · "))];
  if (r.error) parts.push(h("p", { class: "bad small" }, r.error));
  const reasons = (r.reasons || []).map((x) => SNIPPET_REASON[x] || x);
  if (reasons.length) parts.push(h("p", { class: "bad small" }, `Reason: ${reasons.join("; ")}`));
  if (r.truncated) parts.push(h("p", { class: "bad small" }, "Output was truncated at the 1 MiB limit."));
  if (r.compile) parts.push(...snippetCompileParts(r.compile));
  if (r.run) parts.push(...snippetRunParts(r.run));
  return parts;
}

function snippetCard(started, onCancel) {
  const label = SNIPPET_LANGUAGES[started.language]?.label || started.language || "Snippet";
  const status = h("span", { class: "badge running" }, "Running…");
  const cancel = h("button", { class: "btn small bad", type: "button", onclick: () => onCancel(started.id) }, "Cancel");
  const body = h("div", { class: "snippet-body" });
  const el = h("div", { class: "snippet", "data-run": started.id },
    h("div", { class: "row snippet-head" }, h("strong", {}, `${label} run`), status, cancel),
    started.source ? h("details", {}, h("summary", {}, "Source"), h("pre", {}, h("code", {}, started.source))) : null,
    body);
  const finish = (r) => {
    cancel.remove();
    status.className = `badge ${r.status === "completed" ? "done" : "failed"}`;
    status.textContent = SNIPPET_STATUS[r.status] || r.status || "Finished";
    fill(body, snippetResultParts(r));
  };
  return { el, finish };
}

// The manual editor: the owner picks the language; there is no default and no guessing from the code.
function snippetEditor(run) {
  const select = h("select", { class: "snippet-language", "aria-label": "Snippet language" },
    h("option", { value: "" }, "Language…"),
    Object.entries(SNIPPET_LANGUAGES).map(([id, spec]) => h("option", { value: id }, spec.label)));
  const code = h("textarea", { class: "snippet-code", rows: 8, spellcheck: "false", placeholder: "Code to run…",
    "aria-label": "Code to run" });
  const runBtn = h("button", { class: "btn primary small", type: "button", disabled: true }, "▶ Run");
  const sync = () => { runBtn.disabled = !select.value || !code.value.trim(); };
  select.addEventListener("change", sync);
  code.addEventListener("input", sync);
  runBtn.addEventListener("click", async () => {
    if (runBtn.disabled) return;
    runBtn.disabled = true;
    await run(select.value, code.value, "editor");
    sync();
  });
  const el = h("div", { class: "snippet-editor", hidden: true },
    h("div", { class: "row" }, select, runBtn),
    code,
    h("p", { class: "muted small" }, "Runs once in a fresh sandbox: standard library only, no network, 30 s, 1 GiB memory. Output is shown as untrusted text."));
  return { el, select, code, runBtn };
}

// ---------- chat ----------
const CHAT_CHOICE_KEY = "harness.chatChoice";
const CHAT_STARTERS = ["Explain a concept simply", "Review some code I paste", "Summarize a topic with sources"];

function readChatChoice() {
  try { return JSON.parse(localStorage.getItem(CHAT_CHOICE_KEY) || "null") || {}; } catch (_) { return {}; }
}

// Keeps the fixed composer above the on-screen keyboard (iOS does not resize the layout viewport).
function trackKeyboard(composer) {
  const vv = window.visualViewport;
  if (!vv) return () => {};
  const update = () => {
    composer.style.bottom = `${Math.max(0, window.innerHeight - vv.height - vv.offsetTop)}px`;
  };
  vv.addEventListener("resize", update);
  vv.addEventListener("scroll", update);
  update();
  return () => { vv.removeEventListener("resize", update); vv.removeEventListener("scroll", update); };
}

function chatComposer(options, session) {
  const fixed = Boolean(session);
  const choice = readChatChoice();
  const backends = options.backends || [];
  const modelSelect = h("select", { class: "chat-model", "aria-label": "Model", disabled: fixed });
  const effortSelect = h("select", { class: "chat-effort", "aria-label": "Reasoning effort", disabled: fixed });
  const input = h("textarea", { placeholder: "Message…", rows: 1, "aria-label": "Message" });
  const send = h("button", { class: "btn primary", type: "button" }, "Send");
  const cancel = h("button", { class: "btn bad", type: "button", hidden: true }, "Cancel");
  const notice = h("p", { class: "note chat-notice", hidden: true });

  const keyOf = (backend, model) => `${backend}|${model}`;
  if (fixed) {
    modelSelect.append(h("option", {}, `${session.backend || "local"} · ${session.model}`));
    effortSelect.append(h("option", {}, session.effort || "default"));
    effortSelect.hidden = !session.effort;
    modelSelect.title = effortSelect.title = "Start a new chat to change the model or effort.";
  } else {
    for (const b of backends) {
      modelSelect.append(h("optgroup", { label: b.name === "local" ? "Local" : b.name },
        b.models.map((m) => h("option", { value: keyOf(b.name, m) }, m))));
    }
    const wanted = keyOf(choice.backend, choice.model);
    const fallback = backends.find((b) => b.name === options.default_backend) || backends[0];
    if ([...modelSelect.options].some((o) => o.value === wanted)) modelSelect.value = wanted;
    else modelSelect.value = fallback ? keyOf(fallback.name, fallback.model || fallback.models[0]) : "";
  }
  const selected = () => {
    const [backend, ...rest] = (modelSelect.value || "").split("|");
    return { backend, model: rest.join("|"), spec: backends.find((b) => b.name === backend) };
  };
  const syncEffort = () => {
    if (fixed) return;
    const { spec } = selected();
    const efforts = spec?.efforts || [];
    fill(effortSelect, efforts.map((e) => h("option", { value: e }, e)));
    effortSelect.hidden = !efforts.length;
    const pick = choice.effort && efforts.includes(choice.effort) ? choice.effort : spec?.effort;
    if (pick && efforts.includes(pick)) effortSelect.value = pick;
    notice.textContent = spec?.billing_warning || "";
    notice.hidden = !spec?.billing_warning;
  };
  modelSelect.addEventListener("change", syncEffort);
  syncEffort();

  input.addEventListener("input", () => { input.style.height = "44px"; input.style.height = `${Math.min(160, input.scrollHeight)}px`; });
  const el = h("div", { class: "composer chat-composer" }, h("div", { class: "inner", style: "flex-direction:column;align-items:stretch" },
    notice,
    h("div", { class: "row chat-pickers" }, modelSelect, effortSelect),
    h("div", { class: "row", style: "flex-wrap:nowrap;align-items:flex-end" }, input, send, cancel)));
  const remember = () => {
    const { backend, model } = selected();
    try { localStorage.setItem(CHAT_CHOICE_KEY, JSON.stringify({ backend, model, effort: effortSelect.value })); } catch (_) { /* private mode */ }
  };
  return { el, input, send, cancel, effortSelect, selected, remember };
}

async function viewChat(id) {
  if (!canChat()) { go("#/agents", true); return; }
  if (id && !validId(id)) { go("#/chat", true); return; }

  // Paint the page shell before the data fetch below so the route feels instant; the
  // composer and header title are filled in once /chats/<id> or /chats/options resolves (#152).
  setHeader("chat", id ? "" : "Chat");
  document.body.classList.add("chat-page");
  onLeave(() => document.body.classList.remove("chat-page"));

  let ui = null;
  const feed = h("div", { class: "chat-feed", "aria-live": "polite" });
  const welcome = id ? null : h("div", { class: "chat-welcome" },
    h("div", { class: "chat-welcome-mark", "aria-hidden": "true" }, "💬"),
    h("h2", {}, "How can I help?"),
    h("p", { class: "muted" }, "Ask a question or paste code to review. To change files or run work, use Agents."),
    h("div", { class: "chat-starters" }, CHAT_STARTERS.map((text) => h("button", {
      class: "btn small", type: "button",
      onclick: () => {
        if (!ui) return;
        ui.input.value = text;
        ui.input.focus();
      },
    }, text))));
  const wrap = h("div", { class: "chat-wrap" }, welcome, feed);
  append($app, wrap);

  let session = null;
  let options = { backends: [] };
  if (id) session = await api(`/chats/${id}`);
  else options = await api("/chats/options");
  if (session) id = session.id;
  setHeader("chat", session ? session.title : "Chat");
  ui = chatComposer(options, session);
  document.body.append(ui.el);
  const stopKeyboard = trackKeyboard(ui.el);
  onLeave(() => { ui.el.remove(); stopKeyboard(); });

  if (!session && !options.backends.length) {
    feed.append(h("p", { class: "note bad" }, "No model backend is available right now. Check Profile → Backends."));
    ui.send.disabled = true;
  }

  const scrollDown = () => window.scrollTo({ top: document.body.scrollHeight });
  const setBusy = (busy) => { ui.send.hidden = busy; ui.cancel.hidden = !busy; };
  const send = async () => {
    const text = ui.input.value.trim();
    if (!text) return;
    ui.send.disabled = true;
    try {
      if (!session) {
        const { backend, model, spec } = ui.selected();
        ui.remember();
        const created = await api("/chats", { method: "POST", body: {
          prompt: text, backend, model, effort: spec?.efforts?.length ? ui.effortSelect.value : "" } });
        ui.input.value = "";
        go(`#/chat/${created.id}`, true);
        return;
      }
      await api(`/chats/${id}/messages`, { method: "POST", body: { content: text } });
      ui.input.value = "";
      ui.input.style.height = "44px";
      setBusy(true);
    } catch (e) { toast(e.message); }
    ui.send.disabled = false;
  };
  ui.send.addEventListener("click", send);
  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void send(); }
  });
  ui.cancel.addEventListener("click", async () => {
    try { await api(`/chats/${id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
  });
  if (!session) { ui.input.focus(); return; }

  const runSnippet = async (language, source, origin) => {
    try { await api(`/chats/${id}/snippets`, { method: "POST", body: { language, source, origin } }); }
    catch (e) { toast(e.message); }
  };
  const cancelSnippet = async (runId) => {
    try { await api(`/chats/${id}/snippets/${encodeURIComponent(runId)}/cancel`, { method: "POST" }); }
    catch (e) { toast(e.message); }
  };
  const editor = snippetEditor(runSnippet);
  const editorToggle = h("button", { class: "btn small", type: "button", "aria-expanded": "false", onclick: () => {
    editor.el.hidden = !editor.el.hidden;
    editorToggle.setAttribute("aria-expanded", String(!editor.el.hidden));
    if (!editor.el.hidden) editor.select.focus();
  } }, "Run code");
  const effortSuffix = session.effort ? ` · ${session.effort}` : "";
  wrap.prepend(h("div", { class: "row small chat-tools" },
    h("span", { class: "muted" }, `${session.backend || "local"} · ${session.model}${effortSuffix}`),
    editorToggle,
    h("button", { class: "btn small", type: "button", onclick: async () => {
      const title = prompt("Rename chat", session.title);
      if (!title?.trim()) return;
      try { session = await api(`/chats/${id}`, { method: "PATCH", body: { title } }); setHeader("chat", session.title); }
      catch (e) { toast(e.message); }
    } }, "Rename"),
    h("button", { class: "btn small bad", type: "button", onclick: async () => {
      if (!confirm("Delete this chat?")) return;
      try { await api(`/chats/${id}`, { method: "DELETE" }); go("#/chat", true); } catch (e) { toast(e.message); }
    } }, "Delete")), editor.el);

  let lastSeq = 0;
  let live = null;
  let sawContent = false;
  const add = (el) => { feed.append(el); scrollDown(); return el; };
  const assistantMessage = (content) => {
    const el = add(h("div", { class: "msg assistant final", html: md(content, []) }));
    addRunControls(el, runSnippet);
    return el;
  };
  const snippetCards = {};
  const handlers = {
    user_message: (e) => add(h("div", { class: "msg user" }, userMessageParts(e.data.content, runSnippet))),
    snippet_started: (e) => {
      const card = snippetCard(e.data, cancelSnippet);
      snippetCards[e.data.id] = card;
      add(card.el);
    },
    snippet_result: (e) => {
      let card = snippetCards[e.data.id];
      if (!card) {
        card = snippetCards[e.data.id] = snippetCard({ id: e.data.id, language: e.data.language }, cancelSnippet);
        add(card.el);
      }
      card.finish(e.data);
      scrollDown();
    },
    delta: (e) => {
      if (e.data.kind === "reasoning") return;
      if (!live) live = add(h("div", { class: "msg assistant" }));
      live.textContent += e.data.text;
      scrollDown();
    },
    assistant: (e) => {
      live?.remove();
      live = null;
      const d = e.data;
      if (d.content?.trim()) sawContent = true;
      if (d.content?.trim()) assistantMessage(d.content);
      for (const call of d.tool_calls || []) {
        add(h("p", { class: "note" }, call.function?.name === "web_fetch" ? "Reading a web page…" : "Searching the web…"));
      }
    },
    billing_warning: (e) => add(h("p", { class: "note bad" }, e.data.message)),
    limit_waiting: (e) => add(h("p", { class: "note" }, `Rate limit reached; waiting until ${new Date(e.data.resets_at * 1000).toLocaleString()}`)),
    backend_fallback: (e) => add(h("p", { class: "note bad" }, `Rate limit reached; continuing ${e.data.backend} with an API key`)),
    status: (e) => {
      const status = e.data.status;
      session = { ...session, status };
      setBusy(!TERMINAL.has(status));
      if (!TERMINAL.has(status)) return;
      live?.remove();
      live = null;
      const answer = (e.data.answer || "").trim();
      if (answer && !sawContent) assistantMessage(answer);
      if (status !== "done") {
        add(h("p", { class: `status-line${status === "failed" ? " bad" : ""}` }, badge(status),
          e.data.stop_reason && !["final_message", "finished"].includes(e.data.stop_reason) ? ` ${e.data.stop_reason}` : ""));
      }
    },
  };
  const tracked = {};
  for (const type of Object.keys(handlers)) {
    tracked[type] = (e) => {
      if (e.seq !== null && e.seq !== undefined) {
        if (e.seq <= lastSeq) return;
        lastSeq = e.seq;
      }
      handlers[type](e);
    };
  }
  setBusy(!TERMINAL.has(session.status));
  onLeave(openStream(
    () => agentHarnessWeb.url(`/chats/${encodeURIComponent(id)}/events?after=${lastSeq}`, ownerSurface()),
    tracked,
    { authorized: !!agentHarnessWeb.token },
  ));
}

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

// Window vs body vs html disagree on Edge/desktop: use every scroller's metric.
function pageMetrics() {
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

function scrollPage(top) {
  const y = Math.max(0, top);
  window.scrollTo(0, y);
  const se = document.scrollingElement || document.documentElement;
  se.scrollTop = y;
  if (document.body) document.body.scrollTop = y;
}

function bindSessionJumps() {
  const pageHeight = () => pageMetrics().pageH;
  const jumpTop = h("button", { class: "btn small jump jump-top", type: "button", hidden: true, "aria-label": "Jump to start" }, "↑");
  const jumpBottom = h("button", { class: "btn small jump jump-bottom", type: "button", hidden: true, "aria-label": "Jump to end" }, "↓");
  const updateJumps = () => {
    layoutBar();
    const { y, viewH, pageH } = pageMetrics();
    const hide = sessionJumpHidden(y, viewH, pageH);
    jumpTop.hidden = hide.top;
    jumpBottom.hidden = hide.bottom;
  };
  jumpTop.addEventListener("click", () => scrollPage(0));
  jumpBottom.addEventListener("click", () => scrollPage(pageHeight()));
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
  requestAnimationFrame(updateJumps);
  return { updateJumps, pageHeight };
}


const { daemonSettingsCard } = mountDaemonSettings({ h, fill, append, api, toast, isGuest, location, confirm: (m) => confirm(m) });
const { viewProfile, copyBox, githubConnectionCard, readAppIcon, applyAppIcon, applyTheme, applyTextSize } = mountProfile({ $app, $conn, $profileIcon,
  layoutBar, setHeader, h, fill, append, api, getWebAuth: () => webAuth, startGoogle, agentHarnessWeb, isGuest, isMember, toast, go, route, daemonSettingsCard, browser: globalThis });
applyTheme();
applyTextSize();

const { viewInfo } = mountSessionInfo({ $app, h, append, copyBox, downloadDaemonFile });

// ---------- session ----------
const { viewSession } = mountSession({ $app, h, fill, append, api, setHeader, toast, go, route, validId, isGuest, isMember, isOwner, onLeave, badge, reviewBadge,
  progressBar, openStream, sessionTitle, bindSessionJumps, pageMetrics, scrollPage, viewInfo, TERMINAL, agentHarnessWeb, browser: globalThis });

// ---------- new task ----------
const { viewNew, confirmGpuQueue } = mountNewTask({ $app, h, fill, append, api, setHeader, toast, route, isMember, isOwner, onLeave,
  githubConnectionCard, warmModel, browser: globalThis });

// ---------- images ----------
const { viewImages, viewImage, viewImageEdit, viewImageFull } = mountImages({ $app, h, fill, append, api, setHeader, toast, go, route, isGuest, isMember, onLeave,
  progressBar, confirmGpuQueue, daemonImage, downloadDaemonFile, location, confirm: (m) => confirm(m) });

// ---------- scheduled jobs ----------
const jobStatusBadge = (st) => h("span", { class: `badge ${st === "ok" ? "done" : "waiting_approval"}` }, st === "ok" ? "OK" : "⚠ attention");
const { viewJobs, viewJob } = mountJobs({ $app, h, fill, append, api, setHeader, showFab, toast, go, route, isGuest,
  confirmGpuQueue, badge, jobStatusBadge, location, confirm: (m) => confirm(m) });

// ---------- session list ----------
const { viewList } = mountSessions({ $app, h, fill, append, api, setHeader, showFab, onLeave, isMember, isGuest, badge, reviewBadge, REVIEW_LABEL,
  jobStatusBadge, openStream, ownerSurface, agentHarnessWeb, browser: globalThis });

const { viewActions } = mountActions({ $app, h, fill, append, api, setHeader, toast, go, isGuest, isMember, onLeave, copyBox, progressBar });

// ---------- model warm-up ----------
// Loading the model takes about a minute after it has been unloaded. Only an explicit local-model selection (choosing
// the local backend or a model, or typing a task with it selected) starts a load; the server skips it when RAM is
// short. Opening a page never does (#311).
let lastWarm = 0;
async function warmModel(force = false) {
  if (protocolBlocked || isGuest()) return;
  if (!force && Date.now() - lastWarm < 60_000) return;
  try {
    if (!isMember()) {
      const gpu = await api("/gpu");
      if (gpu.manual) return;
    }
    lastWarm = Date.now();
    await api("/models/warm", { method: "POST" });
  } catch (_) { /* offline */ }
}

// ---------- boot ----------
const UPDATE_GUARD = "harness.webUpdateAttempt";

function hasUnsavedInput() {
  return [...document.querySelectorAll("input, textarea, select")].some((el) => {
    if (el.id === "feature-nav") return false;
    if (el.type === "checkbox" || el.type === "radio") return el.checked !== el.defaultChecked;
    if (el.tagName === "SELECT") return [...el.options].some((option) => option.selected !== option.defaultSelected);
    return el.value !== el.defaultValue;
  });
}

async function reloadAndUpdate() {
  if (hasUnsavedInput()) {
    toast("Save or discard your form changes before reloading the app.", 6000);
    return false;
  }
  const attempted = sessionStorage.getItem(UPDATE_GUARD);
  if (attempted === WEB_BUILD_ID) {
    fill($app, h("div", { class: "card" },
      h("h2", {}, "Update did not load"),
      h("p", {}, "Close every installed Agent Harness window, reopen it while online, and reload. If it still fails, remove and reinstall the home-screen app.")));
    return false;
  }
  // Use only the bundle's compiled identifier in browser storage. Compatibility metadata is remote input.
  sessionStorage.setItem(UPDATE_GUARD, WEB_BUILD_ID);
  if (window.caches) {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key.startsWith("harness-shell-")).map((key) => caches.delete(key)));
  }
  const registration = await navigator.serviceWorker?.getRegistration();
  registration?.active?.postMessage("PURGE_SHELL");
  await registration?.update();
  location.reload();
  return true;
}

function blockingUpdate(meta, state) {
  protocolBlocked = true;
  setHeader("agents", "Update required", { page: true });
  const daemonIsOld = state === "daemon_update_required";
  fill($app, h("div", { class: "card" },
    h("h2", {}, daemonIsOld ? "Update Agent Harness Server" : "Update Agent Harness Web"),
    h("p", {}, daemonIsOld
      ? "This browser app uses a newer protocol than the connected server. Update the server, then reload."
      : "This installed app is too old for the connected server."),
    daemonIsOld ? null : h("button", { class: "btn primary", onclick: () => reloadAndUpdate() }, "Reload and update"),
    h("p", { class: "muted small" }, `Web protocol ${WEB_PROTOCOL}; server supports ${meta.protocols?.admin?.min}–${meta.protocols?.admin?.max}.`)));
}

// Asks once per bundle whether to reload into the newer build; true when a reload was started.
async function offerBundleUpdate(foreground) {
  try { await (await navigator.serviceWorker?.getRegistration())?.update(); } catch (_) { /* try again on reload */ }
  const promptKey = "harness.webUpdatePrompt";
  if (sessionStorage.getItem(promptKey) === WEB_BUILD_ID || (foreground && hasUnsavedInput())) return false;
  sessionStorage.setItem(promptKey, WEB_BUILD_ID);
  if (!confirm("A newer Agent Harness Web bundle is available. Reload and update now?")) return false;
  await reloadAndUpdate();
  return true;
}

async function checkCompatibility({ foreground = false } = {}) {
  let meta;
  try { meta = await agentHarnessWeb.compatibility(); }
  catch (_) { return !protocolBlocked; } // stay on the update card if health fails after a skew
  const mismatch = protocolMismatch(meta.protocols?.admin, WEB_PROTOCOL);
  if (mismatch) {
    blockingUpdate(meta, mismatch);
    return false;
  }
  const wasBlocked = protocolBlocked;
  protocolBlocked = false;
  const available = meta.update_hint?.web?.build_id;
  if (available && available !== WEB_BUILD_ID) {
    if (await offerBundleUpdate(foreground)) return false;
  } else {
    sessionStorage.removeItem(UPDATE_GUARD);
  }
  if (wasBlocked) await route();
  return true;
}

if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") void checkCompatibility({ foreground: true });
});

// /health and /me start together; /me is a read-only GET whose result is only adopted once /health passes.
const bootCompatible = checkCompatibility();
const bootIdentity = fetchMe();
void bootCompatible.then((compatible) => (compatible ? bootIdentity : null)).then((me) => {
  if (!me) return null;
  currentMe = me;
  bootMe = Promise.resolve(me);
  paintGuestChrome();
  // The profile emoji paints when it arrives; route data never waits on it.
  void loadProfileIcon().then(() => applyAppIcon(readAppIcon()));
  applyAppIcon(readAppIcon());
  return route();
}).finally(() => {
  // Includes compatibility/login early exits and failures; route paints its existing error state.
  // Removal is instant, with no minimum time or fade-out, even during the icon's fade-in.
  window.dismissBootSplash?.();
});
