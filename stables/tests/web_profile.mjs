// Renders the extracted Profile page with stub deps so a missing import or dep fails CI (#258 stage g).
import assert from "node:assert/strict";
import { mountProfile } from "../public/pages/profile.mjs";

const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
  addEventListener() {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, setAttribute() {}, style: {} });
const text = (n) => (n && typeof n === "object" ? [n.textContent ?? "", ...(n.kids || []).map(text)].join(" ") : String(n ?? ""));
const store = new Map();
const browser = {
  document: { documentElement: { style: { setProperty() {}, removeProperty() {} }, dataset: {}, setAttribute() {} },
    querySelector: () => null, querySelectorAll: () => [], createElement: () => ({ style: {}, getContext: () => null }) },
  window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
  localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) },
  location: { origin: "http://x", hash: "#/profile" }, navigator: {}, history: {}, getComputedStyle: () => ({ getPropertyValue: () => "" }),
  requestAnimationFrame: (f) => f(), confirm: () => true, prompt: () => "", open() {}, setTimeout, clearTimeout, fetch: async () => ({ ok: false }),
};

let owner = true;
let member = false;
// Settings row values (#512): each value path answers with synthetic data; `failing` makes every one of them throw.
let failing = false;
const VALUES = {
  "/backends?auth=skip": [{ name: "local", available: true }, { name: "claude", available: true, effort: "high", model: "opus", today: {}, week: {} }],
  "/smart-approvals": { configured: true, mode: "shadow" },
  "/memory": { enabled: true, writes: true, categories: ["people"], profile: "Be brief." },
  "/resources": { enabled: true, manual: true, manual_remaining_seconds: 41 * 60, signals: [] },
  "/remote-control": { enabled: false },
};
// Daemon administration reads (#545): Settings never asks for them.
const ADMIN_READS = ["/config", "/accounts", "/skills", "/keys", "/pairing-codes", "/runner-pairing-codes"];
const POINTER = /Daemon administration is in the Hub or the\s+harness\s+CLI\./;
const called = [];
const appended = [];
const gone = [];
const headers = [];
const conn = el("conn");
const connListeners = new Set();
const profile = mountProfile({
  $app: "APP", $conn: conn, $profileIcon: el("icon"), layoutBar() {}, setHeader: (...a) => headers.push(a), h: el, fill: (node, ...kids) => { node.kids = kids.flat(Infinity).filter((k) => k !== null && k !== undefined); }, // Like lib/dom.mjs append(): one level of arrays is flattened, so a nested array would show up as a non-node.
  append: (_app, ...n) => appended.push(...n.flat().filter((k) => k != null)),
  api: async (path) => {
    called.push(path);
    if (path === "/me") return { name: "Dan", role: "owner", notify: { enabled: true } };
    if (Object.hasOwn(VALUES, path)) {
      if (failing) throw new Error("offline");
      return VALUES[path];
    }
    return { emoji: "🙂", choices: [] };
  },
  getWebAuth: () => null, startGoogle: async () => {},
  agentHarnessWeb: { baseUrl: "", token: "", csrf: "", configure() {}, independent: false,
    compatibility: async () => { if (failing) throw new Error("offline"); return { release: "0.9.0", protocols: { admin: { min: 1, max: 2 } }, update_hint: { web: { build_id: "B2" } } }; } },
  isGuest: () => false, isMember: () => member, isOwner: () => owner, toast() {}, go: (...a) => gone.push(a), route: async () => {},
  build: { WEB_BUILD_ID: "B1", WEB_PROTOCOL: 2 }, reloadAndUpdate: async () => true, browser,
  onConnState: (fn) => { connListeners.add(fn); return () => connListeners.delete(fn); },
});
for (const name of ["viewProfile", "copyBox", "githubConnectionCard", "readAppIcon", "applyAppIcon", "applyTheme", "applyTextSize"]) {
  assert.equal(typeof profile[name], "function", name);
}

const settle = () => new Promise((r) => setTimeout(r, 0));
const find = (n, pred) => (n && typeof n === "object" ? (pred(n) ? [n] : []).concat((n.kids || []).flatMap((k) => find(k, pred))) : []);
const rows = () => appended.flatMap((n) => find(n, (x) => x.attrs?.class === "set-row"));
const rowValue = (id) => rows().find((r) => r.attrs["data-setting"] === id)?.kids.find((k) => k.attrs?.class === "set-value")?.textContent;
const labels = () => appended.flatMap((n) => find(n, (x) => x.attrs?.class === "section-label")).map(text).map((t) => t.trim());

await profile.viewProfile();
await settle();
assert.match(text(appended[0]), /Dan|🙂/);
assert.match(text(appended[0]), /Bundled server · offline/);
assert.deepEqual(headers.at(-1), ["agents", "Profile", { page: true }]);
// Grouped as the design has it (#512), Actions folded under Server, for the owner only.
assert.deepEqual(labels(), ["This device", "Agents", "Server", "Integrations"]);
assert.ok(appended.every((n) => n && typeof n === "object" && !Array.isArray(n)), "the menu appends nodes, not nested arrays");
const links = (n) => (n && typeof n === "object" ? [n.attrs?.href, ...(n.kids || []).flatMap(links)].filter(Boolean) : []);
const hrefs = appended.flatMap(links);
for (const tab of ["resources", "remote-control", "disk"]) assert.ok(hrefs.includes(`#/actions/${tab}`), `owner Server row ${tab}`);
// Daemon administration rows moved to the Hub and the CLI (#545); one line says so.
for (const href of ["#/actions/accounts", "#/profile/daemon", "#/profile/skills", "#/profile/apps"]) assert.ok(!hrefs.includes(href), href);
assert.deepEqual(rows().map((r) => r.attrs["data-setting"]), ["appearance", "notifications", "connection", "install", "backends",
  "smart-approvals", "memory", "resources", "remote-control", "disk", "endpoint"]);
assert.match(appended.map(text).join(" "), POINTER);
// Every row shows its current value.
assert.equal(rowValue("appearance"), "System · Default text");
assert.equal(rowValue("connection"), "Bundled server");
assert.equal(rowValue("notifications"), "ntfy · on");
assert.equal(rowValue("backends"), "Claude · high +1");
assert.equal(rowValue("smart-approvals"), "Shadow");
assert.equal(rowValue("memory"), "Writes on");
assert.equal(rowValue("resources"), "GPU held · 41 min");
assert.equal(rowValue("remote-control"), "Off");
assert.equal(rowValue("endpoint"), undefined, "the endpoint row reads no keys");
assert.ok(!called.some((p) => ADMIN_READS.includes(p)), "the menu reads no daemon administration");
assert.ok(!called.includes("/maintenance"), "the menu never measures the disk");
// The version row: this bundle, its protocol, the server's view of it, and Reload and update once a newer bundle is offered.
const version = () => appended.flatMap((n) => find(n, (x) => x.attrs?.class === "version-row"))[0];
assert.match(text(version()), /Stables B1 · protocol 2 · update available/);
assert.match(text(version()), /Server 0\.9\.0 · protocol 1–2/);
assert.match(text(version()), /Reload and update/);
// Every value request failing still paints the whole menu, values blank, with no unhandled rejection.
failing = true;
appended.length = 0;
await profile.viewProfile();
await settle();
assert.equal(rows().length, 11);
assert.equal(rowValue("resources"), undefined);
assert.match(text(version()), /server not reachable/);
failing = false;
// A member sees no Server actions, and no Agents header since every Agents row is hidden for them.
member = true;
owner = false;
appended.length = 0;
called.length = 0;
await profile.viewProfile();
await settle();
assert.ok(!labels().includes("Agents"), "no empty Agents group");
assert.ok(!appended.flatMap(links).some((href) => href.startsWith("#/actions")), "members get no Actions links");
assert.ok(!called.some((p) => Object.hasOwn(VALUES, p)), "non-owners read no owner values");
assert.doesNotMatch(appended.map(text).join(" "), POINTER, "members administer nothing, so no pointer");
member = false;
owner = true;
// The header's Settings gear opens the same menu under its own name.
browser.location.hash = "#/settings";
owner = false;
appended.length = 0;
await profile.viewProfile();
assert.deepEqual(headers.at(-1), ["agents", "Settings", { page: true }]);
assert.ok(!appended.flatMap(links).some((href) => href.startsWith("#/actions")), "non-owners get no Actions links");
browser.location.hash = "#/profile";
await profile.viewProfile("connection");
assert.match(text(appended.at(-1)), /Server URL/);
await profile.viewProfile("appearance");
assert.ok(appended.length >= 3);
// Every subpage must render without a missing dep; other errors (stub gaps) are tolerated, a ReferenceError is not.
for (const page of ["account", "notifications", "install", "backends", "smart-approvals", "memory", "endpoint"]) {
  try { await profile.viewProfile(page); } catch (e) { assert.ok(!(e instanceof ReferenceError), `${page}: ${e.message}`); }
}
await profile.viewProfile("nope");
assert.deepEqual(gone.slice(-1), [["#/profile", true]]);
// The removed pages are gone too: old bookmarks land on the menu.
for (const page of ["daemon", "skills", "apps"]) {
  gone.length = 0;
  await profile.viewProfile(page);
  assert.deepEqual(gone, [["#/profile", true]], page);
}
// Where a removed control was, the subpage shows the pointer line instead (#545).
owner = true;
for (const page of ["connection", "notifications", "backends", "smart-approvals", "memory", "endpoint"]) {
  appended.length = 0;
  await profile.viewProfile(page);
  await settle();
  assert.match(appended.map(text).join(" "), POINTER, page);
  const controls = appended.flatMap((n) => find(n, (x) => ["button", "select"].includes(x.tag))).map((x) => text(x).trim());
  for (const removed of ["Send test notification", "Shadow", "Auto", "Off", "Save", "New key", "Revoke", "Create owner token"]) {
    assert.ok(!controls.includes(removed), `${page}: ${removed}`);
  }
  if (page === "backends") assert.ok(!appended.some((n) => find(n, (x) => x.tag === "select").length), "no model or effort pickers");
}
assert.ok(!called.some((p) => ADMIN_READS.includes(p)), "no subpage reads daemon administration");

// The identity line follows the header chip while Settings is open (#512, #510): no state before the chip first
// shows, then each state the chip reports; the first report after the page is gone unsubscribes.
owner = true;
connListeners.clear();
conn.hidden = true;
appended.length = 0;
await profile.viewProfile();
const note = find(appended[0], (x) => x.attrs?.class === "muted small")[0];
assert.equal(text(note).trim(), "Bundled server");
assert.equal(connListeners.size, 1);
note.isConnected = true;
const report = (state) => {
  conn.hidden = false;
  conn.dataset = { state };
  for (const fn of [...connListeners]) fn(state);
};
report("reconnecting");
assert.equal(note.textContent, "Bundled server · reconnecting");
report("live");
assert.equal(note.textContent, "Bundled server · live");
note.isConnected = false;
report("offline");
assert.equal(note.textContent, "Bundled server · live", "a line off the page is left alone");
assert.equal(connListeners.size, 0, "and stops listening");
console.log("ok");
