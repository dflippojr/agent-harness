// Model warm-up (#258). Loading the model takes about a minute after it has been unloaded. Only an explicit local-model
// selection (choosing the local backend or a model, or typing a task with it selected) starts a load; the server skips it
// when RAM is short. Opening a page never does (#311).
export function createWarmModel({ api, session }) {
  let lastWarm = 0;
  return async function warmModel(force = false) {
    if (session.isBlocked() || session.isGuest()) return;
    if (!force && Date.now() - lastWarm < 60_000) return;
    try {
      if (!session.isMember()) {
        const gpu = await api("/gpu");
        if (gpu.manual) return;
      }
      lastWarm = Date.now();
      await api("/models/warm", { method: "POST" });
    } catch (_) { /* offline */ }
  };
}
