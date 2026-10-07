"""Driver for the #307 Mitsuba prompt-helper study (docs/mitsuba-prompt-helper-study.md).

Research tooling only: serves candidates on the bakeoff port 8081, renders with a *separate* ComfyUI on 8189 through its
HTTP API, and never touches harness config, the always-on server or the images module (workflow() is imported read-only).
Subcommands run one phase each so only one big model is resident at a time. Free RAM is watched in a thread; below
2 GB the servers are killed and the run aborts.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

WORK = Path(os.environ.get("MITSUBA_WORK", "C:/AI/downloads/mitsuba-study"))
RUN = WORK / "run"
FORK = WORK / "fork" / "llama-server.exe"
STOCK = Path("C:/AI/llama.cpp/b10950/llama-server.exe")
MITSUBA = WORK / "Mitsuba-ComfyUI-27B-v1.18-PQ2_0.gguf"
MMPROJ = WORK / "mmproj-Q8_0.gguf"
QWEN = Path("C:/AI/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf")
COMFY_DIR = Path("C:/AI/ComfyUI")
# 8189 is taken by tailscaled on the tower; 8191 is free.
PORT, COMFY_PORT, SEED, CTX = 8081, 8191, 307, 8192
RAM_FLOOR_GB = 2.0

SYSTEM = (
    "You rewrite rough image ideas into one detailed text-to-image prompt for a FLUX model. Output only the final prompt "
    "as a single paragraph, 60 to 120 words. Keep every subject, every piece of quoted text (verbatim, in quotes) and "
    "every layout instruction from the user. Describe subject, setting, lighting, camera or art style and colour. Do not "
    "add explanations, headings, lists or negative prompts.")
PROMPTS = [
    "a lighthouse on a cliff at dusk",
    "corgi astronaut floating in a kitchen",
    'a bakery shop front with a sign that says "Hot Bread"',
    'poster for a jazz night, big title "BLUE HOUR", small line "Fridays 8pm"',
    'a coffee mug with the words "Monday Again" printed on it, on a wooden desk',
    "three red apples in a row on a white table, left one largest, right one smallest",
    "split image: left half a snowy forest, right half a desert at noon",
    "an old woman knitting by a window in the rain",
    "futuristic night market in a flooded city, neon reflections",
    "a watercolor fox sleeping in autumn leaves",
]
DESCRIBE_IDX = [2, 3, 5, 6, 8]          # 0-based: prompts 3, 4, 6, 7, 9
DESCRIBE_ASK = "Describe this image in detail, including any visible text, in one paragraph."

_procs: list[subprocess.Popen] = []
_low = {"min": 1e9}


def avail_gb() -> float:
    class MS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                    ("ap", ctypes.c_ulonglong), ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong),
                    ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
    s = MS()
    s.l = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.ap / 2**30


def vram_mb() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out.splitlines()[0])


def kill_all() -> None:
    for p in _procs:
        if p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    _procs.clear()


def guard() -> None:
    def loop() -> None:
        while True:
            a = avail_gb()
            _low["min"] = min(_low["min"], a)
            if a < RAM_FLOOR_GB:
                print(f"ABORT: free RAM {a:.2f} GB < {RAM_FLOOR_GB} GB", flush=True)
                kill_all()
                os._exit(3)
            time.sleep(0.5)
    threading.Thread(target=loop, daemon=True).start()


def http(method: str, url: str, body: dict | None = None, timeout: float = 600):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    try:
        return json.loads(data)
    except ValueError:
        return data


def start_server(name: str, gpu: bool = True) -> dict:
    exe, model, extra = {
        "mitsuba": (FORK, MITSUBA, ["--mmproj", str(MMPROJ), "--no-mmproj-offload", "--reasoning", "off",
                                    "--cache-type-k", "q4_0", "--cache-type-v", "q4_0", "--load-mode", "none"]),
        # Owner's option (a): spill fewer expert layers to RAM (--fit-target 256, as #170); --load-mode none per #405.
        "qwen": (STOCK, QWEN, ["--fit", "on", "--fit-target", "256", "--cache-type-k", "q8_0", "--cache-type-v",
                               "q8_0", "--load-mode", "none"]),
    }[name]
    args = [str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(PORT), "--flash-attn", "on",
            "--parallel", "1", "--jinja", "-c", str(CTX), "--temperature", "0.6", "--top-k", "20", "--top-p", "0.95",
            *extra]
    if name == "mitsuba":
        args += ["--n-gpu-layers", "99" if gpu else "0"]
    RUN.mkdir(parents=True, exist_ok=True)
    log = open(RUN / f"server-{name}-{'gpu' if gpu else 'cpu'}.log", "wb")
    t0 = time.time()
    p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=str(exe.parent))
    _procs.append(p)
    while time.time() - t0 < 900:
        if p.poll() is not None:
            raise SystemExit(f"{name} server exited {p.returncode}; see {log.name}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit("server did not become healthy")
    load_s = time.time() - t0
    time.sleep(2)
    return {"load_s": round(load_s, 1), "vram_mb": vram_mb(), "avail_ram_gb": round(avail_gb(), 2), "proc": p}


def chat(messages: list[dict], max_tokens: int = 400) -> dict:
    body = {"model": "x", "messages": messages, "max_tokens": max_tokens, "stream": True, "temperature": 0.6,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0, first, n, text = time.time(), None, 0, ""
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            d = json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
            if d:
                if first is None:
                    first = time.time() - t0
                n += 1
                text += d
    total = time.time() - t0
    gen = max(total - (first or 0), 1e-6)
    return {"text": text.strip(), "ttft_s": round(first or total, 2), "total_s": round(total, 2), "chunks": n,
            "tok_per_s": round(max(n - 1, 0) / gen, 1)}


def save(name: str, obj) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    (RUN / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def load(name: str):
    return json.loads((RUN / name).read_text(encoding="utf-8"))


def phase_write(name: str, gpu: bool = True) -> None:
    info = start_server(name, gpu)
    proc = info.pop("proc")
    out = {"load": info, "prompts": []}
    try:
        for i, rough in enumerate(PROMPTS, 1):
            r = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": rough}])
            r.update(rough=rough, n=i)
            out["prompts"].append(r)
            print(i, r["tok_per_s"], "tok/s", r["text"][:90], flush=True)
        out["vram_after_mb"], out["avail_ram_after_gb"] = vram_mb(), round(avail_gb(), 2)
    finally:
        kill_all()
        proc.wait()
    out["min_avail_ram_gb"] = round(_low["min"], 2)
    save(f"write-{name}-{'gpu' if gpu else 'cpu'}.json", out)


def comfy_start() -> subprocess.Popen:
    py = COMFY_DIR / "python_embeded" / "python.exe"
    work = RUN / "comfy"
    for s in ("output", "temp", "input"):
        (work / s).mkdir(parents=True, exist_ok=True)
    args = [str(py), "-s", str(COMFY_DIR / "ComfyUI" / "main.py"), "--listen", "127.0.0.1", "--port", str(COMFY_PORT),
            "--disable-auto-launch", "--extra-model-paths-config",
            str(COMFY_DIR / "ComfyUI" / "extra_model_paths.yaml"), "--output-directory", str(work / "output"),
            "--temp-directory", str(work / "temp"), "--input-directory", str(work / "input")]
    p = subprocess.Popen(args, cwd=str(COMFY_DIR), stdout=open(RUN / "comfy.log", "ab"), stderr=subprocess.STDOUT)
    _procs.append(p)
    for _ in range(300):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{COMFY_PORT}/system_stats", timeout=2)
            return p
        except Exception:
            if p.poll() is not None:
                raise SystemExit("ComfyUI exited; see comfy.log")
            time.sleep(1)
    raise SystemExit("ComfyUI did not start")


def render(prompt: str, tag: str) -> dict:
    from harness_modules.images.service import workflow
    g = workflow("flux-fast", prompt, 1024, 1024, SEED, f"mitsuba307_{tag}")
    base = f"http://127.0.0.1:{COMFY_PORT}"
    t0 = time.time()
    pid = http("POST", f"{base}/prompt", {"prompt": g})["prompt_id"]
    while time.time() - t0 < 600:
        h = http("GET", f"{base}/history/{pid}")
        entry = h.get(pid)
        if entry and entry.get("status", {}).get("status_str") == "error":
            raise SystemExit(f"ComfyUI job failed for {tag}: {entry['status']}")
        if entry and entry.get("outputs"):
            img = entry["outputs"]["9"]["images"][0]
            break
        time.sleep(0.5)
    else:
        raise SystemExit(f"ComfyUI job for {tag} did not finish in 600 s")
    data = http("GET", f"{base}/view?filename={img['filename']}&subfolder={img['subfolder']}&type={img['type']}")
    (RUN / "images").mkdir(exist_ok=True)
    (RUN / "images" / f"{tag}.png").write_bytes(data)
    return {"tag": tag, "seconds": round(time.time() - t0, 1)}


def free_comfy() -> None:
    try:
        http("POST", f"http://127.0.0.1:{COMFY_PORT}/free", {"unload_models": True, "free_memory": True}, 30)
    except Exception:
        pass


def phase_images() -> None:
    b, c = load("write-qwen-gpu.json"), load("write-mitsuba-gpu.json")
    texts = {"a": PROMPTS, "b": [p["text"] for p in b["prompts"]], "c": [p["text"] for p in c["prompts"]]}
    comfy_start()
    times = []
    try:
        for i in range(10):
            for v in "abc":
                r = render(texts[v][i], f"p{i + 1:02d}{v}")
                times.append(r)
                print(r, flush=True)
    finally:
        free_comfy()
        kill_all()
    save("render-times.json", times)


def phase_describe() -> None:
    info = start_server("mitsuba")
    proc = info.pop("proc")
    out = []
    try:
        for i in DESCRIBE_IDX:
            tag = f"p{i + 1:02d}a"
            b64 = base64.b64encode((RUN / "images" / f"{tag}.png").read_bytes()).decode()
            r = chat([{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": DESCRIBE_ASK}]}], 500)
            r.update(image=tag, rough=PROMPTS[i])
            out.append(r)
            print(tag, r["text"][:120], flush=True)
        ram = round(avail_gb(), 2)
        vram = vram_mb()
    finally:
        kill_all()
        proc.wait()
    save("describe.json", {"load": info, "avail_ram_after_gb": ram, "vram_after_mb": vram, "captions": out})


def wait_vram_below(mb: int, limit: float = 60) -> float:
    t0 = time.time()
    while vram_mb() > mb and time.time() - t0 < limit:
        time.sleep(0.2)
    return round(time.time() - t0, 1)


def phase_handover() -> None:
    """Qwen unload -> FLUX cold -> Mitsuba load + one prompt -> unload -> FLUX again, on the GPU path."""
    res = {}
    try:
        info = start_server("qwen")
        proc = info.pop("proc")
        t0 = time.time()
        kill_all()
        proc.wait()
        wait_vram_below(1500)
        res["qwen_unload_s"] = round(time.time() - t0, 1)
        comfy_start()
        render(PROMPTS[0], "warm")
        free_comfy()
        t0 = time.time()
        render(PROMPTS[0], "cold_flux")
        res["flux_cold_s"] = round(time.time() - t0, 1)
        free_comfy()
        kill_all()
        t0 = time.time()
        info = start_server("mitsuba")
        proc = info.pop("proc")
        res["one_prompt"] = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPTS[0]}])
        res["mitsuba_load_s"] = info["load_s"]
        kill_all()
        proc.wait()
        res["mitsuba_load_plus_prompt_s"] = round(time.time() - t0, 1)
        comfy_start()
        t0 = time.time()
        render(PROMPTS[0], "after_mitsuba")
        res["flux_after_mitsuba_s"] = round(time.time() - t0, 1)
        res["added_by_mitsuba_s"] = round(res["mitsuba_load_plus_prompt_s"] + res["flux_after_mitsuba_s"]
                                          - res["flux_cold_s"], 1)
    finally:
        kill_all()
    save("handover.json", res)
    print(res)


def phase_handover_cpu() -> None:
    """CPU offload path: FLUX stays resident while Mitsuba (-ngl 0) writes one prompt, then FLUX renders warm."""
    comfy_start()
    res = {}
    try:
        render(PROMPTS[0], "warm_cpu")
        t0 = time.time()
        render(PROMPTS[0], "flux_warm")
        res["flux_warm_s"] = round(time.time() - t0, 1)
        t0 = time.time()
        info = start_server("mitsuba", gpu=False)
        info.pop("proc")
        res["mitsuba_cpu_load"] = info
        res["one_prompt"] = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPTS[0]}])
        res["mitsuba_cpu_load_plus_prompt_s"] = round(time.time() - t0, 1)
        t0 = time.time()
        render(PROMPTS[0], "flux_warm_after_cpu")
        res["flux_warm_with_mitsuba_cpu_resident_s"] = round(time.time() - t0, 1)
        res["avail_ram_gb"], res["vram_mb"] = round(avail_gb(), 2), vram_mb()
    finally:
        kill_all()
    res["min_avail_ram_gb"] = round(_low["min"], 2)
    save("handover-cpu.json", res)
    print(res)


def main() -> None:
    ap = argparse.ArgumentParser()
    phases = {"write-qwen": lambda: phase_write("qwen"), "write-mitsuba": lambda: phase_write("mitsuba"),
              "write-mitsuba-cpu": lambda: phase_write("mitsuba", gpu=False), "images": phase_images,
              "describe": phase_describe, "handover": phase_handover,
              "handover-cpu": phase_handover_cpu}
    ap.add_argument("phase", choices=sorted(phases))
    ns = ap.parse_args()
    guard()
    try:
        phases[ns.phase]()
    finally:
        kill_all()


if __name__ == "__main__":
    main()
