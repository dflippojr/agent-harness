// Boots the real harness/web/app.js under Node for the UI harnesses (#258). Each harness builds a stub DOM and a sandbox of
// browser globals; they are installed on globalThis (the app and its lib/ modules read browser globals at call time), then
// app.js is imported like any other module. Nothing reads app.js as text.
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..", "harness", "web");

export async function runApp(sandbox) {
  for (const [key, value] of Object.entries(sandbox)) {
    if (globalThis[key] === value) continue;
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true, enumerable: true });
  }
  return import(pathToFileURL(join(web, "app.js")).href);
}
