// Agent Harness Web transport: the PWA can be bundled with Agent Harness Server or hosted independently.
// Connection settings are intentionally browser-local and never sent anywhere except the chosen Server.

const BASE_KEY = "harness.daemonUrl";
const TOKEN_KEY = "harness.ownerToken";
export const WEB_BUILD_ID = "2026.09.17.1";
export const WEB_PROTOCOL = 2;

function stripTrailingSlashes(text) {
  let end = text.length;
  while (end > 0 && text[end - 1] === "/") end--;
  return text.slice(0, end);
}

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

export class AgentHarnessWebClient {
  constructor(storage = window.localStorage) {
    this.storage = storage;
    this.baseUrl = normalizeDaemonUrl(this._get(BASE_KEY));
    this.token = this._get(TOKEN_KEY).trim();
  }

  _get(key) {
    try { return this.storage.getItem(key) || ""; } catch (_) { return ""; }
  }

  configure(baseUrl, token) {
    this.baseUrl = normalizeDaemonUrl(baseUrl);
    this.token = String(token || "").trim();
    try {
      if (this.baseUrl) this.storage.setItem(BASE_KEY, this.baseUrl); else this.storage.removeItem(BASE_KEY);
      if (this.token) this.storage.setItem(TOKEN_KEY, this.token); else this.storage.removeItem(TOKEN_KEY);
    } catch (_) { /* private mode */ }
  }

  get independent() { return !!this.baseUrl && this.baseUrl !== location.origin; }

  url(path, surface = "admin") {
    const prefixes = { app: "/api/v1", admin: "/api/admin/v1" };
    const prefix = prefixes[surface] || "";
    return `${this.baseUrl}${prefix}${path}`;
  }

  headers(extra = {}) {
    const headers = { ...extra, "X-Agent-Harness-Client": `web/${WEB_PROTOCOL}` };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    return headers;
  }

  async request(path, { method = "GET", body, surface = "admin" } = {}) {
    const headers = this.headers();
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
    catch (_) { throw new Error("Can't reach Agent Harness Server. Check Connection settings and Tailscale."); }
    if (resp.status === 204) return null;
    const type = resp.headers.get("content-type") || "";
    const data = type.includes("json") ? await resp.json() : await resp.text();
    if (!resp.ok) {
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

  async blob(path, surface = "admin") {
    let resp;
    try { resp = await fetch(this.url(path, surface), { headers: this.headers(), cache: "no-store" }); }
    catch (_) { throw new Error("Can't reach Agent Harness Server. Check Connection settings and Tailscale."); }
    if (!resp.ok) {
      let detail = "";
      try { detail = (await resp.json()).detail || ""; } catch (_) { /* binary/text response */ }
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    return resp.blob();
  }

  async sessionStreamUrl(sessionId, after = 0) {
    if (!this.token) return this.url(`/sessions/${encodeURIComponent(sessionId)}/events?after=${after}`, "app");
    const data = await this.request(`/sessions/${encodeURIComponent(sessionId)}/events/ticket`, { method: "POST", surface: "app" });
    const separator = data.events_url.includes("?") ? "&" : "?";
    return `${this.baseUrl}${data.events_url}${separator}after=${after}`;
  }
}

// Compatibility for code that imported the pre-#91 class name directly.
export const ControlCenterClient = AgentHarnessWebClient;
export const agentHarnessWeb = new AgentHarnessWebClient();
export const controlCenter = agentHarnessWeb;
