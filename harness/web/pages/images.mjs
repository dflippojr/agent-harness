// Images pages (#258): gallery and generation form, one result, masked edit and the full-screen viewer. The shell
// (DOM builder, api, router, header) is injected by app.js so this module imports under plain Node and never
// reaches into another page.

import { ago } from "../lib/format.mjs";

export const IMAGE_PHASE = {
  idle: "", waiting: "Waiting for the GPU (a game or transcode is using it)", switching: "Unloading the language model",
  starting: "Starting ComfyUI", warm: "Image generator is ready", generating: "Generating",
  restoring: "Reloading the language model",
};
export const IMAGE_BUSY = new Set(["waiting", "switching", "starting", "generating", "restoring"]);

export function mountImages({ $app, h, fill, append, api, setHeader, toast, go, route, isGuest, isMember, onLeave, progressBar, confirmGpuQueue, daemonImage, downloadDaemonFile, location, confirm }) {
  function imageCard(img) {
    const ready = img.status === "done";
    const kind = img.operation && img.operation !== "generate" ? img.operation : "";
    const scale = Number(img.scale) > 1 ? `${img.scale}×` : "";
    const placeholderContent = img.status === "failed" || img.status === "cancelled" ? img.status : h("span", { class: "dots" }, img.status);
    return h("a", { class: "card image-card", href: `#/images/${img.id}` },
      ready ? daemonImage(`/images/${img.id}.png`, { alt: img.prompt, loading: "lazy" })
        : h("div", { class: `image-placeholder ${img.status}` }, placeholderContent),
      kind ? h("span", { class: "image-kind" }, kind) : null,
      scale ? h("span", { class: "image-scale" }, scale) : null,
      h("div", { class: "preview small" }, img.prompt));
  }

  function updateImageStatusView(view, s) {
    const text = s.phase in IMAGE_PHASE ? IMAGE_PHASE[s.phase] : s.phase;
    const { label, bar, fill: barFill, detail } = view.imageStatusParts;
    view.hidden = !text;
    if (!text) return view;
    const queued = s.queued ? ` · ${s.queued} queued` : "";
    const p = s.progress || {};
    const busy = IMAGE_BUSY.has(s.phase);
    const hasSteps = s.phase === "generating" && Number(p.max) > 0;
    const upscaling = s.phase === "generating" && p.stage === "upscaling";
    const editing = s.phase === "generating" && p.stage === "editing";
    const upscaleName = upscaling ? "Upscaling" : null;
    const editName = editing ? "Editing" : null;
    const stageName = upscaleName || editName;
    label.textContent = (stageName || text) + queued;
    label.classList.toggle("dots", busy);
    bar.hidden = !busy;
    detail.hidden = !hasSteps;
    if (busy) {
      bar.classList.toggle("indeterminate", !hasSteps);
      if (hasSteps) {
        const fraction = Math.max(0, Math.min(1, Number(p.value || 0) / Number(p.max)));
        barFill.style.width = `${Math.max(2, fraction * 100).toFixed(1)}%`;
        detail.textContent = `${stageName || "Sampling"} ${Math.round(fraction * 100)}% · ${p.value || 0} / ${p.max} steps`;
      } else {
        barFill.style.width = "";
        detail.textContent = "";
      }
    }
    return view;
  }

  function imageStatusView(s) {
    const label = h("span", { class: "image-status-label" });
    const bar = progressBar(null);
    const detail = h("span", { class: "muted small image-status-detail" });
    const view = h("div", { class: "image-status note" }, label, bar, detail);
    view.imageStatusParts = { label, bar, fill: bar.firstElementChild, detail };
    return updateImageStatusView(view, s);
  }

  function imageModeEntries(status) {
    if (status.modes) {
      return Object.entries(status.modes);
    }
    return Object.entries(status.models || {}).map(([id, label]) => [id, {
      label, available: true, resolution: id === "quality" || id === "quality-fast" ? "high" : "standard",
    }]);
  }

  function installedImageModeEntries(status) {
    return imageModeEntries(status).filter(([, spec]) => spec.available !== false);
  }

  const IMAGE_MODELS_EMPTY = "No image models are installed. Install them with ops/images-models.ps1 into the server's configured Comfy models directory.";

  async function viewImages() {
    setHeader("images", "Images");
    let data;
    try { data = await api("/images"); } catch (e) { append($app, h("p", { class: "note bad" }, e.message)); return; }
    let gpu = null;
    if (!isMember()) {
      try { gpu = await api("/gpu"); } catch (_) { /* offline */ }
    }
    const prompt = h("textarea", { placeholder: "Describe the image…" });
    const draftKey = "harness.imageDraft";
    try { prompt.value = localStorage.getItem(draftKey) || ""; } catch (_) { /* private mode */ }
    const startWarmup = () => {
      if (route.imageWarmupPromise) return route.imageWarmupPromise;
      if (route.imageWarmupStarted) return Promise.resolve();
      route.imageWarmupStarted = true;
      const request = api("/images/warmup", { method: "POST" }).catch((error) => {
        route.imageWarmupStarted = false;
        throw error;
      }).finally(() => {
        if (route.imageWarmupPromise === request) route.imageWarmupPromise = null;
      });
      route.imageWarmupPromise = request;
      return request;
    };
    prompt.addEventListener("input", () => {
      try { localStorage.setItem(draftKey, prompt.value); } catch (_) { /* ignore */ }
      if (prompt.value.trim()) startWarmup().catch(() => {});
    });
    const modeEntries = installedImageModeEntries(data.status);
    const modes = Object.fromEntries(modeEntries);
    const modelChoices = modeEntries.map(([key, spec]) => ({ key, ...spec, display_name: spec.label || spec }));
    const emptyModels = h("p", { class: "muted small image-models-empty" }, IMAGE_MODELS_EMPTY);
    const model = modeEntries.length ? h("select", { "aria-label": "Model" }, modeEntries.map(([key, spec]) => h("option", {
      value: key,
    }, spec.label || spec))) : null;
    const aspect = h("select", {}, data.status.aspect_ratios.map((a) => h("option", { value: a }, a)));
    let resolutionTouched = false;
    const resolutionInputs = Object.entries(data.status.resolutions).map(([name, spec]) => {
      const input = h("input", { type: "radio", name: "resolution", value: name, checked: name === "standard" });
      input.addEventListener("change", () => { resolutionTouched = true; });
      const size = h("span", { class: "size" });
      const label = h("label", { class: "resolution-option" }, input, spec.label, size);
      return { name, spec, input, size, label };
    });
    const renderResolutions = () => {
      for (const choice of resolutionInputs) {
        const [w, height] = choice.spec.sizes[aspect.value];
        choice.size.textContent = `${w} × ${height}`;
      }
    };
    aspect.addEventListener("change", renderResolutions);
    if (model) {
      model.addEventListener("change", () => {
        if (!resolutionTouched) {
          const recommended = modes[model.value]?.resolution
            || (model.value === "quality" || model.value === "quality-fast" ? "high" : "standard");
          const choice = resolutionInputs.find((c) => c.name === recommended);
          if (choice) choice.input.checked = true;
        }
        renderResolutions();
      });
    }
    renderResolutions();
    const upscaleInfo = data.status.upscale || {};
    const upscale = h("select", {},
      h("option", { value: "none", selected: true }, "Don't upscale"),
      h("option", { value: "2x", disabled: !upscaleInfo.available }, "Upscale 2× after generate"),
      h("option", { value: "4x", disabled: !upscaleInfo.available }, "Upscale 4× after generate"));
    const phase = imageStatusView(data.status);
    const grid = h("div", { class: "image-grid" });
    const imageGridKey = (img) => [img.id, img.status, img.error, img.prompt, img.finished_at].join("\0");
    const render = (d) => {
      if (d.status.phase === "idle" && !route.imageWarmupPromise) route.imageWarmupStarted = false;
      updateImageStatusView(phase, d.status);
      const keys = d.images.map(imageGridKey).join("\n");
      if (grid.dataset.keys !== keys) {
        grid.dataset.keys = keys;
        fill(grid, d.images.map(imageCard));
      }
      return d;
    };
    render(data);
    const go = h("button", { class: "btn primary", type: "submit" }, "Generate");
    const gpuHold = () => !!(gpu && (gpu.manual || gpu.state !== "clear"));
    const syncHoldUi = () => {
      const queued = gpuHold();
      go.textContent = queued ? "Queue Generation" : "Generate";
      go.classList.toggle("primary", !queued);
      go.classList.toggle("queued", queued);
      go.disabled = !model;
    };
    syncHoldUi();
    const upload = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/jpg", hidden: true, "aria-label": "Upload a photo to edit" });
    const uploadBtn = h("button", { class: "btn", type: "button", onclick: () => upload.click() }, "Upload photo");
    upload.addEventListener("change", async () => {
      const file = upload.files?.[0];
      upload.value = "";
      if (!file) return;
      if (!(await confirmGpuQueue("This image edit"))) return;
      try {
        const body = new FormData();
        body.append("file", file);
        const job = await api("/images/uploads", { method: "POST", body });
        location.hash = `#/images/${job.id}/edit`;
      } catch (err) { toast(err.message); }
    });
    const edit = data.status.edit || {};
    const editHint = (!edit.available || !edit.enabled) && !isGuest()
      ? h("p", { class: "muted small" }, edit.setup || "Masked editing is an optional component.")
      : null;
    const loadNote = "The language model is unloaded while images generate; running tasks pause for a few minutes. ";
    const upscaleNote = loadNote + (upscaleInfo.available
      ? "Upscaling is off unless you choose 2× or 4×."
      : "Real-ESRGAN weights are not installed, so 2×/4× upscaling is unavailable.");
    const uploadControls = edit.available && edit.enabled ? [upload, uploadBtn] : [];
    append($app, 
      isGuest() ? h("p", { class: "muted small" }, "Demo access can view generated images, not start new ones.") : h("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          if (!prompt.value.trim()) return toast("Describe the image first");
          if (!model) return toast(IMAGE_MODELS_EMPTY);
          if (!modelChoices.some((m) => m.key === model.value)) return toast(IMAGE_MODELS_EMPTY);
          if (!(await confirmGpuQueue("This image job"))) return;
          go.disabled = true;
          try {
            await startWarmup().catch(() => {});
            const resolution = resolutionInputs.find((choice) => choice.input.checked).name;
            await api("/images", { method: "POST", body: { prompt: prompt.value, model: model.value, aspect_ratio: aspect.value, resolution, upscale: upscale.value } });
            try { localStorage.removeItem(draftKey); } catch (_) { /* ignore */ }
            render(await api("/images"));
          } catch (err) { toast(err.message); }
          syncHoldUi();
        },
      },
      h("label", {}, "Prompt"), prompt,
      h("div", { class: "row" }, h("div", { style: "flex:2" }, h("label", {}, "Model"), model || emptyModels),
        h("div", { style: "flex:1" }, h("label", {}, "Aspect ratio"), aspect)),
      h("div", { class: "resolution-group" }, h("div", { class: "field-label" }, "Resolution"),
        h("div", { class: "resolution-options" }, resolutionInputs.map((choice) => choice.label))),
      h("div", { class: "row" }, h("div", { style: "flex:1" }, h("label", {}, "Upscale"), upscale)),
      h("p", { class: "muted small" }, upscaleNote),
      editHint,
      h("div", { class: "row image-generate-row", style: "margin-top:12px" },
        uploadControls,
        h("span", { class: "spacer" }), go)),
      phase, grid);
    let timer = 0;
    const tick = async () => {
      try {
        const d = render(await api("/images"));
        timer = setTimeout(tick, IMAGE_BUSY.has(d.status.phase) ? 400 : 4000);
      } catch (err) {
        console.debug("image list unavailable; retrying", err);
        timer = setTimeout(tick, 4000);
      }
    };
    timer = setTimeout(tick, IMAGE_BUSY.has(data.status.phase) ? 400 : 4000);
    onLeave(() => clearTimeout(timer));
  }

  function imageMetaParts(img, when) {
    const meta = [`${img.model} · ${img.width}×${img.height}`];
    if (Number(img.scale) > 1) meta.push(`${img.scale}× ${img.upscale_model || "Real-ESRGAN"}`);
    meta.push(`seed ${img.seed}`, img.source);
    if (img.seconds) meta.push(`${Math.round(img.seconds)} s`);
    if (img.lora) meta.push(`LoRA ${img.lora}`);
    if (img.lora_revision) meta.push(img.lora_revision.slice(0, 8));
    meta.push(when);
    return meta;
  }

  function provenanceNote(img) {
    const p = img.provenance;
    if (!p || !(p.checkpoint_revision || p.steps)) return null;
    return h("p", { class: "muted small" },
      [p.mode || img.model, p.steps && `${p.steps} steps`,
        p.sampler, p.scheduler, p.guidance != null && `cfg ${p.guidance}`,
        p.checkpoint_revision && `ckpt ${String(p.checkpoint_revision).slice(0, 12)}`,
        p.comfy_revision && `ComfyUI ${p.comfy_revision}`].filter(Boolean).join(" · "));
  }

  function imageActionRow(img, id, { editControl, canUpscale, startUpscale }) {
    return h("div", { class: "row" },
      !isGuest() && img.status === "done" && (img.operation || "generate") === "generate" ? h("button", {
        class: "btn",
        onclick: async () => {
          try {
            const again = await api("/images", { method: "POST", body: { prompt: img.prompt, model: img.model, aspect_ratio: img.aspect_ratio, resolution: img.resolution } });
            location.hash = `#/images/${again.id}`;
          } catch (e) { toast(e.message); }
        },
      }, "Another one") : null,
      editControl,
      canUpscale ? h("button", { class: "btn", onclick: startUpscale("2x") }, "Upscale 2×") : null,
      canUpscale ? h("button", { class: "btn", onclick: startUpscale("4x") }, "Upscale 4×") : null,
      img.status === "done" ? h("button", { class: "btn", onclick: () => downloadDaemonFile(`/images/${id}.png`, `${id}.png`) }, "Download") : null,
      !isGuest() && (img.status === "queued" || img.status === "running") ? h("button", {
        class: "btn",
        onclick: async () => {
          if (!confirm("Cancel this image job?")) return;
          try { await api(`/images/${id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
        },
      }, "Cancel") : null,
      !isGuest() ? h("button", {
        class: "btn danger",
        onclick: async () => {
          if (!confirm("Delete this image from the live gallery? Independent backups are not changed.")) return;
          try { await api(`/images/${id}`, { method: "DELETE" }); go("#/images", true); } catch (e) { toast(e.message); }
        },
      }, "Delete") : null,
      img.session_id ? h("a", { class: "btn", href: `#/s/${img.session_id}` }, "Open session") : null);
  }

  async function viewImage(id) {
    setHeader("images", "Image", { page: true });
    const load = async () => {
      const img = await api(`/images/${id}`);
      const when = img.finished_at ? ago(img.finished_at) : ago(img.created_at);
      const edit = (img.service?.edit) || {};
      const sizeOk = img.editable !== false;
      const editReady = !isGuest() && img.status === "done" && edit.enabled && edit.available;
      const canEdit = editReady && sizeOk;
      const editBlockedReason = img.editable_reason || "This source is too large to edit. Use the original or a non-upscaled image.";
      const meta = imageMetaParts(img, when);
      const canUpscale = img.status === "done" && !isGuest() && !img.private && Number(img.scale || 1) === 1;
      const startUpscale = (choice) => async () => {
        try {
          if (!(await confirmGpuQueue("This upscale job"))) return;
          const next = await api(`/images/${id}/upscale`, { method: "POST", body: { upscale: choice } });
          location.hash = `#/images/${next.id}`;
        } catch (e) { toast(e.message); }
      };
      const noteClass = `note${img.status === "failed" || img.status === "cancelled" ? " bad" : ""}`;
      const imageNote = () => {
        if (img.status === "failed") return `Failed: ${img.error}`;
        return img.status === "cancelled" ? "Cancelled" : imageStatusView(img.service);
      };
      let editControl = null;
      if (canEdit) editControl = h("a", { class: "btn", href: `#/images/${id}/edit` }, "Edit");
      else if (editReady) editControl = h("button", { class: "btn", type: "button", disabled: true, title: editBlockedReason }, "Edit");
      fill($app,
        img.status === "done" ? h("a", { href: `#/images/${id}/full` }, daemonImage(`/images/${id}.png`, { class: "image-full", alt: img.prompt }))
          : h("p", { class: noteClass }, imageNote()),
        h("div", { class: "card" },
          h("p", {}, img.prompt),
          h("p", { class: "muted small" }, meta.join(" · ")),
          provenanceNote(img),
          img.parent?.id ? h("p", { class: "muted small" }, "Derived from ",
            h("a", { href: `#/images/${img.parent.id}` }, `${img.parent.width}×${img.parent.height}`)) : null,
          (img.children || []).length ? h("p", { class: "muted small" }, "Derived: ",
            ...(img.children.flatMap((c, i) => [i ? ", " : "", h("a", { href: `#/images/${c.id}` },
              c.operation === "upscale" ? `${c.scale}×` : c.operation)]))) : null,
          !isGuest() && (!edit.enabled || !edit.available) ? h("p", { class: "muted small" }, edit.setup || "") : null,
          imageActionRow(img, id, { editControl, canUpscale, startUpscale })));
      return img;
    };
    let img = await load();
    const timer = setInterval(async () => {
      if (img.status === "done" || img.status === "failed" || img.status === "cancelled") return clearInterval(timer);
      try { img = await load(); } catch (_) { /* offline */ }
    }, 400);
    onLeave(() => clearInterval(timer));
  }

  function maskEditor(width, height, previewImg) {
    const canvas = h("canvas", {
      class: "mask-canvas", width, height, "aria-label": "Edit mask",
    });
    canvas.style.width = "100%";
    canvas.style.height = "auto";
    canvas.style.touchAction = "none";
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, width, height);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    let mode = "draw";
    let size = Math.max(12, Math.round(Math.min(width, height) / 24));
    let drawing = false;
    const pos = (ev) => {
      const r = canvas.getBoundingClientRect();
      return [(ev.clientX - r.left) * (canvas.width / r.width), (ev.clientY - r.top) * (canvas.height / r.height)];
    };
    const paint = (x, y) => {
      ctx.strokeStyle = mode === "draw" ? "#fff" : "#000";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = size;
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(x, y, size / 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(x, y);
    };
    canvas.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      canvas.setPointerCapture(ev.pointerId);
      drawing = true;
      const [x, y] = pos(ev);
      ctx.beginPath();
      ctx.moveTo(x, y);
      paint(x, y);
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (!drawing) return;
      ev.preventDefault();
      const [x, y] = pos(ev);
      paint(x, y);
    });
    const stop = (ev) => {
      if (!drawing) return;
      drawing = false;
      try { canvas.releasePointerCapture(ev.pointerId); } catch (_) { /* already released */ }
    };
    canvas.addEventListener("pointerup", stop);
    canvas.addEventListener("pointercancel", stop);
    const tools = {
      setMode(next) { mode = next; },
      setSize(next) { size = Math.max(2, Number(next) || size); },
      clear() { ctx.fillStyle = "#000"; ctx.fillRect(0, 0, width, height); },
      invert() {
        const data = ctx.getImageData(0, 0, width, height);
        for (let i = 0; i < data.data.length; i += 4) {
          data.data[i] = 255 - data.data[i];
          data.data[i + 1] = 255 - data.data[i + 1];
          data.data[i + 2] = 255 - data.data[i + 2];
        }
        ctx.putImageData(data, 0, 0);
      },
      preview(on) { canvas.classList.toggle("mask-preview", on); previewImg.classList.toggle("mask-preview-source", on); },
      blob() { return new Promise((resolve) => canvas.toBlob(resolve, "image/png")); },
      canvas,
    };
    return tools;
  }

  async function viewImageEdit(id) {
    if (isGuest()) { go(`#/images/${id}`, true); return; }
    setHeader("images", "Edit", { page: true });
    const img = await api(`/images/${id}`);
    const edit = (img.service?.edit) || {};
    if (img.status !== "done") { go(`#/images/${id}`, true); return; }
    if (!edit.enabled || !edit.available) {
      append($app, h("p", { class: "note" }, edit.setup || "Masked editing is not installed."),
        h("a", { class: "btn", href: `#/images/${id}` }, "Back"));
      return;
    }
    if (img.editable === false) {
      append($app, h("p", { class: "note" },
        img.editable_reason || "This source is too large to edit. Use the original or a non-upscaled image."),
        h("a", { class: "btn", href: `#/images/${id}` }, "Back"));
      return;
    }
    const source = daemonImage(`/images/${id}.png`, { class: "mask-source", alt: img.prompt });
    const waitForImage = () => new Promise((resolve, reject) => {
      if (source.complete && source.naturalWidth) return resolve();
      source.addEventListener("load", () => resolve(), { once: true });
      source.addEventListener("error", () => reject(new Error("Could not load the source image")), { once: true });
    });
    try { await waitForImage(); } catch (e) { append($app, h("p", { class: "note bad" }, e.message)); return; }
    const width = source.naturalWidth || img.width;
    const height = source.naturalHeight || img.height;
    const editor = maskEditor(width, height, source);
    const prompt = h("textarea", { placeholder: "Describe the edit…" });
    const brush = h("input", { type: "range", min: "4", max: "96", value: String(Math.max(12, Math.round(Math.min(width, height) / 24))), "aria-label": "Brush size" });
    brush.addEventListener("input", () => editor.setSize(brush.value));
    const feather = h("input", { type: "range", min: "0", max: "32", value: "0", "aria-label": "Feather" });
    const draw = h("button", { class: "btn selected", type: "button", onclick: () => { editor.setMode("draw"); draw.classList.add("selected"); erase.classList.remove("selected"); } }, "Draw");
    const erase = h("button", { class: "btn", type: "button", onclick: () => { editor.setMode("erase"); erase.classList.add("selected"); draw.classList.remove("selected"); } }, "Erase");
    const preview = h("label", { class: "row" }, h("input", { type: "checkbox", onchange: (e) => editor.preview(e.target.checked) }), " Preview mask");
    const go = h("button", { class: "btn primary", type: "submit" }, "Edit");
    append($app, 
      h("p", { class: "muted small" }, `White is edited, black is preserved · ${width}×${height}`),
      h("div", { class: "mask-stage" }, source, editor.canvas),
      h("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          if (!prompt.value.trim()) return toast("Describe the edit first");
          if (!(await confirmGpuQueue("This image edit"))) return;
          go.disabled = true;
          try {
            const mask = await editor.blob();
            if (!mask) throw new Error("Could not read the mask");
            const body = new FormData();
            body.append("prompt", prompt.value);
            body.append("feather", feather.value || "0");
            body.append("mask", mask, "mask.png");
            const job = await api(`/images/${id}/edit`, { method: "POST", body });
            location.hash = `#/images/${job.id}`;
          } catch (err) { toast(err.message); }
          go.disabled = false;
        },
      },
      h("div", { class: "row mask-tools" }, draw, erase,
        h("button", { class: "btn", type: "button", onclick: () => editor.clear() }, "Clear"),
        h("button", { class: "btn", type: "button", onclick: () => editor.invert() }, "Invert")),
      h("label", {}, "Brush size"), brush,
      h("label", {}, "Feather (defaults to 0)"), feather,
      preview,
      h("label", {}, "Edit prompt"), prompt,
      h("div", { class: "row", style: "margin-top:12px" }, h("span", { class: "spacer" }), go)));
  }

  async function viewImageFull(id) {
    const img = await api(`/images/${id}`);
    if (img.status !== "done") { go(`#/images/${id}`, true); return; }
    setHeader("images", "Image", { page: true });
    fill($app, h("div", { class: "image-viewer" },
      daemonImage(`/images/${id}.png`, { alt: img.prompt })));
  }

  return { viewImages, viewImage, viewImageEdit, viewImageFull };
}
