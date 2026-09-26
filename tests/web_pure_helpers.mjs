// UI harness: the small pure helpers pulled out of app.js by the Sonar style cleanup (#229) keep the
// exact strings and choices the inline ternaries produced. Each helper is sliced out of the source and
// evaluated on its own, so no DOM is needed.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = readFileSync(join(root, "harness/web/app.js"), "utf8").replace(/\r\n/g, "\n");

// A top-level `function name(...) {...}` (cut at the first closing brace on its own line) or a
// `const name = ...;` that is either one line or a block arrow closed by `};` on its own line.
function slice(name) {
  const fn = src.indexOf(`function ${name}(`);
  if (fn >= 0) {
    const end = src.indexOf("\n}\n", fn);
    return src.slice(fn, end + 3);
  }
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`helper not found: ${name}`);
  const firstLine = src.indexOf("\n", start);
  const multiline = src.slice(start, firstLine).endsWith("{");
  const end = multiline ? src.indexOf("\n};\n", start) + 4 : firstLine + 1;
  return src.slice(start, end);
}

const load = (names, env = {}) => {
  const keys = Object.keys(env);
  const body = `${names.map(slice).join("\n")}\nreturn { ${names.join(", ")} };`;
  return new Function(...keys, body)(...keys.map((k) => env[k]));
};

const failures = [];
const eq = (label, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) failures.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};

{
  const { fmtTokens, fmtSpan, pluralize } = load(["fmtTokens", "fmtSpan", "pluralize"]);
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
  const { compareTargets } = load(["compareTargets"]);
  eq("targets sort", ["mac", "tower", "alpha"].sort(compareTargets), ["tower", "alpha", "mac"]);
}

{
  const { holdRemainingText } = load(["holdRemainingText", "fmtSpan"]);
  eq("hold forever", holdRemainingText(null), "until you turn it off");
  eq("hold seconds", holdRemainingText(60), "for about 60 s");
  eq("hold minutes", holdRemainingText(100), "for about 2 min");
}

{
  const { toolSummaryText } = load(["toolSummaryText"]);
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
  const { approvalWhat, approvalDiffClass } = load(["approvalWhat", "approvalDiffClass"]);
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
  const { diffLineClass } = load(["diffLineClass"]);
  eq("diff +++", diffLineClass("+++ b/x"), "");
  eq("diff ---", diffLineClass("--- a/x"), "");
  eq("diff add", diffLineClass("+x"), "add");
  eq("diff del", diffLineClass("-x"), "del");
  eq("diff hunk", diffLineClass("@@ x"), "hunk");
}

{
  const { protocolMismatch } = load(["protocolMismatch"], { WEB_PROTOCOL: 5 });
  eq("protocol none", protocolMismatch(undefined), null);
  eq("protocol ok", protocolMismatch({ min: 4, max: 6 }), null);
  eq("protocol client old", protocolMismatch({ min: 6, max: 8 }), "client_update_required");
  eq("protocol daemon old", protocolMismatch({ min: 1, max: 4 }), "daemon_update_required");
}

{
  const { settingValueText, recoveryNote, lastSeenText, compatibilityText } =
    load(["settingValueText", "recoveryNote", "lastSeenText", "compatibilityText"]);
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
  const { runnerStateText, pickDefaultBackend } = load(["runnerStateText", "pickDefaultBackend"], { TARGET_LABEL: { macbook: "MacBook" } });
  eq("runner offline", runnerStateText("macbook", { online: false }),
    "Runs on the MacBook, which is offline or asleep: the task will wait for it");
  eq("runner missing", runnerStateText("x", undefined), "Runs on the x, which is offline or asleep: the task will wait for it");
  eq("runner online", runnerStateText("macbook", { online: true, info: { free_gb: 12 } }), "Runs on the MacBook (online, 12 GB free)");
  eq("runner online no gb", runnerStateText("macbook", { online: true, info: {} }), "Runs on the MacBook (online)");
  eq("backend hold", pickDefaultBackend([{ name: "local" }, { name: "claude" }], true), "claude");
  eq("backend no hold", pickDefaultBackend([{ name: "local" }, { name: "claude" }], false), "local");
  eq("backend none", pickDefaultBackend([], true), "local");
}

{
  // showSecretOnce (hoisted from four nested callbacks): shows the secret once, Copy copies it, Done reloads.
  const calls = [];
  let focused = 0;
  const h = (tag, attrs = {}, ...kids) => ({ tag, attrs, kids, select: () => { focused++; } });
  const fill = (form, ...kids) => calls.push(["fill", form, kids]);
  const copyToClipboard = (text, after) => { calls.push(["copy", text]); after(); };
  const { showSecretOnce } = load(["showSecretOnce"], { h, fill, copyToClipboard });
  let reloaded = 0;
  const form = {};
  showSecretOnce(form, () => { reloaded++; }, "Intro text", "sekret", "Copy install command");
  const [, gotForm, [intro, field, row]] = calls[0];
  eq("secret form", gotForm, form);
  eq("secret intro", intro.kids, ["Intro text"]);
  eq("secret field", [field.attrs.readonly, field.attrs.value], [true, "sekret"]);
  eq("secret labels", row.kids.map((b) => b.kids[0]), ["Copy install command", "Done"]);
  let selected = 0;
  field.attrs.onclick({ target: { select: () => { selected++; } } });
  eq("field click selects", selected, 1);
  row.kids[0].attrs.onclick();
  eq("copy", calls[1], ["copy", "sekret"]);
  eq("copy reselects field", focused, 1);
  row.kids[1].attrs.onclick();
  eq("done reloads", reloaded, 1);
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("ok");
