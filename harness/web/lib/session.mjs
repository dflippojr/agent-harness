// Who is signed in and how requests reach the daemon (#258): the caller's identity, the Google sign-in state, the
// protocol-blocked flag and the api() wrapper. createSession() holds that state in a closure; nothing here touches
// document/window, so it imports under plain Node.

// Which API surface a request uses: guests and members go through the app surface, the owner through admin except for
// the few session endpoints the app surface serves.
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

export function createSession({ agentHarnessWeb }) {
  let currentMe = { role: "owner" };
  let protocolBlocked = false;
  let webAuth = null;
  // Identity fetched during boot; the first route() adopts it instead of requesting /me a second time.
  let bootMe = null;

  const isGuest = () => currentMe.role === "guest";
  const isMember = () => currentMe.role === "member";
  const isOwner = () => currentMe.role === "owner";
  const canChat = () => !isGuest() && !isMember();
  const needsSignIn = () => currentMe.role === "signin";

  function ownerSurface() {
    if (isMember()) return "app";
    if (isGuest() && !agentHarnessWeb.token) return "legacy";
    return "admin";
  }

  async function api(path, { method = "GET", body, surface } = {}) {
    if (protocolBlocked) {
      const err = new Error("Update required");
      err.code = "client_update_required";
      throw err;
    }
    const chosen = surface || apiSurface(path, method, { role: currentMe.role, hasToken: !!agentHarnessWeb.token });
    return agentHarnessWeb.request(path, { method, body, surface: chosen });
  }

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

  // Issue #64: Google sign-in state for bundled, same-origin Web only. The CSRF value stays in memory.
  async function loadWebAuth() {
    webAuth = null;
    agentHarnessWeb.csrf = "";
    if (agentHarnessWeb.independent || agentHarnessWeb.token) return null;
    try { webAuth = await agentHarnessWeb.request("/auth/session", { surface: "app" }); }
    catch (_) { return null; }
    agentHarnessWeb.csrf = webAuth.csrf || "";
    return webAuth;
  }

  return {
    api, fetchMe, loadWebAuth, ownerSurface,
    isGuest, isMember, isOwner, canChat, needsSignIn,
    getMe: () => currentMe,
    setMe: (me) => { currentMe = me; },
    getWebAuth: () => webAuth,
    isBlocked: () => protocolBlocked,
    setBlocked: (on) => { protocolBlocked = on; },
    // The identity boot already fetched; route() takes it once so /me is not requested twice.
    setBootMe: (me) => { bootMe = Promise.resolve(me); },
    takeBootMe: () => { const taken = bootMe; bootMe = null; return taken; },
  };
}
