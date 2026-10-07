# Issue #176: Qwen-Image-2.1 study

**Verdict: no-go at gate 1 (license).** The Qwen Research License grants use for "research or evaluation purposes
only". Ongoing personal self-hosted use as a harness image model is neither. Per decision 5 of the issue, the study
stops after the license read-out. The runtime-fit and quality-grid stages did not run. No weights were downloaded, no
GPU hold was taken, and nothing in `images` settings, ComfyUI or `C:/AI/comfy-models` changed.

## Candidate

| Field | Value |
| --- | --- |
| Repo | [`Qwen/Qwen-Image-2.1`](https://huggingface.co/Qwen/Qwen-Image-2.1) |
| Revision read | `d26bb61231c349cf6b7896fa83353113880e1ba3` (last modified 2026-09-30) |
| Gated | No |
| Runtime | Diffusers only (`QwenImage21Pipeline`, needs diffusers from git and transformers >= 5.17) |
| Size | 7B DiT ("visual generation component"), BF16, text encoder in 4 shards, transformer in 2 shards |
| Resolutions | 2048x2048 up to 2752x1536 (7 aspect ratios), 40 steps in the card's examples |
| Features | Text-to-image, editing with up to 10 references, native RGBA |
| License | `license: other`, `license_name: qwen-research`, [LICENSE](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE) |

The card facts in the issue body check out, except one: there is no ComfyUI path in the repo. A go would have needed
a separate Diffusers runtime next to ComfyUI.

## Current models, for contrast

From `config/harness.yaml`, `docs/issue-13-lightning-results.md` and `docs/flux-fast.md`. All are Apache 2.0:

| Mode | Model |
| --- | --- |
| `fast` | Z-Image-Turbo |
| `quality` | Qwen-Image-2512 |
| `quality-fast` | Qwen-Image-2512 plus the lightx2v Lightning LoRA |
| `flux-fast` (optional) | FLUX.2 [klein] 4B FP8 |
| Masked edit (optional) | Qwen-Image-Edit fp8 |

## Gate 1: license read-out

Owner pre-approval (decision 1, 2026-09-24): personal, self-hosted use with no redistribution. The question is only
whether the license text permits that.

Text read: "Qwen RESEARCH LICENSE AGREEMENT, Release Date: September 20, 2026", licensor Hangzhou Tongyi Laboratory
Technology Co., Ltd.

| Clause | Text (quoted) | Effect on personal self-hosted use |
| --- | --- | --- |
| 1(i) | "'Non-Commercial' shall mean for research or evaluation purposes only." | The permitted field is research or evaluation. It is not "not for profit". |
| 2(a) | grants rights to use the Materials "FOR NON-COMMERCIAL PURPOSES ONLY" | Use is limited to the 1(i) field. |
| 2(b) | "You shall not use the Materials for any commercial purpose without obtaining a separate commercial license" | A commercial license is available from model-business@notice.qwencloud.com. |
| 9(a) | "You shall request a separate license from us, if you use the Materials in ways not expressly agreed to in this Agreement." | Personal everyday use is not named, so it needs a separate license. |
| 7(b) | on termination "you must delete and cease use of the Materials" | Weights already in use could be revoked. |
| 8 | PRC law, People's Courts in Hangzhou | Noted only. |

Reading: the grant does not turn on money or redistribution. It turns on purpose. Generating images through the
harness for the owner's day-to-day use is production use. It is not research and not evaluation, so 2(a) does not
cover it and 9(a) sends it to a separate license. "No redistribution" does not help, because the limit is in 2(a),
not 3. The same definition appears in the 2024 Qwen Research License (for example on
`Qwen/Qwen2.5-3B-Instruct`), so this is the license's standing meaning, not a new drafting slip.

Two more points, both noted in the 2026-09-24 audit:

- The owner's account uses a company email domain. Any work use would also count as commercial under 2(b).
- This study itself counts as evaluation, so running the gates is allowed. A "go" could not be acted on, though,
  because adopting the model is the use the license does not grant. Spending a GPU night on stages 2 and 3 would
  measure a model that cannot ship. So the issue's stop-at-first-failed-gate rule applies.

**Gate 1: FAIL.** The license does not permit the pre-approved use (personal self-hosted production use).

## Gates 2 and 3 (not run)

These were fixed before any run (decision 2) and are recorded here so a rerun does not tune them:

- **Speed:** at most 2x the default model's seconds per image. Follow #13's method: per-job `seconds` from the
  daemon, warm model (one discarded warm-up job per mode), fixed seed, and the same resolution and steps per aspect
  ratio for both sides.
- **VRAM:** before taking the hold, read free VRAM with llama-server loaded. Under the hold, record the candidate's
  peak `nvidia-smi` VRAM. It passes only if the peak is at or below that free figure (audit flaw 2).
- **Quality:** 10 fixed prompts (typography, portraits, fine detail, each aspect ratio in use), anonymized A/B pairs
  and a key. The owner judges later. The gate needs a preference on at least 6 of 10, and the doc ships with that
  verdict pending (audit flaw 3).

Stages left after these (editing vs Qwen-Image-Edit fp8, RGBA usefulness, load/unload/disk effects) also did not run.

## What would reopen this

Any of these would make the study worth running. Each one is an owner decision, not something an agent can do:

1. A separate license from Qwen (2(b) or 9(a)) that covers personal self-hosted use.
2. A later release of Qwen-Image-2.1 under Apache 2.0, like Qwen-Image-2512. Check the `license` field on the
   Hugging Face card.
3. The owner deciding that the study should count as evaluation only, with no intent to adopt the model. In that
   case the result is still a no-go for adoption.

If reopened, the runtime will be the main cost. The Diffusers-only pipeline needs its own venv and process outside
ComfyUI, and the card's 2048-pixel, 40-step defaults plus a BF16 7B DiT will need CPU offload on the tower. Gate 2
may fail without an fp8 or GGUF build.

## Recommendation

Do not adopt Qwen-Image-2.1. Keep the current Apache 2.0 models. Do not file a follow-up implementation issue.
