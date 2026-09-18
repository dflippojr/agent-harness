# Phase 6d: image generation

Built 2026-09-15. Decisions (user): images requested from the phone, assets made by agents, and experimenting; install
both a permissive default and a higher-quality model.

## Models (checked on Hugging Face 2026-09-15)

| Slot | Model | License | Files in `C:\AI\comfy-models` | Notes |
| --- | --- | --- | --- | --- |
| `fast` (default, agent assets) | **Z-Image-Turbo** | Apache 2.0, not gated | `z_image_turbo_bf16` (12 GB), `qwen_3_4b` text encoder (7.5 GB), `ae` VAE | 8 steps, fits in VRAM |
| `quality` (phone) | **Qwen-Image-2512** | Apache 2.0, not gated | `qwen_image_2512_fp8_e4m3fn` (20 GB), `qwen_2.5_vl_7b_fp8_scaled` (8.8 GB), `qwen_image_vae` | 50 steps, cfg 4; best text rendering; ComfyUI streams part of it from RAM |

The user allowed a personal-use license for the quality slot, but the best model that works here without extra steps
is Apache-licensed anyway:

- **FLUX.2 [klein] 9B** (non-commercial) is gated (needs a Hugging Face login and license acceptance), so it wasn't
  installed. It can be added later as a third workflow.
- The "Qwen-Image 2.0" some 2026 articles mention couldn't be found on Hugging Face.

ComfyUI: portable NVIDIA build **v0.35.0** (torch 2.13 + CUDA 13.0) in `C:\AI\ComfyUI`, models via
`ComfyUI\extra_model_paths.yaml`. The workflows are transcribed from ComfyUI's bundled templates
(`image_z_image_turbo.json`, `image_qwen_Image_2512.json`; the optional Lightning LoRA is left out).

## Design

- **GPU hand-over** (`harness/images.py`): Qwen (~14.7 GB) and an image model never fit together. A batch of image
  jobs:
  1. waits while the GPU guard is paused;
  2. takes `InferenceGate.acquire_exclusive()` (the model call in flight finishes, agent turns wait, endpoint requests
     get 503 `Retry-After: 60`);
  3. stops llama-server through the guard's pause flag (works even with the guard disabled);
  4. starts ComfyUI as a hidden child process and runs the jobs;
  5. keeps ComfyUI warm for `linger_seconds` (60), then frees and stops it, removes the flag, waits for llama-server's
     `/health`, and releases the gate.

  If a game starts mid-batch, the current job is put back in the queue and the guard's own restore takes over.
- **ComfyUI on demand**, not always on: an idle ComfyUI would still hold a CUDA context on a GPU that's 14.7/16 GB
  full. On daemon start, a ComfyUI left running by a crash is stopped.
- **Phone:** Images screen (`#/images`, button on the session list): prompt, model, aspect ratio, live phase
  ("Unloading the language model…", "Generating… 42 s"), gallery, detail view with "Another one". A notification is
  sent when a phone job finishes. API: `POST /images`, `GET /images`, `GET /images/{id}`, `GET /images/{id}.png`.
- **Agents:** `generate_image(prompt, filename, aspect_ratio, model)` saves a PNG into the session workspace on the
  tower or, for MacBook sessions, copies it there with a `put_file` runner op. No approval needed. The tool
  description warns that it takes minutes.
- **Inputs:** aspect ratio from a fixed list mapped to each model's native sizes; a random seed is recorded per job;
  upscaling is opt-in Real-ESRGAN 2×/4× (Hermes lesson: default-on upscaling degraded text and faces). The original
  PNG is preserved; derived rows record parent, scale, model version, dimensions, duration and bytes.
- **Metrics:** `harness_images_total{model,source,status}`, `harness_images_seconds_total`,
  `harness_images_gpu_taken`, `harness_images_queued`, `harness_images_upscale_available`; dashboard row "Images".
  `ops/check-stack.ps1` checks the ComfyUI install and model files. Doctor warns when optional Real-ESRGAN weights
  are missing.

## Verification

Tests: 3 new (`tests/test_phase6.py`) with a fake ComfyUI and model server:

- a batch with one failing job does a single stop/start hand-over, sizes and step counts per model are right, the
  endpoint is refused during the batch, and the gate is released after;
- the agent tool writes into the workspace, and a path escape is refused;
- API routes and PNG serving work, and the tool is offered to tower and MacBook sessions;
- a MacBook session copies the PNG into the runner workspace through `put_file`.

Live on the tower (2026-09-15):

| Run | Result |
| --- | --- |
| Phone-style job, `fast`, "friendly robot mascot icon…" | Clean flat icon. Submitted 01:50:33; ComfyUI first start ~45 s; generated in 17.7 s; ComfyUI stopped after the 60 s linger (01:52:34); Qwen healthy again 01:53:53 (~3.3 min total GPU hand-over) |
| Agent session `a8acc6ce82` ("README for Invoice Tools with a logo") | 256 s, 3 turns: wrote the README, called `generate_image` (ComfyUI start ~20 s, image ~17 s, linger, Qwen reload ~95 s), confirmed both files. The logo spelled the name "Invoce Tools": the fast model's weak spot is text. |
| `quality`, 16:9 home-lab scene with an "AGENT HARNESS" poster | 183 s for 50 steps at 1664×928 (19.5 GB model partly streamed from RAM); poster text correct; photorealistic |

## Costs and caveats

- Every batch costs about a minute of Qwen reload afterwards, plus a 20–45 s ComfyUI start. Agent sessions pay that
  inside the run. Endpoint clients get 503 during a batch.
- The fast model misspells text; ask for `quality` when words matter.
- Generated images live in `D:\Agents\harness\images-work\images` and are not part of the nightly backup.

## Optional masked editing (issue #88)

Owner-only Agent Harness Web work. Fast/quality text-to-image is unchanged. The optional `image_edit` component uses
the official Apache-2.0 **Qwen-Image-Edit** family through ComfyUI's Qwen edit graph (fp8, same RAM-streaming pattern
as `quality`):

| | |
| --- | --- |
| Source | [Qwen/Qwen-Image-Edit](https://huggingface.co/Qwen/Qwen-Image-Edit) (Apache 2.0) |
| ComfyUI package | [Comfy-Org/Qwen-Image-Edit_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI) revision `7d41107b653d3039be20972fb82398b01b3213eb` |
| Artifact | `qwen_image_edit_fp8_e4m3fn.safetensors` (20,430,635,136 bytes) |
| SHA-256 | `393c6743d1de2e9031b5197027b36116f2096958ccc0223526d34e1860266021` |

It is never downloaded during an ordinary install or daemon upgrade. Enable with `-EnableModules image_edit` after
the installer has checked disk (~22 GB extra), 16 GB GPU, and 32 GB RAM. CLIP (`qwen_2.5_vl_7b_fp8_scaled`) and VAE
(`qwen_image_vae`) are shared with `quality`. Missing edit weights produce setup guidance; Generate still works.

Edits start from a gallery PNG or an owner PNG/JPEG/WebP upload (metadata stripped, orientation/color normalized).
The mask editor paints white=editable / black=preserved at the source's exact pixel size. The source is never
overwritten; the result is a new row with `parent_id`. Uploads/masks/results are owner-private (no guest or app-token
listing, no hosted-provider upload). Delete confirms and removes only the live files for that row.

### 16 GB GPU exit test (required before closing the issue)

Run on the tower after the weights are installed. Record peak VRAM, peak RAM, and end-to-end time for:

1. a small source (around 1024×1024)
2. a larger source (Qwen-Image high-res envelope, e.g. 1664×928)

Keep real paths, logins, tokens, and tailnet names out of public comments.
