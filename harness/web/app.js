// Agent Harness web app: plain ES module, no build step. Hash routes:
//   #/                       redirects to #/chat (owner) or #/agents
//   #/chat[/<id>]            Chat home: welcome state, or a durable non-agent conversation
//   #/agents                 agent session list
//   #/new                    new task (templates)
//   #/s/<id>                 session transcript (live)
//   #/s/<id>/approval/<aid>  same, focused on one approval (notification deep link)
//   #/s/<id>/changes         diff viewer
//   #/s/<id>/info            session details
//   #/actions[/<tab>]        owner actions: gpu, accounts, remote-control, disk
//   #/profile                identity plus Settings menu
//   #/profile/account        icon picker, account info, connection details
//   #/profile/<section>      a Settings page (appearance, notifications, backends, …)
//   #/profile/{accounts,disk,remote-control} redirect to #/actions/<tab>
//   #/images                 image generation and gallery
//   #/images/<id>            one result (prompt, metadata, Another one)
//   #/images/<id>/edit       masked inpainting / photo edit
//   #/images/<id>/full       in-app fullscreen viewer
//   #/jobs[/new|/<id>]       scheduled jobs

import { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } from "./client.mjs";

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
const TARGET_LABEL = { tower: "tower", macbook: "MacBook" };
// The home machine sorts first, everything else alphabetically.
function compareTargets(a, b) {
  if (a === "tower") return -1;
  if (b === "tower") return 1;
  return a.localeCompare(b);
}
const SESSION_EVENT_TYPES = [
  "session_created", "user_message", "status", "assistant", "delta", "tool_call", "tool_result",
  "approval_requested", "approval_decided", "approval_auto_approved", "smart_review", "compaction", "compacting", "error", "llm_retry", "resumed",
  "run_finished", "queue", "notes", "model_waking", "model_ready", "workspace_ready", "branch_saved", "review",
  "target_waiting", "target_online", "compaction_started", "prompt_progress", "gpu_paused", "gpu_resumed", "app_context", "app_tool_call", "app_tool_result",
  "quote_check", "ungrounded_quotes",
];
const REVIEW_LABEL = { merged: "merged", pushed: "pushed", discarded: "discarded" };
const fmtElapsed = (ms) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
};
const fmtTokens = (n) => {
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return `${n || 0}`;
};
// llama-server's prompt progress counts the cached prefix as processed; the bar covers only the part being read.
const readFraction = (d) => (d.total > d.cached ? (d.processed - d.cached) / (d.total - d.cached) : null);
const readingText = (what, d) => {
  const cached = d.cached ? ` (${fmtTokens(d.cached)} cached)` : "";
  return `${what} ${fmtTokens(Math.max(0, d.processed - d.cached))} of ${fmtTokens(d.total - d.cached)} new tokens${cached}`;
};
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

function profileIconHidden(topLevel, page = false) {
  // Show on Chat, Agents, Tasks, Images, and Actions. Hide on nested Back pages
  // and on Profile (page: true). Guest chrome is unchanged; this flag is route-only.
  return !topLevel || !!page;
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
async function currentUser() {
  const bootstrap = !agentHarnessWeb.token && !agentHarnessWeb.independent ? "legacy" : "admin";
  try {
    currentMe = await api("/me", { surface: bootstrap });
  } catch (_) {
    try { currentMe = await api("/me", { surface: "app" }); }
    catch (_) { currentMe = { role: "guest" }; }
  }
  return currentMe;
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

// One-line summary of a tool call for its collapsed row.
function toolSummaryText(fn, args) {
  if (fn.name === "run_shell") return args.command;
  if (fn.name === "git_clone" || fn.name === "web_fetch") return args.url;
  if (fn.name === "prometheus_query") return args.query;
  if (args.service) {
    const since = args.since ? ` since ${args.since}` : "";
    return `${args.service}${since}`;
  }
  if (args.path) {
    const line = args.start_line ? ` :${args.start_line}` : "";
    return `${args.path}${line}`;
  }
  return fn.arguments;
}

// What an approval card asks the owner to allow.
function approvalWhat(a) {
  if (a.tool === "run_shell" || a.tool === "Bash" || a.tool === "exec_command") {
    const network = a.args.network ? "🌐 network · " : "";
    return `${network}$ ${a.args.command}`;
  }
  if (a.tool === "git_clone") return `git clone ${a.args.url}`;
  if (a.tool === "restart_service") return `restart ${a.args.service}`;
  return JSON.stringify(a.args, null, 2);
}

function approvalDiffClass(line) {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  return line.startsWith("-") ? "del" : "";
}

function diffLineClass(line) {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+") && !line.startsWith("+++")) return "add";
  return line.startsWith("-") && !line.startsWith("---") ? "del" : "";
}

const reviewBadge = (review, label) => h("span", { class: `badge ${review === "discarded" ? "cancelled" : "done"}` }, label);

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function badge(status) {
  return h("span", { class: `badge ${status}` }, STATUS_LABEL[status] || status);
}

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const normalizeQuote = (text) => (text || "").toLowerCase().replace(/[^a-z0-9]/g, "");
const quoteParts = (q) => q.split(/\.\.\.|…/).map(normalizeQuote).filter((p) => p.length >= 12);
const quoteIn = (q, text) => {
  const parts = quoteParts(q);
  return parts.length > 0 && parts.every((p) => normalizeQuote(text).includes(p));
};
function quoteHref(url, quote) {
  const base = (url || "").split("#")[0];
  const snippet = quote.replace(/\s+/g, " ").trim().slice(0, 80);
  return `${base}#:~:text=${encodeURIComponent(snippet)}`;
}
function quoteLinks(answer, pages) {
  const links = {};
  const re = /["“]([^"”\n]{25,400})["”]/g;
  let match;
  while ((match = re.exec(answer || ""))) {
    const q = match[1];
    const page = (pages || []).find((p) => /^https?:\/\//i.test(p.url || "") && quoteIn(q, p.text));
    if (page) links[q] = quoteHref(page.url, q);
  }
  return links;
}
function linkQuotes(html, answer, pages) {
  const links = quoteLinks(answer, pages);
  const quotes = Object.keys(links).sort((a, b) => b.length - a.length);
  for (const q of quotes) {
    const escaped = escapeHtml(q);
    html = html.split(escaped).join(`<a class="quote-source" href="${escapeHtml(links[q])}" target="_blank" rel="noopener">${escaped}</a>`);
  }
  return html;
}

// Small, safe Markdown subset: everything is escaped first, then a few constructs are re-enabled.
// A fenced block in a language the snippet runner supports is marked so Chat can add its Run button.
const MD_BLOCK_MARK = "\u0000";   // brackets a fenced-block placeholder; escaped input never contains it as text we render
const MD_BLOCK_LINE = new RegExp(String.raw`^${MD_BLOCK_MARK}\d+${MD_BLOCK_MARK}$`);
const MD_BLOCK_REF = new RegExp(String.raw`${MD_BLOCK_MARK}(\d+)${MD_BLOCK_MARK}`, "g");
const MD_TABLE_ROW = /^\s*\|.*\|\s*$/;
const MD_LIST_ITEM = /^\s*([-*]|\d+\.) /;

const isMdTagChar = (ch) => /[\w+#-]/.test(ch);

// Replaces each ``` fence (escaped text in, language tag optional) with a placeholder; onBlock(tag, rest) makes the block's html.
function replaceFences(text, onBlock) {
  let out = "";
  let pos = 0;
  for (;;) {
    const open = text.indexOf("```", pos);
    const close = open < 0 ? -1 : text.indexOf("```", open + 3);
    if (close < 0) break;
    let tagEnd = open + 3;
    while (tagEnd < close && isMdTagChar(text[tagEnd])) tagEnd++;
    out += text.slice(pos, open) + onBlock(text.slice(open + 3, tagEnd), text.slice(tagEnd, close));
    pos = close + 3;
  }
  return out + text.slice(pos);
}

const mdInline = (s) => s
  .replace(/`([^`\n]+)`/g, "<code>$1</code>")
  .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
  .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
  .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

// Each block reader returns [html, index of the last line it used].
function mdHeading(line, i) {
  const level = Math.min(6, line.match(/^#+/)[0].length + 2);
  return [`<h${level}>${mdInline(line.replace(/^#+ /, ""))}</h${level}>`, i];
}

function mdTable(lines, i) {
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => mdInline(c.trim()));
  let html = "<table><thead><tr>" + cells(lines[i]).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
  i += 2;
  while (i < lines.length && MD_TABLE_ROW.test(lines[i])) {
    html += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
    i++;
  }
  return [`<div class="md-table">${html}</tbody></table></div>`, i - 1];
}

function mdList(lines, i) {
  const ordered = /^\s*\d+\./.test(lines[i]);
  let html = ordered ? "<ol>" : "<ul>";
  while (i < lines.length && MD_LIST_ITEM.test(lines[i])) {
    html += `<li>${mdInline(lines[i].replace(MD_LIST_ITEM, ""))}</li>`;
    i++;
  }
  return [html + (ordered ? "</ol>" : "</ul>"), i - 1];
}

const isMdTableStart = (lines, i) => MD_TABLE_ROW.test(lines[i]) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1]);

function mdBlock(lines, i) {
  const line = lines[i];
  if (MD_BLOCK_LINE.test(line.trim())) return [line.trim(), i];
  if (/^#{1,6} /.test(line)) return mdHeading(line, i);
  if (isMdTableStart(lines, i)) return mdTable(lines, i);
  if (MD_LIST_ITEM.test(line)) return mdList(lines, i);
  if (/^&gt; ?/.test(line)) return [`<blockquote>${mdInline(line.replace(/^&gt; ?/, ""))}</blockquote>`, i];
  if (!line.trim()) return ["", i];
  return [`<p>${mdInline(line)}</p>`, i];
}

function md(src, pages) {
  const blocks = [];
  const text = replaceFences(escapeHtml(src || ""), (tag, rest) => {
    const code = rest.replace(/^[^\S\n]*\n?/, "");
    const lang = snippetLanguage(tag);
    const langAttr = lang ? ` data-snippet-lang="${lang}"` : "";
    blocks.push(`<pre${langAttr}><code>${code.replace(/\n$/, "")}</code></pre>`);
    return `${MD_BLOCK_MARK}${blocks.length - 1}${MD_BLOCK_MARK}`;
  });
  const out = [];
  const lines = text.split("\n");
  let i = 0;
  while (i < lines.length) {
    const [html, last] = mdBlock(lines, i);
    out.push(html);
    i = last + 1;
  }
  return linkQuotes(out.join("\n").replace(MD_BLOCK_REF, (_, n) => blocks[Number(n)]), src, pages);
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
    if (data.length && handlers[type]) handlers[type](JSON.parse(data.join("\n")));
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
      source.addEventListener(type, (msg) => fn(JSON.parse(msg.data)));
    }
  };
  const onVisible = () => { if (!protocolBlocked && document.visibilityState === "visible") connect(); };
  document.addEventListener("visibilitychange", onVisible);
  connect();
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
  await currentUser();
  paintGuestChrome();
  const parts = hashParts();
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
  refreshDrawerChats();
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
// Mirrors harness/snippets.py LANGUAGES; the server validates every run. A fence tag only decides whether a block
// gets a Run button, and the button names the language it runs as. Nothing runs unless the owner clicks.
const SNIPPET_LANGUAGES = {
  python: { label: "Python", aliases: ["python", "py", "python3"] },
  javascript: { label: "JavaScript", aliases: ["javascript", "js", "node", "mjs", "cjs"] },
  java: { label: "Java", aliases: ["java"] },
  csharp: { label: "C#", aliases: ["csharp", "cs", "c#"] },
  cpp: { label: "C++", aliases: ["cpp", "c++", "cxx", "cc"] },
};
const SNIPPET_STATUS = {
  completed: "Completed", failed: "Failed", compile_failed: "Compile failed", timeout: "Timed out",
  cancelled: "Cancelled", limit_exceeded: "Limit reached", error: "Sandbox error", interrupted: "Interrupted",
};
const SNIPPET_REASON = {
  timeout: "time limit (30 s)", output_limit: "output limit (1 MiB)", memory_limit: "memory limit (1 GiB)",
  pids_limit: "process limit (64)", temp_storage_limit: "temporary storage limit (128 MiB)",
  cancelled: "cancelled", daemon_restart: "the server restarted",
};

function snippetLanguage(tag) {
  const t = String(tag || "").trim().toLowerCase();
  return Object.keys(SNIPPET_LANGUAGES).find((id) => SNIPPET_LANGUAGES[id].aliases.includes(t)) || "";
}

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
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send(); }
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
  if (searchQuery.trim()) runSearch();

  const render = async () => {
    const [freshSessions, queue, gpu, projects] = await Promise.all([
      api("/sessions"), api("/queue"), isMember() ? Promise.resolve(null) : api("/gpu").catch(() => null), api("/projects")]);
    sessions = freshSessions;
    targets = [...new Set(projects.map((p) => p.target || "tower"))]
      .sort(compareTargets);
    const waiting = queue.filter((q) => q.position > 0).length;
    const paused = gpu && (gpu.manual || gpu.state !== "clear");
    fill(queueNote,
      paused ? h("a", { href: "#/actions/gpu" }, `⏸ ${gpuText(gpu)}`) : "",
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

// ---------- new task ----------
// "45 s" under 90 seconds, otherwise whole minutes.
const fmtSpan = (seconds, toMinutes = Math.round) => (seconds >= 90 ? `${toMinutes(seconds / 60)} min` : `${seconds} s`);
const pluralize = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const escalateSuffix = (row) => (row.escalate_reason ? ` (${row.escalate_reason})` : "");
const originsSuffix = (k) => (k.origins?.length ? ` · ${k.origins.join(", ")}` : "");
const usedSuffix = (k) => (k.last_used_at ? ` · used ${ago(k.last_used_at)}` : "");

function holdRemainingText(seconds) {
  if (seconds === null) return "until you turn it off";
  return `for about ${fmtSpan(seconds, Math.ceil)}`;
}

async function confirmGpuQueue(label) {
  if (isMember()) return true;
  try {
    const gpu = await api("/gpu");
    if (!gpu.manual) return true;
    const remaining = holdRemainingText(gpu.manual_remaining_seconds);
    return confirm(`GPU hold is on ${remaining}. ${label} can be queued, but nothing will be sent to the local model until the hold ends. Queue it?`);
  } catch (err) {
    console.debug("GPU hold unreadable; not blocking the queue", err);
    return true;
  }
}

const MODEL_STATE = {
  ready: "✓ Model loaded",
  sleeping: "Model is asleep; loading it now (about a minute)",
  waking: "Model is loading (about a minute); you can start the task anyway",
  unreachable: "Model server isn't answering",
  paused: "⏸ Model unloaded while something else uses the GPU; tasks wait (Actions → GPU)",
};

function paintModelState(modelState, statuses, modelName, holdActive) {
  const current = statuses.find((s) => s.name === modelName) || statuses[0];
  if (!current) return;
  const holdPaused = current.state === "paused" && holdActive;
  modelState.textContent = holdPaused ? "" : (MODEL_STATE[current.state] || current.state);
  modelState.classList.toggle("dots", !holdPaused && (current.state === "waking" || current.state === "sleeping"));
}

function runnerStateText(targetName, runner) {
  const label = TARGET_LABEL[targetName] || targetName;
  if (!runner?.online) return `Runs on the ${label}, which is offline or asleep: the task will wait for it`;
  const free = runner.info.free_gb !== undefined ? `, ${runner.info.free_gb} GB free` : "";
  return `Runs on the ${label} (online${free})`;
}

// With the GPU held, prefer a hosted backend so the task is not stuck behind the hold.
function pickDefaultBackend(available, holdActive) {
  if (holdActive && available.some((b) => b.name === "claude")) return "claude";
  return available[0]?.name || "local";
}

// localStorage can be unavailable (private mode), so a failed read or write just means "not remembered".
function storeGet(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}
function storeSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* private mode: not remembered */ }
}
function storeRemove(key) {
  try { localStorage.removeItem(key); } catch (_) { /* private mode: nothing to remove */ }
}

function skillOption(sk, inputs) {
  const box = h("input", { type: "checkbox", class: "skill-opt", value: sk.slug });
  inputs.push(box);
  return h("label", { class: "row", style: "gap:8px;align-items:flex-start;margin:6px 0" }, box,
    h("span", {}, h("strong", {}, sk.title || sk.slug),
      h("div", { class: "muted small" }, sk.purpose || `v${sk.version} · ${sk.content_hash.slice(0, 12)}`)));
}

// Resolves true when the session was created (and the page moved on), false when the form should stay usable.
async function startSession(fields, draftKey) {
  try {
    const s = await api("/sessions", { method: "POST", body: fields });
    storeRemove(draftKey);
    location.hash = `#/s/${s.id}`;
    return true;
  } catch (err) {
    toast(err.message);
    return false;
  }
}

async function saveTemplate({ prompt, project, backend, model }) {
  if (!prompt.trim()) return toast("Write a prompt first");
  const name = window.prompt("Template name");
  if (!name) return;
  try {
    await api("/templates", { method: "POST", body: { name, project, backend, model, prompt } });
    toast("Template saved");
    warmModel();
    route();
  } catch (err) { toast(err.message); }
}

function templateManager(templates) {
  return h("details", { style: "margin-top:28px" }, h("summary", { class: "muted" }, "Manage templates"),
    templates.map((t) => h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", {}, t.name), h("span", { class: "spacer" }),
        h("button", {
          class: "btn small bad",
          onclick: async () => {
            if (!confirm(`Delete template “${t.name}”?`)) return;
            await api(`/templates/${t.id}`, { method: "DELETE" });
            warmModel();
            route();
          },
        }, "Delete")),
      h("div", { class: "preview" }, `${t.project} · ${t.prompt}`))));
}

const NEW_PROJECT_NOTE = {
  member: "Saved in your household account. Use a lowercase project id; a public HTTPS git source is cloned into your own area.",
  owner: "Saved privately on Agent Harness Server. Use a lowercase project id; a git source gets a reviewable branch per task.",
};

const repoPlaceholder = () => (isMember() ? "https://github.com/org/repo" : String.raw`D:\Projects\example or https://…`);

// Members get a fixed "tower" value; the owner picks which machine the new project lives on.
function projectTargetInput(targets, target) {
  if (isMember()) return h("input", { type: "hidden", value: "tower" });
  const select = h("select", {}, targets.map((name) => h("option", { value: name },
    name === "tower" ? "Tower" : TARGET_LABEL[name] || name)));
  select.value = target;
  return select;
}

function loadNewTaskData() {
  const member = isMember();
  return Promise.all([
    api("/projects"), api("/models"),
    member ? Promise.resolve([]) : api("/templates").catch(() => []),
    member ? Promise.resolve([{ name: "local", available: true }]) : api("/backends?auth=skip"),
    member ? Promise.resolve(null) : api("/gpu").catch(() => null)]);
}

async function viewNew() {
  setHeader("agents", "New task", { page: true });
  let [projects, models, allTemplates, backends, gpu] = await loadNewTaskData();
  // Where the task runs: the tower or a runner (the MacBook). Projects and templates for other machines are hidden.
  const targets = [...new Set(projects.map((p) => p.target))];
  const targetKey = "harness.target";
  let target = storeGet(targetKey) || "tower";
  if (!targets.includes(target)) target = targets[0] || "tower";
  const projectTarget = (name) => projects.find((p) => p.name === name)?.target || "tower";
  let templates = [];
  const tplSelect = h("select", {});
  const project = h("select", {});
  const fillChoices = () => {
    templates = allTemplates.filter((t) => projectTarget(t.project) === target);
    fill(tplSelect, h("option", { value: "" }, templates.length ? "— none —" : "No templates for this machine"),
      templates.map((t) => h("option", { value: t.id }, t.name)));
    fill(project, projects.filter((p) => p.target === target).map((p) => h("option", { value: p.name },
      p.description ? `${p.name} — ${p.description}` : p.name)));
  };
  fillChoices();
  const targetSwitch = targets.length > 1 ? h("div", { class: "row" }, targets.map((name) => h("button", {
    type: "button", class: `btn small${name === target ? " primary" : ""}`, "data-target": name,
    onclick: (ev) => {
      target = name;
      storeSet(targetKey, name);
      newProjectTarget.value = target;
      for (const b of ev.currentTarget.parentNode.children) b.classList.toggle("primary", b === ev.currentTarget);
      fillChoices();
      showTarget();
      showProjectHint();
    },
  }, name === "tower" ? "🖥 Tower" : `💻 ${TARGET_LABEL[name] || name}`))) : null;
  const targetState = h("div", { class: "muted small", style: "margin-top:6px" });
  const showTarget = async () => {
    const p = projects.find((x) => x.name === project.value);
    if (!p || p.target === "tower") { targetState.textContent = ""; return; }
    try {
      const r = (await api("/runners")).find((x) => x.name === p.target);
      targetState.textContent = runnerStateText(p.target, r);
    } catch (_) { /* offline */ }
  };
  const projectHint = h("div", { class: "muted small", style: "margin-top:6px" });
  const showProjectHint = () => {
    projectHint.textContent = (project.value === "scratch" || project.value === "mac-scratch")
      ? "Scratch is a fresh empty folder for this session only. It is not a git repo and does not add a new project."
      : "";
  };
  project.addEventListener("change", () => { showTarget(); showProjectHint(); });
  showTarget();
  showProjectHint();
  const newProjectName = h("input", { type: "text", placeholder: "my-project", maxlength: "64", required: true,
    pattern: "[a-z0-9][a-z0-9._-]{0,63}" });
  const newProjectDescription = h("input", { type: "text", placeholder: "Optional description", maxlength: "240" });
  const newProjectTarget = projectTargetInput(targets, target);
  const newProjectSource = h("select", {},
    h("option", { value: "empty" }, "Empty workspace"),
    h("option", { value: "repo" }, isMember() ? "Public HTTPS repository" : "Local folder or git URL"));
  const newProjectRepo = h("input", { type: "text", placeholder: repoPlaceholder(), hidden: true });
  newProjectSource.addEventListener("change", () => {
    newProjectRepo.hidden = newProjectSource.value !== "repo";
    newProjectRepo.required = newProjectSource.value === "repo";
  });
  const createProjectButton = h("button", { class: "btn primary", type: "submit" }, "Create project");
  const projectCreator = h("details", { class: "card" }, h("summary", {}, "＋ New project"),
    h("form", { onsubmit: async (e) => {
      e.preventDefault();
      createProjectButton.disabled = true;
      try {
        const created = await api("/projects", { method: "POST", body: {
          name: newProjectName.value, description: newProjectDescription.value, target: newProjectTarget.value,
          repo: newProjectSource.value === "repo" ? newProjectRepo.value : "",
        } });
        projects.push(created);
        target = created.target;
        storeSet(targetKey, target);
        fillChoices();
        project.value = created.name;
        if (targetSwitch) for (const b of targetSwitch.children) b.classList.toggle("primary", b.dataset.target === target);
        showTarget();
        showProjectHint();
        syncSkillChecks();
        projectCreator.open = false;
        toast(`Project ${created.name} created`);
      } catch (err) {
        toast(err.message);
      } finally {
        createProjectButton.disabled = false;
      }
    } },
    h("p", { class: "muted small" }, NEW_PROJECT_NOTE[isMember() ? "member" : "owner"]),
    h("label", {}, "Name"), newProjectName,
    h("label", {}, "Description"), newProjectDescription,
    isMember() ? [] : [h("label", {}, "Runs on"), newProjectTarget],
    h("label", {}, "Workspace"), newProjectSource, newProjectRepo,
    h("div", { class: "row", style: "margin-top:18px" }, createProjectButton)));
  const model = h("select", {}, models.map((m) => h("option", { value: m.name, selected: m.default }, m.name)));
  let localModel = model.value;
  model.addEventListener("change", () => { localModel = model.value; });
  const gpuHold = () => !!(gpu && (gpu.manual || gpu.state !== "clear"));
  const availableBackends = backends.filter((b) => b.available);
  const defaultBackend = pickDefaultBackend(availableBackends, gpuHold());
  const backend = h("select", {}, availableBackends.map((b) =>
    h("option", { value: b.name, selected: b.name === defaultBackend }, b.name === "local" ? "Local model" : b.name)));
  backend.value = defaultBackend;
  const backendState = h("div", { class: "muted small", style: "margin-top:6px" });
  const holdNotice = h("div", { class: "muted small gpu-hold-note", style: "margin-top:6px" },
    "Model unloaded while something else uses the GPU; tasks wait (",
    h("a", { href: "#/actions/gpu" }, "Actions → GPU"),
    ")");
  const modelState = h("div", { class: "muted small", style: "margin-top:6px" });
  const start = h("button", { class: "btn primary", type: "submit" }, "Start");
  const syncHoldUi = () => {
    const queued = gpuHold() && backend.value === "local";
    holdNotice.hidden = !queued;
    start.textContent = queued ? "Queue task" : "Start";
    start.classList.toggle("primary", !queued);
    start.classList.toggle("queued", queued);
  };
  const showBackend = () => {
    const b = backends.find((x) => x.name === backend.value);
    const isLocal = backend.value === "local";
    if (isLocal) {
      fill(model, models.map((m) => h("option", { value: m.name, selected: m.name === localModel }, m.name)));
    } else {
      if (models.some((m) => m.name === model.value)) localModel = model.value;
      fill(model, h("option", { value: b?.model || "" }, b?.model || `${backend.value} default`));
    }
    model.disabled = !isLocal;
    modelState.hidden = !isLocal;
    backendState.textContent = b?.billing_warning || "";
    backendState.classList.toggle("bad", !!b?.billing_warning);
    syncHoldUi();
  };
  backend.addEventListener("change", showBackend);
  showBackend();
  const prompt = h("textarea", { placeholder: "e.g. Clone local:invoice-tools, fix the failing test, and report back." });
  const title = h("input", { type: "text", placeholder: "Optional; defaults to the first line" });
  const pollModel = async () => {
    if (!isMember()) {
      try { gpu = await api("/gpu"); } catch (_) { /* offline */ }
      syncHoldUi();
    }
    if (backend.value !== "local") return;
    try {
      paintModelState(modelState, await api("/models/status"), model.value, gpuHold());
    } catch (_) { /* offline: the form's own errors cover it */ }
  };
  warmModel(true);
  pollModel();
  const modelTimer = setInterval(pollModel, 3000);
  onLeave(() => clearInterval(modelTimer));
  const draftKey = "harness.draft";
  prompt.value = storeGet(draftKey) || "";
  prompt.addEventListener("input", () => storeSet(draftKey, prompt.value));

  tplSelect.addEventListener("change", () => {
    const t = templates.find((x) => x.id === tplSelect.value);
    if (!t) return;
    project.value = t.project;
    if ((t.backend || "local") === "local" && t.model) localModel = t.model;
    backend.value = t.backend || "local";
    showBackend();
    showTarget();
    showProjectHint();
    prompt.value = t.prompt;
    syncSkillChecks();
  });

  const enabledSkills = await api("/skills/enabled").catch(() => []);
  const skillInputs = [];
  const skillBoxes = enabledSkills.map((sk) => skillOption(sk, skillInputs));
  const syncSkillChecks = () => {
    for (const box of skillInputs) {
      const sk = enabledSkills.find((s) => s.slug === box.value);
      box.checked = (sk?.projects || []).includes(project.value);
    }
  };
  syncSkillChecks();
  project.addEventListener("change", syncSkillChecks);
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      if (!prompt.value.trim()) return toast("Write a prompt first");
      if (backend.value === "local" && !(await confirmGpuQueue("This task"))) return;
      start.disabled = true;
      const selectedSkills = [...form.querySelectorAll("input.skill-opt:checked")].map((el) => el.value);
      const started = await startSession({ prompt: prompt.value, project: project.value, backend: backend.value,
        model: backend.value === "local" ? model.value : null, title: title.value || null, skills: selectedSkills }, draftKey);
      if (!started) start.disabled = false;
    },
  },
  targetSwitch ? [h("label", {}, "Runs on"), targetSwitch] : null,
  allTemplates.length ? [h("label", {}, "Template"), tplSelect] : null,
  h("label", {}, "Prompt"), prompt,
  h("label", {}, "Project"), project, targetState, projectHint,
  isMember() ? [] : [h("label", {}, "Backend"), backend, holdNotice, backendState],
  h("label", {}, "Model"), model, modelState,
  h("label", {}, "Title"), title,
  skillBoxes.length ? [h("label", {}, "Skills"), h("p", { class: "muted small" }, "Checked skills are injected for this session (exact include list). Skills allowlisted for the selected project start checked; uncheck to exclude them. They stay frozen even if you disable them later."), ...skillBoxes] : null,
  h("div", { class: "row", style: "margin-top:18px" },
    isMember() ? null : h("button", {
      class: "btn", type: "button",
      onclick: () => saveTemplate({ prompt: prompt.value, project: project.value, backend: backend.value,
        model: backend.value === "local" ? model.value : "" }),
    }, "Save as template"),
    h("span", { class: "spacer" }), start));
  append($app, projectCreator, form);

  if (allTemplates.length) append($app, templateManager(allTemplates));
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
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
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

// 0.75*innerHeight on a tall desktop window is often larger than the whole
// overflow, so both arrows stay hidden unless the transcript is >1.75 viewports.
function sessionJumpHidden(y, viewH, pageH) {
  const vh = Math.max(1, Number(viewH) || 0);
  const far = Math.min(160, 0.75 * vh);
  return { top: y <= far, bottom: pageH - vh - y <= far, far };
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
      if (d.totals) totals = d.totals;
      if (d.tokens_after) ctxUsed = d.tokens_after;
      renderHead();
      const text = d.tier === "summary"
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
        `Paused: ${e.data.reason} needs the GPU, so the model was unloaded. The task continues ${Math.round(e.data.resume_after_seconds / 60)} min after that ends (Actions → GPU to resume now)`)));
    },
    gpu_resumed: (e) => {
      const text = `GPU free again after ${fmtSpan(e.data.seconds)}; reloading the model`;
      if (gpuNote) fill(gpuNote, text);
      else add(h("p", { class: "note" }, text));
      gpuNote = null;
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
    : agentHarnessWeb.sessionStreamUrl(sid, lastSeq)), tracked));
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
      route();
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
  const render = () => fill(box, data.repos.map((repo) => repoChanges(sid, repo, state, canComment, render)));
  render();
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

function viewInfo(s) {
  const t = s.totals || {};
  const rows = [
    ["Title", s.title], ["Session", s.id], ["Status", s.stop_reason ? `${s.status} (${s.stop_reason})` : s.status],
    ["Project", s.project], ["Target", s.target], ["Backend", s.backend || "local"], ["Model", s.model],
    ["Created", new Date(s.created_at * 1000).toLocaleString()], ["Updated", new Date(s.updated_at * 1000).toLocaleString()],
    ["Model turns", t.turns || 0], ["Prompt tokens", t.prompt_tokens || 0], ["Completion tokens", t.completion_tokens || 0],
    ["Workspace", s.workspace_removed ? `${s.workspace} (removed)` : s.workspace],
  ];
  if (s.branch) rows.push(["Branch", s.base_branch ? `${s.branch} from ${s.base_branch}` : s.branch], ["Review", s.review || "pending"]);
  const frozen = s.skills || [];
  if (frozen.length) {
    rows.push(["Skills", frozen.map((sk) => `${sk.slug} v${sk.version} (${(sk.content_hash || "").slice(0, 12)})`).join(", ")]);
  }
  append($app, h("div", { class: "card" }, rows.map(([k, v]) => h("div", { class: "row", style: "justify-content:space-between;padding:4px 0" },
    h("span", { class: "muted" }, k), h("span", { style: "overflow-wrap:anywhere;text-align:right" }, String(v))))),
  h("button", { class: "btn", onclick: () => downloadDaemonFile(`/sessions/${s.id}/transcript`, `${s.id}.md`) },
    "Download Markdown transcript"));
}

// ---------- images ----------
const IMAGE_PHASE = {
  idle: "", waiting: "Waiting for the GPU (a game or transcode is using it)", switching: "Unloading the language model",
  starting: "Starting ComfyUI", warm: "Image generator is ready", generating: "Generating",
  restoring: "Reloading the language model",
};
const IMAGE_BUSY = new Set(["waiting", "switching", "starting", "generating", "restoring"]);

function imageCard(img) {
  const ready = img.status === "done";
  const kind = img.operation && img.operation !== "generate" ? img.operation : "";
  const scale = Number(img.scale) > 1 ? `${img.scale}×` : "";
  const placeholderContent = img.status === "failed" || img.status === "cancelled" ? img.status : h("span", { class: "dots" }, img.status);
  return h("a", { class: "card image-card", href: `#/images/${img.id}` },
    ready ? daemonImage(`/images/${img.id}.png`, { alt: img.prompt, loading: "lazy" })
      : h("div", { class: `image-placeholder ${img.status}` }, placeholderContent),
    kind ? h("span", { class: "image-kind" }, kind) : null,
    scale ? h("span", { class: "image-scale" }, scale) : null,
    h("div", { class: "preview small" }, img.prompt));
}

function updateImageStatusView(view, s) {
  const text = s.phase in IMAGE_PHASE ? IMAGE_PHASE[s.phase] : s.phase;
  const { label, bar, fill: barFill, detail } = view.imageStatusParts;
  view.hidden = !text;
  if (!text) return view;
  const queued = s.queued ? ` · ${s.queued} queued` : "";
  const p = s.progress || {};
  const busy = IMAGE_BUSY.has(s.phase);
  const hasSteps = s.phase === "generating" && Number(p.max) > 0;
  const upscaling = s.phase === "generating" && p.stage === "upscaling";
  const editing = s.phase === "generating" && p.stage === "editing";
  const upscaleName = upscaling ? "Upscaling" : null;
  const editName = editing ? "Editing" : null;
  const stageName = upscaleName || editName;
  label.textContent = (stageName || text) + queued;
  label.classList.toggle("dots", busy);
  bar.hidden = !busy;
  detail.hidden = !hasSteps;
  if (busy) {
    bar.classList.toggle("indeterminate", !hasSteps);
    if (hasSteps) {
      const fraction = Math.max(0, Math.min(1, Number(p.value || 0) / Number(p.max)));
      barFill.style.width = `${Math.max(2, fraction * 100).toFixed(1)}%`;
      detail.textContent = `${stageName || "Sampling"} ${Math.round(fraction * 100)}% · ${p.value || 0} / ${p.max} steps`;
    } else {
      barFill.style.width = "";
      detail.textContent = "";
    }
  }
  return view;
}

function imageStatusView(s) {
  const label = h("span", { class: "image-status-label" });
  const bar = progressBar(null);
  const detail = h("span", { class: "muted small image-status-detail" });
  const view = h("div", { class: "image-status note" }, label, bar, detail);
  view.imageStatusParts = { label, bar, fill: bar.firstElementChild, detail };
  return updateImageStatusView(view, s);
}

function imageModeEntries(status) {
  if (status.modes) {
    return Object.entries(status.modes);
  }
  return Object.entries(status.models || {}).map(([id, label]) => [id, {
    label, available: true, resolution: id === "quality" || id === "quality-fast" ? "high" : "standard",
  }]);
}

function installedImageModeEntries(status) {
  return imageModeEntries(status).filter(([, spec]) => spec.available !== false);
}

const IMAGE_MODELS_EMPTY = "No image models are installed. Install them with ops/images-models.ps1 into the server's configured Comfy models directory.";

async function viewImages() {
  setHeader("images", "Images");
  let data;
  try { data = await api("/images"); } catch (e) { append($app, h("p", { class: "note bad" }, e.message)); return; }
  let gpu = null;
  if (!isMember()) {
    try { gpu = await api("/gpu"); } catch (_) { /* offline */ }
  }
  const prompt = h("textarea", { placeholder: "Describe the image…" });
  const draftKey = "harness.imageDraft";
  try { prompt.value = localStorage.getItem(draftKey) || ""; } catch (_) { /* private mode */ }
  const startWarmup = () => {
    if (route.imageWarmupPromise) return route.imageWarmupPromise;
    if (route.imageWarmupStarted) return Promise.resolve();
    route.imageWarmupStarted = true;
    const request = api("/images/warmup", { method: "POST" }).catch((error) => {
      route.imageWarmupStarted = false;
      throw error;
    }).finally(() => {
      if (route.imageWarmupPromise === request) route.imageWarmupPromise = null;
    });
    route.imageWarmupPromise = request;
    return request;
  };
  prompt.addEventListener("input", () => {
    try { localStorage.setItem(draftKey, prompt.value); } catch (_) { /* ignore */ }
    if (prompt.value.trim()) startWarmup().catch(() => {});
  });
  const modeEntries = installedImageModeEntries(data.status);
  const modes = Object.fromEntries(modeEntries);
  const modelChoices = modeEntries.map(([key, spec]) => ({ key, ...spec, display_name: spec.label || spec }));
  const emptyModels = h("p", { class: "muted small image-models-empty" }, IMAGE_MODELS_EMPTY);
  const model = modeEntries.length ? h("select", { "aria-label": "Model" }, modeEntries.map(([key, spec]) => h("option", {
    value: key,
  }, spec.label || spec))) : null;
  const aspect = h("select", {}, data.status.aspect_ratios.map((a) => h("option", { value: a }, a)));
  let resolutionTouched = false;
  const resolutionInputs = Object.entries(data.status.resolutions).map(([name, spec]) => {
    const input = h("input", { type: "radio", name: "resolution", value: name, checked: name === "standard" });
    input.addEventListener("change", () => { resolutionTouched = true; });
    const size = h("span", { class: "size" });
    const label = h("label", { class: "resolution-option" }, input, spec.label, size);
    return { name, spec, input, size, label };
  });
  const renderResolutions = () => {
    for (const choice of resolutionInputs) {
      const [w, height] = choice.spec.sizes[aspect.value];
      choice.size.textContent = `${w} × ${height}`;
    }
  };
  aspect.addEventListener("change", renderResolutions);
  if (model) {
    model.addEventListener("change", () => {
      if (!resolutionTouched) {
        const recommended = modes[model.value]?.resolution
          || (model.value === "quality" || model.value === "quality-fast" ? "high" : "standard");
        const choice = resolutionInputs.find((c) => c.name === recommended);
        if (choice) choice.input.checked = true;
      }
      renderResolutions();
    });
  }
  renderResolutions();
  const upscaleInfo = data.status.upscale || {};
  const upscale = h("select", {},
    h("option", { value: "none", selected: true }, "Don't upscale"),
    h("option", { value: "2x", disabled: !upscaleInfo.available }, "Upscale 2× after generate"),
    h("option", { value: "4x", disabled: !upscaleInfo.available }, "Upscale 4× after generate"));
  const phase = imageStatusView(data.status);
  const grid = h("div", { class: "image-grid" });
  const imageGridKey = (img) => [img.id, img.status, img.error, img.prompt, img.finished_at].join("\0");
  const render = (d) => {
    if (d.status.phase === "idle" && !route.imageWarmupPromise) route.imageWarmupStarted = false;
    updateImageStatusView(phase, d.status);
    const keys = d.images.map(imageGridKey).join("\n");
    if (grid.dataset.keys !== keys) {
      grid.dataset.keys = keys;
      fill(grid, d.images.map(imageCard));
    }
    return d;
  };
  render(data);
  const go = h("button", { class: "btn primary", type: "submit" }, "Generate");
  const gpuHold = () => !!(gpu && (gpu.manual || gpu.state !== "clear"));
  const syncHoldUi = () => {
    const queued = gpuHold();
    go.textContent = queued ? "Queue Generation" : "Generate";
    go.classList.toggle("primary", !queued);
    go.classList.toggle("queued", queued);
    go.disabled = !model;
  };
  syncHoldUi();
  const upload = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/jpg", hidden: true, "aria-label": "Upload a photo to edit" });
  const uploadBtn = h("button", { class: "btn", type: "button", onclick: () => upload.click() }, "Upload photo");
  upload.addEventListener("change", async () => {
    const file = upload.files?.[0];
    upload.value = "";
    if (!file) return;
    if (!(await confirmGpuQueue("This image edit"))) return;
    try {
      const body = new FormData();
      body.append("file", file);
      const job = await api("/images/uploads", { method: "POST", body });
      location.hash = `#/images/${job.id}/edit`;
    } catch (err) { toast(err.message); }
  });
  const edit = data.status.edit || {};
  const editHint = (!edit.available || !edit.enabled) && !isGuest()
    ? h("p", { class: "muted small" }, edit.setup || "Masked editing is an optional component.")
    : null;
  const loadNote = "The language model is unloaded while images generate; running tasks pause for a few minutes. ";
  const upscaleNote = loadNote + (upscaleInfo.available
    ? "Upscaling is off unless you choose 2× or 4×."
    : "Real-ESRGAN weights are not installed, so 2×/4× upscaling is unavailable.");
  const uploadControls = edit.available && edit.enabled ? [upload, uploadBtn] : [];
  append($app, 
    isGuest() ? h("p", { class: "muted small" }, "Demo access can view generated images, not start new ones.") : h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!prompt.value.trim()) return toast("Describe the image first");
        if (!model) return toast(IMAGE_MODELS_EMPTY);
        if (!modelChoices.some((m) => m.key === model.value)) return toast(IMAGE_MODELS_EMPTY);
        if (!(await confirmGpuQueue("This image job"))) return;
        go.disabled = true;
        try {
          await startWarmup().catch(() => {});
          const resolution = resolutionInputs.find((choice) => choice.input.checked).name;
          await api("/images", { method: "POST", body: { prompt: prompt.value, model: model.value, aspect_ratio: aspect.value, resolution, upscale: upscale.value } });
          try { localStorage.removeItem(draftKey); } catch (_) { /* ignore */ }
          render(await api("/images"));
        } catch (err) { toast(err.message); }
        syncHoldUi();
      },
    },
    h("label", {}, "Prompt"), prompt,
    h("div", { class: "row" }, h("div", { style: "flex:2" }, h("label", {}, "Model"), model || emptyModels),
      h("div", { style: "flex:1" }, h("label", {}, "Aspect ratio"), aspect)),
    h("div", { class: "resolution-group" }, h("div", { class: "field-label" }, "Resolution"),
      h("div", { class: "resolution-options" }, resolutionInputs.map((choice) => choice.label))),
    h("div", { class: "row" }, h("div", { style: "flex:1" }, h("label", {}, "Upscale"), upscale)),
    h("p", { class: "muted small" }, upscaleNote),
    editHint,
    h("div", { class: "row image-generate-row", style: "margin-top:12px" },
      uploadControls,
      h("span", { class: "spacer" }), go)),
    phase, grid);
  let timer = 0;
  const tick = async () => {
    try {
      const d = render(await api("/images"));
      timer = setTimeout(tick, IMAGE_BUSY.has(d.status.phase) ? 400 : 4000);
    } catch (err) {
      console.debug("image list unavailable; retrying", err);
      timer = setTimeout(tick, 4000);
    }
  };
  timer = setTimeout(tick, IMAGE_BUSY.has(data.status.phase) ? 400 : 4000);
  onLeave(() => clearTimeout(timer));
}

function imageMetaParts(img, when) {
  const meta = [`${img.model} · ${img.width}×${img.height}`];
  if (Number(img.scale) > 1) meta.push(`${img.scale}× ${img.upscale_model || "Real-ESRGAN"}`);
  meta.push(`seed ${img.seed}`, img.source);
  if (img.seconds) meta.push(`${Math.round(img.seconds)} s`);
  if (img.lora) meta.push(`LoRA ${img.lora}`);
  if (img.lora_revision) meta.push(img.lora_revision.slice(0, 8));
  meta.push(when);
  return meta;
}

function provenanceNote(img) {
  const p = img.provenance;
  if (!p || !(p.checkpoint_revision || p.steps)) return null;
  return h("p", { class: "muted small" },
    [p.mode || img.model, p.steps && `${p.steps} steps`,
      p.sampler, p.scheduler, p.guidance != null && `cfg ${p.guidance}`,
      p.checkpoint_revision && `ckpt ${String(p.checkpoint_revision).slice(0, 12)}`,
      p.comfy_revision && `ComfyUI ${p.comfy_revision}`].filter(Boolean).join(" · "));
}

function imageActionRow(img, id, { editControl, canUpscale, startUpscale }) {
  return h("div", { class: "row" },
    !isGuest() && img.status === "done" && (img.operation || "generate") === "generate" ? h("button", {
      class: "btn",
      onclick: async () => {
        try {
          const again = await api("/images", { method: "POST", body: { prompt: img.prompt, model: img.model, aspect_ratio: img.aspect_ratio, resolution: img.resolution } });
          location.hash = `#/images/${again.id}`;
        } catch (e) { toast(e.message); }
      },
    }, "Another one") : null,
    editControl,
    canUpscale ? h("button", { class: "btn", onclick: startUpscale("2x") }, "Upscale 2×") : null,
    canUpscale ? h("button", { class: "btn", onclick: startUpscale("4x") }, "Upscale 4×") : null,
    img.status === "done" ? h("button", { class: "btn", onclick: () => downloadDaemonFile(`/images/${id}.png`, `${id}.png`) }, "Download") : null,
    !isGuest() && (img.status === "queued" || img.status === "running") ? h("button", {
      class: "btn",
      onclick: async () => {
        if (!confirm("Cancel this image job?")) return;
        try { await api(`/images/${id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
      },
    }, "Cancel") : null,
    !isGuest() ? h("button", {
      class: "btn danger",
      onclick: async () => {
        if (!confirm("Delete this image from the live gallery? Independent backups are not changed.")) return;
        try { await api(`/images/${id}`, { method: "DELETE" }); go("#/images", true); } catch (e) { toast(e.message); }
      },
    }, "Delete") : null,
    img.session_id ? h("a", { class: "btn", href: `#/s/${img.session_id}` }, "Open session") : null);
}

async function viewImage(id) {
  setHeader("images", "Image", { page: true });
  const load = async () => {
    const img = await api(`/images/${id}`);
    const when = img.finished_at ? ago(img.finished_at) : ago(img.created_at);
    const edit = (img.service?.edit) || {};
    const sizeOk = img.editable !== false;
    const editReady = !isGuest() && img.status === "done" && edit.enabled && edit.available;
    const canEdit = editReady && sizeOk;
    const editBlockedReason = img.editable_reason || "This source is too large to edit. Use the original or a non-upscaled image.";
    const meta = imageMetaParts(img, when);
    const canUpscale = img.status === "done" && !isGuest() && !img.private && Number(img.scale || 1) === 1;
    const startUpscale = (choice) => async () => {
      try {
        if (!(await confirmGpuQueue("This upscale job"))) return;
        const next = await api(`/images/${id}/upscale`, { method: "POST", body: { upscale: choice } });
        location.hash = `#/images/${next.id}`;
      } catch (e) { toast(e.message); }
    };
    const noteClass = `note${img.status === "failed" || img.status === "cancelled" ? " bad" : ""}`;
    const imageNote = () => {
      if (img.status === "failed") return `Failed: ${img.error}`;
      return img.status === "cancelled" ? "Cancelled" : imageStatusView(img.service);
    };
    let editControl = null;
    if (canEdit) editControl = h("a", { class: "btn", href: `#/images/${id}/edit` }, "Edit");
    else if (editReady) editControl = h("button", { class: "btn", type: "button", disabled: true, title: editBlockedReason }, "Edit");
    fill($app,
      img.status === "done" ? h("a", { href: `#/images/${id}/full` }, daemonImage(`/images/${id}.png`, { class: "image-full", alt: img.prompt }))
        : h("p", { class: noteClass }, imageNote()),
      h("div", { class: "card" },
        h("p", {}, img.prompt),
        h("p", { class: "muted small" }, meta.join(" · ")),
        provenanceNote(img),
        img.parent?.id ? h("p", { class: "muted small" }, "Derived from ",
          h("a", { href: `#/images/${img.parent.id}` }, `${img.parent.width}×${img.parent.height}`)) : null,
        (img.children || []).length ? h("p", { class: "muted small" }, "Derived: ",
          ...(img.children.flatMap((c, i) => [i ? ", " : "", h("a", { href: `#/images/${c.id}` },
            c.operation === "upscale" ? `${c.scale}×` : c.operation)]))) : null,
        !isGuest() && (!edit.enabled || !edit.available) ? h("p", { class: "muted small" }, edit.setup || "") : null,
        imageActionRow(img, id, { editControl, canUpscale, startUpscale })));
    return img;
  };
  let img = await load();
  const timer = setInterval(async () => {
    if (img.status === "done" || img.status === "failed" || img.status === "cancelled") return clearInterval(timer);
    try { img = await load(); } catch (_) { /* offline */ }
  }, 400);
  onLeave(() => clearInterval(timer));
}

function maskEditor(width, height, previewImg) {
  const canvas = h("canvas", {
    class: "mask-canvas", width, height, "aria-label": "Edit mask",
  });
  canvas.style.width = "100%";
  canvas.style.height = "auto";
  canvas.style.touchAction = "none";
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, width, height);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  let mode = "draw";
  let size = Math.max(12, Math.round(Math.min(width, height) / 24));
  let drawing = false;
  const pos = (ev) => {
    const r = canvas.getBoundingClientRect();
    return [(ev.clientX - r.left) * (canvas.width / r.width), (ev.clientY - r.top) * (canvas.height / r.height)];
  };
  const paint = (x, y) => {
    ctx.strokeStyle = mode === "draw" ? "#fff" : "#000";
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = size;
    ctx.lineTo(x, y);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(x, y, size / 2, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(x, y);
  };
  canvas.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    canvas.setPointerCapture(ev.pointerId);
    drawing = true;
    const [x, y] = pos(ev);
    ctx.beginPath();
    ctx.moveTo(x, y);
    paint(x, y);
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (!drawing) return;
    ev.preventDefault();
    const [x, y] = pos(ev);
    paint(x, y);
  });
  const stop = (ev) => {
    if (!drawing) return;
    drawing = false;
    try { canvas.releasePointerCapture(ev.pointerId); } catch (_) { /* already released */ }
  };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
  const tools = {
    setMode(next) { mode = next; },
    setSize(next) { size = Math.max(2, Number(next) || size); },
    clear() { ctx.fillStyle = "#000"; ctx.fillRect(0, 0, width, height); },
    invert() {
      const data = ctx.getImageData(0, 0, width, height);
      for (let i = 0; i < data.data.length; i += 4) {
        data.data[i] = 255 - data.data[i];
        data.data[i + 1] = 255 - data.data[i + 1];
        data.data[i + 2] = 255 - data.data[i + 2];
      }
      ctx.putImageData(data, 0, 0);
    },
    preview(on) { canvas.classList.toggle("mask-preview", on); previewImg.classList.toggle("mask-preview-source", on); },
    blob() { return new Promise((resolve) => canvas.toBlob(resolve, "image/png")); },
    canvas,
  };
  return tools;
}

async function viewImageEdit(id) {
  if (isGuest()) { go(`#/images/${id}`, true); return; }
  setHeader("images", "Edit", { page: true });
  const img = await api(`/images/${id}`);
  const edit = (img.service?.edit) || {};
  if (img.status !== "done") { go(`#/images/${id}`, true); return; }
  if (!edit.enabled || !edit.available) {
    append($app, h("p", { class: "note" }, edit.setup || "Masked editing is not installed."),
      h("a", { class: "btn", href: `#/images/${id}` }, "Back"));
    return;
  }
  if (img.editable === false) {
    append($app, h("p", { class: "note" },
      img.editable_reason || "This source is too large to edit. Use the original or a non-upscaled image."),
      h("a", { class: "btn", href: `#/images/${id}` }, "Back"));
    return;
  }
  const source = daemonImage(`/images/${id}.png`, { class: "mask-source", alt: img.prompt });
  const waitForImage = () => new Promise((resolve, reject) => {
    if (source.complete && source.naturalWidth) return resolve();
    source.addEventListener("load", () => resolve(), { once: true });
    source.addEventListener("error", () => reject(new Error("Could not load the source image")), { once: true });
  });
  try { await waitForImage(); } catch (e) { append($app, h("p", { class: "note bad" }, e.message)); return; }
  const width = source.naturalWidth || img.width;
  const height = source.naturalHeight || img.height;
  const editor = maskEditor(width, height, source);
  const prompt = h("textarea", { placeholder: "Describe the edit…" });
  const brush = h("input", { type: "range", min: "4", max: "96", value: String(Math.max(12, Math.round(Math.min(width, height) / 24))), "aria-label": "Brush size" });
  brush.addEventListener("input", () => editor.setSize(brush.value));
  const feather = h("input", { type: "range", min: "0", max: "32", value: "0", "aria-label": "Feather" });
  const draw = h("button", { class: "btn selected", type: "button", onclick: () => { editor.setMode("draw"); draw.classList.add("selected"); erase.classList.remove("selected"); } }, "Draw");
  const erase = h("button", { class: "btn", type: "button", onclick: () => { editor.setMode("erase"); erase.classList.add("selected"); draw.classList.remove("selected"); } }, "Erase");
  const preview = h("label", { class: "row" }, h("input", { type: "checkbox", onchange: (e) => editor.preview(e.target.checked) }), " Preview mask");
  const go = h("button", { class: "btn primary", type: "submit" }, "Edit");
  append($app, 
    h("p", { class: "muted small" }, `White is edited, black is preserved · ${width}×${height}`),
    h("div", { class: "mask-stage" }, source, editor.canvas),
    h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!prompt.value.trim()) return toast("Describe the edit first");
        if (!(await confirmGpuQueue("This image edit"))) return;
        go.disabled = true;
        try {
          const mask = await editor.blob();
          if (!mask) throw new Error("Could not read the mask");
          const body = new FormData();
          body.append("prompt", prompt.value);
          body.append("feather", feather.value || "0");
          body.append("mask", mask, "mask.png");
          const job = await api(`/images/${id}/edit`, { method: "POST", body });
          location.hash = `#/images/${job.id}`;
        } catch (err) { toast(err.message); }
        go.disabled = false;
      },
    },
    h("div", { class: "row mask-tools" }, draw, erase,
      h("button", { class: "btn", type: "button", onclick: () => editor.clear() }, "Clear"),
      h("button", { class: "btn", type: "button", onclick: () => editor.invert() }, "Invert")),
    h("label", {}, "Brush size"), brush,
    h("label", {}, "Feather (defaults to 0)"), feather,
    preview,
    h("label", {}, "Edit prompt"), prompt,
    h("div", { class: "row", style: "margin-top:12px" }, h("span", { class: "spacer" }), go)));
}

async function viewImageFull(id) {
  const img = await api(`/images/${id}`);
  if (img.status !== "done") { go(`#/images/${id}`, true); return; }
  setHeader("images", "Image", { page: true });
  fill($app, h("div", { class: "image-viewer" },
    daemonImage(`/images/${id}.png`, { alt: img.prompt })));
}

// ---------- scheduled jobs ----------
const JOB_NOTIFY = {
  attention: "Only when something needs attention",
  low: "Quietly when OK (low-priority notification)",
  always: "Every run",
};
const CRON_PRESETS = [
  ["0 8 * * *", "Every day at 8:00"], ["0 7 * * 1-5", "Weekdays at 7:00"], ["0 * * * *", "Every hour"],
  ["*/30 * * * *", "Every 30 minutes"], ["0 10 * * 0", "Sundays at 10:00"], ["0 9 1 * *", "Monthly, on the 1st at 9:00"],
];
const jobStatusBadge = (st) => h("span", { class: `badge ${st === "ok" ? "done" : "waiting_approval"}` }, st === "ok" ? "OK" : "⚠ attention");
const fmtWhen = (ts) => new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const whenText = (ts) => {
  if (!ts) return "—";
  const s = ts - Date.now() / 1000;
  let rel = `in ${Math.round(s / 86400)} d`;
  if (s < 0) rel = "due";
  else if (s < 3600) rel = `in ${Math.max(1, Math.round(s / 60))} min`;
  else if (s < 86400) rel = `in ${Math.round(s / 3600)} h`;
  return `${fmtWhen(ts)} (${rel})`;
};
const cronLabel = (cron) => (CRON_PRESETS.find(([c]) => c === cron) || [null, cron])[1];

async function viewJobs() {
  setHeader("jobs", "Jobs");
  showFab("#/jobs/new", "+ New job");
  const jobs = await api("/jobs");
  if (!jobs.length) {
    append($app, h("p", { class: "empty" }, "No scheduled jobs yet. A job runs a task on a schedule, such as a morning homelab check, and notifies you only when something needs attention."));
    return;
  }
  append($app, ...jobs.map((j) => {
    const last = j.recent[0];
    const lastJobBadge = last?.job_status ? jobStatusBadge(last.job_status) : null;
    return h("a", { class: "card", href: `#/jobs/${j.id}` },
      h("h3", {}, `${j.enabled ? "" : "⏸ "}${j.name}`),
      h("div", { class: "meta" }, h("span", {}, cronLabel(j.cron)), h("span", {}, j.project),
        j.enabled ? h("span", {}, `next ${whenText(j.next_run_at)}`) : h("span", {}, "paused")),
      last ? h("div", { class: "meta", style: "margin-top:4px" }, h("span", {}, `last run ${ago(last.created_at)}`),
        badge(last.status), lastJobBadge) : null,
      j.last_error ? h("div", { class: "preview bad" }, `Couldn't start: ${j.last_error}`) : null);
  }));
}

async function viewJob(id) {
  const isNew = id === "new";
  setHeader("jobs", isNew ? "New job" : "Job", { page: true });
  const [projects, models, backends, job] = await Promise.all([
    api("/projects"), api("/models"), api("/backends?auth=skip"), isNew ? null : api(`/jobs/${id}`)]);
  const j = job || newJobDefaults(projects);
  const name = h("input", { type: "text", value: j.name, placeholder: "e.g. Morning homelab check" });
  const prompt = h("textarea", { placeholder: "e.g. Check that every homelab service is running and nothing restarted overnight. Look at the logs of anything that isn't healthy." });
  prompt.value = j.prompt;
  const custom = !CRON_PRESETS.some(([c]) => c === j.cron);
  const preset = h("select", {}, CRON_PRESETS.map(([c, label]) => h("option", { value: c, selected: c === j.cron }, label)),
    h("option", { value: "", selected: custom }, "Custom (cron)"));
  const cron = h("input", { type: "text", value: j.cron, placeholder: "minute hour day month weekday", style: "font-family:var(--mono)" });
  const cronNote = h("div", { class: "muted small", style: "margin-top:6px" });
  const project = h("select", {}, projects.filter((p) => p.target === "tower" || p.name === j.project)
    .map((p) => h("option", { value: p.name, selected: p.name === j.project }, p.description ? `${p.name} — ${p.description}` : p.name)));
  const model = h("select", {}, h("option", { value: "" }, "Default model"),
    models.map((m) => h("option", { value: m.name, selected: m.name === j.model }, m.name)));
  let localModel = (j.backend || "local") === "local" ? j.model : "";
  model.addEventListener("change", () => { localModel = model.value; });
  const backend = h("select", {}, backends.filter((b) => b.available).map((b) =>
    h("option", { value: b.name, selected: b.name === (j.backend || "local") }, b.name === "local" ? "Local model" : b.name)));
  const backendNote = h("div", { class: "muted small", style: "margin-top:6px" });
  const showBackend = () => {
    const b = backends.find((x) => x.name === backend.value);
    const isLocal = backend.value === "local";
    if (isLocal) {
      fill(model, h("option", { value: "", selected: !localModel }, "Default model"),
        models.map((m) => h("option", { value: m.name, selected: m.name === localModel }, m.name)));
    } else {
      if (!model.disabled) localModel = model.value;
      fill(model, h("option", { value: b?.model || "" }, b?.model || `${backend.value} default`));
    }
    model.disabled = !isLocal;
    backendNote.textContent = b?.billing_warning || "";
    backendNote.classList.toggle("bad", !!b?.billing_warning);
  };
  backend.addEventListener("change", showBackend);
  showBackend();
  const notify = h("select", {}, Object.entries(JOB_NOTIFY).map(([k, label]) => h("option", { value: k, selected: k === j.notify }, label)));
  const enabled = h("input", { type: "checkbox", checked: j.enabled });
  let previewTimer = null;
  const preview = () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      await previewCron(cron.value, cronNote);
    }, 250);
  };
  preset.addEventListener("change", () => { if (preset.value) { cron.value = preset.value; preview(); } else cron.focus(); });
  cron.addEventListener("input", () => {
    const match = CRON_PRESETS.find(([c]) => c === cron.value.trim());
    preset.value = match ? match[0] : "";
    preview();
  });
  preview();
  if (isGuest()) {
    [name, prompt, cron, preset, project, backend, model, notify, enabled].forEach((el) => { el.disabled = true; });
  }
  const body = () => ({ name: name.value, prompt: prompt.value, cron: cron.value, project: project.value,
    backend: backend.value, model: backend.value === "local" ? model.value : "", notify: notify.value, enabled: enabled.checked });
  const save = h("button", { class: "btn primary", type: "submit" }, isNew ? "Create" : "Save");
  append($app, h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      if (isNew && backend.value === "local" && !(await confirmGpuQueue("This scheduled job"))) return;
      save.disabled = true;
      try {
        const saved = await api(isNew ? "/jobs" : `/jobs/${id}`, { method: isNew ? "POST" : "PUT", body: body() });
        toast(`Saved · next run ${whenText(saved.next_run_at)}`, 3500);
        if (isNew) location.hash = `#/jobs/${saved.id}`; else route();
      } catch (err) { toast(err.message, 5000); }
      save.disabled = false;
    },
  },
  h("label", {}, "Name"), name,
  h("label", {}, "Task"), prompt,
  h("p", { class: "muted small" }, "The agent is asked to end with STATUS: OK or STATUS: ATTENTION, which decides how loudly you're notified. Approvals always notify."),
  h("label", {}, "Schedule (tower time)"), preset, h("div", { style: "margin-top:8px" }, cron), cronNote,
  h("label", {}, "Project"), project,
  h("label", {}, "Backend"), backend, backendNote,
  h("label", {}, "Model"), model,
  h("label", {}, "Notify me"), notify,
  h("label", { class: "row", style: "font-weight:500" }, enabled, "Enabled"),
  h("div", { class: "row", style: "margin-top:18px" },
    !isGuest() && !isNew ? h("button", {
      class: "btn bad", type: "button",
      onclick: async () => {
        if (!confirm(`Delete the job “${j.name}”? Its past sessions stay.`)) return;
        try { await api(`/jobs/${id}`, { method: "DELETE" }); go("#/jobs", true); } catch (err) { toast(err.message); }
      },
    }, "Delete") : null,
    h("span", { class: "spacer" }),
    !isGuest() && !isNew ? h("button", {
      class: "btn", type: "button",
      onclick: async () => {
        if (backend.value === "local" && !(await confirmGpuQueue("This job run"))) return;
        try { const s = await api(`/jobs/${id}/run`, { method: "POST" }); location.hash = `#/s/${s.id}`; } catch (err) { toast(err.message); }
      },
    }, "Run now") : null,
    isGuest() ? null : save)));
  if (job) append($app, ...recentRunsView(job));
}

const newJobDefaults = (projects) => ({
  name: "", prompt: "", cron: "0 8 * * *", backend: "local", model: "", notify: "low", enabled: true,
  project: projects.some((p) => p.name === "homelab") ? "homelab" : "scratch",
});

async function previewCron(cronValue, cronNote) {
  try {
    const r = await api(`/jobs/preview?cron=${encodeURIComponent(cronValue)}`);
    cronNote.classList.toggle("bad", !r.ok);
    cronNote.textContent = r.ok ? `Next: ${r.next.map(fmtWhen).join(" · ")}` : r.error;
  } catch (_) { /* offline */ }
}

function recentRunsView(job) {
  return [h("h3", { style: "margin-top:28px" }, "Recent runs"),
    job.last_skip ? h("p", { class: "muted small" }, `Last skipped: ${job.last_skip}`) : null,
    job.last_error ? h("p", { class: "small bad" }, `Last start failed: ${job.last_error}`) : null,
    job.recent.length ? job.recent.map((s) => h("a", { class: "card", href: `#/s/${s.id}` },
      h("div", { class: "meta" }, badge(s.status), s.job_status ? jobStatusBadge(s.job_status) : null, h("span", {}, ago(s.created_at))),
      s.answer ? h("div", { class: "preview" }, s.answer) : null)) : h("p", { class: "muted small" }, "No runs yet.")];
}

// ---------- profile ----------
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
applyTheme();

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
applyTextSize();

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
      copyToClipboard(value, () => {
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
    h("div", { class: "card" },
      h("h3", {}, "Connection"),
      me.public_url ? copyBox(me.public_url) : h("p", { class: "muted small" }, "No public URL configured."),
      h("p", { class: "muted small" }, live
        ? `Agent Harness Web is connected to Agent Harness Server at ${me.public_url || location.origin}.`
        : `Agent Harness Web is not receiving the live stream from Agent Harness Server at ${me.public_url || location.origin}.`)));
}

function fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(1)} GiB`;
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MiB`;
  return `${n} B`;
}

async function accountsCard() {
  const wrap = h("div");
  const render = async () => {
    let rows = [];
    try { rows = await api("/accounts", { surface: "admin" }); }
    catch (e) { fill(wrap, h("p", { class: "note bad" }, e.message)); return; }
    const login = h("input", { type: "email", placeholder: "member@example.com", required: true });
    const name = h("input", { type: "text", placeholder: "Display name", required: true, maxlength: "80" });
    const create = h("button", { class: "btn primary", type: "submit" }, "Create member");
    fill(wrap,
      h("p", { class: "muted small" }, "Household members authenticate with the exact Tailscale login you enter. The machine owner can still read local storage; this page prevents accidental API and UI cross-account access."),
      h("form", { class: "card", onsubmit: async (e) => {
        e.preventDefault();
        create.disabled = true;
        try {
          await api("/accounts", { method: "POST", surface: "admin", body: {
            login: login.value, display_name: name.value,
          } });
          toast("Member created");
          await render();
        } catch (err) { toast(err.message, 5000); create.disabled = false; }
      } }, h("h3", {}, "New member"), login, name, h("div", { class: "row", style: "margin-top:12px" }, create)),
      rows.length ? rows.map((a) => {
        const patch = async (body, confirmText) => {
          if (confirmText && !confirm(confirmText)) return;
          try {
            await api(`/accounts/${a.user_id}`, { method: "PATCH", surface: "admin", body });
            await render();
          } catch (err) { toast(err.message, 5000); }
        };
        return h("div", { class: "card" },
          h("h3", {}, a.display_name),
          h("p", { class: "muted small" }, a.login),
          h("p", { class: "muted small" }, `id ${a.account_hint} · ${a.enabled ? "enabled" : "disabled"}`),
          h("p", { class: "muted small" }, `${fmtBytes(a.disk_used_bytes)} / ${fmtBytes(a.disk_quota_bytes)} · ${a.running} running · ${a.queued} queued`),
          a.last_activity_at ? h("p", { class: "muted small" }, `Last activity ${ago(a.last_activity_at)}`) : null,
          h("div", { class: "row", style: "flex-wrap:wrap;gap:8px" },
            h("button", { class: "btn small", type: "button", onclick: () => {
              const next = window.prompt("Display name", a.display_name);
              if (next) patch({ display_name: next });
            } }, "Rename"),
            h("button", { class: "btn small", type: "button", onclick: () => {
              const next = window.prompt("New Tailscale login", a.login);
              if (next && next !== a.login && confirm(`Rebind this account to ${next}? The old login stops working immediately.`)) {
                patch({ login: next });
              }
            } }, "Rebind login"),
            h("button", { class: "btn small", type: "button", onclick: () => {
              const next = window.prompt("Disk quota in GiB", String(Math.round(a.disk_quota_bytes / 2 ** 30)));
              if (next) patch({ disk_quota_bytes: Math.round(Number(next) * 2 ** 30) });
            } }, "Quota"),
            h("button", { class: "btn small", type: "button", onclick: () => {
              const running = window.prompt("Max running sessions", String(a.max_running));
              const queued = window.prompt("Max queued sessions", String(a.max_queued));
              if (running || queued) patch({
                max_running: running ? Number(running) : a.max_running,
                max_queued: queued ? Number(queued) : a.max_queued,
              });
            } }, "Concurrency"),
            h("button", { class: "btn small", type: "button", onclick: () => patch(
              { enabled: !a.enabled },
              a.enabled ? `Disable ${a.display_name}? Running work will be cancelled.` : `Re-enable ${a.display_name}?`,
            ) }, a.enabled ? "Disable" : "Re-enable")));
      }) : h("p", { class: "muted small" }, "No household members yet."));
  };
  await render();
  return wrap;
}

const ACTION_TABS = [
  ["gpu", "GPU"],
  ["accounts", "Accounts"],
  ["remote-control", "Claude Remote Control"],
  ["disk", "Disk"],
];

async function viewActions(tab) {
  const selected = ACTION_TABS.some(([id]) => id === tab) ? tab : "gpu";
  if (tab !== selected) { go("#/actions/gpu", true); return; }
  setHeader("agents", "Actions");
  const tabs = h("div", { class: "tabs", role: "tablist", "aria-label": "Actions" },
    ACTION_TABS.map(([id, label]) => h("button", {
      type: "button", role: "tab", class: id === selected ? "on" : "",
      "aria-selected": id === selected ? "true" : "false",
      onclick: () => go(`#/actions/${id}`),
    }, label)));
  let panel;
  if (selected === "gpu") panel = h("div", { class: "card settings-list" }, gpuActionRow());
  else if (selected === "accounts") panel = await accountsCard();
  else panel = selected === "remote-control" ? remoteControlCard() : diskCard();
  append($app, tabs, panel);
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
  setHeader("agents", titles[page] || "Profile", { page: true });
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
          copyToClipboard(key.key, () => field.select());
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
        h("button", { class: "btn", onclick: () => { server.value = ""; token.value = ""; save(); } }, "Use bundled Server"),
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
      else save(sel.value);
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

function settingInput(spec, draft) {
  const current = draft[spec.key] !== undefined ? draft[spec.key] : (spec.pending ?? spec.effective);
  if (spec.type === "bool") {
    const box = h("input", { type: "checkbox", class: "switch", checked: !!current, disabled: !spec.writable });
    box.addEventListener("change", () => { draft[spec.key] = box.checked; });
    return box;
  }
  if (spec.enum?.length) {
    const sel = h("select", { disabled: !spec.writable }, spec.enum.map((item) =>
      h("option", { value: item, selected: item === current }, item)));
    sel.addEventListener("change", () => { draft[spec.key] = sel.value; });
    return sel;
  }
  const input = h("input", {
    type: spec.type === "string" ? "text" : "number",
    value: current == null ? "" : String(current),
    disabled: !spec.writable,
    min: spec.minimum, max: spec.maximum, step: spec.type === "int" ? "1" : "any",
  });
  input.addEventListener("change", () => {
    if (input.value === "") { draft[spec.key] = null; return; }
    draft[spec.key] = spec.type === "string" ? input.value : Number(input.value);
  });
  return input;
}

function settingValueText(spec) {
  const configured = spec.configured != null && spec.configured !== spec.effective ? ` · configured ${spec.configured}` : "";
  const inherited = spec.inherited != null ? ` · inherited ${spec.inherited}` : "";
  return `effective ${spec.effective == null ? "—" : spec.effective}${configured}${inherited}`;
}

function settingMeta(spec) {
  const bits = [];
  bits.push({ live: "applies live", daemon_restart: "needs restart" }[spec.apply] || "file only");
  if (spec.source) bits.push(`source: ${spec.source}`);
  if (spec.pending != null && spec.apply === "daemon_restart") bits.push(`pending: ${spec.pending}`);
  if (spec.capped_by) bits.push(`capped by ${spec.capped_by}`);
  if (spec.file_only) bits.push(spec.guidance || "managed in local configuration");
  return bits.join(" · ");
}

const lastUpdateText = (update) => `${update.ok ? "succeeded" : "failed"}: ${update.message}`;

function compatibilityText(compatibility) {
  const { supported } = compatibility;
  const range = supported ? ` · Server supports ${supported.min}–${supported.max}` : "";
  return `${compatibility.state || "not reported"}${range}`;
}

function lastSeenText(runner) {
  if (runner.last_seen_seconds === null) return "not connected since Agent Harness Server started";
  return `last seen ${Math.round(runner.last_seen_seconds / 60)} min ago`;
}

function recoveryNote(recovery) {
  if (recovery?.recovery === "overlay_quarantined") {
    return ` · ${recovery.reason || "managed overlay quarantined; YAML defaults in effect"}`;
  }
  return recovery?.recovery ? ` · recovered from ${recovery.reason || "failed generation"}` : "";
}

async function daemonSettingsCard() {
  let view;
  try { view = await api("/config"); }
  catch (e) { return h("div", { class: "card" }, h("p", { class: "note bad" }, e.message)); }
  const draft = {};
  const status = h("p", { class: "muted small" },
    `Revision ${view.revision}` +
    (view.pending_revision ? ` · pending ${view.pending_revision}` : "") +
    (view.supervised_restart ? " · supervised restart supported" : " · unsupervised (restart is manual)") +
    (view.warning ? ` · ${view.warning}` : recoveryNote(view.recovery)));
  const restartButton = view.restart_required ? h("button", { class: "btn", type: "button", onclick: () => confirmRestart(view.pending_revision || view.revision, status, errorBox) },
    "Restart daemon") : null;
  const planBox = h("div", { class: "config-plan" });
  const errorBox = h("div");
  const groups = {};
  for (const spec of view.settings || []) {
    groups[spec.category] ||= [];
    groups[spec.category].push(spec);
  }
  const rows = Object.entries(groups).map(([category, specs]) => h("div", { class: "card config-category" },
    h("h3", {}, category),
    specs.map((spec) => h("div", { class: "config-row" },
      h("div", { class: "config-copy" },
        h("label", { class: "field-label" }, spec.label),
        h("p", { class: "muted small" }, spec.help),
        h("p", { class: "muted small config-meta" }, settingMeta(spec)),
        spec.file_only ? null : h("p", { class: "muted small" }, settingValueText(spec))),
      spec.file_only ? h("span", { class: "muted small" }, "local config") : settingInput(spec, draft)))));

  const apply = async ({ restart = false, rollback = false } = {}) => {
    fill(errorBox);
    fill(planBox);
    const changes = {};
    for (const [key, value] of Object.entries(draft)) changes[key] = value;
    try {
      if (rollback) {
        await rollbackConfig(view.revision, status, errorBox);
        return;
      }
      const plan = await api("/config", { method: "PATCH", body: { revision: view.revision, dry_run: true, changes } });
      append(planBox, h("p", { class: "field-label" }, "Change plan"), configPlanList(plan));
      const enables = (plan.changes || []).filter((c) => c.to === true && String(c.key).endsWith(".enabled"));
      if (enables.length && !window.confirm(`Enable ${enables.map((c) => c.key).join(", ")}?`)) return;
      if (!(plan.changes || []).length) return;
      if (!window.confirm("Apply these server settings?")) return;
      const result = await api("/config", { method: "PATCH", body: { revision: view.revision, changes } });
      toast("Saved");
      if (result.restart_required || restart) {
        await confirmRestart(result.pending_revision || result.target_revision || result.revision, status, errorBox);
      } else { location.reload(); }
    } catch (e) {
      showConfigError(errorBox, e);
    }
  };

  return h("div", {},
    h("div", { class: "card" },
      h("p", { class: "muted small" }, "Operational settings for this daemon. Paths, secrets, modules, and network policy stay in local configuration."),
      status),
    ...rows,
    planBox, errorBox,
    isGuest() ? null : h("div", { class: "card config-actions" },
      h("button", { class: "btn", type: "button", onclick: () => apply() }, "Review and apply"),
      h("button", { class: "btn", type: "button", onclick: () => apply({ rollback: true }) }, "Roll back"),
      restartButton));
}

function configPlanList(plan) {
  if (!(plan.changes || []).length) return h("p", { class: "muted small" }, "No changes.");
  return h("ul", { class: "config-plan-list" }, plan.changes.map((c) =>
    h("li", {}, `${c.key}: ${c.from} → ${c.action === "reset" ? "inherited" : c.to} (${c.apply})`)));
}

function showConfigError(errorBox, e) {
  if (e.code === "revision_conflict") {
    append(errorBox, h("p", { class: "note bad" }, "This page is stale. Reload to edit the current revision."));
  } else if (e.keys) {
    append(errorBox, h("p", { class: "note bad" }, e.message),
      h("ul", {}, Object.entries(e.keys).map(([key, info]) =>
        h("li", {}, `${key}: ${info.message || info.code}`))));
  } else {
    append(errorBox, h("p", { class: "note bad" }, e.message));
  }
}

async function rollbackConfig(revision, status, errorBox) {
  if (!window.confirm("Restore the previous confirmed server configuration?")) return;
  const result = await api("/config/rollback", { method: "POST", body: { revision, confirm: true } });
  toast("Rolled back");
  if (result.restart_required) {
    await confirmRestart(result.pending_revision || result.revision, status, errorBox);
  } else { location.hash = "#/profile/daemon"; location.reload(); }
}

async function confirmRestart(targetRevision, status, errorBox) {
  if (!window.confirm("Restart the daemon to apply pending settings?")) return;
  status.textContent = "Restarting… reconnecting to see whether the target revision became active.";
  try {
    await api("/config/restart", { method: "POST", body: { revision: targetRevision, confirm: true } });
  } catch (e) {
    if (e.code === "restart_not_supervised") {
      append(errorBox, h("p", { class: "note bad" }, e.message));
      return;
    }
    // 202 may still parse as success; a dropped connection is expected.
  }
  const started = Date.now();
  while (Date.now() - started < 45000) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    try {
      const next = await api("/config");
      if (next.revision === targetRevision && next.confirmed) {
        toast("Restarted with the new configuration");
        location.reload();
        return;
      }
      if (next.recovery?.recovery === "lkg_restore") {
        append(errorBox, h("p", { class: "note bad" },
          `Automatic recovery restored revision ${next.revision}. ${next.recovery.reason || ""}`.trim()));
        return;
      }
      if (next.recovery?.recovery === "overlay_quarantined") {
        append(errorBox, h("p", { class: "note bad" },
          next.warning || next.recovery.warning || next.recovery.reason ||
          "Managed overlay was quarantined; YAML defaults are in effect."));
        return;
      }
    } catch (_) { /* daemon still down */ }
  }
  append(errorBox, h("p", { class: "note bad" }, "Timed out waiting for the daemon to come back."));
}

function backupLine(b) {
  if (!b?.enabled) return null;
  const failed = b.error && (b.error_at || 0) > (b.ok_at || 0);
  return h("p", { class: `small${failed ? " bad" : ""}` },
    b.ok_at ? `Backup ${ago(b.ok_at)} (${Math.max(1, Math.round(b.bytes / 2 ** 20))} MB) in ${b.dir}` : "No backup yet",
    failed ? ` · last attempt failed: ${b.error}` : "");
}

function imageArchiveBlock(a, reload) {
  if (!a?.enabled) return null;
  const count = Number(a.archived || 0);
  const bytes = Number(a.bytes || 0);
  const warning = a.free_space_warning || (a.errors ? pluralize(a.errors, "image archive error") : "");
  const summary = h("div", {},
    h("p", { class: `small${warning ? " bad" : ""}` },
      h("strong", {}, "Image archive"), " ",
      `${count} image${count === 1 ? "" : "s"} · ${Math.round(bytes / 2 ** 20)} MB · ${a.path}`),
    a.last_reconciliation ? h("p", { class: "muted small" },
      `Reconciled ${ago(a.last_reconciliation)} · ${a.missing || 0} missing · ${a.errors || 0} errors`) : null,
    warning ? h("p", { class: "note bad" }, warning) : null);
  if (!a.retention_days) return summary;
  return h("div", {}, summary,
    h("button", { class: "btn secondary", onclick: async (ev) => {
      ev.target.disabled = true;
      try {
        const p = await api("/maintenance/image-archive/retention/preview", { method: "POST" });
        if (!p.count) { toast("No archived images are old enough to remove"); return; }
        const size = Math.round(p.bytes / 2 ** 20);
        if (!confirm(`Permanently remove ${p.count} archived image${p.count === 1 ? "" : "s"} (${size} MB)? Live gallery images are not deleted.`)) return;
        const r = await api("/maintenance/image-archive/retention/apply", {
          method: "POST", body: JSON.stringify({ confirmation: p.confirmation }),
        });
        toast(`Removed ${r.removed} archived image${r.removed === 1 ? "" : "s"} (${Math.round(r.bytes / 2 ** 20)} MB)`);
        reload();
      } catch (e) { toast(e.message); }
      finally { ev.target.disabled = false; }
    } }, `Review ${a.retention_days}-day image retention`));
}

function gpuText(g) {
  const why = (g.reasons || []).map((r) => r.detail).filter((d, i, a) => a.indexOf(d) === i).join(", ") || "GPU busy";
  if (g.manual) {
    if (g.manual_remaining_seconds === null) return "Local models held until you turn this off";
    const left = g.manual_remaining_seconds;
    return `Local models held for ${fmtSpan(left, Math.ceil)}`;
  }
  if (g.state === "pausing") return `Pausing for ${why}: finishing the current model turn`;
  if (g.state === "paused") return `Paused for ${why}`;
  if (g.state === "resuming") return "GPU free: reloading the model";
  return "Agents have the GPU";
}

function gpuActionRow() {
  const status = h("div", { class: "muted small" }, "Checking…");
  const toggle = h("input", { class: "switch", type: "checkbox", role: "switch", "aria-label": "GPU hold", disabled: isGuest() });
  const duration = h("select", { disabled: isGuest(), "aria-label": "GPU hold duration" },
    h("option", { value: "" }, "Until I turn it off"),
    h("option", { value: "1800" }, "30 minutes"),
    h("option", { value: "3600" }, "1 hour"),
    h("option", { value: "10800" }, "3 hours"));
  const durationRow = h("label", { class: "action-subitem disabled" },
    h("span", {}, "Duration:"), duration);
  const act = async (action) => {
    const seconds = duration.value ? Number(duration.value) : null;
    const body = action === "pause" ? { duration_seconds: seconds } : undefined;
    try { render(await api(`/gpu/${action}`, { method: "POST", body })); } catch (e) { toast(e.message); }
    setTimeout(load, 1500);
  };
  const render = (g) => {
    if (!g.enabled) {
      toggle.disabled = true;
      duration.disabled = true;
      durationRow.classList.add("disabled");
      status.textContent = "GPU guard disabled";
      return;
    }
    const now = g.signals.map((s) => s.detail);
    toggle.checked = g.manual;
    if (g.manual && g.manual_duration_seconds) duration.value = String(g.manual_duration_seconds);
    duration.disabled = isGuest() || !g.manual;
    durationRow.classList.toggle("disabled", duration.disabled);
    const automatic = !g.manual && g.state !== "clear" ? ` · ${gpuText(g)}` : "";
    const ignored = g.override ? " (ignored)" : "";
    const using = now.length ? ` · ${now.join(", ")}${ignored}` : "";
    status.textContent = `${g.manual ? gpuText(g) : "Local models available"}${automatic}${using}`;
  };
  const load = async () => { try { render(await api("/gpu")); } catch (e) { status.textContent = e.message; status.classList.add("bad"); } };
  toggle.addEventListener("change", () => {
    duration.disabled = isGuest() || !toggle.checked;
    durationRow.classList.toggle("disabled", duration.disabled);
    act(toggle.checked ? "pause" : "resume");
  });
  duration.addEventListener("change", () => { if (toggle.checked) act("pause"); });
  load();
  const timer = setInterval(load, 5000);
  onLeave(() => clearInterval(timer));
  return h("div", { class: "action-item" },
    h("div", { class: "action-row" }, h("div", {}, h("strong", {}, "GPU"), status), toggle),
    durationRow,
    isGuest() ? h("div", { class: "muted small" }, "Demo access cannot change GPU hold.") : null);
}

function remoteControlCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Checking…"));
  let busy = "";
  const act = async (project, stop) => {
    busy = project;
    load();
    try {
      const r = await api(`/remote-control/${encodeURIComponent(project)}${stop ? "/stop" : ""}`, { method: "POST" });
      if (stop) toast(`Stopped Remote Control for ${project}`);
      else toast(r.already_running ? "Already running" : "Remote Control is ready");
    } catch (e) { toast(e.message); }
    busy = "";
    load();
  };
  const trust = async (project) => {
    if (!confirm(`Open Claude on the tower to trust “${project}”?\n\nReview the folder shown by Claude, then accept its workspace trust prompt. The harness cannot accept it for you.`)) return;
    busy = project;
    load();
    try {
      const r = await api(`/remote-control/${encodeURIComponent(project)}/trust`, { method: "POST" });
      let message = "Claude trust window opened on the tower";
      if (r.already_trusted) message = "This repository is already trusted";
      else if (r.already_open) message = "The trust window is already open";
      toast(message, 5000);
    } catch (e) { toast(e.message); }
    busy = "";
    load();
  };
  const rcButton = (p) => {
    if (p.running) return h("button", { class: "btn", disabled: !!busy, onclick: () => act(p.project, true) }, "Stop");
    if (!p.trusted) {
      return h("button", { class: "btn", disabled: !!busy || p.trust_prompt_open, onclick: () => trust(p.project) },
        p.trust_prompt_open ? "Trust window open" : "Trust in Claude…");
    }
    return h("button", { class: "btn", disabled: !!busy, onclick: () => act(p.project, false) }, "Start");
  };
  const rcActions = (p) => h("div", { class: "row" },
    p.running && p.pairing_url ? h("a", { class: "btn", href: p.pairing_url, target: "_blank", rel: "noopener" }, "Open in Claude") : null,
    rcButton(p));
  const row = (p) => {
    const remoteControlState = (p) => {
      if (p.running) {
        const sessions = p.active_sessions ? ` · ${pluralize(p.active_sessions, "session")}` : "";
        return `running${sessions} · started ${ago(p.started_at)}`;
      }
      if (!p.trusted) return p.trust_prompt_open ? "trust window open on the tower" : "needs one-time Claude workspace trust";
      return "stopped";
    };
    const state = busy === p.project ? h("span", { class: "dots" }, "working")
      : remoteControlState(p);
    return h("div", { class: "rc-row" },
      h("p", {}, h("strong", {}, p.project), " ", h("span", { class: `muted small${!p.running && !p.trusted ? " bad" : ""}` }, state)),
      h("p", { class: "muted small" }, p.path),
      !p.trusted && p.trust_prompt_open ? h("p", { class: "note small" }, "On the tower, review the folder in Claude and accept its trust prompt. This page will notice automatically.") : null,
      isGuest() ? h("p", { class: "muted small" }, "Demo access cannot start, stop, or trust Remote Control.") : rcActions(p));
  };
  const load = async () => {
    try {
      const r = await api("/remote-control");
      if (!r.enabled) return fill(body, h("p", { class: "muted small" }, "Disabled in config/harness.yaml (remote_control)."));
      fill(body,
        h("p", { class: "muted small" }, "Start Claude Code in a project folder and continue in the Claude app. These sessions use your Claude subscription, not the harness."),
        h("p", { class: "muted small" }, "Only tower projects with a local folder appear. Homelab and scratch have none, so they are omitted."),
        r.projects.length ? r.projects.map(row) : h("p", { class: "muted small" }, "No tower projects with a local folder."));
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  const timer = setInterval(() => { if (!busy) load(); }, 3000);
  onLeave(() => clearInterval(timer));
  return h("div", { class: "card" }, h("h3", {}, "Claude Remote Control"), body);
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
        try { await api(`/skills/${sk.slug}/${toggle}`, { method: "POST" }); route(); } catch (e) { toast(e.message); }
      } }, sk.enabled ? "Disable" : "Enable"),
      h("button", { class: "btn small", onclick: async () => {
        const raw = window.prompt("Project allowlist (comma-separated names)", (sk.projects || []).join(", "));
        if (raw === null) return;
        try {
          await api(`/skills/${sk.slug}/projects`, { method: "PUT", body: { projects: raw.split(",").map((s) => s.trim()).filter(Boolean) } });
          route();
        } catch (e) { toast(e.message); }
      } }, "Projects"),
      h("button", { class: "btn small", onclick: async () => {
        if (!confirm("Roll back to the previous version?")) return;
        try { await api(`/skills/${sk.slug}/rollback`, { method: "POST" }); route(); } catch (e) { toast(e.message); }
      } }, "Rollback"),
      h("button", { class: "btn small bad", onclick: async () => {
        if (!confirm(`Uninstall ${sk.slug}? Later sessions will not receive it.`)) return;
        try { await api(`/skills/${sk.slug}/uninstall`, { method: "POST" }); route(); } catch (e) { toast(e.message); }
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
  (async () => {
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

// Shows a secret exactly once, with a Copy button and a Done button that reloads the card.
function showSecretOnce(form, load, intro, secret, copyLabel) {
  const field = h("input", { type: "text", readonly: true, value: secret, onclick: (e) => e.target.select() });
  fill(form, h("p", { class: "small" }, intro), field,
    h("div", { class: "row", style: "margin-top:8px" },
      h("button", { class: "btn", onclick: () => copyToClipboard(secret, () => field.select()) }, copyLabel),
      h("button", { class: "btn", onclick: load }, "Done")));
}

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
              if (!confirm(`Revoke the key “${k.name}”? Tools using it stop working.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
            },
          }, "Revoke")))) : h("p", { class: "muted small" }, "No keys yet."),
        form, newBtn);
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  return h("div", { class: "card" }, body);
}

const APP_SCOPES = {
  sessions: "Start and follow its own sessions (with context and tools)",
  "sessions:all": "Read all sessions, not only its own",
  approvals: "Approve or deny in its own sessions",
  images: "Generate images",
  inference: "Use the inference endpoint",
  remote_control: "Start and stop Claude Remote Control in a project folder",
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
              if (!confirm(`Revoke the app “${k.name}”? It can no longer start or read sessions.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
            },
          }, "Revoke")))) : h("p", { class: "muted small" }, "No apps yet."),
        webConnections.length ? [h("p", { class: "section-label" }, "Web connections"),
          h("ul", { class: "small" }, webConnections.map((k) => h("li", {},
            h("strong", {}, k.name), ` ${k.prefix}… · ${k.origins?.join(", ") || "non-browser"}${usedSuffix(k)} `,
            h("button", { class: "btn small bad", onclick: async () => {
              if (!confirm(`Revoke “${k.name}”? That Agent Harness Web connection will stop working.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
            } }, "Revoke"))))] : null,
        cliConnections.length ? [h("p", { class: "section-label" }, "CLI connections"),
          h("ul", { class: "small" }, cliConnections.map((k) => h("li", {},
            h("strong", {}, k.name), ` ${k.prefix}… · non-browser${usedSuffix(k)} `,
            h("button", { class: "btn small bad", onclick: async () => {
              if (!confirm(`Revoke “${k.name}”? That Agent Harness CLI connection will stop working.`)) return;
              try { await api(`/keys/${k.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
            } }, "Revoke"))))] : null,
        pending.length ? h("ul", { class: "small" }, pending.map((p) => h("li", {},
          `Pairing pending for ${p.name} at ${p.origin} · expires ${new Date(p.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} `,
          h("button", { class: "btn small bad", onclick: async () => {
            try { await api(`/pairing-codes/${p.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
          } }, "Cancel")))) : null,
        pendingRunners.length ? h("ul", { class: "small" }, pendingRunners.map((p) => h("li", {},
          `Mac pairing pending for ${p.name} (${p.runner}) Â· expires ${new Date(p.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} `,
          h("button", { class: "btn small bad", onclick: async () => {
            try { await api(`/runner-pairing-codes/${p.id}`, { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
          } }, "Cancel")))) : null,
        form, h("div", { class: "row", style: "margin-top:10px" }, newBtn, pairBtn, macBtn));
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  return h("div", { class: "card" }, body);
}

function gbLabel(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return `${v >= 10 ? Math.round(v) : v.toFixed(1)} GB`;
}

function diskCard() {
  const body = h("div", {}, h("p", { class: "muted small" }, "Measuring…"));
  const load = async () => {
    try {
      const u = await api("/maintenance");
      const mb = (n) => (n < 1 ? "<1 MB" : `${n} MB`);
      const device = (name, free, total, extra, { offline = false } = {}) => {
        const freeN = Number(free);
        const totalN = Number(total);
        const haveMeter = !offline && Number.isFinite(freeN) && Number.isFinite(totalN) && totalN > 0;
        const used = haveMeter ? Math.max(0, Math.min(1, (totalN - freeN) / totalN)) : null;
        return h("div", { class: "disk-device" },
          h("strong", {}, name),
          haveMeter ? progressBar(used) : null,
          h("div", { class: "disk-meter" },
            h("span", {}, offline ? "Offline" : `${gbLabel(free)} free`),
            !offline && Number.isFinite(totalN) ? h("span", { class: "muted" }, `of ${gbLabel(total)}`) : null),
          extra);
      };
      const fact = (label, value) => h("p", { class: "small" }, h("strong", {}, label), " ", value);
      const top = u.workspaces.slice(0, 5);
      const towerExtra = h("div", { class: "disk-facts" },
        fact("Workspaces", `${mb(u.workspaces_mb)} · ${u.workspaces.length} session${u.workspaces.length === 1 ? "" : "s"} · ${u.quota_mb} MB quota each`),
        fact("Sandboxes", `${u.containers.length} container${u.containers.length === 1 ? "" : "s"}`),
        backupLine(u.backup),
        isGuest() ? null : imageArchiveBlock(u.image_archive, load),
        top.length ? h("p", { class: "muted small", style: "margin-top:10px" }, "Largest workspaces") : null,
        top.length ? h("ul", { class: "small" }, top.map((w) => h("li", {}, h("a", { href: `#/s/${w.session}/info` }, w.session), ` ${mb(w.mb)}`))) : null);
      fill(body,
        device("Tower", u.free_gb, u.total_gb, towerExtra),
        (u.runners || []).map((r) => {
          const online = !!r.online;
          const compatibility = r.compatibility || {};
          const compatible = compatibility.state === "compatible";
          const canUpdate = online && compatible && r.update_supported;
          const extra = h("div", { class: "disk-facts" },
            fact("Runner", online
              ? `${r.info.version} · protocol ${r.info.protocol ?? "not reported"} · macOS ${r.info.macos}`
              : lastSeenText(r)),
            fact("Compatibility", compatibilityText(compatibility)),
            r.last_update ? fact("Last update", lastUpdateText(r.last_update)) : null,
            isGuest() ? null : h("button", {
              class: "btn", type: "button", disabled: !canUpdate,
              onclick: async (ev) => {
                ev.target.disabled = true;
                try {
                  const result = await api(`/runners/${r.name}/update`, { method: "POST" });
                  toast(result.message || "Mac client update queued", 5000);
                  setTimeout(load, 3000);
                } catch (e) { toast(e.message, 8000); ev.target.disabled = false; }
              },
            }, "Update Mac client"),
            !canUpdate ? h("p", { class: "muted small" }, `On the Mac, run: ${r.manual_update || "harness update"}`) : null);
          return device(TARGET_LABEL[r.name] || r.name,
            online ? r.info.free_gb : "—",
            online && r.info.total_gb != null ? r.info.total_gb : null,
            extra, { offline: !online });
        }));
    } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
  };
  load();
  return h("div", {},
    h("div", { class: "card" }, body),
    isGuest() ? null : h("div", { class: "card" },
      h("p", { class: "muted small" }, "Removes stopped sandbox containers, expired session workspaces, and leftover workspace folders."),
      h("button", {
        class: "btn",
        onclick: async (ev) => {
          if (!confirm("Remove stopped sandbox containers, expired session workspaces, and leftover workspace folders?")) return;
          ev.target.disabled = true;
          try {
            const r = await api("/maintenance/cleanup", { method: "POST" });
            toast(`Removed ${r.containers_removed.length} containers, ${r.workspaces_removed.length + r.orphans_removed.length} workspaces`);
            load();
          } catch (e) { toast(e.message); }
          ev.target.disabled = false;
        },
      }, "Clean up now")));
}

// ---------- model warm-up ----------
// Loading the model takes about a minute after it has slept, so start as soon as the app is opened.
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
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") warmModel(); });

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

// Which side must update when the server's admin protocol range excludes this client (null = compatible).
function protocolMismatch(range) {
  if (!range) return null;
  if (WEB_PROTOCOL < range.min) return "client_update_required";
  return WEB_PROTOCOL > range.max ? "daemon_update_required" : null;
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
  const mismatch = protocolMismatch(meta.protocols?.admin);
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
  if (document.visibilityState === "visible") checkCompatibility({ foreground: true });
});

checkCompatibility().then((compatible) => compatible && currentUser()).then((user) => {
  if (!user) return null;
  paintGuestChrome();
  if (!isGuest()) warmModel();
  return loadProfileIcon();
}).then((ready) => {
  if (ready === null) return null;
  return applyAppIcon(readAppIcon());
}).then((ready) => { if (ready !== null) route(); });
