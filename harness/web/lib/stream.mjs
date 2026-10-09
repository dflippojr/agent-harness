// Server-sent event streams (#258): the URL guard (pure) and openStream(), an EventSource that survives iOS suspending the app.
// Browser globals arrive through `browser` (globalThis in the app, a stub under Node), so the module imports under plain Node.

// Ids come from the URL hash, so only the characters the daemon issues (hex, "-", "_") may reach a request path.
const SAFE_ID = /^[A-Za-z0-9_-]{1,64}$/;
export const validId = (id) => typeof id === "string" && SAFE_ID.test(id);
const STREAM_PATH = /^(?:\/api\/v1|\/api\/admin\/v1)?\/(?:(?:sessions|chats)\/[A-Za-z0-9_-]{1,64}\/events|events|queue)$/;
const STREAM_QUERY = /^(?:\?[A-Za-z0-9_=&.-]*)?$/;

// Returns a rebuilt same-origin stream URL, or null when it is not a known API stream path (fail closed).
export function safeStreamUrl(url, base = "") {
  if (typeof url !== "string" || url.length > 2048) return null;
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

// Reconnect schedule (#510): exponential backoff from 1 s up to 30 s, with "equal jitter" (half fixed, half random) so many
// tabs or phones that lost the server together do not all retry on the same tick. `attempt` counts failures since the
// stream was last live, from 0.
export const RETRY_BASE_MS = 1000;
export const RETRY_CAP_MS = 30000;
export function retryDelay(attempt, random = Math.random) {
  const ceiling = Math.min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** Math.max(0, attempt));
  return Math.round(ceiling / 2 + random() * (ceiling / 2));
}
// After this many failed attempts in a row (about 8–15 s with the schedule above) "Reconnecting" becomes "Offline". The
// stream keeps retrying either way; the browser reporting no network goes straight to Offline.
export const OFFLINE_AFTER = 4;

export function mountStream({ agentHarnessWeb, isBlocked, setConnState, ownerSurface, isGuest, browser }) {
  // EventSource that survives iOS suspending the app: reconnects from the last seq when visible again.
  // Connection state ("live", "reconnecting", "offline") goes to `onState`; the header chip follows it only for the stream
  // opened with `indicate`, so page streams can close without a false offline state.
  function openStream(urlFor, handlers, { authorized = false, indicate = false, onState = null } = {}) {
    const { document } = browser;
    let es = null;
    let controller = null;
    let closed = false;
    let retry = null;
    let generation = 0;
    let attempts = 0;
    let state = "";
    const setState = (next) => {
      if (closed || next === state) return;
      state = next;
      if (indicate) setConnState(next);
      onState?.(next);
    };
    const browserOffline = () => browser.navigator?.onLine === false;
    const live = () => {
      attempts = 0;
      setState("live");
    };
    // Every failure path ends here: say so, then retry on the backoff schedule.
    const fail = (run) => {
      if (closed || run !== generation) return;
      attempts += 1;
      setState(browserOffline() || attempts >= OFFLINE_AFTER ? "offline" : "reconnecting");
      clearTimeout(retry);
      retry = setTimeout(connect, retryDelay(attempts - 1));
    };
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
      const resp = await browser.fetch(url, { headers: agentHarnessWeb.headers(), cache: "no-store", signal: controller.signal });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
      live();
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
      if (closed || isBlocked()) return;
      clearTimeout(retry);  // a resume or "online" event may come while a backoff retry is pending
      const run = ++generation;
      es?.close();
      controller?.abort();
      let url;
      try { url = await urlFor(); }
      catch (_) { fail(run); return; }
      url = safeStreamUrl(url, agentHarnessWeb.baseUrl || "");
      if (!url) { setState("offline"); return; }
      if (authorized && agentHarnessWeb.token) {
        try { await fetchStream(url); } catch (_) { /* retry below */ }
        fail(run);
        return;
      }
      const { EventSource } = browser;
      const source = new EventSource(url);
      es = source;
      source.onopen = () => { if (run === generation) live(); };
      // Close on any error and follow our own backoff, rather than the browser's fixed retry interval.
      source.onerror = () => {
        source.close();
        fail(run);
      };
      for (const [type, fn] of Object.entries(handlers)) {
        source.addEventListener(type, (msg) => {
          if (msg.data === undefined) return; // the browser's own connection "error" event, handled by onerror
          fn(JSON.parse(msg.data));
        });
      }
    };
    const onVisible = () => { if (!isBlocked() && document.visibilityState === "visible") void connect(); };
    const onOnline = () => { if (!isBlocked()) void connect(); };
    const onOffline = () => setState("offline");  // the stream errors soon anyway; say so now
    document.addEventListener("visibilitychange", onVisible);
    browser.window?.addEventListener?.("online", onOnline);
    browser.window?.addEventListener?.("offline", onOffline);
    void connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      es?.close();
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisible);
      browser.window?.removeEventListener?.("online", onOnline);
      browser.window?.removeEventListener?.("offline", onOffline);
    };
  }

  let watching = false;
  function watchDaemonConnection() {
    if (watching) return;
    watching = true;
    openStream(() => agentHarnessWeb.url("/events", ownerSurface()), {}, {
      authorized: !(isGuest() && !agentHarnessWeb.token),
      indicate: true,
    });
  }

  return { openStream, watchDaemonConnection };
}
