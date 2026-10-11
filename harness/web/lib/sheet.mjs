// @ts-nocheck
// In-app sheets that replace the browser's native confirm and prompt dialogs (#513). Each opens a <dialog> as a bottom sheet
// on phones and a centred card on wide screens, and resolves a Promise: confirmSheet() to true or false, promptSheet() to the
// entered text or null, formSheet() to { name: value } or null; panelSheet() shows a read-only panel (the keyboard shortcuts)
// and resolves null. Escape, a tap outside the sheet or a route change dismisses.
// Browser globals are read when a sheet opens, never at module top level, so this imports under plain Node.
import { h } from "./dom.mjs";

let current = null; // the open sheet's close(), so a new sheet or a navigation dismisses it
let serial = 0;

// Opens `dialog` as the one sheet on the page. wire(close) attaches the sheet's own controls and returns the element that
// takes focus first. Escape, a tap on the backdrop or (with dismissOnRoute) a route change closes it with null; focus goes
// back where it was, and the Promise resolves with what close() was given.
function present(dialog, { dismissOnRoute = true, wire }) {
  current?.(null);
  const doc = globalThis.document;
  const win = globalThis.window;
  const returnFocus = doc.activeElement;
  return new Promise((resolve) => {
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
    const first = wire(close);
    dialog.addEventListener("cancel", (ev) => { ev.preventDefault?.(); close(null); }); // Escape
    // A tap on the backdrop dismisses, but only when the press started there too: a text selection dragged out of the
    // sheet also ends in a click on the dialog.
    let pressedBackdrop = false;
    dialog.addEventListener("pointerdown", (ev) => { pressedBackdrop = ev.target === dialog; });
    dialog.addEventListener("click", (ev) => { if (ev.target === dialog && pressedBackdrop) close(null); });
    if (dismissOnRoute) win?.addEventListener?.("hashchange", onRoute);

    doc.body.append(dialog);
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
    first?.focus?.();
  });
}

function openSheet({ title, message, fields = [], confirmLabel, cancelLabel = "Cancel", destructive = false, dismissOnRoute = true }) {
  const id = `sheet-${++serial}`;
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
  return present(dialog, { dismissOnRoute, wire: (close) => {
    form.addEventListener("submit", (ev) => {
      ev.preventDefault?.();
      if (!valid()) return;
      close(fields.length ? Object.fromEntries(fields.map((f, i) => [f.name, inputs[i].value])) : true);
    });
    cancel.addEventListener("click", () => close(null));
    // Confirmations start on the safe choice; input sheets start on their first field.
    return inputs[0] || cancel;
  } });
}

const CLOSE_SVG = '<svg class="tab-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M18 6 6 18M6 6l12 12"/></svg>';

// A sheet that shows something rather than asks (the keyboard shortcuts, #571): a heading with an optional inline-SVG icon,
// the body and a Close button, which takes focus first. Resolves null once dismissed. `className` adds to "sheet".
export function panelSheet({ title, icon = "", body, className = "" }) {
  const id = `sheet-${++serial}`;
  const closeButton = h("button", { type: "button", class: "icon sheet-close", "aria-label": "Close", "data-sheet": "cancel",
    html: CLOSE_SVG });
  const dialog = h("dialog", { class: `sheet ${className}`.trim(), "aria-labelledby": `${id}-t` },
    h("div", { class: "sheet-body" },
      h("div", { class: "sheet-handle", "aria-hidden": "true" }),
      h("div", { class: "sheet-head" },
        icon ? h("span", { class: "sheet-icon", html: icon }) : null,
        h("h2", { id: `${id}-t`, class: "sheet-title" }, title),
        closeButton),
      body));
  return present(dialog, { wire: (close) => {
    closeButton.addEventListener("click", () => close(null));
    return closeButton;
  } });
}

// Dismisses whichever sheet is open, as Escape would.
export const dismissSheet = () => current?.(null);

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
