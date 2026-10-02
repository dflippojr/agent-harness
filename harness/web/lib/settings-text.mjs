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
