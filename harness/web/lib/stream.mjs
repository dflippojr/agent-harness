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

export function mountStream({ agentHarnessWeb, isBlocked, setConnLive, ownerSurface, isGuest, browser }) {
  // EventSource that survives iOS suspending the app: reconnects from the last seq when visible again.
  // Connection-dot updates are opt-in (`indicate`) so page streams can close without a false offline state.
  function openStream(urlFor, handlers, { authorized = false, indicate = false } = {}) {
    const { document } = browser;
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
      const resp = await browser.fetch(url, { headers: agentHarnessWeb.headers(), cache: "no-store", signal: controller.signal });
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
      if (closed || isBlocked()) return;
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
      url = safeStreamUrl(url, agentHarnessWeb.baseUrl || "");
      if (!url) { mark(false); return; }
      if (authorized && agentHarnessWeb.token) {
        try { await fetchStream(url); } catch (_) { /* retry below */ }
        mark(false);
        if (!closed && run === generation) retry = setTimeout(connect, 3000);
        return;
      }
      const { EventSource } = browser;
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
    const onVisible = () => { if (!isBlocked() && document.visibilityState === "visible") void connect(); };
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
