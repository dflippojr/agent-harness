// @ts-nocheck
// Text for Settings rows. Pure: no DOM.

export const lastUpdateText = (update) => `${update.ok ? "succeeded" : "failed"}: ${update.message}`;

export function compatibilityText(compatibility) {
  const { supported } = compatibility;
  const range = supported ? ` · Server supports ${supported.min}–${supported.max}` : "";
  return `${compatibility.state || "not reported"}${range}`;
}

export function lastSeenText(runner) {
  if (runner.last_seen_seconds === null) return "not connected since Agent Harness Server started";
  return `last seen ${Math.round(runner.last_seen_seconds / 60)} min ago`;
}

// ---- Settings menu row values (#512): one short line per row, read at a glance. Each takes the row's own API
// response and returns { text, warn? }; a missing or unexpected response gives an empty value, never a throw.
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const BACKEND_TITLES = { local: "Qwen", claude: "Claude", codex: "Codex", cursor: "Cursor" };
const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");

export function backendsValue(rows) {
  if (!Array.isArray(rows)) return { text: "" };
  const ready = rows.filter((b) => b.available !== false);
  const first = ready.find((b) => b.name !== "local") || ready[0];
  if (!first) return { text: "None ready" };
  const name = BACKEND_TITLES[first.name] || first.name;
  const effort = first.name === "local" ? "" : first.effort;
  const label = [name, effort].filter(Boolean).join(" · ");
  return { text: ready.length > 1 ? `${label} +${ready.length - 1}` : label };
}

export function smartApprovalsValue(data) {
  if (!data) return { text: "" };
  return { text: (data.configured || data.enabled) && data.mode && data.mode !== "off" ? capitalize(data.mode) : "Off" };
}

export function memoryValue(mem) {
  if (!mem) return { text: "" };
  if (!mem.enabled) return { text: "Off" };
  return { text: mem.writes ? "Writes on" : "Read-only" };
}

export function notificationsValue(notify) {
  if (!notify) return { text: "" };
  return { text: notify.enabled ? "ntfy · on" : "Off" };
}

// GPU hold and the automatic pause are the states worth noticing, so they carry the warn colour.
export function resourcesValue(g) {
  if (!g) return { text: "" };
  if (!g.enabled) return { text: "Guard off" };
  if (g.manual) {
    const left = g.manual_remaining_seconds;
    return { text: left == null ? "GPU held" : `GPU held · ${Math.max(1, Math.ceil(left / 60))} min`, warn: true };
  }
  if (g.state === "paused" || g.state === "pausing") return { text: "Paused for GPU", warn: true };
  return { text: "GPU free" };
}

export function remoteControlValue(r) {
  if (!r) return { text: "" };
  if (!r.enabled) return { text: "Off" };
  const running = (r.projects || []).filter((p) => p.running).length;
  return { text: running ? `${running} running` : "Ready" };
}

// The version row: this bundle, its protocol, and what the connected server says about it.
export function versionStatus(meta, buildId, protocol, mismatch) {
  if (!meta) return { text: "server not reachable", update: false };
  if (mismatch === "client_update_required") return { text: "update required", update: true, warn: true };
  if (mismatch === "daemon_update_required") return { text: "server update required", update: false, warn: true };
  const available = meta.update_hint?.web?.build_id;
  if (available && available !== buildId) return { text: "update available", update: true };
  return { text: "up to date", update: false };
}

export function serverVersionText(meta) {
  if (!meta) return "";
  const range = meta.protocols?.admin;
  const build = meta.build_id ? ` (${meta.build_id})` : "";
  const supports = range ? `protocol ${range.min}–${range.max}` : "";
  return [`Server ${meta.release || "?"}${build}`, supports].filter(Boolean).join(" · ");
}
