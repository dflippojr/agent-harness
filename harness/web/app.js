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
import { lastUpdateText, compatibilityText, lastSeenText } from "./lib/settings-text.mjs";
import { profileIconHidden } from "./lib/layout.mjs";
import { pageMetrics, scrollPage } from "./lib/session-ui.mjs";
import { protocolMismatch } from "./lib/compat.mjs";
import { mountImages } from "./pages/images.mjs";
import { mountJobs } from "./pages/jobs.mjs";
import { mountActions } from "./pages/actions.mjs";
import { mountSessionInfo } from "./pages/session-info.mjs";
import { mountDaemonSettings } from "./pages/daemon-settings.mjs";
import { mountProfile } from "./pages/profile.mjs";
import { mountNewTask } from "./pages/new-task.mjs";
import { mountSessions } from "./pages/sessions.mjs";
import { mountChat } from "./pages/chat.mjs";
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
    const { y, viewH, pageH } = pageMetrics(globalThis); // the cross-engine measurements the jump buttons use
    if (y > pageH - viewH) scrollPage(pageH - viewH, globalThis);
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

const { daemonSettingsCard } = mountDaemonSettings({ h, fill, append, api, toast, isGuest, location, confirm: (m) => confirm(m) });
const { viewProfile, copyBox, githubConnectionCard, readAppIcon, applyAppIcon, applyTheme, applyTextSize } = mountProfile({ $app, $conn, $profileIcon,
  layoutBar, setHeader, h, fill, append, api, getWebAuth: () => webAuth, startGoogle, agentHarnessWeb, isGuest, isMember, toast, go, route, daemonSettingsCard, browser: globalThis });
applyTheme();
applyTextSize();

const { viewInfo } = mountSessionInfo({ $app, h, append, copyBox, downloadDaemonFile });

// ---------- chat ----------
const { viewChat } = mountChat({ $app, h, fill, append, api, setHeader, toast, go, validId, canChat, onLeave, openStream, ownerSurface, badge,
  TERMINAL, agentHarnessWeb, browser: globalThis });

// ---------- session ----------
const { viewSession } = mountSession({ $app, h, fill, append, api, setHeader, toast, go, route, validId, isGuest, isMember, isOwner, onLeave, badge, reviewBadge,
  progressBar, openStream, layoutBar, viewInfo, TERMINAL, agentHarnessWeb, browser: globalThis });

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
