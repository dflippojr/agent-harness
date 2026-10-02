// The md() link pattern (javascript:S8786) backtracked super-linearly on crafted input: a nested-bracket label
// and a run of "[x](http://" with no closing ")" (#239). lib/markdown.mjs uses a linear scanner (mdLinks/mdLinkAt);
// this proves it matches the old regex on every input that was previously linear, and stays fast on the rest.
import { mdInline } from "../harness/web/lib/markdown.mjs";

const fail = (msg) => { throw new Error(msg); };

// Kept here in isolation, never imported by the app, only to prove the new scanner matches it on linear inputs.
const OLD_LINK_RE = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g;
const oldLinks = (s) => s.replace(OLD_LINK_RE, '<a href="$2" target="_blank" rel="noopener">$1</a>');

const corpus = [
  "",
  "no links here",
  "[label](https://example.com/path)",
  "[label](http://example.com)",
  "[a [b](http://u)", // nested bracket in the label
  "[a[b](url)", // no http(s) prefix: stays plain text
  "[]()", // empty label and empty url
  "[x]()", // empty url
  "[](http://u)", // empty label
  "text [a](https://x) middle [b](http://y) end",
  "[a](https://x) [b](https://y) [c](https://z)", // adjacent links
  "[a](https://x)[b](https://y)", // touching links, no separator
  "\\[escaped\\](http://not-a-link)", // no escape handling: brackets are literal to this scanner, same as before
  "title [a](http://u \"a title\")", // a space before the ")" breaks the url class: no match, same as before
  "[a](http://u)extra](http://v)", // a link immediately followed by more bracket/paren text
  "[a](httpx://u)", // wrong protocol
  "ftp [a](ftp://u)", // unsupported protocol
  "[a](https://" + "u".repeat(2000) + ")", // very long url
  "[" + "a".repeat(2000) + "](http://u)", // very long label
  "[a](http://u)".repeat(500), // many adjacent legitimate links
  "[a\nb](http://u)", // newline inside label breaks the label class: no match, same as before
  "[[[[[x](http://u)", // several unmatched "[" before a real link
];

let bad = 0;
for (const input of corpus) {
  const want = oldLinks(input);
  const got = mdInline(input);
  if (got !== want) {
    bad++;
    console.log("MISMATCH", JSON.stringify(input), "\n  want", JSON.stringify(want), "\n  got ", JSON.stringify(got));
  }
}
if (bad) process.exit(1);

// Previously quadratic: many "[x](http://" starts, none with a closing ")", so every "[" used to rescan to the
// end of the string. Growth-based, not an absolute bound, so a loaded CI runner cannot flake it (#217). Each time
// is the median of several runs, so one GC or JIT pause on a shared runner can't pass for quadratic growth (#312).
const medianMs = (run, runs = 5) => {
  const times = [];
  for (let i = 0; i < runs; i++) {
    const t0 = performance.now();
    run();
    times.push(performance.now() - t0);
  }
  return times.sort((a, b) => a - b)[Math.floor(runs / 2)];
};
const timeUnclosedLinks = (n) => {
  const input = "[x](http://".repeat(n);
  return medianMs(() => mdInline(input));
};
timeUnclosedLinks(1000); // warm up
const baseline = Math.max(timeUnclosedLinks(1000), 5);
const scaled = timeUnclosedLinks(10000);
if (scaled > baseline * 40 + 300) fail(`mdInline() backtracks super-linearly on unclosed links: ${baseline.toFixed(1)}ms at 1000, ${scaled.toFixed(1)}ms at 10000`);

// Previously quadratic the other way: many nested-bracket labels with no closing "]" for the outer "[" either.
const timeUnclosedBrackets = (n) => {
  const input = "[a[b".repeat(n);
  return medianMs(() => mdInline(input));
};
timeUnclosedBrackets(1000); // warm up
const bracketBaseline = Math.max(timeUnclosedBrackets(1000), 5);
const bracketScaled = timeUnclosedBrackets(10000);
if (bracketScaled > bracketBaseline * 40 + 300) fail(`mdInline() backtracks super-linearly on unclosed brackets: ${bracketBaseline.toFixed(1)}ms at 1000, ${bracketScaled.toFixed(1)}ms at 10000`);

// Acceptance: no 100,000-character input takes more than a small fixed time, whichever pathological shape it is.
for (const make of [(n) => "[x](http://".repeat(Math.ceil(n / 11)), (n) => "[a[b".repeat(Math.ceil(n / 4))]) {
  const input = make(100000).slice(0, 100000);
  const elapsed = medianMs(() => mdInline(input), 3);
  if (elapsed > 1000) fail(`mdInline() on a 100,000-char input took ${elapsed.toFixed(1)}ms`);
}

console.log("ok");
