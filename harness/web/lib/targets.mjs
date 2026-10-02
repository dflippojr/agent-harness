// Runner targets and backend choice. Pure: no DOM.

export const TARGET_LABEL = { tower: "tower", macbook: "MacBook" };
// The home machine sorts first, everything else alphabetically.
export function compareTargets(a, b) {
  if (a === "tower") return -1;
  if (b === "tower") return 1;
  return a.localeCompare(b);
}

export function runnerStateText(targetName, runner) {
  const label = TARGET_LABEL[targetName] || targetName;
  if (!runner?.online) return `Runs on the ${label}, which is offline or asleep: the task will wait for it`;
  const free = runner.info.free_gb !== undefined ? `, ${runner.info.free_gb} GB free` : "";
  return `Runs on the ${label} (online${free})`;
}

// With the GPU held, prefer a hosted backend so the task is not stuck behind the hold.
export function pickDefaultBackend(available, holdActive) {
  if (holdActive && available.some((b) => b.name === "claude")) return "claude";
  return available[0]?.name || "local";
}
