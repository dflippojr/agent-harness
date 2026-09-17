// Agent Harness Web transport: the PWA can be bundled with Agent Harness Server or hosted independently.
// Connection settings are intentionally browser-local and never sent anywhere except the chosen Server.

const BASE_KEY = "harness.daemonUrl";
const TOKEN_KEY = "harness.ownerToken";

export function normalizeDaemonUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  let url;
  try { url = new URL(raw); } catch (_) { throw new Error("Agent Harness Server URL must be a complete http(s) URL"); }
  if (!(["http:", "https:"].includes(url.protocol)) || url.username || url.password || url.search || url.hash) {
    throw new Error("Agent Harness Server URL must contain only http(s), host, port, and an optional path");
  }
  url.pathname = url.pathname.replace(/\/+$/, "");
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
    const prefix = surface === "app" ? "/api/v1" : surface === "admin" ? "/api/admin/v1" : "";
    return `${this.baseUrl}${prefix}${path}`;
  }

  headers(extra = {}) {
    return this.token ? { ...extra, Authorization: `Bearer ${this.token}` } : { ...extra };
  }

  async request(path, { method = "GET", body, surface = "admin" } = {}) {
    const headers = this.headers();
    const opts = { method, headers, cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try { resp = await fetch(this.url(path, surface), opts); }
    catch (_) { throw new Error("Can't reach Agent Harness Server. Check Connection settings and Tailscale."); }
    if (resp.status === 204) return null;
    const type = resp.headers.get("content-type") || "";
    const data = type.includes("json") ? await resp.json() : await resp.text();
    if (!resp.ok) throw new Error((data && data.detail) || `HTTP ${resp.status}`);
    return data;
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
    if (!this.token) return this.url(`/sessions/${sessionId}/events?after=${after}`, "app");
    const data = await this.request(`/sessions/${sessionId}/events/ticket`, { method: "POST", surface: "app" });
    const separator = data.events_url.includes("?") ? "&" : "?";
    return `${this.baseUrl}${data.events_url}${separator}after=${after}`;
  }
}

// Compatibility for code that imported the pre-#91 class name directly.
export const ControlCenterClient = AgentHarnessWebClient;
export const agentHarnessWeb = new AgentHarnessWebClient();
export const controlCenter = agentHarnessWeb;
