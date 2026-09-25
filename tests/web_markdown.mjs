// md() renders the same html after the Sonar refactor (#206): expected outputs in web_markdown_cases.json were
// recorded from the pre-refactor implementation. escapeHtml, snippetLanguage and linkQuotes are stubbed.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(p, "utf8").split("\r").join("");
const app = read(join(here, "..", "harness/web/app.js"));
const start = app.indexOf("const MD_BLOCK_MARK");
const end = app.indexOf("\n}\n", app.indexOf("function md(")) + 3;
if (start < 0 || end < 3) throw new Error("markdown block not found in app.js");

const stubs = `const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const snippetLanguage = (t) => ({ python: "python", js: "javascript" })[t] || "";
const linkQuotes = (h) => h;`;
const md = runInNewContext(`${stubs}\n${app.slice(start, end)}\nmd;`);

let bad = 0;
for (const [input, expected] of JSON.parse(read(join(here, "web_markdown_cases.json")))) {
  const got = md(input);
  if (got !== expected) { bad++; console.log("MISMATCH", JSON.stringify(input), "\n  want", JSON.stringify(expected), "\n  got ", JSON.stringify(got)); }
}
if (bad) process.exit(1);
console.log("ok");
