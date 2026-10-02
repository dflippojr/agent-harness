// Runs harness/web/app.js in a UI harness's vm sandbox. app.js is an ES module entry, so its static imports are
// rebound before it runs as a script: client.mjs names come from the harness's own sandbox stubs, and every
// lib/ module is imported for real here and handed to the sandbox (#258).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { runInContext } from "node:vm";

const web = join(dirname(fileURLToPath(import.meta.url)), "..", "harness", "web");
const IMPORT = /^import \{([^}]+)\} from "\.\/([^"]+)";\r?\n/gm;

const entry = readFileSync(join(web, "app.js"), "utf8");
const modules = {};
for (const [, , path] of entry.matchAll(IMPORT)) {
  if (path !== "client.mjs") modules[path] = await import(pathToFileURL(join(web, path)).href);
}
const appSrc = entry.replace(IMPORT, (_, names, path) => {
  if (path === "client.mjs") return "";
  const bindings = names.split(",").map((n) => n.trim().replace(/\s+as\s+/, ": ")).join(", ");
  return `const { ${bindings} } = __webModules[${JSON.stringify(path)}];\n`;
});

export function runApp(sandbox) {
  sandbox.__webModules = modules;
  return runInContext(appSrc, sandbox);
}
