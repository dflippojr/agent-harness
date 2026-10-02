// Small, safe Markdown subset rendered to an HTML string. Pure: no DOM.
import { snippetLanguage } from "./snippets.mjs";

export const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const normalizeQuote = (text) => (text || "").toLowerCase().replace(/[^a-z0-9]/g, "");
const quoteParts = (q) => q.split(/\.\.\.|…/).map(normalizeQuote).filter((p) => p.length >= 12);
const quoteIn = (q, text) => {
  const parts = quoteParts(q);
  return parts.length > 0 && parts.every((p) => normalizeQuote(text).includes(p));
};
function quoteHref(url, quote) {
  const base = (url || "").split("#")[0];
  const snippet = quote.replace(/\s+/g, " ").trim().slice(0, 80);
  return `${base}#:~:text=${encodeURIComponent(snippet)}`;
}
function quoteLinks(answer, pages) {
  const links = {};
  const re = /["“]([^"”\n]{25,400})["”]/g;
  let match;
  while ((match = re.exec(answer || ""))) {
    const q = match[1];
    const page = (pages || []).find((p) => /^https?:\/\//i.test(p.url || "") && quoteIn(q, p.text));
    if (page) links[q] = quoteHref(page.url, q);
  }
  return links;
}
function linkQuotes(html, answer, pages) {
  const links = quoteLinks(answer, pages);
  const quotes = Object.keys(links).sort((a, b) => b.length - a.length);
  for (const q of quotes) {
    const escaped = escapeHtml(q);
    html = html.split(escaped).join(`<a class="quote-source" href="${escapeHtml(links[q])}" target="_blank" rel="noopener">${escaped}</a>`);
  }
  return html;
}

// Small, safe Markdown subset: everything is escaped first, then a few constructs are re-enabled.
// A fenced block in a language the snippet runner supports is marked so Chat can add its Run button.
const MD_BLOCK_MARK = "\u0000";   // brackets a fenced-block placeholder; escaped input never contains it as text we render
const MD_BLOCK_LINE = new RegExp(String.raw`^${MD_BLOCK_MARK}\d+${MD_BLOCK_MARK}$`);
const MD_BLOCK_REF = new RegExp(String.raw`${MD_BLOCK_MARK}(\d+)${MD_BLOCK_MARK}`, "g");
const MD_TABLE_ROW = /^\s*\|.*\|\s*$/;
const MD_LIST_ITEM = /^\s*([-*]|\d+\.) /;

const isMdTagChar = (ch) => /[\w+#-]/.test(ch);

// Replaces each ``` fence (escaped text in, language tag optional) with a placeholder; onBlock(tag, rest) makes the block's html.
function replaceFences(text, onBlock) {
  let out = "";
  let pos = 0;
  for (;;) {
    const open = text.indexOf("```", pos);
    const close = open < 0 ? -1 : text.indexOf("```", open + 3);
    if (close < 0) break;
    let tagEnd = open + 3;
    while (tagEnd < close && isMdTagChar(text[tagEnd])) tagEnd++;
    out += text.slice(pos, open) + onBlock(text.slice(open + 3, tagEnd), text.slice(tagEnd, close));
    pos = close + 3;
  }
  return out + text.slice(pos);
}

// table[i] = nearest index >= i where stop(char) is true, or -1 if none remains. Built once per mdLinks() call so
// every "[" can look up its own bounds in O(1) instead of rescanning the tail of the string.
function nextStopTable(s, stop) {
  const table = new Array(s.length + 1);
  table[s.length] = -1;
  for (let i = s.length - 1; i >= 0; i--) table[i] = stop(s[i]) ? i : table[i + 1];
  return table;
}

// Tries to match a link starting at s[open] === "[", using the same bounds as the regex this replaced: the label
// runs to the first "]" or newline, the url needs an http(s) prefix and runs to the first ")" or whitespace.
function mdLinkAt(s, open, closeBracket, closeParen) {
  const labelStart = open + 1;
  const bracket = closeBracket[labelStart];
  if (bracket < 0 || bracket === labelStart || s[bracket] !== "]" || s[bracket + 1] !== "(") return null;
  const protoStart = bracket + 2;
  const proto = s.startsWith("https://", protoStart) ? "https://" : s.startsWith("http://", protoStart) ? "http://" : null;
  if (!proto) return null;
  const urlStart = protoStart + proto.length;
  const paren = closeParen[urlStart];
  if (paren < 0 || paren === urlStart || s[paren] !== ")") return null;
  return { html: `<a href="${s.slice(protoStart, paren)}" target="_blank" rel="noopener">${s.slice(labelStart, bracket)}</a>`, end: paren + 1 };
}

// Replaces markdown links in one linear pass instead of the backtracking regex this replaced (S8786), which was
// quadratic both on nested-bracket labels and on a run of "[x](http://" with no closing ")" (#239).
function mdLinks(s) {
  if (!s.includes("[")) return s;
  const closeBracket = nextStopTable(s, (c) => c === "]" || c === "\n");
  const closeParen = nextStopTable(s, (c) => c === ")" || /\s/.test(c));
  let out = "";
  let pos = 0;
  for (let open = s.indexOf("[", pos); open >= 0; open = s.indexOf("[", pos)) {
    out += s.slice(pos, open);
    const link = mdLinkAt(s, open, closeBracket, closeParen);
    if (!link) { out += "["; pos = open + 1; continue; }
    out += link.html;
    pos = link.end;
  }
  return out + s.slice(pos);
}

export const mdInline = (s) => mdLinks(s
  .replace(/`([^`\n]+)`/g, "<code>$1</code>")
  .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
  .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>"));

// Each block reader returns [html, index of the last line it used].
function mdHeading(line, i) {
  const level = Math.min(6, line.match(/^#+/)[0].length + 2);
  return [`<h${level}>${mdInline(line.replace(/^#+ /, ""))}</h${level}>`, i];
}

function mdTable(lines, i) {
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => mdInline(c.trim()));
  let html = "<table><thead><tr>" + cells(lines[i]).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
  i += 2;
  while (i < lines.length && MD_TABLE_ROW.test(lines[i])) {
    html += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
    i++;
  }
  return [`<div class="md-table">${html}</tbody></table></div>`, i - 1];
}

function mdList(lines, i) {
  const ordered = /^\s*\d+\./.test(lines[i]);
  let html = ordered ? "<ol>" : "<ul>";
  while (i < lines.length && MD_LIST_ITEM.test(lines[i])) {
    html += `<li>${mdInline(lines[i].replace(MD_LIST_ITEM, ""))}</li>`;
    i++;
  }
  return [html + (ordered ? "</ol>" : "</ul>"), i - 1];
}

const isMdTableStart = (lines, i) => MD_TABLE_ROW.test(lines[i]) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1]);

function mdBlock(lines, i) {
  const line = lines[i];
  if (MD_BLOCK_LINE.test(line.trim())) return [line.trim(), i];
  if (/^#{1,6} /.test(line)) return mdHeading(line, i);
  if (isMdTableStart(lines, i)) return mdTable(lines, i);
  if (MD_LIST_ITEM.test(line)) return mdList(lines, i);
  if (/^&gt; ?/.test(line)) return [`<blockquote>${mdInline(line.replace(/^&gt; ?/, ""))}</blockquote>`, i];
  if (!line.trim()) return ["", i];
  return [`<p>${mdInline(line)}</p>`, i];
}

export function md(src, pages) {
  const blocks = [];
  const text = replaceFences(escapeHtml(src || ""), (tag, rest) => {
    const code = rest.replace(/^[^\S\n]*\n?/, "");
    const lang = snippetLanguage(tag);
    const langAttr = lang ? ` data-snippet-lang="${lang}"` : "";
    blocks.push(`<pre${langAttr}><code>${code.replace(/\n$/, "")}</code></pre>`);
    return `${MD_BLOCK_MARK}${blocks.length - 1}${MD_BLOCK_MARK}`;
  });
  const out = [];
  const lines = text.split("\n");
  let i = 0;
  while (i < lines.length) {
    const [html, last] = mdBlock(lines, i);
    out.push(html);
    i = last + 1;
  }
  return linkQuotes(out.join("\n").replace(MD_BLOCK_REF, (_, n) => blocks[Number(n)]), src, pages);
}
