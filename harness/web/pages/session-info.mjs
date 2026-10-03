// Session detail -> Info tab (#258): read-only facts about one session plus the transcript download. The shell
// (DOM builder, copyBox, download) is injected by app.js so this module imports under plain Node and never
// reaches into another page.
import { traceInfo } from "../lib/trace.mjs";

export function mountSessionInfo({ $app, h, append, copyBox, downloadDaemonFile }) {
  function viewInfo(s) {
    const t = s.totals || {};
    const rows = [
      ["Title", s.title], ["Session", s.id], ["Status", s.stop_reason ? `${s.status} (${s.stop_reason})` : s.status],
      ["Project", s.project], ["Target", s.target], ["Backend", s.backend || "local"], ["Model", s.model],
      ["Created", new Date(s.created_at * 1000).toLocaleString()], ["Updated", new Date(s.updated_at * 1000).toLocaleString()],
      ["Model turns", t.turns || 0], ["Prompt tokens", t.prompt_tokens || 0], ["Completion tokens", t.completion_tokens || 0],
      ["Workspace", s.workspace_removed ? `${s.workspace} (removed)` : s.workspace],
    ];
    if (s.branch) rows.push(["Branch", s.base_branch ? `${s.branch} from ${s.base_branch}` : s.branch], ["Review", s.review || "pending"]);
    const frozen = s.skills || [];
    if (frozen.length) {
      rows.push(["Skills", frozen.map((sk) => `${sk.slug} v${sk.version} (${(sk.content_hash || "").slice(0, 12)})`).join(", ")]);
    }
    const trace = traceInfo(s);
    const traceRow = trace && h("div", { class: "row", style: "justify-content:space-between;padding:4px 0" },
      h("span", { class: "muted" }, "Trace"),
      trace.url ? h("a", { href: trace.url, target: "_blank", rel: "noopener", style: "overflow-wrap:anywhere;text-align:right" }, trace.id)
        : copyBox(trace.id));
    append($app, h("div", { class: "card" }, rows.map(([k, v]) => h("div", { class: "row", style: "justify-content:space-between;padding:4px 0" },
      h("span", { class: "muted" }, k), h("span", { style: "overflow-wrap:anywhere;text-align:right" }, String(v)))), traceRow),
    h("button", { class: "btn", onclick: () => downloadDaemonFile(`/sessions/${s.id}/transcript`, `${s.id}.md`) },
      "Download Markdown transcript"));
  }
  return { viewInfo };
}
