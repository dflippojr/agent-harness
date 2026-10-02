// md() renders the same html after the Sonar refactor (#206): expected outputs in web_markdown_cases.json were
// recorded from the pre-refactor implementation. md() is imported from lib/markdown.mjs with its real escapeHtml,
// snippetLanguage and linkQuotes (no pages, so no quote links), so the "py" fence case expects the real language map.
import { readFileSync } from "node:fs";
import { md } from "../harness/web/lib/markdown.mjs";

const cases = JSON.parse(readFileSync(new URL("web_markdown_cases.json", import.meta.url), "utf8"));
let bad = 0;
for (const [input, expected] of cases) {
  const got = md(input);
  if (got !== expected) { bad++; console.log("MISMATCH", JSON.stringify(input), "\n  want", JSON.stringify(expected), "\n  got ", JSON.stringify(got)); }
}
if (bad) process.exit(1);
console.log("ok");
