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
import { fmtElapsed, fmtTokens, readFraction, readingText, ago, fmtSpan, pluralize, gpuText } from "./lib/format.mjs";
import { TARGET_LABEL, compareTargets } from "./lib/targets.mjs";
import { toolSummaryText, approvalWhat } from "./lib/tools.mjs";
import { approvalDiffClass, diffLineClass } from "./lib/diff.mjs";
import { SNIPPET_LANGUAGES, snippetLanguage } from "./lib/snippets.mjs";
import { escapeHtml, md } from "./lib/markdown.mjs";
import { lastUpdateText, compatibilityText, lastSeenText } from "./lib/settings-text.mjs";
import { profileIconHidden, sessionJumpHidden } from "./lib/layout.mjs";
import { protocolMismatch } from "./lib/compat.mjs";
import { withTaint } from "./lib/taint.mjs";
import { mountImages } from "./pages/images.mjs";
import { mountJobs } from "./pages/jobs.mjs";
import { mountActions } from "./pages/actions.mjs";
import { mountSessionInfo } from "./pages/session-info.mjs";
import { mountDaemonSettings } from "./pages/daemon-settings.mjs";
import { mountProfile } from "./pages/profile.mjs";
import { mountNewTask } from "./pages/new-task.mjs";

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
const SESSION_EVENT_TYPES = [
  "session_created", "user_message", "status", "assistant", "delta", "tool_call", "tool_result",
  "approval_requested", "approval_decided", "approval_auto_approved", "smart_review", "compaction", "compacting", "error", "llm_retry", "resumed",
  "run_finished", "queue", "notes", "state", "model_waking", "model_ready", "workspace_ready", "branch_saved", "review",
  "target_waiting", "target_online", "compaction_started", "prompt_progress", "gpu_paused", "gpu_resumed", "waiting_memory", "memory_recovered", "app_context", "app_tool_call", "app_tool_result",
  "quote_check", "ungrounded_quotes", "taint_added", "taint_cleared", "checkpoint", "rewound", "forked",
];
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

// ---------- session list ----------
let searchQuery = "";  // kept while navigating, so Back from a result returns to the results
let sessionTarget = "all";
try { sessionTarget = localStorage.getItem("harness.sessionTarget") || "all"; } catch (_) { /* private mode */ }

// Search passages mark matches with U+0002 … U+0003 (control characters, matched on purpose); everything else is escaped.
const markPassage = (text) => escapeHtml(text).replaceAll("\u0002", "<mark>").replaceAll("\u0003", "</mark>");
const PASSAGE_KIND = { title: "title", message: "you", assistant: "agent", tool: "tool output", answer: "answer", context: "app context" };

async function viewList() {
  setHeader("agents", "Agents");
  const list = h("div");
  const results = h("div", { hidden: true });
  const queueNote = h("p", { class: "note" });
  const search = h("input", { type: "search", placeholder: "Search", value: searchQuery, class: "search" });
  const targetSwitch = h("div", { class: "tabs", role: "group", "aria-label": "Filter sessions by machine" });
  append($app, h("div", { class: "search-wrap" }, search), targetSwitch, queueNote, results, list);
  showFab("#/new", "+ New task");

  let sessions = [];
  let targets = [];
  const targetName = (target) => target === "tower" ? "Tower" : TARGET_LABEL[target] || target;
  const sessionCardKey = (s) => [
    s.id, s.title, s.status, s.updated_at, s.chat_summary, s.queue_position, s.review, s.job_status,
    s.target, s.project, (s.pending_approvals || []).map((a) => a.id).join(","),
  ].join("\0");
  const renderSessions = () => {
    const visible = sessionTarget === "all" ? sessions : sessions.filter((s) => s.target === sessionTarget);
    if (!sessions.length) {
      delete list.dataset.keys;
      fill(list, h("p", { class: "empty" }, "No sessions yet. Start one with “New task”."));
      return;
    }
    if (!visible.length) {
      delete list.dataset.keys;
      fill(list, h("p", { class: "empty" }, `No sessions on the ${targetName(sessionTarget)} yet.`));
      return;
    }
    const keys = visible.map(sessionCardKey).join("\n");
    if (list.dataset.keys === keys && list.querySelector("a.card")) return;
    list.dataset.keys = keys;
    fill(list, visible.map((s) => {
      const pending = (s.pending_approvals || []).length;
      const approvalPath = pending ? `/approval/${s.pending_approvals[0].id}` : "";
      return h("a", { class: "card", href: `#/s/${s.id}${approvalPath}` },
        h("h3", {}, s.title),
        h("div", { class: "meta" },
          badge(s.status),
          pending ? h("span", { class: "badge waiting_approval" }, pluralize(pending, "approval")) : null,
          s.queue_position > 0 ? h("span", {}, `#${s.queue_position} in queue`) : null,
          s.review ? reviewBadge(s.review, REVIEW_LABEL[s.review] || s.review) : null,
          s.job_status ? jobStatusBadge(s.job_status) : null,
          s.target !== "tower" ? h("span", {}, `💻 ${TARGET_LABEL[s.target] || s.target}`) : null,
          h("span", {}, s.project), h("span", {}, ago(s.updated_at))),
        s.chat_summary ? h("div", { class: "preview" }, s.chat_summary) : null);
    }));
  };
  const renderTargetSwitch = () => {
    if (!targets.includes(sessionTarget)) sessionTarget = "all";
    targetSwitch.hidden = !!search.value.trim() || targets.length < 2;
    fill(targetSwitch, ["all", ...targets].map((target) => h("button", {
      type: "button", class: target === sessionTarget ? "on" : "", "aria-pressed": target === sessionTarget,
      onclick: () => {
        sessionTarget = target;
        try { localStorage.setItem("harness.sessionTarget", target); } catch (_) { /* private mode */ }
        renderTargetSwitch();
        renderSessions();
      },
    }, target === "all" ? "All" : targetName(target))));
  };

  const runSearch = async () => {
    const q = search.value.trim();
    searchQuery = search.value;
    results.hidden = !q;
    list.hidden = !!q;
    queueNote.hidden = !!q;
    targetSwitch.hidden = !!q || targets.length < 2;
    if (!q) return;
    try {
      const data = await api(`/search?q=${encodeURIComponent(q)}`);
      if (search.value.trim() !== q) return;  // a newer query is on its way
      fill(results,
        data.mode === "any" && data.results.length ? h("p", { class: "muted small" }, "No session matches every word; showing partial matches.") : null,
        data.results.length ? data.results.map((r) => h("a", { class: "card", href: `#/s/${r.id}` },
          h("h3", {}, r.title),
          h("div", { class: "meta" }, badge(r.status), h("span", {}, r.project), h("span", {}, ago(r.created_at)),
            h("span", {}, `${r.hits} match${r.hits === 1 ? "" : "es"}`)),
          r.passages.map((p) => h("div", { class: "passage small" }, h("span", { class: "muted" }, `${PASSAGE_KIND[p.kind] || p.kind}: `),
            h("span", { html: markPassage(p.text) }))))) : h("p", { class: "empty" }, `Nothing matches “${q}”.`));
    } catch (e) { fill(results, h("p", { class: "note bad" }, e.message)); }
  };
  let searchTimer = null;
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250); });
  if (searchQuery.trim()) void runSearch();

  const render = async () => {
    const [freshSessions, queue, gpu, projects] = await Promise.all([
      api("/sessions"), api("/queue"), isMember() ? Promise.resolve(null) : api("/gpu").catch(() => null), api("/projects")]);
    sessions = freshSessions;
    targets = [...new Set(projects.map((p) => p.target || "tower"))]
      .sort(compareTargets);
    const waiting = queue.filter((q) => q.position > 0).length;
    const paused = gpu && (gpu.manual || gpu.state !== "clear");
    fill(queueNote,
      paused ? h("a", { href: "#/actions/resources" }, `⏸ ${gpuText(gpu)}`) : "",
      paused && waiting ? " · " : "",
      waiting ? `${waiting} waiting for the GPU` : "");
    renderTargetSwitch();
    renderSessions();
  };
  await render();
  let timer = null;
  let holding = false;
  let pendingRefresh = false;
  const releaseHold = () => {
    holding = false;
    if (pendingRefresh) {
      pendingRefresh = false;
      render().catch(() => {});
    }
  };
  list.addEventListener("pointerdown", () => { holding = true; });
  window.addEventListener("pointerup", releaseHold);
  window.addEventListener("pointercancel", releaseHold);
  onLeave(() => {
    window.removeEventListener("pointerup", releaseHold);
    window.removeEventListener("pointercancel", releaseHold);
  });
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (holding) { pendingRefresh = true; return; }
      render().catch(() => {});
    }, 300);
  };
  const handlers = {};
  for (const type of ["session_created", "status", "approval_requested", "approval_decided", "run_finished", "queue"]) {
    handlers[type] = refresh;
  }
  onLeave(openStream(() => agentHarnessWeb.url("/events", ownerSurface()), handlers,
    { authorized: !(isGuest() && !agentHarnessWeb.token) }));
  const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
  document.addEventListener("visibilitychange", onVisible);
  onLeave(() => document.removeEventListener("visibilitychange", onVisible));
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

// ---------- session ----------
async function viewSession(sid, tab, focusApproval) {
  if (!validId(sid)) { go("#/agents", true); return; }
  let session = await api(`/sessions/${sid}`);
  sid = session.id;
  setHeader("agents", session.title || "Session");
  let left = false;
  onLeave(() => { left = true; });

  const tabs = h("div", { class: "tabs" },
    ["transcript", "changes", "info"].map((name) => h("button", {
      class: (tab === name || (tab === "approval" && name === "transcript")) ? "on" : "",
      onclick: () => go(name === "transcript" ? `#/s/${sid}` : `#/s/${sid}/${name}`, true),
    }, name[0].toUpperCase() + name.slice(1))));
  const head = h("div", { class: "row small" });
  const usage = h("div", { class: "row small usage" });
  append($app, h("div", { class: "session-chrome" }, sessionTitle(session, () => !left)), head, usage, tabs);
  const jumps = bindSessionJumps();
  const pages = [];
  const fetchById = new Map();
  const rememberFetch = (id, url, text) => {
    const href = (url || "").trim();
    if (!/^https?:\/\//i.test(href)) return;
    if (id) fetchById.set(id, href);
    pages.push({ url: href, text: text || "" });
  };
  let totals = session.totals || {};
  let ctxUsed = session.context_used || 0;
  const ctxLimit = session.context_limit || 0;

  const renderHead = () => {
    const limits = session.run?.rate_limits || {};
    const limitName = String(limits.rateLimitType || "limit").replace("seven_day", "7d").replace("five_hour", "5h");
    const backendUsage = session.backend && session.backend !== "local" && limits.utilization !== undefined
      ? ` · ${limitName} ${Math.round(limits.utilization * 100)}%` : "";
    const onTarget = session.target !== "tower" ? ` on ${TARGET_LABEL[session.target] || session.target}` : "";
    fill(head, badge(session.status),
      session.queue_position > 0 ? h("span", { class: "muted" }, `#${session.queue_position} in GPU queue`) : null,
      h("span", { class: "muted" }, `${session.project}${onTarget} · ${session.backend || "local"}${backendUsage} · ${session.model}`));
    const pct = ctxLimit && ctxUsed ? Math.round((100 * ctxUsed) / ctxLimit) : null;
    const ctxClass = `ctx${pct >= 55 ? " high" : ""}`;
    fill(usage,
      h("span", { class: "muted", title: "Cumulative tokens for this session (prompt tokens in, generated tokens out)" },
        `Tokens ${fmtTokens(totals.prompt_tokens)} in · ${fmtTokens(totals.completion_tokens)} out`),
      pct === null ? null : h("span", { class: ctxClass, title: `Context window: ~${ctxUsed} of ${ctxLimit} tokens. Older context is condensed as it fills up.` },
        progressBar(pct / 100), `${pct}% context`));
  };
  renderHead();

  if (tab === "changes") { await viewChanges(session); jumps.updateJumps(); return; }
  if (tab === "info") { viewInfo(session); jumps.updateJumps(); return; }

  const feed = h("div");
  append($app, feed);

  // composer (owner only; guests may watch the live transcript)
  const input = h("textarea", { placeholder: "Message the agent…", rows: 1 });
  const send = h("button", { class: "btn primary" }, "Send");
  const actions = h("div", { class: "row", style: "margin-bottom:6px" });
  const composer = isGuest() ? null : h("div", { class: "composer" }, h("div", { class: "inner", style: "flex-direction:column;align-items:stretch" },
    actions, h("div", { class: "row", style: "flex-wrap:nowrap;align-items:flex-end" }, input, send)));
  if (composer) document.body.append(composer);
  input.addEventListener("input", () => { input.style.height = "44px"; input.style.height = `${Math.min(160, input.scrollHeight)}px`; });
  send.addEventListener("click", async () => {
    const text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    try {
      await api(`/sessions/${sid}/messages`, { method: "POST", body: { content: text } });
      input.value = "";
      input.style.height = "44px";
    } catch (e) { toast(e.message); }
    send.disabled = false;
  });

  const renderActions = () => {
    if (isGuest()) return;
    const active = !TERMINAL.has(session.status);
    input.placeholder = active ? "Add guidance…" : "Continue this session…";
    fill(actions,
      active ? h("button", {
        class: "btn small bad",
        onclick: async () => {
          if (!confirm("Cancel this task?")) return;
          try { session = { ...session, ...(await api(`/sessions/${sid}/cancel`, { method: "POST" })) }; } catch (e) { toast(e.message); }
        },
      }, "Cancel") : null,
      !active ? h("button", {
        class: "btn small",
        onclick: async () => {
          try {
            const s = await api(`/sessions/${sid}/rerun`, { method: "POST" });
            location.hash = `#/s/${s.id}`;
          } catch (e) { toast(e.message); }
        },
      }, "Run again as new session") : null,
      (session.taint || []).length ? h("button", {
        class: "btn small",
        type: "button",
        title: `Untrusted content read: ${session.taint.map((t) => t.origin).join(", ")}. Risky actions ask for approval until cleared.`,
        onclick: async () => {
          if (!confirm("Clear taint? Risky actions will follow the project rules again.")) return;
          try { session = { ...session, ...(await api(`/sessions/${sid}/taint/clear`, { method: "POST" })) }; renderActions(); } catch (e) { toast(e.message); }
        },
      }, "Clear taint") : null,
      h("span", { class: "spacer" }),
      h("button", { class: "btn small", type: "button", onclick: () => go(`#/s/${sid}/changes`, true) }, "Changes"));
  };
  renderActions();

  // transcript rendering
  // Follow new output only while the reader is at the bottom. Any upward scroll (wheel, finger, momentum) stops
  // following, however small; reaching the bottom again resumes it. A generous "near the bottom" margin used to
  // snap slow upward scrolls back down on every streamed token.
  let follow = true;
  let touching = false;
  let lastY = pageMetrics().y;
  const pageHeight = jumps.pageHeight;
  const atBottom = () => {
    const { y, viewH, pageH } = pageMetrics();
    return viewH + y >= pageH - 2;
  };
  const scrollDown = (force = false) => {
    if (!force && (!follow || touching)) return;
    scrollPage(pageHeight());
    lastY = pageMetrics().y;
    jumps.updateJumps();
  };
  const onScroll = () => {
    const y = pageMetrics().y;
    if (y < lastY - 0.5 && !atBottom()) follow = false; // content shrinking at the bottom also moves y; ignore that
    else if (atBottom()) follow = true;
    lastY = y;
  };
  const stopFollowing = () => { follow = false; };
  const onWheel = (e) => { if (e.deltaY < 0) stopFollowing(); };
  const onTouchStart = () => { touching = true; };
  const onTouchEnd = () => { touching = false; };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("wheel", onWheel, { passive: true });
  window.addEventListener("touchstart", onTouchStart, { passive: true });
  window.addEventListener("touchend", onTouchEnd, { passive: true });
  window.addEventListener("touchcancel", onTouchEnd, { passive: true });
  onLeave(() => {
    window.removeEventListener("scroll", onScroll);
    window.removeEventListener("wheel", onWheel);
    window.removeEventListener("touchstart", onTouchStart);
    window.removeEventListener("touchend", onTouchEnd);
    window.removeEventListener("touchcancel", onTouchEnd);
  });
  const grew = () => { if (follow && !touching) scrollDown(); else jumps.updateJumps(); };
  const add = (el) => {
    feed.append(el);
    if (live && live.el !== el) feed.append(live.el); // the in-progress turn always stays last
    grew();
    return el;
  };
  const calls = new Map();   // tool call id -> {el, state, body}
  const approvals = new Map();
  let live = null;           // streaming bubble
  let lastSeq = 0;
  let lastContent = "";
  let wakingNote = null;
  let gpuNote = null;
  let memoryNote = null;
  let targetNote = null;
  let compactNote = null;    // {el, label, bar, detail, elapsed, start} while older context is being summarized
  let lastEventAt = Date.now();  // server time of the latest persisted event: when the current step began
  let prevEventAt = Date.now();
  const pendingCalls = new Set();  // tool calls of the last assistant turn without a result yet

  const liveBubble = () => {
    if (live) return live;
    const thinkText = h("div", { class: "text" });
    const label = h("span", { class: "dots" }, "Thinking");
    const elapsed = h("span", { class: "elapsed" });
    const think = h("details", { class: "thinking" }, h("summary", {}, label, elapsed), thinkText);
    const content = h("div", { class: "msg assistant", style: "white-space:pre-wrap", hidden: true });
    const el = add(h("div", { class: "ev" }, think, content));
    live = { el, think, thinkText, content, label, elapsed, start: lastEventAt, frozen: false, chars: 0, reading: null };
    tick();
    return live;
  };
  // A model turn has started when the session is running and nothing is waiting on a tool.
  const maybeThinking = () => {
    if (session.status === "running" && pendingCalls.size === 0 && !compactNote) liveBubble();
  };

  const compacting = () => {
    if (compactNote) return compactNote;
    const label = h("span", { class: "dots" }, "Condensing older context");
    const elapsed = h("span", { class: "elapsed" });
    const bar = h("div");
    const detail = h("div", { class: "muted small" }, "Summarizing older messages so the agent can keep going");
    fill(bar, progressBar(null));
    const el = add(h("div", { class: "note compacting ev" }, h("div", { class: "row between" }, label, elapsed), bar, detail));
    compactNote = { el, label, bar, detail, elapsed, start: lastEventAt };
    tick();
    return compactNote;
  };
  function tick() {
    const now = Date.now();
    if (live && !live.frozen) live.elapsed.textContent = ` ${fmtElapsed(now - live.start)}`;
    if (compactNote) compactNote.elapsed.textContent = fmtElapsed(now - compactNote.start);
  }
  const ticker = setInterval(tick, 1000);
  onLeave(() => clearInterval(ticker));

  const toolEl = (call) => {
    const fn = call.function || {};
    let args = {};
    try { args = JSON.parse(fn.arguments || "{}"); } catch (_) { args = { raw: fn.arguments }; }
    if (fn.name === "web_fetch" && args.url) rememberFetch(call.id, args.url, "");
    const summaryText = toolSummaryText(fn, args);
    const state = h("span", { class: "state" }, "…");
    const body = h("div", { class: "body" }, h("pre", {}, JSON.stringify(args, null, 2)));
    const el = h("details", { class: "tool" },
      h("summary", {}, h("span", { class: "name" }, fn.name), h("span", { class: "args" }, summaryText || ""), state), body);
    const slot = h("div", { class: "ev" }, el);
    calls.set(call.id, { el, state, body, slot });
    return slot;
  };

  const approvalCard = (a) => {
    const note = h("input", { type: "text", placeholder: "Note for the agent (optional)" });
    const buttons = h("div", { class: "row end" });
    const decide = async (decision) => {
      buttons.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        await api(`/sessions/${sid}/approvals/${a.id}`, { method: "POST", body: { decision, note: note.value } });
      } catch (e) {
        toast(e.message);
        buttons.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    };
    if (isGuest()) {
      append(buttons,h("p", { class: "muted small" }, "Demo access cannot approve or deny."));
    } else {
      append(buttons,
        h("button", { class: "btn bad solid", onclick: () => decide("deny") }, "Deny"),
        h("button", { class: "btn ok", onclick: () => decide("approve") }, "Approve"));
    }
    const what = approvalWhat(a);
    const reviewerReason = a.smart?.reason ? `: ${a.smart.reason}` : "";
    const rec = a.smart?.recommendation
      ? h("p", { class: "smart-rec" },
          `Reviewer ${a.smart.recommendation} (${Math.round((a.smart.confidence || 0) * 100)}%)${reviewerReason}`)
      : null;
    // Memory library changes carry "summary\n\n<unified diff>"; file writes carry just the diff.
    const memory = a.tool === "memory_edit" || a.tool === "memory_write";
    const [summary, diff] = memory && a.detail.includes("\n\n") ? [a.detail.slice(0, a.detail.indexOf("\n\n")), a.detail.slice(a.detail.indexOf("\n\n") + 2)] : ["", a.detail || ""];
    const diffView = /^@@ /m.test(diff) ? h("div", { class: "diff approval-diff" }, diff.split("\n")
      .filter((line) => !/^(---|\+\+\+) /.test(line))
      .map((line) => h("div", { class: approvalDiffClass(line) }, line))) : null;
    const card = h("div", { class: "approval", id: `approval-${a.id}` },
      h("h4", {}, `Approval needed: ${a.reason || a.tool}`),
      rec,
      summary ? h("p", { style: "margin:4px 0 8px" }, summary) : null,
      diffView || h("pre", {}, a.detail || what),
      a.detail ? h("div", { class: "muted small" }, `${a.tool} ${a.args.path || ""}`) : null,
      note, buttons);
    approvals.set(a.id, { card, buttons, note });
    if (focusApproval === a.id) {
      card.classList.add("focus");
      setTimeout(() => card.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
    }
    return card;
  };

  const handlers = {
    user_message: (e) => { add(h("div", { class: "ev msg user" }, e.data.content)); },
    app_context: (e) => add(h("details", { class: "thinking ev" }, h("summary", {}, "Context from the app"), h("div", { class: "text" }, e.data.content))),
    taint_added: (e) => {
      session = { ...session, taint: withTaint(session.taint, e.data) };
      renderActions();
      add(h("p", { class: "note" }, `Session read untrusted content from ${e.data.origin}: risky actions now ask for approval`));
    },
    taint_cleared: () => {
      session = { ...session, taint: [] };
      renderActions();
      add(h("p", { class: "note" }, "Taint cleared by the owner"));
    },
    app_tool_call: (e) => add(h("p", { class: "note" }, `Asked the app to run ${e.data.name}`)),
    app_tool_result: (e) => add(h("p", { class: "note" }, `The app returned ${e.data.ok ? "a result" : "an error"} (${e.data.chars} characters)`)),
    billing_warning: (e) => add(h("p", { class: "note bad" }, e.data.message)),
    limit_waiting: (e) => add(h("p", { class: "note" }, `Rate limit reached; waiting until ${new Date(e.data.resets_at * 1000).toLocaleString()}`)),
    backend_fallback: (e) => add(h("p", { class: "note bad" }, `Rate limit reached; continuing ${e.data.backend} with an API key`)),
    prompt_progress: (e) => {
      const b = liveBubble();
      const d = e.data;
      if (!b.reading) {
        b.reading = h("div", { class: "reading small muted" });
        b.el.prepend(b.reading);
      }
      fill(b.reading, readingText("Reading context", d), progressBar(readFraction(d)));
      grew();
    },
    delta: (e) => {
      const b = liveBubble();
      if (b.reading) { b.reading.remove(); b.reading = null; }
      if (e.data.kind === "reasoning") {
        b.thinkText.textContent += e.data.text;
        b.chars += e.data.text.length;
      } else {
        b.content.hidden = false;
        b.content.textContent += e.data.text;
        if (!b.frozen) {
          tick();
          b.frozen = true;
          b.label.classList.remove("dots");
          b.label.textContent = "Thought for";
        }
      }
      if (follow && !touching) scrollDown();
    },
    assistant: (e) => {
      const d = e.data;
      if (d.totals) totals = d.totals;
      if (d.prompt_tokens) ctxUsed = d.prompt_tokens + d.completion_tokens;
      renderHead();
      live?.el.remove();
      live = null;
      const took = e.ts ? fmtElapsed(e.ts * 1000 - prevEventAt) : "";
      for (const call of d.tool_calls || []) pendingCalls.add(call.id);
      const wrap = h("div", { class: "ev" });
      if (d.reasoning) {
        const thought = took ? ` for ${took}` : "";
        wrap.append(h("details", { class: "thinking" }, h("summary", {}, `Thought${thought} (${d.completion_tokens} tokens · ${d.gen_tps} tok/s)`),
          h("div", { class: "text" }, d.reasoning)));
      }
      if (d.content?.trim()) {
        lastContent = d.content.trim();
        wrap.append(h("div", { class: `msg assistant${d.tool_calls.length ? "" : " final"}`, html: md(d.content, pages) }));
      }
      if (wrap.childNodes.length) add(wrap);
      for (const call of d.tool_calls || []) add(toolEl(call));
    },
    tool_call: (e) => {
      const c = calls.get(e.data.id);
      if (c && e.data.decision !== "allow") c.state.textContent = e.data.decision === "ask" ? "needs approval" : "blocked";
    },
    approval_requested: (e) => {
      const card = approvalCard(e.data);
      const c = calls.get(e.data.tool_call_id);
      if (c) c.slot.append(card); else feed.append(h("div", { class: "ev" }, card));
      if (focusApproval !== e.data.id) grew();
    },
    approval_auto_approved: (e) => {
      const badge = h("p", { class: "note smart-auto" },
        `Auto-approved: the deterministic gate and smart reviewer both allowed this ${e.data.tool || "call"} (${e.data.reason || "routine workspace work"}).`);
      add(badge);
      const c = calls.get(e.data.tool_call_id);
      if (c) c.state.textContent = "auto-approved";
    },
    smart_review: () => {},
    approval_decided: (e) => {
      const a = approvals.get(e.data.id);
      if (!a) return;
      a.card.classList.add("decided");
      a.card.classList.remove("focus");
      a.note.remove();
      fill(a.buttons, h("span", { class: `badge ${e.data.status === "approved" ? "done" : "failed"}` },
        e.data.status + (e.data.note ? `: ${e.data.note}` : "")));
    },
    tool_result: (e) => {
      pendingCalls.delete(e.data.id);
      if ((e.data.name === "web_fetch" || fetchById.has(e.data.id)) && e.data.output) {
        const fromOutput = (e.data.output.split("\n").find((line) => /^https?:\/\//i.test(line.trim())) || "").trim();
        rememberFetch(e.data.id, fetchById.get(e.data.id) || fromOutput, e.data.output);
      }
      const c = calls.get(e.data.id);
      const out = h("pre", {}, e.data.output);
      if (!c) { add(h("details", { class: "tool ev" }, h("summary", {}, e.data.name), out)); return; }
      c.state.textContent = `${e.data.ok ? "ok" : "error"} · ${e.data.seconds}s`;
      c.state.className = `state ${e.data.ok ? "ok" : "err"}`;
      c.body.append(out);
    },
    compaction_started: (e) => {
      if (live && !live.thinkText.textContent && !live.content.textContent) { live.el.remove(); live = null; }
      const c = compacting();
      c.detail.textContent = `Summarizing ${e.data.messages} older messages (~${fmtTokens(e.data.tokens_before)} tokens in context) so the agent can keep going`;
    },
    compacting: (e) => {
      const c = compacting();
      const d = e.data;
      if (d.phase === "reading") {
        fill(c.label, readingText("Condensing older context · step 1 of 2: reading", d));
        fill(c.bar, progressBar(readFraction(d)));
      } else if (d.phase === "writing") {
        fill(c.label, `Condensing older context · step 2 of 2: writing the summary (${d.tokens} tokens)`);
        fill(c.bar, progressBar(null));
      }
    },
    compaction: (e) => {
      const d = e.data;
      if (d.tier === "mask") {
        add(h("p", { class: "note" },
          `Replaced old tool outputs with recoverable receipts (~${fmtTokens(d.tokens_saved)} tokens saved)`));
        return;
      }
      if (d.totals) totals = d.totals;
      if (d.tokens_after) ctxUsed = d.tokens_after;
      renderHead();
      const text = d.tier === "round_reset"
        ? `Round reset: ~${fmtTokens(d.tokens_before)} → ~${fmtTokens(d.tokens_after)} tokens`
        : d.tier === "summary"
        ? `Context condensed: ~${fmtTokens(d.tokens_before)} → ~${fmtTokens(d.tokens_after)} tokens (${d.summarized_messages} messages summarized)`
        : `Trimmed old tool output: ~${fmtTokens(d.tokens_before)} → ~${fmtTokens(d.tokens_after)} tokens`;
      if (compactNote) {
        const c = compactNote;
        compactNote = null;
        if (e.ts) c.elapsed.textContent = `took ${fmtElapsed(e.ts * 1000 - c.start)}`;
        c.label.classList.remove("dots");
        fill(c.label, d.tier === "summary" ? text : `${text} (the summary failed; see the error above)`);
        fill(c.bar, progressBar(1));
        fill(c.detail, d.summary ? h("details", {}, h("summary", {}, "Show summary"), h("div", { class: "text", style: "white-space:pre-wrap" }, d.summary)) : "");
        c.el.classList.add("done");
      } else if (d.tokens_before - d.tokens_after >= 1000) {
        add(h("p", { class: "note" }, text)); // small trims happen every turn near the limit; only the meter shows those
      }
    },
    notes: (e) => add(h("details", { class: "thinking ev" }, h("summary", {}, "Agent saved notes"), h("div", { class: "text" }, e.data.notes))),
    state: (e) => add(h("details", { class: "thinking ev" }, h("summary", {}, "Agent saved state"), h("pre", { class: "text" }, JSON.stringify(e.data.state || e.data, null, 2)))),
    error: (e) => add(h("p", { class: "note bad" }, e.data.message)),
    quote_check: (e) => add(h("details", { class: "thinking ev" },
      h("summary", {}, `Asked the agent to fix ${e.data.quotes.length} quote${e.data.quotes.length === 1 ? "" : "s"} not found in anything it read`),
      h("div", { class: "text" }, e.data.quotes.map((q) => `“${q}”`).join("\n")))),
    ungrounded_quotes: (e) => add(h("div", { class: "note bad" },
      h("p", {}, `⚠ ${e.data.quotes.length === 1 ? "This quote" : "These quotes"} in the answer didn't appear in anything the agent read, so ${e.data.quotes.length === 1 ? "it" : "they"} may be made up:`),
      h("div", { class: "text", style: "white-space:pre-wrap" }, e.data.quotes.map((q) => `“${q}”`).join("\n")))),
    llm_retry: (e) => add(h("p", { class: "note" }, `Model call retried (${e.data.attempt})`)),
    resumed: () => add(h("p", { class: "note" }, "Agent Harness Server restarted — session resumed")),
    workspace_ready: (e) => add(h("p", { class: "note" }, `Checked out on branch ${e.data.branch} (from ${e.data.base_branch})`)),
    branch_saved: (e) => add(h("p", { class: "note" }, h("button", {
      class: "btn small", type: "button", onclick: () => go(`#/s/${sid}/changes`, true),
    }, `Branch saved: ${e.data.commits.length} commit${e.data.commits.length === 1 ? "" : "s"} to review${e.data.auto_commit ? " (leftover edits committed)" : ""}`))),
    review: (e) => add(h("p", { class: "note" }, `Review: ${e.data.detail}`)),
    checkpoint: (e) => {
      if (e.data.status === "skipped") { add(h("p", { class: "note" }, `Turn not checkpointed: ${e.data.reason}`)); return; }
      const turn = e.data.turn;
      const act = async (btn, path, body) => {
        btn.disabled = true;
        try {
          const s = await api(`/sessions/${sid}/checkpoints/${turn}/${path}`, { method: "POST", body });
          if (path === "fork") go(`#/s/${s.id}`, true); else await viewSession(sid);
        } catch (err) { toast(err.message, 8000); btn.disabled = false; }
      };
      // Hosted CLI sessions keep their own state, which can't be truncated: Fork (with a transcript digest) only.
      const local = !session.backend || session.backend === "local";
      if (isMember()) { add(h("p", { class: "note checkpoint" }, `Checkpoint ${turn} saved`)); return; } // owner API only
      add(h("p", { class: "note checkpoint" }, `Checkpoint ${turn} saved `,
        local ? h("button", {
          class: "btn small", type: "button", title: "Restore the workspace and the agent's context to this point. Packages, processes and files outside the workspace are not undone.",
          onclick: (ev) => confirm(`Rewind to checkpoint ${turn}? Later file changes are undone (the transcript keeps them).`) && void act(ev.target, "rewind"),
        }, "Rewind here") : null, " ",
        h("button", {
          class: "btn small", type: "button", title: "Start a new session from this point, on its own branch",
          onclick: (ev) => { const prompt = window.prompt("Instruction for the forked session"); if (prompt?.trim()) void act(ev.target, "fork", { prompt }); },
        }, "Fork from here")));
    },
    rewound: (e) => add(h("p", { class: "note" }, `Rewound to checkpoint ${e.data.turn}: the workspace and context are as they were then; later turns above are kept for the record`)),
    forked: (e) => add(h("p", { class: "note" }, "Forked from ", h("a", { href: `#/s/${e.data.parent}` }, e.data.parent),
      ` at checkpoint ${e.data.turn}${e.data.summary_note ? ` (${e.data.summary_note})` : ""}`)),
    model_waking: (e) => {
      wakingNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `The model was asleep. Waking it (about ${Math.round(e.data.expected_seconds / 60) || 1} min)`)));
    },
    model_ready: (e) => {
      if (wakingNote) fill(wakingNote, `Model woke up in ${e.data.seconds} s`);
      else add(h("p", { class: "note" }, `Model woke up in ${e.data.seconds} s`));
      wakingNote = null;
    },
    gpu_paused: (e) => {
      gpuNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `Paused: ${e.data.reason} needs the GPU, so the model was unloaded. The task continues ${Math.round(e.data.resume_after_seconds / 60)} min after that ends (Actions → Resources to resume now)`)));
    },
    gpu_resumed: (e) => {
      const text = `GPU free again after ${fmtSpan(e.data.seconds)}; reloading the model`;
      if (gpuNote) fill(gpuNote, text);
      else add(h("p", { class: "note" }, text));
      gpuNote = null;
    },
    waiting_memory: (e) => {
      memoryNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `Waiting for memory: ${e.data.reason}, so the ${e.data.waiting_for || "work"} doesn't start yet (Actions → Resources)`)));
    },
    memory_recovered: (e) => {
      const text = `Memory recovered after ${fmtSpan(e.data.seconds)}; continuing`;
      if (memoryNote) fill(memoryNote, text);
      else add(h("p", { class: "note" }, text));
      memoryNote = null;
    },
    target_waiting: (e) => {
      targetNote = add(h("p", { class: "note" }, h("span", { class: "dots" },
        `Waiting for the ${TARGET_LABEL[e.data.target] || e.data.target}: it's offline or asleep. The task continues when it wakes`)));
    },
    target_online: (e) => {
      const text = `${TARGET_LABEL[e.data.target] || e.data.target} is back after ${fmtSpan(e.data.seconds)}`;
      if (targetNote) fill(targetNote, text);
      else add(h("p", { class: "note" }, text));
      targetNote = null;
    },
    queue: (e) => { session.queue_position = e.data.position; renderHead(); },
    status: (e) => {
      session.status = e.data.status;
      if (e.data.status !== "queued") session.queue_position = null;
      renderHead();
      renderActions();
      if (e.data.status !== "running" && live && !live.thinkText.textContent && !live.content.textContent) {
        live.el.remove(); // queued, waiting for an approval or the Mac: not thinking
        live = null;
      }
      if (TERMINAL.has(e.data.status)) {
        pendingCalls.clear();
        live?.el.remove();
        live = null;
        const answer = (e.data.answer || "").trim();
        if (answer && answer !== lastContent) add(h("div", { class: "ev msg assistant final", html: md(answer, pages) }));
        add(h("p", { class: "status-line" }, badge(e.data.status),
          e.data.stop_reason && !["final_message", "finished"].includes(e.data.stop_reason) ? ` ${e.data.stop_reason}` : ""));
      }
    },
  };
  const tracked = {};
  for (const type of SESSION_EVENT_TYPES) {
    tracked[type] = (e) => {
      const persisted = e.seq !== null && e.seq !== undefined;
      if (persisted) {
        if (e.seq <= lastSeq) return;
        lastSeq = e.seq;
        if (e.ts) { prevEventAt = lastEventAt; lastEventAt = e.ts * 1000; }
      }
      handlers[type]?.(e);
      const finalAnswer = type === "assistant" && !(e.data.tool_calls || []).length && (e.data.content || "").trim();
      if (persisted && !finalAnswer && !TERMINAL.has(session.status)) maybeThinking();
    };
  }
  onLeave(openStream(() => (isGuest() && !agentHarnessWeb.token
    ? agentHarnessWeb.url(`/sessions/${encodeURIComponent(sid)}/events?after=${lastSeq}`, "legacy")
    : agentHarnessWeb.sessionStreamUrl(sid, lastSeq)), tracked,
    { authorized: !!agentHarnessWeb.token }));
  if (composer) onLeave(() => composer.remove());
}

function reviewCard(s) {
  if (!s.repo_kind || !s.branch) return null;
  const busy = !TERMINAL.has(s.status);
  const base = s.base_branch || "base";
  const conflictFiles = (message) => {
    const match = /^merge conflicts in (.+?)\. Ask the agent/.exec(message);
    return match ? match[1].split(", ").filter(Boolean) : [];
  };
  const conflictHelp = (files) => {
    if (isGuest()) {
      return h("div", { class: "merge-conflict" },
        h("p", { class: "muted small" }, `Merge conflicts in ${files.join(", ")}. Demo access cannot ask the agent to resolve them.`));
    }
    const ask = h("button", { class: "btn primary" }, "Ask agent to resolve");
    ask.addEventListener("click", async () => {
      if (!confirm(`Ask the agent to merge origin/${base} and resolve ${files.length} conflicting file${files.length === 1 ? "" : "s"}?`)) return;
      ask.disabled = true;
      try {
        await api(`/sessions/${s.id}/messages`, { method: "POST", body: { content:
          `Merge origin/${base} into your branch, resolve the merge conflicts in ${files.join(", ")}, run the relevant tests, and commit the resolution. Do not push.` } });
        toast("Asked the agent to resolve the conflicts", 4000);
        location.hash = `#/s/${s.id}`;
      } catch (e) {
        toast(e.message, 6000);
        ask.disabled = false;
      }
    });
    return h("div", { class: "approval merge-conflict", style: "margin-top:10px" },
      h("h4", {}, "Merge needs conflict resolution"),
      h("p", { class: "small" }, "Conflicting files:"),
      h("ul", { class: "small" }, files.map((file) => h("li", {}, h("code", {}, file)))),
      h("div", { class: "row end" }, ask));
  };
  const act = (action, question) => async (ev) => {
    if (question && !confirm(question)) return;
    const card = ev.target.closest(".card");
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      const updated = await api(`/sessions/${s.id}/review/${action}`, { method: "POST" });
      toast(updated.review_detail || `${action} done`, 4000);
      void route();
    } catch (e) {
      toast(e.message, 6000);
      card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      const files = action === "merge" ? conflictFiles(e.message) : [];
      if (files.length) {
        card.querySelector(".merge-conflict")?.remove();
        card.append(conflictHelp(files));
      }
    }
  };
  const buttons = [];
  if (!isGuest() && !busy && !s.workspace_removed && s.review !== "discarded") {
    if (s.repo_kind === "local") {
      buttons.push(h("button", { class: "btn ok", onclick: act("merge", `Squash-merge ${s.branch} into ${base}?`) }, `Merge into ${base}`));
      if (s.push_target) {
        buttons.push(h("button", { class: "btn ok", onclick: act("push",
          `Push branch ${s.branch} to GitHub repository ${s.push_target} (branch ${s.branch}) using your GitHub connection? `
          + "GitHub records your account as the pusher; commit authors stay as they are.") }, "Push to GitHub"));
      }
    } else {
      buttons.push(h("button", { class: "btn ok", onclick: act("push", `Push ${s.branch} to the remote?`) }, "Push branch"));
    }
    buttons.push(h("button", { class: "btn bad solid", onclick: act("discard", "Discard this branch and delete the workspace? This can't be undone.") }, "Discard"));
  }
  return h("section", { class: "card" },
    h("h3", {}, "Review"),
    h("div", { class: "meta" }, h("span", {}, `branch ${s.branch}`), s.base_branch ? h("span", {}, `from ${s.base_branch}`) : null,
      s.review ? reviewBadge(s.review, s.review) : null),
    s.review_detail ? h("p", { class: "muted small" }, s.review_detail) : null,
    busy ? h("p", { class: "muted small" }, "The agent is still working; review when the run ends.") : null,
    buttons.length ? h("div", { class: "row end", style: "margin-top:8px" }, buttons) : null);
}

async function viewChanges(session) {
  const sid = session.id;
  const box = h("div", {}, h("p", { class: "note" }, "Loading changes…"));
  append($app, reviewCard(session), box);
  const data = await api(`/sessions/${sid}/changes`);
  if (data.removed) {
    fill(box, h("p", { class: "empty" }, "This workspace was cleaned up or discarded."));
    return;
  }
  if (!data.repos.length) {
    fill(box, h("p", { class: "empty" }, "No git repositories in this workspace yet."));
    return;
  }
  const canComment = !isGuest() && data.repos.some((r) => r.parsed);
  let comments = [];
  if (canComment) {
    try { comments = await api(`/sessions/${sid}/review-comments`); } catch { comments = []; }
  }
  const state = { comments, sel: null };  // sel: {repo, path, side, anchor, start, end}
  const render = () => fill(box, secretScanCard(sid, data.secret_scan, state, canComment, render),
    data.repos.map((repo) => repoChanges(sid, repo, state, canComment, render)));
  render();
}

// Secret scan of the added lines (issue #263): findings block Merge/Push until fixed or dismissed with a reason.
// Values never reach the browser; `preview` keeps at most the first and last two characters.
function secretScanCard(sid, scan, state, canComment, render) {
  if (!scan) return null;
  if (scan.status === "unsupported") {  // remote targets: the gate covers tower sessions only
    return h("section", { class: "card secret-scan" }, h("h3", {}, "Secret scan"),
      h("p", { class: "note" }, `Secret scan not available for this target: ${scan.message}.`));
  }
  if (scan.status !== "ok") {
    return h("section", { class: "card secret-scan" }, h("h3", {}, "Secret scan"),
      h("p", { class: "note" }, `The secret scan could not run, so Merge and Push are blocked: ${scan.message}`));
  }
  if (!scan.findings.length) return null;
  const askFix = async (e) => {
    e.currentTarget.disabled = true;
    try {
      // Drafts for lines in the diff; a history-rewrite request for values only in earlier commits; commits
      // already on the remote can only be dismissed. The server's message says which happened.
      const result = await api(`/sessions/${sid}/secret-findings/fix`, { method: "POST" });
      state.comments.push(...result.drafts);
      toast(result.message, 6000);
    } catch (err) { toast(err.message, 6000); }
    render();
  };
  const dismiss = (f) => async (e) => {
    const box = e.currentTarget.closest(".secret-finding");
    const input = h("input", { type: "text", maxlength: "500", placeholder: "Why this is not a secret (required)", "aria-label": "Reason" });
    const confirm = h("button", { class: "btn small bad", type: "button", onclick: async () => {
      const reason = input.value.trim();
      if (!reason) return input.focus();
      confirm.disabled = true;
      try {
        const done = await api(`/sessions/${sid}/secret-findings/${f.fingerprint}/dismiss`, { method: "POST", body: { reason } });
        Object.assign(f, { dismissed: true, dismissal: done.dismissal });
        scan.open = scan.findings.filter((x) => !x.dismissed).length;
        render();
      } catch (err) { toast(err.message, 6000); confirm.disabled = false; }
    } }, "Dismiss");
    box.querySelector(".secret-actions").replaceChildren(input, confirm);
    input.focus();
  };
  const row = (f) => h("div", { class: "secret-finding" },
    h("div", { class: "row", style: "justify-content:space-between" },
      h("span", { class: "small" }, `${f.repo === "." ? "" : f.repo + "/"}${f.file}:${f.line}${f.commit ? ` @ ${f.commit}` : ""} · ${f.rule} · `, h("code", {}, f.preview)),
      f.dismissed ? h("span", { class: "badge cancelled" }, "dismissed") : null),
    f.commit && !f.dismissed ? h("div", { class: "muted small" }, `Removed by a later commit but still in commit ${f.commit}, so it blocks Push (not Merge). Dismiss it, or rewrite the branch.`) : null,
    f.dismissed && f.dismissal ? h("div", { class: "muted small" }, `Reason: ${f.dismissal.reason}`) : null,
    !f.dismissed && isOwner() ? h("div", { class: "row end secret-actions" },
      h("button", { class: "btn small", type: "button", onclick: dismiss(f) }, "Dismiss…")) : null);
  return h("section", { class: "card secret-scan" },
    h("h3", {}, "Secret scan"),
    h("p", { class: scan.open ? "note" : "muted small" }, scan.open
      ? `${pluralize(scan.open, "possible secret")} in the added lines. Merge and Push are blocked until each is fixed or dismissed with a reason.`
      : "Every finding was dismissed."),
    scan.findings.map(row),
    scan.open && canComment ? h("div", { class: "row end", style: "margin-top:8px" },
      h("button", { class: "btn ok", type: "button", onclick: askFix }, "Ask agent to fix")) : null,
    h("p", { class: "muted small" }, `${scan.scanner}${scan.cached ? " · cached" : ""}`));
}

// Text of each line on one side of a file in a parsed diff: {line number: text}.
function sideLines(parsed, path, side) {
  const key = side === "old" ? "old" : "new";
  const out = {};
  for (const f of parsed || []) {
    if (f.name !== path) continue;
    for (const ln of f.lines) if (ln[key] !== null) out[ln[key]] = ln.text;
  }
  return out;
}

// A draft is stale once any commented line no longer reads the same in the current diff.
function commentStale(repo, c) {
  const lines = sideLines(repo.parsed, c.path, c.side);
  for (let n = c.start_line; n <= c.end_line; n++) if (lines[n] !== c.quoted[n - c.start_line]) return true;
  return false;
}

function lineRange(start, end) { return start === end ? `${start}` : `${start}–${end}`; }

function repoChanges(sid, repo, state, canComment, render) {
  const files = splitDiff(repo.diff);
  const parsedFiles = new Map((repo.parsed || []).map((f) => [f.name, f]));
  const mine = state.comments.filter((c) => c.repo === repo.path);
  const sel = state.sel?.repo === repo.path ? state.sel : null;

  const pick = (path, side, num) => {
    if (sel?.path === path && sel.side === side) {
      const lines = sideLines(repo.parsed, path, side);
      const start = Math.min(sel.anchor, num), end = Math.max(sel.anchor, num);
      for (let n = start; n <= end; n++) if (lines[n] === undefined) return toast("Pick lines within one hunk.");
      state.sel = { ...sel, start, end };
    } else {
      state.sel = { repo: repo.path, path, side, anchor: num, start: num, end: num };
    }
    render();
  };
  const composer = () => {
    const lines = sideLines(repo.parsed, sel.path, sel.side);
    const quoted = [];
    for (let n = sel.start; n <= sel.end; n++) quoted.push(lines[n]);
    const input = h("textarea", { class: "review-input", rows: 3, placeholder: "Comment for the agent", "aria-label": "Comment" });
    input.value = sel.text || "";  // kept across re-renders while extending the range
    input.addEventListener("input", () => { sel.text = input.value; });
    const add = h("button", { class: "btn ok", type: "button", onclick: async () => {
      const text = input.value.trim();
      if (!text) return input.focus();
      add.disabled = true;
      try {
        const made = await api(`/sessions/${sid}/review-comments`, { method: "POST", body: {
          repo: repo.path, path: sel.path, side: sel.side, start_line: sel.start, end_line: sel.end,
          quoted, comment: text, base: repo.base, head: repo.head } });
        state.comments.push(made);
        state.sel = null;
        render();
      } catch (e) { toast(e.message, 6000); add.disabled = false; }
    } }, "Add comment");
    const where = `${sel.side === "old" ? "removed " : ""}line ${lineRange(sel.start, sel.end)}`;
    return h("div", { class: "review-composer" },
      h("div", { class: "muted small" }, `${sel.path} · ${where}. Tap another line to extend.`),
      input,
      h("div", { class: "row end" }, h("button", { class: "btn", type: "button", onclick: () => { state.sel = null; render(); } }, "Cancel"), add));
  };
  const lineRow = (f, ln) => {
    if (ln.kind === "hunk") return h("div", { class: "hunk" }, ln.text);
    const sign = { add: "+", del: "-" }[ln.kind] || " ";
    let side = "new";
    if (ln.kind === "del") side = "old";
    else if (ln.kind !== "add" && sel?.path === f.name) side = sel.side;
    const num = side === "old" ? ln.old : ln.new;
    const picked = sel?.path === f.name && sel.side === side && num >= sel.start && num <= sel.end;
    const commented = mine.some((c) => c.path === f.name && c.side === side && num >= c.start_line && num <= c.end_line);
    const removed = side === "old" ? "removed " : "";
    const tap = canComment ? {
      role: "button", tabindex: "0", "aria-label": `Comment on ${removed}line ${num}`,
      onclick: () => pick(f.name, side, num),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(f.name, side, num); } },
    } : {};
    const rowClass = `dl ${ln.kind}${picked ? " picked" : ""}${commented ? " commented" : ""}`;
    return h("div", { class: rowClass, ...tap },
      h("span", { class: "ln" }, ln.old ?? ""), h("span", { class: "ln" }, ln.new ?? ""), h("span", { class: "tx" }, `${sign}${ln.text}`));
  };
  const fileBody = (f) => {
    const parsed = parsedFiles.get(f.name);
    if (!parsed) {
      return f.lines.map((line) => h("div", {
        class: diffLineClass(line),
      }, line));
    }
    const out = [];
    for (const ln of parsed.lines) {
      out.push(lineRow(f, ln));
      // The composer opens under the last selected line.
      if (sel?.path === f.name && ln.kind !== "hunk" && (sel.side === "old" ? ln.old : ln.new) === sel.end) out.push(composer());
    }
    return out;
  };
  const removeDraft = async (c) => {
    try {
      await api(`/sessions/${sid}/review-comments/${c.id}`, { method: "DELETE" });
      state.comments = state.comments.filter((x) => x.id !== c.id);
      render();
    } catch (e) { toast(e.message, 6000); }
  };
  const send = async (e) => {
    e.currentTarget.disabled = true;
    try {
      await api(`/sessions/${sid}/review-comments/send`, { method: "POST" });
      state.comments = [];
      toast("Sent to the agent.");
      go(`#/s/${sid}`, true);
    } catch (err) { toast(err.message, 6000); render(); }
  };
  const drafts = mine.length ? h("div", { class: "review-drafts" },
    h("h4", {}, `Draft comments (${mine.length})`),
    mine.map((c) => h("div", { class: "review-draft" },
      h("div", { class: "row", style: "justify-content:space-between" },
        h("span", { class: "small" }, `${c.path} · ${c.side === "old" ? "removed " : ""}line ${lineRange(c.start_line, c.end_line)}`,
          commentStale(repo, c) ? h("span", { class: "badge cancelled", style: "margin-left:6px" }, "stale") : null),
        h("button", { class: "btn small bad", type: "button", "aria-label": "Delete comment", onclick: () => removeDraft(c) }, "Delete")),
      h("pre", { class: "small review-quote" }, c.quoted.join("\n")),
      h("div", {}, c.comment))),
    h("div", { class: "row end", style: "margin-top:8px" },
      h("button", { class: "btn ok", type: "button", onclick: send }, `Send ${state.comments.length} to agent`))) : null;
  return h("section", { class: "card" },
    h("h3", {}, repo.path === "." ? "workspace" : repo.path),
    h("div", { class: "meta" }, h("span", {}, `branch ${repo.branch}`), repo.base ? h("span", {}, `since ${repo.base.slice(0, 8)}`) : null,
      h("span", {}, `${repo.files.length} changed file${repo.files.length === 1 ? "" : "s"}`)),
    repo.commits.length ? h("details", { style: "margin-top:8px" }, h("summary", {}, pluralize(repo.commits.length, "new commit")),
      h("pre", { class: "small", style: "white-space:pre-wrap" }, repo.commits.join("\n"))) : null,
    drafts,
    files.length ? files.map((f) => h("details", { class: "file", open: files.length <= 4 || (sel?.path === f.name) || undefined },
      h("summary", {}, f.name),
      h("div", { class: "diff" }, fileBody(f)))) : h("p", { class: "muted small" }, "No differences."),
    repo.truncated ? h("p", { class: "note" }, "Diff truncated.") : null);
}

function splitDiff(diff) {
  const files = [];
  let cur = null;
  for (const line of (diff || "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { name: m ? m[1] : line, lines: [] };
      files.push(cur);
    } else if (cur && !/^(index |new file mode|deleted file mode|--- |\+\+\+ )/.test(line)) {
      cur.lines.push(line);
    }
  }
  files.forEach((f) => { while (f.lines.length && !f.lines.at(-1)) f.lines.pop(); });
  return files;
}

const { daemonSettingsCard } = mountDaemonSettings({ h, fill, append, api, toast, isGuest, location, confirm: (m) => confirm(m) });
const { viewProfile, copyBox, githubConnectionCard, readAppIcon, applyAppIcon, applyTheme, applyTextSize } = mountProfile({ $app, $conn, $profileIcon,
  layoutBar, setHeader, h, fill, append, api, getWebAuth: () => webAuth, startGoogle, agentHarnessWeb, isGuest, isMember, toast, go, route, daemonSettingsCard, browser: globalThis });
applyTheme();
applyTextSize();

const { viewInfo } = mountSessionInfo({ $app, h, append, copyBox, downloadDaemonFile });

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
