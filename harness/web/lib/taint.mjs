// Session taint (#262): the event stream replays every taint_added on each view or reconnect, over a
// GET /sessions/{sid} snapshot that already lists those sources, so adding one is idempotent. Keyed by origin,
// like harness/taint.py add().
export function withTaint(taint, entry) {
  const list = taint || [];
  if (list.some((t) => t.origin === entry.origin)) return list;
  return [...list, { origin: entry.origin, kind: entry.kind }];
}
