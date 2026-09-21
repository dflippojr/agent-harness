"""Interleaved repeated runs of the Colibri engine on the upstream synthetic fixture (CPU only).
Records per-run decode tok/s, prefill (TTFT proxy) seconds, expert I/O, RAM/commit/pagefile and CPU
telemetry, plus an output hash. Skips a run (bounded wait) while runner workers or other load are present.
usage: python run_modes.py <engine.exe> <model_dir> <label> [runs]
"""
import ctypes, hashlib, json, os, re, statistics, subprocess, sys, time
import psutil


class PERF(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong)] + [(n, ctypes.c_size_t) for n in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable", "SystemCache",
        "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize", "HandleCount", "ProcessCount", "ThreadCount")]


def commit():
    p = PERF(); p.cb = ctypes.sizeof(p)
    ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(p), p.cb)
    return round(p.CommitTotal * p.PageSize / p.CommitLimit / p.PageSize * 100, 1)


def load_ok():
    if any(("Runner.Worker" in (p.info["name"] or "")) for p in psutil.process_iter(["name"])):
        return False, "runner-worker"
    cpu = psutil.cpu_percent(interval=3)
    return cpu < 20, f"cpu{cpu}"


def wait_idle(limit=600):
    t0, seen = time.time(), []
    while True:
        ok, why = load_ok()
        if ok:
            return {"waited_s": round(time.time() - t0), "load_seen": sorted(set(seen))}
        seen.append(why.split("cpu")[0] or "cpu-busy")
        if time.time() - t0 > limit:
            return {"waited_s": round(time.time() - t0), "load_seen": sorted(set(seen)), "measured_under_load": True}
        time.sleep(15)


def gpu():
    o = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,utilization.gpu,clocks_throttle_reasons.active",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip().split(", ")
    return {"gpu_temp_c": int(o[0]), "vram_used_mib": int(o[1]), "gpu_util": int(o[2]), "throttle_mask": o[3]}


def correctness(engine, model, env):
    e = os.environ.copy()
    for k in ("PIN", "PIN_GB", "STATS", "COLI_CUDA", "COLI_GPU"):
        e.pop(k, None)
    e.update(SNAP=model, REF=model + "/ref_glm.json", DRAFT="0", **env)
    out = subprocess.run([engine], env=e, capture_output=True, text=True).stdout
    m = re.search(r"Matching tokens: (\d+)/(\d+)", out)
    toks = re.search(r"GLM C engine\s*: (.*)", out)
    return {"matching": m.group(0) if m else "none", "pass": bool(m and m.group(1) == m.group(2)),
            "token_sha": hashlib.sha256((toks.group(1) if toks else "").encode()).hexdigest()[:12]}


def one(engine, model, env):
    e = os.environ.copy()
    for k in ("PIN", "PIN_GB", "STATS", "COLI_CUDA", "COLI_GPU"):
        e.pop(k, None)
    e.update(SNAP=model, REF=model + "/ref_glm.json", REPLAY="1", DRAFT="0", PROF="1", OMP_PLACES="cores", **env)
    io0, cpu0 = psutil.disk_io_counters(), psutil.cpu_percent(None)
    t0 = time.time()
    p = subprocess.Popen([engine], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak_mem, cpu_s = 0, []
    while p.poll() is None:
        peak_mem = max(peak_mem, psutil.virtual_memory().used)
        cpu_s.append(psutil.cpu_percent(interval=0.5))
    out = p.stdout.read(); dt = time.time() - t0; io1 = psutil.disk_io_counters()
    m = re.search(r"REPLAY decode: (\d+) tokens in ([\d.]+)s \| ([\d.]+) tok/s", out)
    pre = re.findall(r"\+([\d.]+)s", out.split("== GLM")[0])
    fp = re.search(r"expert I/O: ([\d.]+) GB fetched.*?([\d.]+) GB/s", out)
    body = "\n".join(l for l in out.splitlines() if not re.match(r"\[|loaded in|REPLAY|PROFILE|ATTENTION", l))
    return {"rc": p.returncode, "decode_tok_s": float(m.group(3)) if m else None, "decode_tokens": int(m.group(1)) if m else None,
            "prefill_s": float(pre[-1]) if pre else None, "wall_s": round(dt, 1),
            "expert_gb_fetched": float(fp.group(1)) if fp else None,
            "disk_read_mb_s_os": round((io1.read_bytes - io0.read_bytes) / dt / 1e6, 1),
            "ram_used_peak_gb": round(peak_mem / 2**30, 1), "commit_pct": commit(),
            "cpu_util_mean": round(statistics.mean(cpu_s), 1) if cpu_s else None,
            "errors": [l for l in out.splitlines() if re.search("mismatch|ERROR|FAIL|fatal", l, re.I)][:3]}


def pct(v, q):
    v = sorted(v); return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


if __name__ == "__main__":
    engine, model, label = sys.argv[1:4]
    runs = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    stats = model + "/bench_stats.txt"
    base = {"OMP_NUM_THREADS": "20"}
    modes = {"stream_nopin": {}, "pin_1gb": {"PIN": stats, "PIN_GB": "1"}, "pin_all": {"PIN": stats, "PIN_GB": "all"}}
    print("stats-run", one(engine, model, base | {"STATS": stats})["rc"], file=sys.stderr)
    corr = {}
    for k, v in modes.items():
        corr[k] = correctness(engine, model, base | v)
        one(engine, model, base | v)  # warm-up
    res = {k: [] for k in modes}
    names = list(modes)
    for i in range(runs):
        for k in names[i % 3:] + names[:i % 3]:
            load = wait_idle()
            r = one(engine, model, base | modes[k]); r.update(load); r.update(gpu()); res[k].append(r)
    out = {"label": label, "correctness_greedy": corr, "modes": {}}
    for k, rs in res.items():
        d = [r["decode_tok_s"] for r in rs if r["decode_tok_s"]]
        p_ = [r["prefill_s"] for r in rs if r["prefill_s"] is not None]
        out["modes"][k] = {"env": modes[k], "runs": rs, "decode_tok_s": {"median": statistics.median(d), "p10": pct(d, .1), "p90": pct(d, .9)},
                           "prefill_s": {"median": statistics.median(p_), "p10": pct(p_, .1), "p90": pct(p_, .9)} if p_ else None,
                           }
    print(json.dumps(out, indent=1))
