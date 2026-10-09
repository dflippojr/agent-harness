// Renders the extracted Profile page with stub deps so a missing import or dep fails CI (#258 stage g).
import assert from "node:assert/strict";
import { mountProfile } from "../harness/web/pages/profile.mjs";

const el = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
  addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, style: {} });
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));
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
const appended = [];
const gone = [];
const headers = [];
const profile = mountProfile({
  $app: "APP", $conn: el("conn"), $profileIcon: el("icon"), layoutBar() {}, setHeader: (...a) => headers.push(a), h: el, fill() {}, append: (_app, ...n) => appended.push(...n),
  api: async (path) => (path === "/me" ? { name: "Dan", role: "owner" } : { emoji: "🙂", choices: [] }),
  getWebAuth: () => null, startGoogle: async () => {}, agentHarnessWeb: { baseUrl: "http://x", token: "", csrf: "", configure() {} },
  isGuest: () => false, isMember: () => false, isOwner: () => owner, toast() {}, go: (...a) => gone.push(a), route: async () => {},
  daemonSettingsCard: () => el("daemon"), browser,
});
for (const name of ["viewProfile", "copyBox", "githubConnectionCard", "readAppIcon", "applyAppIcon", "applyTheme", "applyTextSize"]) {
  assert.equal(typeof profile[name], "function", name);
}

await profile.viewProfile();
assert.match(text(appended[0]), /Dan|🙂/);
assert.deepEqual(headers.at(-1), ["agents", "Profile", { page: true }]);
// The drawer's Actions entry lives in the menu now (#506), for the owner only.
const links = (n) => (n && typeof n === "object" ? [n.attrs?.href, ...(n.kids || []).flatMap(links)].filter(Boolean) : []);
assert.ok(appended.flatMap(links).includes("#/actions/resources"), "owner menu links to Actions");
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
for (const page of ["account", "notifications", "install", "backends", "smart-approvals", "daemon", "memory", "skills", "apps", "endpoint"]) {
  try { await profile.viewProfile(page); } catch (e) { assert.ok(!(e instanceof ReferenceError), `${page}: ${e.message}`); }
}
await profile.viewProfile("nope");
assert.deepEqual(gone.slice(-1), [["#/profile", true]]);
console.log("ok");
