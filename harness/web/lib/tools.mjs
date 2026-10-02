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
