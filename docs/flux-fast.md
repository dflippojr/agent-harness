# Optional `flux-fast` image mode (issue #92)

Apache-2.0 distilled **FLUX.2 [klein] 4B FP8** as a second fast text-to-image style. Z-Image-Turbo stays the `fast`
default. This mode is disabled until its assets and ComfyUI nodes pass preflight, and it never falls back to another
model.

Checked-in code does **not** download weights or change the live ComfyUI path. Installing the component and promoting
a staged ComfyUI build need an explicit owner command (disk, bandwidth, and the working image stack).

## Pins

Manifest: [`harness/images_flux_fast.json`](../harness/images_flux_fast.json). Weights are not in git.

| Asset | Source | Revision | File | Bytes | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| Checkpoint | [`black-forest-labs/FLUX.2-klein-4b-fp8`](https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8) | `5b4408e59397a4a37ccb46afe426d8ed86379441` | `flux-2-klein-4b-fp8.safetensors` | 4,070,624,520 | `97ed34fe…c0ccb6` |
| VAE | [`Comfy-Org/vae-text-encorder-for-flux-klein-4b`](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b) | `5f526678002e43af5551dadb73ce2e8c91b43afe` | `flux2-vae.safetensors` | 336,211,292 | `868fe7b3…ce8f3` |
| Encoder | same Comfy-Org repo | same | `qwen_3_4b.safetensors` | 8,044,982,048 | `6c671498…dfc5a` |

License: [Apache 2.0](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/main/LICENSE.md). Model card:
https://huggingface.co/black-forest-labs/FLUX.2-klein-4B. Official workflow guide:
https://docs.comfy.org/tutorials/flux/flux-2-klein.

The Z-Image `fast` workflow already uses a file named `qwen_3_4b.safetensors`. Install reuses that path only when size
and SHA-256 match the pin. Otherwise the FLUX encoder is stored as `qwen_3_4b_flux2.safetensors` and only the
`flux-fast` graph points at it. Remove never deletes a matching shared encoder.

## Graph

API-format transcription of the **distilled** subgraph in
`Comfy-Org/workflow_templates` `templates/image_flux2_klein_text_to_image.json` at revision
`8f6709b8f6ef808b0eccc47eff28ada4a58adbbe` (the template also contains a muted 20-step base subgraph; this issue does
not use it).

Upstream settings kept: `KSamplerSelect` euler, `Flux2Scheduler` **4** steps, `CFGGuider` **cfg=1.0**,
`ConditioningZeroOut` (no negative prompt), `EmptyFlux2LatentImage`, `CLIPLoader` type `flux2`.

Deliberate deviation: UNET filename is BFL's public FP8 `flux-2-klein-4b-fp8.safetensors` instead of the template's
bf16 `flux-2-klein-4b.safetensors`.

`flux-fast` uses existing `standard` dimensions only. `high` is rejected rather than resized.

## Owner commands

From the repo (tower venv):

```powershell
powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 status flux-fast
powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 install flux-fast
powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 remove flux-fast
```

Or `python -m harness.images_models status|install|remove flux-fast`. Downloads stream to `.part`, resume with
`Range` when the server honors it, verify size and SHA-256, then `os.replace` onto the destination. Free-space
preflight: missing payload + one temp copy of the largest file + 5 GiB reserve. Query strings are never logged.

## ComfyUI

The tower's recorded production build is portable **v0.35.0**. FLUX.2 klein core nodes appear in stable notes from
**v0.26.0**; this repo still confirms classes via a tree scan or `/object_info`, not the version string alone.

Pinned portable to stage (not `latest`, not nightly): **v0.36.0**
`ComfyUI_windows_portable_nvidia.7z`
(`sha256:c3c60192840f8b68c9a47cf3e8161ecb108e5ffdf5ea236c1c72a402e442695d`, 1,917,442,353 bytes).

```powershell
powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 stage-comfyui
# extract the verified 7z into C:\AI\ComfyUI.staged (or pass -ExtractComfyUI 7z)
powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 validate-comfyui
powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 promote-comfyui
powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 rollback-comfyui
```

`stage` writes beside production. `validate` checks `python_embeded` + `ComfyUI/main.py` and node classes for
`fast`, `quality`, and `flux-fast`. `promote` is refused when validation fails, so `C:\AI\ComfyUI` is unchanged.
Promote renames production to `C:\AI\ComfyUI.prev` and the staged tree into `C:\AI\ComfyUI`. `rollback-comfyui` is
the one-command restore.

## Doctor / check-stack

`python -m harness.doctor` and `ops/check-stack.ps1` report flux-fast as a **warning** when the optional component is
missing. Existing `fast`/`quality` checks still fail the stack if those required files are absent.

## Tower exit test (owner go-ahead)

Do not run this from an unattended agent. On the RTX 4070 Ti Super 16 GB tower, after an explicit go-ahead:

1. Record installed bytes and whether `qwen_3_4b.safetensors` was reused.
2. One cold and three warm 1024×1024 successes, a cancel, a forced error, and language-model restore after each batch.
3. The same ten prompts/seeds through current Z-Image `fast` and `flux-fast` (people, typography, product/icon,
   photorealistic, illustration, difficult composition). Keep outputs **outside** the repo.
4. ComfyUI start/load, per-image and end-to-end time, peak VRAM/RAM/commit, output size, CPU offload.
5. One `fast` and one `quality` generation on the staged ComfyUI build, then again after promote.

**Ship only if** no OOM/instability, ≥1 GiB GPU headroom at peak, warmed 1024×1024 median ≤30 s and ≤1.5× the current
Z-Image median, existing modes regress ≤10%, and the comparison is a useful visual alternative.

**If the gate fails:** do not expose the mode or switch production ComfyUI. `rollback-comfyui`, optionally
`remove flux-fast`, record the no-go here, and leave the thresholds alone.

### Fixed comparison suite (prompts + seeds)

| # | Seed | Prompt |
| --- | --- | --- |
| 1 | 92 | A candid photo of two colleagues laughing at a standing desk in a sunlit home office, 35mm, natural skin texture |
| 2 | 92 | A storefront sign that reads AGENT HARNESS in bold sans-serif, dusk, wet pavement reflections |
| 3 | 92 | Flat app icon of a friendly robot mascot, centered, simple shapes, no text |
| 4 | 92 | Photorealistic close-up of a stainless pour-over kettle on walnut, steam, window light |
| 5 | 92 | Watercolor illustration of a coastal lighthouse at dusk, paper grain, limited palette |
| 6 | 92 | Exploded-view product render of a mechanical keyboard, labeled switches, clean studio background |
| 7 | 92 | Crowded night market, dozens of people, hanging lanterns, readable stall signs for tea and noodles |
| 8 | 92 | Technical isometric diagram of a 16 GB GPU handing off from a language model to an image model |
| 9 | 92 | Portrait of an older jazz guitarist on a dim stage, film grain, catchlights, detailed hands |
| 10 | 92 | Children's book spread: a fox and a robot sharing a map, hand lettering that says FIND THE WAY |

### Gate record

| Date | Decision | Notes |
| --- | --- | --- |
| 2026-09-17 | **pending owner go-ahead** | Code and tests landed without downloading weights or changing `C:\AI\ComfyUI`. |

Comparison outputs live outside the repository (suggested: `D:\Agents\harness\flux-fast-gate\`).
