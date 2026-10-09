// Bundle compatibility and update flow (#258): checks the server's protocol range and newer-bundle hint, shows the
// blocking "Update required" card and reloads into a new build. mountUpdate() receives the build identity, shell elements
// and browser globals as arguments (client.mjs touches localStorage on import, so it is not imported here), so importing
// this module works under plain Node.
import { protocolMismatch } from "./compat.mjs";
import { h, fill } from "./dom.mjs";
import * as sheets from "./sheet.mjs";

export const UPDATE_GUARD = "harness.webUpdateAttempt";

export function mountUpdate({ els, agentHarnessWeb, session, chrome, tabs, route, build, browser, confirmSheet = sheets.confirmSheet }) {
  const { $app } = els;
  const { document } = browser;
  const { WEB_BUILD_ID, WEB_PROTOCOL } = build;

  function hasUnsavedInput() {
    return [...document.querySelectorAll("input, textarea, select")].some((el) => {
      if (el.type === "checkbox" || el.type === "radio") return el.checked !== el.defaultChecked;
      if (el.tagName === "SELECT") return [...el.options].some((option) => option.selected !== option.defaultSelected);
      return el.value !== el.defaultValue;
    });
  }

  // checkInput: false when the caller already checked, as the update offer does before it opens: the sheet is modal, and
  // the page routed underneath it may fill fields from code (a job's prompt, a restored draft), which reads as unsaved.
  async function reloadAndUpdate({ checkInput = true } = {}) {
    if (checkInput && hasUnsavedInput()) {
      chrome.toast("Save or discard your form changes before reloading the app.", 6000);
      return false;
    }
    const attempted = browser.sessionStorage.getItem(UPDATE_GUARD);
    if (attempted === WEB_BUILD_ID) {
      fill($app, h("div", { class: "card" },
        h("h2", {}, "Update did not load"),
        h("p", {}, "Close every installed Agent Harness window, reopen it while online, and reload. If it still fails, remove and reinstall the home-screen app.")));
      return false;
    }
    // Use only the bundle's compiled identifier in browser storage. Compatibility metadata is remote input.
    browser.sessionStorage.setItem(UPDATE_GUARD, WEB_BUILD_ID);
    if (browser.window.caches) {
      const keys = await browser.caches.keys();
      await Promise.all(keys.filter((key) => key.startsWith("harness-shell-")).map((key) => browser.caches.delete(key)));
    }
    const registration = await browser.navigator.serviceWorker?.getRegistration();
    registration?.active?.postMessage("PURGE_SHELL");
    await registration?.update();
    browser.location.reload();
    return true;
  }

  function blockingUpdate(meta, state) {
    session.setBlocked(true);
    tabs.paint([], { hidden: true }); // a blocked app offers no navigation
    chrome.setHeader("agents", "Update required", { page: true });
    const daemonIsOld = state === "daemon_update_required";
    fill($app, h("div", { class: "card" },
      h("h2", {}, daemonIsOld ? "Update Agent Harness Server" : "Update Agent Harness Web"),
      h("p", {}, daemonIsOld
        ? "This browser app uses a newer protocol than the connected server. Update the server, then reload."
        : "This installed app is too old for the connected server."),
      daemonIsOld ? null : h("button", { class: "btn primary", onclick: () => reloadAndUpdate() }, "Reload and update"),
      h("p", { class: "muted small" }, `Web protocol ${WEB_PROTOCOL}; server supports ${meta.protocols?.admin?.min}–${meta.protocols?.admin?.max}.`)));
  }

  // Offers once per bundle to reload into the newer build. The sheet does not hold up boot or routing: the app keeps
  // running underneath, and Update reloads it.
  async function offerBundleUpdate(foreground) {
    try { await (await browser.navigator.serviceWorker?.getRegistration())?.update(); } catch (_) { /* try again on reload */ }
    const promptKey = "harness.webUpdatePrompt";
    if (browser.sessionStorage.getItem(promptKey) === WEB_BUILD_ID || (foreground && hasUnsavedInput())) return;
    browser.sessionStorage.setItem(promptKey, WEB_BUILD_ID);
    void confirmSheet({ title: "Update Agent Harness Web?", message: "A newer version is available. Updating reloads the app.",
      confirmLabel: "Update now", cancelLabel: "Later", dismissOnRoute: false }).then((yes) => (yes ? reloadAndUpdate({ checkInput: false }) : false))
      .catch((e) => chrome.toast(e.message, 6000));
  }

  async function checkCompatibility({ foreground = false } = {}) {
    let meta;
    try { meta = await agentHarnessWeb.compatibility(); }
    catch (_) { return !session.isBlocked(); } // stay on the update card if health fails after a skew
    const mismatch = protocolMismatch(meta.protocols?.admin, WEB_PROTOCOL);
    if (mismatch) {
      blockingUpdate(meta, mismatch);
      return false;
    }
    const wasBlocked = session.isBlocked();
    session.setBlocked(false);
    const available = meta.update_hint?.web?.build_id;
    if (available && available !== WEB_BUILD_ID) {
      await offerBundleUpdate(foreground);
    } else {
      browser.sessionStorage.removeItem(UPDATE_GUARD);
    }
    if (wasBlocked) await route();
    return true;
  }

  return { checkCompatibility, hasUnsavedInput, reloadAndUpdate };
}
