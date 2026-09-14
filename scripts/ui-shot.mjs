// Phone-sized screenshots of the web app through headless Edge's DevTools protocol (no npm packages).
//   node scripts/ui-shot.mjs <out-dir> <name>=<url>[#after-js] ...
// Each shot waits for the page to settle; "#after-js" (URL-encoded JS after a '|') runs before capture.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const [outDir, ...shots] = process.argv.slice(2);
mkdirSync(outDir, { recursive: true });
const port = 9333;
const profile = join(tmpdir(), "harness-ui-shot");
const edge = spawn(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--no-first-run", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let wsUrl;
for (let i = 0; i < 50 && !wsUrl; i++) {
  await sleep(200);
  try {
    const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    wsUrl = pages.find((p) => p.type === "page")?.webSocketDebuggerUrl;
  } catch (_) { /* not up yet */ }
}
if (!wsUrl) { edge.kill(); throw new Error("Edge DevTools endpoint didn't come up"); }

const ws = new WebSocket(wsUrl);
await new Promise((r) => ws.addEventListener("open", r));
let nextId = 1;
const pending = new Map();
const logs = [];
ws.addEventListener("message", (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  if (msg.method === "Runtime.consoleAPICalled") logs.push(msg.params.args.map((a) => a.value ?? a.description).join(" "));
  if (msg.method === "Runtime.exceptionThrown") logs.push("EXCEPTION " + JSON.stringify(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text));
});
const send = (method, params = {}) => new Promise((resolve) => {
  const id = nextId++;
  pending.set(id, resolve);
  ws.send(JSON.stringify({ id, method, params }));
});

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: process.env.SCHEME || "dark" }] });

for (const spec of shots) {
  const eq = spec.indexOf("=");
  const name = spec.slice(0, eq);
  const [url, js] = spec.slice(eq + 1).split("|");
  await send("Page.navigate", { url });
  await sleep(2500);
  if (js) {
    const r = await send("Runtime.evaluate", { expression: decodeURIComponent(js), awaitPromise: true, returnByValue: true });
    logs.push(`[${name}] eval -> ${JSON.stringify(r.result?.result?.value ?? r.result?.exceptionDetails?.text)}`);
    await sleep(2000);
  }
  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(join(outDir, `${name}.png`), Buffer.from(shot.result.data, "base64"));
  console.log(`saved ${name}.png`);
}
if (logs.length) console.log("console:\n" + logs.join("\n"));
ws.close();
edge.kill();
process.exit(0);
