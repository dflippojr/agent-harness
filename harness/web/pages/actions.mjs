// Owner Actions page (#258): Resources (GPU hold, local model, diagnostics; #311), household accounts, Claude Remote Control and disk/maintenance tabs. The shell
// (DOM builder, api, router, header) is injected by app.js so this module imports under plain Node and never
// reaches into another page.
import { ago, pluralize, fmtBytes, gpuText } from "../lib/format.mjs";
import { TARGET_LABEL } from "../lib/targets.mjs";
import { lastUpdateText, compatibilityText, lastSeenText } from "../lib/settings-text.mjs";
import * as sheets from "../lib/sheet.mjs";

export function mountActions({ $app, h, fill, append, api, setHeader, toast, go, isGuest, isMember, onLeave, copyBox, progressBar,
  confirmSheet = sheets.confirmSheet, promptSheet = sheets.promptSheet, formSheet = sheets.formSheet }) {
  // Issue #63: the owner switches member GitHub sign-in on/off and can erase a member's credential. The owner
  // never sees repository URLs, GitHub usernames, or codes, and cannot connect, test, or use the credential.
  function githubOwnerCard(view, rerender) {
    if (!view || !view.configured) {
      return h("div", { class: "card" }, h("h3", {}, "Member GitHub sign-in"),
        h("p", { class: "muted small" }, "Not set up. Configure github_member_auth (Git Credential Manager path and secure store) in harness.yaml to offer it."));
    }
    const pf = view.preflight;
    const toggle = async () => {
      const next = !view.enabled;
      if (!(await confirmSheet(next
        ? { title: "Turn on member GitHub sign-in?", message: "Household members can connect their own GitHub accounts.", confirmLabel: "Turn on" }
        : { title: "Turn off member GitHub sign-in?", message: "Connection attempts and running GitHub operations stop now; stored credentials are not erased.",
          confirmLabel: "Turn off", destructive: true }))) return;
      try { await api("/github-member-auth", { method: "PUT", surface: "admin", body: { enabled: next } }); await rerender(); }
      catch (e) { toast(e.message, 6000); }
    };
    return h("div", { class: "card" }, h("h3", {}, "Member GitHub sign-in"),
      h("p", { class: "muted small" }, view.enabled ? "On." : "Off."),
      pf && !pf.ok ? h("p", { class: "note bad" }, pf.message || "The secure store check failed.") : null,
      h("p", { class: "muted small" }, view.scopes_note || ""),
      h("div", { class: "row" }, h("button", { class: "btn small", type: "button", onclick: toggle },
        view.enabled ? "Turn off" : "Turn on")));
  }

  async function accountsCard() {
    const wrap = h("div");
    const render = async () => {
      let rows = [];
      let github = null;
      let google = null;
      try {
        [rows, github, google] = await Promise.all([api("/accounts", { surface: "admin" }),
          api("/github-member-auth", { surface: "admin" }).catch(() => null),
          api("/google-signin", { surface: "admin" }).catch(() => null)]);
      } catch (e) { fill(wrap, h("p", { class: "note bad" }, e.message)); return; }
      const githubState = Object.fromEntries((github?.members || []).map((row) => [row.user_id, row]));
      const login = h("input", { type: "email", placeholder: "member@example.com", required: true });
      const name = h("input", { type: "text", placeholder: "Display name", required: true, maxlength: "80" });
      const create = h("button", { class: "btn primary", type: "submit" }, "Create member");
      fill(wrap,
        h("p", { class: "muted small" }, "Household members authenticate with the exact Tailscale login you enter. The machine owner can still read local storage; this page prevents accidental API and UI cross-account access."),
        h("form", { class: "card", onsubmit: async (e) => {
          e.preventDefault();
          create.disabled = true;
          try {
            await api("/accounts", { method: "POST", surface: "admin", body: {
              login: login.value, display_name: name.value,
            } });
            toast("Member created");
            await render();
          } catch (err) { toast(err.message, 5000); create.disabled = false; }
        } }, h("h3", {}, "New member"), login, name, h("div", { class: "row", style: "margin-top:12px" }, create)),
        githubOwnerCard(github, render),
        googleOwnerCard(google),
        rows.length ? rows.map((a) => {
          const gh = githubState[a.user_id];
          const resetGithub = async () => {
            if (!(await confirmSheet({ title: `Erase ${a.display_name}'s GitHub credential?`,
              message: "This only erases it; they can connect again themselves.", confirmLabel: "Erase", destructive: true }))) return;
            try {
              await api(`/accounts/${a.user_id}/github-connection/reset`, { method: "POST", surface: "admin", body: { confirm: true } });
              toast("GitHub credential erased");
              await render();
            } catch (err) { toast(err.message, 6000); }
          };
          const patch = async (body, ask) => {
            if (ask && !(await confirmSheet(ask))) return;
            try {
              await api(`/accounts/${a.user_id}`, { method: "PATCH", surface: "admin", body });
              await render();
            } catch (err) { toast(err.message, 5000); }
          };
          return h("div", { class: "card" },
            h("h3", {}, a.display_name),
            h("p", { class: "muted small" }, a.login),
            h("p", { class: "muted small" }, `id ${a.account_hint} · ${a.enabled ? "enabled" : "disabled"}`),
            h("p", { class: "muted small" }, `${fmtBytes(a.disk_used_bytes)} / ${fmtBytes(a.disk_quota_bytes)} · ${a.running} running · ${a.queued} queued`),
            a.last_activity_at ? h("p", { class: "muted small" }, `Last activity ${ago(a.last_activity_at)}`) : null,
            gh && github?.configured ? h("p", { class: "muted small" },
              `GitHub: ${gh.status.replace("_", " ")}${gh.last_used_at ? ` · last used ${ago(gh.last_used_at)}` : ""}`) : null,
            h("div", { class: "row", style: "flex-wrap:wrap;gap:8px" },
              h("button", { class: "btn small", type: "button", onclick: async () => {
                const next = (await promptSheet({ title: `Rename ${a.display_name}`, label: "Display name", value: a.display_name,
                  confirmLabel: "Rename", validate: sheets.required("a display name") }))?.trim();
                if (next && next !== a.display_name) void patch({ display_name: next });
              } }, "Rename"),
              h("button", { class: "btn small", type: "button", onclick: async () => {
                const next = (await promptSheet({ title: `Rebind ${a.display_name}'s login`, label: "New Tailscale login", value: a.login,
                  message: "The old login stops working immediately.", confirmLabel: "Rebind", validate: sheets.required("a Tailscale login") }))?.trim();
                if (next && next !== a.login) void patch({ login: next });
              } }, "Rebind login"),
              h("button", { class: "btn small", type: "button", onclick: async () => {
                const next = await promptSheet({ title: `Disk quota for ${a.display_name}`, label: "Disk quota in GiB",
                  value: String(Math.round(a.disk_quota_bytes / 2 ** 30)), inputmode: "decimal",
                  validate: (v) => (Number(v) > 0 ? "" : "Enter a size in GiB, more than 0.") });
                if (next) void patch({ disk_quota_bytes: Math.round(Number(next) * 2 ** 30) });
              } }, "Quota"),
              h("button", { class: "btn small", type: "button", onclick: async () => {
                const limits = await formSheet({ title: `Concurrency for ${a.display_name}`, fields: [
                  { name: "running", label: "Max running sessions", value: String(a.max_running), inputmode: "numeric", validate: sheets.wholeNumber(0) },
                  { name: "queued", label: "Max queued sessions", value: String(a.max_queued), inputmode: "numeric", validate: sheets.wholeNumber(0) },
                ] });
                if (limits) void patch({ max_running: Number(limits.running), max_queued: Number(limits.queued) });
              } }, "Concurrency"),
              h("button", { class: "btn small", type: "button", onclick: () => patch(
                { enabled: !a.enabled },
                a.enabled
                  ? { title: `Disable ${a.display_name}?`, message: "Running work will be cancelled.", confirmLabel: "Disable", destructive: true }
                  : { title: `Re-enable ${a.display_name}?`, confirmLabel: "Re-enable" },
              ) }, a.enabled ? "Disable" : "Re-enable"),
              gh && github?.configured && gh.status !== "disconnected"
                ? h("button", { class: "btn small", type: "button", onclick: resetGithub }, "Erase GitHub credential") : null,
              ...googleMemberButtons(a, google, render, wrap)),
            googleMemberLine(a, google));
        }) : h("p", { class: "muted small" }, "No household members yet."));
    };
    await render();
    return wrap;
  }

  // Issue #64: coarse Google sign-in state for the owner. Never tokens, `sub`, claims, or invitation hashes.
  function googleOwnerCard(view) {
    if (!view || !view.enabled) return null;
    const problems = view.preflight?.problems || [];
    return h("div", { class: "card" },
      h("h3", {}, "Google sign-in"),
      h("p", { class: "muted small" }, view.ready
        ? "Ready. Members on admitted devices can sign in with their linked Google account."
        : "Not ready. Google sign-in stays off until these are fixed:"),
      problems.length ? h("ul", { class: "muted small" }, problems.map((p) => h("li", {}, p))) : null,
      view.redirect_uri ? h("p", { class: "muted small" }, "Authorized redirect URI for Google Cloud Console:") : null,
      view.redirect_uri ? copyBox(view.redirect_uri) : null);
  }

  function googleMemberLine(a, google) {
    const g = a.google;
    if (!g || !google?.enabled) return null;
    const parts = [g.linked ? `Google: ${g.email}` : "Google: not linked"];
    if (g.last_sign_in_at) parts.push(`last sign-in ${ago(g.last_sign_in_at)}`);
    if (g.active_web_sessions) parts.push(`${g.active_web_sessions} Web session${g.active_web_sessions === 1 ? "" : "s"}`);
    if (g.invitation_expires_at) parts.push(`link code pending until ${new Date(g.invitation_expires_at * 1000).toLocaleTimeString()}`);
    return h("p", { class: "muted small" }, parts.join(" · "));
  }

  function googleMemberButtons(a, google, rerender, wrap) {
    const g = a.google;
    if (!g || !google?.enabled) return [];
    const call = async (path, method, body, ask, done) => {
      if (ask && !(await confirmSheet(ask))) return;
      try {
        const out = await api(`/accounts/${a.user_id}/google${path}`, { method, surface: "admin", body });
        if (done) done(out);
        else await rerender();
      } catch (err) { toast(err.message, 6000); }
    };
    // Shown once, in this page only: never in a URL, QR code, or the audit log.
    const showCode = (out) => fill(wrap, h("div", { class: "card" },
      h("h3", {}, `Link code for ${a.display_name}`),
      h("p", { class: "muted small" }, `Give this to ${a.display_name} privately. It works once, until ${new Date(out.expires_at * 1000).toLocaleTimeString()}, and is not shown again.`),
      copyBox(out.code),
      h("div", { class: "row" }, h("button", { class: "btn small", type: "button", onclick: () => rerender() }, "Done"))));
    const buttons = [];
    if (!g.linked && a.enabled) {
      buttons.push(h("button", { class: "btn small", type: "button", onclick: () => call("/invitation", "POST", undefined,
        g.invitation_expires_at ? { title: "Replace the pending link code?", message: "The old code stops working.", confirmLabel: "Replace" } : null,
        showCode) },
      "Google link code"));
    }
    if (g.invitation_expires_at) {
      buttons.push(h("button", { class: "btn small", type: "button", onclick: () => call("/invitation", "DELETE") }, "Cancel link code"));
    }
    if (g.active_web_sessions) {
      buttons.push(h("button", { class: "btn small", type: "button", onclick: () => call("/revoke-sessions", "POST", undefined,
        { title: `Sign ${a.display_name} out of every Google Web session?`, confirmLabel: "Sign out", destructive: true }) },
      "Revoke Web sessions"));
    }
    if (g.linked) {
      buttons.push(h("button", { class: "btn small", type: "button", onclick: () => call("", "DELETE", { confirm: true },
        { title: `Unlink Google from ${a.display_name}?`, message: "Their data and Tailscale login stay; their Google Web sessions end.",
          confirmLabel: "Unlink", destructive: true }) }, "Unlink Google"));
    }
    return buttons;
  }

  const ACTION_TABS = [
    ["resources", "Resources"],
    ["accounts", "Accounts"],
    ["remote-control", "Claude Remote Control"],
    ["disk", "Disk"],
  ];

  async function viewActions(tab) {
    const selected = ACTION_TABS.some(([id]) => id === tab) ? tab : "resources";
    if (tab !== selected) { go("#/actions/resources", true); return; }  // also old #/actions/gpu bookmarks
    setHeader("agents", "Actions", { page: true });
    const tabs = h("div", { class: "tabs resources-tabs", role: "tablist", "aria-label": "Server settings" },
      ACTION_TABS.map(([id, label]) => h("button", {
        type: "button", role: "tab", class: id === selected ? "on" : "",
        "aria-selected": id === selected ? "true" : "false",
        onclick: () => go(`#/actions/${id}`),
      }, label)));
    let panel;
    if (selected === "resources") panel = resourcesPanel();
    else if (selected === "accounts") panel = await accountsCard();
    else panel = selected === "remote-control" ? remoteControlCard() : diskCard();
    append($app, tabs, panel);
  }

  function backupLine(b) {
    if (!b?.enabled) return null;
    const failed = b.error && (b.error_at || 0) > (b.ok_at || 0);
    return h("p", { class: `small${failed ? " bad" : ""}` },
      b.ok_at ? `Backup ${ago(b.ok_at)} (${Math.max(1, Math.round(b.bytes / 2 ** 20))} MB) in ${b.dir}` : "No backup yet",
      failed ? ` · last attempt failed: ${b.error}` : "");
  }

  function imageArchiveBlock(a, reload) {
    if (!a?.enabled) return null;
    const count = Number(a.archived || 0);
    const bytes = Number(a.bytes || 0);
    const warning = a.free_space_warning || (a.errors ? pluralize(a.errors, "image archive error") : "");
    const summary = h("div", {},
      h("p", { class: `small${warning ? " bad" : ""}` },
        h("strong", {}, "Image archive"), " ",
        `${count} image${count === 1 ? "" : "s"} · ${Math.round(bytes / 2 ** 20)} MB · ${a.path}`),
      a.last_reconciliation ? h("p", { class: "muted small" },
        `Reconciled ${ago(a.last_reconciliation)} · ${a.missing || 0} missing · ${a.errors || 0} errors`) : null,
      warning ? h("p", { class: "note bad" }, warning) : null);
    if (!a.retention_days) return summary;
    return h("div", {}, summary,
      h("button", { class: "btn secondary", onclick: async (ev) => {
        ev.target.disabled = true;
        try {
          const p = await api("/maintenance/image-archive/retention/preview", { method: "POST" });
          if (!p.count) { toast("No archived images are old enough to remove"); return; }
          const size = Math.round(p.bytes / 2 ** 20);
          if (!(await confirmSheet({ title: `Permanently remove ${p.count} archived image${p.count === 1 ? "" : "s"} (${size} MB)?`,
            message: "Live gallery images are not deleted.", confirmLabel: "Remove", destructive: true }))) return;
          const r = await api("/maintenance/image-archive/retention/apply", {
            method: "POST", body: JSON.stringify({ confirmation: p.confirmation }),
          });
          toast(`Removed ${r.removed} archived image${r.removed === 1 ? "" : "s"} (${Math.round(r.bytes / 2 ** 20)} MB)`);
          reload();
        } catch (e) { toast(e.message); }
        finally { ev.target.disabled = false; }
      } }, `Review ${a.retention_days}-day image retention`));
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

  function discoveryLimitsText(limits) {
    return `${limits.visited_directories.toLocaleString()} directories · ${limits.candidates} candidates · ${limits.seconds} seconds · ${limits.errors} reported errors · one active scan · ${limits.expiry_seconds / 60}-minute expiry`;
  }

  function folderDiscoveryPanel(meta, reload) {
    const panel = h("div", { class: "folder-discovery" });
    let scan = null, pending = false, timer = null, expiryTimer = null, expired = false, closed = false;
    const stopPolling = () => { clearTimeout(timer); timer = null; };
    onLeave(() => { closed = true; stopPolling(); clearTimeout(expiryTimer); });
    const request = async (path, method = "GET", body) => {
      pending = true;
      try { return await api(`/remote-control/discovery/scans${path}`, { method, body }); }
      catch (e) { toast(e.message); return null; }
      finally { pending = false; }
    };
    const poll = async () => {
      if (closed || !scan || scan.status !== "running") return;
      const updated = await request(`/${encodeURIComponent(scan.id)}`);
      if (updated) scan = updated;
      render();
    };
    const add = async (candidate, slug) => {
      if (!(await confirmSheet({ title: "Add this owner-only folder?",
        message: `${candidate.path}\nMarkers: ${candidate.markers.join(", ")}\n\nMarker presence does not imply safety or trust. Adding never runs Claude or accepts trust.`,
        confirmLabel: "Add folder" }))) return;
      const result = await request(`/${encodeURIComponent(scan.id)}/candidates/${encodeURIComponent(candidate.id)}/promote`,
        "POST", { slug, confirmed_path: candidate.path, confirmed_markers: candidate.markers });
      if (result) { candidate.promoted = true; toast("Folder added. Trust in Claude is a separate action."); reload(); }
      render();
    };
    const candidateCard = candidate => {
      const slug = h("input", { type: "text", value: candidate.suggested_slug, maxlength: 64,
        "aria-label": "Folder display slug", pattern: "[a-z0-9][a-z0-9._-]{0,63}" });
      return h("div", { class: "card discovery-candidate" },
        h("p", { class: "discovery-path" }, candidate.path),
        h("p", { class: "muted small" }, `Markers: ${candidate.markers.join(", ")}`),
        h("p", { class: "muted small" }, candidate.trusted ? "Claude already trusts this exact folder" : "Claude workspace trust has not been accepted"),
        candidate.requires_git ? h("p", { class: "muted small" }, candidate.git ? "Git marker present; current spawn mode requires Git" : "Cannot add for worktree mode: Git marker required") : null,
        h("p", { class: "muted small" }, "Markers do not imply safety or trust."),
        candidate.promoted || candidate.configured_duplicate
          ? h("p", { class: "muted small" }, candidate.promoted ? "Already added" : "Already configured")
          : scan.status === "running" ? h("p", { class: "muted small" }, "Add folders after the scan finishes or is cancelled.")
          : h("div", { class: "row" }, slug, h("button", { class: "btn", disabled: pending || !candidate.launchable,
            onclick: () => add(candidate, slug.value) }, "Add folder")));
    };
    const render = () => {
      if (closed) return;
      stopPolling();
      fill(panel, h("h4", {}, "Find folders"),
        h("p", { class: "muted small" }, "Windows · owner-only · metadata-only. No file contents are read. Hidden/system entries, reparse points (including OneDrive), sensitive locations, caches and build folders are excluded. Apps and agents cannot access these folders."),
        h("p", { class: "muted small" }, discoveryLimitsText(meta.limits)),
        !meta.enabled ? h("p", { class: "note" }, "Discovery is off. Configure valid roots and enable it in Settings.") : null,
        expired ? h("p", { class: "note" }, "Results expired. Find folders again.") : null,
        h("button", { class: "btn", disabled: pending || !meta.enabled || scan?.status === "running", onclick: async () => {
          scan = await request("", "POST"); expired = false; clearTimeout(expiryTimer); expiryTimer = null; render();
        } }, "Find folders"),
        scan ? h("p", { role: "status" }, `${scan.status} · ${scan.visited} directories visited · ${scan.candidates.length} candidates · expires in ${scan.expires_in}s`) : null,
        scan?.status === "running" ? h("button", { class: "btn", disabled: pending, onclick: async () => {
          const updated = await request(`/${encodeURIComponent(scan.id)}`, "DELETE"); if (updated) scan = updated; render();
        } }, "Cancel scan") : null,
        scan?.truncated ? h("p", { class: "note" }, `Scan truncated: ${scan.reason}. Results are partial; scans do not resume.`) : null,
        scan?.reason && scan.status === "failed" ? h("p", { class: "note bad" }, scan.reason) : null,
        scan?.errors.length ? h("p", { class: "note" }, `Some directories could not be inspected (${scan.errors.length} reported errors).`) : null,
        scan?.errors.map(error => h("p", { class: "muted small" }, `${error.location}: ${error.code}`)),
        scan?.candidates.map(candidateCard));
      if (scan?.status === "running") timer = setTimeout(() => void poll(), 750);
      if (scan && !expiryTimer) expiryTimer = setTimeout(() => {
        scan = null; expired = true; expiryTimer = null; render();
      }, (scan.expires_in + 1) * 1000);
    };
    panel.updateDiscovery = next => { if (meta.enabled !== next.enabled) { meta = next; render(); } };
    render();
    return panel;
  }

  function remoteControlCard() {
    const body = h("div", {}, h("p", { class: "muted small" }, "Checking…"));
    let busy = "";
    let finder = null;
    const act = async (project, stop) => {
      busy = project;
      void load();
      try {
        const r = await api(`/remote-control/${encodeURIComponent(project)}${stop ? "/stop" : ""}`, { method: "POST" });
        if (stop) toast(`Stopped Remote Control for ${project}`);
        else toast(r.already_running ? "Already running" : "Remote Control is ready");
      } catch (e) { toast(e.message); }
      busy = "";
      void load();
    };
    const trust = async (project) => {
      if (!(await confirmSheet({ title: `Open Claude on the tower to trust “${project}”?`,
        message: "Review the folder shown by Claude, then accept its workspace trust prompt. The harness cannot accept it for you.",
        confirmLabel: "Open Claude" }))) return;
      busy = project;
      void load();
      try {
        const r = await api(`/remote-control/${encodeURIComponent(project)}/trust`, { method: "POST" });
        let message = "Claude trust window opened on the tower";
        if (r.already_trusted) message = "This repository is already trusted";
        else if (r.already_open) message = "The trust window is already open";
        toast(message, 5000);
      } catch (e) { toast(e.message); }
      busy = "";
      void load();
    };
    const rcButton = (p) => {
      if (p.running) return h("button", { class: "btn", disabled: !!busy, onclick: () => act(p.project, true) }, "Stop");
      if (p.invalid) return h("span", { class: "note bad" }, `Invalid folder: ${p.invalid}`);
      if (!p.trusted) {
        return h("button", { class: "btn", disabled: !!busy || p.trust_prompt_open, onclick: () => trust(p.project) },
          p.trust_prompt_open ? "Trust window open" : "Trust in Claude…");
      }
      return h("button", { class: "btn", disabled: !!busy, onclick: () => act(p.project, false) }, "Start");
    };
    const rcActions = (p) => h("div", { class: "row" },
      p.running && p.pairing_url ? h("a", { class: "btn", href: p.pairing_url, target: "_blank", rel: "noopener" }, "Open in Claude") : null,
      rcButton(p),
      p.managed ? h("button", { class: "btn", disabled: !!busy || p.running || p.trust_prompt_open, onclick: async () => {
        if (!(await confirmSheet({ title: `Remove managed folder “${p.project}”?`,
          message: `${p.path}\n\nThis removes only the harness entry. Files and Claude trust remain.`, confirmLabel: "Remove", destructive: true }))) return;
        try { await api(`/remote-control/folders/${encodeURIComponent(p.project)}`, { method: "DELETE" }); toast("Folder removed"); void load(); }
        catch (e) { toast(e.message); }
      } }, "Remove folder") : null);
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
        isGuest() ? h("p", { class: "muted small" }, "Demo access cannot start, stop, or trust Remote Control.") : rcActions(p));
    };
    const load = async () => {
      try {
        const r = await api("/remote-control");
        if (!r.enabled) return fill(body, h("p", { class: "muted small" }, "Disabled in config/harness.yaml (remote_control)."));
        if (r.discovery?.supported && !isGuest() && !isMember()) {
          finder ||= folderDiscoveryPanel(r.discovery, () => void load());
          finder.updateDiscovery(r.discovery);
        }
        fill(body,
          h("p", { class: "muted small" }, "Start Claude Code in a project folder and continue in the Claude app. These sessions use your Claude subscription, not the harness."),
          h("p", { class: "muted small" }, "Only tower projects with a local folder appear. Homelab and scratch have none, so they are omitted."),
          r.projects.length ? r.projects.map(row) : h("p", { class: "muted small" }, "No tower projects with a local folder."), finder);
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
          isGuest() ? null : imageArchiveBlock(u.image_archive, load),
          top.length ? h("p", { class: "muted small", style: "margin-top:10px" }, "Largest workspaces") : null,
          top.length ? h("ul", { class: "small" }, top.map((w) => h("li", {}, h("a", { href: `#/s/${w.session}/info` }, w.session), ` ${mb(w.mb)}`))) : null);
        fill(body,
          device("Tower", u.free_gb, u.total_gb, towerExtra),
          (u.runners || []).map((r) => {
            const online = !!r.online;
            const compatibility = r.compatibility || {};
            const compatible = compatibility.state === "compatible";
            const canUpdate = online && compatible && r.update_supported;
            const extra = h("div", { class: "disk-facts" },
              fact("Runner", online
                ? `${r.info.version} · protocol ${r.info.protocol ?? "not reported"} · macOS ${r.info.macos}`
                : lastSeenText(r)),
              fact("Compatibility", compatibilityText(compatibility)),
              r.last_update ? fact("Last update", lastUpdateText(r.last_update)) : null,
              isGuest() ? null : h("button", {
                class: "btn", type: "button", disabled: !canUpdate,
                onclick: async (ev) => {
                  ev.target.disabled = true;
                  try {
                    const result = await api(`/runners/${r.name}/update`, { method: "POST" });
                    toast(result.message || "Mac client update queued", 5000);
                    setTimeout(load, 3000);
                  } catch (e) { toast(e.message, 8000); ev.target.disabled = false; }
                },
              }, "Update Mac client"),
              !canUpdate ? h("p", { class: "muted small" }, `On the Mac, run: ${r.manual_update || "harness update"}`) : null);
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
      isGuest() ? null : h("div", { class: "card" },
        h("p", { class: "muted small" }, "Removes stopped sandbox containers, expired session workspaces, and leftover workspace folders."),
        h("button", {
          class: "btn",
          onclick: async (ev) => {
            if (!(await confirmSheet({ title: "Clean up now?",
              message: "Removes stopped sandbox containers, expired session workspaces, and leftover workspace folders.",
              confirmLabel: "Clean up", destructive: true }))) return;
            ev.target.disabled = true;
            try {
              const r = await api("/maintenance/cleanup", { method: "POST" });
              toast(`Removed ${r.containers_removed} containers, ${r.workspaces_removed + r.orphans_removed} workspaces`);
              void load();
            } catch (e) { toast(e.message); }
            ev.target.disabled = false;
          },
        }, "Clean up now")));
  }

  return { viewActions, folderDiscoveryPanel, remoteControlCard };
}
