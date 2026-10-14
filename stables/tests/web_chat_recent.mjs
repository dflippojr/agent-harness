// Recent chats sit outside the welcome gradient.
import assert from "node:assert/strict";
import { page, rendered, text } from "./web_chat.mjs";

rendered.length = 0;
await page.viewChat();
const home = rendered.find((n) => n.attrs?.class === "chat-wrap");
const welcome = home.kids.find((n) => n.attrs?.class === "chat-welcome");
assert.deepEqual(welcome.kids.map((n) => n.attrs?.class), ["chat-welcome-content", "recent-chats-section"]);
assert.match(text(welcome.kids[0]), /How can I help\?/);
console.log("ok");
