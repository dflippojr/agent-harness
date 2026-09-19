# Issue #13: Qwen-Image-2512 Lightning 4-step (`quality-fast`)

Opt-in mode next to unchanged `fast` (Z-Image-Turbo, 8 steps) and `quality` (Qwen-Image-2512, 50 steps, cfg 4).
The Lightning path reuses the installed Qwen FP8 UNET, Qwen 2.5-VL encoder, and Qwen VAE. The only extra asset is
the Apache-2.0 LoRA from [lightx2v/Qwen-Image-2512-Lightning](https://huggingface.co/lightx2v/Qwen-Image-2512-Lightning),
documented by [QwenLM/Qwen-Image](https://github.com/QwenLM/Qwen-Image) and ComfyUI's native
[Qwen-Image-2512 template](https://docs.comfy.org/tutorials/image/qwen/qwen-image-2512).

## Pinned LoRA

| Field | Value |
| --- | --- |
| Repo | `lightx2v/Qwen-Image-2512-Lightning` |
| Revision | `a52649c9d0f6e1a248bff13f0df33bb8a2abdb52` |
| File | `Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors` |
| Bytes | 1698951104 |
| SHA-256 | `ad12117461cb41e2ea637fec8df6392ce8e8550c47fbe2b829ed3deb98262066` |
| License | Apache-2.0 |
| Default path | `{images.models_dir}/loras/` (default `C:/AI/comfy-models/loras/`) |

`python -m harness.doctor` warns (does not fail) when the file is missing or the size is wrong, and prints the
download URL. Missing LoRA disables only `quality-fast`; `fast` and `quality` keep working. Requesting `quality-fast`
without the file returns an error instead of running 50-step `quality`.

## Official 4-step graph (from `image_qwen_Image_2512.json`)

- `LoraLoaderModelOnly`: filename above, `strength_model` 1
- `ModelSamplingAuraFlow` shift 3.1 (same as `quality`)
- `KSampler`: 4 steps, cfg 1, `euler` / `simple`, denoise 1
- Same Qwen native resolutions as `quality`

## Comparison protocol

Run against a daemon that already has images enabled and the LoRA installed:

```
python -m bakeoff.compare_qwen_lightning --out issue-13-lightning-outputs
```

Ten prompts cover people, landscape, illustration, small text, dense text, 9:16, 2:3, interior, and fur detail.
Each mode is submitted as one batch so the first job is cold (GPU hand-over + ComfyUI) and the rest are warm.
The script records per-job `seconds`, wall time, bytes, LoRA revision/hash, and peak `nvidia-smi` VRAM.

## Live tower results

Not run yet. On this checkout the pinned LoRA was not present under `models_dir/loras/` (ComfyUI's
`extra_model_paths.yaml` already maps `loras: loras`). After merging, download the file from the URL in
`python -m harness.doctor`, restart the daemon, then:

```
python -m bakeoff.compare_qwen_lightning --out issue-13-lightning-outputs
```

Paste the summary table and a visual note before closing:

- Confirm 4-step is useful as a speed/quality tradeoff (especially portraits and landscapes).
- Confirm it does not replace `quality` for small/dense text.
- Record any regressions (text errors, plastic skin, composition collapse) against the same seed 50-step images.
