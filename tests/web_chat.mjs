// Renders the extracted chat page (welcome state and a durable conversation) with stub deps so a missing import or dep fails CI (#258 stage k).
import assert from "node:assert/strict";
import { mountChat } from "../harness/web/pages/chat.mjs";

const el = (tag, attrs, ...kids) => {
  const node = { tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k !== null && k !== undefined),
    value: "", hidden: false, dataset: {}, style: {}, options: [], addEventListener() {}, querySelector: () => null, querySelectorAll: () => [],
    remove() {}, setAttribute() {}, focus() {} };
  node.append = (...more) => node.kids.push(...more.flat(Infinity));
  node.prepend = (...more) => node.kids.unshift(...more.flat(Infinity));
  return node;
};
const text = (n) => (n && typeof n === "object" ? [...(n.kids || [])].map(text).join(" ") : String(n ?? ""));

const store = new Map();
const bodyKids = [];
const classes = new Set();
const browser = {
  window: { innerHeight: 800, visualViewport: null, scrollTo() {} },
  document: { body: { scrollHeight: 1, append: (...n) => bodyKids.push(...n), classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) } } },
  localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) },
  confirm: () => true, prompt: () => "",
};
const api = async (path) => {
  if (path === "/chats/options") return { default_backend: "local", backends: [{ name: "local", models: ["m1"], model: "m1", efforts: [] }] };
  if (path === "/chats/c1") return { id: "c1", title: "Notes", status: "done", backend: "local", model: "m1", effort: "" };
  throw new Error(`unexpected ${path}`);
};
const rendered = [];
const headers = [];
const streams = [];
const leave = [];
const page = mountChat({
  $app: "APP", h: el, fill: () => {}, append: (_t, ...n) => rendered.push(...n.flat(Infinity)), api, setHeader: (...a) => headers.push(a), toast() {},
  go() {}, validId: () => true, canChat: () => true, onLeave: (fn) => leave.push(fn), badge: (s) => el("badge", {}, s),
  openStream: (url, handlers) => { streams.push({ url: url(), handlers }); return () => {}; }, ownerSurface: () => ({}),
  TERMINAL: new Set(["done", "failed", "cancelled"]), agentHarnessWeb: { url: (p) => p, token: "" }, browser,
});
assert.equal(typeof page.viewChat, "function");

await page.viewChat();
assert.deepEqual(headers.at(-1), ["chat", "Chat"]);
assert.match(rendered.map(text).join(" "), /How can I help\?/);
assert.ok(classes.has("chat-page"), "the chat page class is set while the route is open");
assert.equal(bodyKids.length, 1, "the composer is attached to the body");
leave.forEach((fn) => fn());
assert.ok(!classes.has("chat-page"));

rendered.length = 0;
await page.viewChat("c1");
assert.deepEqual(headers.at(-1), ["chat", "Notes"]);
assert.equal(streams.length, 1);
assert.match(streams[0].url, /\/chats\/c1\/events\?after=0/);
streams[0].handlers.user_message({ seq: 1, data: { content: "hello there" } });
streams[0].handlers.assistant({ seq: 2, data: { content: "Hi!" } });
streams[0].handlers.status({ seq: 3, data: { status: "done" } });
const wrap = rendered.find((n) => n.attrs?.class === "chat-wrap");
assert.ok(wrap, "the chat wrap is appended");
assert.match(text(wrap), /hello there/);
assert.ok(wrap.kids.some((k) => k.attrs?.class === "chat-feed" && k.kids.some((m) => m.attrs?.html?.includes("Hi!"))), "the assistant reply is in the feed");
console.log("ok");
