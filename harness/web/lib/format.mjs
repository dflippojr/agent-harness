// Time, size and count formatting shared by every page. Pure: no DOM.

export const fmtElapsed = (ms) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
};
export const fmtTokens = (n) => {
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return `${n || 0}`;
};
// llama-server's prompt progress counts the cached prefix as processed; the bar covers only the part being read.
export const readFraction = (d) => (d.total > d.cached ? (d.processed - d.cached) / (d.total - d.cached) : null);
export const readingText = (what, d) => {
  const cached = d.cached ? ` (${fmtTokens(d.cached)} cached)` : "";
  return `${what} ${fmtTokens(Math.max(0, d.processed - d.cached))} of ${fmtTokens(d.total - d.cached)} new tokens${cached}`;
};

export function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// "45 s" under 90 seconds, otherwise whole minutes.
export const fmtSpan = (seconds, toMinutes = Math.round) => (seconds >= 90 ? `${toMinutes(seconds / 60)} min` : `${seconds} s`);
export const pluralize = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function holdRemainingText(seconds) {
  if (seconds === null) return "until you turn it off";
  return `for about ${fmtSpan(seconds, Math.ceil)}`;
}

export function fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(1)} GiB`;
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MiB`;
  return `${n} B`;
}

export function gpuText(g) {
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
