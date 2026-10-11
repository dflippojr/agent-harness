// @ts-nocheck
// Client/server protocol compatibility. Pure: no DOM.

// Which side must update when the server's admin protocol range excludes this client (null = compatible).
export function protocolMismatch(range, protocol) {
  if (!range) return null;
  if (protocol < range.min) return "client_update_required";
  return protocol > range.max ? "daemon_update_required" : null;
}
