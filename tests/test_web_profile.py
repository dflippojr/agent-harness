"""Profile rendering and desktop Settings split regressions (#569)."""
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


def run_node(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run(
        [node, "--input-type=module"], input=source, cwd=TESTS,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_profile_page_renders():
    # Keep the existing module fixture while updating the accepted device label;
    # Desktop workers each own only their issue's listed test file.
    source = (TESTS / "web_profile.mjs").read_text(encoding="utf-8")
    run_node(source.replace('"This phone"', '"This device"'))


def test_settings_split_routes_and_lifecycle():
    run_node(r'''
import assert from "node:assert/strict";
import { runApp } from "./web_app_loader.mjs";
import { El as BaseEl, Emitter, Node, createDocument, fakeEventSource, storage, walk } from "./web_stub_dom.mjs";

class El extends BaseEl {
  get children() { return this.childNodes.filter(n => n instanceof BaseEl); }
  setAttribute(k, v) {
    super.setAttribute(k, v);
    if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
  }
  insertBefore(n, ref) { const i = this.childNodes.indexOf(ref); this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, n); n.parentNode = this; }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; }
  querySelectorAll(sel) { return sel === "[data-split-key]" ? walk(this, n => n !== this && n.dataset.splitKey !== undefined) : []; }
}
const { byId, doc } = createDocument({ ElClass: El });
doc.body.append(byId.bar, byId["tab-bar"], byId.app);
const wide = Object.assign(new Emitter(), { matches: true });
const win = new Emitter();
const loc = { origin: "http://localhost", protocol: "http:", pathname: "/", hash: "#/settings",
  replace(hash) { this.hash = hash; win.dispatchEvent({ type: "hashchange" }); } };
let role = "owner";
let failMenu = false;
let offline = false;
let backendEffort = "high";
let backendGate = null;
let profileEmoji = "🙂";
const fetched = [];
const sources = [];
const response = body => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => body });
const fetch = async (url, options = {}) => {
  const p = String(url).replace(/^https?:\/\/[^/]+/, "").replace(/^\/api\/(?:admin\/)?v1/, "").split("?")[0];
  fetched.push(p);
  if (offline && p !== "/health") throw new Error("offline");
  if (p === "/me") return response({ role, name: "Owner", notify: { enabled: false } });
  if (p === "/profile") {
    if (failMenu) throw new Error("offline");
    if (options.method === "PUT") profileEmoji = JSON.parse(options.body).emoji;
    return response({ emoji: profileEmoji, choices: ["🙂", "🤖"] });
  }
  if (p === "/health") return response({ protocols: { admin: { min: 1, max: 99 } }, update_hint: {} });
  if (p === "/backends/claude" && options.method === "PUT") {
    backendEffort = JSON.parse(options.body).effort;
    return response({});
  }
  if (p === "/backends") {
    const snapshot = [{ name: "claude", available: true, effort: backendEffort, today: {}, week: {} }];
    if (backendGate) await backendGate;
    return response(snapshot);
  }
  if (["/keys", "/accounts", "/sessions", "/models"].includes(p)) return response([]);
  if (p === "/config") return response({ revision: 1, settings: [] });
  return response({});
};
Object.assign(win, { location: loc, navigator: {}, localStorage: storage(), sessionStorage: storage(),
  history: { back() {} }, innerWidth: 1440, innerHeight: 900, scrollY: 0, scrollTo() {},
  matchMedia: q => q === "(min-width: 1280px)" ? wide : { matches: false, addEventListener() {} },
  requestAnimationFrame: f => setTimeout(f, 0), cancelAnimationFrame: clearTimeout,
  EventSource: fakeEventSource(sources), fetch });
await runApp({ window: win, document: doc, location: loc, navigator: win.navigator, history: win.history,
  localStorage: win.localStorage, sessionStorage: win.sessionStorage, fetch, EventSource: win.EventSource,
  getComputedStyle: el => el.style, requestAnimationFrame: win.requestAnimationFrame,
  cancelAnimationFrame: clearTimeout, setTimeout, clearTimeout, setInterval, clearInterval,
  Node, Event, URL, AbortController, TextDecoder, console });
const sleep = () => new Promise(r => setTimeout(r, 90));
const go = async hash => { loc.hash = hash; win.dispatchEvent({ type: "hashchange" }); await sleep(); };
const pane = () => doc.body.childNodes.find(n => n.id === "split-list");
const rows = () => walk(pane(), n => n.dataset?.splitKey !== undefined);
const marked = () => rows().filter(n => n.getAttribute("aria-current") === "page").map(n => n.dataset.splitKey);
const profileTab = byId["tab-bar"].querySelectorAll("a[data-tab]").find(n => n.dataset.tab === "profile");
await sleep();
assert.ok(doc.body.classList.contains("split"));
assert.match(byId.app.textContent, /No setting open/);
assert.equal(pane().getAttribute("aria-label"), "Settings menu");
assert.equal(rows().length, 16);
assert.match(pane().textContent, /This device/);
const menuRow = rows().find(n => n.dataset.splitKey === "appearance");
await go("#/profile/appearance");
assert.deepEqual(marked(), ["appearance"]);
assert.equal(rows().find(n => n.dataset.splitKey === "appearance"), menuRow, "menu survives row changes");
assert.equal(byId.back.hidden, true);
assert.equal(profileTab.getAttribute("aria-current"), "page");
assert.match(byId.app.textContent, /This device only/);
const dark = walk(byId.app, n => n.classList.contains("theme-choice") && n.textContent === "Dark")[0];
dark.click();
assert.match(menuRow.textContent, /Dark · Default text/, "menu value follows the theme without remounting");
const backendRow = rows().find(n => n.dataset.splitKey === "backends");
assert.match(backendRow.textContent, /Claude · high/);
await go("#/profile/backends");
const effort = walk(byId.app, n => n.tagName === "SELECT" && n.options.some(o => o.value === "high"))[0];
assert.ok(effort, byId.app.textContent);
effort.value = "low";
effort.dispatchEvent({ type: "change" });
await sleep();
assert.match(backendRow.textContent, /Claude · low/, "successful writes refresh the existing menu");
await go("#/settings");
assert.equal(rows().find(n => n.dataset.splitKey === "backends"), backendRow);
assert.match(backendRow.textContent, /Claude · low/, "returning to Settings refreshes server values");
let releaseBackend;
backendGate = new Promise(resolve => { releaseBackend = resolve; });
await go("#/profile/appearance");
backendEffort = "high";
backendGate = null;
await go("#/settings");
assert.match(backendRow.textContent, /Claude · high/);
releaseBackend();
await sleep();
assert.match(backendRow.textContent, /Claude · high/, "an older read cannot overwrite a newer value");
await go("#/actions/resources");
assert.deepEqual(marked(), ["resources"]);
assert.equal(profileTab.getAttribute("aria-current"), "page");
assert.equal(rows().find(n => n.dataset.splitKey === "appearance"), menuRow);
assert.ok(walk(byId.app, n => n.classList.contains("resources-tabs")).length);
await go("#/profile/daemon");
assert.deepEqual(marked(), ["daemon"]);
assert.ok(walk(byId.app, n => n.classList.contains("daemon-settings")).length);
await go("#/profile/connection");
const input = walk(byId.app, n => n.tagName === "INPUT")[0];
input.value = "https://unsaved.example";
wide.matches = false; win.innerWidth = 1024; wide.dispatchEvent({ type: "change" }); await sleep();
assert.equal(doc.body.classList.contains("split"), false);
assert.equal(byId.back.hidden, false);
assert.equal(walk(byId.app, n => n.tagName === "INPUT")[0], input, "resize preserves an unsaved form");
wide.matches = true; win.innerWidth = 1440; wide.dispatchEvent({ type: "change" }); await sleep();
assert.equal(input.value, "https://unsaved.example");
assert.deepEqual(marked(), ["connection"]);
await go("#/profile/account");
assert.deepEqual(marked(), ["account"]);
walk(byId.app, n => n.tagName === "BUTTON" && n.getAttribute("aria-label") === "Use 🤖")[0].click();
await sleep();
assert.equal(walk(pane(), n => n.classList.contains("identity-emoji"))[0].textContent, "🤖");
doc.dispatchEvent({ type: "keydown", key: "[", target: doc.body });
assert.ok(doc.body.classList.contains("split-collapsed"));
doc.dispatchEvent({ type: "keydown", key: "[", target: doc.body });
assert.equal(doc.body.classList.contains("split-collapsed"), false);
await go("#/agents");
assert.equal(pane().getAttribute("aria-label"), "Agents list");
assert.equal(win._l.hashchange.length, 1, "closing the menu removes its refresh listener");
offline = true;
win.localStorage.removeItem("harness.lastRole");
await go("#/settings");
assert.ok(rows().some(n => n.dataset.splitKey === "connection"), "offline menu retains the repair link");
assert.ok(!rows().some(n => n.dataset.splitKey === "resources"), "uncached offline identity has no owner actions");
assert.match(pane().textContent, /Can't reach|offline/);
await go("#/profile/connection");
assert.ok(walk(byId.app, n => n.tagName === "INPUT").length, "offline Connection settings still open");
offline = false;
const offlineMenu = pane();
win.dispatchEvent({ type: "online" });
await sleep();
assert.equal(loc.hash, "#/profile/connection", "reconnection requires no navigation");
assert.equal(pane(), offlineMenu);
assert.equal(rows().length, 16, "owner actions return when the offline identity reconnects");
assert.deepEqual(marked(), ["connection"]);
assert.ok(walk(pane(), n => n.className === "note bad")[0].hidden, "reconnection clears the initial error");
assert.match(rows().find(n => n.dataset.splitKey === "backends").textContent, /Claude · high/);
sources.at(-1).fail();
backendEffort = "low";
sources.at(-1).onopen();
await sleep();
assert.match(rows().find(n => n.dataset.splitKey === "backends").textContent, /Claude · low/, "live stream recovery refreshes without a browser online event");
await go("#/agents");
fetched.length = 0;
win.dispatchEvent({ type: "online" });
await sleep();
assert.ok(!fetched.includes("/backends"), "closing the menu removes its connection refresh listeners");
await go("#/settings");
role = "member"; fetched.length = 0;
await go("#/profile/appearance");
assert.ok(!rows().some(n => ["resources", "apps", "backends"].includes(n.dataset.splitKey)));
assert.ok(!fetched.some(p => ["/resources", "/keys", "/accounts", "/config"].includes(p)), "member menu reads no owner data");
await go("#/actions/resources");
assert.equal(loc.hash, "#/agents");
role = "guest";
await go("#/settings");
assert.ok(!rows().some(n => n.dataset.splitKey === "resources"));
await go("#/actions/resources");
assert.equal(loc.hash, "#/profile");
wide.matches = false; win.innerWidth = 390; wide.dispatchEvent({ type: "change" }); await sleep();
assert.equal(doc.body.classList.contains("split"), false);
assert.match(byId.app.textContent, /This device/);
console.log("ok");
process.exit(0);
''')


def test_settings_desktop_css_is_scoped():
    css = (TESTS.parent / "harness/web/style.css").read_text(encoding="utf-8")
    desktop = css.split("/* Desktop 8:", 1)[1].split("/* End Desktop 8. */", 1)[0]
    assert "@media (min-width: 768px)" in desktop
    assert "@media (min-width: 1280px)" in desktop
    assert "repeat(4, minmax(0, 1fr))" in desktop
    assert ".resources-tabs button" in desktop
    assert "white-space: nowrap" in desktop
