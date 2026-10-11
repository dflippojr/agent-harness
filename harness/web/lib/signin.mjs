// @ts-nocheck
// Household Google sign-in view (#258, issue #64). mountSignIn() receives the shell elements and browser globals as
// arguments, so importing this module touches nothing and works under plain Node.
import { h, fill } from "./dom.mjs";

export const GOOGLE_FAILED = "Google sign-in did not complete. Try again, or ask the owner for a new link code.";

export function mountSignIn({ els, api, getWebAuth, toast, browser }) {
  const { $app, $title } = els;

  async function startGoogle(mode, code) {
    const body = { mode };
    if (code) body.code = code;
    const started = await api("/auth/google/start", { method: "POST", surface: "app", body });
    browser.location.assign(started.authorization_url);
  }

  function linkCodeForm(label) {
    const input = h("input", { type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: "Link code from the owner", required: true });
    const submit = h("button", { class: "btn", type: "submit" }, label);
    return h("form", { onsubmit: async (e) => {
      e.preventDefault();
      submit.disabled = true;
      try { await startGoogle("invite", input.value.trim()); }
      catch (err) { toast(err.message, 6000); submit.disabled = false; }
      input.value = "";
    } }, input, h("div", { class: "row", style: "margin-top:8px" }, submit));
  }

  function viewSignIn(failed) {
    $title.textContent = "Sign in";
    const webAuth = getWebAuth();
    const available = !!webAuth?.google?.available;
    const button = h("button", { class: "btn primary", type: "button", onclick: async () => {
      button.disabled = true;
      try { await startGoogle("signin"); } catch (e) { toast(e.message, 6000); button.disabled = false; }
    } }, "Sign in with Google");
    fill($app, h("div", { class: "card" },
      h("h3", {}, "Household sign-in"),
      failed ? h("p", { class: "note bad" }, GOOGLE_FAILED) : null,
      available ? h("p", { class: "muted small" }, webAuth.google.explanation) : null,
      available ? h("div", { class: "row" }, button)
        : h("p", { class: "muted small" }, "Google sign-in is not available on this server. Ask the owner."),
      available ? h("p", { class: "muted small", style: "margin-top:16px" },
        "First time on this device? Enter the one-time link code the owner gave you.") : null,
      available ? linkCodeForm("Link with Google") : null));
  }

  return { startGoogle, viewSignIn };
}
