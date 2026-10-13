// The backend and model control of New task, shared with the session page's Fork, Run again and Continue on another
// backend sheets (#664). A hosted backend runs its configured default model; the local model offers each of `models`.
// The DOM builder comes in as `h` and `fill`, so this module imports under plain Node.

/** @param {string} name */
export const backendLabel = (name) => (name === "local" ? "Local model" : name);

/**
 * The model options for one backend: every local model, or the hosted backend's default as its one choice.
 * @param {any} h
 * @param {string} name
 * @param {Array<{name: string, model?: string | null}>} backends
 * @param {Array<{name: string}>} models
 * @param {string} [selected]
 */
export function modelOptions(h, name, backends, models, selected) {
  if (name === "local") return models.map((m) => h("option", { value: m.name, selected: m.name === selected }, m.name));
  const b = backends.find((x) => x.name === name);
  return [h("option", { value: b?.model || "" }, b?.model || `${name} default`)];
}

/**
 * Backend and model selects for a sheet. `keep` names the session's backend and adds a first "Same as this session"
 * choice (value ""), which leaves both to the server; `exclude` leaves a backend out. `choice()` is the request body
 * part: {} to keep, else {backend, model}, with model null on a hosted backend (its default).
 * @param {{h: any, fill: any, backends: Array<{name: string, available?: boolean, model?: string | null}>,
 *   models: Array<{name: string, default?: boolean}>, keep?: string, exclude?: string, id?: string}} options
 */
export function backendPicker({ h, fill, backends, models, keep = "", exclude = "", id = "backend-choice" }) {
  const available = backends.filter((b) => b.available && b.name !== exclude);
  const localDefault = (models.find((m) => m.default) || models[0])?.name;
  const backend = h("select", { id: `${id}-backend` },
    keep ? h("option", { value: "" }, `Same as this session (${backendLabel(keep)})`) : null,
    available.map((b) => h("option", { value: b.name }, backendLabel(b.name))));
  const model = h("select", { id: `${id}-model` });
  const sync = () => {
    fill(model, backend.value ? modelOptions(h, backend.value, backends, models, localDefault) : h("option", { value: "" }, "Same model"));
    if (backend.value === "local") model.value = localDefault || "";
    model.disabled = backend.value !== "local";
  };
  backend.value = keep ? "" : (available[0]?.name || "");
  backend.addEventListener("change", sync);
  sync();
  const choice = () => (backend.value
    ? { backend: backend.value, model: backend.value === "local" ? model.value || null : null } : {});
  return { backend, model, available, choice };
}
