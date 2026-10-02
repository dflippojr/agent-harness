// The session Info tab's trace row (#259). Pure: no DOM.

// What to show for a session's OpenTelemetry trace: null when tracing is off (no id), else the id and the
// Grafana link (only when the daemon built one from telemetry.trace_url_template).
export function traceInfo(session) {
  const id = String((session && session.trace_id) || "");
  if (!id) return null;
  const url = String(session.trace_url || "");
  return { id, url: /^https?:\/\//i.test(url) ? url : "" };
}
