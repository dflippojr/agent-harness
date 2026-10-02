# Repo map study (#264)

**Status: pending run.** The generator, switches, task group and report script are built and tested. The measured A/B
needs the model server (and the GPU), so it has not been run. The results table below is empty; the owner or the
orchestrator runs the commands in [How to run](#how-to-run) on a planned GPU night and fills it in.

## Question

Does giving the local model a compact, ranked map of the repository's symbols (the idea behind Aider's repo map)
cut exploratory turns and context use enough to adopt? Qwen's bottleneck is prompt processing (~880 tok/s; a cold
29K prompt takes ~33 s), so every exploratory `search` or read turn that fills context costs real time.

## What was built (experiment only)

- `harness/repomap.py` parses files with tree-sitter (parse only: nothing in the repository is imported, executed or
  built; symlinks are not followed and nothing resolving outside the workspace is read). Languages: Python, JS/TS
  (`.js .jsx .mjs .cjs .ts .tsx`), Go, Rust. Other languages, unparseable files and files over 1 MB are skipped
  silently. File list: `git ls-files` in a git repo (so `.gitignore` is respected), otherwise a directory walk
  skipping `.git`, `node_modules`, `venv`, `.venv`, `__pycache__`, `dist`, `build`.
- Ranking: PageRank in pure Python over a file graph. An edge A -> B for each identifier defined in B and referenced
  in A (weight split across files when several define the name). Output lists files in rank order, each with its
  top-level classes, functions and their members as signatures, cut at a token budget (default 1500, estimated as
  chars/4). Ties break by path, so the text is deterministic and the prompt prefix is cacheable.
- Grammar packages are pinned in `requirements-repomap.txt`, not `requirements.txt`, and imported lazily. If they are
  missing, one warning is logged and the map is empty, so the prompt is unchanged.
- Switches, both off by default (default behavior and prompt are byte-identical when off):
  - bake-off: `python -m bakeoff.run --repo-map [--repo-map-budget N]`, appended to `SYSTEM_PROMPT` in
    `bakeoff/agent.py` once, at the start of each run.
  - harness: `repo_map.enabled` (and `repo_map.budget_tokens`) in `config/harness.yaml`. The map is appended to the
    system message the first time a local-model tower session runs, and refreshed only when a round reset is applied
    (`Runner._round_reset`), never mid-round. Hosted backends and chat sessions never get a map.
- `bakeoff/tasks_large.py`: the large-repo group (`--suite large`), five tasks against a pinned checkout of this
  repository, **commit `0dd8560d5f22ec361eb9c3b8cc2956f387210ffc`**. Files come straight from that commit
  (`git ls-tree`/`git show`); `harness/`, `bakeoff/`, `docs/`, `ops/`, `scripts/`, `config/`, `install/`, `sdk/`,
  `macrunner/` and top-level text files are included (209 files). `tests/` is left out because the existing tests need
  packages the sandbox lacks; the prompts tell the model to verify with `python -c`. Each code task has a hidden
  check the agent never sees (`bakeoff --selftest` proves it fails with no work and passes with the reference solution).

  | Task | Kind | What it asks |
  | --- | --- | --- |
  | `large_elide_ends` | code | add `head_chars`/`tail_chars` to the compaction elide tier |
  | `large_rate_limit` | code | `Rate limited:` results must not seed a dead-end retry |
  | `large_quote_min` | code | lower the minimum checked quote length from 25 to 15 |
  | `large_trash_deleter` | code | treat `trash`/`trash-put` as deleters in the command policy |
  | `large_trace_reset` | understand | trace `reset_round` through runner and compaction |

  The recorded-web suite (`bakeoff/web_suite.py`) has no code repository, so a map is a no-op there and it is not an
  A/B arm. `python -m bakeoff.web_suite run --repo-map` runs it with the switch on once; each result records
  `repo_map_in_prompt` and the run fails if a map was added.

## Method

- Same task set per group, map off vs on, **3 repeats per arm**, the same model and the sampling settings the suite
  already takes from `bakeoff/models.yaml`. Groups reported separately: the hard suite (small synthetic repos, a map
  may be near-trivial) and the large-repo group.
- Metrics per arm: pass rate, mean turns, mean total prompt tokens, mean wall seconds (from each run's
  `result.json`/`summaries.json`). `harness/efficiency.py` composition and dead-end-retry figures are produced by the
  production runner, not by `bakeoff/agent.py`, so they are not available for this A/B; the bake-off's turn and
  prompt-token counts are the yardstick.
- **Go criterion (pre-registered):** go if, on the large-repo group, mean turns drop by at least 15% or mean total
  prompt tokens drop by at least 15% with pass rate no lower than the off arm, and the hard-suite group shows no
  pass-rate regression. Otherwise no-go. `bakeoff/repomap_report.py` applies this rule.

## How to run

Measured runs need the GPU and model server: stop other GPU work first (and do not overlap with #265 or #227). Do
each command once per arm (omit `--repo-map` for off). Set `MODEL` to the Qwen entry in `bakeoff/models.yaml`.

```
pip install -r requirements-repomap.txt
python -m bakeoff.run --suite large --models $MODEL --repeats 3 --skip-perf                # large, map off
python -m bakeoff.run --suite large --models $MODEL --repeats 3 --skip-perf --repo-map     # large, map on
python -m bakeoff.run --suite hard  --models $MODEL --repeats 3 --skip-perf                # hard, map off
python -m bakeoff.run --suite hard  --models $MODEL --repeats 3 --skip-perf --repo-map     # hard, map on
python -m bakeoff.web_suite run --repeats 1 --repo-map                                    # regression check only
python -m bakeoff.repomap_report --large runs/<large-off> runs/<large-on> --hard runs/<hard-off> runs/<hard-on> --model $MODEL
```

`--repo-map` run directories carry a `-repomap` suffix. Without a model server, `python -m bakeoff.run --suite large
--selftest` (needs Docker and the sandbox image) still checks that the task checkers work.

## Results

Pending run. Fill the table from `bakeoff.repomap_report` output.

| Group | Map | Runs | Pass rate | Mean turns | Mean prompt tokens | Mean wall s |
| --- | --- | --- | --- | --- | --- | --- |
| large-repo | off | pending | pending | pending | pending | pending |
| large-repo | on | pending | pending | pending | pending | pending |
| hard suite | off | pending | pending | pending | pending | pending |
| hard suite | on | pending | pending | pending | pending | pending |

Web-suite regression check (switch on, prompt unchanged): pending.

## Decision

Pending run: apply the go criterion above to the table.

If go, file a follow-up issue for the production feature: per-project opt-out, language expansion and budget tuning
(all out of scope here).
