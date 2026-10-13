// @ts-nocheck
// Shared session classification for the Agents list and persistent shell. No browser globals.
// A failure stays under Needs you this long, then joins Recent (#509): the list has no "seen" state to clear it by.
export const FAILED_NEEDS_YOU_SECONDS = 24 * 3600;
const TERMINAL = new Set(["done", "failed", "cancelled"]);
export const SESSION_GROUPS = [["needs", "Needs you"], ["running", "Running"], ["recent", "Recent"]];

// Which group a session row sits in (#509): pending approvals and fresh failures need the owner; anything not finished
// is Running (queued and waiting states included); the rest is Recent.
export function sessionGroup(s, now = Date.now() / 1000) {
  if (s.status === "waiting_approval" || (s.pending_approvals || []).length) return "needs";
  if (s.status === "failed" && now - s.updated_at < FAILED_NEEDS_YOU_SECONDS) return "needs";
  return TERMINAL.has(s.status) ? "recent" : "running";
}

// The non-empty groups in display order, each sorted by most recent activity.
export function groupSessions(sessions, now = Date.now() / 1000) {
  const byGroup = new Map(SESSION_GROUPS.map(([key]) => [key, []]));
  for (const s of sessions) byGroup.get(sessionGroup(s, now)).push(s);
  return SESSION_GROUPS.map(([key, label]) => ({ key, label, sessions: byGroup.get(key).sort((a, b) => b.updated_at - a.updated_at) }))
    .filter((g) => g.sessions.length);
}

