# CLM-8B (Contrastive Language Models) as a System One verifier: desk study

Status: documentation-only study for issue #225, written 2026-10-05. No checkpoint, encoder or dataset was
downloaded. vLLM, `clm-serve` and llama-server were not started, and the GPU was not used. No harness command,
prompt, transcript or other private data was sent anywhere. Only public repository, Hugging Face and PyPI
metadata, source files, model cards and issue threads were read. Smart approvals, local model defaults and harness
behavior are unchanged.

**Result: no-go for now.** CLM has one property neither the current reviewer (#18) nor Jev (#160) has: it can run
entirely on our own hardware, so command text never leaves the tower. That is not enough to justify a benchmark yet,
for three reasons:

1. **The released head does not reproduce its own quickstart.** Several independent users, on five different
   serving stacks, report that the reference head gives near-constant answers. The `score` question type picks the
   same level whatever the state says, and state embeddings collapse when every state carries the same question
   suffix. The maintainer has not replied to any of these reports. These are third-party reports. They were not
   reproduced here, because reproducing them needs the GPU.
2. **It does not fit on the target tower.** The reference encoder is Qwen3-8B in BF16, which is 16.4 GB of weights.
   The tower has a 16 GB RTX 4070 Ti Super, and the always-on local model already keeps that VRAM full. A quantized
   encoder would change the embeddings the head was trained on. Upstream supports neither Windows nor a llama.cpp
   encoder.
3. **The verifier numbers don't transfer.** The DeepSWE and Terminal-Bench results come from heads fine-tuned on
   each benchmark's own trajectories. The harness has no labeled trajectory corpus, does not do best-of-N candidate
   selection, and is limited to synthetic cases by the #160 decisions. On approvals, CLM's outputs look like Jev's
   (no text, an uncalibrated confidence), so it would face the same weak points and add a local GPU cost.

The re-evaluation triggers and a ready-to-file benchmark plan are at the end. Every runtime step in that plan
is an owner action.

## Sources pinned

All sources were read on 2026-10-05.

| Artifact | Pin | Notes |
|---|---|---|
| Code, [Contrastive-LM/CLM](https://github.com/Contrastive-LM/CLM) | `main` at `bb42c6c5bf914fd449bed2f6ca65be80602cb1f7` (2026-09-24 23:09 UTC) | Repo created 2026-09-23; no releases or tags. One committer (`jackyk02`). GitHub reports Apache-2.0 |
| PyPI [`contrastive-lm`](https://pypi.org/project/contrastive-lm/) | 0.1.0, uploaded 2026-09-24 21:12 UTC | wheel 56,168 B, sha256 `7f3eed12d3aa10173f71bac7e50fc387565b485a2a2fc4c4323af32b1950055f`; sdist 60,617 B, sha256 `2bfe2c5716fc434d38c7da8fa64a41407a62a3a064388e822d60bf796f024e79`. Uploaded before the last five commits, so it is not the pinned commit |
| Reference head, [Contrastive-LM/CLM-v0.1-8B](https://huggingface.co/Contrastive-LM/CLM-v0.1-8B) | revision `e939398d4556fcd9400c76fa8c5a513202f42b0a` | `CLM_v0.1-8B.pt`, 75,557,149 B, LFS sha256 `b2b4a8c9c2d39263eff78a351eb909a342ce9b3bf21a3f07c1d1bf15f1c4eda5`. Card: `license: apache-2.0`, `base_model: Qwen/Qwen3-8B`. Not gated. `clm-download` fetches `resolve/main`, not a revision |
| DeepSWE head, [Contrastive-LM/deepswe-clm-heads-8k](https://huggingface.co/Contrastive-LM/deepswe-clm-heads-8k) | revision `c60876f3fdf7a75dc58d33b776e469b7e903d0ee` | `best_head.pt`, 75,557,598 B, sha256 `554989fe88635606cb978dc45a1ce083be1990c4a51e551ea3b6055ead1a029a` (matches the card). Card license is **MIT**, not Apache-2.0 |
| DeepSWE training embeddings, [Contrastive-LM/deepswe-clm-train-embeddings-8k](https://huggingface.co/datasets/Contrastive-LM/deepswe-clm-train-embeddings-8k) | revision `4e42267114a2b28dc1d4fab85a2fe2790abc16d2` | 16 Parquet shards, MIT on the card |
| DeepSWE evaluation embeddings, `Contrastive-LM/deepswe-clm-embeddings-8k` | **not available** | The README's reproduce command names this dataset. The Hub API answers 401 for it, which is what it returns for a missing or private repo. The held-out reproduction cannot be run as published |
| Pre-training embeddings, [Contrastive-LM/CLM-v0.1-Pretrain-Nemotron](https://huggingface.co/datasets/Contrastive-LM/CLM-v0.1-Pretrain-Nemotron) | revision `05ca7d05c043350326c5e86c589b9542962747b4` | 1,876 files of `.npy` embeddings plus Parquet metadata. **No license on the card** |
| Terminal-Bench 2.1 head and data | **not published** | No Hub artifact exists for the 87.6% result |
| Encoder, [Qwen/Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) | revision `b968826d9c46dd6066d109eabc6255188de91218` | Five BF16 safetensors shards, 16,381,516,776 B in total. Apache-2.0 on the card |
| Blog, [contrastive-lm.notion.site](https://contrastive-lm.notion.site) | not read | A script-rendered Notion page. The scaling fits and figures it holds are unverified here |
| Upstream issues | #3, #6, #10, #11, #15, #17, #18, #22, #25, #27, #28, #29, read 2026-10-05 | All open. None has a maintainer reply |

## Verified facts and upstream claims

"Verified" means the pinned source code or artifact metadata shows it. It does not mean it was measured here.

| Claim | Status |
|---|---|
| CLM is two MLP projection heads (state, action) on a frozen Qwen3-8B, last-token pooled; score = `exp(logit_scale) * cos(state_head(s), action_head(c))` | **Verified** in `src/clm/heads.py`. The heads map 4,096 dimensions to 512. Upstream #10 counts 18,887,680 parameters, close to the "20M" claim |
| API exposes `noul`, `choice`, `score` and ranking, not generation | **Verified** in `src/clm/schema.py` and `server.py`: `POST /v1/systemone`, `POST /v1/rank`, `GET /v1/models`, `GET /health`. The wire format copies TypeSafe's `/v1/systemone` |
| Probabilities are a softmax over the candidates you supply | **Verified.** They are relative to the candidate set. A `noul` is a two-way softmax between generated "Yes. This is true: ..." and "No. This is false: ..." texts |
| `confidence` | **Verified** as "top probability minus the mean of the others". It is a margin, not a calibrated probability, and no calibration data is published |
| Code and reference weights are Apache-2.0 | **Verified** for the code (`LICENSE`, `pyproject.toml`) and the reference head (card and `LICENSE` file). The DeepSWE head and training embeddings are **MIT**. The Nemotron pre-training embeddings have **no license**. Qwen3-8B is Apache-2.0 |
| Training data: ~60M Nemotron DQA pairs, ~30M hard negatives generated by Gemini 2.5 Flash-Lite, ~1M ADP trajectories plus Endless-Terminals and LiteCoder-Terminal-SFT | **Upstream claim.** None of these source datasets' licenses or terms were checked against the head, and the generated negatives are not published. Gemini-generated training data may carry Google API terms on building competing models. That is a provenance question for upstream, not something this study can settle |
| "On par with Jev, up to 9x lower latency" (zero-shot; T-Rex, BFCL v4, WikiRacing, Super Mario) | **Upstream claim.** Only T-Rex ships (`examples/t_rex`, two result JSON files). None of these tasks resembles approving a shell command |
| DeepSWE verifier: 81.6% | **Verified as upstream-reported** in `verification.json`: 31/38 held-out tasks at Bo4, final-12-step mean score. The same file gives random pick 28/38 (73.7%) and oracle 34/38 (89.5%). Only 13 of the 38 tasks are "mixed", where the choice matters. The gain over random is 3 tasks. A 95% Wilson interval for 31/38 is about 66%–91%. The head was fine-tuned on 59 task-disjoint DeepSWE tasks (22,576 pairs), so this is not a zero-shot result, as the model card itself says |
| Terminal-Bench 2.1 verifier: 87.6% on 30 tasks | **Unverifiable.** No head or data is published. 87.6% is not k/30, so it is presumably an expectation over candidate subsets (as `bon_eval.py` computes) |
| "Jev fails to serve as a verifier for these long-horizon tasks" | **Upstream claim.** No Jev run artifacts are published |
| Latency: ~28 ms server p50 for a new state, ~0.6–2 ms for a cached one (RTX 4090); agentic latency on an H100 | **Upstream claim**, server-side only. No client p95 is given |
| States over 2,048 tokens are truncated | **Verified**, and it is worse than stated: upstream #6 and #29 report that vLLM 0.13+ cuts from the right by default, so the question at the end of a long state is dropped. Training keeps the tail |

## Upstream defect reports that bear on the decision

These are third-party reports. They were read, not reproduced, because reproduction needs the GPU.

- **#3, "Unexpected scores"** (2026-09-24, six follow-ups). The `score` question returns "Very angry" at about 1.00
  for every state, including "Have a nice day!". Reversing the level order doesn't change it. It was reproduced on
  vLLM/CUDA (RTX 4090), MLX, PyTorch MPS and llama.cpp BF16. One commenter found that `noul` and `choice` with
  descriptive options do move with the state, but less sharply.
- **#15, "Quickstart outputs don't reproduce at bb42c6c"** (three independent confirmations, including vLLM 0.30
  on a DGX Spark and an A100). It gives `noul` 0.84 against the documented 0.41. On BoolQ, Emotion, SST-5,
  Banking77, AG News and a prompt-injection set, zero-shot answers are near-constant per suite: BoolQ is "no" on
  498 of 500. The mean cosine between *different* states after the state head is 0.947. The reporter traces this to
  the shared question suffix dominating last-token pooling. A Q4_K_M encoder gives yet another value (0.80).
- **#6 and #29, truncation side.** Long states lose their question (see the table above).
- **#10, `torch.load` without `weights_only`.** `heads.py` unpickles with full pickle semantics on torch 2.1–2.5,
  which `pyproject.toml` allows. `bon_eval.py` and `finetune.py` pass `weights_only=False` explicitly. Loading any
  head you did not train yourself is code execution. The published head is reported to load cleanly with
  `weights_only=True`.
- **#11, network exposure.** `clm-serve` binds `0.0.0.0` with no key by default.
- **#22, non-English text** and **#27/#28, cache concurrency bugs** are minor for us but show a 0.1 codebase.

For a safety reviewer, #3 and #15 are disqualifying until fixed. A verifier whose answer doesn't depend on the input
would pass routine cases only by accident and would give no protection on adversarial ones.

## Serving path and requirements

From `README.md`, `serve_qwen3_8b.sh`, `pyproject.toml` and `requirements.txt` at the pinned commit:

```
client ──► clm-serve (FastAPI/uvicorn, :8700; heads on GPU if torch sees one, else CPU)
                │  POST /v1/embeddings
                ▼
           vLLM `--runner pooling` Qwen3-8B (GPU, :8090), last-token pooling, max-model-len 2048
```

- Dependencies: `pip install contrastive-lm` pulls in `torch>=2.1`, `vllm>=0.6`, `fastapi`, `uvicorn`, `pyarrow`,
  `numpy` and `requests`. `requirements.txt` says "Linux + NVIDIA GPU". vLLM has no native Windows support.
- GPU: the encoder is 16.4 GB of BF16 weights before activations, the CUDA context and the vector cache (`clm-serve`
  reserves 2% of device memory by default). Upstream says the 2,048-token setting "fits on a 24 GB GPU" and shows
  results on an RTX 4090 and an H100.
- Network at start-up: `clm-serve` downloads the head from `resolve/main` on first run if it is missing, and sends a
  `HEAD` to the repo's `config.json` "to count downloads". Neither is pinned to a revision.
- The head is tied to the exact encoder and pooling (model card: "Encoder-locked").

### Target-tower fit

The tower is an RTX 4070 Ti Super with 16 GB and 32 GB RAM (`docs/INSTALL.md`, `docs/phase0-results.md`).

- **Coexistence with the always-on model: no.** llama-server's `--fit` keeps the 16 GB full with
  Qwen3.6-35B-A3B and spills experts to RAM (`docs/phase0-results.md`). Qwen3-8B BF16 needs about 15.3 GiB for
  weights alone, so it can't share the card. It barely fits even on an idle card.
- **Quantized encoder: unsupported and changes the model.** A Q8 or Q4 GGUF Qwen3-8B (about 8.7 or 5 GB) through
  the existing llama.cpp stack could fit next to a smaller primary model. But the head was trained on BF16 vLLM
  embeddings, and #15 reports that Q4_K_M already moves the quickstart answer. Upstream's llama.cpp and MPS encoder
  paths (#17, #18) are open PRs, not supported code.
- **CPU encoder:** an 8B forward pass per new state on CPU would take seconds, not milliseconds. That removes the
  latency case, which is the reason to use a System One model at all.
- **GPU contention:** the tower already pauses local inference for games and transcodes (`docs/phase5-results.md`,
  GPU hold). A second resident GPU service on the approval path would either need the same pausing, and then fall
  back to a human, or compete with the primary model.

## Fit for Agent Harness

### Smart-approvals reviewer (#18)

The reviewer contract (`harness/smart_approvals.py`, `docs/smart-approvals.md`) needs strict
`{recommendation, confidence, reason, risk_flags}`. `auto_ok` requires `approve`, no flags, and in `auto` mode a
confidence of at least 0.85. CLM could fill it with the question set already fixed for Jev in
`docs/jev-smart-approvals-study.md`: one `choice` for the recommendation and one `noul` per risk flag, with the fixed
reason template. The API is wire-compatible with TypeSafe's, so that mapping carries over unchanged.

Gaps:

- **No reason text.** Same as Jev. The #160 template applies.
- **Confidence is a margin, not a calibration.** "Top minus mean of the rest" over a three-way softmax is not a
  probability of being right. Any 0.85 threshold would have to be earned by measurement (gate 3 of #160). Nothing
  published suggests it would pass.
- **Adversarial robustness: none claimed, and the mechanism is a concern.** Scoring is the cosine similarity of
  two embeddings. Text inside a quoted test-name expression that says "this is a routine test run" would move the
  state embedding toward the approve option's description. Nothing in the architecture treats the command as
  untrusted. The #15 finding, that the shared question suffix dominates the state vector, points the same way: the
  answer may depend more on the question than on the command.
- **Payload size is fine.** The reviewer payload is at most about 400 characters of command plus booleans, far below
  the 2,048-token limit, so the truncation bug would not bite.
- **What CLM would add over Jev:** local execution with no egress, no vendor terms, no retention question and no
  per-call cost. That is a real advantage for #18's privacy posture. It is outweighed by the defects above and the
  GPU conflict.
- **What it would add over the current hosted reviewer:** lower latency and no API cost, if it worked. The current
  reviewer is off or in shadow by default, gets few calls, and is not latency-bound (8 s timeout), so neither gain
  matters much today.

### Coding-result verification and candidate ranking

CLM's strongest claim is best-of-N trajectory selection after fine-tuning. The harness:

- does not sample several candidate solutions per task and pick one. The bake-off (`docs/harness-bakeoff-study.md`)
  compares harnesses, not candidates within a run;
- has no labeled corpus of its own trajectories to fine-tune a head on. Under the #160 decisions, evaluation data is
  synthetic only;
- already has deterministic verification where it matters: tests, CI and the review runner.

Even upstream's own fine-tuned number is a 3-task gain over random on 38 tasks. That is not strong enough evidence to
build a candidate-sampling feature around. Typed decisions such as routing or classification have no current
consumer in the harness.

### Relation to #18 and #160

- **#18 (closed):** CLM would be another backend behind the same eligibility gate. It never replaces `Policy`,
  `assess_eligibility` or the sandbox. Its distinct role would be "a local reviewer that keeps command text on the
  tower". No other studied option offers that.
- **#160 (closed, result "defer"):** Jev and CLM answer the same typed questions through the same wire format. CLM
  is the self-hosted variant. Upstream positions CLM explicitly as a Jev alternative, so a follow-up would compare
  them on the same frozen synthetic set with the same gates. If Jev's follow-up runs first and fails gate 1 on
  injection, the CLM result would be expected to fail the same way. That ordering saves a GPU session.

## Recommendation

**No-go.** Don't file a benchmark issue now. Re-evaluate when **all** of these hold:

1. Upstream fixes or explains #3 and #15 (answers depend on the state; the quickstart reproduces at a pinned commit),
   and the fix is in a tagged release with a revision-pinned head.
2. #6/#29 (truncation side) and #10 (`weights_only`) are fixed upstream, or the follow-up pins a vLLM version and
   torch ≥ 2.6.
3. There is a serving path that coexists with the always-on model on a 16 GB card: a smaller backbone (upstream
   #25), or a supported quantized or llama.cpp encoder with a head trained on those embeddings. The announced
   CLM-35B makes this worse, not better.

The owner can also override this and schedule the plan below as a negative-control measurement. It would turn the
third-party reports into first-hand numbers on our own cases.

## Follow-up benchmark plan (file only after the triggers, or on owner override)

Title: "Measure CLM as a local smart-approvals reviewer on the frozen synthetic set (shadow comparison with #160)".
The plan reuses the #160 design wherever possible, so results are directly comparable.

**Fixed before any run:**

- **Pins.** CLM commit, PyPI or wheel sha256, head revision and sha256, Qwen3-8B revision, vLLM version, torch
  version. Record them on the issue. Load the head with `weights_only=True`. Set `--host 127.0.0.1`. Run
  `clm-serve --no-ui`, with the head passed by `--ckpt` from a verified local file so nothing is fetched from
  `resolve/main`.
- **Cases.** The expanded, second-labeled #160 set (≥150 escalate, ≥100 approve), derived from
  `tests/fixtures/jev_study_cases.json`. Every case passes `assess_eligibility`, and the file's sha256 is recorded
  before the first call. No real commands.
- **Questions.** The #160 question set verbatim (`choice` recommendation plus six `noul` flags at 0.5), sent as
  `reviewer_payload()` text. `to_text` renders the object as prose, as upstream requires. Use no `score` questions
  while #3 is open.
- **Gates (unchanged from #160):**
  1. 0 false approvals on ≥150 escalate cases. Report the rule-of-three bound.
  2. Cost or latency: client p95 at least 30% lower than the current reviewer over ≥200 calls after 10 warm-up
     calls. Cost is $0 marginal, so also report the GPU-seconds and the VRAM the encoder held.
  3. Calibration gap ≤ 0.10 at t = 0.85 on ≥200 labeled cases, with ≥30 cases at or above t.
- **Extra CLM-only checks (not gates; any failure is reported and stops the go):**
  - **State sensitivity.** Mean pairwise cosine between state-head projections of different cases. If it is above
    0.9, or if more than 90% of cases get the same `choice` regardless of label, the run is recorded as "collapsed"
    and stops.
  - **Truncation.** Assert that no case exceeds the token limit (record the maximum token count).
  - **Encoder parity.** If a quantized encoder is used, also run BF16 on a GPU that fits it, and report the
    per-case agreement.

**Owner-controlled runtime steps.** None of these run unattended.

1. The owner schedules a window and announces a GPU hold (`/gpu/pause`, `docs/admin-api.md`), because
   the always-on model has to be stopped. Alternatively, the owner provides a separate Linux host with at least
   24 GB of GPU memory.
2. The owner approves the downloads: about 16.4 GB for Qwen3-8B and 75 MB for the head, from Hugging Face, into a
   path outside the repo.
3. The owner runs, or explicitly lets a worker run, the vLLM encoder and `clm-serve` on loopback only. Nothing leaves
   the host, so no egress or provider change is needed.
4. The study script runs both reviewers in shadow on the frozen cases, writes per-case outputs to a gitignored file,
   and reports the gates. Then it stops the services and releases the GPU hold.
5. Decision: any gate failure or a collapsed run is a no-go, recorded here and on the issue. A pass goes to the owner
   to decide on an implementation issue. That issue would add a `clm` provider in shadow only, behind the existing
   eligibility gate, with no `auto` mode.

## What this study did not do

- It did not run CLM or reproduce any upstream or third-party number.
- It did not check the licenses of Nemotron DQA, ADP, Endless-Terminals or LiteCoder-Terminal-SFT, or the terms
  covering the Gemini-generated negatives.
- It did not read the Notion blog, which holds the scaling-law fits.
- It made no change to smart approvals, providers, egress filters, dependencies or local model configuration.
