// @ts-nocheck
// Text for Settings rows. Pure: no DOM.

export function settingValueText(spec) {
  const configured = spec.configured != null && spec.configured !== spec.effective ? ` · configured ${spec.configured}` : "";
  const inherited = spec.inherited != null ? ` · inherited ${spec.inherited}` : "";
  return `effective ${spec.effective == null ? "—" : spec.effective}${configured}${inherited}`;
}

export function settingMeta(spec) {
  const bits = [];
  bits.push({ live: "applies live", daemon_restart: "needs restart" }[spec.apply] || "file only");
  if (spec.source) bits.push(`source: ${spec.source}`);
  if (spec.pending != null && spec.apply === "daemon_restart") bits.push(`pending: ${spec.pending}`);
  if (spec.capped_by) bits.push(`capped by ${spec.capped_by}`);
  if (spec.file_only) bits.push(spec.guidance || "managed in local configuration");
  return bits.join(" · ");
}

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

export function recoveryNote(recovery) {
  if (recovery?.recovery === "overlay_quarantined") {
    return ` · ${recovery.reason || "managed overlay quarantined; YAML defaults in effect"}`;
  }
  return recovery?.recovery ? ` · recovered from ${recovery.reason || "failed generation"}` : "";
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

export function skillsValue(data) {
  if (!data) return { text: "" };
  if (!data.enabled) return { text: "Off" };
  const installed = (data.installed || []).length;
  const proposals = (data.proposals || []).length;
  return { text: [`${installed} installed`, proposals && plural(proposals, "proposal")].filter(Boolean).join(" · ") };
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

export function serverSettingsValue(view) {
  if (!view) return { text: "" };
  const pending = (view.settings || []).filter((s) => s.apply === "daemon_restart" && s.pending != null).length;
  if (pending) return { text: `${pending} pending restart` };
  if (view.restart_required) return { text: "Restart pending" };
  return { text: view.revision != null ? `Revision ${view.revision}` : "" };
}

export function accountsValue(rows) {
  if (!Array.isArray(rows)) return { text: "" };
  return { text: rows.length ? plural(rows.length, "member") : "Owner only" };
}

export function remoteControlValue(r) {
  if (!r) return { text: "" };
  if (!r.enabled) return { text: "Off" };
  const running = (r.projects || []).filter((p) => p.running).length;
  return { text: running ? `${running} running` : "Ready" };
}

// Same filters as the Apps and Inference endpoint pages, so the row and the page agree.
export function appsValue(keys) {
  if (!Array.isArray(keys)) return { text: "" };
  const live = keys.filter((k) => !k.revoked_at);
  const apps = live.filter((k) => k.kind === "app").length;
  const web = live.filter((k) => k.kind === "owner" && k.origins?.length).length;
  return { text: [plural(apps, "app"), web && `${web} web`].filter(Boolean).join(" · ") };
}

export function endpointValue(keys) {
  if (!Array.isArray(keys)) return { text: "" };
  return { text: plural(keys.filter((k) => !k.revoked_at && k.kind !== "app").length, "key") };
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
