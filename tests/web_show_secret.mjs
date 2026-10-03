// showSecretOnce (hoisted from four nested callbacks, #229, then into lib/secret.mjs, #258): shows the secret once, Copy copies it,
// Done reloads. It builds DOM through dom.mjs, so a stub document records the elements and their listeners.
const listenersOf = (el, type) => el.listeners[type] || [];
class StubNode {}
class StubEl extends StubNode {
  constructor(tag) { super(); this.tag = tag; this.attrs = {}; this.kids = []; this.listeners = {}; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  append(...nodes) { this.kids.push(...nodes); }
  replaceChildren(...nodes) { this.kids = [...nodes]; }
}
let focused = 0;
globalThis.Node = StubNode;
globalThis.document = {
  createElement: (tag) => { const el = new StubEl(tag); el.select = () => { focused++; }; return el; },
  createTextNode: (text) => ({ text: String(text) }),
};
const { showSecretOnce } = await import("../harness/web/lib/secret.mjs");

const failures = [];
const eq = (label, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) failures.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};

const copies = [];
const copyToClipboard = (text, after) => { copies.push(text); after(); };
let reloaded = 0;
const form = new StubEl("div");
showSecretOnce(form, () => { reloaded++; }, "Intro text", "sekret", "Copy install command", copyToClipboard);
const [intro, field, row] = form.kids;
eq("secret intro", intro.kids.map((t) => t.text), ["Intro text"]);
eq("secret field", [field.attrs.readonly, field.attrs.value], ["", "sekret"]);
eq("secret labels", row.kids.map((b) => b.kids[0].text), ["Copy install command", "Done"]);
let selected = 0;
listenersOf(field, "click")[0]({ target: { select: () => { selected++; } } });
eq("field click selects", selected, 1);
listenersOf(row.kids[0], "click")[0]();
eq("copy", copies, ["sekret"]);
eq("copy reselects field", focused, 1);
listenersOf(row.kids[1], "click")[0]();
eq("done reloads", reloaded, 1);

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("ok");
