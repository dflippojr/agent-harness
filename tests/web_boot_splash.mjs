// Exercise the actual inline fallback and boot promise chain without a browser dependency.
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(join(root, "harness/web/index.html"), "utf8");
const css = readFileSync(join(root, "harness/web/style.css"), "utf8");
const app = readFileSync(join(root, "harness/web/app.js"), "utf8");
const inline = html.match(/<script>\s*(\/\/ Runs before the module:[\s\S]*?)<\/script>/)[1];
const boot = app.slice(app.lastIndexOf("void checkCompatibility().then"));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function launch() {
  let present = true;
  let timer = null;
  let delay = null;
  const window = {};
  runInNewContext(inline, {
    window,
    document: { getElementById(id) {
      assert.equal(id, "boot-splash");
      return { remove() { present = false; } };
    } },
    setTimeout(fn, ms) { timer = fn; delay = ms; return 1; },
    clearTimeout(id) { assert.equal(id, 1); timer = null; },
  });
  return { window, get present() { return present; }, get timer() { return timer; }, delay };
}

// Every document contains the decorative, static brand before any boot code runs.
assert.match(html, /<div id="boot-splash" aria-hidden="true">\s*<img src="\/icon-192.png" width="96" height="96" alt="">\s*<\/div>/);
assert.ok(existsSync(join(root, "harness/web/icon-192.png")));
assert.ok(html.indexOf('id="boot-splash"') < html.indexOf("// Runs before the module:"));
assert.ok(html.indexOf("// Runs before the module:") < html.indexOf('type="module"'));
assert.match(html, /style\.css\?v=5/);
assert.match(css, /#boot-splash\s*\{[^}]*position: fixed;[^}]*inset: 0;[^}]*background: var\(--bg\);[^}]*pointer-events: none;/);
assert.match(css, /#boot-splash img\s*\{ animation: boot-splash-in 120ms ease-out; \}/);
assert.match(css, /@media \(prefers-reduced-motion: reduce\)\s*\{\s*#boot-splash img\s*\{ animation: none; \}/);

// The cap works with no app module at all. Dismissal is immediate and idempotent.
const missingModule = launch();
assert.equal(missingModule.present, true);
assert.equal(missingModule.delay, 4000);
missingModule.timer();
assert.equal(missingModule.present, false);
assert.equal(missingModule.timer, null);
missingModule.window.dismissBootSplash();
assert.equal(launch().present, true, "a subsequent full load shows the splash again");

async function bootScenario({ compatible = true, user = {}, failure = null, timeout = false } = {}) {
  const splash = launch();
  const view = deferred();
  let routes = 0;
  let painted = false;
  const context = {
    window: splash.window,
    checkCompatibility: async () => {
      if (failure === "compatibility") throw new Error("startup failed");
      return compatible;
    },
    currentUser: async () => user,
    paintGuestChrome() {},
    isGuest: () => user?.role === "guest",
    warmModel() {},
    loadProfileIcon: async () => {
      if (failure === "profile") throw new Error("profile failed");
    },
    applyAppIcon() {}, readAppIcon() { return "profile"; },
    route: async () => {
      routes++;
      await view.promise;
      painted = true;
    },
  };
  // Replace only `void` so the test can await settlement of the production chain.
  const done = runInNewContext(boot.replace(/^void /, ""), context);
  const outcome = done.then(() => null, (error) => error);
  for (let i = 0; i < 20; i++) await Promise.resolve();
  if (!compatible || !user || failure) {
    assert.equal(routes, 0);
    assert.equal(splash.present, false, "early exits and startup failures dismiss without the cap");
  } else {
    assert.equal(routes, 1);
    assert.equal(painted, false);
    assert.equal(splash.present, true, "wait for the first route to settle");
    if (timeout) {
      splash.timer();
      assert.equal(splash.present, false, "a stalled first view is revealed at the cap");
    }
    view.resolve();
  }
  const error = await outcome;
  assert.equal(Boolean(error), Boolean(failure));
  assert.equal(splash.present, false);
  assert.equal(splash.timer, null);
  if (compatible && user && !failure) {
    await context.route();
    assert.equal(splash.present, false, "in-app navigation never recreates the splash");
  }
}

await bootScenario();
await bootScenario({ user: { role: "guest" } });
await bootScenario({ user: null });
await bootScenario({ compatible: false });
await bootScenario({ failure: "compatibility" });
await bootScenario({ failure: "profile" });
await bootScenario({ timeout: true });
console.log("ok: boot splash show, readiness, early exits, failures, timeout and reduced motion");
