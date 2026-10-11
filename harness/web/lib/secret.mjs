// @ts-nocheck
// Show-once secret card (#258, #229): shows a secret exactly once, with a Copy button and a Done button that reloads the
// card. The clipboard helper is passed in because it toasts through the page; DOM comes from dom.mjs at call time.
import { h, fill } from "./dom.mjs";

export function showSecretOnce(form, load, intro, secret, copyLabel, copyToClipboard) {
  const field = h("input", { type: "text", readonly: true, value: secret, onclick: (e) => e.target.select() });
  fill(form, h("p", { class: "small" }, intro), field,
    h("div", { class: "row", style: "margin-top:8px" },
      h("button", { class: "btn", onclick: () => copyToClipboard(secret, () => field.select()) }, copyLabel),
      h("button", { class: "btn", onclick: load }, "Done")));
}
