# Speculative decoding on the tower's llama-server (#772)

Run 2026-10-29 22:50 to 2026-10-30 00:40 on the RTX 4070 Ti Super 16 GB / 32 GB RAM tower, now on Ubuntu, under the
owner's GPU hold for #772-#774 (one GPU job at a time). Production Qwen (`agent-harness-main-llama.service`, port 8090)
was parked for the whole run (pause flag present, `park.conf` drop-in) and was not touched. A watch on the pause flag
and the production container saw no unpark. Every measurement used a throwaway container on `127.0.0.1:8081`,
stopped after each configuration. Baseline and speculative runs used the same build, model file, flags and sampling
in the same session; no older numbers are reused. Raw output is in the gitignored `runs/ah772/`.

The host was not quiet: other agent sessions ran tests and Playwright (load average 8 to 11, about 6 GB of swap in
use). Absolute tok/s drifted between batches, so comparisons below are within one batch, and the main comparison was
run as two interleaved rounds.

## Verdict: **go**, with a DFlash drafter at `--spec-draft-n-max 4` and `--fit-target 2048`

Follow-up for adoption: #851 (the installer's llama-server arguments and drafter download; `run-qwen.ps1` parity for
Windows). Production config is unchanged; the owner decides there.

- **Decode gain at the profile sampling** (temperature 0.6, top_p 0.95, top_k 20): median **103 to 112 tok/s against
  74.5** on the 8-prompt coding set at 32K context (two rounds, +39% to +50%), **96.0 against 74.9** at the production
  64K context (+28%), and **+11% to +47%** decode after 2K to 56K-token prompts. Greedy gains are similar. Draft
  acceptance is about 64% at n-max 4.
- **Canary hard tasks:** 10/12 for both, the same task failing the same way in both (below). In agent runs the median
  decode rose from 77.7 to 91.3 tok/s and the 12 runs took 490 s instead of 555 s (-12%).
- **Expert spill:** the drafter pushes 3 to 4 more expert layers to system RAM (17-18 overflowing → 20-21). The gain
  survives it.
- **Costs:** prompt processing is slower: 7% to 20% lower tok/s on long prompts (56K tokens: 68.3 s instead of
  60.1 s) and about 25% lower in agent turns (median 360 against 477 tok/s in the canary runs). Peak VRAM is the same as
  today (15,301 vs 15,335 MiB at 64K) because `--fit-target 2048` gives back the room the drafter takes. Without that
  flag the server crashed on its first request (see "Costs").

The draft model of the same family (Qwen3.5-0.8B), DSpark and n-gram lookup all decode **slower** than the baseline
here, as does DFlash at a long draft (n-max 15). With 17+ expert layers in system RAM, verifying a draft of k tokens
reads more experts from RAM, so only a cheap drafter with high acceptance at short drafts wins.

## Build, model and drafters

The issue's Windows build (`C:\AI\llama.cpp\b10950`) and `run-qwen.ps1` no longer apply. Since 2026-10-26 the tower
runs Ubuntu, and production runs llama-server through `install/run-server.sh` in the pinned official image. Decision 3
(no llama.cpp upgrade) therefore pins the build the tower actually runs.

| | |
| --- | --- |
| llama.cpp | Docker image `ghcr.io/ggml-org/llama.cpp:server-cuda-b10830`, `llama-server --version`: build 10830, commit `465e49b9c` (the production unit's image) |
| Main model | `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` from `unsloth/Qwen3.6-35B-A3B-GGUF` (revision `a483e9e6cbd595906af30beda3187c2663a1118c`), 22,360,456,160 bytes, SHA-256 `707a55a8a4397ecde44de0c499d3e68c1ad1d240d1da65826b4949d1043f4450` (computed locally, matches the model page). Architecture `qwen35moe`: 40 blocks (30 Gated DeltaNet + 10 full attention), 256 experts, 8 routed, vocab 248,320, pre-tokenizer `qwen35` |
| Production flags | `--ctx-size 65536 --flash-attn on --parallel 1 --jinja --metrics --sleep-idle-seconds 600 --fit on --cache-type-k q8_0 --cache-type-v q8_0` (the unit's `ExecStart`) |

**Support (question 1).** b10830 supports speculative decoding on this hybrid model. With any drafter the server logs
`load_model: speculative decoding will use checkpoints`: rejected drafts are rolled back by restoring a recurrent-state
checkpoint, the rollback decision 3 asks about. `--spec-type` offers `draft-simple`, `draft-eagle3`, `draft-mtp`,
`draft-dflash`, `draft-dspark` and several n-gram types. The `--draft-max`/`--draft-min` flags the issue names are
removed in this build; they are now `--spec-draft-n-max`, `--spec-draft-n-min` and `--spec-draft-p-min`.

**MTP.** Not tried (decision 4): the production GGUF has no MTP heads (733 tensors, 40 blocks, no `nextn` tensors or
`nextn_predict_layers` key). Desk note: `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` publishes the same model with the MTP
layer kept, which `--spec-type draft-mtp` could use. Trying it means changing the main model file, which is out of
scope here.

**Drafters (question 2).** All three have the main model's vocabulary (248,320 tokens, `qwen35` pre-tokenizer, same
BOS/EOS ids), and llama.cpp loaded each without a vocab warning. Each was downloaded at a pinned revision and its
SHA-256 computed locally; every hash matches the Hugging Face LFS hash.

| Drafter | Repository @ revision | File, bytes | SHA-256 | Type |
| --- | --- | --- | --- | --- |
| Qwen3.5-0.8B (small model of the same family) | `ggml-org/Qwen3.5-0.8B-GGUF` @ `807dfd9a6fadbf703207aac3bfb0b2b3040d077c` | `Qwen3.5-0.8B-Q8_0.gguf`, 833,592,096 | `37ae482d336108d23516fa35e8e0c4126688d81018b87178a18d752a1357814f` | `draft-simple` (itself a DeltaNet hybrid) |
| **DFlash**, trained for this target by z-lab | `Alittlehammmer/Qwen3.6-35B-A3B-DFlash-GGUF-llama.cpp` @ `2aa57b642f211d2639083954c5b66d9c1ee503fc`, a GGUF of `z-lab/Qwen3.6-35B-A3B-DFlash` @ `f181eece646affea2c38b2765f1aaa01a9734ccd` (Apache-2.0) | `Qwen3.6-35B-A3B-DFlash-Q8_0.gguf`, 421,060,800 | `724ac8a102763e0d896cd49bf558fdf5bfdb146ec9f7097d18b035e4a95eac3f` | `draft-dflash` (6-layer block drafter that reads 8 target layers' hidden states) |
| DSpark, trained for this target by Red Hat | `Anbeeld/Qwen3.6-35B-A3B-DSpark-GGUF` @ `c906bddc2f2177c7881fe1bdd0d8db77cb5cd1a7`, a GGUF of `RedHatAI/Qwen3.6-35B-A3B-speculator.dspark` @ `53814b238c3a6ce5f332066a6bedb9d179777ede` (Apache-2.0) | `Qwen3.6-35B-A3B-DSpark-Q8_0.gguf`, 1,020,631,552 | `2b350c8bf958673977d4b1fa403af7a2051dfc74873437994fbdc5f7f5904790` | `draft-dspark` |

Provenance: the authoritative pages (z-lab, RedHatAI) publish safetensors only. The GGUFs are third-party conversions.
The DFlash card says it used upstream `convert_hf_to_gguf.py` and `llama-quantize`. The DSpark card says it targets
the BeeLlama.cpp fork, though the file loads and runs on upstream b10830. Neither conversion was rebuilt from the
safetensors here; the follow-up should either accept the conversion or convert z-lab's file itself.

## Commands

The scripts are in `docs/speculative-decoding-study/`: `serve.sh` (one container), `spec_bench.py` (prompt set, long
prompts and memory sampling; the config names below are its `CONFIGS`) and `canary_run.py` (the canary's hard tasks).
Drafters go in `.tmp/models/` (or `DRAFTERS_DIR`). The tables below came from these runs, from the repository root,
with the project venv and Docker:

```
D=docs/speculative-decoding-study
python $D/spec_bench.py runs/ah772/screen base,draft08-n2,draft08-n4,draft08-n8,dflash-n4,dflash-n6,dflash-n15,dspark-n7,ngram-mod --prompts duration,refactor,regex
python $D/spec_bench.py runs/ah772/screen dflash-n4-ft2g,dflash-n6-ft2g,dflash-n15-ft2g,dspark-n7-ft2g,ngram-mod,base-ft2g --prompts duration,refactor,regex
for r in 1 2; do python $D/spec_bench.py runs/ah772/main-r$r base,dflash-n6-ft2g,base-ft2g,dflash-n4-ft2g --long; done
python $D/canary_run.py runs/ah772/canary base,dflash-n4-ft2g --repeats 2
python $D/spec_bench.py runs/ah772/ctx64k base-64k,dflash-n4-ft2g-64k --long --long-sizes 2000,16000,30000,58000 --modes profile
python $D/spec_bench.py runs/ah772/vram-attr dflash-n4-ft2g,dflash-n4-ft2g-64k --long --long-sizes 2000,30000 --modes profile
```

The first screening run stopped at `dflash-n4`'s crash, so `dflash-n6`, `dflash-n15`, `dspark-n7` and `ngram-mod`
never ran at the default fit target; the script was then changed to record a crashed config and go on, and the second
batch re-ran them with `--fit-target 2048`. They were also tidied
for the repository afterwards (paths made relative, options passed as arguments); what they send and measure is
unchanged. The last run also logged `nvidia-smi --query-compute-apps=pid,process_name,used_memory` every second.

Each configuration runs in its own container (`serve.sh`). Production flags, except `--ctx-size 32768` (the
bake-off's size; the 64K configs pass `--ctx-size 65536` last, which overrides it) and no `--sleep-idle-seconds`:

```
port=8081
docker run --rm --name ah772-bench --gpus all --network host \
  --health-cmd "curl -f http://127.0.0.1:$port/health" --health-start-period 300s \
  -v ~/.local/share/agent-harness/models:/models:ro -v <repository>/.tmp/models:/drafters:ro \
  ghcr.io/ggml-org/llama.cpp:server-cuda-b10830 \
  -m /models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --alias qwen3.6-35b-a3b --host 127.0.0.1 --port "$port" \
  --ctx-size 32768 --flash-attn on --parallel 1 --jinja --metrics \
  --fit on --cache-type-k q8_0 --cache-type-v q8_0 -lv 4 <config args>
```

| Config | Extra args |
| --- | --- |
| `base` | none (production) |
| `base-ft2g` | `--fit-target 2048` |
| `draft08-nK` | `-md /drafters/Qwen3.5-0.8B-Q8_0.gguf --spec-type draft-simple --spec-draft-n-max K` (K = 2, 4, 8) |
| `dflash-nK` | `-md /drafters/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf --spec-type draft-dflash --spec-draft-n-max K` (K = 4) |
| `dflash-nK-ft2g` | the same plus `--fit-target 2048` (K = 4, 6, 15) |
| `dspark-n7-ft2g` | `-md /drafters/Qwen3.6-35B-A3B-DSpark-Q8_0.gguf --spec-type draft-dspark --spec-draft-n-max 7 --fit-target 2048` |
| `ngram-mod` | `--spec-type ngram-mod` (no drafter) |
| `base-64k`, `dflash-n4-ft2g-64k` | as `base` and `dflash-n4-ft2g`, plus `--ctx-size 65536` |

**Prompt set.** Each prompt was sent once per sampling as a single user message to `/v1/chat/completions` with
`max_tokens: 512` and `cache_prompt: false`, after one unrecorded warm-up request. Profile sampling is
`{"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}`; greedy is `{"temperature": 0.0, "top_k": 1}`. Decode
tok/s, prompt time and acceptance come from the response's `timings` (`predicted_per_second`, `prompt_ms`,
`draft_n_accepted / draft_n`). Thinking stayed on (the template's default), and every answer used all 512 tokens.

| Id | Prompt |
| --- | --- |
| `duration` | Write a Python function parse_duration(s) that turns strings like '1h30m', '45s' and '2h' into seconds and raises ValueError on malformed input such as '1hm' or ''. Include pytest tests. |
| `lru` | Implement an LRU cache class in Python with get and put in O(1), without functools. Explain the design briefly. |
| `sql` | Given tables orders(id, customer_id, total, created_at) and customers(id, name, country), write a SQLite query for the top 5 customers by total spend in 2025 per country, then explain it. |
| `refactor` | Refactor this function to be readable and add type hints: (code below) |
| `bash` | Write a bash script that finds every *.log file under a directory older than 7 days, gzips it, and prints a summary of how many files and bytes were compressed. Handle spaces in file names. |
| `bugfix` | This Python code is supposed to merge overlapping intervals but returns wrong results. Find the bug and give the fixed code: (code below) |
| `json_cli` | Write a small Python CLI with argparse that reads a CSV file and prints it as a JSON array of objects, with a --pretty flag. Include a unit test. |
| `regex` | Explain, step by step, what this regular expression matches and give three matching and three non-matching examples: `^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{1,5})?$` |

The exact strings are `PROMPTS` in `spec_bench.py`. The two code prompts end with a blank line and this code
(`refactor`, then `bugfix`):

```python
def f(d):
    r=[]
    for k in d:
        if d[k]!=None and d[k]>0:
            r.append((k,d[k]*2))
    r.sort(key=lambda x:-x[1])
    return r
```

```python
def merge(iv):
    iv.sort()
    out = [iv[0]]
    for s, e in iv[1:]:
        if s < out[-1][1]:
            out[-1][1] = e
        else:
            out.append([s, e])
    return out
```

**Long prompts** reuse `bakeoff/perf.py`'s `build_prompt` filler (2K and 16K tokens; 2K, 16K, 30K and 58K at 64K
context), with `max_tokens: 200` at the profile sampling.

**Canary hard tasks.** `python -m bakeoff.canary` runs through the production `Manager` against the configured
`default_model`, so it can't target a throwaway server. The canary's six hard tasks (`bakeoff/canary.yaml`:
`sqlite_report`, `merge_conflict`, `cli_json_output`, `log_correlation`, `duration_parser`, `multi_bug_inventory`;
the three web tasks were not run) ran instead through `bakeoff.run.run_model` with 2 repeats, the #174 study's path.
It used a `LlamaServer` subclass whose `command()` starts the container above, the profile's sampling and the existing
Docker sandbox image. `bakeoff/server.py` itself spawns a Windows `llama-server.exe` and reads memory through
`windll`, so it doesn't run on the Ubuntu tower unchanged.

**Memory** was sampled every second: whole-card `memory.used` from `nvidia-smi` (it includes about 354 MiB of desktop
and Sunshine), the llama-server process's `VmRSS` and `RssAnon` from `/proc/<pid>/status`, and the host's lowest
`MemAvailable`.

## Results

### Screening (3 prompts: `duration`, `refactor`, `regex`; mean decode tok/s)

Two batches. The second ran during a slow spell on the host (its no-drafter baseline decoded 57 tok/s against 75 in
the first), so compare within a batch only.

| Batch | Config | `--fit` placement (overflowing layers) | Profile tok/s | Greedy tok/s | Acceptance (profile) |
| --- | --- | --- | --- | --- | --- |
| 1 | `base` | 17 | 75.1 | 77.1 | - |
| 1 | `draft08-n2` | 20 | 39.7 | 42.7 | 61% |
| 1 | `draft08-n4` | 20 | 42.5 | 43.8 | 49% |
| 1 | `draft08-n8` | 20 | 34.4 | 36.9 | 30% |
| 1 | `dflash-n4` (default fit target) | 19 | crashed | | |
| 2 | `base-ft2g` | 19 | 57.0 | 64.9 | - |
| 2 | `dflash-n4-ft2g` | 20 | 84.2 | 88.0 | 59% |
| 2 | `dflash-n6-ft2g` | 21 | 88.0 | 92.6 | 48% |
| 2 | `dflash-n15-ft2g` | 23 | 56.7 | 51.6 | 23% |
| 2 | `dspark-n7-ft2g` | 23 | 50.7 | 54.1 | 31% |
| 2 | `ngram-mod` | 17 | 53.6 | 62.9 | 4% (2 of 54; greedy 44%) |

The 0.8B draft model halves decode speed even at 61% acceptance: it is a 25-layer model run k times per step, against
a target that decodes in about 13 ms. DFlash drafts a whole block in one pass. n-gram lookup barely fires on fresh
prompts with sampling.

### Main comparison, 32K context (8 prompts, two interleaved rounds)

Order per round: `base`, `dflash-n6-ft2g`, `base-ft2g`, `dflash-n4-ft2g`.

| Config | Round | Profile median (min-max) | Greedy median | Acceptance profile / greedy | 2K prompt: prefill tok/s, decode | 16K prompt: prefill tok/s, decode |
| --- | --- | --- | --- | --- | --- | --- |
| `base` | 1 | 74.5 (67.9-76.7) | 73.3 | - | 927, 68.4 | 875, 70.1 |
| `base` | 2 | 74.5 (66.8-75.8) | 75.1 | - | 884, 77.9 | 974, 71.6 |
| `base-ft2g` | 1 | 74.5 (65.8-76.4) | 74.9 | - | 833, 71.1 | 914, 68.5 |
| `base-ft2g` | 2 | 65.7 (61.3-72.2) | 71.4 | - | 829, 72.4 | 905, 70.0 |
| `dflash-n4-ft2g` | 1 | **103.4** (77.4-119.2) | 104.4 | 64% / 66% | 748, 98.7 | 818, 94.9 |
| `dflash-n4-ft2g` | 2 | **111.9** (73.8-115.5) | 109.3 | 65% / 63% | 774, 98.8 | 853, 95.7 |
| `dflash-n6-ft2g` | 1 | 84.1 (66.2-99.3) | 91.7 | 51% / 51% | 715, 89.4 | 818, 83.2 |
| `dflash-n6-ft2g` | 2 | 90.3 (68.0-110.4) | 92.7 | 49% / 53% | 605, 77.9 | 771, 98.7 |

Per prompt, `dflash-n4-ft2g` against `base` (profile sampling, mean of both rounds, tok/s and acceptance): `duration`
75.6 vs 67.4 (55%), `lru` 93.0 vs 73.3 (63%), `sql` 116.0 vs 74.8 (73%), `refactor` 101.0 vs 73.9 (59%), `bash`
98.7 vs 74.6 (56%), `bugfix` 116.6 vs 76.3 (77%), `json_cli` 114.8 vs 74.8 (69%), `regex` 114.2 vs 73.4 (69%). Every
prompt gains; the gain follows acceptance.

### Production context, 64K (8 prompts, profile sampling only)

| Config | Placement | Prompt-set median decode | Long prompt 2K / 16K / 29K / 56K: prefill s, decode tok/s |
| --- | --- | --- | --- |
| `base-64k` | 18 overflowing, 1,121 MiB free | 74.9 | 2.2 s, 75.6 / 17.9 s, 71.8 / 30.2 s, 64.6 / 60.1 s, 62.0 |
| `dflash-n4-ft2g-64k` | 21 overflowing, 2,157 MiB free (before the drafter) | **96.0** (acceptance 62%) | 2.8 s, 89.3 / 19.2 s, 79.5 / 35.7 s, 94.7 / 68.3 s, 78.9 |

### Canary hard tasks (32K, profile sampling, 2 repeats)

| Task | `base` | `dflash-n4-ft2g` |
| --- | --- | --- |
| `multi_bug_inventory` | 2/2 | 2/2 |
| `duration_parser` | 2/2 | 2/2 |
| `merge_conflict` | 2/2 | 2/2 |
| `log_correlation` | 2/2 | 2/2 |
| `sqlite_report` | 0/2 | 0/2 |
| `cli_json_output` | 2/2 | 2/2 |
| **Total** | **10/12** | **10/12** |
| Invalid tool calls / tool errors | 0 / 5 | 0 / 4 |
| Avg turns, total wall time | 11.2, 555 s | 11.2, 490 s |
| Median decode / prompt tok/s over agent turns | 77.7 / 477 | 91.3 / 360 |

Both arms fail `sqlite_report` with the same wrong revenue figures (it is the canary's known hard miss; Qwen was 1/2
in phase 0 and 1/3 in #174). Losslessness at temperature 0.6 is in distribution only, so identical answers aren't
expected, but nothing here points to a quality change. The agent-level gain (+17% decode, -12% wall time) is smaller
than the prompt-set gain because agent turns are short and prefill is a larger share of them, and prefill is slower.

## Costs (question 4)

| | `base` | `dflash-n4-ft2g` |
| --- | --- | --- |
| Expert layers overflowing to system RAM | 17-18 | 20-21 |
| VRAM after load (whole card, 32K / 64K) | 14,765-14,891 / 14,799 MiB | 14,821-15,347 / 15,233 MiB |
| Peak VRAM while serving (32K main rounds and canary / 64K) | 14,789-15,427 / 15,335 MiB | 15,145-15,267 / 15,301 MiB |
| Peak llama-server RSS / anonymous RSS (64K) | 16,834 / 1,700 MiB | 16,761 / 2,217 MiB |
| `MemAvailable` before load / lowest during the 64K run | 21,605 / 18,431 MiB | 21,004 / 17,923 MiB |
| Load time to `/health` | 25-36 s | 25-34 s |
| Prefill tok/s (long prompts, both contexts) | | 7% to 20% lower |

- **`--fit-target 2048` is required.** With the default 1,024 MiB target, `--fit` reserved too little for the DFlash
  drafter (19 overflowing layers, 15,873 MiB used after load), and the first request died with
  `CUDA error: the resource allocation failed` in `cublasCreate_v2` (`runs/ah772/screen/dflash-n4.server.log`). With
  2,048 the card ends where production is today, about 1.1 GB free. The 0.8B draft model did not need it, because
  `--fit` counts a `draft-simple` model (20 overflowing at the default target).
- One screening reading peaked at 15,899 MiB whole-card for `dflash-n4-ft2g`. Every later run of the same config
  (two rounds, the canary, 64K) peaked at 15,145 to 15,301. A last run at 32K and 64K logged per-process GPU memory:
  the peak was 15,300 MiB whole-card, of which llama-server held 14,942 MiB and Sunshine 215 MiB. The outlier did not
  recur, so it was most likely another process on the card at that moment, but it can't be attributed after the fact.
- **System RAM:** 3 to 4 more expert layers (about 1.5 to 3 GB of the 22 GB file) are read from RAM instead of VRAM.
  With mmap these are page cache, so the server's RSS stays the same (16.8 GB). The drop in `MemAvailable` from
  before load to the low point was the same for both (about 3.1 GB); the drafter file is 421 MB.

## Resource guard

- **VRAM:** unchanged headroom with `--fit-target 2048`, so the guard's VRAM triggers (games, Plex, Sunshine) see the
  same free VRAM as today. `docs/gpu-vram-budget.md`'s option B (`--fit-target 2048` to make room for Plex and
  Sunshine) does not combine with this for free: the drafter uses the extra margin. Getting both needs a larger
  target (about 3,072), which pushes another 1 to 2 expert layers to RAM; that combination was not measured.
- **RAM:** the load gate (`model_ram_gb: 14` in `config/harness.yaml`) still covers the model. More of the model is
  in page cache rather than VRAM, but the measured drop in `MemAvailable` during serving was the same as the
  baseline's, so the gate needs no change.
- **Park and reload:** nothing changes. The supervisor's `docker stop` on the pause flag stops the drafter with the
  server, and load time is unchanged.

## Cleanup

All `ah772-*` containers and the canary's sandbox containers are gone (`docker ps -a` lists no `ah772-*` or
`harness-<hex>` names); the GPU is back at 353 MiB (desktop and Sunshine). The 0.8B and DSpark drafter files were deleted. The DFlash file is kept for the follow-up
(decision 4: go) at `~/.local/share/agent-harness/downloads/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf`, same SHA-256 as above.
Production Qwen stays parked as it was found: the dispatch for this run forbade unparking or restarting the
`agent-harness-main-llama` unit, so the issue's "always-on Qwen restarted" step is left to the owner or the
orchestrator that holds the GPU hold.
