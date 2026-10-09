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

// The same ask on one line, for an Agents list row (#509): `$ cmd`, `tool path`, else the tool and its compact args.
export function approvalLine(a) {
  const args = a.args || {};
  if (a.tool === "run_shell" || a.tool === "Bash" || a.tool === "exec_command") return `$ ${args.command}`;
  if (a.tool === "git_clone") return `git clone ${args.url}`;
  if (a.tool === "restart_service") return `restart ${args.service}`;
  const target = args.path || args.file_path || args.url || args.service;
  if (target) return `${a.tool} ${target}`;
  const json = JSON.stringify(args);
  return json === "{}" ? a.tool : `${a.tool} ${json.length > 120 ? `${json.slice(0, 119)}…` : json}`;
}
