// @ts-nocheck
// Owner Actions page (#258): Resources (GPU hold, local model, diagnostics; #311), Claude Remote Control and Disk tabs.
// Household members, maintenance, runner updates and Remote Control folder trust and discovery are daemon
// administration, in the Hub and the CLI (#545). The shell (DOM builder, api, router, header) is injected by app.js so
// this module imports under plain Node and never reaches into another page.
import { ago, pluralize, gpuText } from "../lib/format.mjs";
import { TARGET_LABEL } from "../lib/targets.mjs";
import { lastUpdateText, compatibilityText, lastSeenText } from "../lib/settings-text.mjs";
import { adminPointer } from "../lib/widgets.mjs";
import * as sheets from "../lib/sheet.mjs";
import { refreshingSettingsApi } from "./profile.mjs";

export function mountActions({ $app, h, fill, append, api, setHeader, toast, go, isGuest, onLeave, progressBar,
  confirmSheet = sheets.confirmSheet }) {
  api = refreshingSettingsApi(api);
  const ACTION_TABS = [
    ["resources", "Resources"],
    ["remote-control", "Claude Remote Control"],
    ["disk", "Disk"],
  ];

  async function viewActions(tab) {
    const selected = ACTION_TABS.some(([id]) => id === tab) ? tab : "resources";
    if (tab !== selected) { go("#/actions/resources", true); return; }  // also old #/actions/gpu and accounts bookmarks
    setHeader("agents", "Actions", { page: true });
    const tabs = h("div", { class: "tabs resources-tabs", role: "tablist", "aria-label": "Server settings" },
      ACTION_TABS.map(([id, label]) => h("button", {
        type: "button", role: "tab", class: id === selected ? "on" : "",
        "aria-selected": id === selected ? "true" : "false",
        onclick: () => go(`#/actions/${id}`),
      }, label)));
    const panels = { resources: resourcesPanel, "remote-control": remoteControlCard, disk: diskCard };
    append($app, tabs, panels[selected]());
  }

  function backupLine(b) {
    if (!b?.enabled) return null;
    const failed = b.error && (b.error_at || 0) > (b.ok_at || 0);
    return h("p", { class: `small${failed ? " bad" : ""}` },
      b.ok_at ? `Backup ${ago(b.ok_at)} (${Math.max(1, Math.round(b.bytes / 2 ** 20))} MB) in ${b.dir}` : "No backup yet",
      failed ? ` · last attempt failed: ${b.error}` : "");
  }

  function imageArchiveBlock(a) {
    if (!a?.enabled) return null;
    const count = Number(a.archived || 0);
    const bytes = Number(a.bytes || 0);
    const warning = a.free_space_warning || (a.errors ? pluralize(a.errors, "image archive error") : "");
    return h("div", {},
      h("p", { class: `small${warning ? " bad" : ""}` },
        h("strong", {}, "Image archive"), " ",
        `${count} image${count === 1 ? "" : "s"} · ${Math.round(bytes / 2 ** 20)} MB · ${a.path}`),
      a.last_reconciliation ? h("p", { class: "muted small" },
        `Reconciled ${ago(a.last_reconciliation)} · ${a.missing || 0} missing · ${a.errors || 0} errors`) : null,
      warning ? h("p", { class: "note bad" }, warning) : null);
  }

  // Actions -> Resources (#311): the GPU hold, loading/unloading the local model, and one diagnostics reading.
  function resourcesPanel() {
    const model = modelActionRow();
    return h("div", {},
      h("div", { class: "card settings-list" }, gpuActionRow(model.render), model.el),
      diagnosticsCard());
  }

  const MODEL_SHOWN = {
    loaded: "Loaded", unloaded: "Unloaded; it loads when a task needs it", waking: "Loading (about a minute)",
    paused: "Unloaded while the GPU is held", disabled: "Local model disabled",
  };

  function clock(epochSeconds) {
    return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function modelStateText(m) {
    if (!m) return "Checking…";
    if (m.pinned_until && m.state !== "paused") return `Loaded until ${clock(m.pinned_until)}`;
    return MODEL_SHOWN[m.state] || m.state;
  }

  function modelActionRow() {
    const status = h("div", { class: "muted small" }, "Checking…");
    const keepFor = h("select", { disabled: isGuest(), "aria-label": "Keep the model loaded for" },
      [["1800", "30 minutes"], ["3600", "1 hour"], ["10800", "3 hours"], ["28800", "8 hours"]].map(([v, label]) =>
        h("option", { value: v }, label)));
    let defaulted = false;
    const loadBtn = h("button", { class: "btn small", type: "button", disabled: isGuest() }, "Load local model now");
    const unloadBtn = h("button", { class: "btn small secondary", type: "button", disabled: isGuest() }, "Unload now");
    const render = (g) => {
      const m = g.model;
      status.textContent = modelStateText(m);
      if (!defaulted && g.load_now_default_minutes) {
        const want = String(g.load_now_default_minutes * 60);
        if (![...keepFor.options].some((o) => o.value === want)) {
          keepFor.append(h("option", { value: want }, `${g.load_now_default_minutes} minutes`));
        }
        keepFor.value = want;
        defaulted = true;
      }
      const held = g.manual || (g.state && g.state !== "clear");
      const off = isGuest() || !g.enabled || held || m?.state === "disabled";
      loadBtn.disabled = off;
      unloadBtn.disabled = off || m?.state === "unloaded";
    };
    const post = async (action, body) => {
      const r = await api(`/resources/${action}`, { method: "POST", body });
      render(r);
      return r;
    };
    const LOADING = "Loading the local model (about a minute)";
    loadBtn.addEventListener("click", async () => {
      const body = { duration_seconds: Number(keepFor.value) };
      loadBtn.disabled = true;
      try {
        await post("load", body);
        toast(LOADING);
      } catch (e) {
        const lowMemory = /low memory/i.test(e.message);
        if (!lowMemory) toast(e.message);
        else if (await confirmSheet({ title: "Load the model anyway?", message: `${e.message.replace(/\. Send force.*$/, "")}.`,
          confirmLabel: "Load anyway", destructive: true })) {
          try { await post("load", { ...body, force: true }); toast(LOADING); } catch (e2) { toast(e2.message); }
        }
      } finally { loadBtn.disabled = false; }
    });
    unloadBtn.addEventListener("click", async () => {
      try { await post("unload"); toast("Local model unloaded"); } catch (e) { toast(e.message); }
    });
    const el = h("div", { class: "action-item" },
      h("div", { class: "action-row" }, h("div", {}, h("strong", {}, "Local model"), status), unloadBtn),
      h("label", { class: "action-subitem" }, h("span", {}, "Keep loaded for:"), keepFor, loadBtn),
      h("div", { class: "muted small" }, "Ending a GPU hold doesn't load the model; a task, an endpoint request or "
        + "this button does. Idle unload is suspended while it's kept loaded."));
    return { el, render };
  }

  function pct(n) { return n == null ? "n/a" : `${Math.round(n)}%`; }
  function gib(n) { return n == null ? "n/a" : gbLabel(n / 2 ** 30); }

  function diagnosticsCard() {
    const asOf = h("span", { class: "muted small" }, "");
    const body = h("div", {}, h("p", { class: "muted small" }, "Reading…"));
    const refresh = h("button", {
      class: "btn small secondary", type: "button", title: "Refresh", "aria-label": "Refresh diagnostics",
      onclick: () => void load(),
    }, "↻");
    const meter = (name, used, total, line, extra) => {
      const ok = Number.isFinite(used) && Number.isFinite(total) && total > 0;
      return h("div", { class: "disk-device" },
        h("strong", {}, name),
        ok ? progressBar(Math.max(0, Math.min(1, used / total))) : null,
        h("div", { class: "disk-meter" }, h("span", {}, line), ok ? h("span", { class: "muted" }, `of ${gib(total)}`) : null),
        extra);
    };
    const fact = (label, value) => h("p", { class: "small" }, h("strong", {}, label), " ", value);
    const load = async () => {
      refresh.disabled = true;
      try {
        const d = await api("/resources/diagnostics");
        const r = d.ram || {};
        const share = r.harness || {};
        const vram = d.vram || {};
        const guard = d.guard || {};
        const reasons = (guard.reasons || []).map((x) => x.detail);
        if (r.low) reasons.push("RAM under the threshold: new model, worker and image work waits");
        const ramUsed = r.total_bytes != null && r.available_bytes != null ? r.total_bytes - r.available_bytes : null;
        fill(body,
          meter("VRAM", vram.used_bytes, vram.total_bytes, vram.used_bytes == null ? "n/a" : `${gib(vram.used_bytes)} used`,
            h("div", { class: "disk-facts" },
              fact("Held by", (vram.holders || []).join(", ") || "nothing the harness runs"),
              fact("GPU load", pct(d.gpu_load)))),
          meter("RAM", ramUsed, r.total_bytes, `${gib(r.available_bytes)} available`,
            h("div", { class: "disk-facts" },
              fact("Commit", r.commit_bytes == null ? "n/a" : `${gib(r.commit_bytes)} of ${gib(r.commit_limit_bytes)}`),
              fact("Harness", `daemon ${gib(share.daemon_bytes)} · llama-server ${gib(share.llama_server_bytes)}`
                + ` · containers ${gib(share.containers_bytes)}`),
              fact("Threshold", r.threshold_bytes ? `${gib(r.threshold_bytes)} available${r.low ? " (below it now)" : ""}` : "off"),
              fact("CPU load", pct(d.cpu_load)))),
          h("div", { class: "disk-facts" },
            fact("Model", modelStateText(d.model)),
            fact("Guard", `${guard.enabled === false ? "disabled" : guard.state}${reasons.length ? ` · ${reasons.join(", ")}` : ""}`)));
        asOf.textContent = `as of ${clock(d.as_of)}`;
      } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
      finally { refresh.disabled = false; }
    };
    void load();  // one reading when the tab opens, then only on refresh: no background polling
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", {}, "Diagnostics"), h("span", { class: "spacer" }), asOf, refresh),
      body);
  }

  function gpuActionRow(onStatus = () => {}) {
    const status = h("div", { class: "muted small" }, "Checking…");
    const toggle = h("input", { class: "switch", type: "checkbox", role: "switch", "aria-label": "GPU hold", disabled: isGuest() });
    const duration = h("select", { disabled: isGuest(), "aria-label": "GPU hold duration" },
      h("option", { value: "" }, "Until I turn it off"),
      h("option", { value: "1800" }, "30 minutes"),
      h("option", { value: "3600" }, "1 hour"),
      h("option", { value: "10800" }, "3 hours"));
    const durationRow = h("label", { class: "action-subitem disabled" },
      h("span", {}, "Duration:"), duration);
    const act = async (action) => {
      const seconds = duration.value ? Number(duration.value) : null;
      const body = action === "pause" ? { duration_seconds: seconds } : undefined;
      try { render(await api(`/resources/${action}`, { method: "POST", body })); } catch (e) { toast(e.message); }
      setTimeout(load, 1500);
    };
    const render = (g) => {
      onStatus(g);
      if (!g.enabled) {
        toggle.disabled = true;
        duration.disabled = true;
        durationRow.classList.add("disabled");
        status.textContent = "Resource guard disabled";
        return;
      }
      const now = g.signals.map((s) => s.detail);
      toggle.checked = g.manual;
      if (g.manual && g.manual_duration_seconds) duration.value = String(g.manual_duration_seconds);
      duration.disabled = isGuest() || !g.manual;
      durationRow.classList.toggle("disabled", duration.disabled);
      const automatic = !g.manual && g.state !== "clear" ? ` · ${gpuText(g)}` : "";
      const ignored = g.override ? " (ignored)" : "";
      const using = now.length ? ` · ${now.join(", ")}${ignored}` : "";
      status.textContent = `${g.manual ? gpuText(g) : "Local models available"}${automatic}${using}`;
    };
    const load = async () => { try { render(await api("/resources")); } catch (e) { status.textContent = e.message; status.classList.add("bad"); } };
    toggle.addEventListener("change", () => {
      duration.disabled = isGuest() || !toggle.checked;
      durationRow.classList.toggle("disabled", duration.disabled);
      void act(toggle.checked ? "pause" : "resume");
    });
    duration.addEventListener("change", () => { if (toggle.checked) void act("pause"); });
    void load();
    const timer = setInterval(load, 5000);
    onLeave(() => clearInterval(timer));
    return h("div", { class: "action-item" },
      h("div", { class: "action-row" }, h("div", {}, h("strong", {}, "GPU hold"), status), toggle),
      durationRow,
      isGuest() ? h("div", { class: "muted small" }, "Demo access cannot change GPU hold.") : null);
  }

  function remoteControlCard() {
    const body = h("div", {}, h("p", { class: "muted small" }, "Checking…"));
    let busy = "";
    const act = async (project, stop) => {
      busy = project;
      void load();
      try {
        const name = encodeURIComponent(project);
        const r = await api(stop ? `/remote-control/${name}/stop` : `/remote-control/${name}`, { method: "POST" });
        if (stop) toast(`Stopped Remote Control for ${project}`);
        else toast(r.already_running ? "Already running" : "Remote Control is ready");
      } catch (e) { toast(e.message); }
      busy = "";
      void load();
    };
    const rcButton = (p) => {
      if (p.running) return h("button", { class: "btn", disabled: !!busy, onclick: () => act(p.project, true) }, "Stop");
      if (p.invalid) return h("span", { class: "note bad" }, `Invalid folder: ${p.invalid}`);
      if (!p.trusted) return null;
      return h("button", { class: "btn", disabled: !!busy, onclick: () => act(p.project, false) }, "Start");
    };
    const rcActions = (p) => h("div", { class: "row" },
      p.running && p.pairing_url ? h("a", { class: "btn", href: p.pairing_url, target: "_blank", rel: "noopener" }, "Open in Claude") : null,
      rcButton(p));
    const row = (p) => {
      const remoteControlState = (p) => {
        if (p.running) {
          const sessions = p.active_sessions ? ` · ${pluralize(p.active_sessions, "session")}` : "";
          return `running${sessions} · started ${ago(p.started_at)}`;
        }
        if (!p.trusted) return p.trust_prompt_open ? "trust window open on the tower" : "needs one-time Claude workspace trust";
        return "stopped";
      };
      const state = busy === p.project ? h("span", { class: "dots" }, "working")
        : remoteControlState(p);
      return h("div", { class: "rc-row" },
        h("p", {}, h("strong", {}, p.project), " ", h("span", { class: `muted small${!p.running && !p.trusted ? " bad" : ""}` }, state)),
        h("p", { class: "muted small" }, p.path),
        !p.trusted && p.trust_prompt_open ? h("p", { class: "note small" }, "On the tower, review the folder in Claude and accept its trust prompt. This page will notice automatically.") : null,
        isGuest() ? h("p", { class: "muted small" }, "Demo access cannot start or stop Remote Control.") : rcActions(p));
    };
    const load = async () => {
      try {
        const r = await api("/remote-control");
        if (!r.enabled) return fill(body, h("p", { class: "muted small" }, "Disabled in config/harness.yaml (remote_control)."));
        fill(body,
          h("p", { class: "muted small" }, "Start Claude Code in a project folder and continue in the Claude app. These sessions use your Claude subscription, not the harness."),
          h("p", { class: "muted small" }, "Only tower projects with a local folder appear. Homelab and scratch have none, so they are omitted."),
          r.projects.length ? r.projects.map(row) : h("p", { class: "muted small" }, "No tower projects with a local folder."),
          isGuest() ? null : adminPointer(h));
      } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
    };
    void load();
    const timer = setInterval(() => { if (!busy) void load(); }, 3000);
    onLeave(() => clearInterval(timer));
    return h("div", { class: "card" }, h("h3", {}, "Claude Remote Control"), body);
  }

  function gbLabel(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return String(n);
    return `${v >= 10 ? Math.round(v) : v.toFixed(1)} GB`;
  }

  function diskCard() {
    const body = h("div", {}, h("p", { class: "muted small" }, "Measuring…"));
    const load = async () => {
      try {
        const u = await api("/maintenance");
        const mb = (n) => (n < 1 ? "<1 MB" : `${n} MB`);
        const device = (name, free, total, extra, { offline = false } = {}) => {
          const freeN = Number(free);
          const totalN = Number(total);
          const haveMeter = !offline && Number.isFinite(freeN) && Number.isFinite(totalN) && totalN > 0;
          const used = haveMeter ? Math.max(0, Math.min(1, (totalN - freeN) / totalN)) : null;
          return h("div", { class: "disk-device" },
            h("strong", {}, name),
            haveMeter ? progressBar(used) : null,
            h("div", { class: "disk-meter" },
              h("span", {}, offline ? "Offline" : `${gbLabel(free)} free`),
              !offline && Number.isFinite(totalN) ? h("span", { class: "muted" }, `of ${gbLabel(total)}`) : null),
            extra);
        };
        const fact = (label, value) => h("p", { class: "small" }, h("strong", {}, label), " ", value);
        const top = u.workspaces.slice(0, 5);
        const towerExtra = h("div", { class: "disk-facts" },
          fact("Workspaces", `${mb(u.workspaces_mb)} · ${u.workspaces.length} session${u.workspaces.length === 1 ? "" : "s"} · ${u.quota_mb} MB quota each`),
          u.checkpoints ? fact("Checkpoints", `${mb(u.checkpoints_mb)} · ${u.checkpoints.length} session${u.checkpoints.length === 1 ? "" : "s"} · rewind/fork history, removed with the workspace`) : null,
          fact("Sandboxes", `${u.containers.length} container${u.containers.length === 1 ? "" : "s"}`),
          backupLine(u.backup),
          isGuest() ? null : imageArchiveBlock(u.image_archive),
          top.length ? h("p", { class: "muted small", style: "margin-top:10px" }, "Largest workspaces") : null,
          top.length ? h("ul", { class: "small disk-workspaces" }, top.map((w) => h("li", {},
            h("a", { href: `#/s/${w.session}/info` }, h("span", {}, w.session), h("span", { class: "muted" }, mb(w.mb)))))) : null);
        fill(body,
          device("Tower", u.free_gb, u.total_gb, towerExtra),
          (u.runners || []).map((r) => {
            const online = !!r.online;
            const compatibility = r.compatibility || {};
            const extra = h("div", { class: "disk-facts" },
              fact("Runner", online
                ? `${r.info.version} · protocol ${r.info.protocol ?? "not reported"} · macOS ${r.info.macos}`
                : lastSeenText(r)),
              fact("Compatibility", compatibilityText(compatibility)),
              r.last_update ? fact("Last update", lastUpdateText(r.last_update)) : null,
              h("p", { class: "muted small" }, `On the Mac, run: ${r.manual_update || "harness update"}`));
            return device(TARGET_LABEL[r.name] || r.name,
              online ? r.info.free_gb : "—",
              online && r.info.total_gb != null ? r.info.total_gb : null,
              extra, { offline: !online });
          }));
      } catch (e) { fill(body, h("p", { class: "note bad" }, e.message)); }
    };
    void load();
    return h("div", {},
      h("div", { class: "card" }, body),
      isGuest() ? null : adminPointer(h));
  }

  return { viewActions, remoteControlCard };
}
