// Small shared widgets (#258): status/review/job badges and the progress bar. Labels are plain data; the elements come from
// dom.mjs, so nothing here touches document at module top level.
import { h } from "./dom.mjs";
import { fmtSpan } from "./format.mjs";

export const TERMINAL = new Set(["done", "failed", "cancelled"]);
export const STATUS_LABEL = {
  queued: "queued", running: "running", waiting_approval: "needs approval", waiting_target: "waiting for Mac", waiting_app: "waiting for app", waiting_limit: "waiting for limit",
  done: "done", failed: "failed", cancelled: "cancelled",
};
export const REVIEW_LABEL = { merged: "merged", pushed: "pushed", discarded: "discarded" };

export const progressBar = (fraction) => h("div", { class: `progress${fraction === null ? " indeterminate" : ""}` },
  h("span", { style: fraction === null ? "" : `width:${Math.max(2, Math.min(100, fraction * 100)).toFixed(1)}%` }));

export const reviewBadge = (review, label) => h("span", { class: `badge ${review === "discarded" ? "cancelled" : "done"}` }, label);

export function badge(status) {
  return h("span", { class: `badge ${status}` }, STATUS_LABEL[status] || status);
}

export const jobStatusBadge = (st) => h("span", { class: `badge ${st === "ok" ? "done" : "waiting_approval"}` }, st === "ok" ? "OK" : "⚠ attention");

// "12 s ago" / "3 min ago" from a millisecond timestamp, for connection and staleness notes (#510).
export const sinceText = (ms, now = Date.now()) => `${fmtSpan(Math.max(0, Math.round((now - ms) / 1000)))} ago`;

// A list's "may be stale" note (#510): after a failed refresh it says when the list last updated and why the refresh
// failed, with Retry; a successful refresh hides it. The age ticks while it shows; stop() ends the tick when the page
// goes. Pages pass their own `make` (the injected h) so the note builds with whatever DOM they use, and `place(el)` to put
// it on the page the first time a refresh fails, so a list that never failed carries no hidden Retry.
export function staleNote({ make = h, place, onRetry, updatedAt = Date.now() }) {
  const text = make("span", { class: "stale-text" });
  const why = make("span", { class: "stale-why" });
  const retry = make("button", { type: "button", class: "stale-retry", onclick: () => onRetry() }, "Retry");
  const el = make("div", { class: "stale-note", role: "status", hidden: true }, make("span", { class: "stale-body" }, text, why), retry);
  let timer = null;
  const paint = () => { text.textContent = `List may be stale · last updated ${sinceText(updatedAt)}`; };
  const stop = () => { clearInterval(timer); timer = null; };
  return {
    el,
    ok() {
      updatedAt = Date.now();
      el.hidden = true;
      stop();
    },
    failed(err) {
      if (!el.parentNode) place(el);
      why.textContent = `Couldn't refresh: ${err?.message || err}`;
      el.hidden = false;
      paint();
      if (!timer) timer = setInterval(paint, 5000);
    },
    stop,
  };
}
