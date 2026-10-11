// Who is signed in and how requests reach the daemon (#258): the caller's identity, the Google sign-in state, the
// protocol-blocked flag and the api() wrapper. createSession() holds that state in a closure; nothing here touches
// document/window, so it imports under plain Node.
/** @import { AgentHarnessWebClient } from '../client.mjs' */
/** @import { ApiResponse, ClientError, HttpMethod, Identity, RequestOptions, WebAuth } from '../../../tools/web-types/contracts.js' */

// Which API surface a request uses: guests and members go through the app surface, the owner through admin except for
// the few session endpoints the app surface serves.
/** @param {string} path @param {string} method @param {{role: string, hasToken: boolean}} identity */
export function apiSurface(path, method, { role, hasToken }) {
  if (role === "guest" && !hasToken) return "legacy";
  if (role === "member") return "app";
  const route = path.split("?")[0];
  if (route === "/sessions" && (method === "GET" || method === "POST")) return "app";
  if (/^\/sessions\/[^/]+$/.test(route) && method === "GET") return "app";
  if (/^\/sessions\/[^/]+\/(messages|cancel)$/.test(route) && method === "POST") return "app";
  if (/^\/sessions\/[^/]+\/approvals\/[^/]+$/.test(route) && method === "POST") return "app";
  return "admin";
}

// Last identity role seen from /me, so an offline launch keeps its shell instead of becoming a guest (#368). Role only.
export const LAST_ROLE_KEY = "harness.lastRole";
const CACHED_ROLES = new Set(["owner", "member"]);

// `storage` is localStorage or null; every access is guarded because it can throw in private windows.
/** @param {{agentHarnessWeb: AgentHarnessWebClient, storage?: Storage | null}} dependencies */
export function createSession({ agentHarnessWeb, storage = null }) {
  /** @type {Identity} */
  let currentMe = { role: "owner" };
  let protocolBlocked = false;
  /** @type {WebAuth | null} */
  let webAuth = null;
  // Identity fetched during boot; the first route() adopts it instead of requesting /me a second time.
  /** @type {Promise<Identity> | null} */
  let bootMe = null;

  const isGuest = () => currentMe.role === "guest";
  const isMember = () => currentMe.role === "member";
  const isOwner = () => currentMe.role === "owner";
  const canChat = () => !isGuest() && !isMember();
  const needsSignIn = () => currentMe.role === "signin";
  // The server could not be reached when identity was checked: either a cached role (pages paint their own
  // "Can't reach" state) or role "offline" (no cached role; route shows the offline card).
  const isOffline = () => !!currentMe.offline;

  function ownerSurface() {
    if (isMember()) return "app";
    if (isGuest() && !agentHarnessWeb.token) return "legacy";
    return "admin";
  }

  /**
   * @template {string} P
   * @template {HttpMethod} [M="GET"]
   * @param {P} path
   * @param {RequestOptions<M>} [options]
   * @returns {Promise<ApiResponse<P, M>>}
   */
  async function api(path, { method = /** @type {M} */ ("GET"), body, surface } = {}) {
    if (protocolBlocked) {
      /** @type {ClientError} */
      const err = new Error("Update required");
      err.code = "client_update_required";
      throw err;
    }
    const chosen = surface || apiSurface(path, method, { role: currentMe.role, hasToken: !!agentHarnessWeb.token });
    return agentHarnessWeb.request(path, { method, body, surface: chosen });
  }

  // Called only when an identity is adopted (setMe), never from fetchMe: boot discards a speculative /me on a
  // protocol mismatch, and that result must not touch the cache. An offline identity leaves it as it is.
  /** @param {Identity} me */
  function rememberRole(me) {
    if (me?.offline) return;
    try {
      if (CACHED_ROLES.has(me?.role)) storage?.setItem(LAST_ROLE_KEY, me.role);
      else storage?.removeItem(LAST_ROLE_KEY);
    } catch (_) { /* storage unavailable */ }
  }

  /** @returns {Identity} */
  function offlineMe() {
    let role = null;
    try { role = storage?.getItem(LAST_ROLE_KEY); } catch (_) { /* storage unavailable */ }
    return { role: role === "owner" || role === "member" ? role : "offline", offline: true };
  }

  // Resolves (never rejects) to the caller's identity without touching app state, so boot can start it
  // speculatively beside /health and only adopt the result once compatibility has passed. A network failure is not
  // "not signed in": it resolves to the last known role (or the offline marker), never to guest.
  /** @returns {Promise<Identity>} */
  async function fetchMe() {
    const bootstrap = !agentHarnessWeb.token && !agentHarnessWeb.independent ? "legacy" : "admin";
    try {
      return await api("/me", { surface: bootstrap });
    } catch (e) {
      if (e.code === "offline") return offlineMe();
      if (e.code === "sign_in_required") return { role: "signin" };
      try { return await api("/me", { surface: "app" }); }
      catch (e2) { return e2.code === "offline" ? offlineMe() : { role: "guest" }; }
    }
  }

  // Issue #64: Google sign-in state for bundled, same-origin Web only. The CSRF value stays in memory.
  async function loadWebAuth() {
    webAuth = null;
    agentHarnessWeb.csrf = "";
    if (agentHarnessWeb.independent || agentHarnessWeb.token) return null;
    try { webAuth = await agentHarnessWeb.request("/auth/session", { surface: "app" }); }
    catch (_) { return null; }
    agentHarnessWeb.csrf = webAuth?.csrf || "";
    return webAuth;
  }

  return {
    api, fetchMe, loadWebAuth, ownerSurface,
    isGuest, isMember, isOwner, canChat, needsSignIn, isOffline,
    getMe: () => currentMe,
    setMe: (/** @type {Identity} */ me) => { currentMe = me; rememberRole(me); },
    getWebAuth: () => webAuth,
    isBlocked: () => protocolBlocked,
    setBlocked: (/** @type {boolean} */ on) => { protocolBlocked = on; },
    // The identity boot already fetched; route() takes it once so /me is not requested twice.
    setBootMe: (/** @type {Identity | Promise<Identity>} */ me) => { bootMe = Promise.resolve(me); },
    takeBootMe: () => { const taken = bootMe; bootMe = null; return taken; },
  };
}
