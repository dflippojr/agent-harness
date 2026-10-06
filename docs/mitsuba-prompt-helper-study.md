# Mitsuba-ComfyUI-27B as an Images prompt-writer and image describer (issue #307)

Research only. Status: **protocol committed before any run** (this section is frozen; results are appended below it).

## Protocol (frozen before the run)

- Machine: the tower, GPU hold on, always-on Qwen on 8090 paused by the hold. Candidate and the comparison Qwen are served
  on the bakeoff port **8081** only. No change to `config/harness.yaml`, the always-on server, installers or
  `harness_modules/images/`.
- Context size: 8192 for every server (the task needs well under 2K tokens). RAM is watched continuously; the run stops
  and deletes the candidate if free RAM drops below 2 GB (the failure that stopped #170).
- Images: ComfyUI through its normal HTTP API (`/prompt`, `/history`, `/view`) with the repo's `flux-fast` graph
  (`harness_modules.images.service.workflow`, imported read-only), `standard` 1024x1024, **seed 307**, installed models only.
- Variants per prompt: (a) raw prompt; (b) current local Qwen (Qwen3.6-35B-A3B UD-Q4_K_XL, the bakeoff profile) with the
  system message below, thinking off; (c) Mitsuba v1.18 `PQ2_0` with the same system message, thinking off.
- Describe image: 5 of the generated images captioned by Mitsuba + mmproj; quality notes only (Qwen has no vision).
- Go criterion (fixed): go only if (c) is preferred over (b) on at least 6 of 10 prompts **and** it adds under 15 s to a
  generation on the GPU path without an extra swap. If (b) is preferred on 5 or more: "use Qwen with a system prompt; no
  new model". The agent records its own preference; the owner visual check can overturn it.

### System message (identical for (b) and (c))

```
You rewrite rough image ideas into one detailed text-to-image prompt for a FLUX model. Output only the final prompt as a
single paragraph, 60 to 120 words. Keep every subject, every piece of quoted text (verbatim, in quotes) and every
layout instruction from the user. Describe subject, setting, lighting, camera or art style and colour. Do not add
explanations, headings, lists or negative prompts.
```

### The 10 synthetic prompts

| # | Rough prompt | Kind |
| --- | --- | --- |
| 1 | a lighthouse on a cliff at dusk | plain |
| 2 | corgi astronaut floating in a kitchen | plain |
| 3 | a bakery shop front with a sign that says "Hot Bread" | text |
| 4 | poster for a jazz night, big title "BLUE HOUR", small line "Fridays 8pm" | text |
| 5 | a coffee mug with the words "Monday Again" printed on it, on a wooden desk | text |
| 6 | three red apples in a row on a white table, left one largest, right one smallest | layout |
| 7 | split image: left half a snowy forest, right half a desert at noon | layout |
| 8 | an old woman knitting by a window in the rain | plain |
| 9 | futuristic night market in a flooded city, neon reflections | plain |
| 10 | a watercolor fox sleeping in autumn leaves | plain |

(3 text-in-image: #3, #4, #5. 2 strict layout: #6, #7. No owner prompts or private data.)
