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
