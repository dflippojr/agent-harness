// showSecretOnce (hoisted from four nested callbacks, #229): shows the secret once, Copy copies it, Done reloads.
// It builds DOM and still lives in app.js, so it is sliced out of the source with h/fill/copyToClipboard stubbed.
// Delete this slicer when the Actions/Settings pages move to harness/web/pages/ and it can be imported (#258).
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../harness/web/app.js", import.meta.url), "utf8").replace(/\r\n/g, "\n");
const start = src.indexOf("function showSecretOnce(");
if (start < 0) throw new Error("showSecretOnce missing from app.js");
const body = src.slice(start, src.indexOf("\n}\n", start) + 3);

const failures = [];
const eq = (label, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) failures.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};

const calls = [];
let focused = 0;
const h = (tag, attrs = {}, ...kids) => ({ tag, attrs, kids, select: () => { focused++; } });
const fill = (form, ...kids) => calls.push(["fill", form, kids]);
const copyToClipboard = (text, after) => { calls.push(["copy", text]); after(); };
const showSecretOnce = new Function("h", "fill", "copyToClipboard", `${body}\nreturn showSecretOnce;`)(h, fill, copyToClipboard);
let reloaded = 0;
const form = {};
showSecretOnce(form, () => { reloaded++; }, "Intro text", "sekret", "Copy install command");
const [, gotForm, [intro, field, row]] = calls[0];
eq("secret form", gotForm, form);
eq("secret intro", intro.kids, ["Intro text"]);
eq("secret field", [field.attrs.readonly, field.attrs.value], [true, "sekret"]);
eq("secret labels", row.kids.map((b) => b.kids[0]), ["Copy install command", "Done"]);
let selected = 0;
field.attrs.onclick({ target: { select: () => { selected++; } } });
eq("field click selects", selected, 1);
row.kids[0].attrs.onclick();
eq("copy", calls[1], ["copy", "sekret"]);
eq("copy reselects field", focused, 1);
row.kids[1].attrs.onclick();
eq("done reloads", reloaded, 1);

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("ok");
