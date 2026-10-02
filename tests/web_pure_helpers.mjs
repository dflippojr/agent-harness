// UI harness: the small pure helpers pulled out of app.js by the Sonar style cleanup (#229) keep the
// exact strings and choices the inline ternaries produced. They live in harness/web/lib/ and are imported
// directly, so no DOM is needed (#258).
import { approvalDiffClass, diffLineClass } from "../harness/web/lib/diff.mjs";
import { protocolMismatch } from "../harness/web/lib/compat.mjs";
import { fmtSpan, fmtTokens, holdRemainingText, pluralize } from "../harness/web/lib/format.mjs";
import { compatibilityText, lastSeenText, recoveryNote, settingValueText } from "../harness/web/lib/settings-text.mjs";
import { compareTargets, pickDefaultBackend, runnerStateText } from "../harness/web/lib/targets.mjs";
import { approvalWhat, toolSummaryText } from "../harness/web/lib/tools.mjs";

const failures = [];
const eq = (label, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) failures.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};

{
  eq("fmtTokens 0", fmtTokens(0), "0");
  eq("fmtTokens undefined", fmtTokens(undefined), "0");
  eq("fmtTokens 999", fmtTokens(999), "999");
  eq("fmtTokens 1500", fmtTokens(1500), "2K");
  eq("fmtTokens 2.5M", fmtTokens(2.5e6), "2.5M");
  eq("fmtTokens 12M", fmtTokens(1.2e7), "12M");
  eq("fmtSpan 45", fmtSpan(45), "45 s");
  eq("fmtSpan 89", fmtSpan(89), "89 s");
  eq("fmtSpan 90", fmtSpan(90), "2 min");
  eq("fmtSpan 100 ceil", fmtSpan(100, Math.ceil), "2 min");
  eq("fmtSpan 130 round", fmtSpan(130), "2 min");
  eq("pluralize 1", pluralize(1, "commit"), "1 commit");
  eq("pluralize 0", pluralize(0, "commit"), "0 commits");
  eq("pluralize 3", pluralize(3, "new commit"), "3 new commits");
}

{
  eq("targets sort", ["mac", "tower", "alpha"].sort(compareTargets), ["tower", "alpha", "mac"]);
}

{
  eq("hold forever", holdRemainingText(null), "until you turn it off");
  eq("hold seconds", holdRemainingText(60), "for about 60 s");
  eq("hold minutes", holdRemainingText(100), "for about 2 min");
}

{
  eq("summary shell", toolSummaryText({ name: "run_shell" }, { command: "ls" }), "ls");
  eq("summary clone", toolSummaryText({ name: "git_clone" }, { url: "u" }), "u");
  eq("summary fetch", toolSummaryText({ name: "web_fetch" }, { url: "w" }), "w");
  eq("summary prom", toolSummaryText({ name: "prometheus_query" }, { query: "up" }), "up");
  eq("summary service", toolSummaryText({ name: "logs" }, { service: "svc", since: "1h" }), "svc since 1h");
  eq("summary service bare", toolSummaryText({ name: "logs" }, { service: "svc" }), "svc");
  eq("summary path", toolSummaryText({ name: "read" }, { path: "a.py", start_line: 4 }), "a.py :4");
  eq("summary fallback", toolSummaryText({ name: "x", arguments: "{}" }, {}), "{}");
}

{
  eq("what shell", approvalWhat({ tool: "run_shell", args: { command: "ls", network: true } }), "🌐 network · $ ls");
  eq("what bash", approvalWhat({ tool: "Bash", args: { command: "ls" } }), "$ ls");
  eq("what clone", approvalWhat({ tool: "git_clone", args: { url: "u" } }), "git clone u");
  eq("what restart", approvalWhat({ tool: "restart_service", args: { service: "s" } }), "restart s");
  eq("what other", approvalWhat({ tool: "t", args: { a: 1 } }), JSON.stringify({ a: 1 }, null, 2));
  eq("approval hunk", approvalDiffClass("@@ -1 +1 @@"), "hunk");
  eq("approval add", approvalDiffClass("+x"), "add");
  eq("approval del", approvalDiffClass("-x"), "del");
  eq("approval ctx", approvalDiffClass(" x"), "");
}

{
  eq("diff +++", diffLineClass("+++ b/x"), "");
  eq("diff ---", diffLineClass("--- a/x"), "");
  eq("diff add", diffLineClass("+x"), "add");
  eq("diff del", diffLineClass("-x"), "del");
  eq("diff hunk", diffLineClass("@@ x"), "hunk");
}

{
  eq("protocol none", protocolMismatch(undefined, 5), null);
  eq("protocol ok", protocolMismatch({ min: 4, max: 6 }, 5), null);
  eq("protocol client old", protocolMismatch({ min: 6, max: 8 }, 5), "client_update_required");
  eq("protocol daemon old", protocolMismatch({ min: 1, max: 4 }, 5), "daemon_update_required");
}

{
  eq("setting plain", settingValueText({ effective: 3 }), "effective 3");
  eq("setting none", settingValueText({ effective: null }), "effective —");
  eq("setting configured", settingValueText({ effective: 3, configured: 5, inherited: 1 }), "effective 3 · configured 5 · inherited 1");
  eq("setting same configured", settingValueText({ effective: 3, configured: 3 }), "effective 3");
  eq("recovery none", recoveryNote(undefined), "");
  eq("recovery quarantined", recoveryNote({ recovery: "overlay_quarantined" }), " · managed overlay quarantined; YAML defaults in effect");
  eq("recovery generic", recoveryNote({ recovery: "lkg_restore", reason: "bad" }), " · recovered from bad");
  eq("seen never", lastSeenText({ last_seen_seconds: null }), "not connected since Agent Harness Server started");
  eq("seen min", lastSeenText({ last_seen_seconds: 120 }), "last seen 2 min ago");
  eq("compat bare", compatibilityText({}), "not reported");
  eq("compat range", compatibilityText({ state: "compatible", supported: { min: 1, max: 2 } }), "compatible · Server supports 1–2");
}

{
  eq("runner offline", runnerStateText("macbook", { online: false }),
    "Runs on the MacBook, which is offline or asleep: the task will wait for it");
  eq("runner missing", runnerStateText("x", undefined), "Runs on the x, which is offline or asleep: the task will wait for it");
  eq("runner online", runnerStateText("macbook", { online: true, info: { free_gb: 12 } }), "Runs on the MacBook (online, 12 GB free)");
  eq("runner online no gb", runnerStateText("macbook", { online: true, info: {} }), "Runs on the MacBook (online)");
  eq("backend hold", pickDefaultBackend([{ name: "local" }, { name: "claude" }], true), "claude");
  eq("backend no hold", pickDefaultBackend([{ name: "local" }, { name: "claude" }], false), "local");
  eq("backend none", pickDefaultBackend([], true), "local");
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("ok");
