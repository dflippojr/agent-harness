// CSS class for a unified-diff line. Pure: no DOM.

export function approvalDiffClass(line) {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  return line.startsWith("-") ? "del" : "";
}

export function diffLineClass(line) {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+") && !line.startsWith("+++")) return "add";
  return line.startsWith("-") && !line.startsWith("---") ? "del" : "";
}

// File sections from a unified diff, including binary and unparsed files.
export function splitDiff(diff) {
  const files = [];
  let cur = null;
  for (const line of (diff || "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { name: m ? m[1] : line, lines: [] };
      files.push(cur);
    } else if (cur && !/^(index |new file mode|deleted file mode|--- |\+\+\+ )/.test(line)) {
      cur.lines.push(line);
    }
  }
  files.forEach((f) => { while (f.lines.length && !f.lines.at(-1)) f.lines.pop(); });
  return files;
}
