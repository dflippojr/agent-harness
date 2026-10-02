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
