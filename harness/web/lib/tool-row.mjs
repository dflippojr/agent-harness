// Tool-call rows in the session transcript (#508): a 48 px summary (icon, name, argument summary, status pill) whose
// output and arguments are only built when the row first opens, each behind a line count with Copy and Open. Open shows
// the full text in a viewer (full-screen on phones, centred on desktop) with wrap and monospace.
// The DOM builder and browser globals are injected, so this imports under plain Node.
import { lineCount, previewText } from "./tools.mjs";
import { fmtTokens, pluralize } from "./format.mjs";

export function createToolRows({ h, fill, toast, browser }) {
  const { document } = browser;
  let viewer = null;
  let returnFocus = null;

  const selectText = (node) => {
    const selection = browser.window?.getSelection?.();
    if (!selection || !document.createRange) return;
    const range = document.createRange();
    range.selectNodeContents(node);
    selection.removeAllRanges();
    selection.addRange(range);
  };

  const closeViewer = () => {
    const open = viewer;
    viewer = null;
    if (!open) return;
    if (open.close) open.close();
    open.remove();
    returnFocus?.focus?.();
    returnFocus = null;
  };

  // Modal viewer: Escape and Close dismiss and return focus to the opener; the text is the one scroller.
  function openViewer(title, text) {
    closeViewer();
    returnFocus = document.activeElement;
    let wrapped = true;
    const pre = h("pre", { class: "tool-viewer-text wrap", tabindex: "0" }, text);
    const wrapBtn = h("button", { class: "tool-btn", type: "button", "aria-pressed": "true" }, "Wrap");
    wrapBtn.addEventListener("click", () => {
      wrapped = !wrapped;
      wrapBtn.setAttribute("aria-pressed", String(wrapped));
      pre.className = wrapped ? "tool-viewer-text wrap" : "tool-viewer-text";
    });
    const closeBtn = h("button", { class: "tool-btn", type: "button", onclick: closeViewer }, "Close");
    const dialog = h("dialog", { class: "tool-viewer", "aria-label": title },
      h("header", { class: "tool-viewer-head" },
        h("h2", {}, title),
        wrapBtn,
        h("button", { class: "tool-btn", type: "button", onclick: () => copy(text, () => selectText(pre)) }, "Copy"),
        closeBtn),
      pre);
    dialog.addEventListener("cancel", (ev) => { ev.preventDefault?.(); closeViewer(); });
    dialog.addEventListener("close", () => { if (viewer === dialog) closeViewer(); });
    let pressedBackdrop = false;
    dialog.addEventListener("pointerdown", (ev) => { pressedBackdrop = ev.target === dialog; });
    dialog.addEventListener("click", (ev) => { if (ev.target === dialog && pressedBackdrop) closeViewer(); });
    viewer = dialog;
    document.body.append(dialog);
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
    closeBtn.focus?.();
    return { dialog, pre };
  }

  // Copies and says so; without clipboard access (plain http on the tailnet, permission denied) the text opens in the
  // viewer, selected, to copy by hand.
  async function copy(text, fallback) {
    try {
      await browser.navigator.clipboard.writeText(text);
      toast("Copied");
    } catch (err) {
      console.debug("clipboard write failed", err);
      toast("Couldn't reach the clipboard: the text is selected, copy it by hand");
      fallback();
    }
  }

  // One labelled block: "Output · 23 lines" with Copy and Open, then a clipped preview of the first lines.
  function ioBlock(title, label, text) {
    const preview = previewText(text);
    return [
      h("div", { class: "tool-io-head" },
        h("span", { class: "tool-io-label" }, label),
        h("button", { class: "tool-btn", type: "button", onclick: () => copy(text, () => selectText(openViewer(title, text).pre)) }, "Copy"),
        h("button", { class: "tool-btn tool-open", type: "button", onclick: () => openViewer(title, text) }, "Open")),
      h("pre", { class: preview.more ? "tool-preview more" : "tool-preview" }, preview.text),
    ];
  }

  function outputBlock(name, output, outputChars) {
    if (output === null) return h("div", { class: "tool-io-head tool-io-label" }, "Waiting for output…");
    if (!output) return h("div", { class: "tool-io-head tool-io-label" }, "No output");
    const trimmed = outputChars > output.length ? ` · middle trimmed from ${fmtTokens(outputChars)} chars` : "";
    return ioBlock(`${name} · output`, `Output · ${pluralize(lineCount(output), "line")}${trimmed}`, output);
  }

  // Arguments fold behind their own line count; the JSON is only pretty-printed when that fold opens.
  function argsBlock(name, args) {
    const raw = JSON.stringify(args, null, 2);
    const inner = h("div", { class: "tool-args-body" });
    const fold = h("details", { class: "tool-args" },
      h("summary", {}, `Arguments · ${pluralize(lineCount(raw), "line")}`), inner);
    fold.addEventListener("toggle", () => {
      if (fold.open && !inner.childNodes.length) fill(inner, ioBlock(`${name} · arguments`, "Arguments", raw));
    });
    return fold;
  }

  // A row for one call. `args` is the parsed arguments object, or null when only a result arrived.
  function toolRow({ name, summary, kind, args }) {
    const state = h("span", { class: "tool-state run" }, "…");
    const outSlot = h("div", { class: "tool-out" });
    const body = h("div", { class: "body" });
    const el = h("details", { class: "tool" },
      h("summary", {},
        h("span", { class: `tool-ic ${kind}`, "aria-hidden": "true" }),
        h("span", { class: "name" }, name),
        h("span", { class: "args" }, summary || ""),
        state),
      body);
    const row = { el, state, body, output: null, outputChars: 0, rendered: false };
    const renderOutput = () => fill(outSlot, outputBlock(name, row.output, row.outputChars));
    el.addEventListener("toggle", () => {
      if (!el.open || row.rendered) return;
      row.rendered = true;
      renderOutput();
      fill(body, outSlot, args ? argsBlock(name, args) : null);
    });
    row.setState = (text, kindClass) => {
      state.textContent = text;
      state.className = `tool-state ${kindClass}`;
    };
    row.setOutput = (output, outputChars) => {
      row.output = output ?? "";
      row.outputChars = outputChars || 0;
      if (row.rendered) renderOutput();
    };
    return row;
  }

  return { toolRow, openViewer, closeViewer };
}
