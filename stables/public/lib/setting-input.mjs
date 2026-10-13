// @ts-nocheck
// Input control for one daemon setting; `h` is the DOM builder injected by the caller so this imports under plain Node.
export function settingInput(h, spec, draft) {
  const current = draft[spec.key] !== undefined ? draft[spec.key] : (spec.pending ?? spec.effective);
  if (spec.type === "discovery_root_list") {
    const input = h("textarea", { class: "discovery-roots", rows: 3, disabled: !spec.writable,
      placeholder: "One local directory per line (up to 8)", value: (current || []).join("\n") });
    input.addEventListener("change", () => {
      draft[spec.key] = input.value.split(/\r?\n/).filter(s => s !== "");
    });
    return input;
  }
  if (spec.type === "bool") {
    const box = h("input", { type: "checkbox", class: "switch", checked: !!current, disabled: !spec.writable });
    box.addEventListener("change", () => { draft[spec.key] = box.checked; });
    return box;
  }
  if (spec.enum?.length) {
    const sel = h("select", { disabled: !spec.writable }, spec.enum.map((item) =>
      h("option", { value: item, selected: item === current }, item)));
    sel.addEventListener("change", () => { draft[spec.key] = sel.value; });
    return sel;
  }
  const input = h("input", {
    type: spec.type === "string" ? "text" : "number",
    value: current == null ? "" : String(current),
    disabled: !spec.writable,
    min: spec.minimum, max: spec.maximum, step: spec.type === "int" ? "1" : "any",
  });
  input.addEventListener("change", () => {
    if (input.value === "") { draft[spec.key] = null; return; }
    draft[spec.key] = spec.type === "string" ? input.value : Number(input.value);
  });
  return input;
}
