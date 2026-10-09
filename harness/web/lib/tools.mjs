// One-line text for tool calls and approval cards. Pure: no DOM.

// One-line summary of a tool call for its collapsed row.
export function toolSummaryText(fn, args) {
  if (fn.name === "run_shell") return args.command;
  if (fn.name === "git_clone" || fn.name === "web_fetch") return args.url;
  if (fn.name === "prometheus_query") return args.query;
  if (args.service) {
    const since = args.since ? ` since ${args.since}` : "";
    return `${args.service}${since}`;
  }
  if (args.path) {
    const line = args.start_line ? ` :${args.start_line}` : "";
    return `${args.path}${line}`;
  }
  return fn.arguments;
}

// What an approval card asks the owner to allow.
export function approvalWhat(a) {
  if (a.tool === "run_shell" || a.tool === "Bash" || a.tool === "exec_command") {
    const network = a.args.network ? "🌐 network · " : "";
    return `${network}$ ${a.args.command}`;
  }
  if (a.tool === "git_clone") return `git clone ${a.args.url}`;
  if (a.tool === "restart_service") return `restart ${a.args.service}`;
  return JSON.stringify(a.args, null, 2);
}

// Which icon a tool row shows (#508): a shell prompt, a web globe, a file, or a generic tool.
export function toolKind(name, args) {
  if (name === "run_shell" || name === "Bash" || name === "exec_command") return "shell";
  if (name === "web_fetch" || name === "web_search" || name === "git_clone") return "web";
  if (args && typeof args === "object" && args.path) return "file";
  return "other";
}

// Lines in a tool's text, ignoring one trailing newline; empty text has none.
export function lineCount(text) {
  if (!text) return 0;
  const body = text.endsWith("\n") ? text.slice(0, -1) : text;
  let n = 1;
  for (let i = body.indexOf("\n"); i !== -1; i = body.indexOf("\n", i + 1)) n++;
  return n;
}

// The first few lines for an expanded row's preview; the full text only goes to the viewer, so a 20 000-character
// output never lands in the transcript DOM.
export function previewText(text, maxLines = 8, maxChars = 1200) {
  let end = -1;
  for (let i = 0; i < maxLines; i++) {
    end = text.indexOf("\n", end + 1);
    if (end === -1) break;
  }
  let head = end === -1 ? text : text.slice(0, end);
  if (head.length > maxChars) head = head.slice(0, maxChars);
  return { text: head, more: head.length < text.trimEnd().length };
}

// The status pill once a tool returns: "ok · 4 s" or "error · <1 s".
export function resultState(ok, seconds) {
  const s = Number(seconds) || 0;
  const took = s < 1 ? "<1 s" : `${Math.round(s)} s`;
  return { text: `${ok ? "ok" : "error"} · ${took}`, kind: ok ? "ok" : "err" };
}
