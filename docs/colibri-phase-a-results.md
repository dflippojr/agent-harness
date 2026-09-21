# Colibri Phase A: hardware benchmark (issue #90)

Date: 2026-09-20. Scope: feasibility only. Nothing here touches the harness runtime; benchmark code lives in
`ops/colibri-bench/` and all artifacts (engine, fixture, venv, raw JSON) were kept outside the repo.
The 372 GB checkpoint was **not** downloaded, per the Phase A boundary.

## Recommendation

**Investigate a named bottleneck first; do not request Phase B yet.** Two blockers, one of them decisive:

1. **Storage.** The 22 TB `D:` volume (where harness data lives) is backed by SATA HDDs. Unbuffered random 1 MiB
   reads measured **~40 MB/s** (QD1 and QD4). Upstream reports ~11 GB read per cold token, i.e. roughly 4.5 min/token
   (~0.004 tok/s, ~75 h per 1,024 tokens). That is far under the 0.10 tok/s no-go line. `D:` is not usable for the model.
2. **Only the NVMe OS drive is fast enough** (`C:`, ~3.9 GB/s at QD1, ~6.6 GB/s at QD4, p50 0.25 ms). It holds the
   pagefile and has ~0.96 TB free; a 372 GB checkpoint would leave about 0.59 TB (~30%) free, above the 20% rule,
   but it puts the model on the OS/pagefile volume. That needs an explicit owner decision, not an executor default.

CUDA feasibility is still unknown (see "Not run"). Suggested next step: owner confirms whether `C:` may host the
model; if yes, a Phase A2 with a CUDA build and the GPU hold actually on, before any Phase B request.

## Provenance

| Item | Value |
|---|---|
| Upstream | https://github.com/JustVugg/colibri, tag `v1.12.0`, commit `dcd73832f293750086643e1f0ccd2cd6d067259c` |
| License | Apache-2.0 (upstream `LICENSE`, `NOTICE`) |
| Engine used | Upstream prebuilt `colibri-v1.12.0-windows-x86_64.zip`, sha256 `c36e394ccda37637b4c864593a62c5d78a8abedfd224d138ad55f4b32975efbd` (matches upstream `SHA256SUMS.txt`) |
| Build inputs | None. No C compiler, CMake or CUDA toolkit is installed here, so no source build |
| Fixture | `c/tools/make_glm_bench_model.py --device cpu` at that commit (313M params, random weights, bf16, 1.2 GB), via torch 2.14.0+cpu / transformers 5.17.0 in a scratch venv |

## Machine (sanitized)

- CPU: Intel Core i7-14700KF, 28 logical / 20 physical cores (engine used 20 threads).
- RAM: 31.8 GB total; ~16 GB available at run start (other agent and CI activity on the host).
- GPU: NVIDIA RTX 4070 Ti SUPER, 16 GB, driver 616.92. 32 C and idle throttle mask `0x1` throughout.
- OS: Windows 11 Pro 10.0.26200. Pagefile 32 GB allocated, peak usage 2.7 GB before the run. Commit stayed ~43.6% during runs.
- Storage: `C:` NVMe SSD 2 TB (0.96 TB free); `D:` 22 TB NTFS volume on a pool of SATA HDDs (16 TB free).
  Physical disks report Healthy. Serials and paths omitted.

## Measured storage read throughput

`ops/colibri-bench/disk_probe.py`: 8 GB scratch file, `FILE_FLAG_NO_BUFFERING`, 1 MiB random reads, 20 s, file deleted afterwards.
Measured on a shared host.

| Volume | QD | MB/s | IOPS | latency p50 / p99 |
|---|---|---|---|---|
| `D:` (HDD pool) | 1 | 40.3 | 38 | 22.5 / 71.2 ms |
| `D:` (HDD pool) | 4 | 38.3 | 36 | 68.7 / 1044 ms |
| `C:` (NVMe) | 1 | 3929 | 3747 | 0.25 / 0.33 ms |
| `C:` (NVMe) | 4 | 6617 | 6311 | 0.61 / 1.02 ms |

## Correctness

- Upstream build/unit/CUDA-kernel tests: **not run** (need a compiler and CUDA toolkit).
- Oracle replay on the fixture, greedy generation vs. `ref_glm.json`: **8/8 tokens match** in all three modes on both
  volumes, identical token hash `1595dacb685a`. No mismatch or error lines in any measured run.
- Teacher-forced oracle (`TF=1`): **16/20 positions, 4 mismatches**, on the random-weight fixture (8-bit engine vs. bf16 oracle).
  Random weights give flat logits and upstream documents floating-point near-ties, but I did not confirm these are
  near-ties (`DEBUG_LOGITS=1` not run). **Open question for the owner**: the issue makes a correctness mismatch a hard no-go.

## Benchmark results (CPU only)

Command shape: `SNAP=<fixture> REF=<fixture>/ref_glm.json REPLAY=1 DRAFT=0 PROF=1 OMP_NUM_THREADS=20 [PIN=<stats> PIN_GB=<n>] colibri.exe`
(`ops/colibri-bench/run_modes.py`). Fixture prompt: 12 tokens, replay decode of 8 tokens. Warmup: 1 run per mode.
Measured: 5 runs per mode, interleaved. Cache state: **warm** (the 1.2 GB fixture fits the page cache; no cold-cache
state was established, so these numbers say nothing about cold storage behavior). TTFT proxy = engine prefill time.
Before each run the script checks for runner worker processes and CPU under 20%; none was found and no wait occurred.

| Volume | Mode | Decode tok/s median (p10 / p90) | Prefill s median |
|---|---|---|---|
| `D:` HDD pool | no pin (LRU streaming) | 8.61 (6.45 / 9.34) | 5.46 |
| `D:` HDD pool | pin 1 GB | 77.29 (75.94 / 77.79) | 0.03 |
| `D:` HDD pool | pin all | 75.91 (75.30 / 77.80) | 0.03 |
| `C:` NVMe | no pin (LRU streaming) | 48.04 (47.47 / 48.52) | 0.26 |
| `C:` NVMe | pin 1 GB | 77.35 (75.95 / 77.83) | 0.03 |
| `C:` NVMe | pin all | 76.49 (76.11 / 76.84) | 0.03 |

Streaming from the HDD pool costs 5.6x decode versus NVMe even on a 1.2 GB model with a 95% hit rate. Pinned modes are
storage-independent because the fixture is fully RAM-resident, so they do not predict the real model.

Telemetry in the raw JSON (kept outside git): system-wide RAM peak ~17.3 GB, commit ~43.6%, GPU 32 C, VRAM in use ~1.1 GB
(not ours), OS-level read ~1.0 GB/s (NVMe) vs ~0.18 GB/s (HDD) during model load. CPU utilization and disk IOPS/latency
were sampled coarsely and are not reported as findings. CPU temperature needs extra tooling and was not recorded.

## Predicted full-model ranges (assumption-based, not measured)

Basis: 372 GB int4 checkpoint, ~11 GB read per cold token (upstream `docs/benchmarks.md`), upstream 25 GB dev box floor
of 0.05-0.1 tok/s cold. This host is a similar RAM class (32 GB).

| Model on | Cold I/O bound | Comment |
|---|---|---|
| `D:` HDD pool | ~0.004 tok/s, ~75 h per 1,024 tokens | Below 0.10 tok/s: no-go band |
| `C:` NVMe | I/O ceiling ~0.4-0.6 tok/s; upstream floor for this RAM class 0.05-0.1 tok/s | Between the bands; needs a real measurement |

## Not run

- CUDA path and CUDA kernel tests: the upstream Windows release is CPU-only (`coli doctor`: "GPU detected but the engine is
  CPU-only") and there is no nvcc to build one. The owner note said the GPU hold was on, but `GET /gpu` at start reported
  `manual:false, state:clear`. I did not toggle it (the owner controls it), and ran no CUDA work.
- `coli tune`: refuses the fixture (no `tokenizer.json`). No measured tuning profile exists.
- `coli doctor`/`plan` ran on the fixture; `doctor` fails only on the missing tokenizer, expected for a synthetic model.
- Cold-cache runs, 256/1,024-token matrix, soak: Phase B only.

## Owner decision needed before Phase B

Not yet requestable. Facts a request would carry: about 372 GB GLM-5.2 int4-gs64 container from upstream (revision and hash
to be pinned before any download; resumability and cleanup to be confirmed in `c/tools/download_glm52.py`, not read in this
phase). Destination candidates: `C:` NVMe (~0.96 TB free now, ~0.59 TB after, OS/pagefile volume) or `D:` HDD pool
(16 TB free, ~40 MB/s, not viable).

## #21 status

`needs owner decision` (leaning no-go if `D:` is the only option). Not marked ready.

## Reproduce

```
python ops/colibri-bench/disk_probe.py <dir> 8
python <colibri-commit>/c/tools/make_glm_bench_model.py --output <fixture> --device cpu
python ops/colibri-bench/run_modes.py <colibri.exe> <fixture> <label> 5
```
