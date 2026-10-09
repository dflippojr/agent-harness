// Pure scheduled-job helpers (#258): schedule labels, next-run text and new-job defaults. No DOM.
export const JOB_NOTIFY = {
  attention: "Only when something needs attention",
  low: "Quietly when OK (low-priority notification)",
  always: "Every run",
};
export const CRON_PRESETS = [
  ["0 8 * * *", "Every day at 8:00"], ["0 7 * * 1-5", "Weekdays at 7:00"], ["0 * * * *", "Every hour"],
  ["*/30 * * * *", "Every 30 minutes"], ["0 10 * * 0", "Sundays at 10:00"], ["0 9 1 * *", "Monthly, on the 1st at 9:00"],
];
export const fmtWhen = (ts) => new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
export const whenText = (ts) => {
  if (!ts) return "—";
  const s = ts - Date.now() / 1000;
  let rel = `in ${Math.round(s / 86400)} d`;
  if (s < 0) rel = "due";
  else if (s < 3600) rel = `in ${Math.max(1, Math.round(s / 60))} min`;
  else if (s < 86400) rel = `in ${Math.round(s / 3600)} h`;
  return `${fmtWhen(ts)} (${rel})`;
};
export const cronLabel = (cron) => (CRON_PRESETS.find(([c]) => c === cron) || [null, cron])[1];

export const newJobDefaults = (projects) => ({
  name: "", prompt: "", cron: "0 8 * * *", backend: "local", model: "", notify: "low", enabled: true,
  project: projects.some((p) => p.name === "homelab") ? "homelab" : "scratch",
});

// Jobs list groups (#511), in display order. Paused wins: switching a job off is the owner's call, whatever its last run.
// A last run that ended ATTENTION, failed or waits on an approval needs attention; a start error (last_error, such as the GPU hold) stays in
// Scheduled with its red line, because the next run may well start.
export const JOB_GROUPS = [["attention", "Needs attention"], ["scheduled", "Scheduled"], ["paused", "Paused"]];
export function jobGroup(job) {
  if (!job.enabled) return "paused";
  const last = job.recent?.[0];
  return last && (last.job_status === "attention" || last.status === "failed" || last.status === "waiting_approval") ? "attention" : "scheduled";
}

// "Fri 8:00": weekday and time, short enough for the 13 px meta row; today's runs drop the weekday.
export const shortWhen = (ts, now = Date.now()) => {
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === new Date(now).toDateString() ? time : `${d.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
};

// The last run's outcome as [badge class, label]: STATUS: OK/ATTENTION when the agent reported one, else the session status.
export function lastRunPill(last) {
  if (!last) return null;
  if (last.job_status === "attention") return ["waiting_approval", "Attention"];
  if (last.job_status === "ok") return ["done", "OK"];
  if (last.status === "failed") return ["failed", "Failed"];
  if (last.status === "waiting_approval") return ["waiting_approval", "Needs approval"];
  if (last.status === "done") return ["done", "Done"];
  if (last.status === "cancelled") return ["cancelled", "Cancelled"];
  return ["running", "Running"];
}

// "Last run 8:01 · 7 h ago", "Last run Sun 10:04 · 2 d ago", then just the date once it is a week old.
export const lastRunText = (ts, now = Date.now()) => {
  const s = Math.max(0, now / 1000 - ts);
  if (s < 60) return "Last run just now";
  if (s < 3600) return `Last run ${shortWhen(ts, now)} · ${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `Last run ${shortWhen(ts, now)} · ${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `Last run ${shortWhen(ts, now)} · ${Math.floor(s / 86400)} d ago`;
  return `Last run ${new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
};

// PUT /jobs/{id} replaces the whole job, so the inline Enabled switch sends every field back with only `enabled` changed.
export const JOB_FIELDS = ["name", "prompt", "cron", "project", "backend", "model", "notify", "enabled", "catch_up_minutes"];
export const jobBody = (job, changes = {}) => Object.fromEntries(JOB_FIELDS.filter((k) => job[k] !== undefined && job[k] !== null)
  .map((k) => [k, k in changes ? changes[k] : job[k]]));
