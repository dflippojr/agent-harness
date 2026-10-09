// In-app sheets that replace the browser's native confirm and prompt dialogs (#513). Each opens a <dialog> as a bottom sheet
// on phones and a centred card on wide screens, and resolves a Promise: confirmSheet() to true or false, promptSheet() to the
// entered text or null, formSheet() to { name: value } or null. Escape, a tap outside the sheet or a route change dismisses.
// Browser globals are read when a sheet opens, never at module top level, so this imports under plain Node.
import { h } from "./dom.mjs";

let current = null; // the open sheet's close(), so a new sheet or a navigation dismisses it
let serial = 0;

function openSheet({ title, message, fields = [], confirmLabel, cancelLabel = "Cancel", destructive = false, dismissOnRoute = true }) {
  current?.(null);
  const doc = globalThis.document;
  const win = globalThis.window;
  const id = `sheet-${++serial}`;
  const returnFocus = doc.activeElement;
  return new Promise((resolve) => {
    const inputs = fields.map((f, i) => {
      const input = h("input", { id: `${id}-f${i}`, name: f.name, type: f.type || "text", inputmode: f.inputmode,
        placeholder: f.placeholder, autocomplete: "off", "aria-describedby": `${id}-e${i}` });
      input.value = f.value ?? "";
      return input;
    });
    const errors = fields.map((_, i) => h("p", { id: `${id}-e${i}`, class: "sheet-error", "aria-live": "polite" }));
    const cancel = h("button", { type: "button", class: "btn sheet-cancel", "data-sheet": "cancel" }, cancelLabel);
    const ok = h("button", { type: "submit", class: `btn ${destructive ? "sheet-danger" : "primary"}`, "data-sheet": "confirm" },
      confirmLabel);
    const form = h("form", { class: "sheet-body", novalidate: true },
      h("div", { class: "sheet-handle", "aria-hidden": "true" }),
      h("h2", { id: `${id}-t`, class: "sheet-title" }, title),
      message ? h("p", { class: "sheet-message" }, message) : null,
      fields.map((f, i) => h("div", { class: "sheet-field" },
        h("label", { for: inputs[i].id }, f.label), inputs[i], errors[i])),
      h("div", { class: "sheet-actions" }, cancel, ok));
    const dialog = h("dialog", { class: "sheet", "aria-labelledby": `${id}-t` }, form);

    const close = (result) => {
      if (current !== close) return;
      current = null;
      win?.removeEventListener?.("hashchange", onRoute);
      if (dialog.open) dialog.close?.();
      dialog.remove?.();
      returnFocus?.focus?.();
      resolve(result);
    };
    const onRoute = () => close(null);
    current = close;

    // Each field's validate(value) returns an error message, or nothing when the value is fine; the sheet stays open on error.
    const valid = () => {
      let first = null;
      fields.forEach((f, i) => {
        const problem = f.validate?.(inputs[i].value) || "";
        errors[i].textContent = problem;
        if (problem) inputs[i].setAttribute("aria-invalid", "true"); else inputs[i].removeAttribute("aria-invalid");
        if (problem && !first) first = inputs[i];
      });
      first?.focus?.();
      return !first;
    };
    form.addEventListener("submit", (ev) => {
      ev.preventDefault?.();
      if (!valid()) return;
      close(fields.length ? Object.fromEntries(fields.map((f, i) => [f.name, inputs[i].value])) : true);
    });
    cancel.addEventListener("click", () => close(null));
    dialog.addEventListener("cancel", (ev) => { ev.preventDefault?.(); close(null); }); // Escape
    dialog.addEventListener("click", (ev) => { if (ev.target === dialog) close(null); }); // a tap on the backdrop
    if (dismissOnRoute) win?.addEventListener?.("hashchange", onRoute);

    doc.body.append(dialog);
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
    // A destructive sheet starts on the safe choice; an input sheet on its first field.
    (inputs[0] || (destructive ? cancel : ok)).focus?.();
  });
}

// True while a sheet waits for an answer. An app-level offer checks it so it never replaces a sheet the person is answering.
export const sheetOpen = () => current !== null;

// Resolves true when the person picks confirmLabel, false otherwise. Name the action ("Delete job"), not "OK".
// dismissOnRoute: false keeps an app-level sheet (the update offer) open across the boot redirect and later navigation.
export async function confirmSheet({ title, message, confirmLabel = "OK", cancelLabel, destructive = false, dismissOnRoute }) {
  return (await openSheet({ title, message, confirmLabel, cancelLabel, destructive, dismissOnRoute })) === true;
}

// Resolves the entered text, or null when dismissed. validate(value) returns an inline error message, or nothing.
export async function promptSheet({ title, message, label, value = "", confirmLabel = "Save", cancelLabel, type, inputmode,
  placeholder, validate }) {
  const result = await openSheet({ title, message, confirmLabel, cancelLabel,
    fields: [{ name: "value", label: label || title, value, type, inputmode, placeholder, validate }] });
  return result ? result.value : null;
}

// Several inputs in one sheet. fields: [{ name, label, value, type, inputmode, placeholder, validate }].
export async function formSheet({ title, message, fields, confirmLabel = "Save", cancelLabel, destructive = false }) {
  return openSheet({ title, message, fields, confirmLabel, cancelLabel, destructive });
}

// Validators for promptSheet and formSheet fields.
export const required = (what) => (v) => (v.trim() ? "" : `Enter ${what}.`);
export const wholeNumber = (min = 0) => (v) => (/^\d+$/.test(v.trim()) && Number(v) >= min ? "" : `Enter a whole number, ${min} or more.`);
