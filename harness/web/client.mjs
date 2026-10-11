// Agent Harness Web transport: the PWA can be bundled with Agent Harness Server or hosted independently.
// Connection settings are intentionally browser-local and never sent anywhere except the chosen Server.
/** @import { ApiResponse, ClientError, HttpMethod, RequestOptions, Surface } from '../../tools/web-types/contracts.js' */

const BASE_KEY = "harness.daemonUrl";
const TOKEN_KEY = "harness.ownerToken";
export const WEB_BUILD_ID = "2026.10.10.1";
export const WEB_PROTOCOL = 2;

/** @param {string} text */
function stripTrailingSlashes(text) {
  let end = text.length;
  while (end > 0 && text[end - 1] === "/") end--;
  return text.slice(0, end);
}

/** @param {unknown} value */
export function normalizeDaemonUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  let url;
  try { url = new URL(raw); } catch (_) { throw new Error("Agent Harness Server URL must be a complete http(s) URL"); }
  if (!(["http:", "https:"].includes(url.protocol)) || url.username || url.password || url.search || url.hash) {
    throw new Error("Agent Harness Server URL must contain only http(s), host, port, and an optional path");
  }
  url.pathname = stripTrailingSlashes(url.pathname);
  return url.toString().replace(/\/$/, "");
}

// A request that never reached the server (offline, DNS, Tailscale down). The `offline` code tells it apart from an HTTP
// error, so boot does not mistake "can't connect" for "not signed in" (#368).
function unreachable() {
  /** @type {ClientError} */
  const err = new Error("Can't reach Agent Harness Server. Check Connection settings and Tailscale.");
  err.code = "offline";
  return err;
}

export class AgentHarnessWebClient {
  /** @param {Storage} [storage] */
  constructor(storage = window.localStorage) {
    this.storage = storage;
    this.baseUrl = normalizeDaemonUrl(this._get(BASE_KEY));
    this.token = this._get(TOKEN_KEY).trim();
    // Issue #64: per-session CSRF value for a Google Web session. Memory only: never storage, never a URL.
    this.csrf = "";
  }

  /** @param {string} key */
  _get(key) {
    try { return this.storage.getItem(key) || ""; } catch (_) { return ""; }
  }

  /** @param {string} baseUrl @param {string} token */
  configure(baseUrl, token) {
    this.baseUrl = normalizeDaemonUrl(baseUrl);
    this.token = String(token || "").trim();
    try {
      if (this.baseUrl) this.storage.setItem(BASE_KEY, this.baseUrl); else this.storage.removeItem(BASE_KEY);
      if (this.token) this.storage.setItem(TOKEN_KEY, this.token); else this.storage.removeItem(TOKEN_KEY);
    } catch (_) { /* private mode */ }
  }

  get independent() { return !!this.baseUrl && this.baseUrl !== location.origin; }

  /** @param {string} path @param {Surface} [surface] */
  url(path, surface = "admin") {
    /** @type {Partial<Record<Surface, string>>} */
    const prefixes = { app: "/api/v1", admin: "/api/admin/v1" };
    const prefix = prefixes[surface] || "";
    return `${this.baseUrl}${prefix}${path}`;
  }

  /** @param {Record<string, string>} [extra] @returns {Record<string, string>} */
  headers(extra = {}) {
    /** @type {Record<string, string>} */
    const headers = { ...extra, "X-Agent-Harness-Client": `web/${WEB_PROTOCOL}` };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (this.csrf && !this.independent) headers["X-Agent-Harness-CSRF"] = this.csrf;
    return headers;
  }

  /**
   * @template {string} P
   * @template {HttpMethod} [M="GET"]
   * @template {Surface} [S="admin"]
   * @param {P} path
   * @param {RequestOptions<M, S>} [options]
   * @returns {Promise<ApiResponse<P, M, S>>}
   */
  async request(path, { method = /** @type {M} */ ("GET"), body, surface = /** @type {S} */ ("admin") } = {}) {
    const headers = this.headers();
    /** @type {RequestInit} */
    const opts = { method, headers, cache: "no-store" };
    if (body !== undefined) {
      if (typeof FormData !== "undefined" && body instanceof FormData) {
        opts.body = body;
      } else {
        headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
    }
    let resp;
    try { resp = await fetch(this.url(path, surface), opts); }
    catch (_) { throw unreachable(); }
    if (resp.status === 204) return /** @type {ApiResponse<P, M, S>} */ (null);
    const type = resp.headers.get("content-type") || "";
    const data = type.includes("json") ? await resp.json() : await resp.text();
    if (!resp.ok) {
      /** @type {ClientError} */
      const err = new Error(data?.detail || `HTTP ${resp.status}`);
      err.status = resp.status;
      err.code = data?.error?.code;
      err.keys = data?.error?.keys;
      err.details = data?.error?.details;
      err.data = data;
      throw err;
    }
    return data;
  }

  compatibility() {
    return this.request("/health", { surface: "" });
  }

  /** @param {string} path @param {Surface} [surface] */
  async blob(path, surface = "admin") {
    let resp;
    try { resp = await fetch(this.url(path, surface), { headers: this.headers(), cache: "no-store" }); }
    catch (_) { throw unreachable(); }
    if (!resp.ok) {
      let detail = "";
      try { detail = (await resp.json()).detail || ""; } catch (_) { /* binary/text response */ }
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    return resp.blob();
  }

  // With a token the page streams via fetch with the Authorization header (openStream's `authorized` path), so no
  // ticket is needed. Tickets are for native EventSource, which a same-origin page cannot use: it sends no Origin
  // header, so the ticket's Origin binding never matches and the stream is refused with 401 (#82).
  /** @param {string} sessionId @param {number} [after] */
  sessionStreamUrl(sessionId, after = 0) {
    return this.url(`/sessions/${encodeURIComponent(sessionId)}/events?after=${after}`, "app");
  }
}

// Compatibility for code that imported the pre-#91 class name directly.
export const ControlCenterClient = AgentHarnessWebClient;
export const agentHarnessWeb = new AgentHarnessWebClient();
export const controlCenter = agentHarnessWeb;
