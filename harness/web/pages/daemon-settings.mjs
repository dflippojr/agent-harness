// Owner Settings -> Daemon page (#258): operational config view, change plan, apply, rollback and supervised restart.
// The shell (DOM builder, api, toast) is injected by app.js so this module imports under plain Node and never
// reaches into another page.
import { settingValueText, settingMeta, recoveryNote } from "../lib/settings-text.mjs";
import { settingInput } from "../lib/setting-input.mjs";
import * as sheets from "../lib/sheet.mjs";

export function mountDaemonSettings({ h, fill, append, api, toast, isGuest, location, confirmSheet = sheets.confirmSheet }) {
  async function daemonSettingsCard() {
    let view;
    try { view = await api("/config"); }
    catch (e) { return h("div", { class: "card" }, h("p", { class: "note bad" }, e.message)); }
    const draft = {};
    const status = h("p", { class: "muted small" },
      `Revision ${view.revision}` +
      (view.pending_revision ? ` · pending ${view.pending_revision}` : "") +
      (view.supervised_restart ? " · supervised restart supported" : " · unsupervised (restart is manual)") +
      (view.warning ? ` · ${view.warning}` : recoveryNote(view.recovery)));
    const restartButton = view.restart_required ? h("button", { class: "btn", type: "button", onclick: () => confirmRestart(view.pending_revision || view.revision, status, errorBox) },
      "Restart daemon") : null;
    const planBox = h("div", { class: "config-plan" });
    const errorBox = h("div");
    const groups = {};
    for (const spec of view.settings || []) {
      if (spec.key.startsWith("remote_control.discovery.") && !spec.available) continue;
      groups[spec.category] ||= [];
      groups[spec.category].push(spec);
    }
    const rows = Object.entries(groups).map(([category, specs]) => h("div", { class: "card config-category" },
      h("h3", {}, category),
      specs.map((spec) => h("div", { class: "config-row" },
        h("div", { class: "config-copy" },
          h("label", { class: "field-label" }, spec.label),
          h("p", { class: "muted small" }, spec.help),
          h("p", { class: "muted small config-meta" }, settingMeta(spec)),
          spec.file_only ? null : h("p", { class: "muted small" }, settingValueText(spec))),
        spec.file_only ? h("span", { class: "muted small" }, "local config") : settingInput(h, spec, draft)))));

    const apply = async ({ restart = false, rollback = false } = {}) => {
      fill(errorBox);
      fill(planBox);
      const changes = {};
      for (const [key, value] of Object.entries(draft)) changes[key] = value;
      try {
        if (rollback) {
          await rollbackConfig(view.revision, status, errorBox);
          return;
        }
        const plan = await api("/config", { method: "PATCH", body: { revision: view.revision, dry_run: true, changes } });
        append(planBox, h("p", { class: "field-label" }, "Change plan"), configPlanList(plan));
        const enables = (plan.changes || []).filter((c) => c.to === true && String(c.key).endsWith(".enabled"));
        if (enables.length && !(await confirmSheet({ title: `Enable ${enables.map((c) => c.key).join(", ")}?`, confirmLabel: "Enable" }))) return;
        if (!(plan.changes || []).length) return;
        if (!(await confirmSheet({ title: "Apply these server settings?", confirmLabel: "Apply" }))) return;
        const result = await api("/config", { method: "PATCH", body: { revision: view.revision, changes } });
        toast("Saved");
        if (result.restart_required || restart) {
          await confirmRestart(result.pending_revision || result.target_revision || result.revision, status, errorBox);
        } else { location.reload(); }
      } catch (e) {
        showConfigError(errorBox, e);
      }
    };

    return h("div", {},
      h("div", { class: "card" },
        h("p", { class: "muted small" }, "Operational settings for this daemon. Paths, secrets, modules, and network policy stay in local configuration."),
        status),
      ...rows,
      planBox, errorBox,
      isGuest() ? null : h("div", { class: "card config-actions" },
        h("button", { class: "btn", type: "button", onclick: () => apply() }, "Review and apply"),
        h("button", { class: "btn", type: "button", onclick: () => apply({ rollback: true }) }, "Roll back"),
        restartButton));
  }

  function configPlanList(plan) {
    if (!(plan.changes || []).length) return h("p", { class: "muted small" }, "No changes.");
    return h("ul", { class: "config-plan-list" }, plan.changes.map((c) =>
      h("li", {}, `${c.key}: ${c.from} → ${c.action === "reset" ? "inherited" : c.to} (${c.apply})`)));
  }

  function showConfigError(errorBox, e) {
    if (e.code === "revision_conflict") {
      append(errorBox, h("p", { class: "note bad" }, "This page is stale. Reload to edit the current revision."));
    } else if (e.keys) {
      append(errorBox, h("p", { class: "note bad" }, e.message),
        h("ul", {}, Object.entries(e.keys).map(([key, info]) =>
          h("li", {}, `${key}: ${info.message || info.code}`))));
    } else {
      append(errorBox, h("p", { class: "note bad" }, e.message));
    }
  }

  async function rollbackConfig(revision, status, errorBox) {
    if (!(await confirmSheet({ title: "Restore the previous confirmed server configuration?", confirmLabel: "Roll back", destructive: true }))) return;
    const result = await api("/config/rollback", { method: "POST", body: { revision, confirm: true } });
    toast("Rolled back");
    if (result.restart_required) {
      await confirmRestart(result.pending_revision || result.revision, status, errorBox);
    } else { location.hash = "#/profile/daemon"; location.reload(); }
  }

  async function confirmRestart(targetRevision, status, errorBox) {
    if (!(await confirmSheet({ title: "Restart the daemon to apply pending settings?",
      confirmLabel: "Restart", destructive: true }))) return;
    status.textContent = "Restarting… reconnecting to see whether the target revision became active.";
    try {
      await api("/config/restart", { method: "POST", body: { revision: targetRevision, confirm: true } });
    } catch (e) {
      if (e.code === "restart_not_supervised") {
        append(errorBox, h("p", { class: "note bad" }, e.message));
        return;
      }
      // 202 may still parse as success; a dropped connection is expected.
    }
    const started = Date.now();
    while (Date.now() - started < 45000) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        const next = await api("/config");
        if (next.revision === targetRevision && next.confirmed) {
          toast("Restarted with the new configuration");
          location.reload();
          return;
        }
        if (next.recovery?.recovery === "lkg_restore") {
          append(errorBox, h("p", { class: "note bad" },
            `Automatic recovery restored revision ${next.revision}. ${next.recovery.reason || ""}`.trim()));
          return;
        }
        if (next.recovery?.recovery === "overlay_quarantined") {
          append(errorBox, h("p", { class: "note bad" },
            next.warning || next.recovery.warning || next.recovery.reason ||
            "Managed overlay was quarantined; YAML defaults are in effect."));
          return;
        }
      } catch (_) { /* daemon still down */ }
    }
    append(errorBox, h("p", { class: "note bad" }, "Timed out waiting for the daemon to come back."));
  }

  return { daemonSettingsCard };
}
