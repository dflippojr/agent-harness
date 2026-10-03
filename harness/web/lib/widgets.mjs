// Small shared widgets (#258): status/review/job badges and the progress bar. Labels are plain data; the elements come from
// dom.mjs, so nothing here touches document at module top level.
import { h } from "./dom.mjs";

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
