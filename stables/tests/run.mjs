// Runs every Stables Node harness in its own process: `node stables/tests/run.mjs [name-filter...]`.
// A harness passes when it exits 0. `--list` prints the runs without starting them. No dependencies, no pytest.
import { spawn } from "node:child_process";
import { readdirSync } from "node:fs";
import { availableParallelism } from "node:os";
import { fileURLToPath } from "node:url";

const directory = fileURLToPath(new URL("./", import.meta.url));
// Imported by the harnesses; they have no tests of their own.
export const HELPERS = new Set(["run.mjs", "web_app_loader.mjs", "web_stub_dom.mjs"]);
// Harnesses that take a scenario argument run once per scenario.
export const SCENARIOS = {
  "web_boot_requests.mjs": ["owner", "member", "guest", "me-fallback", "client-update", "daemon-update", "offline",
    "offline-cached"],
};
const TIMEOUT_MS = 60_000;

export function runs(filters = []) {
  return readdirSync(directory)
    .filter((name) => name.endsWith(".mjs") && !HELPERS.has(name))
    .sort()
    .flatMap((name) => (SCENARIOS[name] || [null]).map((scenario) => ({ name, args: scenario ? [scenario] : [] })))
    .filter((run) => !filters.length || filters.some((filter) => run.name.includes(filter)));
}

function label(run) {
  return [run.name, ...run.args].join(" ");
}

function start(run) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [run.name, ...run.args], { cwd: directory, timeout: TIMEOUT_MS });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { output += chunk; });
    child.on("error", (error) => resolve({ run, code: 1, output: `${output}${error}\n` }));
    child.on("close", (code, signal) => resolve({ run, code: signal ? `${signal}` : code, output }));
  });
}

async function main(argv) {
  const filters = argv.filter((arg) => !arg.startsWith("--"));
  const selected = runs(filters);
  if (argv.includes("--list")) {
    for (const run of selected) console.log(label(run));
    return 0;
  }
  if (!selected.length) {
    console.error(`no harness matches ${filters.join(", ")}`);
    return 1;
  }
  const queue = [...selected];
  const failed = [];
  const worker = async () => {
    for (let run = queue.shift(); run; run = queue.shift()) {
      const result = await start(run);
      if (result.code === 0) {
        console.log(`pass  ${label(run)}`);
      } else {
        failed.push(result);
        console.log(`FAIL  ${label(run)} (exit ${result.code})`);
      }
    }
  };
  // Modest parallelism: each harness is its own Node process, and CI and the tower share RAM.
  await Promise.all(Array.from({ length: Math.min(4, availableParallelism()) }, worker));
  for (const { run, output } of failed) console.log(`\n--- ${label(run)}\n${output.trimEnd()}`);
  console.log(`\n${selected.length - failed.length} passed, ${failed.length} failed`);
  return failed.length ? 1 : 0;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  process.exitCode = await main(process.argv.slice(2));
}
