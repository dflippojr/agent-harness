"""#772 speculative-decoding bench: one llama-server container per config on 127.0.0.1:8081, fixed coding prompts.

From the repository root (docs/speculative-decoding-study.md, "Commands"):

    python docs/speculative-decoding-study/spec_bench.py OUT_DIR CONFIG[,CONFIG...] [--prompts all|p1,p2]
        [--repeats N] [--long] [--long-sizes 2000,16000] [--modes profile,greedy]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
SERVE = HERE / "serve.sh"
BASE = "http://127.0.0.1:8081"
sys.path.insert(0, str(HERE.parents[1]))
from bakeoff.perf import build_prompt  # noqa: E402

CONFIGS = {
    "base": [],
    "draft08-n2": ["-md", "/drafters/Qwen3.5-0.8B-Q8_0.gguf", "--spec-type", "draft-simple", "--spec-draft-n-max", "2"],
    "draft08-n4": ["-md", "/drafters/Qwen3.5-0.8B-Q8_0.gguf", "--spec-type", "draft-simple", "--spec-draft-n-max", "4"],
    "draft08-n8": ["-md", "/drafters/Qwen3.5-0.8B-Q8_0.gguf", "--spec-type", "draft-simple", "--spec-draft-n-max", "8"],
    "dflash-n4": ["-md", "/drafters/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf", "--spec-type", "draft-dflash", "--spec-draft-n-max", "4"],
    "dflash-n6": ["-md", "/drafters/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf", "--spec-type", "draft-dflash", "--spec-draft-n-max", "6"],
    "dflash-n15": ["-md", "/drafters/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf", "--spec-type", "draft-dflash", "--spec-draft-n-max", "15"],
    "dspark-n7": ["-md", "/drafters/Qwen3.6-35B-A3B-DSpark-Q8_0.gguf", "--spec-type", "draft-dspark", "--spec-draft-n-max", "7"],
    "dflash-n4-ft2g": ["-md", "/drafters/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf", "--spec-type", "draft-dflash", "--spec-draft-n-max", "4", "--fit-target", "2048"],
    "dflash-n6-ft2g": ["-md", "/drafters/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf", "--spec-type", "draft-dflash", "--spec-draft-n-max", "6", "--fit-target", "2048"],
    "dflash-n15-ft2g": ["-md", "/drafters/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf", "--spec-type", "draft-dflash", "--spec-draft-n-max", "15", "--fit-target", "2048"],
    "dspark-n7-ft2g": ["-md", "/drafters/Qwen3.6-35B-A3B-DSpark-Q8_0.gguf", "--spec-type", "draft-dspark", "--spec-draft-n-max", "7", "--fit-target", "2048"],
    "base-ft2g": ["--fit-target", "2048"],
    "ngram-mod": ["--spec-type", "ngram-mod"],
    "base-64k": ["--ctx-size", "65536"],
    "dflash-n4-ft2g-64k": ["-md", "/drafters/Qwen3.6-35B-A3B-DFlash-Q8_0.gguf", "--spec-type", "draft-dflash", "--spec-draft-n-max", "4", "--fit-target", "2048", "--ctx-size", "65536"],
}

PROMPTS = {
    "duration": "Write a Python function parse_duration(s) that turns strings like '1h30m', '45s' and '2h' into seconds "
                "and raises ValueError on malformed input such as '1hm' or ''. Include pytest tests.",
    "lru": "Implement an LRU cache class in Python with get and put in O(1), without functools. Explain the design briefly.",
    "sql": "Given tables orders(id, customer_id, total, created_at) and customers(id, name, country), write a SQLite query "
           "for the top 5 customers by total spend in 2025 per country, then explain it.",
    "refactor": "Refactor this function to be readable and add type hints:\n\n"
                "def f(d):\n    r=[]\n    for k in d:\n        if d[k]!=None and d[k]>0:\n            r.append((k,d[k]*2))\n"
                "    r.sort(key=lambda x:-x[1])\n    return r\n",
    "bash": "Write a bash script that finds every *.log file under a directory older than 7 days, gzips it, and prints a "
            "summary of how many files and bytes were compressed. Handle spaces in file names.",
    "bugfix": "This Python code is supposed to merge overlapping intervals but returns wrong results. Find the bug and give "
              "the fixed code:\n\ndef merge(iv):\n    iv.sort()\n    out = [iv[0]]\n    for s, e in iv[1:]:\n"
              "        if s < out[-1][1]:\n            out[-1][1] = e\n        else:\n            out.append([s, e])\n"
              "    return out\n",
    "json_cli": "Write a small Python CLI with argparse that reads a CSV file and prints it as a JSON array of objects, with a "
                "--pretty flag. Include a unit test.",
    "regex": "Explain, step by step, what this regular expression matches and give three matching and three non-matching "
             "examples: ^(?:[a-z0-9-]+\\.)+[a-z]{2,}(?::\\d{1,5})?$",
}

SAMPLING = {
    "profile": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
    "greedy": {"temperature": 0.0, "top_k": 1},
}


def gpu_mib() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip().splitlines()[0])


def mem_available_mib() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return -1


def status_mib(status: str, field: str) -> int:
    m = re.search(rf"{field}:\s+(\d+)", status)
    return int(m.group(1)) // 1024 if m else 0


class Sampler:
    """Peak VRAM, peak llama-server RSS (anon/file) and lowest MemAvailable while running."""

    def __init__(self, container: str):
        self.container = container
        self.peak_gpu = self.peak_anon = self.peak_rss = 0
        self.min_avail = None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _pid(self) -> int | None:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", self.container], capture_output=True, text=True)
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() not in ("", "0") else None

    def _run(self) -> None:
        pid = None
        while not self._stop.is_set():
            pid = pid or self._pid()
            try:
                self.peak_gpu = max(self.peak_gpu, gpu_mib())
            except (subprocess.SubprocessError, ValueError):
                pass
            avail = mem_available_mib()
            self.min_avail = avail if self.min_avail is None else min(self.min_avail, avail)
            if pid:
                try:
                    st = Path(f"/proc/{pid}/status").read_text()
                except OSError:
                    pid = None
                else:
                    self.peak_anon = max(self.peak_anon, status_mib(st, "RssAnon"))
                    self.peak_rss = max(self.peak_rss, status_mib(st, "VmRSS"))
            self._stop.wait(1.0)

    def start(self) -> "Sampler":
        self._t.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        self._t.join()
        return {"peak_vram_mib": self.peak_gpu, "peak_server_rss_mib": self.peak_rss,
                "peak_server_anon_mib": self.peak_anon, "min_mem_available_mib": self.min_avail}


def chat(content: str, sampling: dict, max_tokens: int) -> dict:
    r = httpx.post(f"{BASE}/v1/chat/completions", timeout=1800, json={
        "model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens, "cache_prompt": False, **sampling})
    r.raise_for_status()
    t = r.json()["timings"]
    return {"prompt_n": t["prompt_n"], "prompt_ms": round(t["prompt_ms"], 1), "prompt_tps": round(t["prompt_per_second"], 1),
            "gen_n": t["predicted_n"], "gen_tps": round(t["predicted_per_second"], 2),
            "draft_n": t.get("draft_n", 0), "draft_accepted": t.get("draft_n_accepted", 0)}


def fit_line(log: Path) -> str:
    lines = [ln for ln in log.read_text(errors="replace").splitlines() if "common_params_fit_impl:   - CUDA0" in ln]
    return lines[-1].split("):", 1)[-1].strip() if lines else "?"


def run_config(name: str, out: Path, prompts: list[str], repeats: int, modes: list[str],
               long_sizes: tuple[int, ...]) -> dict:
    log = out / f"{name}.server.log"
    container = "ah772-bench"
    before_gpu, before_avail = gpu_mib(), mem_available_mib()
    started = time.monotonic()
    proc = subprocess.Popen([str(SERVE), "bench", str(log), "-lv", "4", *CONFIGS[name]])
    sampler = Sampler(container).start()
    rec: dict = {"config": name, "args": CONFIGS[name], "vram_before_mib": before_gpu, "mem_available_before_mib": before_avail}
    try:
        while True:
            if proc.poll() is not None:
                rec["error"] = f"server exited {proc.returncode}"
                rec["log_tail"] = log.read_text(errors="replace").splitlines()[-15:]
                return rec
            try:
                if httpx.get(f"{BASE}/health", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() - started > 600:
                rec["error"] = "load timeout"
                return rec
            time.sleep(1)
        rec["load_seconds"] = round(time.monotonic() - started, 1)
        rec["vram_after_load_mib"] = gpu_mib()
        rec["fit"] = fit_line(log)
        print(f"[{name}] loaded {rec['load_seconds']}s VRAM {rec['vram_after_load_mib']} fit: {rec['fit']}", flush=True)
        chat("Say hi.", SAMPLING["greedy"], 8)  # warm-up, not recorded
        rows = rec["rows"] = []
        for mode in modes:
            for pid in prompts:
                for r in range(repeats):
                    row = {"prompt": pid, "sampling": mode, "repeat": r, **chat(PROMPTS[pid], SAMPLING[mode], 512)}
                    rows.append(row)
                    print(f"[{name}] {mode:7} {pid:9} gen {row['gen_tps']:6.2f} tok/s n={row['gen_n']} "
                          f"accept {row['draft_accepted']}/{row['draft_n']}", flush=True)
        if long_sizes:
            rec["long"] = []
            for size in long_sizes:
                text, n = build_prompt(BASE, size)
                row = {"target": size, **chat(text + "\n\nWrite a long, detailed summary of the text above.",
                                              SAMPLING["profile"], 200)}
                rec["long"].append(row)
                print(f"[{name}] long {row['prompt_n']} tok: prompt {row['prompt_ms']} ms ({row['prompt_tps']} tok/s) "
                      f"gen {row['gen_tps']}", flush=True)
    except httpx.HTTPError as e:
        rec["error"] = f"request failed: {e!r}"
        rec["log_tail"] = [ln for ln in log.read_text(errors="replace").splitlines() if " E " in ln][-6:]
    finally:
        rec.update(sampler.stop())
        subprocess.run(["docker", "stop", container], capture_output=True)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("configs")
    ap.add_argument("--prompts", default="all")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--long", action="store_true")
    ap.add_argument("--long-sizes", default="2000,16000")
    ap.add_argument("--modes", default="profile,greedy")
    a = ap.parse_args()
    long_sizes = tuple(int(x) for x in a.long_sizes.split(",")) if a.long else ()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prompts = list(PROMPTS) if a.prompts == "all" else a.prompts.split(",")
    for name in a.configs.split(","):
        rec = run_config(name, out, prompts, a.repeats, a.modes.split(","), long_sizes)
        (out / f"{name}.json").write_text(json.dumps(rec, indent=1))
        if "error" in rec:
            print(f"[{name}] ERROR {rec['error']}", *rec.get("log_tail", []), sep="\n", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
