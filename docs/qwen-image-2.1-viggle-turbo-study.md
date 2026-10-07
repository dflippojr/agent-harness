# Issue #226: Qwen-Image-2.1-Viggle-Turbo study

**Verdict: no-go at gate 1 (license).** Viggle-Turbo is a derivative of Qwen-Image-2.1 and ships under the same Qwen
Research License, byte for byte. That license grants use for "research or evaluation purposes only", which #176
([`docs/qwen-image-2.1-study.md`](qwen-image-2.1-study.md), PR #423) already found does not cover personal
self-hosted use as a harness image model. Per decision 1 of the issue, the study stops after the license read-out.
The runtime-fit and quality-grid stages did not run. No weights were downloaded, no GPU hold was taken, and nothing
in `images` settings, ComfyUI or `C:/AI/comfy-models` changed.

## Candidate

Read 2026-10-06 from the Hugging Face API and the files at the pinned revision.

| Field | Value |
| --- | --- |
| Repo | [`Viggle/Qwen-Image-2.1-viggle-turbo`](https://huggingface.co/Viggle/Qwen-Image-2.1-viggle-turbo) |
| Revision read | `009a44a895ef85f7e643c80fdca9543795248867` (last modified 2026-10-01) |
| Gated | No |
| Card metadata | `license: other`, `license_name: qwen-research`, `base_model: Qwen/Qwen-Image-2.1`, `base_model_relation: adapter` |
| Base revision | `Qwen/Qwen-Image-2.1` at `d26bb61231c349cf6b7896fa83353113880e1ba3`, still `qwen-research` (same revision #176 read) |
| Current version | v0.3 (2026-09-29), 6 steps, no CFG. v0.2.1, v0.2 and the v0.1 4-step adapter are still in the repo |
| Recommended adapter | `Qwen-Image-2.1-viggle-turbo-v0.3-6step-lora-r256.safetensors`, 1,359,147,904 bytes, LFS sha256 `f06c266e04438b5272bdfb99410421d52a65d7a37f6f42aabc3cb1faf0142644` |
| ComfyUI adapter | `Qwen-Image-2.1-viggle-turbo-v0.3-6step-lora-r128.safetensors`, 679,604,800 bytes, LFS sha256 `0c98591700346f9777051d4e6fa29aa94519abec0d85b1f1f672a2a3db8c94b3` |
| Other files | Merged single-file transformers (int8 convrot, fp8 e4m3fn, GGUF Q8_0 to Q4_K_M, 4.3 to 7.7 GB), peft adapters, a modified scheduler config, ComfyUI custom nodes (`comfyui/viggle_turbo.py`) and workflows |

### Card facts in the issue, checked

The issue body was written against v0.2.1. The card has moved on:

- The current release is **v0.3**, not v0.2.1. Both are 6-step LoRAs at rank 256 (1.3 GB) and rank 128 (0.7 GB), so
  the sizes in the issue still hold.
- Text-to-image and editing with 1 to 3 references: confirmed. "About 5x faster than the 40-step base": still the
  card's claim. v0.3 adds a 9-step mode (7 turbo steps, then 2 base steps) at about 1.4 to 1.5x the 6-step time,
  diffusers and the demo Space only.
- ComfyUI: needs ComfyUI 0.37.0 or later (native Qwen-Image-2.1 support) plus Viggle's custom nodes. The author calls
  the port "mostly vibe-coded" and says to "expect rough edges". The default workflow (int8 base and text encoder,
  r128 LoRA, prompt enhancer on) peaks at about 26 GB VRAM at 1248x832. All of these are publisher claims, not
  measurements.
- The repo carries only the transformer (modified) and adapters. Text encoder, VAE and processor load from
  `Qwen/Qwen-Image-2.1` or its Comfy-Org repack, so any run also uses the base model's own files directly.

## Gate 1: license read-out

Owner pre-approval (decision 1, carried over from #176): personal, self-hosted use with no redistribution.

| Evidence | Finding |
| --- | --- |
| `LICENSE` | "Qwen RESEARCH LICENSE AGREEMENT, Release Date: September 20, 2026". SHA-256 `8dc973f024ff95966bea25866efa443fd16776dcb1001e681e3d467ea572b28d`, identical to the `LICENSE` at the base revision #176 read. |
| `NOTICE` | "This repository is a derivative work of Qwen/Qwen-Image-2.1, produced by Viggle. Built with Qwen." It carries the 3(c) attribution line and lists every added or modified file. |
| Card, License section | "distributed under the Qwen RESEARCH LICENSE AGREEMENT: non-commercial use only, research or evaluation purposes. Commercial use requires a separate licence from the licensor." |

Does the derivative inherit the base terms? Yes, on three separate grounds:

1. **Viggle chose the same terms.** Clause 3(d) would let Viggle add its own terms for its modifications, but only
   while still complying with the agreement. Viggle added none and relicensed under the Qwen Research License as is.
2. **The weights are still Qwen Materials.** The merged transformers are the base transformer with the adapter folded
   in. The LoRA files only work applied to that transformer. Under 2(a) and 3, derivative works stay "subject to
   Section 2", so the "for non-commercial purposes only" limit follows them.
3. **The base files are needed at runtime.** The text encoder and VAE come straight from `Qwen/Qwen-Image-2.1`, so
   using Viggle-Turbo means using the base Materials under their own license whatever the adapter's terms were.

So clauses 1(i), 2(a), 2(b), 7(b) and 9(a) apply exactly as #176 quoted them. The purpose test is the same:
generating images through the harness for the owner's day-to-day use is production use, not research or evaluation,
and 9(a) sends any use not expressly granted to a separate license. "No redistribution" does not help, because the
limit is in 2(a), not 3. The company email domain point from #176 (any work use is commercial under 2(b)) applies too.

**Gate 1: FAIL.** The license does not permit the pre-approved use. The speed-up does not change the terms.

## Gates 2 and 3 (not run)

The gates fixed by decision 2 are unchanged from #176 and recorded there so a rerun does not tune them: peak VRAM at
or below free VRAM with llama-server loaded, at most 2x the default model's seconds per image (#13's method), and an
owner preference on at least 6 of 10 anonymized same-seed prompts against Qwen-Image-2.1 and the current fast/default
model. Editing against Qwen-Image-Edit fp8 (#88) was gated behind those and also did not run.

For a rerun, two card facts already point at gate 2 risk:

- The ComfyUI default path peaks at about 26 GB VRAM at 1248x832, before llama-server. It would need the GGUF
  single-file transformers (4.3 to 7.7 GB) and probably a smaller text encoder to fit beside llama-server.
- The Viggle custom nodes would have to be installed into the production ComfyUI, which is a production change this
  issue rules out. A rerun would need a separate ComfyUI instance or an owner decision.

## What would reopen this

Each of these is an owner decision, not something an agent can do:

1. A separate license from Qwen (2(b) or 9(a)) covering personal self-hosted use of Qwen-Image-2.1 and derivatives.
2. A later release of Qwen-Image-2.1 under Apache 2.0, followed by Viggle relicensing the turbo files. Check the
   `license` field on both cards: the base alone is not enough, since Viggle chose the research license for its own
   files.
3. The owner deciding the study should count as evaluation only, with no intent to adopt. The result is still a
   no-go for adoption.

## Recommendation

Do not adopt Qwen-Image-2.1-Viggle-Turbo. Keep the current Apache 2.0 models (Z-Image-Turbo, Qwen-Image-2512 with and
without the Lightning LoRA, FLUX.2 [klein] 4B FP8, Qwen-Image-Edit fp8). Do not file a follow-up implementation issue.
If a few-step Qwen model is still wanted, the existing `quality-fast` mode (Qwen-Image-2512 plus Lightning, #13)
already fills that role under Apache 2.0.
